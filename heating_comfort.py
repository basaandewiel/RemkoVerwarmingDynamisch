#!/usr/bin/env python3
"""Vloerverwarming-bufferregeling: laad de vloer op de goedkoopste uren.

De vloerverwarming is een grote warmtebuffer. Deze regeling kiest het
**goedkoopste aaneengesloten blok** van `charge_block_hours` uur (default 3) op
de **gecorrigeerde** prijs — stroomprijs gedeeld door de COP, dus €/kWh warmte —
rekening houdend met de buitentemperatuurvoorspelling. In dat blok laadt de
vloer warmte op; daarbuiten staat de warmtepomp stil zodat de vloermassa de
kamer blijft voeden.

Er zijn twee regelwijzen (config `mqtt.heating_comfort.control`):

``"supply"`` (default) — **moduleren via register 1975**
  * in de **goedkoopste uren** vraagt ze een **hogere** aanvoertemperatuur
    (`charge_temp`, default 40 °C) zodat de vloer warmte opslaat;
  * in de **duurste uren** vraagt ze een **lage** aanvoertemperatuur
    (`coast_temp`, default 28 °C) zodat de warmtepomp stilstaat en de vloer de
    kamer blijft voeden;
  * daarbuiten staat de aanvoer op de normale, constante waarde (`base_temp`).

``"onoff"`` — **aan/uit via register 1951** (`main_mode`)
  * in het laadblok schrijft ze **1951 = 02** (Verwarmen): de Remko verwarmt
    en kiest op de **stooklijn** (`1972 = 0`) zelf de laagst mogelijke aanvoer;
  * daarbuiten schrijft ze **1951 = 03** (Standby): compressor en pomp staan
    écht stil — geen sluipverbruik. Dit is de krachtigste zuinige stand.

**SWW gaat vóór.** Er is één compressor, dus SWW laden en ruimteverwarming
kunnen niet samen. Daarom:

  * bij het **kiezen** van het laadblok worden uren overgeslagen die met een
    gepland SWW-laadvenster (`dhw_comfort_state.json`) overlappen;
  * bij het **draaien** pauzeert de regeling zodra de Remko op dat moment echt
    SWW laadt (`opmode` register **5001** = 4), en hervat daarna.

De rustwaarde `base_temp` wordt bij de start **uit het register gelezen**:
register `1975` (met `1974` en `5085` als terugval). Verzet je de vaste waarde in
de GUI, dan volgt de regeling hem bij de volgende verversing automatisch. In de
config kun je `base_temp` ook op een vast getal of op `"config"` zetten.

Er wordt **geen ruimtetemperatuur gelezen**: het kamercomfort blijft bepaald
door de bestaande thermostaat (bv. Watts). De SWW-regeling (register 1082) wordt
nooit aangeraakt. Draai deze service daarom naast `dhw_comfort`.

Wire-formaat temperatuur (zelfde als 1082): temperatuur × 10 in hexadecimaal,
4 tekens — 40 °C -> 400 decimaal -> 0x0190 -> "0190". Wire-formaat modus: twee
hex-tekens ("02" = Verwarmen, "03" = Standby).

Gebruik:
  python3 heating_comfort.py                 # één beslissing, en publiceren
  python3 heating_comfort.py --watch         # als service: elke `interval_minutes`
  python3 heating_comfort.py --dry-run       # niets publiceren, alleen tonen
  python3 heating_comfort.py --now 2026-10-11T08:00:00+02:00
  python3 heating_comfort.py --control onoff # aan/uit via 1951 (config overschrijven)
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
# Registers van de ruimteverwarming.
#
# De gewenste aanvoertemperatuur (`heat_water_temp_req`) zit in 5085, maar dat is
# een **read-only resultante**: de gateway accepteert er geen schrijfacties op.
# De **schrijfbare** instelling is 1975 (de "vaste waarde" van de VK-modus, die
# de GUI ook zet); 5085 volgt 1975 automatisch. We sturen dus **1975** en lezen
# 5085 terug als controle.
#
# Er zijn twee manieren om te regelen (`control` in de config):
#
#   * ``"supply"`` (default) — **moduleren**: de aanvoer-temperatuur (1975)
#     tussen `charge_temp`, `base_temp` en `coast_temp` schuiven.
#   * ``"onoff"`` — **aan/uit**: register **1951** (`main_mode`) schakelen tussen
#     Verwarmen in het laadblok en Standby daarbuiten. Dat zet de compressor
#     écht stil buiten het blok (harde knop), terwijl de Remko binnen het blok
#     de laagst mogelijke aanvoer zelf kiest op de stooklijn.
SUPPLY_REGISTER = 5085        # gewenste aanvoertemperatuur (read-only resultante)
FIXED_TEMP_REGISTER = 1975    # schrijfbare "vaste waarde" van de VK-modus
FIXED_TEMP_FALLBACK = 1974    # oudere/alternatieve vaste-waarde-register
OPMODE_REGISTER = 5001        # bedrijfsmodus (4 = SWW laden, 6 = verwarmen, 9 = nawerking)
MAIN_MODE_REGISTER = 1951     # schrijfbare hoofdmodus (aan/uit-knop)

# `opmode`-waarden (register 5001) die betekenen: niet aan de ruimteverwarming
# komen. Tijdens SWW-laden stuwt de Remko `5085` zelf op (~60 °C) en heeft onze
# vaste waarde geen effect; we laten dat laden dus met rust.
SWW_LOADING_OPMODE = 4

# Waarden voor register 1951 (`main_mode`), hard gemeten op 20-10-2026:
# `01` -> Auto, `02` -> Verwarmen, `03` -> Standby (compressor + pomp stil),
# `04` -> Koelen.
MAIN_MODE_AUTO = "01"
MAIN_MODE_HEAT = "02"
MAIN_MODE_STANDBY = "03"
MAIN_MODE_COOL = "04"

# Regelwijzen (config `mqtt.heating_comfort.control`).
CONTROL_SUPPLY = "supply"
CONTROL_ONOFF = "onoff"

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
DEFAULT_BASE_READ_TIMEOUT = 8.0
# Marge rond een gepland SWW-laadvenster waarin we de vloer niet laten laden
# (de compressor kan maar één ding tegelijk).
DEFAULT_DHW_GUARD_MINUTES = 15.0
# De gateway stuurt 1951 alleen in periodieke statusframes (~20-30 s), niet in
# een query-antwoord; luister daarom iets langer dan de gewone read-timeout.
DEFAULT_MAIN_MODE_READ_TIMEOUT = 35.0
# Ondergrens/ bovengrens waarbinnen een uit register 1974/5085 gelezen
# rustwaarde geloofwaardig is (daarbuiten negeren we hem).
MIN_READABLE_BASE_TEMP = 15.0
MAX_READABLE_BASE_TEMP = 50.0
EPS = 1e-6


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def heating_cfg(cfg: dict) -> dict:
    """Lees de `mqtt.heating_comfort`-sectie met verstandige defaults.

    `base_temp` (de rustwaarde buiten de blokken) mag op drie manieren:

    * een getal  -> die waarde wordt gebruikt;
    * ``null`` of weggelaten -> de **bestaande vaste waarde** wordt bij de start
      uit register 1974 (`fixed_temp_req`) gelezen, met 5085 als terugval. Dat is
      de waarde die in de GUI onder "vaste waarde" staat, zodat er buiten de
      blokken niets verandert;
    * ``"config"`` -> terugvallen op `heatpump.supply_temperature`.
    """
    mqtt_cfg = cfg.get("mqtt") or {}
    hc = mqtt_cfg.get("heating_comfort") or {}
    hp = cfg.get("heatpump") or {}
    raw_base = hc.get("base_temp", None)
    # Een expliciet getal in de config gebruikt dat; anders lezen we de vaste
    # waarde uit het register (zie read_fixed_temp).
    if raw_base is None or str(raw_base).strip() in ("", "null", "register"):
        base_temp = float(hp.get("supply_temperature", DEFAULT_BASE_TEMP))
        base_from_register = True
    elif str(raw_base).strip() == "config":
        base_temp = float(hp.get("supply_temperature", DEFAULT_BASE_TEMP))
        base_from_register = False
    else:
        base_temp = float(raw_base)
        base_from_register = False
    control = str(hc.get("control", CONTROL_SUPPLY)).strip().lower()
    if control not in (CONTROL_SUPPLY, CONTROL_ONOFF):
        control = CONTROL_SUPPLY
    return {
        "enabled": bool(hc.get("enabled", False)),
        "control": control,
        "avoid_dhw": bool(hc.get("avoid_dhw", True)),
        "dhw_guard_minutes": float(
            hc.get("dhw_guard_minutes", DEFAULT_DHW_GUARD_MINUTES)
        ),
        "charge_temp": float(hc.get("charge_temp", DEFAULT_CHARGE_TEMP)),
        "base_temp": base_temp,
        "base_temp_from_register": base_from_register,
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
        "base_read_timeout_seconds": float(
            hc.get("base_read_timeout_seconds", DEFAULT_BASE_READ_TIMEOUT)
        ),
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
    """Payload die de **vaste waarde** van de VK-modus zet (register 1975).

    We schrijven **1975** (schrijfbaar); de gateway werkt daarna 5085
    (`heat_water_temp_req`) bij. Wire-formaat: temperatuur × 10 in hexadecimaal,
    4 tekens — 40 °C -> 400 -> 0x0190 -> "0190".

    Let op: geen `FORCE_RESPONSE` — dat zou als *query* gelezen worden; een
    schrijfactie is alleen `{"values": {...}}` (zoals de HA-integratie doet).
    """
    value_hex = format(int(round(temperature * 10.0)), "04x")
    return {"values": {str(FIXED_TEMP_REGISTER): value_hex}}


def build_main_mode_payload(mode_hex: str) -> dict:
    """Payload die de **hoofdmodus** (register 1951) zet — de aan/uit-knop.

    Wire-formaat: twee hex-tekens, bv. "02" (Verwarmen) of "03" (Standby).
    Ook hier geen `FORCE_RESPONSE`: het is een schrijfactie.
    """
    return {"values": {str(MAIN_MODE_REGISTER): str(mode_hex)}}


# --------------------------------------------------------------------------- #
# SWW-laadvenster (uit de dhw_comfort-status)
# --------------------------------------------------------------------------- #

def dhw_state_path() -> str:
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "remko-wkf70")
    return os.path.join(cache_dir, "dhw_comfort_state.json")


def load_dhw_state(path: Optional[str] = None) -> dict:
    """Lees de statusfile van `dhw_comfort` (stil: leeg bij ontbreken/fout)."""
    try:
        with open(path or dhw_state_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def dhw_charge_window(dhw_state: dict) -> Optional[Tuple[datetime, datetime]]:
    """Het SWW-laadvenster uit de dhw-status als (start, end), of None.

    `dhw_comfort` schrijft `charge_start`/`charge_end` zolang het vat geladen
    wordt. Alleen een geldig, nog niet verstreken venster telt mee.
    """
    if not dhw_state:
        return None
    try:
        start = datetime.fromisoformat(str(dhw_state.get("charge_start")))
        end = datetime.fromisoformat(str(dhw_state.get("charge_end")))
    except (TypeError, ValueError):
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    return start, end


def _conflicts_dhw(
    block: dict,
    dhw_window: Optional[Tuple[datetime, datetime]],
    guard_minutes: float,
) -> bool:
    """True als `block` met het SWW-laadvenster botst (incl. veiligheidsmarge)."""
    if not dhw_window:
        return False
    start, end = dhw_window
    return _overlaps(
        block["start"], block["end"], start, end, guard_minutes / 60.0
    )


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
    dhw_window: Optional[Tuple[datetime, datetime]] = None,
) -> List[dict]:
    """De laadblokken (goedkoopste uren) binnen de horizon, met tussenruimte.

    Kies de goedkoopste blokken van `charge_block_hours` uur, gespreid over de
    horizon met minstens `min_gap_hours` tussen de blokken, tot
    `charge_blocks_per_day` per etmaal. Blokken waarin het te warm is om te
    verwarmen (boven `max_outdoor_for_heating_c`) vallen af, net als blokken die
    met een gepland SWW-laadvenster (`dhw_window`) overlappen — de compressor
    kan maar één ding tegelijk.
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
    guard = ccfg.get("dhw_guard_minutes", DEFAULT_DHW_GUARD_MINUTES)
    avoid = ccfg.get("avoid_dhw", True)
    candidates = [
        b
        for b in candidates
        if b["end"] > now
        and _heating_needed(b, ccfg)
        and not (avoid and _conflicts_dhw(b, dhw_window, guard))
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
    opmode: Optional[int] = None,
    dhw_window: Optional[Tuple[datetime, datetime]] = None,
) -> dict:
    """Bepaal de actie en het gewenste register.

    In ``control = "supply"`` levert `setpoint_temp` de aanvoertemperatuur voor
    register 1975. In ``control = "onoff"`` levert `main_mode` de te schrijven
    modus voor register 1951 ("02" = Verwarmen in het blok, "03" = Standby
    daarbuiten). `setpoint_temp`/`main_mode` zijn None als er niets te doen valt.

    ``opmode`` is de bedrijfsmodus (register 5001). Staat die op **SWW laden**
    (4), dan pauzeert de regeling: de Remko stuwt `5085` dan zélf op. In
    onoff-stand gaat de Remko dan ook op **Standby** — SWW laden en
    ruimteverwarming kunnen niet samen (één compressor).

    ``dhw_window`` is het geplande SWW-laadvenster; blokken die daarmee
    overlappen worden bij het plannen overgeslagen.
    """
    state = state or {}
    onoff = ccfg.get("control") == CONTROL_ONOFF
    out: dict = {
        "now": now.isoformat(),
        "enabled": ccfg["enabled"],
        "control": ccfg.get("control", CONTROL_SUPPLY),
        "avoid_dhw": ccfg.get("avoid_dhw", True),
        "charge_temp": ccfg["charge_temp"],
        "base_temp": ccfg["base_temp"],
        "base_temp_from_register": ccfg.get("base_temp_from_register", False),
        "coast_temp": ccfg["coast_temp"],
        "min_supply": ccfg["min_supply"],
        "max_supply": ccfg["max_supply"],
        "opmode": opmode,
        "setpoint_temp": None,
        "main_mode": None,
    }
    if dhw_window:
        out["dhw_window"] = {
            "start": dhw_window[0].isoformat(),
            "end": dhw_window[1].isoformat(),
        }
    if not ccfg["enabled"]:
        out["status"] = "disabled"
        out["setpoint_temp"] = None
        out["main_mode"] = None
        return out

    if is_dhw_loading(opmode):
        # SWW gaat vóór (één compressor). In onoff-stand zetten we de Remko
        # expliciet op Standby zodat ruimteverwarming niet met SWW concurreert.
        out["status"] = "dhw"
        out["setpoint_temp"] = None
        out["main_mode"] = MAIN_MODE_STANDBY if onoff else None
        return out

    if not rows:
        # Geen prijs-/temperatuurdata: niets doen. In onoff-stand is "niets doen"
        # de veilige Standby: geen verwarming zonder onderbouwing.
        out["status"] = "no_data"
        out["setpoint_temp"] = None
        out["main_mode"] = MAIN_MODE_STANDBY if onoff else None
        return out

    charge_blocks = plan_charge_blocks(rows, granularity_min, now, ccfg, dhw_window)
    out["charge_blocks"] = [_block_out(b) for b in charge_blocks]

    if onoff:
        active = next((b for b in charge_blocks if b["start"] <= now < b["end"]), None)
        if active is not None:
            out["status"] = "charge"
            out["main_mode"] = MAIN_MODE_HEAT
            out["block"] = _block_out(active)
            return out
        out["status"] = "standby"
        out["main_mode"] = MAIN_MODE_STANDBY
        nxt = next((b for b in charge_blocks if b["start"] > now), None)
        out["next_block"] = _block_out(nxt)
        return out

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
# De bestaande vaste waarde uitlezen (register 1975 / 5085)
# --------------------------------------------------------------------------- #

