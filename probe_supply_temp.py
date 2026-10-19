#!/usr/bin/env python3
"""Bepaal de "uit-drempel" van de ruimteverwarming: bij welke gewenste
aanvoertemperatuur stopt de compressor?

Achtergrond: we schrijven de **schrijfbare vaste waarde** (register **1975**);
de gateway werkt daarna `heat_water_temp_req` **5085** bij, en dát is de
**aan/uit- én modulatieknop** van de Remko. Uit losse metingen bleek 19 °C ->
compressor uit (Nawerking) en 28-30 °C -> vol draaien. Dit hulpprogramma zet een
reeks waarden (aflopend) en meet na elke stap de reactie (we lezen `5085` terug
als controle), zodat we de omslag precies kunnen vinden en `coast_temp` daarop
kunnen instellen.

**Let op:** dit schrijft register 1975 (het is dus géén read-only tool). De
huidige waarde wordt aan het begin gelezen en aan het eind teruggezet (tenzij
`--no-restore`).

Gebruik:
  python3 probe_supply_temp.py                       # 28,26,24,22,20 -> terug
  python3 probe_supply_temp.py --temps 30,25,20
  python3 probe_supply_temp.py --settle 120          # seconden wachten per stap
  python3 probe_supply_temp.py --no-restore          # laat laatste waarde staan
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional

import energy_log
import heating_comfort
import main as app
import mqtt_out

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_TEMPS = [28.0, 26.0, 24.0, 22.0, 20.0]
DEFAULT_SETTLE = 100.0          # seconden wachten na het zetten van een setpoint
DEFAULT_SAMPLE = 10.0           # seconden tussen metingen binnen een stap

# Registers die de reactie van de warmtepomp tonen.
WATCH_REGISTERS = [1975, 5085, 5190, 5132, 5581, 5051, 5625, 5205, 5320, 5321, 5049]


def _log(*parts) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}]", *parts, flush=True)


def _read(mqtt_cfg: dict, registers: List[int]) -> dict:
    return heating_comfort._query_registers(mqtt_cfg, registers, timeout=8.0)


def _fmt(values: dict) -> str:
    parts = []
    for reg in WATCH_REGISTERS:
        if reg in values:
            parts.append(f"{reg}={values[reg]:g}")
    return "  ".join(parts)


def probe(mqtt_cfg: dict, temps: List[float], settle: float, sample: float,
          restore: bool) -> int:
    topic = energy_log.command_topic(mqtt_cfg)
    _log(f"commando-topic: {topic}")

    before = _read(mqtt_cfg, WATCH_REGISTERS)
    # De rustwaarde lezen we bij voorkeur uit 1975 (de schrijfbare vaste waarde),
    # met 5085 (de resultante) als terugval.
    original = before.get(1975, before.get(5085))
    _log(f"huidige toestand: {_fmt(before)}")

    results = []
    for temp in temps:
        payload = heating_comfort.build_setpoint_payload(temp)
        ok, detail = mqtt_out.publish_command(
            mqtt_cfg, topic, payload,
            qos=1, retain=False,
        )
        _log(f"-> 1975 = {temp:g} °C   ({json.dumps(payload)})  "
             f"[{'verstuurd' if ok else 'MISLUKT: ' + detail}]")
        if not ok:
            continue

        t_end = time.time() + settle
        last = {}
        peak_freq = 0.0
        peak_el = 0.0
        accepted = False
        while time.time() < t_end:
            time.sleep(sample)
            last = _read(mqtt_cfg, WATCH_REGISTERS)
            freq = last.get(5205, 0.0)
            el = last.get(5320, 0.0)
            peak_freq = max(peak_freq, freq)
            peak_el = max(peak_el, el)
            got = last.get(5085)
            if got is not None and abs(got - temp) <= 0.6:
                accepted = True
            _log(f"   {_fmt(last)}"
                 + ("" if accepted else "   [1975/5085 NIET overgenomen]"))

        results.append({
            "setpoint": temp,
            "final": last,
            "peak_freq": peak_freq,
            "peak_el": peak_el,
            "accepted": accepted,
        })

    _log("")
    _log("=== samenvatting ===")
    for r in results:
        if not r["final"]:
            verdict = "GEEN DATA"
        elif not r["accepted"]:
            verdict = "GENEGEERD"
        else:
            verdict = "DRAAIT" if r["peak_freq"] > 0 else "STIL"
        _log(f"  5085 = {r['setpoint']:>5g} °C  ->  {verdict:<9} "
             f"(piek {r['peak_freq']:g} Hz / {r['peak_el']:g} W; "
             f"eind 5085={r['final'].get(5085, float('nan')):g} °C, "
             f"compressor={r['final'].get(5625, r['final'].get(5205, '?'))})")
    if any(not r["accepted"] for r in results):
        _log("")
        _log("LET OP: één of meer waarden werden NIET overgenomen. Dat gebeurt als")
        _log("  de VK-modus op 'stooklijn' staat (die overschrijft 5085) of als de")
        _log("  hoofdmodus op Standby/automatisch staat. Zet de VK-modus op 'vaste")
        _log("  waarde' en de expert-modus op 'Verwarmen' om via 1975 te kunnen sturen.")

    if restore and original is not None:
        payload = heating_comfort.build_setpoint_payload(original)
        ok, _ = mqtt_out.publish_command(mqtt_cfg, topic, payload, qos=1)
        _log(f"oorspronkelijke waarde {original:g} °C teruggezet: {'ok' if ok else 'MISLUKT'}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Bepaal de uit-drempel van de ruimteverwarming (via 1975)")
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.json"))
    ap.add_argument("--temps", default=",".join(str(t) for t in DEFAULT_TEMPS),
                    help="komma-lijst van setpoints (aflopend aanbevolen)")
    ap.add_argument("--settle", type=float, default=DEFAULT_SETTLE,
                    help=f"seconden wachten per setpoint (default {DEFAULT_SETTLE:g})")
    ap.add_argument("--sample", type=float, default=DEFAULT_SAMPLE,
                    help=f"seconden tussen metingen (default {DEFAULT_SAMPLE:g})")
    ap.add_argument("--no-restore", action="store_true",
                    help="de oorspronkelijke 5085-waarde niet terugzetten")
    args = ap.parse_args(argv)

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

    try:
        temps = [float(t) for t in args.temps.split(",") if t.strip()]
    except ValueError:
        print("FOUT: --temps moet een komma-lijst van getallen zijn", file=sys.stderr)
        return 1

    return probe(mqtt_cfg, temps, args.settle, args.sample,
                 restore=not args.no_restore)


if __name__ == "__main__":
    sys.exit(main())
