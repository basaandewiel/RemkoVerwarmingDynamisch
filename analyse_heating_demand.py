#!/usr/bin/env python3
"""Analyse van de nachtmeting: kwam de warmtevraag van de thermostaat?

Leest de CSV van `register_monitor.py` en beantwoordt de vraag die de meting
moest beslechten: **stuurt de Watts-thermostaat de Remko aan, en zo ja via welk
register?**

Aanpak:
  * detecteer de **verwarmingsperiodes** (wanneer de compressor draait, of
    `heat_water_temp_req` boven een drempel staat);
  * kijk per periode welke registers *vóór* de vraag bewogen (de "trigger") en
    welke volgden;
  * bepaal of de HC1-kring (mengklep/pomp: `5156/5157/5577/5799/5059/5071`)
    ooit van 0 afkwam — dat is het bewijs dat de thermostaat de vloerkring
    openstuurt;
  * kijk naar de relatie tussen `hc1_room_temp` (5048), `hc1_room_target_temp`
    (5077) en de vraag.

Gebruik:
  python3 analyse_heating_demand.py                 # samenvatting
  python3 analyse_heating_demand.py --csv pad.csv   # ander bestand
  python3 analyse_heating_demand.py --json          # machineleesbaar
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import energy_log

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CSV = os.path.join(
    os.path.expanduser("~"), ".cache", "remko-wkf70", "heating_probe.csv"
)

# Registers van de HC1-vloerkring (de thermostaat/mengunit).
# LET OP: 5582 (`heating_flow_rate`) is het *primaire* debiet van de warmtepomp,
# niet de HC1-kring; die hoort hier dus NIET bij. `1972` (heating_circ_mode) is
# de kring aan/uit maar kan ook door een GUI-instelling komen; daarom telt hij
# mee als "kring actief" maar niet als hard bewijs dat de *thermostaat* vraagt.
HC1_REGISTERS = {
    5156: "hc1_mixer_status",
    5157: "hc1_mixer_position",
    5799: "hc1_pump_demand",
    5577: "hc1_pump_speed_rel",
    5059: "hc1_flow_rate",
    5071: "hc1_power",
    5068: "hc1_op_mode",
    1972: "heating_circ_mode",
}

# De mengklep/pomp: als deze van 0 afkomen, komt de vraag écht van de
# thermostaat/mengunit (en niet van een GUI-instelling).
HC1_MIXER_PUMP = {
    "hc1_mixer_status", "hc1_mixer_position", "hc1_pump_demand",
    "hc1_pump_speed_rel", "hc1_flow_rate", "hc1_power",
}

# Aantal opeenvolgende samples dat de vraag aan moet houden voor een "periode".
MIN_PERIOD_SAMPLES = 3


def _f(value, default: float = float("nan")) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_temp_column(name: str) -> bool:
    """Temperatuurkolommen opslaan we met een komma/heel getal (°C)."""
    return (
        name.endswith("_temp")
        or name in ("heat_water_temp_req", "water_temp_req")
        or name.endswith("_temp_req")
    )


def moving_registers(rows: List[dict]) -> Dict[str, List[Tuple[str, str, str]]]:
    """Per kolom de opeenvolgende waardewijzigingen (tijd, oud, nieuw)."""
    out: Dict[str, List[Tuple[str, str, str]]] = {}
    if not rows:
        return out
    columns = [c for c in rows[0] if c != "timestamp"]
    for col in columns:
        prev = None
        series: List[Tuple[str, str, str]] = []
        for row in rows:
            val = row.get(col)
            if val in (None, ""):
                continue
            if prev is not None and val != prev:
                series.append((row["timestamp"], prev, val))
            prev = val
        if series:
            out[col] = series
    return out


def find_heating_periods(rows: List[dict]) -> List[dict]:
    """Periodes waarin de warmtepomp verwarmde (compressor of vraag > drempel)."""
    periods: List[dict] = []
    cur: Optional[dict] = None
    for row in rows:
        freq = _f(row.get("compressor_frequency"), 0.0)
        req = _f(row.get("heat_water_temp_req"))
        opmode = _f(row.get("opmode"), -1.0)
        active = freq > 0 or (req == req and req >= 25.0) or int(opmode) == 6
        if active:
            if cur is None:
                cur = {"start": row["timestamp"], "end": row["timestamp"],
                       "rows": [row]}
            else:
                cur["end"] = row["timestamp"]
                cur["rows"].append(row)
        elif cur is not None:
            periods.append(cur)
            cur = None
    if cur is not None:
        periods.append(cur)
    return [p for p in periods if len(p["rows"]) >= MIN_PERIOD_SAMPLES]


def summarize_period(period: dict, moving: Dict[str, List]) -> dict:
    rows = period["rows"]
    max_freq = max(_f(r.get("compressor_frequency"), 0.0) for r in rows)
    max_el = max(_f(r.get("el_consumption"), 0.0) for r in rows)
    req = [_f(r.get("heat_water_temp_req")) for r in rows]
    req = [v for v in req if v == v]
    flow = [_f(r.get("hp_flow_temp")) for r in rows]
    flow = [v for v in flow if v == v]
    ret = [_f(r.get("hp_return_temp")) for r in rows]
    ret = [v for v in ret if v == v]

    # Bewoog de HC1-kring in deze periode?
    hc1_moved = {}
    for name in HC1_REGISTERS.values():
        series = moving.get(name, [])
        inside = [e for e in series if period["start"] <= e[0] <= period["end"]]
        if inside:
            hc1_moved[name] = [f"{t} {o}->{n}" for t, o, n in inside]

    # Alle registers die in deze periode bewogen (behalve ruis op buitentemp e.d.)
    all_moved = {
        col: [f"{t} {o}->{n}" for t, o, n in states
              if period["start"] <= t <= period["end"]]
        for col, states in moving.items()
    }
    all_moved = {k: v for k, v in all_moved.items() if v}

    return {
        "start": period["start"],
        "end": period["end"],
        "samples": len(rows),
        "max_compressor_frequency_hz": max_freq,
        "max_el_consumption_w": max_el,
        "heat_water_temp_req_min": round(min(req), 1) if req else None,
        "heat_water_temp_req_max": round(max(req), 1) if req else None,
        "hp_flow_temp_range": [round(min(flow), 1), round(max(flow), 1)] if flow else None,
        "hp_return_temp_range": [round(min(ret), 1), round(max(ret), 1)] if ret else None,
        "hc1_moved": hc1_moved,
        "registers_moved": sorted(all_moved.keys()),
    }


def render_report(rows: List[dict], path: str) -> List[str]:
    lines = [f"Nachtanalyse warmtevraag — {path}", "=" * 72]
    if not rows:
        lines.append("  (geen rijen)")
        return lines

    moving = moving_registers(rows)
    periods = find_heating_periods(rows)
    span = f"{rows[0]['timestamp']}  ..  {rows[-1]['timestamp']}"
    lines.append(f"  {len(rows)} samples over {span}")
    lines.append(f"  Kolommen die ooit bewogen: {len(moving)}")
    lines.append("")

    if not periods:
        lines.append("  Geen verwarmingsperiodes gevonden "
                     "(compressor bleef uit en heat_water_temp_req < 25 °C).")
        lines.append("  -> De Remko heeft vannacht niet verwarmd.")
        return lines

    lines.append(f"  Verwarmingsperiodes: {len(periods)}")
    hc1_evidence = False
    for i, p in enumerate(periods, 1):
        s = summarize_period(p, moving)
        lines.append("")
        lines.append(f"  [{i}] {s['start']}  ->  {s['end']}  ({s['samples']} samples)")
        lines.append(
            f"      compressor max {s['max_compressor_frequency_hz']:g} Hz, "
            f"max {s['max_el_consumption_w']:g} W; "
            f"vraag (5085) {s['heat_water_temp_req_min']}–"
            f"{s['heat_water_temp_req_max']} °C; "
            f"aanvoer {s['hp_flow_temp_range']} °C, retour {s['hp_return_temp_range']} °C"
        )
        if s["hc1_moved"]:
            mixer_pump = {
                n: e for n, e in s["hc1_moved"].items() if n in HC1_MIXER_PUMP
            }
            if mixer_pump:
                hc1_evidence = True
                lines.append("      HC1-mengklep/pomp BEWOOG (thermostaat vraagt!):")
            else:
                lines.append("      HC1-kring actief (maar zonder mengklep/pomp-beweging):")
            for name, events in s["hc1_moved"].items():
                lines.append(f"        - {name}: " + "; ".join(events[:6]))
        else:
            lines.append("      HC1-kring (mengklep/pomp) bleef ongemoeid.")
        shown = [c for c in s["registers_moved"] if c not in HC1_REGISTERS.values()]
        if shown:
            lines.append("      Overige bewegende registers: " + ", ".join(shown))

    lines.append("")
    lines.append("  Conclusie:")
    if hc1_evidence:
        lines.append("    De thermostaat stuurt de HC1-mengklep/pomp aan.")
        lines.append("    -> de Remko ziet de kamerwarmtevraag dus via de HC1-registers.")
    else:
        lines.append("    De HC1-mengklep/pomp kwamen de hele periode niet van 0.")
        lines.append("    -> de thermostaat stuurt NIET via de Remko-registers;")
        lines.append("       de kamerwarmtevraag loopt buiten de gateway om.")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Analyse van de nachtmeting warmtevraag")
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    path = os.path.expanduser(args.csv)
    if not os.path.exists(path):
        print(f"FOUT: geen CSV op {path}", file=sys.stderr)
        return 1
    rows = energy_log.read_rows(path)
    if args.json:
        moving = moving_registers(rows)
        periods = [summarize_period(p, moving) for p in find_heating_periods(rows)]
        print(json.dumps({"periods": periods}, ensure_ascii=False, indent=2, default=str))
    else:
        print("\n".join(render_report(rows, path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