def read_fixed_temp(mqtt_cfg: dict, timeout: float = DEFAULT_BASE_READ_TIMEOUT) -> Optional[float]:
    """Lees de in de GUI ingestelde **vaste waarde** van de VK-modus.

    Dat is register **1975**; als de gateway die niet meestuurt, valt het terug
    op **1974** en daarna op **5085** (`heat_water_temp_req`, de actieve
    resultante). Geeft ``None`` als er niets bruikbaars terugkwam.

    Read-only: stuurt alleen een `FORCE_RESPONSE`-query.
    """
    order = (FIXED_TEMP_REGISTER, FIXED_TEMP_FALLBACK, SUPPLY_REGISTER)
    values = _query_registers(mqtt_cfg, list(order), timeout)
    for reg in order:
        if reg not in values:
            continue
        temp = float(values[reg])
        if MIN_READABLE_BASE_TEMP <= temp <= MAX_READABLE_BASE_TEMP:
            return round(temp, 1)
    return None


def read_opmode(mqtt_cfg: dict, timeout: float = DEFAULT_BASE_READ_TIMEOUT) -> Optional[int]:
    """Lees de bedrijfsmodus (register 5001).

    ``4`` = SWW laden, ``6`` = verwarmen, ``9`` = nawerking, ``10`` = standby.
    Geeft ``None`` als het niet leesbaar is (dan gaan we uit van "geen SWW").
    """
    values = _query_registers(mqtt_cfg, [OPMODE_REGISTER], timeout)
    if OPMODE_REGISTER not in values:
        return None
    try:
        return int(values[OPMODE_REGISTER])
    except (TypeError, ValueError):
        return None


