"""Tests voor het ophalen van ENTSO-E-prijzen: caching, retries en het
terugvallen op een oudere cachekopie bij een tijdelijke storing.

De HTTP-laag wordt nagebootst door `_fetch_day_once` te vervangen, zodat de
tests geen netwerk en geen echte sleeps nodig hebben.
"""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
import time as _time
import unittest
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import entsoe

TZ = ZoneInfo("Europe/Amsterdam")
DOMAIN = entsoe.DEFAULT_DOMAIN
DAY = date(2026, 10, 5)


def _expected_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=TZ).astimezone(timezone.utc)


def _slots(day: date, n: int = 4, price: float = 50.0):
    start = _expected_start(day)
    return [
        (start + timedelta(minutes=15 * i), price) for i in range(n)
    ]


class _CacheDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _request(self, cache_ttl=entsoe.DEFAULT_CACHE_TTL):
        return entsoe._request_day(
            "key", DAY, DOMAIN, DOMAIN, _expected_start(DAY),
            cache_dir=self.cache_dir, cache_ttl_seconds=cache_ttl,
        )


class EntsoeFreshCacheTest(_CacheDirTest):
    def test_fetch_then_serve_from_cache(self):
        """Een geslaagde fetch wordt opgeslagen en daarna niet herhaald."""
        with patch.object(entsoe, "_fetch_day_once", return_value=_slots(DAY)) as m:
            first = self._request()
            second = self._request()
        self.assertEqual(len(first), 4)
        self.assertEqual(second, first)
        self.assertEqual(m.call_count, 1, "tweede keer moet uit de cache komen")

    def test_cache_path_written_to_disk(self):
        with patch.object(entsoe, "_fetch_day_once", return_value=_slots(DAY)):
            self._request()
        path = entsoe._day_cache_path(self.cache_dir, DAY, DOMAIN)
        self.assertTrue(os.path.exists(path), "cachebestand moet aangemaakt zijn")


class EntsoeRetryTest(_CacheDirTest):
    """Tijdelijke fouten (HTTP 5xx/599, timeouts) worden herhaald."""

    def setUp(self):
        super().setUp()
        # sleeps overslaan: alleen het aantal pogingen telt
        patcher = patch.object(entsoe._time, "sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_transient_error_then_success(self):
        """Eerste poging 599, daarna succes: het advies komt gewoon door."""
        good = _slots(DAY)
        side_effect = [
            entsoe._TransientDayError("HTTP 599"),
            good,
        ]
        with patch.object(entsoe, "_fetch_day_once", side_effect=side_effect) as m:
            result = self._request()
        self.assertEqual(result, good)
        self.assertEqual(m.call_count, 2)

    def test_all_attempts_fail_raises_transient(self):
        """Blijft het mis, dan een duidelijke (tijdelijke) fout — geen cache."""
        with patch.object(
            entsoe, "_fetch_day_once",
            side_effect=entsoe._TransientDayError("HTTP 527"),
        ) as m:
            with self.assertRaises(entsoe._TransientDayError):
                self._request()
        self.assertEqual(m.call_count, len(entsoe.RETRY_DELAYS) + 1)

    def test_first_attempt_has_no_delay(self):
        """Een gezonde dag mag geen wachttijd kosten: pauze pas ná een fout."""
        sleeps: list = []
        with patch.object(entsoe._time, "sleep", side_effect=sleeps.append):
            with patch.object(entsoe, "_fetch_day_once", return_value=_slots(DAY)):
                self._request()
        self.assertEqual(sleeps, [], "succesvolle eerste poging mag niet slapen")

    def test_non_transient_error_not_retried(self):
        """401/403 is een key-probleem: meteen falen, geen 4× proberen."""
        with patch.object(entsoe, "_fetch_day_once", side_effect=RuntimeError("HTTP 401")) as m:
            with self.assertRaises(RuntimeError) as ctx:
                self._request()
        self.assertEqual(m.call_count, 1)
        self.assertIn("401", str(ctx.exception))


class EntsoeStaleCacheFallbackTest(_CacheDirTest):
    """Bij een storing alsnog de laatst bekende prijzen gebruiken."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(entsoe._time, "sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_cache(self):
        good = _slots(DAY, price=42.0)
        path = entsoe._day_cache_path(self.cache_dir, DAY, DOMAIN)
        entsoe._save_day_cache(path, good)
        # cache is nu ouder dan de TTL -> een verse fetch zou de API raadplegen
        old = _time.time() - 7200
        os.utime(path, (old, old))
        return good

    def test_stale_cache_used_when_api_unreachable(self):
        """Netwerk stuk, cache 2 u oud: alsnog plannen met die prijzen."""
        cached = self._seed_cache()
        with patch.object(
            entsoe, "_fetch_day_once",
            side_effect=entsoe._TransientDayError("The read operation timed out"),
        ) as m:
            result = self._request()
        self.assertEqual(result, cached)
        self.assertEqual(len(result), 4)
        self.assertTrue(m.called, "er is wel geprobeerd te verversen")

    def test_fresh_cache_beats_api(self):
        """Binnen de TTL wordt de API helemaal niet aangeraakt."""
        cached = self._seed_cache()
        path = entsoe._day_cache_path(self.cache_dir, DAY, DOMAIN)
        fresh = _time.time() - 60  # jonger dan de 1 uur TTL
        os.utime(path, (fresh, fresh))
        with patch.object(entsoe, "_fetch_day_once") as m:
            result = self._request()
        self.assertEqual(result, cached)
        m.assert_not_called()


class EntsoeTransientClassificationTest(unittest.TestCase):
    def test_server_and_rate_limit_codes_are_transient(self):
        for code in (500, 502, 503, 504, 520, 527, 599, 408, 429):
            self.assertTrue(entsoe._is_transient_http(code), f"{code} moet tijdelijk zijn")

    def test_auth_codes_are_not_transient(self):
        for code in (400, 401, 403, 404):
            self.assertFalse(entsoe._is_transient_http(code), f"{code} mag niet herhaald worden")


class EntsoeFetchPricesToleranceTest(unittest.TestCase):
    """Eén mislukte dag mag het advies van de andere dagen niet blokkeren."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _fetch(self):
        return entsoe.fetch_prices(
            "key", tz="Europe/Amsterdam", days_ahead=2,
            cache_dir=self.cache_dir, cache_ttl_seconds=0,
        )

    def _day_slot_patch(self, failing_day):
        """_fetch_day_once: faalt voor `failing_day`, slaat de andere dag op."""

        def side_effect(api_key, day, in_domain, out_domain, expected_start):
            if day == failing_day:
                raise entsoe._TransientDayError(f"HTTP 599 voor dag {day}")
            return _slots(day, n=8)

        return patch.object(entsoe, "_fetch_day_once", side_effect=side_effect)

    def test_one_failing_day_still_returns_prices(self):
        today = _dt.datetime.now(TZ).date()
        with patch.object(entsoe._time, "sleep", return_value=None):
            with self._day_slot_patch(today + timedelta(days=1)):
                result = self._fetch()
        self.assertTrue(result["slots"], "er moeten alsnog prijzen zijn")
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("599", result["warnings"][0])

    def test_all_days_failing_raises(self):
        """Als álle dagen mislukken mag het advies niet op niks gebaseerd zijn."""
        with patch.object(entsoe._time, "sleep", return_value=None):
            with patch.object(
                entsoe, "_fetch_day_once",
                side_effect=entsoe._TransientDayError("HTTP 599"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    self._fetch()
        self.assertIn("599", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()