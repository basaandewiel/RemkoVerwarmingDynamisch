#!/usr/bin/env python3
"""Regressietests voor de energie-logger (energy_log.py).

De pure helpers (decoderen, payload parsen, topic afleiden, episode-detectie,
CSV wegschrijven/lezen) zijn los testbaar zonder MQTT-broker.

Draaien:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import energy_log


class TestDecode(unittest.TestCase):
    def test_temp_positive(self):
        self.assertAlmostEqual(energy_log.decode("temp", "00EB"), 23.5)

    def test_temp_negative(self):
        # 0xFFF7 = -9 (signed 16-bit) -> -0.9 °C
        self.assertAlmostEqual(energy_log.decode("temp", "FFF7"), -0.9)

    def test_temp_zero(self):
        self.assertAlmostEqual(energy_log.decode("temp", "0000"), 0.0)

    def test_energy_is_plain_int(self):
        self.assertEqual(energy_log.decode("energy", "7B1D"), 0x7B1D)
        self.assertEqual(energy_log.decode("energy", "EBD9"), 0xEBD9)

    def test_mode_and_counter(self):
        self.assertEqual(energy_log.decode("mode", "04"), 4)
        self.assertEqual(energy_log.decode("counter", "1A2B"), 0x1A2B)


class TestParseValues(unittest.TestCase):
    def test_known_registers(self):
        payload = (
            '{"values": {"5105": "7B1D", "5376": "36E5", "5001": "04", '
            '"5032": "0064", "9999": "FFFF"}}'
        )
        out = energy_log.parse_values(payload)
        self.assertEqual(out[5105], 0x7B1D)
        self.assertEqual(out[5376], 0x36E5)
        self.assertEqual(out[5001], 4)
        self.assertAlmostEqual(out[5032], 10.0)
        self.assertNotIn(9999, out)  # onbekend register genegeerd

    def test_invalid_json(self):
        self.assertEqual(energy_log.parse_values("niet-json"), {})

    def test_missing_values_key(self):
        self.assertEqual(energy_log.parse_values('{"FORCE_RESPONSE": true}'), {})

    def test_bad_hex_skipped(self):
        out = energy_log.parse_values('{"values": {"5105": "XYZ", "5376": "0001"}}')
        self.assertNotIn(5105, out)
        self.assertEqual(out[5376], 1)


class TestTopics(unittest.TestCase):
    def test_data_topic_from_control_topic(self):
        cfg = {"control_topic": "V04P26/SMTID/CLIENT2HOST"}
        self.assertEqual(energy_log.data_topic(cfg), "V04P26/SMTID/HOST2CLIENT")

    def test_data_topic_explicit(self):
        cfg = {
            "control_topic": "V04P26/SMTID/CLIENT2HOST",
            "data_topic": "custom/HOST2CLIENT",
        }
        self.assertEqual(energy_log.data_topic(cfg), "custom/HOST2CLIENT")

    def test_data_topic_from_topic_base(self):
        cfg = {"topic_base": "V04P26/SMTID/CLIENT2HOST"}
        self.assertEqual(energy_log.data_topic(cfg), "V04P26/SMTID/HOST2CLIENT")

    def test_command_topic_prefers_control_topic(self):
        cfg = {"topic_base": "x/y", "control_topic": "V04P26/SMTID/CLIENT2HOST"}
        self.assertEqual(energy_log.command_topic(cfg), "V04P26/SMTID/CLIENT2HOST")


class TestBuildQuery(unittest.TestCase):
    def test_without_handshake(self):
        q = energy_log.build_query([5105, 5376])
        self.assertTrue(q["FORCE_RESPONSE"])
        self.assertEqual(q["query_list"], [5105, 5376])
        self.assertNotIn("values", q)

    def test_with_handshake(self):
        q = energy_log.build_query([5105], include_handshake=True)
        self.assertEqual(q["values"], energy_log.HANDSHAKE)


class TestCsvColumns(unittest.TestCase):
    def test_subset_keeps_field_order(self):
        cols = energy_log.csv_columns([5376, 5105])
        self.assertEqual(cols[0], "timestamp")
        self.assertEqual(cols[1], "energy_electric_kwh")
        self.assertEqual(cols[2], "energy_dhw_kwh")

    def test_price_column_appended_last(self):
        cols = energy_log.csv_columns([5105], include_price=True)
        self.assertEqual(cols[-1], energy_log.PRICE_FIELD)
        self.assertNotIn(energy_log.PRICE_FIELD, energy_log.csv_columns([5105]))


class TestCsvRoundTrip(unittest.TestCase):
    def test_append_and_read(self):
        cols = energy_log.csv_columns([5105, 5376])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "e.csv")
            energy_log.append_row(path, cols, {"timestamp": "2026-10-11T12:00:00+02:00",
                                               "energy_electric_kwh": 200.0,
                                               "energy_dhw_kwh": 100.0})
            energy_log.append_row(path, cols, {"timestamp": "2026-10-11T12:30:00+02:00",
                                               "energy_electric_kwh": 200.4,
                                               "energy_dhw_kwh": 101.0})
            rows = energy_log.read_rows(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["energy_dhw_kwh"], "100.0")
            self.assertEqual(rows[1]["timestamp"], "2026-10-11T12:30:00+02:00")

    def test_header_change_rotates_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "e.csv")
            c1 = energy_log.csv_columns([5105, 5376])
            energy_log.append_row(path, c1, {"timestamp": "t0",
                                             "energy_electric_kwh": 1.0,
                                             "energy_dhw_kwh": 1.0})
            # log_price aan: kolomset verandert -> oud bestand opzij (.bak)
            c2 = energy_log.csv_columns([5105, 5376], include_price=True)
            energy_log.append_row(path, c2, {"timestamp": "t1",
                                             "energy_electric_kwh": 2.0,
                                             "energy_dhw_kwh": 2.0,
                                             energy_log.PRICE_FIELD: 0.1})
            rows = energy_log.read_rows(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(list(rows[0].keys()), c2)
            baks = [f for f in os.listdir(d) if f.endswith(".bak")]
            self.assertEqual(len(baks), 1)


class TestPriceBook(unittest.TestCase):
    def _book(self):
        tz = ZoneInfo("Europe/Amsterdam")
        book = energy_log.PriceBook()
        s0 = datetime(2026, 10, 11, 12, 0, tzinfo=tz)
        s1 = datetime(2026, 10, 11, 13, 0, tzinfo=tz)
        s2 = datetime(2026, 10, 11, 14, 0, tzinfo=tz)
        book.set_slots([(s2, 0.30), (s0, 0.10), (s1, 0.20)], "test")
        return book, s0, s1, s2

    def test_price_at_picks_starting_slot(self):
        book, s0, s1, s2 = self._book()
        self.assertAlmostEqual(book.price_at(s0), 0.10)
        self.assertAlmostEqual(book.price_at(s1 + timedelta(minutes=5)), 0.20)
        # na het laatste slot blijft de laatste prijs gelden
        self.assertAlmostEqual(book.price_at(s2 + timedelta(hours=2)), 0.30)

    def test_price_at_before_window_is_none(self):
        book, s0, _, _ = self._book()
        self.assertIsNone(book.price_at(s0 - timedelta(minutes=1)))

    def test_empty_book(self):
        self.assertIsNone(energy_log.PriceBook().price_at(datetime.now()))


def _row(ts, dhw, el, out=None):
    r = {"timestamp": ts, "energy_dhw_kwh": str(dhw), "energy_electric_kwh": str(el)}
    if out is not None:
        r["out_temp_c"] = str(out)
    return r


def _rowp(ts, dhw, el, price, setpoint):
    r = _row(ts, dhw, el)
    r["price_eur_per_kwh"] = str(price)
    r["water_temp_req_c"] = str(setpoint)
    return r


class TestSummarizeEpisodes(unittest.TestCase):
    def _series(self):
        # 12:00 -> 13:00 actief (dhw +2), dan 3 vlakke samples (idle > max_gap),
        # dan 14:30 -> 15:30 actief (dhw +2).
        return [
            _row("2026-10-11T12:00:00+02:00", 100.0, 200.0, 9.0),
            _row("2026-10-11T12:30:00+02:00", 101.0, 200.4, 9.0),
            _row("2026-10-11T13:00:00+02:00", 102.0, 200.8, 9.0),
            _row("2026-10-11T13:30:00+02:00", 102.0, 200.8, 9.0),
            _row("2026-10-11T14:00:00+02:00", 102.0, 200.8, 9.0),
            _row("2026-10-11T14:30:00+02:00", 102.0, 200.8, 9.0),
            _row("2026-10-11T15:00:00+02:00", 103.0, 201.3, 8.0),
            _row("2026-10-11T15:30:00+02:00", 104.0, 201.8, 8.0),
        ]

    def test_two_episodes(self):
        eps = energy_log.summarize_episodes(self._series())
        self.assertEqual(len(eps), 2)

        first = eps[0]
        self.assertEqual(first["start"], "2026-10-11T12:00:00+02:00")
        self.assertEqual(first["end"], "2026-10-11T13:00:00+02:00")
        self.assertAlmostEqual(first["dhw_kwh"], 2.0)
        self.assertAlmostEqual(first["el_kwh"], 0.8)
        self.assertAlmostEqual(first["cop"], 2.5)
        self.assertAlmostEqual(first["minutes"], 60.0)
        self.assertAlmostEqual(first["out_mean"], 9.0)

        second = eps[1]
        self.assertEqual(second["start"], "2026-10-11T14:30:00+02:00")
        self.assertEqual(second["end"], "2026-10-11T15:30:00+02:00")
        self.assertAlmostEqual(second["dhw_kwh"], 2.0)
        self.assertAlmostEqual(second["el_kwh"], 1.0)

    def test_gap_tolerance_bridges_single_flat_sample(self):
        rows = [
            _row("t0", 100.0, 200.0),
            _row("t1", 101.0, 200.4),
            _row("t2", 101.0, 200.4),  # één vlak sample middenin
            _row("t3", 102.0, 200.8),
        ]
        eps = energy_log.summarize_episodes(rows)
        self.assertEqual(len(eps), 1)
        self.assertAlmostEqual(eps[0]["dhw_kwh"], 2.0)

    def test_no_episodes_when_flat(self):
        rows = [_row("t0", 100.0, 200.0), _row("t1", 100.0, 200.0)]
        self.assertEqual(energy_log.summarize_episodes(rows), [])

    def test_report_render(self):
        eps = energy_log.summarize_episodes(self._series())
        lines = energy_log.render_report(eps, self._series())
        self.assertTrue(any("COP" in ln for ln in lines))
        self.assertTrue(any("Totaal" in ln for ln in lines))

    def test_cost_from_price_column(self):
        rows = [
            _rowp("2026-10-11T12:00:00+02:00", 100.0, 200.0, 0.10, 53.0),
            _rowp("2026-10-11T12:30:00+02:00", 101.0, 200.4, 0.20, 53.0),
            _rowp("2026-10-11T13:00:00+02:00", 102.0, 200.8, 0.20, 53.0),
        ]
        eps = energy_log.summarize_episodes(rows)
        self.assertEqual(len(eps), 1)
        ep = eps[0]
        self.assertAlmostEqual(ep["el_kwh"], 0.8)
        # interval 12:00->12:30 tegen prijs 12:00 (0.10), 12:30->13:00 tegen 0.20
        self.assertAlmostEqual(ep["cost_eur"], 0.4 * 0.10 + 0.4 * 0.20)
        self.assertAlmostEqual(ep["price_mean"], ep["cost_eur"] / 0.8)
        self.assertEqual(ep["setpoint"], 53.0)

    def test_no_cost_when_no_price_column(self):
        eps = energy_log.summarize_episodes(self._series())
        self.assertTrue(all(e["cost_eur"] is None for e in eps))

    def test_report_groups_by_setpoint(self):
        rows = [
            _rowp("t0", 100.0, 200.0, 0.10, 53.0),
            _rowp("t1", 101.0, 200.5, 0.10, 53.0),
            _rowp("t2", 101.0, 200.5, 0.10, 53.0),
            _rowp("t3", 101.0, 200.5, 0.10, 53.0),
            _rowp("t4", 101.0, 200.5, 0.10, 53.0),  # > max_gap vlak
            _rowp("t5", 102.0, 201.0, 0.10, 45.0),
            _rowp("t6", 103.0, 201.5, 0.10, 45.0),
        ]
        eps = energy_log.summarize_episodes(rows)
        groups = dict(energy_log.group_by_setpoint(eps))
        self.assertIn(53.0, groups)
        self.assertIn(45.0, groups)
        lines = energy_log.render_report(eps, rows)
        self.assertTrue(any("Per setpoint" in ln for ln in lines))


class _FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload))


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


class TestEnergyLoggerSampling(unittest.TestCase):
    def _logger(self):
        mqtt_cfg = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 1883,
            "control_topic": "V04P26/SMTID/CLIENT2HOST",
            "qos": 0,
        }
        ecfg = {
            "enabled": True,
            "interval_seconds": 60.0,
            "response_timeout_seconds": 0.05,
            "csv_path": "/tmp/niet-gebruikt.csv",
            "include_handshake": False,
            "log_price": False,
            "price_refresh_seconds": 3600.0,
            "price_days_ahead": 2,
            "registers": list(energy_log.ALL_REGISTERS),
        }
        return energy_log.EnergyLogger(
            {}, mqtt_cfg, ecfg, ZoneInfo("Europe/Amsterdam")
        )

    def test_collect_returns_row_after_message(self):
        lg = self._logger()
        client = _FakeClient()
        msg = _Msg(
            "V04P26/SMTID/HOST2CLIENT",
            b'{"values": {"5105": "7B1D", "5376": "36E5", "5001": "04"}}',
        )
        lg.on_message(None, None, msg)
        row = lg._collect(client)
        self.assertIsNotNone(row)
        self.assertEqual(row["energy_electric_kwh"], 0x7B1D)
        self.assertEqual(row["energy_dhw_kwh"], 0x36E5)
        self.assertEqual(row["opmode"], 4)
        self.assertIn("timestamp", row)
        # query is gepubliceerd op CLIENT2HOST
        self.assertEqual(client.published[0][0], "V04P26/SMTID/CLIENT2HOST")

    def test_collect_returns_none_without_new_message(self):
        lg = self._logger()
        self.assertIsNone(lg._collect(_FakeClient()))

    def test_message_other_topic_ignored(self):
        lg = self._logger()
        lg.on_message(None, None, _Msg("iets/anders", b'{"values": {"5105": "0001"}}'))
        self.assertIsNone(lg._collect(_FakeClient()))


if __name__ == "__main__":
    unittest.main()