def is_dhw_loading(opmode: Optional[int]) -> bool:
    """True als de Remko op dit moment SWW aan het laden is (register 5001 = 4)."""
    return opmode == SWW_LOADING_OPMODE


def read_main_mode(mqtt_cfg: dict,
                   timeout: float = DEFAULT_MAIN_MODE_READ_TIMEOUT) -> Optional[str]:
    """Lees de hoofdmodus (register 1951) als hex-tekens, bv. "02"/"03".

    Let op: de gateway echoot `1951` **niet** in een `FORCE_RESPONSE`-antwoord
    op een gerichte query (gemeten 20-10-2026), maar stuurt hem wél mee in zijn
    **periodieke statusframes** (~elke 20-30 s). We luisteren daarom passief
    mee in plaats van te queryen. Geeft ``None`` als er binnen `timeout` niets
    bruikbaars langskomt.
    """
    values = _listen_registers(mqtt_cfg, [MAIN_MODE_REGISTER], timeout)
    if MAIN_MODE_REGISTER not in values:
        return None
    try:
        return format(int(values[MAIN_MODE_REGISTER]), "02X")
    except (TypeError, ValueError):
        return None


def _query_registers(mqtt_cfg: dict, registers: List[int],
                     timeout: float = DEFAULT_BASE_READ_TIMEOUT) -> dict:
    """Vraag een paar registers op en geef {register: waarde} terug.

    Blokkeert tot `timeout` seconden of tot de gateway antwoordt. Fouten worden
    stil opgevangen: de regeling moet zonder MQTT-antwoord gewoon door kunnen.
    """
    try:
        mqtt = mqtt_out._paho()
    except Exception:  # noqa: BLE001 — paho ontbreekt
        return {}
    got: dict = {}
    done = threading.Event()
    cmd_topic = energy_log.command_topic(mqtt_cfg)
    data_topic = energy_log.data_topic(mqtt_cfg)
    if not cmd_topic or not data_topic:
        return {}

    def on_connect(client, _userdata, _flags, _rc, properties=None):
        try:
            client.subscribe(data_topic, qos=0)
            client.publish(cmd_topic,
                           json.dumps(energy_log.build_query(registers)), qos=0)
        except Exception:  # noqa: BLE001
            done.set()

    def on_message(_client, _userdata, message):
        if message.topic != data_topic:
            return
        try:
            payload = message.payload.decode()
        except Exception:  # noqa: BLE001
            return
        parsed = energy_log.parse_values(payload)
        wanted = {r: v for r, v in parsed.items() if r in registers}
        if wanted:
            got.update(wanted)
            done.set()

    client = None
    try:
        client = energy_log._make_connected_client(mqtt, mqtt_cfg)
        client.on_connect = on_connect
        client.on_message = on_message
        client.loop_start()
        done.wait(timeout)
    except Exception:  # noqa: BLE001 — geen verbinding
        return {}
    finally:
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return got


