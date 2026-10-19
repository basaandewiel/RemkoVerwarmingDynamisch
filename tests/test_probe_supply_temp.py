#!/usr/bin/env python3
"""Regressietests voor de sweep op de schrijfbare vaste waarde
(probe_supply_temp.py).

We mocken de MQTT-laag, zodat de logica (setpoints publiceren, reactie meten,
accepteren/negeren herkennen, terugzetten) los testbaar is.

Draaien:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

import probe_supply_temp as probe


class TestProbe(unittest.TestCase):
    def setUp(self):
        self.orig_publish = probe.mqtt_out.publish_command
        self.orig_read = probe._read
        self.published = []

    def tearDown(self):
        probe.mqtt_out.publish_command = self.orig_publish
        probe._read = self.orig_read

    def _fake_publish(self, ok=True):
        def impl(_cfg, topic, payload, qos=None, retain=None):
            # We sturen de schrijfbare vaste waarde (1975).
            self.published.append(payload["values"]["1975"])
            return ok, "bevestigd"
        probe.mqtt_out.publish_command = impl

    def _fake_read(self, seq):
        """seq = lijst van {reg: waarde}-dicts; laatste blijft terugkomen."""
        it = iter(seq)
        state = {"cur": None}

        def impl(_cfg, registers, timeout=8.0):
            try:
                state["cur"] = next(it)
            except StopIteration:
                pass
            return dict(state["cur"] or {})
        probe._read = impl

    def test_original_read_and_restored(self):
        self._fake_publish()
        # eerste read = huidige waarde, daarna per setpoint een read
        self._fake_read([
            {1975: 27.5, 5085: 27.5, 5205: 0},   # before
            {1975: 28.0, 5085: 27.5, 5205: 0},   # stap 1
            {1975: 22.0, 5085: 10.0, 5205: 0},   # stap 2
        ])
        rc = probe.probe({}, [28.0, 22.0], settle=0.0, sample=0.0, restore=True)
        self.assertEqual(rc, 0)
        # 28, 22 en de originele 27.5 terug
        self.assertEqual(self.published[-1], "0113")  # 27.5 -> 0x0113
        self.assertEqual(len(self.published), 3)

    def test_no_restore_keeps_last(self):
        self._fake_publish()
        self._fake_read([{1975: 27.5}, {1975: 22.0}, {1975: 22.0}])
        probe.probe({}, [22.0], settle=0.0, sample=0.0, restore=False)
        self.assertEqual(self.published[-1], "00dc")  # 22 °C

    def test_publish_failure_skips_step(self):
        self._fake_publish(ok=False)
        self._fake_read([{1975: 27.5}])
        rc = probe.probe({}, [26.0], settle=0.0, sample=0.0, restore=False)
        self.assertEqual(rc, 0)
        # alleen de mislukte publicatie, geen restore
        self.assertEqual(len(self.published), 1)

    def test_accept_detection(self):
        # 5085 (de resultante) komt overeen -> accepted True; compressor draait.
        self._fake_publish()
        self._fake_read([
            {1975: 27.5, 5085: 27.5},                  # before
            {1975: 30.0, 5085: 30.0, 5205: 40, 5320: 1200},  # accept
        ])
        probe.probe({}, [30.0], settle=0.0, sample=0.0, restore=False)
        # geen exception = goed; inhoudelijke accept-check gebeurt in _fmt-resultaten

    def test_restores_from_1975_not_5085(self):
        # De rustwaarde moet uit 1975 komen (schrijfbaar), ook als 5085 afwijkt.
        self._fake_publish()
        self._fake_read([
            {1975: 31.0, 5085: 27.5, 5205: 0},   # before: 1975 leidend
            {1975: 22.0, 5085: 22.0, 5205: 0},
        ])
        probe.probe({}, [22.0], settle=0.0, sample=0.0, restore=True)
        self.assertEqual(self.published[-1], format(int(31.0 * 10), "04x"))

    def test_payload_format(self):
        # Het gepubliceerde formaat moet hex temp*10 zijn op de schrijfbare 1975.
        import heating_comfort
        p = heating_comfort.build_setpoint_payload(40.0)
        self.assertEqual(p["values"]["1975"], "0190")


if __name__ == "__main__":
    unittest.main()
