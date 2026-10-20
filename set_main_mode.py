#!/usr/bin/env python3
"""Zet de Remko-hoofdmodus (register 1951) — de harde aan/uit-knop.

Register **1951** (`main_mode`) is **schrijfbaar** via MQTT en schakelt de
warmtepomp direct:

| Waarde | Modus    | Effect                                   |
|--------|----------|------------------------------------------|
| `01`   | Auto     | regeling kiest zelf (stooklijn)          |
| `02`   | Verwarmen| ruimteverwarming aan                      |
| `03`   | Standby  | **alles uit** (compressor + pomp stil)    |
| `04`   | Koelen   | koelen                                    |

Gemeten (20-10-2026): `1951 = 03` zette `5001` binnen 8 s op `Standby` met
`5085 = 10 °C`; `1951 = 02` zette hem terug op `Verwarmen` met `5085` = de
vaste waarde. Dit is dus een echte aan/uit-knop, naast de modulerende 1975.

Gebruik:
  python3 set_main_mode.py standbys            # 1951 = 03
  python3 set_main_mode.py verwarmen           # 1951 = 02
  python3 set_main_mode.py auto                # 1951 = 01
  python3 set_main_mode.py 02                  # ook als hex/decimaal cijfer
  python3 set_main_mode.py standbys --dry-run  # alleen tonen, niet schrijven
  python3 set_main_mode.py standbys --verify   # na 10 s de toestand tonen
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

import energy_log
import heating_comfort
import main as app
import mqtt_out

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

MAIN_MODE_REGISTER = 1951

# Naam -> (hex-waarde, omschrijving)
MODES = {
    "auto": ("01", "Auto (regeling kiest zelf)"),
    "verwarmen": ("02", "Verwarmen (ruimteverwarming aan)"),
    "standby": ("03", "Standby (alles uit)"),
    "standbys": ("03", "Standby (alles uit)"),
    "koelen": ("04", "Koelen"),
}


def build_payload(mode_hex: str) -> dict:
    """Payload die register 1951 zet. Geen FORCE_RESPONSE: dit is een schrijfactie."""
    return {"values": {str(MAIN_MODE_REGISTER): mode_hex}}


def resolve_mode(token: str) -> Optional[tuple]:
    """Zet een naam of cijfer om in (hex, omschrijving)."""
    key = token.strip().lower()
    if key in MODES:
        return MODES[key]
    # los cijfer: "2", "02", "0x02" (let op: int("03", 0) faalt, dus losse basis)
    try:
        text = key[2:] if key.startswith("0x") else key
        base = 16 if key.startswith("0x") else 10
        value = int(text, base)
        if 0 <= value <= 255:
            return (f"{value:02X}", f"modus {value}")
    except ValueError:
        pass
    return None


def read_state(mqtt_cfg: dict) -> dict:
    return heating_comfort._query_registers(
        mqtt_cfg, [1951, 5001, 1972, 5085], timeout=8.0
    )


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Zet de Remko-hoofdmodus (1951)")
    ap.add_argument("mode", help="auto | verwarmen | standby | koelen | <cijfer>")
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="alleen tonen")
    ap.add_argument("--verify", action="store_true",
                    help="na 10 s de registers teruglezen")
    args = ap.parse_args(argv)

    resolved = resolve_mode(args.mode)
    if resolved is None:
        print(f"FOUT: onbekende modus {args.mode!r}. "
              f"Kies uit: {', '.join(sorted(MODES))} of een cijfer 0-255.",
              file=sys.stderr)
        return 2
    mode_hex, description = resolved

    try:
        cfg = app.load_config(args.config)
    except OSError as exc:
        print(f"FOUT: config niet leesbaar: {exc}", file=sys.stderr)
        return 1
    mqtt_cfg = cfg.get("mqtt") or {}
    if not mqtt_cfg.get("enabled"):
        print("FOUT: mqtt.enabled = false", file=sys.stderr)
        return 1
    if mqtt_out._paho() is None:
        print("FOUT: paho-mqtt niet geïnstalleerd", file=sys.stderr)
        return 1

    payload = build_payload(mode_hex)
    if args.dry_run:
        print(f"[dry-run] zou sturen: {payload}   ({description})")
        return 0

    topic = energy_log.command_topic(mqtt_cfg)
    ok, detail = mqtt_out.publish_command(mqtt_cfg, topic, payload, qos=2)
    print(f"1951 = {mode_hex} ({description}): {'verstuurd' if ok else 'MISLUKT — ' + detail}")
    if not ok:
        return 1

    if args.verify:
        time.sleep(10)
        state = read_state(mqtt_cfg)
        print(f"  na 10 s: 1951={state.get(1951)}  5001={state.get(5001)}  "
              f"1972={state.get(1972)}  5085={state.get(5085)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