def _listen_registers(mqtt_cfg: dict, registers: List[int],
                      timeout: float = DEFAULT_MAIN_MODE_READ_TIMEOUT) -> dict:
    """Luister passief naar de periodieke statusframes en geef {register: waarde}.

    Voor registers die de gateway **niet** in een query-antwoord echoot (zoals
    `1951`), maar wél periodiek uitzendt. Er wordt niets gepubliceerd. Blokkeert
    tot `timeout` seconden of tot alle gevraagde registers langsgekomen zijn.
    """
    try:
        mqtt = mqtt_out._paho()
    except Exception:  # noqa: BLE001 — paho ontbreekt
        return {}
    got: dict = {}
    done = threading.Event()
    data_topic = energy_log.data_topic(mqtt_cfg)
    if not data_topic:
        return {}

    def on_connect(client, _userdata, _flags, _rc, properties=None):
        try:
            client.subscribe(data_topic, qos=0)
        except Exception:  # noqa: BLE001
            done.set()

    def on_message(_client, _userdata, message):
        if message.topic != data_topic:
            return
        try:
            payload = message.payload.decode()
        except Exception:  # noqa: BLE001
            return
        parsed = energy_log.parse_values(payload)
        wanted = {r: v for r, v in parsed.items() if r in registers}
        if wanted:
            got.update(wanted)
            if all(r in got for r in registers):
                done.set()

    client = None
    try:
        client = energy_log._make_connected_client(mqtt, mqtt_cfg)
        client.on_connect = on_connect
        client.on_message = on_message
        client.loop_start()
        done.wait(timeout)
    except Exception:  # noqa: BLE001 — geen verbinding
        return {}
    finally:
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return got


