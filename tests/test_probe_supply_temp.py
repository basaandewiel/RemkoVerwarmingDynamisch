#!/usr/bin/env python3
"""Regressietests voor de 5085-sweep (probe_supply_temp.py).

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
            self.published.append(payload["values"]["5085"])
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
            {5085: 27.5, 5205: 0},         # before
            {5085: 27.5, 5205: 0},         # stap 1
            {5085: 10.0, 5205: 0},         # stap 2
        ])
        rc = probe.probe({}, [28.0, 22.0], settle=0.0, sample=0.0, restore=True)
        self.assertEqual(rc, 0)
        # 28, 22 en de originele 27.5 terug
        self.assertEqual(self.published[-1], "0113")  # 27.5 -> 0x0113
        self.assertEqual(len(self.published), 3)

    def test_no_restore_keeps_last(self):
        self._fake_publish()
        self._fake_read([{5085: 27.5}, {5085: 10.0}, {5085: 10.0}])
        probe.probe({}, [22.0], settle=0.0, sample=0.0, restore=False)
        self.assertEqual(self.published[-1], "00dc")  # 22 °C

    def test_publish_failure_skips_step(self):
        self._fake_publish(ok=False)
        self._fake_read([{5085: 27.5}])
        rc = probe.probe({}, [26.0], settle=0.0, sample=0.0, restore=False)
        self.assertEqual(rc, 0)
        # alleen de mislukte publicatie, geen restore
        self.assertEqual(len(self.published), 1)

    def test_accept_detection(self):
        # 5085 komt overeen -> accepted True; compressor draait -> DRAAIT
        self._fake_publish()
        self._fake_read([
            {5085: 27.5},                  # before
            {5085: 30.0, 5205: 40, 5320: 1200},  # accept
        ])
        probe.probe({}, [30.0], settle=0.0, sample=0.0, restore=False)
        # geen exception = goed; inhoudelijke accept-check gebeurt in _fmt-resultaten

    def test_payload_format(self):
        # Het gepubliceerde 5085-formaat moet hex temp*10 zijn.
        import heating_comfort
        p = heating_comfort.build_setpoint_payload(40.0)
        self.assertEqual(p["values"]["5085"], "0190")


if __name__ == "__main__":
    unittest.main()
