#!/usr/bin/env python3
"""Regressietests voor dhw_boost: de reset naar de default-temperatuur moet
exact op het blokeinde (en anders bij de eerstvolgende wake ná het einde)
worden verstuurd, en de watcher mag nooit een tweede boost op dezelfde dag
sturen.

Draaien:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import dhw_boost
import main

TZ = ZoneInfo("Europe/Amsterdam")
CFG = main.load_config(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"))

# Een verstuurd blok waarvan de reset nog niet gedaan is (last_reset_start
# wijst naar het blok van de dag ervoor).
PENDING_STATE = {
    "last_sent_start": "2026-09-25T12:15:00+02:00",
    "last_sent_end": "2026-09-25T15:15:00+02:00",
    "sent_at": "2026-09-25T12:15:00+02:00",
    "sent_mean_cop": 3.27,
    "sent_mean_corrected": 0.04978720693170235,
    "last_reset_start": "2026-09-24T12:15:00+02:00",
    "reset_at": "2026-09-24T15:15:00+02:00",
}

DONE_STATE = dict(PENDING_STATE, last_reset_start=PENDING_STATE["last_sent_start"])


def _advice(next_block) -> dict:
    """Canned build_advice-resultaat: een SWW-advies met één blok."""
    return {
        "dhw": {
            "enabled": True,
            "rows": [{"dummy": True}],
            "best": next_block,
            "blocks": [next_block] if next_block else [],
        }
    }


class DhwBoostResetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state_file = os.path.join(self._tmp.name, "dhw_boost_state.json")
        patchers = [
            patch.object(dhw_boost, "state_path", return_value=self._state_file),
        ]
        # build_advice vervangen door een fictief advies, zodat tests offline
        # en deterministisch blijven (geen ENTSO-E/met.no-oproepen).
        self._next_block = None

        def fake_advice(_cfg, _args, _now):
            return _advice(self._next_block)

        patchers.append(patch.object(dhw_boost.app, "build_advice", side_effect=fake_advice))
        self._patchers = patchers
        for p in patchers:
            p.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def _write_state(self, state: dict):
        with open(self._state_file, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def test_pending_reset_wakes_exactly_at_block_end(self):
        """Reset open + blok loopt nog -> de watcher moet exact op het blokeinde
        wakker worden, niet doorslapen naar het volgende blok."""
        self._write_state(PENDING_STATE)
        # Het 'volgende blok' begint pas de volgende dag: min(volgende_start-LEAD,
        # refresh) ligt ná het blokeinde — precies de situatie van vorige week.
        self._next_block = {
            "start": datetime.fromisoformat("2026-09-26T03:00:00+02:00"),
            "end": datetime.fromisoformat("2026-09-26T06:00:00+02:00"),
            "mean_cop": 3.0,
            "mean_corrected": 0.05,
        }
        now = datetime.fromisoformat("2026-09-25T13:00:00+02:00")
        out = dhw_boost.decide(CFG, now)
        self.assertEqual(out["status"], "wait")
        self.assertEqual(out["pending_reset_at"], PENDING_STATE["last_sent_end"])
        wake = dhw_boost.next_wake_time(out, now, TZ, "13:30")
        self.assertEqual(
            wake, datetime.fromisoformat("2026-09-25T15:15:00+02:00"),
            "wake moet het blokeinde zijn, niet het volgende blok",
        )

    def test_pending_reset_fires_after_end(self):
        """Na het blokeinde moet decide() 'reset' opleveren met de default-temp."""
        self._write_state(PENDING_STATE)
        self._next_block = None
        out = dhw_boost.decide(CFG, datetime.fromisoformat("2026-09-25T15:16:00+02:00"))
        self.assertEqual(out["status"], "reset")
        self.assertEqual(out["payload"]["values"]["1082"], "0190")  # 40 °C
        # blok-info voor de reset komt uit de statusfile (COP-tonen)
        self.assertEqual(out["block"]["start"], PENDING_STATE["last_sent_start"])

    def test_no_pending_reset_after_reset_done(self):
        """Is de reset al gedaan, dan geen pending wake meer."""
        self._write_state(DONE_STATE)
        self._next_block = {
            "start": datetime.fromisoformat("2026-09-26T03:00:00+02:00"),
            "end": datetime.fromisoformat("2026-09-26T06:00:00+02:00"),
            "mean_cop": 3.0,
            "mean_corrected": 0.05,
        }
        out = dhw_boost.decide(CFG, datetime.fromisoformat("2026-09-25T13:00:00+02:00"))
        self.assertIsNone(out.get("pending_reset_at"))
        self.assertEqual(out["status"], "wait")

    def test_no_second_boost_same_day(self):
        """Blok grenst aan het gebooste blok (zelfde dag): binnen het
        trigger-venster moet decide() 'already_boosted_today' geven, zodat er
        géén tweede boost verstuurd wordt."""
        self._write_state(DONE_STATE)
        adjacent = {
            "start": datetime.fromisoformat("2026-09-25T15:15:00+02:00"),
            "end": datetime.fromisoformat("2026-09-25T18:15:00+02:00"),
            "mean_cop": 3.1,
            "mean_corrected": 0.051,
        }
        self._next_block = adjacent
        # In het trigger-venster van het aangrenzende blok (status zou 'send'
        # zijn zonder daglimiet):
        out = dhw_boost.decide(CFG, datetime.fromisoformat("2026-09-25T15:20:00+02:00"))
        self.assertEqual(out["status"], "already_boosted_today")

    def test_guard_send_when_no_previous_boost(self):
        """Zonder eerdere boost mag er verstuurd worden."""
        self.assertEqual(
            dhw_boost._boost_pending_actions(
                {}, datetime.fromisoformat("2026-09-26T12:45:00+02:00")
            ),
            "send",
        )

    def test_guard_already_sent_same_block(self):
        """Hetzelfde blok staat als verstuurd in de statusfile -> niet opnieuw."""
        start = "2026-09-26T12:45:00+02:00"
        self.assertEqual(
            dhw_boost._boost_pending_actions(
                {"last_sent_start": start}, datetime.fromisoformat(start)
            ),
            "already_sent",
        )

    def test_guard_no_second_boost_same_day_via_state(self):
        """Alleen op statusfile gebaseerd: vandaag al een boost -> niet sturen."""
        self.assertEqual(
            dhw_boost._boost_pending_actions(
                {"last_sent_start": "2026-09-26T09:00:00+02:00"},
                datetime.fromisoformat("2026-09-26T12:45:00+02:00"),
            ),
            "already_boosted_today",
        )

    def test_guard_send_next_day(self):
        """Volgende dag: gewoon weer versturen."""
        self.assertEqual(
            dhw_boost._boost_pending_actions(
                {"last_sent_start": "2026-09-26T09:00:00+02:00"},
                datetime.fromisoformat("2026-09-27T12:45:00+02:00"),
            ),
            "send",
        )

    def test_reset_takes_priority_over_next_block_wake(self):
        """Een openstaande reset moet voorrang hebben op de wake van een
        al aangrenzend volgend blok (geen tweede boost, eerst de reset)."""
        self._write_state(PENDING_STATE)
        adjacent = {
            "start": datetime.fromisoformat("2026-09-25T15:15:00+02:00"),
            "end": datetime.fromisoformat("2026-09-25T18:15:00+02:00"),
            "mean_cop": 3.1,
            "mean_corrected": 0.051,
        }
        self._next_block = adjacent
        out = dhw_boost.decide(CFG, datetime.fromisoformat("2026-09-25T15:13:00+02:00"))
        self.assertEqual(out["status"], "wait")
        wake = dhw_boost.next_wake_time(out, datetime.fromisoformat("2026-09-25T15:13:00+02:00"), TZ, "13:30")
        # vóór de renovatie gaf dit min(aangrenzend_start - LEAD, refresh) =
        # nét ná het blokeinde, exact het moment waarop await de boost zou
        # afvuren. Nu wint de wake voor de reset op het blokeinde.
        self.assertEqual(wake, datetime.fromisoformat("2026-09-25T15:15:00+02:00"))


if __name__ == "__main__":
    unittest.main()