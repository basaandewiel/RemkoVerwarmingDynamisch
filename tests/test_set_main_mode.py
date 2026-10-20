#!/usr/bin/env python3
"""Regressietests voor set_main_mode.py (de 1951 aan/uit-knop)."""

from __future__ import annotations

import unittest

import set_main_mode as smm


class TestResolveMode(unittest.TestCase):
    def test_names(self):
        self.assertEqual(smm.resolve_mode("standby")[0], "03")
        self.assertEqual(smm.resolve_mode("standbys")[0], "03")
        self.assertEqual(smm.resolve_mode("verwarmen")[0], "02")
        self.assertEqual(smm.resolve_mode("auto")[0], "01")
        self.assertEqual(smm.resolve_mode("koelen")[0], "04")

    def test_case_insensitive(self):
        self.assertEqual(smm.resolve_mode("STANDBY")[0], "03")
        self.assertEqual(smm.resolve_mode(" Verwarmen ")[0], "02")

    def test_numeric(self):
        self.assertEqual(smm.resolve_mode("2")[0], "02")
        self.assertEqual(smm.resolve_mode("03")[0], "03")
        self.assertEqual(smm.resolve_mode("0x03")[0], "03")

    def test_unknown(self):
        self.assertIsNone(smm.resolve_mode("banaan"))
        self.assertIsNone(smm.resolve_mode("999"))


class TestPayload(unittest.TestCase):
    def test_payload_format(self):
        p = smm.build_payload("03")
        self.assertEqual(p, {"values": {"1951": "03"}})

    def test_no_force_response(self):
        # Een schrijfactie mag GEEN FORCE_RESPONSE bevatten.
        self.assertNotIn("FORCE_RESPONSE", smm.build_payload("02"))

    def test_register(self):
        self.assertEqual(smm.MAIN_MODE_REGISTER, 1951)


if __name__ == "__main__":
    unittest.main()