def resolve_base_temp(ccfg: dict, mqtt_cfg: dict, state: dict,
                      log=None) -> float:
    """Bepaal de rustwaarde (`base_temp`), indien nodig uit de regeling.

    Werkt `ccfg["base_temp"]` bij als `base_temp_from_register` aan staat:
    bij voorkeur uit register 1974/5085, anders uit de eerder opgeslagen waarde
    in de statusfile, en anders blijft de config-waarde staan.
    """
    if not ccfg.get("base_temp_from_register"):
        return ccfg["base_temp"]
    temp = read_fixed_temp(mqtt_cfg, ccfg.get("base_read_timeout_seconds",
                                              DEFAULT_BASE_READ_TIMEOUT))
    if temp is None:
        remembered = state.get("base_temp")
        if isinstance(remembered, (int, float)):
            temp = float(remembered)
            if log:
                log(f"vaste waarde niet leesbaar — eerder gelezen {temp:g} °C")
        else:
            if log:
                log("vaste waarde niet leesbaar — config-waarde "
                    f"{ccfg['base_temp']:g} °C blijft staan")
            return ccfg["base_temp"]
    ccfg["base_temp"] = float(temp)
    state["base_temp"] = float(temp)
    if log:
        log(f"vaste waarde uit register: {temp:g} °C")
    return ccfg["base_temp"]


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
    """Publiceer de gewenste stand als hij veranderd is; werk state bij.

    In ``supply``-stand is dat de aanvoertemperatuur (1975); in ``onoff``-stand
    de hoofdmodus (1951). Er wordt niets gepubliceerd als de waarde gelijk is
    aan de vorige keer (state) — MQTT blijft dan stil.
    """
    onoff = ccfg.get("control") == CONTROL_ONOFF
    if onoff:
        want = out.get("main_mode")
        if want is None:
            out["changed"] = False
            return out
        last = state.get("last_sent_main_mode")
        changed = last is None or str(last) != str(want)
        out["changed"] = changed
        out["payload"] = build_main_mode_payload(str(want))
    else:
        setpoint = out.get("setpoint_temp")
        if setpoint is None:
            out["changed"] = False
            return out
        last = state.get("last_sent_setpoint")
        changed = last is None or abs(float(last) - float(setpoint)) > EPS
        out["changed"] = changed
        out["payload"] = build_setpoint_payload(float(setpoint))

    # Behoud de oude sleutel voor bestaande gebruikers van de JSON-uitvoer.
    out["setpoint_changed"] = bool(changed)
    if not changed:
        return out

    topic = energy_log.command_topic(mqtt_cfg)
    out["topic"] = topic

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
        if onoff:
            state["last_sent_main_mode"] = str(want)
        else:
            state["last_sent_setpoint"] = float(out["setpoint_temp"])
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

    if ccfg.get("control") == CONTROL_ONOFF:
        _log(
            "Vloerverwarming aan/uit gestart — in het blok 1951=02 (Verwarmen), "
            f"daarbuiten 1951=03 (Standby); blok {ccfg['charge_block_hours']:g} u x "
            f"{ccfg['charge_blocks_per_day']}/dag; "
            f"SWW mijden: {ccfg.get('avoid_dhw')}"
        )
    else:
        _log(
            "Vloerverwarming-buffer gestart — aanvoer laden tot "
            f"{ccfg['charge_temp']:g} °C, normaal {ccfg['base_temp']:g} °C, "
            f"duur-uur {ccfg['coast_temp']:g} °C; "
            f"blokken {ccfg['charge_block_hours']:g} u x "
            f"{ccfg['charge_blocks_per_day']}/dag; "
            f"berekenen elke {ccfg['interval_minutes']:g} min"
        )

    state = load_state()
    if ccfg.get("base_temp_from_register") and ccfg.get("control") != CONTROL_ONOFF:
        resolve_base_temp(ccfg, mqtt_cfg, state, log=_log)
        save_state(state)
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
            # Lees de vaste waarde opnieuw: verzet de gebruiker 's winters de
            # GUI, dan volgt de rustwaarde automatisch.
            if ccfg.get("base_temp_from_register") and ccfg.get("control") != CONTROL_ONOFF:
                resolve_base_temp(ccfg, mqtt_cfg, state, log=_log)

        # SWW-laden? Dan pauzeren (de Remko stuurt 5085 zelf op).
        opmode = read_opmode(mqtt_cfg, ccfg.get("base_read_timeout_seconds",
                                                DEFAULT_BASE_READ_TIMEOUT))
        dhw_window = dhw_charge_window(load_dhw_state())
        out = decide(ccfg, now, rows, granularity, state,
                     opmode=opmode, dhw_window=dhw_window)
        execute(out, mqtt_cfg, ccfg, state, dry_run)
        save_state(state)
        _log(_status_log(out))
        stop.wait(ccfg["interval_minutes"] * 60.0)

    _log("Vloerverwarming gestopt")
    return 0


