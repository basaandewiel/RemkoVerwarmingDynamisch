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
        self.assertEqual(c["base_temp"], 35.0)
        self.assertEqual(c["coast_temp"], 28.0)
        self.assertEqual(c["min_supply"], 20.0)
        self.assertEqual(c["max_supply"], 45.0)
        self.assertEqual(c["charge_block_hours"], 3.0)
        self.assertEqual(c["charge_blocks_per_day"], 2)
        self.assertEqual(c["min_gap_hours"], 6.0)
        self.assertEqual(c["coast_block_hours"], 2.0)
        self.assertEqual(c["coast_blocks_per_day"], 1)
        self.assertEqual(c["max_outdoor_for_heating_c"], 15.0)

    def test_base_temp_follows_heatpump_supply(self):
        cfg = {"heatpump": {"supply_temperature": 45}, "mqtt": {"heating_comfort": {}}}
        self.assertEqual(heating_comfort.heating_cfg(cfg)["base_temp"], 45.0)

    def test_overrides(self):
        c = _cfg(enabled=True, charge_temp=42, base_temp=32, coast_temp=26,
                 charge_blocks_per_day=3, max_outdoor_for_heating_c=12)
        self.assertTrue(c["enabled"])
        self.assertEqual(c["charge_temp"], 42.0)
        self.assertEqual(c["base_temp"], 32.0)
        self.assertEqual(c["coast_temp"], 26.0)
        self.assertEqual(c["charge_blocks_per_day"], 3)
        self.assertEqual(c["max_outdoor_for_heating_c"], 12.0)


class TestPayload(unittest.TestCase):
    def test_known_values(self):
        p = heating_comfort.build_setpoint_payload(40.0)
        self.assertEqual(p["values"]["5085"], "0190")
        self.assertEqual(heating_comfort.build_setpoint_payload(35.0)["values"]["5085"], "015e")
        self.assertEqual(heating_comfort.build_setpoint_payload(28.0)["values"]["5085"], "0118")
        self.assertEqual(heating_comfort.build_setpoint_payload(24.0)["values"]["5085"], "00f0")
        self.assertEqual(heating_comfort.build_setpoint_payload(45.0)["values"]["5085"], "01c2")

    def test_force_response(self):
        self.assertTrue(heating_comfort.build_setpoint_payload(40.0)["FORCE_RESPONSE"])


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
        self.assertEqual(out["payload"]["values"]["5085"], "0190")


class TestRender(unittest.TestCase):
    def test_render_charge(self):
        c = _cfg(enabled=True)
        now = _dt(8, 0)
        rows = _mk_rows(now, 96, price=0.20, temp=8.0)
        out = heating_comfort.decide(c, now, rows, 15, {})
        lines = "\n".join(heating_comfort.render_human(out))
        self.assertIn("Vloerverwarming", lines)
        self.assertIn("5085", lines)

    def test_render_disabled(self):
        out = heating_comfort.decide(_cfg(), _dt(8, 0), [], 15, {})
        lines = "\n".join(heating_comfort.render_human(out))
        self.assertIn("uit", lines)


if __name__ == "__main__":
    unittest.main()
