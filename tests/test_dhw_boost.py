#!/usr/bin/env python3
"""Regressietests voor dhw_boost: de reset naar de default-temperatuur moet
exact op het blokeinde (en anders bij de eerstvolgende wake ná het einde)
worden verstuurd, en de watcher mag hooguit `boosts_per_day` boosts binnen
een rollend 24-uursvenster sturen — gespreid (nooit vlak na elkaar), zonder
kalenderdaggrens.

Draaien:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
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

# Eén boost vandaag verstuurd (blok 02:00-05:00) — de daglimiet (2) is dus
# nog niet bereikt en een tweede boost is toegestaan, maar gespreid.
GAP_STATE = {
    "last_sent_start": "2026-09-26T02:00:00+02:00",
    "last_sent_end": "2026-09-26T05:00:00+02:00",
    "sent_at": "2026-09-26T02:00:00+02:00",
    "sent_mean_cop": 3.27,
    "sent_mean_corrected": 0.04978720693170235,
    "last_reset_start": "2026-09-26T02:00:00+02:00",
    "reset_at": "2026-09-26T05:00:00+02:00",
    "daily_boosts": {"2026-09-26": ["2026-09-26T02:00:00+02:00"]},
}


def _rows(start_dt: datetime, n: int = 24, price: float = 0.12) -> list:
    """Aaneengesloten 15-minuten-slots met constante prijs: het goedkoopste
    blok is dan simpelweg het eerste dat aan de randvoorwaarden voldoet."""
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


def _priced_rows(start_dt: datetime, prices: list) -> list:
    """Aaneengesloten 15-minuten-slots met per-slot prijs (voor tests waarin
    een ver-weg goedkoop blok de keuze van de tweede boost op de proef stelt)."""
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


def _advice(rows_from: datetime, n: int = 24, horizon_end=None, price_rows=None) -> dict:
    """Canned build_advice-resultaat: SWW-advies met reeële rijen (zonder
    deze rijen kan _next_boost_block geen blok kiezen). `horizon_end` is het
    uiteinde van de prijsdata (ontbreekt de nieuwe dag, dan reikt die tot
    vandaag). `price_rows` overschrijft de standaard constante-prijs-rijen."""
    return {
        "prices": {"granularity_min": 15, "horizon_end": horizon_end},
        "dhw": {
            "enabled": True,
            "rows": price_rows if price_rows is not None else _rows(rows_from, n),
            "best": None,
            "blocks": [],
        },
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
        self._rows_from = datetime.fromisoformat("2026-09-26T00:00:00+02:00")
        self._n_rows = 24
        # Uiteinde van de prijsdata (None = geen horizon-info -> geen
        # 'late publicatie'-gedrag); per test te overschrijven.
        self._horizon_end = None
        # Optionele per-slot prijzen (anders constante prijs via _n_rows).
        self._prices = None

        def fake_advice(_cfg, _args, _now):
            if self._prices is not None:
                return _advice(
                    self._rows_from,
                    n=len(self._prices),
                    horizon_end=self._horizon_end,
                    price_rows=_priced_rows(self._rows_from, self._prices),
                )
            return _advice(self._rows_from, self._n_rows, horizon_end=self._horizon_end)

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
        out = dhw_boost.decide(CFG, datetime.fromisoformat("2026-09-25T15:16:00+02:00"))
        self.assertEqual(out["status"], "reset")
        self.assertEqual(out["payload"]["values"]["1082"], "0190")  # 40 °C
        # blok-info voor de reset komt uit de statusfile (COP-tonen)
        self.assertEqual(out["block"]["start"], PENDING_STATE["last_sent_start"])

    def test_no_pending_reset_after_reset_done(self):
        """Is de reset al gedaan, dan geen pending wake meer."""
        self._write_state(DONE_STATE)
        out = dhw_boost.decide(CFG, datetime.fromisoformat("2026-09-25T13:00:00+02:00"))
        self.assertIsNone(out.get("pending_reset_at"))
        self.assertEqual(out["status"], "wait")

    def test_rolling_twentyfour_limit_blocks_third_boost(self):
        """boosts_per_day=2 over een rollend 24-uursvenster: 2 boosts in de
        afgelopen 24 u -> geen derde; pas wakker als het oudste blok uit het
        venster valt (oudste start + 24 u), niet al om middernacht."""
        state = dict(
            DONE_STATE,
            daily_boosts={
                "2026-09-25": [
                    "2026-09-25T02:00:00+02:00",
                    "2026-09-25T09:00:00+02:00",
                ],
            },
        )
        self._write_state(state)
        out = dhw_boost.decide(CFG, datetime.fromisoformat("2026-09-25T13:00:00+02:00"))
        self.assertEqual(out["status"], "boost_limit")
        self.assertEqual(out["boosts_recent"], 2)
        self.assertEqual(out["boosts_per_day"], 2)
        wake = dhw_boost.next_wake_time(out, datetime.fromisoformat("2026-09-25T13:00:00+02:00"), TZ, "13:30")
        self.assertEqual(wake, datetime.fromisoformat("2026-09-26T02:00:02+02:00"))

    def test_second_boost_may_be_on_other_day(self):
        """De tweede boost mag op een ándere kalenderdag vallen: boost1 op
        25-09 23:00 (blok t/m 02:00), nu net ná middernacht 26-09 — er is nog
        plaats in het 24-uursvenster, dus de tweede boost wordt gewoon gepland
        (6:00 op 26-09: 4 u gap vanaf het blokeinde)."""
        state = {
            "last_sent_start": "2026-09-25T23:00:00+02:00",
            "last_sent_end": "2026-09-26T02:00:00+02:00",
            "sent_at": "2026-09-25T23:00:00+02:00",
            "last_reset_start": "2026-09-25T23:00:00+02:00",
            "reset_at": "2026-09-26T02:00:00+02:00",
            "daily_boosts": {"2026-09-25": ["2026-09-25T23:00:00+02:00"]},
        }
        self._write_state(state)
        self._rows_from = datetime.fromisoformat("2026-09-26T06:00:00+02:00")
        now = datetime.fromisoformat("2026-09-26T00:30:00+02:00")
        out = dhw_boost.decide(CFG, now)
        self.assertEqual(out["status"], "wait")
        self.assertEqual(out["boosts_recent"], 1)
        # gap vanaf blokeinde (02:00) + 4 u -> vroegste start 06:00 op 26-09
        self.assertEqual(
            out["block"]["start"],
            datetime.fromisoformat("2026-09-26T06:00:00+02:00").isoformat(),
        )

    def test_third_boost_blocked_even_across_midnight(self):
        """Geen kalenderdag meer maatgevend: boost1 (25-09 02:00) en boost2
        (26-09 00:30) liggen allebei binnen 24 u van 'nu' (26-09 01:00) -> een
        derde boost is geblokkeerd óók al is het inmiddels een nieuwe dag."""
        state = dict(
            DONE_STATE,
            daily_boosts={
                "2026-09-25": ["2026-09-25T02:00:00+02:00"],
                "2026-09-26": ["2026-09-26T00:30:00+02:00"],
            },
        )
        self._write_state(state)
        out = dhw_boost.decide(CFG, datetime.fromisoformat("2026-09-26T01:00:00+02:00"))
        self.assertEqual(out["status"], "boost_limit")
        self.assertEqual(out["boosts_recent"], 2)
        # oudste blok in het venster (02:00 25-09) valt uit op 02:00 26-09 + 2 s
        wake = dhw_boost.next_wake_time(out, datetime.fromisoformat("2026-09-26T01:00:00+02:00"), TZ, "13:30")
        self.assertEqual(wake, datetime.fromisoformat("2026-09-26T02:00:02+02:00"))

    def test_second_boost_allowed_but_spaced(self):
        """Eén boost vandaag (02:00-05:00) -> een tweede is toegestaan, maar
        moet pas ná min_gap_hours (4 u) ná het einde beginnen: v.a. 09:00."""
        self._write_state(GAP_STATE)
        self._rows_from = datetime.fromisoformat("2026-09-26T08:00:00+02:00")
        now = datetime.fromisoformat("2026-09-26T06:00:00+02:00")
        out = dhw_boost.decide(CFG, now)
        self.assertEqual(out["status"], "wait")
        self.assertEqual(out["boosts_recent"], 1)
        self.assertEqual(
            out["block"]["start"],
            datetime.fromisoformat("2026-09-26T09:00:00+02:00").isoformat(),
            "2e boost mag niet vlak na de 1e staan (vóór 09:00)",
        )

    def test_no_gap_needed_after_gap_elapsed(self):
        """Lag de vorige boost lang genoeg terug (≥ min_gap_hours), dan mag de
        volgende gewoon het goedkoopste blok vanaf nu zijn (geen kunstmatige
        verdere spreiding)."""
        state = dict(GAP_STATE)
        state["last_sent_start"] = "2026-09-26T09:00:00+02:00"
        state["last_sent_end"] = "2026-09-26T12:00:00+02:00"
        state["last_reset_start"] = "2026-09-26T09:00:00+02:00"
        state["daily_boosts"] = {"2026-09-26": ["2026-09-26T09:00:00+02:00"]}
        self._write_state(state)
        self._rows_from = datetime.fromisoformat("2026-09-26T17:00:00+02:00")
        now = datetime.fromisoformat("2026-09-26T16:45:00+02:00")
        out = dhw_boost.decide(CFG, now)
        self.assertEqual(out["status"], "wait")
        self.assertEqual(
            out["block"]["start"],
            datetime.fromisoformat("2026-09-26T17:00:00+02:00").isoformat(),
            "niet verder doorgeschoven: end+gap (16:00) lag al achter ons",
        )

    def test_second_boost_capped_by_max_gap(self):
        """De 2e boost moet uiterlijk max_gap_hours (12 u) ná de start van de
        1e beginnen. Zonder die bovengrens kiest decide het goedkoopste blok
        'morgenmiddag' (de situatie uit de melding: 2 dagen achter elkaar maar
        1x per dag opgewarmd, omdat de 2e boost telkens naar de volgende dag
        doorschoof) in plaats van een blok binnen de grens."""
        state = {
            "last_sent_start": "2026-09-26T13:15:00+02:00",
            "last_sent_end": "2026-09-26T16:15:00+02:00",
            "sent_at": "2026-09-26T13:15:00+02:00",
            "last_reset_start": "2026-09-26T13:15:00+02:00",
            "reset_at": "2026-09-26T16:15:00+02:00",
            "daily_boosts": {"2026-09-26": ["2026-09-26T13:15:00+02:00"]},
        }
        self._write_state(state)
        cfg = {"mqtt": {"enabled": True, "dhw_boost": {"min_gap_hours": 4.0, "max_gap_hours": 12.0}}}
        # data van 16:15 t/m de volgende dag 16:15; alleen morgen 12:00-15:00
        # is goedkoop (het 'globale optimum' voor de 2e boost, ruim ná de grens
        # 13:15 + 12 u = 01:15) -> de 2e boost moet binnen de grens worden
        # gekozen: het goedkoopste blok v.a. 20:15 (16:15 + 4 u gap).
        self._rows_from = datetime.fromisoformat("2026-09-26T16:15:00+02:00")
        self._prices = [0.30] * 79 + [0.05] * 12 + [0.30] * 5
        now = datetime.fromisoformat("2026-09-26T16:15:00+02:00")
        out = dhw_boost.decide(cfg, now)
        self.assertEqual(out["status"], "wait")
        self.assertEqual(out["boosts_recent"], 1)
        self.assertEqual(out["max_gap_hours"], 12.0)
        cap = datetime.fromisoformat("2026-09-26T13:15:00+02:00") + timedelta(hours=12)
        self.assertLessEqual(
            datetime.fromisoformat(out["block"]["start"]),
            cap,
            "2e boost moet binnen max_gap_hours ná de start van de 1e beginnen",
        )
        self.assertNotEqual(
            out["block"]["start"],
            datetime.fromisoformat("2026-09-27T12:00:00+02:00").isoformat(),
            "het ver-weg goedkoopste blok (morgenmiddag) moet afgewezen worden",
        )
        self.assertEqual(
            out["block"]["start"],
            datetime.fromisoformat("2026-09-26T20:15:00+02:00").isoformat(),
        )

    def test_max_gap_never_clamps_stale_previous_boost(self):
        """Lag de vorige boost lang genoeg terug (≥ max_gap_hours), dan klemt
        de maximale tussenruimte niet: de grens ligt dan al in het verleden en
        de volgende boost is gewoon het goedkoopste blok vanaf nu (geen
        vastloop als een boost ooit gemist wordt)."""
        state = {
            "last_sent_start": "2026-09-25T02:00:00+02:00",
            "last_sent_end": "2026-09-25T05:00:00+02:00",
            "sent_at": "2026-09-25T02:00:00+02:00",
            "last_reset_start": "2026-09-25T02:00:00+02:00",
            "reset_at": "2026-09-25T05:00:00+02:00",
            "daily_boosts": {"2026-09-25": ["2026-09-25T02:00:00+02:00"]},
        }
        self._write_state(state)
        cfg = {"mqtt": {"enabled": True, "dhw_boost": {"min_gap_hours": 4.0, "max_gap_hours": 12.0}}}
        self._rows_from = datetime.fromisoformat("2026-09-26T12:00:00+02:00")
        now = datetime.fromisoformat("2026-09-26T11:50:00+02:00")
        out = dhw_boost.decide(cfg, now)
        self.assertEqual(out["status"], "wait")
        self.assertEqual(out["boosts_recent"], 0)
        self.assertEqual(
            out["block"]["start"],
            datetime.fromisoformat("2026-09-26T12:00:00+02:00").isoformat(),
            "geen clamp: boost vanaf nu, de verouderde start klemt niet",
        )

    def test_wait_log_late_publication_only_when_recheck_is_next(self):
        """De late-publicatie-hint in de watcher-log nóóit tonen als de
        hercontrole níét de eerstvolgende wake is (blokstart/reset gaan
        vóór) — anders belooft de melding een 'hercontrole later' die er
        nooit komt."""
        now = datetime.fromisoformat("2026-09-26T13:31:00+02:00")
        out = {"status": "wait", "wait_minutes": 429.0, "next_day_missing": True}
        # hercontrole is de volgende wake -> hint
        msg = dhw_boost._wait_log(out, now + timedelta(minutes=45), now, 45.0)
        self.assertIn("late publicatie", msg)
        # een andere wake (bv. blokstart) volgt -> gewone melding
        msg2 = dhw_boost._wait_log(out, now + timedelta(seconds=90), now, 45.0)
        self.assertNotIn("late publicatie", msg2)
        self.assertTrue(msg2.startswith("blokstart over"))
        # géén late publicatie -> altijd de gewone melding
        out2 = {"status": "wait", "wait_minutes": 30.0, "next_day_missing": False}
        self.assertNotIn(
            "late publicatie", dhw_boost._wait_log(out2, now + timedelta(minutes=45), now, 45.0)
        )

    def test_late_publication_triggers_recheck_after_refresh(self):
        """Late publicatie: om 13:31 is de nieuwe dag nog niet beschikbaar ->
        de watcher wekt ná --price-recheck-min (default 45 min) opnieuw om te
        herberekenen, i.p.v. pas vlak vóór de blokstart."""
        self._write_state({})
        self._rows_from = datetime.fromisoformat("2026-09-26T15:00:00+02:00")
        # prijsdata reikt alleen tot vanavond -> de nieuwe dag ontbreekt
        self._horizon_end = datetime.fromisoformat("2026-09-26T20:45:00+02:00")
        now = datetime.fromisoformat("2026-09-26T13:31:00+02:00")
        out = dhw_boost.decide(CFG, now)
        self.assertEqual(out["status"], "wait")
        self.assertTrue(out["next_day_missing"])
        wake = dhw_boost.next_wake_time(out, now, TZ, "13:30")
        self.assertEqual(wake, datetime.fromisoformat("2026-09-26T14:16:00+02:00"))

    def test_late_publication_recheck_uses_custom_interval(self):
        """Het hercontrole-interval is instelbaar (--price-recheck-min)."""
        self._write_state({})
        self._rows_from = datetime.fromisoformat("2026-09-26T15:00:00+02:00")
        self._horizon_end = datetime.fromisoformat("2026-09-26T20:45:00+02:00")
        now = datetime.fromisoformat("2026-09-26T13:31:00+02:00")
        out = dhw_boost.decide(CFG, now)
        wake = dhw_boost.next_wake_time(out, now, TZ, "13:30", recheck_min=60.0)
        self.assertEqual(wake, datetime.fromisoformat("2026-09-26T14:31:00+02:00"))

    def test_no_recheck_when_next_day_present(self):
        """Zijn de prijzen van de nieuwe dag er wél, dan geen extra hercontrole:
        normale wake = min(blokstart - LEAD, volgende prijs-update)."""
        self._write_state({})
        self._rows_from = datetime.fromisoformat("2026-09-26T15:00:00+02:00")
        self._horizon_end = datetime.fromisoformat("2026-09-27T23:45:00+02:00")
        now = datetime.fromisoformat("2026-09-26T13:31:00+02:00")
        out = dhw_boost.decide(CFG, now)
        self.assertEqual(out["status"], "wait")
        self.assertFalse(out["next_day_missing"])
        wake = dhw_boost.next_wake_time(out, now, TZ, "13:30")
        self.assertEqual(wake, datetime.fromisoformat("2026-09-26T14:58:00+02:00"))

    def test_no_recheck_outside_late_window_at_night(self):
        """'s Nachts (buiten het venster ná de refresh-tijd) geen extra
        hercontroles: een blok om 03:00 wordt normaal gevolgd, ook al reikt de
        data nog niet tot morgen (morgen komt immers pas om 13:30)."""
        self._write_state({})
        self._rows_from = datetime.fromisoformat("2026-09-26T03:00:00+02:00")
        self._horizon_end = datetime.fromisoformat("2026-09-26T08:45:00+02:00")
        now = datetime.fromisoformat("2026-09-26T00:30:00+02:00")
        out = dhw_boost.decide(CFG, now)
        self.assertEqual(out["status"], "wait")
        self.assertTrue(out["next_day_missing"])
        wake = dhw_boost.next_wake_time(out, now, TZ, "13:30")
        self.assertEqual(wake, datetime.fromisoformat("2026-09-26T02:58:00+02:00"))

    def test_no_recheck_when_block_is_imminent(self):
        """Blok start binnen de recheck-horizon: géén extra wake die de
        geplande trigger kan verstoren — gewoon wakker vóór de start (LEAD)."""
        self._write_state({})
        self._rows_from = datetime.fromisoformat("2026-09-26T14:00:00+02:00")
        self._horizon_end = datetime.fromisoformat("2026-09-26T20:00:00+02:00")
        now = datetime.fromisoformat("2026-09-26T13:31:00+02:00")
        out = dhw_boost.decide(CFG, now)
        self.assertEqual(out["status"], "wait")
        self.assertTrue(out["next_day_missing"])
        wake = dhw_boost.next_wake_time(out, now, TZ, "13:30")
        self.assertEqual(wake, datetime.fromisoformat("2026-09-26T13:58:00+02:00"))

    def test_execute_records_daily_boost(self):
        """Na een geslaagde boost-publicatie wordt het blok in daily_boosts
        geregistreerd, zodat de daglimiet geteld kan worden."""
        self._write_state({})
        out = {
            "status": "send",
            "now": "2026-09-26T12:45:00+02:00",
            "topic": "V04P26/SMTID/CLIENT2HOST",
            "payload": {"FORCE_RESPONSE": True, "values": {"1082": "0212"}},
            "reset_payload": {"FORCE_RESPONSE": True, "values": {"1082": "0190"}},
            "qos": 1,
            "retain": False,
            "block": {
                "start": "2026-09-26T12:45:00+02:00",
                "end": "2026-09-26T15:45:00+02:00",
                "mean_cop": 3.1,
                "mean_corrected_eur_per_kwh_heat": 0.048,
            },
        }
        with patch.object(
            dhw_boost.mqtt_out, "publish_command", return_value=(True, "PUBACK")
        ):
            dhw_boost.execute(out, CFG["mqtt"], dry_run=False, force=False)
        self.assertTrue(out["mqtt_published"])
        state = dhw_boost.load_state()
        self.assertEqual(
            state["daily_boosts"]["2026-09-26"],
            ["2026-09-26T12:45:00+02:00"],
        )
        self.assertEqual(state["last_sent_start"], out["block"]["start"])

    def test_guard_send_when_no_previous_boost(self):
        """Zonder eerdere boost mag er verstuurd worden."""
        self.assertEqual(
            dhw_boost._boost_pending_actions(
                {}, datetime.fromisoformat("2026-09-26T12:45:00+02:00"), 2
            ),
            "send",
        )

    def test_guard_already_sent_same_block(self):
        """Hetzelfde blok staat als verstuurd in de statusfile -> niet opnieuw."""
        start = "2026-09-26T12:45:00+02:00"
        self.assertEqual(
            dhw_boost._boost_pending_actions(
                {"last_sent_start": start}, datetime.fromisoformat(start), 2
            ),
            "already_sent",
        )

    def test_guard_twentyfour_limit_reached_via_state(self):
        """Alleen op statusfile gebaseerd: 24-uurslimiet (2) is bereikt ->
        niet nóg een boost sturen."""
        self.assertEqual(
            dhw_boost._boost_pending_actions(
                {
                    "daily_boosts": {
                        "2026-09-26": [
                            "2026-09-26T02:00:00+02:00",
                            "2026-09-26T09:00:00+02:00",
                        ]
                    }
                },
                datetime.fromisoformat("2026-09-26T12:45:00+02:00"),
                2,
            ),
            "boost_limit",
        )

    def test_guard_second_boost_below_cap(self):
        """Eén boost vandaag (onder de daglimiet): een tweede mag gaan."""
        self.assertEqual(
            dhw_boost._boost_pending_actions(
                {"daily_boosts": {"2026-09-26": ["2026-09-26T02:00:00+02:00"]}},
                datetime.fromisoformat("2026-09-26T12:45:00+02:00"),
                2,
            ),
            "send",
        )

    def test_guard_send_next_day(self):
        """Volgende dag: gewoon weer versturen."""
        self.assertEqual(
            dhw_boost._boost_pending_actions(
                {"last_sent_start": "2026-09-26T09:00:00+02:00"},
                datetime.fromisoformat("2026-09-27T12:45:00+02:00"),
                2,
            ),
            "send",
        )

    def test_reset_takes_priority_over_next_block_wake(self):
        """Een openstaande reset moet voorrang hebben op de wake van een
        al aangrenzend volgend blok (geen tweede boost, eerst de reset)."""
        self._write_state(PENDING_STATE)
        now = datetime.fromisoformat("2026-09-25T15:13:00+02:00")
        out = dhw_boost.decide(CFG, now)
        self.assertEqual(out["status"], "wait")
        wake = dhw_boost.next_wake_time(out, now, TZ, "13:30")
        # vóór de renovatie gaf dit min(aangrenzend_start - LEAD, refresh) =
        # nét ná het blokeinde, exact het moment waarop await de boost zou
        # afvuren. Nu wint de wake voor de reset op het blokeinde.
        self.assertEqual(wake, datetime.fromisoformat("2026-09-25T15:15:00+02:00"))


if __name__ == "__main__":
    unittest.main()