def _status_log(out: dict) -> str:
    status = out.get("status")
    sp = out.get("setpoint_temp")
    onoff = out.get("control") == CONTROL_ONOFF
    if status == "charge":
        parts = [("LADEN -> 1951=Verwarmen" if onoff
                  else f"LADEN -> aanvoer {sp:g} °C")]
        b = out.get("block")
        if b:
            parts.append(f"blok t/m {_fmt_iso(b['end'])}")
    elif status == "standby":
        parts = ["STANDBY -> 1951=Standby (compressor uit)"]
        nb = out.get("next_block")
        if nb:
            parts.append(f"volgend laadblok {_fmt_iso(nb['start'])}")
    elif status == "coast":
        parts = [f"DUUR UUR -> aanvoer {sp:g} °C (vloer laten uitwerken)"]
    elif status == "base":
        parts = [f"normaal -> aanvoer {sp:g} °C"]
        nb = out.get("next_block")
        if nb:
            parts.append(f"volgend laadblok {_fmt_iso(nb['start'])}")
    elif status == "no_data":
        parts = ["geen prijs-/temperatuurdata"]
        parts.append("-> 1951=Standby (veilig)" if onoff else "— aanvoer ongemoeid")
    elif status == "dhw":
        parts = ["SWW laden actief — regeling pauzeert"]
        if onoff:
            parts.append("1951=Standby (niet tegelijk met SWW)")
    elif status == "disabled":
        parts = ["uit"]
    else:
        parts = [str(status)]
    if out.get("changed") and out.get("topic"):
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
    onoff = out.get("control") == CONTROL_ONOFF
    lines: List[str] = []
    lines.append("Vloerverwarming — laden op de goedkoopste uren"
                 + (" (aan/uit via 1951)" if onoff else ""))
    lines.append("=" * 64)
    if "now" in out:
        lines.append(f"Nu       : {app.fmt_dt(datetime.fromisoformat(out['now']))}")
    if onoff:
        lines.append(
            "Regeling : in laadblok 1951=02 (Verwarmen), daarbuiten 1951=03 "
            "(Standby)"
        )
        lines.append(
            "SWW      : " + ("laden mijden aan" if out.get("avoid_dhw", True)
                             else "laden mijden uit")
        )
    else:
        lines.append(
            f"Regeling : laden {out.get('charge_temp', DEFAULT_CHARGE_TEMP):g} °C, "
            f"normaal {out.get('base_temp', DEFAULT_BASE_TEMP):g} °C, "
            f"duur uur {out.get('coast_temp', DEFAULT_COAST_TEMP):g} °C "
            f"(register {FIXED_TEMP_REGISTER})"
        )
        if out.get("base_temp_from_register"):
            lines.append(
                f"Rustwaarde: {out.get('base_temp', DEFAULT_BASE_TEMP):g} °C "
                f"uit register {FIXED_TEMP_REGISTER}/{SUPPLY_REGISTER}"
            )
    dw = out.get("dhw_window")
    if dw:
        lines.append(f"SWW-venster: {_fmt_iso(dw['start'])} – {_fmt_iso(dw['end'])} "
                     "(blokken die hiermee botsen vallen af)")

    status = out.get("status")
    if status == "disabled":
        lines.append("Status   : uit (mqtt.heating_comfort.enabled = false of mqtt uit)")
        return lines
    if status == "no_data":
        lines.append("Status   : geen prijs-/temperatuurdata"
                     + (" — 1951=Standby (veilig)" if onoff else " — aanvoer ongemoeid"))
        return lines
    if status == "dhw":
        lines.append("Status   : SWW laden actief — regeling pauzeert")
        if onoff:
            lines.append("           1951=Standby (niet tegelijk met SWW)")
        return lines

    if status == "standby":
        lines.append("Actie    : STANDBY -> 1951=03 (compressor uit)")
    elif status == "charge" and onoff:
        lines.append("Actie    : LADEN -> 1951=02 (Verwarmen, stooklijn kiest aanvoer)")
    else:
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

    if out.get("changed"):
        what = "Modus" if onoff else "Setpoint"
        if out.get("mqtt_published"):
            lines.append(
                f"{what:<9}: VERSTUURD → {out.get('topic')} "
                f"{json.dumps(out.get('payload'), ensure_ascii=False)}"
            )
        elif str(out.get("mqtt_detail", "")).startswith("dry-run"):
            lines.append(f"{what:<9}: (dry-run: niet gepubliceerd)")
        else:
            lines.append(f"{what:<9}: NIET verstuurd ({out.get('mqtt_detail')})")
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
    ap.add_argument("--base-temp", type=float, default=None,
                    help="rustwaarde overschrijven (anders uit config/register 1974)")
    ap.add_argument("--read-base", action="store_true",
                    help="de rustwaarde nu uit register 1974/5085 lezen en tonen")
    ap.add_argument("--control", choices=[CONTROL_SUPPLY, CONTROL_ONOFF], default=None,
                    help="regelwijze: 'supply' (1975-moduleren) of 'onoff' (1951 aan/uit)")
    ap.add_argument("--read-mode", action="store_true",
                    help="de hoofdmodus nu uit register 1951 lezen en tonen")
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
    if args.base_temp is not None:
        ccfg["base_temp"] = float(args.base_temp)
        ccfg["base_temp_from_register"] = False
    if args.control is not None:
        ccfg["control"] = args.control

    mqtt_cfg = cfg.get("mqtt") or {}
    state = load_state()

    if args.read_mode:
        mode = read_main_mode(mqtt_cfg, ccfg.get("base_read_timeout_seconds",
                                                 DEFAULT_BASE_READ_TIMEOUT))
        if mode is None:
            print("hoofdmodus: niet leesbaar (register 1951 gaf niets)", file=sys.stderr)
            return 1
        label = {"01": "Auto", "02": "Verwarmen", "03": "Standby",
                 "04": "Koelen"}.get(mode, "?")
        print(f"hoofdmodus (register {MAIN_MODE_REGISTER}): {mode} ({label})")
        return 0

    if args.read_base:
        temp = read_fixed_temp(mqtt_cfg, ccfg["base_read_timeout_seconds"])
        if temp is None:
            print("vaste waarde: niet leesbaar (register 1975/1974/5085 gaf niets)",
                  file=sys.stderr)
            return 1
        state["base_temp"] = temp
        save_state(state)
        print(f"vaste waarde (register {FIXED_TEMP_REGISTER}/"
              f"{FIXED_TEMP_FALLBACK}/{SUPPLY_REGISTER}): {temp:g} °C")
        return 0

    if args.watch and not args.now:
        return watch(cfg, tz, ccfg, dry_run=args.dry_run)

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    if ccfg.get("base_temp_from_register") and ccfg.get("control") != CONTROL_ONOFF:
        resolve_base_temp(ccfg, mqtt_cfg, state, log=lambda m: print(m, file=sys.stderr))

    rows: List[dict] = []
    granularity = 15
    try:
        rows, granularity, _source = build_rows(cfg)
    except Exception as exc:  # noqa: BLE001 — nette melding, ga door zonder data
        print(f"WAARSCHUWING: prijzen/voorspelling ophalen mislukt: {exc}", file=sys.stderr)

    opmode = read_opmode(mqtt_cfg, ccfg.get("base_read_timeout_seconds",
                                            DEFAULT_BASE_READ_TIMEOUT))
    dhw_window = dhw_charge_window(load_dhw_state())
    out = decide(ccfg, now, rows, granularity, state,
                 opmode=opmode, dhw_window=dhw_window)
    execute(out, mqtt_cfg, ccfg, state, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print("\n".join(render_human(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
