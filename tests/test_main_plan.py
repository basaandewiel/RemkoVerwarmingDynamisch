#!/usr/bin/env python3
"""Tests voor het gespreide SWW-boost-plan in main.py (_dhw_boost_plan).

Het plan volgt de échte staat van de watcher (de nog resterende boosts binnen
het rollend 24-uursvenster, niet opnieuw vanaf nul), en gebruikt dezelfde
beslisregels: min. min_gap_hours ná het einde van de vorige boost, per
kalenderdag één boost (het verplichte dagblok) binnen
afternoon_from–afternoon_to (default 12:00–23:00) zodra er op díe dag nog
geen startte, het LAATSTE blok dat eindigt tussen last_block_end_from en
last_block_end_to (default 19:00–08:00), en élk blok dat start vóór het
ochtend-`last_block_end_to`.

Draaien:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import dhw_boost
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

    def test_plan_spaces_second_boost_after_gap(self):
        """Verse cyclus (geen boost in het venster): blok 1 is vrij (het
        goedkoopste moment, 06:00), blok 2 wordt het verplichte dagvenster-
        blok binnen 12:00-23:00 (12:00) — ruim ná de 4-u-gap ná blok 1."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}  # defaults: 2 / 4.0 / 1 u / 19:00-08:00
        plan, per_day, gap, end_from, end_to, dag_from, dag_to, _ = main._dhw_boost_plan(
            cfg, 15, now, _rows(now, n=56), state={}
        )
        self.assertEqual(per_day, 2)
        self.assertEqual(gap, 4.0)
        self.assertEqual(end_from, "19:00")
        self.assertEqual(end_to, "08:00")
        self.assertEqual((dag_from, dag_to), ("12:00", "23:00"))
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["start"], now, "blok 1 is vrij gekozen")
        self.assertFalse(plan[0]["dagblok"])
        self.assertGreaterEqual(
            plan[1]["start"],
            plan[0]["end"] + timedelta(hours=4),
            "2e boost mag pas ná min_gap_hours ná het einde van de 1e",
        )
        self.assertTrue(plan[1]["dagblok"], "blok 2 is het verplichte dagvenster-blok")
        self.assertEqual(
            plan[1]["start"].hour, 12, "dagvenster-blok binnen 12:00-23:00"
        )

    def test_plan_respects_boosts_per_day(self):
        """boosts_per_day=1 -> maar één gepland moment; vrij, geen dagblok."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {"boosts_per_day": 1}}}
        plan, per_day, _, _, _, _, _, _ = main._dhw_boost_plan(
            cfg, 15, now, _rows(now), state={}
        )
        self.assertEqual(per_day, 1)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["start"], now)  # vrij gekozen, geen venster
        self.assertFalse(plan[0]["dagblok"])

    def test_plan_custom_gap(self):
        """min_gap_hours uit de config (bv. 6 u) wordt toegepast; met 3 boosts
        is er een vrij blok (06:00), een dagvenster-blok (13:00) en een
        laatste blok dat alsnog in het avond-venster eindigt (20:00-21:00)."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {"boosts_per_day": 3, "min_gap_hours": 6.0}}}
        plan, per_day, gap, _, _, _, _, _ = main._dhw_boost_plan(
            cfg, 15, now, _rows(now, n=96), state={}
        )
        self.assertEqual(per_day, 3)
        self.assertEqual(gap, 6.0)
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[0]["start"], now)
        self.assertEqual(plan[1]["start"].hour, 13, "dagvenster-blok")
        self.assertTrue(plan[1]["dagblok"])
        self.assertEqual(plan[2]["start"].hour, 20, "laatste blok in avond-venster")
        self.assertFalse(plan[2]["dagblok"])
        for a, b in zip(plan, plan[1:]):
            self.assertGreaterEqual(b["start"], a["end"] + timedelta(hours=6))

    def test_plan_yesterday_window_boost_does_not_cover_today(self):
        """Dagvenster-dekking is per kalenderdag: de boost van gisteren 20:00
        telt niet meer voor vandaag. Vandaag staat er dus nog een boost, en
        dat is het verplichte dagvenster-blok: het goedkoopste uur in het
        venster vandaag (18:00-19:00). De remaining boost-count blijft op 1
        (recent=1): er komt geen extra derde boost van."""
        now = datetime.fromisoformat("2026-09-30T10:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        state = {
            "last_sent_start": "2026-09-29T20:00:00+02:00",
            "last_sent_end": "2026-09-29T21:00:00+02:00",
            "daily_boosts": {"2026-09-29": ["2026-09-29T20:00:00+02:00"]},
        }
        # 10:00-18:00 duur, alleen 18:00-19:00 goedkoop (einde 19:00).
        prices = [0.30] * 32 + [0.05] * 4 + [0.30] * 8
        plan, _, _, end_from, end_to, _, _, _ = main._dhw_boost_plan(
            cfg, 15, now, self._priced_rows(now, prices), state=state
        )
        self.assertEqual(end_from, "19:00")
        self.assertEqual(end_to, "08:00")
        self.assertEqual(len(plan), 1, "recent=1 -> nog één resterende boost")
        self.assertTrue(plan[0]["dagblok"], "vandaag mist nog een dagvenster-boost")
        start, end = plan[0]["start"], plan[0]["end"]
        self.assertEqual(start, datetime.fromisoformat("2026-09-30T18:00:00+02:00"))
        self.assertGreaterEqual(end, datetime.fromisoformat("2026-09-30T19:00:00+02:00"))
        self.assertLessEqual(end, datetime.fromisoformat("2026-10-01T08:00:00+02:00"))
        self.assertLess(start, datetime.fromisoformat("2026-10-01T08:00:00+02:00"))

    def test_plan_reports_dagvenster_gedekt(self):
        """De 8e return: startte er VANDAAG al een boost binnen het dagvenster,
        dan is de dagvenster-verplichting van vandaag gedekt (flag True) en
        staat er terecht geen dagblok meer in het plan."""
        now = datetime.fromisoformat("2026-09-30T20:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        state = {
            "last_sent_start": "2026-09-30T18:00:00+02:00",
            "last_sent_end": "2026-09-30T19:00:00+02:00",
            "daily_boosts": {"2026-09-30": ["2026-09-30T18:00:00+02:00"]},
        }
        plan, _, _, _, _, _, _, dag_gedekt = main._dhw_boost_plan(
            cfg, 15, now, _rows(now), state=state
        )
        self.assertTrue(dag_gedekt, "boost vandaag 18:00 ligt binnen 12:00-23:00")
        self.assertTrue(plan)
        self.assertFalse(any(b.get("dagblok") for b in plan))

    def test_plan_reports_dagvenster_niet_gedekt(self):
        """Alleen een boost buiten het dagvenster (nacht): flag False en het
        dagblok wordt wél (verplicht) gepland."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        state = {
            "last_sent_start": "2026-09-30T02:00:00+02:00",
            "last_sent_end": "2026-09-30T05:00:00+02:00",
            "daily_boosts": {"2026-09-30": ["2026-09-30T02:00:00+02:00"]},
        }
        plan, _, _, _, _, _, _, dag_gedekt = main._dhw_boost_plan(
            cfg, 15, now, _rows(now), state=state
        )
        self.assertFalse(dag_gedekt, "nachtboost 02:00 ligt buiten 12:00-23:00")
        self.assertTrue(any(b.get("dagblok") for b in plan), "dagblok wél verplicht")

    def test_plan_yesterday_window_boost_still_forces_today(self):
        """Dekking is per kalenderdag: een boost van gisteren 14:00 (binnen
        het venster) telt niet meer voor vandaag. Het plan plant vandaag dus
        wél het verplichte dagblok (12:00), én niet ook nog een derde blok."""
        now = datetime.fromisoformat("2026-09-30T11:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        state = {
            "last_sent_start": "2026-09-29T14:00:00+02:00",
            "last_sent_end": "2026-09-29T15:00:00+02:00",
            "daily_boosts": {"2026-09-29": ["2026-09-29T14:00:00+02:00"]},
        }
        plan, _, _, _, _, _, _, dag_gedekt = main._dhw_boost_plan(
            cfg, 15, now, _rows(now), state=state
        )
        self.assertFalse(dag_gedekt, "boost van gisteren dekt vandaag niet")
        self.assertEqual(len(plan), 1, "recent=1 -> één resterende boost")
        self.assertTrue(plan[0]["dagblok"])
        self.assertEqual(
            plan[0]["start"], datetime.fromisoformat("2026-09-30T12:00:00+02:00")
        )

    def test_plan_last_block_starts_before_next_morning(self):
        """Het resterende blok na een eerdere dagboost blijft in de eerstvolgende
        nacht: start vóór het ochtend-08:00 (23:00, einde 00:00), ook als de
        avond erna goedkoper is (17:00-21:00 op 01-10, buiten de start-grens)."""
        now = datetime.fromisoformat("2026-09-30T20:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        state = {
            "last_sent_start": "2026-09-30T18:00:00+02:00",
            "last_sent_end": "2026-09-30T19:00:00+02:00",
            "daily_boosts": {"2026-09-30": ["2026-09-30T18:00:00+02:00"]},
        }
        # 20:00-23:00 duur, 23:00-08:00 goedkoop (nacht), daarna duur tot
        # 17:00; 01-10 17:00-21:00 nog goedkoper (0.04) maar ná de start-grens.
        prices = (
            [0.30] * 12 + [0.05] * 36 + [0.30] * 36 + [0.04] * 16 + [0.30] * 8
        )
        plan, _, _, _, _, _, _, _ = main._dhw_boost_plan(
            cfg, 15, now, self._priced_rows(now, prices), state=state
        )
        self.assertEqual(len(plan), 1)
        self.assertEqual(
            plan[0]["start"],
            datetime.fromisoformat("2026-09-30T23:00:00+02:00"),
            "laatste blok in de eerstvolgende nacht, niet de goedkopere avond erna",
        )
        self.assertLess(plan[0]["start"], datetime.fromisoformat("2026-10-01T08:00:00+02:00"))
        self.assertNotEqual(
            plan[0]["start"], datetime.fromisoformat("2026-10-01T18:00:00+02:00")
        )

    def test_plan_stops_when_horizon_exhausted(self):
        """Raken de prijsdata op, dan stopt het plan netjes i.p.v. te crashen."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        # slechts 1 blok data (12 slots) -> tweede boost kan er niet meer bij
        plan, _, _, _, _, _, _, _ = main._dhw_boost_plan(
            cfg, 15, now, _rows(now, n=12), state={}
        )
        self.assertEqual(len(plan), 1)

    def test_plan_follows_status_state(self):
        """Zonder state-argument leest het plan de échte statusfile (patched)
        en plant het de RESTERENDE boosts — één nachtboost al gedaan -> nog één
        boost, en dat is het verplichte dagvenster-blok (12:00)."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        gekende = {
            "last_sent_start": "2026-09-30T02:00:00+02:00",
            "last_sent_end": "2026-09-30T05:00:00+02:00",
            "daily_boosts": {"2026-09-30": ["2026-09-30T02:00:00+02:00"]},
        }
        with patch.object(dhw_boost, "load_state", return_value=dict(gekende)):
            plan, per_day, _, _, _, dag_from, dag_to, _ = main._dhw_boost_plan(
                cfg, 15, now, _rows(now)
            )
        self.assertEqual(per_day, 2)
        self.assertEqual(len(plan), 1, "recent=1 -> het dagblok rest nog")
        self.assertEqual(
            plan[0]["start"], datetime.fromisoformat("2026-09-30T12:00:00+02:00")
        )
        self.assertTrue(plan[0]["dagblok"])
        self.assertEqual((dag_from, dag_to), ("12:00", "23:00"))

    def test_plan_at_limit_wakes_window_before_next_block(self):
        """Recent=2 (daglimiet bereikt): het plan toont het blok ná het vrijkomen
        van het venster (oudste boost + 24 u), niet een onmogelijke derde boost.
        De meegegeven state wordt bovendien niet gemuteerd."""
        now = datetime.fromisoformat("2026-09-30T10:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        state = {
            "daily_boosts": {
                "2026-09-29": ["2026-09-29T08:00:00+02:00"],
                "2026-09-30": ["2026-09-30T02:00:00+02:00"],
            },
        }
        plan, _, _, _, _, _, _, _ = main._dhw_boost_plan(
            cfg, 15, now, _rows(now, n=120), state=state
        )
        self.assertEqual(len(state["daily_boosts"]), 2, "input-state niet aanpassen")
        self.assertEqual(len(plan), 1)
        self.assertTrue(plan[0]["dagblok"])
        self.assertGreaterEqual(
            plan[0]["start"],
            datetime.fromisoformat("2026-09-30T08:00:02+02:00"),
            "pas ná het vrijkomen van het venster (oudste boost + 24 u)",
        )

    def test_plan_flags_horizon_bound_block(self):
        """Eindigt een gekozen blok precies op het einde van de prijsdata, dan
        wordt het gemarkeerd als horizon-bound: de goedkopere uren kunnen ná
        de horizon liggen (de watcher herberekent zodra die dag gepubliceerd
        is en verschuift het blok dan eventueel)."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        prices = [0.30] * 8 + [0.05] * 4  # alleen het laatste uur (08:00-09:00) is goedkoop
        plan, _, _, _, _, _, _, _ = main._dhw_boost_plan(
            cfg, 15, now, self._priced_rows(now, prices), state={}
        )
        self.assertEqual(len(plan), 1)
        self.assertTrue(plan[0]["horizon_bound"])
        self.assertEqual(plan[0]["end"], datetime.fromisoformat("2026-09-30T09:00:00+02:00"))

    def test_plan_not_horizon_bound_when_headroom(self):
        """Eindigt het blok ruim vóór het einde van de data, dan géén markering."""
        now = datetime.fromisoformat("2026-09-30T06:00:00+02:00")
        cfg = {"mqtt": {"dhw_boost": {}}}
        prices = [0.05] * 12 + [0.30] * 12  # goedkoopste periode = de eerste 3 u
        plan, _, _, _, _, _, _, _ = main._dhw_boost_plan(
            cfg, 15, now, self._priced_rows(now, prices), state={}
        )
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["start"], now)
        self.assertFalse(plan[0]["horizon_bound"])


if __name__ == "__main__":
    unittest.main()