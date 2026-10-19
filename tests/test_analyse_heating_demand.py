#!/usr/bin/env python3
"""Regressietests voor de nachtanalyse (analyse_heating_demand.py).

Draaien:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

import analyse_heating_demand as analyse


def _row(i, warm, hc1_moves=False):
    # Met hc1_moves gaat de kring 1 sample vóór de verwarming open (zoals een
    # thermostaat die de mengklep/pomp aanstuurt voordat de Remko warmte maakt).
    hc1 = warm and hc1_moves
    return {
        "timestamp": f"2026-10-19T{i:02d}:00:00+02:00",
        "opmode": "6" if warm else "9",
        "heat_water_temp_req": "28.0" if warm else "19.0",
        "compressor_frequency": "40" if warm else "0",
        "el_consumption": "1500.0" if warm else "0.0",
        "hp_flow_temp": "28.0" if warm else "20.0",
        "hp_return_temp": "24.0" if warm else "20.0",
        "hc1_mixer_position": "40" if hc1 else "0",
        "hc1_pump_speed_rel": "50" if hc1 else "0",
        "hc1_flow_rate": "12" if hc1 else "0",
        "hc1_power": "3" if hc1 else "0",
    }


def _rows(warm_from=3, warm_to=7, hc1_moves=False):
    return [_row(i, warm_from <= i <= warm_to, hc1_moves) for i in range(10)]


class TestMoving(unittest.TestCase):
    def test_detects_transitions(self):
        moving = analyse.moving_registers(_rows())
        self.assertIn("heat_water_temp_req", moving)
        self.assertIn("compressor_frequency", moving)
        # hc1 bleef 0 -> geen entries
        self.assertNotIn("hc1_mixer_position", moving)


class TestPeriods(unittest.TestCase):
    def test_one_period(self):
        periods = analyse.find_heating_periods(_rows())
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0]["start"], "2026-10-19T03:00:00+02:00")
        self.assertEqual(periods[0]["end"], "2026-10-19T07:00:00+02:00")

    def test_no_period_when_idle(self):
        self.assertEqual(analyse.find_heating_periods(_rows(99, 99)), [])

    def test_summary_counts_and_hc1(self):
        moving = analyse.moving_registers(_rows(hc1_moves=True))
        s = analyse.summarize_period(
            analyse.find_heating_periods(_rows(hc1_moves=True))[0], moving
        )
        self.assertEqual(s["max_compressor_frequency_hz"], 40.0)
        self.assertEqual(s["max_el_consumption_w"], 1500.0)
        self.assertEqual(s["heat_water_temp_req_max"], 28.0)
        self.assertIn("hc1_pump_speed_rel", s["hc1_moved"])

    def test_summary_without_hc1(self):
        moving = analyse.moving_registers(_rows(hc1_moves=False))
        s = analyse.summarize_period(analyse.find_heating_periods(_rows())[0], moving)
        self.assertEqual(s["hc1_moved"], {})


class TestRender(unittest.TestCase):
    def test_conclusion_negative(self):
        lines = analyse.render_report(_rows(hc1_moves=False), "test.csv")
        text = "\n".join(lines)
        self.assertIn("Verwarmingsperiodes: 1", text)
        self.assertIn("stuurt NIET via de Remko-registers", text)

    def test_conclusion_positive(self):
        lines = analyse.render_report(_rows(hc1_moves=True), "test.csv")
        text = "\n".join(lines)
        self.assertIn("HC1-mengklep/pomp BEWOOG", text)
        self.assertIn("via de HC1-registers", text)

    def test_1972_alone_is_not_proof(self):
        # Alleen heating_circ_mode beweegt (GUI-instelling): geen hard bewijs.
        rows = _rows(hc1_moves=False)
        for r in rows:
            r["heating_circ_mode"] = "1"
        rows[3]["heating_circ_mode"] = "0"
        lines = analyse.render_report(rows, "test.csv")
        text = "\n".join(lines)
        self.assertIn("buiten de gateway om", text)

    def test_no_periods(self):
        lines = analyse.render_report(_rows(99, 99), "test.csv")
        self.assertTrue(any("niet verwarmd" in ln for ln in lines))

    def test_empty(self):
        lines = analyse.render_report([], "test.csv")
        self.assertTrue(any("geen rijen" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
