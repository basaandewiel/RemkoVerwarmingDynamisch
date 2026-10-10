"""Tests voor de automatische fallback tussen prijsbronnen in main.py.

`_fetch_prices_with_fallback` kiest de primaire bron uit de config; faalt die
volledig, dan wordt de reservebron (default: de andere bekende bron)
geprobeerd. Een gebruikte fallback moet altijd zichtbaar zijn (bron + een
waarschuwing) — nooit stilzwijgend overschakelen.

Draaien:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import main

TZ = ZoneInfo("Europe/Amsterdam")

CFG_P = {
    "source": "entsoe",
    "days_ahead": 3,
    "entsoe": {"api_key": "k1", "cache_ttl_seconds": 3600},
    "energyzero": {},
}
LOC = {"timezone": "Europe/Amsterdam"}


def _slots():
    now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    return [(now + timedelta(hours=i), 0.10 + i * 0.01) for i in range(4)]


def _pr(source: str, **extra) -> dict:
    d = {"slots": _slots(), "granularity_min": 60, "source": source}
    d.update(extra)
    return d


class PricesFallbackTest(unittest.TestCase):
    def test_primary_success_no_fallback(self):
        """Primaire bron werkt: geen fallback, geen waarschuwing."""
        with patch.object(main.entsoe, "fetch_prices", return_value=_pr("ENTSO-E x")) as me, \
             patch.object(main.energyzero, "fetch_prices", return_value=_pr("EnergyZero")) as ez:
            pr, used = main._fetch_prices_with_fallback(CFG_P, LOC, 3)
        self.assertEqual(used, "entsoe")
        self.assertEqual(pr["source"], "ENTSO-E x")
        self.assertNotIn("warnings", pr)
        ez.assert_not_called()

    def test_fallback_used_and_marked(self):
        """ENTSO-E faalt, EnergyZero werkt: duidelijk gemarkeerd."""
        with patch.object(main.entsoe, "fetch_prices", side_effect=RuntimeError("kapot")), \
             patch.object(main.energyzero, "fetch_prices", return_value=_pr("EnergyZero")):
            pr, used = main._fetch_prices_with_fallback(CFG_P, LOC, 3)
        self.assertEqual(used, "energyzero")
        self.assertIn("fallback", pr["source"])
        self.assertIn("kapot", pr["warnings"][0])
        self.assertIn("energyzero", pr["warnings"][0])

    def test_fallback_disabled(self):
        """fallback_source=false: geen reservebron, primaire fout komt door."""
        cfg = dict(CFG_P, fallback_source=False)
        with patch.object(main.entsoe, "fetch_prices", side_effect=RuntimeError("kapot")), \
             patch.object(main.energyzero, "fetch_prices") as ez:
            with self.assertRaises(RuntimeError):
                main._fetch_prices_with_fallback(cfg, LOC, 3)
        ez.assert_not_called()

    def test_both_fail_raises_primary_error(self):
        """Beide bronnen stuk: de fout van de primaire (entsoe) wordt gegooid."""
        with patch.object(main.entsoe, "fetch_prices", side_effect=RuntimeError("primair kapot")), \
             patch.object(main.energyzero, "fetch_prices", side_effect=RuntimeError("reserve weg")):
            with self.assertRaises(RuntimeError) as ctx:
                main._fetch_prices_with_fallback(CFG_P, LOC, 3)
        self.assertIn("primair kapot", str(ctx.exception))

    def test_primary_energyzero_falls_back_to_entsoe(self):
        """Symmetrie: source=energyzero, reserve = entsoe."""
        cfg = dict(CFG_P, source="energyzero")
        with patch.object(main.energyzero, "fetch_prices", side_effect=RuntimeError("ez stuk")), \
             patch.object(main.entsoe, "fetch_prices", return_value=_pr("ENTSO-E x")):
            pr, used = main._fetch_prices_with_fallback(cfg, LOC, 3)
        self.assertEqual(used, "entsoe")
        self.assertIn("fallback", pr["source"])

    def test_pinned_fallback_same_as_primary_is_deduplicated(self):
        """fallback_source die gelijk is aan de primaire bron wordt weggehaald."""
        cfg = dict(CFG_P, fallback_source="entsoe")
        with patch.object(main.entsoe, "fetch_prices", side_effect=RuntimeError("kapot")), \
             patch.object(main.energyzero, "fetch_prices") as ez:
            with self.assertRaises(RuntimeError):
                main._fetch_prices_with_fallback(cfg, LOC, 3)
        ez.assert_not_called()

    def test_unknown_source_raises_value_error(self):
        """Onbekende prijsbron: fout zoals voorheen, geen reserves geprobeerd."""
        cfg = dict(CFG_P, source="nordpool")
        with patch.object(main.entsoe, "fetch_prices") as me, \
             patch.object(main.energyzero, "fetch_prices") as ez:
            with self.assertRaises(ValueError):
                main._fetch_prices_with_fallback(cfg, LOC, 3)
        me.assert_not_called()
        ez.assert_not_called()


if __name__ == "__main__":
    unittest.main()