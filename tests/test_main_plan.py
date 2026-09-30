#!/usr/bin/env python3
"""Tests voor het gespreide SWW-boost-plan in main.py (_dhw_boost_plan):
boosts_per_day blokken, telkens min. min_gap_hours ná het einde van de vorige.

Draaien:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import main


def _rows(start_dt: datetime, n: int = 40, price: float = 0.12) -> list:
    """Aaneengesloten 15-minuten-slots met constante prijs: het goedkoopste
    blok is dan eenvoudig het eerste dat aan de randvoorwaarden voldoet."""
    rows = []
    for i in range(n):
        dt = start_dt + timedelta(minutes=15 * i)
        rows.append(
            {
                "dt_local": dt,
                "dt_utc": dt.astimezone(ZoneInfo("UTC")),
                "price": price,
                "temp": 18.0,
                "cop": 3.27,
                "corrected": price / 3.27,
            }
        )
    return rows


class MainDhwPlanTest(unittest.TestCase):
    def test_plan_spaces_second_boost_after_gap(self):
        """Met de default (2x/dag, 4 u gap) moet de tweede boost pas ná
        4 u ná het einde van de eerste starten (niet vlak erna)."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}  # defaults: 2 / 4.0
        plan, per_day, gap = main._dhw_boost_plan(
            cfg, 15, 3, now, _rows(now)
        )
        self.assertEqual(per_day, 2)
        self.assertEqual(gap, 4.0)
        self.assertEqual(len(plan), 2)
        first_end = plan[0]["end"]
        self.assertGreaterEqual(
            plan[1]["start"],
            first_end + timedelta(hours=4),
            "2e boost mag pas ná min_gap_hours ná het einde van de 1e",
        )

    def test_plan_respects_boosts_per_day(self):
        """boosts_per_day=1 in de config -> maar één gepland moment."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {"boosts_per_day": 1}}}
        plan, per_day, _ = main._dhw_boost_plan(cfg, 15, 3, now, _rows(now))
        self.assertEqual(per_day, 1)
        self.assertEqual(len(plan), 1)

    def test_plan_custom_gap(self):
        """min_gap_hours uit de config (bv. 6 u) wordt toegepast."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {"boosts_per_day": 3, "min_gap_hours": 6.0}}}
        plan, per_day, gap = main._dhw_boost_plan(
            cfg, 15, 3, now, _rows(now, n=96)  # 24 u data: 3×3 u + 2×6 u gap
        )
        self.assertEqual(per_day, 3)
        self.assertEqual(gap, 6.0)
        self.assertEqual(len(plan), 3)
        for a, b in zip(plan, plan[1:]):
            self.assertGreaterEqual(b["start"], a["end"] + timedelta(hours=6))

    def test_plan_stops_when_horizon_exhausted(self):
        """Raken de prijsdata op, dan stopt het plan netjes i.p.v. te crashen."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        # slechts 1 blok data (12 slots) -> tweede boost kan er niet meer bij
        plan, _, _ = main._dhw_boost_plan(cfg, 15, 3, now, _rows(now, n=12))
        self.assertEqual(len(plan), 1)

    def _priced_rows(self, start_dt: datetime, prices: list) -> list:
        rows = []
        for i, p in enumerate(prices):
            dt = start_dt + timedelta(minutes=15 * i)
            rows.append(
                {
                    "dt_local": dt,
                    "dt_utc": dt.astimezone(ZoneInfo("UTC")),
                    "price": p,
                    "temp": 18.0,
                    "cop": 3.27,
                    "corrected": p / 3.27,
                }
            )
        return rows

    def test_plan_flags_horizon_bound_block(self):
        """Eindigt een gekozen blok precies op het einde van de prijsdata, dan
        wordt het gemarkeerd als horizon-bound: de goedkopere uren kunnen ná
        de horizon liggen (de watcher herberekent zodra die dag gepubliceerd
        is en verschuift het blok dan eventueel)."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        prices = [0.30] * 12 + [0.05] * 12  # alleen de laatste 3 u zijn goedkoop
        plan, _, _ = main._dhw_boost_plan(cfg, 15, 3, now, self._priced_rows(now, prices))
        self.assertEqual(len(plan), 1)
        self.assertTrue(plan[0]["horizon_bound"])
        self.assertEqual(plan[0]["end"], datetime.fromisoformat("2026-09-30T12:00:00+02:00"))

    def test_plan_not_horizon_bound_when_headroom(self):
        """Eindigt het blok ruim vóór het einde van de data, dan géén markering."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        prices = [0.05] * 12 + [0.30] * 12  # goedkoopste periode = de eerste 3 u
        plan, _, _ = main._dhw_boost_plan(cfg, 15, 3, now, self._priced_rows(now, prices))
        self.assertEqual(len(plan), 1)
        self.assertFalse(plan[0]["horizon_bound"])


if __name__ == "__main__":
    unittest.main()