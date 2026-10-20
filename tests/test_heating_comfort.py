#!/usr/bin/env python3
"""Regressietests voor de vloerverwarming-bufferregeling (heating_comfort.py).

De beslislogica is puur: blokplanning op prijs/COP, de setpoint-keuze en het
payload-formaat zijn los testbaar zonder MQTT of netwerk.

Draaien:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import heating_comfort

TZ = ZoneInfo("Europe/Amsterdam")


def _dt(hour, minute=0, day=11):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def _mk_rows(start, n, gran=15, price=0.20, temp=8.0, cop=3.0, price_fn=None):
    rows = []
    for i in range(n):
        dt = start + timedelta(minutes=gran * i)
        p = price_fn(dt) if price_fn else price
        rows.append(
            {
                "dt_local": dt,
                "dt_utc": dt.astimezone(__import__("datetime").timezone.utc),
                "price": p,
                "temp": temp,
                "cop": cop,
                "corrected": p / cop,
            }
        )
    return rows


def _cfg(**over):
    cfg = {"heatpump": {"supply_temperature": 35}, "mqtt": {"heating_comfort": dict(over)}}
    return heating_comfort.heating_cfg(cfg)


class TestHeatingCfg(unittest.TestCase):
    def test_defaults_disabled(self):
        c = _cfg()
        self.assertFalse(c["enabled"])
        self.assertEqual(c["charge_temp"], 40.0)
        # Zonder base_temp in de config komt hij uit het register (1974/5085).
        self.assertTrue(c["base_temp_from_register"])
        self.assertEqual(c["base_temp"], 35.0)  # voorlopige fallback
        self.assertEqual(c["coast_temp"], 28.0)
        self.assertEqual(c["min_supply"], 20.0)
        self.assertEqual(c["max_supply"], 45.0)
        self.assertEqual(c["charge_block_hours"], 3.0)
        self.assertEqual(c["charge_blocks_per_day"], 2)
        self.assertEqual(c["min_gap_hours"], 6.0)
        self.assertEqual(c["coast_block_hours"], 2.0)
        self.assertEqual(c["coast_blocks_per_day"], 1)
        self.assertEqual(c["max_outdoor_for_heating_c"], 15.0)

    def test_base_temp_null_means_register(self):
        c = _cfg(base_temp=None)
        self.assertTrue(c["base_temp_from_register"])

    def test_base_temp_config_uses_heatpump_supply(self):
        c = _cfg(base_temp="config")
        self.assertFalse(c["base_temp_from_register"])
        self.assertEqual(c["base_temp"], 35.0)

    def test_base_temp_from_heatpump_when_register(self):
        cfg = {"heatpump": {"supply_temperature": 45}, "mqtt": {"heating_comfort": {}}}
        c = heating_comfort.heating_cfg(cfg)
        self.assertTrue(c["base_temp_from_register"])
        self.assertEqual(c["base_temp"], 45.0)

    def test_overrides(self):
        c = _cfg(enabled=True, charge_temp=42, base_temp=32, coast_temp=26,
                 charge_blocks_per_day=3, max_outdoor_for_heating_c=12)
        self.assertTrue(c["enabled"])
        self.assertEqual(c["charge_temp"], 42.0)
        self.assertEqual(c["base_temp"], 32.0)
        self.assertFalse(c["base_temp_from_register"])
        self.assertEqual(c["coast_temp"], 26.0)
        self.assertEqual(c["charge_blocks_per_day"], 3)
        self.assertEqual(c["max_outdoor_for_heating_c"], 12.0)


class TestPayload(unittest.TestCase):
    def test_known_values(self):
        p = heating_comfort.build_setpoint_payload(40.0)
        # We schrijven de schrijfbare vaste waarde (1975); 5085 volgt.
        self.assertEqual(p["values"]["1975"], "0190")
        self.assertEqual(heating_comfort.build_setpoint_payload(35.0)["values"]["1975"], "015e")
        self.assertEqual(heating_comfort.build_setpoint_payload(28.0)["values"]["1975"], "0118")
        self.assertEqual(heating_comfort.build_setpoint_payload(24.0)["values"]["1975"], "00f0")
        self.assertEqual(heating_comfort.build_setpoint_payload(45.0)["values"]["1975"], "01c2")

    def test_no_force_response_on_write(self):
        # Een schrijfactie mag GEEN FORCE_RESPONSE bevatten (dat zou een query zijn).
        self.assertNotIn("FORCE_RESPONSE", heating_comfort.build_setpoint_payload(40.0))


class TestClamp(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(charge_temp=99, coast_temp=5)

    def test_clamps_high(self):
        self.assertEqual(heating_comfort.clamp_supply(self.c["charge_temp"], self.c), 45.0)

    def test_clamps_low(self):
        self.assertEqual(heating_comfort.clamp_supply(self.c["coast_temp"], self.c), 20.0)

    def test_within_range(self):
        self.assertEqual(heating_comfort.clamp_supply(37.0, self.c), 37.0)


class TestSelectSpaced(unittest.TestCase):
    def _b(self, h):
        start = datetime(2026, 10, 11, h, tzinfo=ZoneInfo("Europe/Amsterdam"))
        return {"start": start, "end": start + timedelta(hours=3)}

    def test_respects_gap(self):
        blocks = [self._b(8), self._b(10), self._b(20)]
        chosen = heating_comfort._select_spaced(blocks, 5, 6.0)
        # 8:00 en 10:00 liggen te dicht bij elkaar (gat < 6 u); 20:00 mag wel
        self.assertEqual([b["start"].hour for b in chosen], [8, 20])

    def test_max_count(self):
        blocks = [self._b(0), self._b(8), self._b(16)]
        chosen = heating_comfort._select_spaced(blocks, 2, 6.0)
        self.assertEqual(len(chosen), 2)


class TestPlanBlocks(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(enabled=True)
        self.now = _dt(8, 0)

    def test_charge_picks_cheapest(self):
        rows = _mk_rows(
            self.now, 96, price_fn=lambda d: 0.10 if 12 <= d.hour < 15 else 0.40
        )
        blocks = heating_comfort.plan_charge_blocks(rows, 15, self.now, self.c)
        self.assertTrue(blocks)
        # het goedkoopste 3-uursblok begint om 12:00
        self.assertEqual(blocks[0]["start"].hour, 12)

    def test_warm_blocks_filtered(self):
        rows = _mk_rows(self.now, 96, temp=20.0)
        self.assertEqual(heating_comfort.plan_charge_blocks(rows, 15, self.now, self.c), [])

    def test_empty_rows(self):
        self.assertEqual(heating_comfort.plan_charge_blocks([], 15, self.now, self.c), [])


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Europe/Amsterdam")
        self.now = _dt(8, 0)

    def _b(self, h, hours=3):
        s = datetime(2026, 10, 11, h, tzinfo=ZoneInfo("Europe/Amsterdam"))
        return {"start": s, "end": s + timedelta(hours=hours)}

    def test_charge_active(self):
        status, blk = heating_comfort.classify(self.now, [self._b(7)], [])
        self.assertEqual(status, "charge")

    def test_coast_when_no_charge(self):
        status, blk = heating_comfort.classify(self.now, [], [self._b(7)])
        self.assertEqual(status, "coast")

    def test_base(self):
        status, block = heating_comfort.classify(self.now, [self._b(20)], [])
        self.assertEqual(status, "base")
        self.assertIsNone(block)


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(enabled=True)
        self.now = _dt(8, 0)
        self.rows = _mk_rows(self.now, 96, price=0.20, temp=8.0)

    def test_disabled(self):
        out = heating_comfort.decide(_cfg(), self.now, self.rows, 15, {})
        self.assertEqual(out["status"], "disabled")
        self.assertIsNone(out["setpoint_temp"])

    def test_dhw_loading_skips(self):
        # Tijdens SWW-laden (5001 = 4) doet de regeling niets.
        out = heating_comfort.decide(self.c, self.now, self.rows, 15, {},
                                     opmode=heating_comfort.SWW_LOADING_OPMODE)
        self.assertEqual(out["status"], "dhw")
        self.assertIsNone(out["setpoint_temp"])

    def test_dhw_loading_overrides_charge(self):
        # Zelfs midden in een laadblok: SWW gaat vóór.
        rows = _mk_rows(self.now, 96, price_fn=lambda d: 0.10)
        out = heating_comfort.decide(self.c, self.now, rows, 15, {}, opmode=4)
        self.assertEqual(out["status"], "dhw")
        self.assertIsNone(out["setpoint_temp"])

    def test_heating_opmode_proceeds(self):
        out = heating_comfort.decide(self.c, self.now, self.rows, 15, {}, opmode=6)
        self.assertNotEqual(out["status"], "dhw")
        self.assertIsNotNone(out["setpoint_temp"])

    def test_no_opmode_proceeds(self):
        # Onleesbare opmode -> gewoon door (geen valse SWW-detectie).
        out = heating_comfort.decide(self.c, self.now, self.rows, 15, {}, opmode=None)
        self.assertNotEqual(out["status"], "dhw")

    def test_no_data(self):
        out = heating_comfort.decide(self.c, self.now, [], 15, {})
        self.assertEqual(out["status"], "no_data")
        self.assertIsNone(out["setpoint_temp"])

    def test_active_charge(self):
        out = heating_comfort.decide(self.c, self.now, self.rows, 15, {})
        self.assertEqual(out["status"], "charge")
        self.assertEqual(out["setpoint_temp"], self.c["charge_temp"])

    def test_coast_during_expensive_start(self):
        rows = _mk_rows(
            self.now, 96, price_fn=lambda d: 0.60 if d < self.now + timedelta(hours=2) else 0.10
        )
        out = heating_comfort.decide(self.c, self.now, rows, 15, {})
        self.assertEqual(out["status"], "coast")
        self.assertEqual(out["setpoint_temp"], self.c["coast_temp"])

    def test_base_when_warm(self):
        rows = _mk_rows(self.now, 96, price=0.20, temp=20.0)
        out = heating_comfort.decide(self.c, self.now, rows, 15, {})
        self.assertEqual(out["status"], "base")
        self.assertEqual(out["setpoint_temp"], self.c["base_temp"])

    def test_base_has_next_block(self):
        def pf(d):
            if 10 <= d.hour < 12:
                return 0.60
            if 14 <= d.hour < 17:
                return 0.05
            return 0.20

        rows = _mk_rows(self.now, 96, price_fn=pf)
        out = heating_comfort.decide(self.c, self.now, rows, 15, {})
        self.assertEqual(out["status"], "base")
        self.assertIsNotNone(out["next_block"])
        self.assertEqual(out["next_block"]["start"][11:13], "14")


class TestExecute(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(enabled=True)
        self.mqtt = {"enabled": False}
        self.now = _dt(8, 0)
        self.rows = _mk_rows(self.now, 96, price=0.20, temp=8.0)

    def test_dry_run_does_not_publish(self):
        state = {}
        out = heating_comfort.decide(self.c, self.now, self.rows, 15, state)
        heating_comfort.execute(out, self.mqtt, self.c, state, dry_run=True)
        self.assertTrue(out["setpoint_changed"])
        self.assertFalse(out["mqtt_published"])
        self.assertNotIn("last_sent_setpoint", state)

    def test_unchanged_setpoint_not_republished(self):
        state = {"last_sent_setpoint": self.c["charge_temp"]}
        out = heating_comfort.decide(self.c, self.now, self.rows, 15, state)
        heating_comfort.execute(out, self.mqtt, self.c, state, dry_run=True)
        self.assertFalse(out["setpoint_changed"])

    def test_successful_publish_updates_state(self):
        state = {}
        out = heating_comfort.decide(self.c, self.now, self.rows, 15, state)
        self.mqtt["enabled"] = True
        orig = heating_comfort.mqtt_out.publish_command
        heating_comfort.mqtt_out.publish_command = lambda *a, **k: (True, "bevestigd")
        try:
            heating_comfort.execute(out, self.mqtt, self.c, state, dry_run=False)
        finally:
            heating_comfort.mqtt_out.publish_command = orig
        self.assertTrue(out["mqtt_published"])
        self.assertEqual(state["last_sent_setpoint"], self.c["charge_temp"])
        self.assertEqual(out["payload"]["values"]["1975"], "0190")


class TestRender(unittest.TestCase):
    def test_render_charge(self):
        c = _cfg(enabled=True)
        now = _dt(8, 0)
        rows = _mk_rows(now, 96, price=0.20, temp=8.0)
        out = heating_comfort.decide(c, now, rows, 15, {})
        lines = "\n".join(heating_comfort.render_human(out))
        self.assertIn("Vloerverwarming", lines)
        self.assertIn("1975", lines)   # we sturen de schrijfbare vaste waarde

    def test_render_disabled(self):
        out = heating_comfort.decide(_cfg(), _dt(8, 0), [], 15, {})
        lines = "\n".join(heating_comfort.render_human(out))
        self.assertIn("uit", lines)


class TestOpMode(unittest.TestCase):
    """Bedrijfsmodus (5001) lezen en SWW-detectie."""

    def setUp(self):
        self.orig = heating_comfort._query_registers

    def tearDown(self):
        heating_comfort._query_registers = self.orig

    def _fake(self, values):
        heating_comfort._query_registers = lambda *a, **k: dict(values)

    def test_read_opmode(self):
        self._fake({5001: 4})
        self.assertEqual(heating_comfort.read_opmode({}), 4)

    def test_read_opmode_missing(self):
        self._fake({})
        self.assertIsNone(heating_comfort.read_opmode({}))

    def test_is_dhw_loading(self):
        self.assertTrue(heating_comfort.is_dhw_loading(4))
        self.assertFalse(heating_comfort.is_dhw_loading(6))
        self.assertFalse(heating_comfort.is_dhw_loading(None))

    def test_render_dhw(self):
        c = _cfg(enabled=True)
        now = _dt(8, 0)
        rows = _mk_rows(now, 96, price=0.20, temp=8.0)
        out = heating_comfort.decide(c, now, rows, 15, {}, opmode=4)
        lines = "\n".join(heating_comfort.render_human(out))
        self.assertIn("SWW", lines)
        self.assertIn("pauzeert", lines)


class TestMainMode(unittest.TestCase):
    """Aan/uit-regeling via register 1951 (`control = "onoff"`)."""

    def setUp(self):
        self.c = _cfg(enabled=True, control="onoff")
        self.now = _dt(8, 0)

    def test_control_default_supply(self):
        self.assertEqual(_cfg()["control"], heating_comfort.CONTROL_SUPPLY)

    def test_control_onoff_parsed(self):
        self.assertEqual(self.c["control"], heating_comfort.CONTROL_ONOFF)

    def test_control_unknown_falls_back(self):
        self.assertEqual(_cfg(control="banaan")["control"],
                         heating_comfort.CONTROL_SUPPLY)

    def test_payload_format(self):
        self.assertEqual(heating_comfort.build_main_mode_payload("02"),
                         {"values": {"1951": "02"}})

    def test_in_block_is_heat(self):
        rows = _mk_rows(self.now, 96, price_fn=lambda d: 0.10)
        out = heating_comfort.decide(self.c, self.now, rows, 15, {})
        self.assertEqual(out["status"], "charge")
        self.assertEqual(out["main_mode"], heating_comfort.MAIN_MODE_HEAT)
        self.assertIsNone(out["setpoint_temp"])

    def test_outside_block_is_standby(self):
        # Goedkoopste blok pas 's middags: nu standby.
        def pf(d):
            return 0.05 if 13 <= d.hour < 16 else 0.40

        rows = _mk_rows(self.now, 96, price_fn=pf)
        out = heating_comfort.decide(self.c, self.now, rows, 15, {})
        self.assertEqual(out["status"], "standby")
        self.assertEqual(out["main_mode"], heating_comfort.MAIN_MODE_STANDBY)
        self.assertIsNotNone(out["next_block"])

    def test_dhw_loading_forces_standby(self):
        rows = _mk_rows(self.now, 96, price_fn=lambda d: 0.10)
        out = heating_comfort.decide(self.c, self.now, rows, 15, {}, opmode=4)
        self.assertEqual(out["status"], "dhw")
        self.assertEqual(out["main_mode"], heating_comfort.MAIN_MODE_STANDBY)

    def test_no_data_forces_standby(self):
        out = heating_comfort.decide(self.c, self.now, [], 15, {})
        self.assertEqual(out["status"], "no_data")
        self.assertEqual(out["main_mode"], heating_comfort.MAIN_MODE_STANDBY)

    def test_disabled_has_no_mode(self):
        out = heating_comfort.decide(_cfg(control="onoff"), self.now, [], 15, {})
        self.assertEqual(out["status"], "disabled")
        self.assertIsNone(out["main_mode"])

    def test_execute_publishes_main_mode(self):
        c = self.c
        rows = _mk_rows(self.now, 96, price_fn=lambda d: 0.10)
        state = {}
        out = heating_comfort.decide(c, self.now, rows, 15, state)
        heating_comfort.execute(out, {"enabled": True}, c, state, dry_run=True)
        self.assertTrue(out["changed"])
        self.assertEqual(out["payload"]["values"]["1951"], "02")

    def test_execute_unchanged_mode_not_republished(self):
        c = self.c
        rows = _mk_rows(self.now, 96, price_fn=lambda d: 0.10)
        state = {"last_sent_main_mode": "02"}
        out = heating_comfort.decide(c, self.now, rows, 15, state)
        heating_comfort.execute(out, {"enabled": True}, c, state, dry_run=True)
        self.assertFalse(out["changed"])

    def test_execute_updates_state_on_publish(self):
        c = self.c
        rows = _mk_rows(self.now, 96, price_fn=lambda d: 0.10)
        state = {}
        out = heating_comfort.decide(c, self.now, rows, 15, state)
        orig = heating_comfort.mqtt_out.publish_command
        heating_comfort.mqtt_out.publish_command = lambda *a, **k: (True, "ok")
        try:
            heating_comfort.execute(out, {"enabled": True}, c, state, dry_run=False)
        finally:
            heating_comfort.mqtt_out.publish_command = orig
        self.assertEqual(state["last_sent_main_mode"], "02")

    def test_render_onoff(self):
        rows = _mk_rows(self.now, 96, price_fn=lambda d: 0.10)
        out = heating_comfort.decide(self.c, self.now, rows, 15, {})
        lines = "\n".join(heating_comfort.render_human(out))
        self.assertIn("1951", lines)
        self.assertIn("aan/uit", lines)


class TestReadMainMode(unittest.TestCase):
    def setUp(self):
        self.orig = heating_comfort._query_registers

    def tearDown(self):
        heating_comfort._query_registers = self.orig

    def _fake(self, values):
        heating_comfort._query_registers = lambda *a, **k: dict(values)

    def test_reads_hex(self):
        self._fake({1951: 3})
        self.assertEqual(heating_comfort.read_main_mode({}), "03")

    def test_missing(self):
        self._fake({})
        self.assertIsNone(heating_comfort.read_main_mode({}))


class TestDhwExclusion(unittest.TestCase):
    """Blok kiezen dat niet met een SWW-laadvenster botst."""

    def setUp(self):
        self.c = _cfg(enabled=True, control="onoff")
        self.now = _dt(8, 0)

    def test_dhw_charge_window_none_when_empty(self):
        self.assertIsNone(heating_comfort.dhw_charge_window({}))

    def test_dhw_charge_window_parses(self):
        st = {
            "charge_start": "2026-10-11T09:00:00+02:00",
            "charge_end": "2026-10-11T10:00:00+02:00",
        }
        w = heating_comfort.dhw_charge_window(st)
        self.assertIsNotNone(w)
        self.assertEqual(w[0].hour, 9)

    def test_dhw_charge_window_bad(self):
        self.assertIsNone(heating_comfort.dhw_charge_window(
            {"charge_start": "niet-een-tijd"}))

    def test_block_avoids_dhw_window(self):
        # Het goedkoopste blok (08:00) valt samen met het SWW-venster; het
        # op één na goedkoopste (rond 13:00) moet dan gekozen worden.
        def pf(d):
            if 8 <= d.hour < 11:
                return 0.05      # goedkoopst, maar botst met SWW
            if 13 <= d.hour < 16:
                return 0.15
            return 0.40

        rows = _mk_rows(self.now, 96, price_fn=pf)
        window = (_dt(8, 0), _dt(11, 0))
        out = heating_comfort.decide(self.c, self.now, rows, 15, {},
                                     dhw_window=window)
        starts = [b["start"] for b in out["charge_blocks"]]
        self.assertTrue(starts, "er moet een laadblok overblijven")
        for s in starts:
            hour = int(s[11:13])
            self.assertNotIn(hour, (8, 9, 10))

    def test_avoid_dhw_can_be_disabled(self):
        c = _cfg(enabled=True, control="onoff", avoid_dhw=False)
        rows = _mk_rows(self.now, 96, price_fn=lambda d: 0.10)
        window = (_dt(8, 0), _dt(11, 0))
        out = heating_comfort.decide(c, self.now, rows, 15, {}, dhw_window=window)
        # Met mijden uit mag het blok nu wél met SWW samenvallen.
        self.assertEqual(out["status"], "charge")


class TestReadFixedTemp(unittest.TestCase):
    """Register 1975/1974/5085 uitlezen en als rustwaarde gebruiken."""

    def setUp(self):
        self.orig = heating_comfort._query_registers
        self.seen = []

    def tearDown(self):
        heating_comfort._query_registers = self.orig

    def _fake(self, values):
        def impl(_cfg, registers, timeout=0):
            self.seen.append(list(registers))
            return dict(values)
        heating_comfort._query_registers = impl

    def test_prefers_1975(self):
        self._fake({1975: 30.0, 1974: 26.0, 5085: 19.0})
        self.assertEqual(heating_comfort.read_fixed_temp({}), 30.0)

    def test_falls_back_to_1974(self):
        self._fake({1974: 26.0, 5085: 19.0})
        self.assertEqual(heating_comfort.read_fixed_temp({}), 26.0)

    def test_falls_back_to_5085(self):
        self._fake({5085: 27.5})
        self.assertEqual(heating_comfort.read_fixed_temp({}), 27.5)

    def test_ignores_implausible(self):
        self._fake({1975: 300.0, 1974: 300.0, 5085: 0.0})
        self.assertIsNone(heating_comfort.read_fixed_temp({}))

    def test_nothing_readable(self):
        self._fake({})
        self.assertIsNone(heating_comfort.read_fixed_temp({}))

    def test_queries_three_registers(self):
        self._fake({1975: 30.0})
        heating_comfort.read_fixed_temp({})
        self.assertEqual(self.seen[0], [1975, 1974, 5085])

    def test_resolve_uses_register(self):
        self._fake({1975: 28.0})
        c = _cfg(enabled=True)          # base_temp_from_register = True
        state = {}
        got = heating_comfort.resolve_base_temp(c, {}, state)
        self.assertEqual(got, 28.0)
        self.assertEqual(c["base_temp"], 28.0)
        self.assertEqual(state["base_temp"], 28.0)

    def test_resolve_remembers_last(self):
        self._fake({})                 # niets leesbaar
        c = _cfg(enabled=True)
        state = {"base_temp": 26.0}
        got = heating_comfort.resolve_base_temp(c, {}, state)
        self.assertEqual(got, 26.0)
        self.assertEqual(c["base_temp"], 26.0)

    def test_resolve_keeps_config_without_memory(self):
        self._fake({})
        c = _cfg(enabled=True, base_temp=None)
        c["base_temp"] = 33.0
        c["base_temp_from_register"] = True
        got = heating_comfort.resolve_base_temp(c, {}, {})
        self.assertEqual(got, 33.0)

    def test_resolve_skips_when_disabled(self):
        self._fake({1975: 28.0})
        c = _cfg(enabled=True, base_temp=32)
        c["base_temp_from_register"] = False
        got = heating_comfort.resolve_base_temp(c, {}, {})
        self.assertEqual(got, 32.0)
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
