#!/usr/bin/env python3
"""Regressietests voor de registermonitor (register_monitor.py).

De decodering, de selectie/CSV-kolommen en de wijzigingsdetectie zijn puur en
los testbaar zonder MQTT-broker.

Draaien:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from zoneinfo import ZoneInfo

import register_monitor as rm


class TestDecodeRaw(unittest.TestCase):
    def test_temp_positive(self):
        self.assertAlmostEqual(rm.decode_raw("temp", "00EB"), 23.5)

    def test_temp_negative(self):
        self.assertAlmostEqual(rm.decode_raw("temp", "FFF7"), -0.9)

    def test_temp_kind_is_tenths(self):
        self.assertAlmostEqual(rm.decode_raw("temp_k", "0005"), 0.5)

    def test_power_el_scales_by_100(self):
        self.assertEqual(rm.decode_raw("power_el", "0003"), 300.0)
        self.assertEqual(rm.decode_raw("power_el", "0000"), 0.0)

    def test_raw_types_are_unsigned_int(self):
        self.assertEqual(rm.decode_raw("counter", "1A2B"), 0x1A2B)
        self.assertEqual(rm.decode_raw("energy", "7B1D"), 0x7B1D)
        self.assertEqual(rm.decode_raw("opmode", "0A"), 10)

    def test_bad_hex_is_none(self):
        self.assertIsNone(rm.decode_raw("temp", "XYZ"))
        self.assertIsNone(rm.decode_raw("temp", None))


class TestParseValues(unittest.TestCase):
    def test_known_registers(self):
        payload = (
            '{"values": {"5085": "0190", "5190": "00EB", "5001": "06", '
            '"1972": "01", "9999": "FFFF"}}'
        )
        out = rm.parse_values(payload)
        self.assertAlmostEqual(out[5085], 40.0)
        self.assertAlmostEqual(out[5190], 23.5)
        self.assertEqual(out[5001], 6)
        self.assertEqual(out[1972], 1)
        self.assertNotIn(9999, out)  # onbekend register genegeerd

    def test_invalid_json(self):
        self.assertEqual(rm.parse_values("niet-json"), {})

    def test_missing_values_key(self):
        self.assertEqual(rm.parse_values('{"FORCE_RESPONSE": true}'), {})

    def test_bad_hex_skipped(self):
        out = rm.parse_values('{"values": {"5085": "XYZ", "5190": "0001"}}')
        self.assertNotIn(5085, out)
        self.assertAlmostEqual(out[5190], 0.1)


class TestFormatValue(unittest.TestCase):
    def test_temperature(self):
        self.assertEqual(rm.format_value(5085, 35.0), "35.0 °C")

    def test_opmode_label(self):
        self.assertEqual(rm.format_value(5001, 10), "Standby")
        self.assertEqual(rm.format_value(5001, 6), "Verwarmen")
        self.assertEqual(rm.format_value(5001, 99), "opmode99")

    def test_main_mode_label(self):
        self.assertEqual(rm.format_value(1951, 2), "Verwarmen")
        self.assertEqual(rm.format_value(1951, 3), "Standby")

    def test_bool_and_switch(self):
        self.assertEqual(rm.format_value(1972, 1), "aan")
        self.assertEqual(rm.format_value(1972, 0), "uit")

    def test_power_and_counter(self):
        self.assertEqual(rm.format_value(5320, 300.0), "300 W")
        self.assertEqual(rm.format_value(5822, 9681), "9681")

    def test_none(self):
        self.assertEqual(rm.format_value(5085, None), "?")


class TestColumnsAndSelection(unittest.TestCase):
    def test_csv_columns_in_catalog_order(self):
        cols = rm.csv_columns([5190, 5085, 5001])
        self.assertEqual(cols, ["timestamp", "opmode", "heat_water_temp_req",
                                "heating_actual_temp"])

    def test_selected_registers_default(self):
        regs = rm.selected_registers([])
        self.assertIn(5085, regs)
        self.assertIn(1972, regs)
        # catalogusorde: 5001 komt vóór 5085
        self.assertLess(regs.index(5001), regs.index(5085))

    def test_selected_registers_filters_unknown_and_text(self):
        regs = rm.selected_registers(["5085", "9999", "geen-getal"])
        self.assertEqual(regs, [5085])

    def test_snapshot_lines(self):
        values = {5085: 35.0, 5001: 10.0}
        lines = rm.snapshot_lines(values, [5001, 5085])
        self.assertEqual(len(lines), 2)
        self.assertIn("Standby", lines[0])
        self.assertIn("35.0 °C", lines[1])


def _rows():
    return [
        {"timestamp": "t0", "heat_water_temp_req": "10.0", "opmode": "10"},
        {"timestamp": "t1", "heat_water_temp_req": "35.0", "opmode": "10"},
        {"timestamp": "t2", "heat_water_temp_req": "35.0", "opmode": "6"},
        {"timestamp": "t3", "heat_water_temp_req": "40.0", "opmode": "6"},
    ]


class TestTransitions(unittest.TestCase):
    def test_detect(self):
        events = rm.detect_transitions(
            rm.energy_log.read_rows(_write_csv(rm.csv_columns([5085, 5001]))),
            [5085, 5001],
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["register"], 5085)
        self.assertEqual(events[0]["oud"], 40.0)
        self.assertEqual(events[0]["nieuw"], 35.0)
        self.assertEqual(events[1]["register"], 5001)
        self.assertEqual(events[1]["oud"], 10.0)
        self.assertEqual(events[1]["nieuw"], 6.0)

    def test_render_contains_changes_and_counts(self):
        rows = rm.energy_log.read_rows(_write_csv(rm.csv_columns([5085, 5001])))
        lines = rm.render_transitions(rows, [5085, 5001], "test.csv")
        text = "\n".join(lines)
        self.assertIn("Wijzigingen", text)
        self.assertIn("heat_water_temp_req", text)
        self.assertIn("Aantal wijzigingen per register", text)

    def test_no_changes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "flat.csv")
            cols = rm.csv_columns([5085])
            for i in range(3):
                rm.energy_log.append_row(path, cols, {"timestamp": f"t{i}",
                                                      "heat_water_temp_req": 35.0})
            rows = rm.energy_log.read_rows(path)
            lines = rm.render_transitions(rows, [5085], path)
            self.assertTrue(any("constant" in ln for ln in lines))


def _write_csv(columns) -> str:
    d = tempfile.mkdtemp()
    path = os.path.join(d, "probe.csv")
    data = [
        ("t0", 40.0, 10),
        ("t1", 35.0, 10),
        ("t2", 35.0, 6),
    ]
    for ts, sp, mode in data:
        row = {"timestamp": ts}
        if "heat_water_temp_req" in columns:
            row["heat_water_temp_req"] = sp
        if "opmode" in columns:
            row["opmode"] = mode
        rm.energy_log.append_row(path, columns, row)
    return path


class TestMonitorCfg(unittest.TestCase):
    def test_defaults(self):
        m = rm.monitor_cfg({})
        self.assertIn(5085, m["registers"])
        self.assertEqual(m["interval_seconds"], rm.DEFAULT_INTERVAL)
        self.assertTrue(m["log_csv"])
        self.assertFalse(m["include_handshake"])

    def test_override(self):
        cfg = {"mqtt": {"register_monitor": {
            "registers": [5001, 5085],
            "interval_seconds": 5,
            "csv": "/tmp/x.csv",
            "log_csv": False,
        }}}
        m = rm.monitor_cfg(cfg)
        self.assertEqual(m["registers"], [5001, 5085])
        self.assertEqual(m["interval_seconds"], 5.0)
        self.assertEqual(m["csv_path"], "/tmp/x.csv")
        self.assertFalse(m["log_csv"])


class TestEmit(unittest.TestCase):
    def _monitor(self, path):
        mqtt_cfg = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 1883,
            "control_topic": "V04P26/SMTID/CLIENT2HOST",
            "qos": 0,
        }
        mcfg = rm.monitor_cfg({})
        mcfg["registers"] = rm.selected_registers([5001, 5085, 1972])
        mcfg["csv_path"] = path
        mcfg["log_csv"] = True
        return rm.RegisterMonitor(mqtt_cfg, mcfg, ZoneInfo("Europe/Amsterdam"))

    def test_emit_writes_csv_and_detects_changes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "probe.csv")
            mon = self._monitor(path)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                mon._emit({5001: 10, 5085: 10.0, 1972: 0})   # eerste = snapshot
                mon._emit({5001: 10, 5085: 35.0, 1972: 1})   # twee wijzigingen
            out = buf.getvalue()
            self.assertIn("heat_water_temp_req", out)
            self.assertIn("10.0 °C -> 35.0 °C", out)
            self.assertIn("heating_circ_mode", out)
            rows = rm.energy_log.read_rows(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["opmode"], "10")

    def test_query_uses_control_topic(self):
        mon = self._monitor("/tmp/niet-gebruikt.csv")
        self.assertEqual(mon.cmd_topic, "V04P26/SMTID/CLIENT2HOST")
        self.assertEqual(mon.data_topic, "V04P26/SMTID/HOST2CLIENT")
        self.assertEqual(mon.query["query_list"], [5001, 1972, 5085])


if __name__ == "__main__":
    unittest.main()
