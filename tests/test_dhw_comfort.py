#!/usr/bin/env python3
"""Regressietests voor de SWW-comfortregeling (dhw_comfort.py).

De beslislogica is puur: venster, temperatuurvoorspelling, goedkoopste
opwarmblok en de setpoint-keuze zijn los testbaar zonder MQTT of netwerk.

Draaien:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import dhw_comfort

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
                "dt_utc": dt.astimezone(timezone.utc),
                "price": p,
                "temp": temp,
                "cop": cop,
                "corrected": p / cop,
            }
        )
    return rows


def _cfg(**over):
    cc = {"mqtt": {"dhw_comfort": dict(over)}} if over else {}
    return dhw_comfort.comfort_cfg(cc)


class TestComfortCfg(unittest.TestCase):
    def test_defaults(self):
        c = dhw_comfort.comfort_cfg({})
        self.assertTrue(c["enabled"])
        self.assertEqual(c["window_from"], "07:00")
        self.assertEqual(c["window_to"], "22:00")
        self.assertEqual(c["min_temp"], 42.0)
        self.assertEqual(c["buffer_temp"], 47.0)
        self.assertEqual(c["charge_temp"], 52.0)
        self.assertEqual(c["off_temp"], 35.0)
        self.assertEqual(c["cooling_c_per_hour"], 0.3)
        self.assertEqual(c["shower_drop_c"], 5.0)
        self.assertTrue(c["allow_outside_window"])

    def test_overrides(self):
        c = _cfg(buffer_temp=48, charge_temp=55, allow_outside_window=False)
        self.assertEqual(c["buffer_temp"], 48.0)
        self.assertEqual(c["charge_temp"], 55.0)
        self.assertFalse(c["allow_outside_window"])


class TestWindow(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def test_in_window_boundaries(self):
        self.assertFalse(dhw_comfort.in_window(_dt(6, 59), self.c))
        self.assertTrue(dhw_comfort.in_window(_dt(7, 0), self.c))
        self.assertTrue(dhw_comfort.in_window(_dt(21, 59), self.c))
        self.assertFalse(dhw_comfort.in_window(_dt(22, 0), self.c))
        self.assertFalse(dhw_comfort.in_window(_dt(3, 0), self.c))

    def test_next_window_before(self):
        s, e = dhw_comfort.next_window(_dt(6, 0), self.c)
        self.assertEqual((s.hour, s.minute), (7, 0))
        self.assertEqual((e.hour, e.minute), (22, 0))
        self.assertEqual(s.day, 11)

    def test_next_window_inside(self):
        s, e = dhw_comfort.next_window(_dt(8, 0), self.c)
        self.assertEqual(s.day, 11)
        self.assertEqual((s.hour, s.minute), (7, 0))

    def test_next_window_after(self):
        s, e = dhw_comfort.next_window(_dt(23, 0), self.c)
        self.assertEqual(s.day, 12)
        self.assertEqual((s.hour, s.minute), (7, 0))


class TestBuildSetpointPayload(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(
            dhw_comfort.build_setpoint_payload(47.0)["values"]["1082"], "01d6"
        )
        self.assertEqual(
            dhw_comfort.build_setpoint_payload(52.0)["values"]["1082"], "0208"
        )
        self.assertEqual(
            dhw_comfort.build_setpoint_payload(42.0)["values"]["1082"], "01a4"
        )
        self.assertEqual(
            dhw_comfort.build_setpoint_payload(40.0)["values"]["1082"], "0190"
        )

    def test_force_response(self):
        self.assertTrue(dhw_comfort.build_setpoint_payload(47.0)["FORCE_RESPONSE"])


class TestFirstShortfall(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def test_crossing_inside_window(self):
        now = _dt(8, 0)
        horizon = now + timedelta(hours=48)
        dl = dhw_comfort.first_shortfall(now, 50.0, self.c, horizon)
        # (50-47)/0,3 = 10 uur -> 18:00
        self.assertEqual(dl, now + timedelta(hours=10))

    def test_before_window_needs_buffer_at_start(self):
        now = _dt(6, 0)
        horizon = now + timedelta(hours=48)
        dl = dhw_comfort.first_shortfall(now, 40.0, self.c, horizon)
        self.assertEqual(dl, _dt(7, 0))

    def test_after_window_looks_at_tomorrow(self):
        now = _dt(23, 0)
        horizon = now + timedelta(hours=48)
        dl = dhw_comfort.first_shortfall(now, 40.0, self.c, horizon)
        self.assertEqual(dl, _dt(7, 0, day=12))

    def test_none_within_short_horizon(self):
        now = _dt(8, 0)
        horizon = now + timedelta(hours=10)
        self.assertIsNone(dhw_comfort.first_shortfall(now, 60.0, self.c, horizon))

    def test_already_below_returns_now(self):
        now = _dt(9, 0)
        horizon = now + timedelta(hours=48)
        self.assertEqual(dhw_comfort.first_shortfall(now, 45.0, self.c, horizon), now)


class TestCheapestChargeBlock(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def test_constant_price_picks_earliest(self):
        now = _dt(8, 0)
        rows = _mk_rows(now, 24, price=0.20)
        b = dhw_comfort.cheapest_charge_block(rows, 15, self.c, now, _dt(12, 0))
        self.assertEqual(b["start"], now)

    def test_varying_price_picks_cheapest(self):
        now = _dt(8, 0)
        rows = _mk_rows(now, 24, price_fn=lambda d: 0.40 if d.hour < 10 else 0.10)
        b = dhw_comfort.cheapest_charge_block(rows, 15, self.c, now, _dt(12, 0))
        self.assertEqual(b["start"], _dt(10, 0))
        self.assertAlmostEqual(b["mean_corrected"], 0.10 / 3.0)

    def test_deadline_too_close_starts_now(self):
        now = _dt(8, 0)
        rows = _mk_rows(now, 24, price=0.20)
        b = dhw_comfort.cheapest_charge_block(rows, 15, self.c, now, now + timedelta(minutes=30))
        self.assertEqual(b["start"], now)
        self.assertIsNone(b["mean_corrected"])

    def test_outside_window_excluded_when_not_allowed(self):
        c = _cfg(allow_outside_window=False)
        now = _dt(23, 0)
        rows = _mk_rows(now, 12, price=0.20)
        b = dhw_comfort.cheapest_charge_block(rows, 15, c, now, _dt(1, 0, day=12))
        self.assertIsNone(b)

    def test_outside_window_allowed(self):
        now = _dt(23, 0)
        rows = _mk_rows(now, 12, price=0.20)
        b = dhw_comfort.cheapest_charge_block(rows, 15, self.c, now, _dt(1, 0, day=12))
        self.assertEqual(b["start"], now)


class TestDetectShower(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def test_shower_detected(self):
        now = _dt(12, 0)
        state = {"last_temp": 47.0, "last_temp_at": (now - timedelta(hours=1)).isoformat()}
        self.assertAlmostEqual(dhw_comfort.detect_shower(state, now, 41.0, self.c), 6.0)

    def test_normal_cooling_not_a_shower(self):
        now = _dt(12, 0)
        state = {"last_temp": 47.0, "last_temp_at": (now - timedelta(hours=1)).isoformat()}
        self.assertIsNone(dhw_comfort.detect_shower(state, now, 46.7, self.c))

    def test_no_previous_sample(self):
        self.assertIsNone(dhw_comfort.detect_shower({}, _dt(12, 0), 41.0, self.c))


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()
        self.now = _dt(8, 0)
        self.rows = _mk_rows(self.now - timedelta(hours=1), 96, price=0.20)

    def test_disabled(self):
        c = _cfg(enabled=False)
        out = dhw_comfort.decide(c, self.now, 45.0, self.rows, 15, {})
        self.assertEqual(out["status"], "disabled")
        self.assertIsNone(out["setpoint_temp"])

    def test_no_read(self):
        out = dhw_comfort.decide(self.c, self.now, None, self.rows, 15, {})
        self.assertEqual(out["status"], "no_read")
        self.assertIsNone(out["setpoint_temp"])

    def test_hot_goes_to_floor(self):
        out = dhw_comfort.decide(self.c, self.now, 52.0, self.rows, 15, {})
        self.assertEqual(out["status"], "hot")
        self.assertEqual(out["setpoint_temp"], self.c["buffer_temp"])

    def test_active_charge_keeps_charging(self):
        state = {
            "charge_start": (self.now - timedelta(minutes=30)).isoformat(),
            "charge_end": (self.now + timedelta(minutes=30)).isoformat(),
        }
        out = dhw_comfort.decide(self.c, self.now, 45.0, self.rows, 15, state)
        self.assertEqual(out["status"], "charge")
        self.assertEqual(out["setpoint_temp"], self.c["charge_temp"])

    def test_below_buffer_charges_now(self):
        out = dhw_comfort.decide(self.c, self.now, 45.0, self.rows, 15, {})
        self.assertEqual(out["status"], "charge")
        self.assertEqual(out["setpoint_temp"], self.c["charge_temp"])
        self.assertEqual(out["deadline"], self.now.isoformat())

    def test_wait_when_deadline_far(self):
        out = dhw_comfort.decide(self.c, self.now, 50.0, self.rows, 15, {})
        self.assertEqual(out["status"], "wait")
        self.assertEqual(out["setpoint_temp"], self.c["buffer_temp"])

    def test_schedule_when_cheapest_block_is_future(self):
        rows = _mk_rows(
            self.now, 20, price_fn=lambda d: 0.40 if d.hour < 10 else 0.10
        )
        out = dhw_comfort.decide(self.c, self.now, 48.0, rows, 15, {})
        self.assertEqual(out["status"], "schedule")
        self.assertEqual(out["setpoint_temp"], self.c["buffer_temp"])
        self.assertEqual(out["block"]["start"], _dt(10, 0).isoformat())

    def test_no_data_keeps_floor(self):
        out = dhw_comfort.decide(self.c, self.now, 45.0, [], 15, {})
        self.assertEqual(out["status"], "no_data")
        self.assertEqual(out["setpoint_temp"], self.c["buffer_temp"])

    def test_no_data_outside_window_uses_off_temp(self):
        out = dhw_comfort.decide(self.c, _dt(23, 0), 45.0, [], 15, {})
        self.assertEqual(out["setpoint_temp"], self.c["off_temp"])


class TestExecute(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()
        self.mqtt = {"enabled": False}  # publiceren mislukt netjes
        self.now = _dt(8, 0)
        self.rows = _mk_rows(self.now, 96, price=0.20)

    def test_dry_run_does_not_publish(self):
        state = {}
        out = dhw_comfort.decide(self.c, self.now, 45.0, self.rows, 15, state)
        dhw_comfort.execute(out, self.mqtt, self.c, state, dry_run=True)
        self.assertFalse(out["mqtt_published"])
        self.assertTrue(out["setpoint_changed"])
        self.assertNotIn("last_sent_setpoint", state)

    def test_unchanged_setpoint_not_republished(self):
        state = {"last_sent_setpoint": self.c["buffer_temp"]}
        out = dhw_comfort.decide(self.c, self.now, 50.0, self.rows, 15, state)
        dhw_comfort.execute(out, self.mqtt, self.c, state, dry_run=True)
        self.assertFalse(out["setpoint_changed"])

    def test_charge_window_saved_when_published(self):
        state = {}
        out = dhw_comfort.decide(self.c, self.now, 45.0, self.rows, 15, state)
        # forceer een 'geslaagde' publicatie door mqtt enabled + publish te mocken
        self.mqtt["enabled"] = True
        orig = dhw_comfort.mqtt_out.publish_command
        dhw_comfort.mqtt_out.publish_command = lambda *a, **k: (True, "bevestigd")
        try:
            dhw_comfort.execute(out, self.mqtt, self.c, state, dry_run=False)
        finally:
            dhw_comfort.mqtt_out.publish_command = orig
        self.assertTrue(out["mqtt_published"])
        self.assertEqual(state["last_sent_setpoint"], self.c["charge_temp"])
        self.assertIn("charge_end", state)


class TestRender(unittest.TestCase):
    def test_render_charge(self):
        c = _cfg()
        now = _dt(8, 0)
        rows = _mk_rows(now, 96, price=0.20)
        out = dhw_comfort.decide(c, now, 45.0, rows, 15, {})
        text = "\n".join(dhw_comfort.render_human(out))
        self.assertIn("SWW-comfort", text)
        self.assertIn("Setpoint", text)

    def test_render_no_read(self):
        c = _cfg()
        out = dhw_comfort.decide(c, _dt(8, 0), None, [], 15, {})
        text = "\n".join(dhw_comfort.render_human(out))
        self.assertIn("gateway", text)


if __name__ == "__main__":
    unittest.main()
