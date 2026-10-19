#!/usr/bin/env python3
"""Vloerverwarming-bufferregeling: laad de vloer op de goedkoopste uren.

De vloerverwarming is een grote warmtebuffer. Deze regeling stuurt de
**gewenste aanvoertemperatuur ruimteverwarming** (register 5085,
`heat_water_temp_req`) op basis van de dynamische stroomprijs en de COP van de
Remko:

  * in de **goedkoopste uren** (kwartierprijs / COP) vraagt ze een **hogere**
    aanvoertemperatuur (`charge_temp`, default 40 °C) zodat de vloer warmte
    opslaat;
  * in de **duurste uren** vraagt ze een **lage** aanvoertemperatuur
    (`coast_temp`, default 28 °C) zodat de warmtepomp stilstaat en de vloer de
    kamer blijft voeden;
  * daarbuiten staat de aanvoer op de normale, constante waarde (`base_temp`,
    default gelijk aan `heatpump.supply_temperature`, meestal 35 °C).

Er wordt **geen ruimtetemperatuur gelezen**: het kamercomfort blijft bepaald
door de bestaande thermostaat (bv. Watts). Er wordt uitsluitend register 5085
geschreven — niet aan de SWW-regeling (register 1082) of aan de hoofdmode
(register 1951) geraakt. Draai deze service daarom naast `dhw_comfort`.

Wire-formaat (zelfde als 1082): temperatuur × 10 in hexadecimaal, 4 tekens.
40 °C -> 400 decimaal -> 0x0190 -> "0190".

Gebruik:
  python3 heating_comfort.py                 # één beslissing, en publiceren
  python3 heating_comfort.py --watch         # als service: elke `interval_minutes`
  python3 heating_comfort.py --dry-run       # niets publiceren, alleen tonen
  python3 heating_comfort.py --now 2026-10-11T08:00:00+02:00
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import cop_model as cop_mod
import energy_log
import main as app
import metno
import mqtt_out
from optimizer import build_corrected_rows, find_cheapest_blocks

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Register
SUPPLY_REGISTER = 5085      # gewenste aanvoertemperatuur ruimteverwarming

# Defaults (zie de module-docstring)
DEFAULT_CHARGE_TEMP = 40.0
DEFAULT_BASE_TEMP = 35.0
DEFAULT_COAST_TEMP = 28.0
DEFAULT_MIN_SUPPLY = 20.0
DEFAULT_MAX_SUPPLY = 45.0
DEFAULT_CHARGE_BLOCK_HOURS = 3.0
DEFAULT_CHARGE_BLOCKS_PER_DAY = 2
DEFAULT_MIN_GAP_HOURS = 6.0
DEFAULT_COAST_BLOCK_HOURS = 2.0
DEFAULT_COAST_BLOCKS_PER_DAY = 1
DEFAULT_HORIZON_HOURS = 24.0
DEFAULT_MAX_OUTDOOR_FOR_HEATING = 15.0
DEFAULT_INTERVAL_MINUTES = 5.0
DEFAULT_RESPONSE_TIMEOUT = 8.0
DEFAULT_ROWS_REFRESH_SECONDS = 1800.0
DEFAULT_PRICE_DAYS_AHEAD = 3
EPS = 1e-6


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def heating_cfg(cfg: dict) -> dict:
    """Lees de `mqtt.heating_comfort`-sectie met verstandige defaults."""
    mqtt_cfg = cfg.get("mqtt") or {}
    hc = mqtt_cfg.get("heating_comfort") or {}
    hp = cfg.get("heatpump") or {}
    # Zonder expliciete `base_temp` valt de normale aanvoer terug op de
    # aanvoertemperatuur uit de heatpump-config (meestal 35 °C), zodat er
    # buiten de blokken niets verandert aan de bestaande, constante aanvoer.
    base_default = float(hp.get("supply_temperature", DEFAULT_BASE_TEMP))
    return {
        "enabled": bool(hc.get("enabled", False)),
        "charge_temp": float(hc.get("charge_temp", DEFAULT_CHARGE_TEMP)),
        "base_temp": float(hc.get("base_temp", base_default)),
        "coast_temp": float(hc.get("coast_temp", DEFAULT_COAST_TEMP)),
        "min_supply": float(hc.get("min_supply", DEFAULT_MIN_SUPPLY)),
        "max_supply": float(hc.get("max_supply", DEFAULT_MAX_SUPPLY)),
        "charge_block_hours": float(
            hc.get("charge_block_hours", DEFAULT_CHARGE_BLOCK_HOURS)
        ),
        "charge_blocks_per_day": int(
            hc.get("charge_blocks_per_day", DEFAULT_CHARGE_BLOCKS_PER_DAY)
        ),
        "min_gap_hours": float(hc.get("min_gap_hours", DEFAULT_MIN_GAP_HOURS)),
        "coast_enabled": bool(hc.get("coast_enabled", True)),
        "coast_block_hours": float(
            hc.get("coast_block_hours", DEFAULT_COAST_BLOCK_HOURS)
        ),
        "coast_blocks_per_day": int(
            hc.get("coast_blocks_per_day", DEFAULT_COAST_BLOCKS_PER_DAY)
        ),
        "horizon_hours": float(hc.get("horizon_hours", DEFAULT_HORIZON_HOURS)),
        "max_outdoor_for_heating_c": float(
            hc.get("max_outdoor_for_heating_c", DEFAULT_MAX_OUTDOOR_FOR_HEATING)
        ),
        "interval_minutes": float(
            hc.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
        ),
        "response_timeout_seconds": float(
            hc.get("response_timeout_seconds", DEFAULT_RESPONSE_TIMEOUT)
        ),
        "rows_refresh_seconds": float(
            hc.get("rows_refresh_seconds", DEFAULT_ROWS_REFRESH_SECONDS)
        ),
        "price_days_ahead": int(hc.get("price_days_ahead", DEFAULT_PRICE_DAYS_AHEAD)),
        "qos": int(hc.get("qos", 1)),
        "retain": bool(hc.get("retain", False)),
    }


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testbaar)
# --------------------------------------------------------------------------- #

def clamp_supply(temperature: float, ccfg: dict) -> float:
    """Klem de aanvoertemperatuur op [min_supply, max_supply]."""
    return max(ccfg["min_supply"], min(ccfg["max_supply"], float(temperature)))


def build_setpoint_payload(temperature: float) -> dict:
    """5085-waarde = gewenste aanvoertemperatuur (°C × 10) in hexadecimaal.

    Zelfde wire-formaat als `dhw_comfort.build_setpoint_payload`:
    40 °C -> 400 decimaal -> 0x0190 -> "0190".
    """
    value_hex = format(int(round(temperature * 10.0)), "04x")
    return {"FORCE_RESPONSE": True, "values": {str(SUPPLY_REGISTER): value_hex}}


def _hours_per_day(horizon_hours: float) -> int:
    """Aantal etmalen dat de horizon beslaat (minimaal 1)."""
    return max(1, int(math.ceil(horizon_hours / 24.0)))


def _overlaps(a_start, a_end, b_start, b_end, gap_hours: float) -> bool:
    """True als [a_start, a_end] en [b_start, b_end] binnen `gap_hours` liggen."""
    gap = timedelta(hours=max(0.0, gap_hours))
    return not (a_end + gap <= b_start or b_end + gap <= a_start)


def _select_spaced(blocks: List[dict], max_count: int, gap_hours: float) -> List[dict]:
    """Kies uit `blocks` (op voorkeur gesorteerd) telkens het eerstvolgende blok
    dat `gap_hours` vrij houdt van de al gekozen blokken, tot `max_count`."""
    chosen: List[dict] = []
    if max_count <= 0:
        return chosen
    for b in blocks:
        if all(
            not _overlaps(b["start"], b["end"], c["start"], c["end"], gap_hours)
            for c in chosen
        ):
            chosen.append(b)
            if len(chosen) >= max_count:
                break
    return sorted(chosen, key=lambda b: b["start"])


def _heating_needed(block: dict, ccfg: dict) -> bool:
    """Is verwarmen aannemelijk nodig tijdens dit blok?

    Boven `max_outdoor_for_heating_c` (default 15 °C) verwarmt een huis met
    vloerverwarming doorgaans niet; dan laten we de aanvoer met rust in plaats
    van onnodig warmte te vragen.
    """
    temp = block.get("mean_temp")
    return temp is not None and temp <= ccfg["max_outdoor_for_heating_c"]


def _block_out(block: Optional[dict]) -> Optional[dict]:
    if not block:
        return None
    return {
        "start": block["start"].isoformat(),
        "end": block["end"].isoformat(),
        "mean_corrected": block.get("mean_corrected"),
        "mean_cop": block.get("mean_cop"),
        "mean_temp": block.get("mean_temp"),
    }


def plan_charge_blocks(
    rows: List[dict],
    granularity_min: int,
    now: datetime,
    ccfg: dict,
) -> List[dict]:
    """De laadblokken (goedkoopste uren) binnen de horizon, met tussenruimte.

    Kies de goedkoopste blokken van `charge_block_hours` uur, gespreid over de
    horizon met minstens `min_gap_hours` tussen de blokken, tot
    `charge_blocks_per_day` per etmaal. Blokken waarin het te warm is om te
    verwarmen (boven `max_outdoor_for_heating_c`) vallen af.
    """
    if not rows or ccfg["charge_blocks_per_day"] <= 0:
        return []
    bh = ccfg["charge_block_hours"]
    horizon_end = now + timedelta(hours=ccfg["horizon_hours"])
    candidates = find_cheapest_blocks(
        rows,
        granularity_min=granularity_min,
        block_hours=bh,
        only_future=False,
        top_n=100000,
        now=now,
        earliest_start=now - timedelta(hours=bh),
        start_before=horizon_end,
    )
    candidates = [
        b for b in candidates if b["end"] > now and _heating_needed(b, ccfg)
    ]
    max_count = ccfg["charge_blocks_per_day"] * _hours_per_day(ccfg["horizon_hours"])
    return _select_spaced(candidates, max_count, ccfg["min_gap_hours"])


def plan_coast_blocks(
    rows: List[dict],
    granularity_min: int,
    now: datetime,
    ccfg: dict,
) -> List[dict]:
    """De duurste blokken (`coast_temp`) binnen de horizon, met tussenruimte."""
    if not rows or not ccfg["coast_enabled"] or ccfg["coast_blocks_per_day"] <= 0:
        return []
    bh = ccfg["coast_block_hours"]
    horizon_end = now + timedelta(hours=ccfg["horizon_hours"])
    candidates = find_cheapest_blocks(
        rows,
        granularity_min=granularity_min,
        block_hours=bh,
        only_future=False,
        top_n=100000,
        now=now,
        earliest_start=now - timedelta(hours=bh),
        start_before=horizon_end,
    )
    candidates = [
        b for b in candidates if b["end"] > now and _heating_needed(b, ccfg)
    ]
    # duurste eerst (find_cheapest_blocks sorteert juist oplopend)
    candidates.sort(key=lambda b: b["mean_corrected"], reverse=True)
    max_count = ccfg["coast_blocks_per_day"] * _hours_per_day(ccfg["horizon_hours"])
    return _select_spaced(candidates, max_count, ccfg["min_gap_hours"])


# --------------------------------------------------------------------------- #
# Beslissing
# --------------------------------------------------------------------------- #

def classify(
    now: datetime,
    charge_blocks: List[dict],
    coast_blocks: List[dict],
) -> Tuple[str, Optional[dict]]:
    """Bepaal de actie op `now`: ('charge'|'coast'|'base', actief blok).

    Een laadblok wint van een koelblok als ze onverhoopt samenvallen.
    """
    active = next((b for b in charge_blocks if b["start"] <= now < b["end"]), None)
    if active is not None:
        return "charge", active
    active = next((b for b in coast_blocks if b["start"] <= now < b["end"]), None)
    if active is not None:
        return "coast", active
    return "base", None


def decide(
    ccfg: dict,
    now: datetime,
    rows: List[dict],
    granularity_min: int,
    state: Optional[dict] = None,
) -> dict:
    """Bepaal de gewenste aanvoertemperatuur (register 5085) en de status.

    Geeft een dict met o.a. `status`, `setpoint_temp` en het gekozen blok.
    `setpoint_temp` is None als er niets te doen valt (niet schrijven).
    """
    state = state or {}
    out: dict = {
        "now": now.isoformat(),
        "enabled": ccfg["enabled"],
        "charge_temp": ccfg["charge_temp"],
        "base_temp": ccfg["base_temp"],
        "coast_temp": ccfg["coast_temp"],
        "min_supply": ccfg["min_supply"],
        "max_supply": ccfg["max_supply"],
    }
    if not ccfg["enabled"]:
        out["status"] = "disabled"
        out["setpoint_temp"] = None
        return out

    if not rows:
        # Geen prijs-/temperatuurdata: laat de aanvoer ongemoeid (de warmtepomp
        # houdt zijn eigen, normale waarde aan).
        out["status"] = "no_data"
        out["setpoint_temp"] = None
        return out

    charge_blocks = plan_charge_blocks(rows, granularity_min, now, ccfg)
    coast_blocks = plan_coast_blocks(rows, granularity_min, now, ccfg)
    # Een koelblok dat met een laadblok samenvalt laten we vallen.
    coast_blocks = [
        b
        for b in coast_blocks
        if not any(
            _overlaps(b["start"], b["end"], c["start"], c["end"], 0.0)
            for c in charge_blocks
        )
    ]
    out["charge_blocks"] = [_block_out(b) for b in charge_blocks]
    out["coast_blocks"] = [_block_out(b) for b in coast_blocks]

    status, active = classify(now, charge_blocks, coast_blocks)
    if status == "charge":
        out["status"] = "charge"
        out["setpoint_temp"] = clamp_supply(ccfg["charge_temp"], ccfg)
        out["block"] = _block_out(active)
        return out
    if status == "coast":
        out["status"] = "coast"
        out["setpoint_temp"] = clamp_supply(ccfg["coast_temp"], ccfg)
        out["block"] = _block_out(active)
        return out

    out["status"] = "base"
    out["setpoint_temp"] = clamp_supply(ccfg["base_temp"], ccfg)
    nxt = next((b for b in charge_blocks if b["start"] > now), None)
    out["next_block"] = _block_out(nxt)
    return out


# --------------------------------------------------------------------------- #
# Statusfile
# --------------------------------------------------------------------------- #

def state_path() -> str:
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "remko-wkf70")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "heating_comfort_state.json")


def load_state() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    with open(state_path(), "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Uitvoeren (MQTT)
# --------------------------------------------------------------------------- #

def execute(
    out: dict,
    mqtt_cfg: dict,
    ccfg: dict,
    state: dict,
    dry_run: bool,
) -> dict:
    """Publiceer de aanvoertemperatuur als ze veranderd is; werk state bij."""
    setpoint = out.get("setpoint_temp")
    if setpoint is None:
        out["setpoint_changed"] = False
        return out

    last = state.get("last_sent_setpoint")
    changed = last is None or abs(float(last) - float(setpoint)) > EPS
    out["setpoint_changed"] = changed

    if not changed:
        return out

    topic = energy_log.command_topic(mqtt_cfg)
    out["topic"] = topic
    out["payload"] = build_setpoint_payload(float(setpoint))

    if dry_run:
        out["mqtt_published"] = False
        out["mqtt_detail"] = "dry-run: niet gepubliceerd"
        return out

    ok, detail = mqtt_out.publish_command(
        mqtt_cfg,
        topic,
        out["payload"],
        qos=ccfg.get("qos"),
        retain=ccfg.get("retain"),
    )
    out["mqtt_published"] = ok
    out["mqtt_detail"] = detail
    if ok:
        state["last_sent_setpoint"] = float(setpoint)
        state["last_sent_at"] = out["now"]
        save_state(state)
    return out


# --------------------------------------------------------------------------- #
# Data (prijzen + voorspelling)
# --------------------------------------------------------------------------- #

def heating_cop_model(hp: dict) -> cop_mod.CopModel:
    """COP-model voor de ruimteverwarming (aanvoer = heatpump.supply_temperature)."""
    supply = int(hp.get("supply_temperature", 35))
    curves = {
        35: hp.get("cop_curve_w35"),
        45: hp.get("cop_curve_w45"),
        55: hp.get("cop_curve_w55"),
    }
    curve = hp.get(f"cop_curve_w{supply}") or curves.get(supply)
    try:
        return cop_mod.CopModel(supply_temperature=supply, curve=curve)
    except KeyError:
        return cop_mod.CopModel(supply_temperature=supply)


def build_rows(cfg: dict) -> Tuple[List[dict], int, str]:
    """Prijzen + uurvoorspelling -> rijen met gecorrigeerde prijs (€/kWh warmte)."""
    loc = cfg["location"]
    fc = cfg.get("forecast") or {}
    hp = cfg.get("heatpump") or {}
    prices_cfg = cfg.get("prices") or {}

    model = heating_cop_model(hp)
    forecast = metno.fetch_hourly_forecast(
        lat=loc["lat"],
        lon=loc["lon"],
        tz=loc["timezone"],
        user_agent=fc.get("user_agent", "remko-wkf70-comfort/1.0"),
        horizon_hours=int(fc.get("horizon_hours", 72)),
        cache_ttl_seconds=int(fc.get("cache_ttl_seconds", 600)),
    )
    temps_by_hour = metno.hourly_temperatures(forecast)

    days_ahead = int(prices_cfg.get("days_ahead", DEFAULT_PRICE_DAYS_AHEAD))
    pr, _used = app._fetch_prices_with_fallback(prices_cfg, loc, days_ahead)

    adj = prices_cfg.get("price_adjustments") or {}
    vat = float(adj.get("vat_pct", 0.0))
    tax = float(adj.get("fixed_tax_per_kwh", 0.0))
    slots = [(dt, price * (1.0 + vat / 100.0) + tax) for dt, price in pr["slots"]]
    rows = build_corrected_rows(slots, temps_by_hour, model)
    return rows, int(pr["granularity_min"]), str(pr["source"])


# --------------------------------------------------------------------------- #
# Watcher
# --------------------------------------------------------------------------- #

def _log(*parts) -> None:
    stamp = datetime.now().strftime("%d-%m %H:%M:%S")
    print(f"[{stamp}]", *parts, flush=True)


def watch(cfg: dict, tz: ZoneInfo, ccfg: dict, dry_run: bool) -> int:
    if mqtt_out._paho() is None:
        _log("FOUT: paho-mqtt niet geïnstalleerd")
        return 1
    mqtt_cfg = cfg.get("mqtt") or {}
    if not mqtt_cfg.get("enabled"):
        _log("FOUT: mqtt.enabled = false")
        return 1

    stop = threading.Event()

    def _stop(*_args):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    _log(
        "Vloerverwarming-buffer gestart — aanvoer laden tot "
        f"{ccfg['charge_temp']:g} °C, normaal {ccfg['base_temp']:g} °C, "
        f"duur-uur {ccfg['coast_temp']:g} °C; "
        f"blokken {ccfg['charge_block_hours']:g} u x "
        f"{ccfg['charge_blocks_per_day']}/dag; "
        f"berekenen elke {ccfg['interval_minutes']:g} min"
    )

    state = load_state()
    rows: List[dict] = []
    granularity = 15
    rows_at = 0.0

    while not stop.is_set():
        now = datetime.now(tz)
        if not rows or (time.time() - rows_at) >= ccfg["rows_refresh_seconds"]:
            try:
                rows, granularity, source = build_rows(cfg)
                rows_at = time.time()
                _log(f"prijzen + voorspelling bijgewerkt ({source})")
            except Exception as exc:  # noqa: BLE001 — netwerk mag de service niet stoppen
                _log("FOUT: prijzen/voorspelling ophalen mislukt:", exc)
                if rows:
                    rows_at = time.time()

        out = decide(ccfg, now, rows, granularity, state)
        execute(out, mqtt_cfg, ccfg, state, dry_run)
        save_state(state)
        _log(_status_log(out))
        stop.wait(ccfg["interval_minutes"] * 60.0)

    _log("Vloerverwarming-buffer gestopt")
    return 0


def _status_log(out: dict) -> str:
    status = out.get("status")
    sp = out.get("setpoint_temp")
    if status == "charge":
        parts = [f"LADEN -> aanvoer {sp:g} °C"]
    elif status == "coast":
        parts = [f"DUUR UUR -> aanvoer {sp:g} °C (vloer laten uitwerken)"]
    elif status == "base":
        parts = [f"normaal -> aanvoer {sp:g} °C"]
        nb = out.get("next_block")
        if nb:
            parts.append(f"volgend laadblok {_fmt_iso(nb['start'])}")
    elif status == "no_data":
        parts = ["geen prijs-/temperatuurdata — aanvoer ongemoeid"]
    elif status == "disabled":
        parts = ["uit"]
    else:
        parts = [str(status)]
    if out.get("setpoint_changed") and out.get("topic"):
        if out.get("mqtt_published"):
            parts.append(f"VERSTUURD {json.dumps(out.get('payload'), ensure_ascii=False)}")
        else:
            parts.append(f"NIET verstuurd ({out.get('mqtt_detail')})")
    return " | ".join(parts)


def _fmt_iso(iso: Optional[str]) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "?"
    return f"{app.DUTCH_DAYS[dt.weekday()][:3]} {dt:%d-%m %H:%M}"


# --------------------------------------------------------------------------- #
# Tekstuele weergave
# --------------------------------------------------------------------------- #

def render_human(out: dict) -> List[str]:
    lines: List[str] = []
    lines.append("Vloerverwarming-bufferregeling — laden op de goedkoopste uren")
    lines.append("=" * 64)
    if "now" in out:
        lines.append(f"Nu       : {app.fmt_dt(datetime.fromisoformat(out['now']))}")
    lines.append(
        f"Regeling : laden {out.get('charge_temp', DEFAULT_CHARGE_TEMP):g} °C, "
        f"normaal {out.get('base_temp', DEFAULT_BASE_TEMP):g} °C, "
        f"duur uur {out.get('coast_temp', DEFAULT_COAST_TEMP):g} °C "
        f"(register {SUPPLY_REGISTER})"
    )

    status = out.get("status")
    if status == "disabled":
        lines.append("Status   : uit (mqtt.heating_comfort.enabled = false of mqtt uit)")
        return lines
    if status == "no_data":
        lines.append("Status   : geen prijs-/temperatuurdata — aanvoer ongemoeid")
        return lines

    label = {"charge": "LADEN", "coast": "DUUR UUR", "base": "NORMAAL"}.get(
        status, str(status)
    )
    lines.append(f"Actie    : {label} -> aanvoer {out.get('setpoint_temp'):g} °C")
    b = out.get("block")
    if b:
        lines.append(
            f"Blok     : {_fmt_iso(b['start'])} – {_fmt_iso(b['end'])} "
            f"(buitentemp {app.nl(b['mean_temp'], 1)} °C, "
            f"COP {app.nl(b['mean_cop'], 2)}, "
            f"{app.nl(b['mean_corrected'], 4)} €/kWh warmte)"
        )
    plan = out.get("charge_blocks") or []
    if plan:
        lines.append("Laadplan :")
        for blk in plan:
            lines.append(
                f"  - {_fmt_iso(blk['start'])} – {_fmt_iso(blk['end'])}  "
                f"{app.nl(blk['mean_corrected'], 4)} €/kWh warmte"
            )
    coast = out.get("coast_blocks") or []
    if coast:
        lines.append("Duur-uur :")
        for blk in coast:
            lines.append(
                f"  - {_fmt_iso(blk['start'])} – {_fmt_iso(blk['end'])}  "
                f"{app.nl(blk['mean_corrected'], 4)} €/kWh warmte"
            )

    if out.get("setpoint_changed"):
        if out.get("mqtt_published"):
            lines.append(
                f"Setpoint : VERSTUURD → {out.get('topic')} "
                f"{json.dumps(out.get('payload'), ensure_ascii=False)}"
            )
        elif str(out.get("mqtt_detail", "")).startswith("dry-run"):
            lines.append("Setpoint : (dry-run: niet gepubliceerd)")
        else:
            lines.append(f"Setpoint : NIET verstuurd ({out.get('mqtt_detail')})")
    return lines


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Vloerverwarming-buffer: stuur de aanvoer op de goedkoopste uren"
    )
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.json"))
    ap.add_argument("--now", default=None, help="ISO-tijdstip voor 'nu' (testen)")
    ap.add_argument("--dry-run", action="store_true", help="niets publiceren, alleen tonen")
    ap.add_argument("--json", action="store_true", help="JSON-uitvoer op stdout")
    ap.add_argument("--watch", action="store_true", help="blijf draaien")
    ap.add_argument("--interval", type=float, default=None,
                    help="seconden tussen samples in --watch (default uit config)")
    args = ap.parse_args(argv)

    try:
        cfg = app.load_config(args.config)
    except OSError as exc:
        print(f"FOUT: config niet leesbaar: {exc}", file=sys.stderr)
        return 1

    try:
        tz = ZoneInfo(cfg["location"]["timezone"])
    except KeyError:
        tz = ZoneInfo("Europe/Amsterdam")
    ccfg = heating_cfg(cfg)
    if args.interval is not None:
        ccfg["interval_minutes"] = args.interval / 60.0

    if args.watch and not args.now:
        return watch(cfg, tz, ccfg, dry_run=args.dry_run)

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    mqtt_cfg = cfg.get("mqtt") or {}
    rows: List[dict] = []
    granularity = 15
    try:
        rows, granularity, _source = build_rows(cfg)
    except Exception as exc:  # noqa: BLE001 — nette melding, ga door zonder data
        print(f"WAARSCHUWING: prijzen/voorspelling ophalen mislukt: {exc}", file=sys.stderr)

    state = load_state()
    out = decide(ccfg, now, rows, granularity, state)
    execute(out, mqtt_cfg, ccfg, state, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print("\n".join(render_human(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
