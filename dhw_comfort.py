#!/usr/bin/env python3
"""SWW-comfortregeling: houd het boilervat tussen 07:00 en 22:00 op peil.

Doel (eenvoudiger dan de boost-planner in `dhw_boost.py`):
  * tussen `window_from` en `window_to` (default 07:00-22:00) moet er **altijd
    genoeg warm douchewater** zijn: het vat mag niet onder `min_temp`
    (default 42 °C) zakken;
  * buiten dat venster mag het kouder zijn;
  * de opwarmmomenten worden gepland op basis van de **voorspelde
    buitentemperatuur per uur**, de **COP van de Remko** en de
    **stroomprijs per kwartier** (goedkoopste gecorrigeerde prijs =
    kWh-prijs / COP).

Hoe het werkt:
  1. lees de **echte** SWW-temperatuur (register 5039) via MQTT;
  2. voorspel de temperatuur vooruit met de gemiddelde afkoeling
     (`cooling_c_per_hour`, default 0,3 °C/h): `T(t) = T_nu - 0,3 * uren`;
  3. zoek het eerstvolgende moment **binnen het venster** waarop dat onder de
     buffer zou zakken (de "deadline");
  4. is die deadline dichtbij (`lead_hours`, default 4 u), dan wordt in het
     venster [nu, deadline] het **goedkoopste opwarmblok** gezocht op basis
     van `prijs / COP(buitentemp.)` en gaat op dat moment het setpoint
     (register 1082) naar `charge_temp` (default 52 °C);
  5. buiten zo'n blok staat het setpoint op de ondergrens: `buffer_temp`
     (default 47 °C) binnen het venster — de warmtepomp houdt het vat dan
     zelf op peil — en `off_temp` (default 35 °C) daarbuiten.

De **buffer** (`buffer_temp`, default 47 °C) is bewust hoger dan de harde
grens (`min_temp`, 42 °C): een douchebeurt verlaagt het vat met ongeveer
`shower_drop_c` (default 5 °C), dus pas met ~47 °C vóór de douche is er ná de
douche nog ≥ 42 °C over. Douchebeurten zijn onvoorspelbaar; de regeling leest
continu de échte temperatuur, dus een onverwachte daling wordt meteen gezien
en de volgende cyclus bijgeregeld — je hoeft geen doucheschema op te geven.

Buiten het venster mag er wél worden opgewarmd (`allow_outside_window`,
default true): 's nachts is de stroom vaak goedkoper, en zo is het vat om
07:00 al op temperatuur.

Gebruik:
  python3 dhw_comfort.py                 # één beslissing, en publiceren
  python3 dhw_comfort.py --watch         # als service: elke `interval_minutes`
  python3 dhw_comfort.py --dry-run      # niets publiceren, alleen tonen
  python3 dhw_comfort.py --temp 45.5 --now 2026-10-11T08:00:00+02:00
"""

from __future__ import annotations

import argparse
import json
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

# Registers
TANK_REGISTER = 5039        # boilertemperatuur (SWW)
SETPOINT_REGISTER = 1082    # gewenste SWW-temperatuur

# Defaults (zie de module-docstring)
DEFAULT_WINDOW_FROM = "07:00"
DEFAULT_WINDOW_TO = "22:00"
DEFAULT_MIN_TEMP = 42.0
DEFAULT_BUFFER_TEMP = 47.0
DEFAULT_CHARGE_TEMP = 52.0
DEFAULT_OFF_TEMP = 35.0
DEFAULT_TANK_LITERS = 300.0
DEFAULT_COOLING_C_PER_HOUR = 0.3
DEFAULT_SHOWER_DROP_C = 5.0
DEFAULT_SHOWER_DETECT_C = 2.0
DEFAULT_CHARGE_BLOCK_HOURS = 1.0
DEFAULT_LEAD_HOURS = 4.0
DEFAULT_INTERVAL_MINUTES = 5.0
DEFAULT_RESPONSE_TIMEOUT = 8.0
DEFAULT_ROWS_REFRESH_SECONDS = 1800.0
DEFAULT_PRICE_DAYS_AHEAD = 3
EPS = 0.05


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def comfort_cfg(cfg: dict) -> dict:
    """Lees de `mqtt.dhw_comfort`-sectie met verstandige defaults."""
    mqtt_cfg = cfg.get("mqtt") or {}
    cc = mqtt_cfg.get("dhw_comfort") or {}
    return {
        "enabled": bool(cc.get("enabled", True)),
        "window_from": cc.get("window_from", DEFAULT_WINDOW_FROM),
        "window_to": cc.get("window_to", DEFAULT_WINDOW_TO),
        "min_temp": float(cc.get("min_temp", DEFAULT_MIN_TEMP)),
        "buffer_temp": float(cc.get("buffer_temp", DEFAULT_BUFFER_TEMP)),
        "charge_temp": float(cc.get("charge_temp", DEFAULT_CHARGE_TEMP)),
        "off_temp": float(cc.get("off_temp", DEFAULT_OFF_TEMP)),
        "tank_liters": float(cc.get("tank_liters", DEFAULT_TANK_LITERS)),
        "cooling_c_per_hour": float(
            cc.get("cooling_c_per_hour", DEFAULT_COOLING_C_PER_HOUR)
        ),
        "shower_drop_c": float(cc.get("shower_drop_c", DEFAULT_SHOWER_DROP_C)),
        "shower_detect_c": float(
            cc.get("shower_detect_c", DEFAULT_SHOWER_DETECT_C)
        ),
        "charge_block_hours": float(
            cc.get("charge_block_hours", DEFAULT_CHARGE_BLOCK_HOURS)
        ),
        "lead_hours": float(cc.get("lead_hours", DEFAULT_LEAD_HOURS)),
        "allow_outside_window": bool(cc.get("allow_outside_window", True)),
        "include_handshake": bool(cc.get("include_handshake", False)),
        "interval_minutes": float(
            cc.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
        ),
        "response_timeout_seconds": float(
            cc.get("response_timeout_seconds", DEFAULT_RESPONSE_TIMEOUT)
        ),
        "rows_refresh_seconds": float(
            cc.get("rows_refresh_seconds", DEFAULT_ROWS_REFRESH_SECONDS)
        ),
        "price_days_ahead": int(cc.get("price_days_ahead", DEFAULT_PRICE_DAYS_AHEAD)),
        "qos": int(cc.get("qos", 1)),
        "retain": bool(cc.get("retain", False)),
    }


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testbaar)
# --------------------------------------------------------------------------- #

def _hhmm_minutes(hhmm: str) -> int:
    """'07:30' -> 450 minuten na middernacht."""
    hour, minute = (int(x) for x in str(hhmm).split(":"))
    return hour * 60 + minute


def _window_bounds(dt: datetime, ccfg: dict) -> Tuple[datetime, datetime]:
    """Begin en einde van het (kalenderdag)venster van `dt`."""
    fm = _hhmm_minutes(ccfg["window_from"])
    tm = _hhmm_minutes(ccfg["window_to"])
    start = dt.replace(hour=fm // 60, minute=fm % 60, second=0, microsecond=0)
    end = dt.replace(hour=tm // 60, minute=tm % 60, second=0, microsecond=0)
    return start, end


def in_window(dt: datetime, ccfg: dict) -> bool:
    """Valt `dt` binnen [window_from, window_to)? (halve-open)"""
    fm = _hhmm_minutes(ccfg["window_from"])
    tm = _hhmm_minutes(ccfg["window_to"])
    minutes = dt.hour * 60 + dt.minute
    if fm <= tm:
        return fm <= minutes < tm
    # venster loopt over middernacht (bijv. 22:00-06:00)
    return minutes >= fm or minutes < tm


def next_window(dt: datetime, ccfg: dict) -> Tuple[datetime, datetime]:
    """Het eerstvolgende (of huidige) venster op/na `dt`."""
    fm = _hhmm_minutes(ccfg["window_from"])
    tm = _hhmm_minutes(ccfg["window_to"])
    start, end = _window_bounds(dt, ccfg)
    if fm <= tm:
        if dt < start:
            return start, end
        if dt < end:
            return start, end
        s2, e2 = _window_bounds(dt + timedelta(days=1), ccfg)
        return s2, e2
    # over middernacht: liggen we na start of vóór einde?
    if dt >= start:
        return start, end + timedelta(days=1)
    if dt < end:
        return start - timedelta(days=1), end
    return start, end + timedelta(days=1)


def build_setpoint_payload(temperature: float) -> dict:
    """1082-waarde = gewenste SWW-temperatuur (°C × 10) in hexadecimaal.

    Zelfde wire-formaat als `dhw_boost.build_boost_payload`:
    47 °C -> 470 decimal -> 0x1D6 -> "01d6".
    """
    value_hex = format(int(round(temperature * 10.0)), "04x")
    return {"FORCE_RESPONSE": True, "values": {str(SETPOINT_REGISTER): value_hex}}


def _temp_at(T_now: float, now: datetime, t: datetime, cooling: float) -> float:
    return T_now - cooling * (t - now).total_seconds() / 3600.0


def first_shortfall(
    now: datetime,
    T_now: float,
    ccfg: dict,
    horizon_end: datetime,
    max_windows: int = 6,
) -> Optional[datetime]:
    """Het eerstvolgende moment **binnen het venster** waarop het vat
    (zonder bijwarmen) onder de buffer zou zakken — of None als dat binnen de
    horizon niet gebeurt.

    Buiten het venster geldt geen eis; wel moet het vat aan het begin van het
    volgende venster op peil zijn. Daarom kijkt de functie per venster naar
    het minimum (het venstereinde, want het koelt monotoon af)."""
    cool = ccfg["cooling_c_per_hour"]
    buf = ccfg["buffer_temp"]
    if cool <= 0:
        return None
    t = now
    for _ in range(max_windows):
        start, end = next_window(t, ccfg)
        if start >= horizon_end:
            return None
        s = max(start, now)
        if _temp_at(T_now, now, s, cool) < buf:
            return s
        if _temp_at(T_now, now, end, cool) < buf:
            hours = (T_now - buf) / cool
            return now + timedelta(hours=hours)
        # dit venster is ruim; kijk naar het volgende
        t = end + timedelta(seconds=1)
    return None


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


def cheapest_charge_block(
    rows: List[dict],
    granularity_min: int,
    ccfg: dict,
    now: datetime,
    deadline: datetime,
) -> Optional[dict]:
    """Het goedkoopste opwarmblok (op `prijs / COP`) dat vóór `deadline`
    eindigt en niet vóór `now` begint.

    Past er geen volledig blok meer vóór de deadline, dan begint het blok
    meteen (beter een fractie te laat dan de buffer missen). Zonder
    `allow_outside_window` moet het blok binnen het venster beginnen.
    """
    if not rows:
        return None
    block_hours = ccfg["charge_block_hours"]
    if deadline <= now:
        # De buffer is nu al (bijna) bereikt: meteen beginnen.
        return _immediate_block(now, block_hours)
    start_before = deadline - timedelta(hours=block_hours)
    if start_before > now:
        blocks = find_cheapest_blocks(
            rows,
            granularity_min=granularity_min,
            block_hours=block_hours,
            only_future=True,
            top_n=50,
            now=now,
            earliest_start=now,
            start_before=start_before,
        )
        for b in blocks:
            if not ccfg["allow_outside_window"] and not in_window(b["start"], ccfg):
                continue
            return b
        return None
    # deadline te dichtbij voor een volledig blok: meteen beginnen
    return _immediate_block(now, block_hours)


def _immediate_block(now: datetime, block_hours: float) -> dict:
    return {
        "start": now,
        "end": now + timedelta(hours=block_hours),
        "mean_corrected": None,
        "mean_cop": None,
        "mean_temp": None,
        "slots": [],
    }


def detect_shower(
    state: dict, now: datetime, T_now: float, ccfg: dict
) -> Optional[float]:
    """Onvoorspelbare douchebeurt: een daling die groter is dan de verwachte
    afkoeling sinds het vorige sample. Geeft de extra daling (°C) terug, of
    None. Puur informatief — de regeling gebruikt altijd de échte temperatuur.
    """
    prev = state.get("last_temp")
    prev_at = state.get("last_temp_at")
    drop = None
    if prev is not None and prev_at:
        try:
            hours = (now - datetime.fromisoformat(prev_at)).total_seconds() / 3600.0
        except (ValueError, TypeError):
            hours = None
        if hours and hours > 0:
            expected = ccfg["cooling_c_per_hour"] * hours
            observed = float(prev) - T_now
            if observed - expected >= ccfg["shower_detect_c"]:
                drop = observed
    return drop


# --------------------------------------------------------------------------- #
# Beslissing
# --------------------------------------------------------------------------- #

def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def decide(
    ccfg: dict,
    now: datetime,
    T_now: Optional[float],
    rows: List[dict],
    granularity_min: int,
    state: Optional[dict] = None,
    horizon_end: Optional[datetime] = None,
) -> dict:
    """Bepaal het gewenste setpoint (register 1082) en de status.

    Geeft een dict met o.a. `status`, `setpoint_temp`, `deadline` en `block`.
    `setpoint_temp` is None als er niets te doen/te weten valt (niet lezen).
    """
    state = state or {}
    out: dict = {
        "now": now.isoformat(),
        "tank_temp": T_now,
        "enabled": ccfg["enabled"],
        "window_from": ccfg["window_from"],
        "window_to": ccfg["window_to"],
        "min_temp": ccfg["min_temp"],
        "buffer_temp": ccfg["buffer_temp"],
        "charge_temp": ccfg["charge_temp"],
    }
    if not ccfg["enabled"]:
        out["status"] = "disabled"
        out["setpoint_temp"] = None
        return out

    in_win = in_window(now, ccfg)
    floor = ccfg["buffer_temp"] if in_win else ccfg["off_temp"]
    out["in_window"] = in_win
    out["floor_temp"] = floor

    if T_now is None:
        # Geen meting: laat het laatst gezette setpoint ongemoeid.
        out["status"] = "no_read"
        out["setpoint_temp"] = None
        return out

    charge_temp = ccfg["charge_temp"]

    # Al warm genoeg? Dan niet (bij)laden: het setpoint mag terug naar de
    # ondergrens, zodat het vat rustig uitzakt in plaats van op charge_temp
    # te blijven hangen.
    if T_now >= charge_temp - EPS:
        out["status"] = "hot"
        out["setpoint_temp"] = floor
        out["charge_start"] = None
        out["charge_end"] = None
        return out

    # Loopt er al een opwarmblok?
    a_start = _parse_dt(state.get("charge_start"))
    a_end = _parse_dt(state.get("charge_end"))
    active = bool(a_start and a_end and a_start <= now < a_end)
    if active:
        out["status"] = "charge"
        out["setpoint_temp"] = charge_temp
        out["charge_start"] = a_start.isoformat()
        out["charge_end"] = a_end.isoformat()
        out["charge_until"] = a_end.isoformat()
        return out

    if not rows:
        # Geen prijs-/temperatuurdata: wel de veilige ondergrens aanhouden.
        out["status"] = "no_data"
        out["setpoint_temp"] = floor
        out["charge_start"] = None
        out["charge_end"] = None
        return out

    if horizon_end is None:
        horizon_end = rows[-1]["dt_local"] + timedelta(minutes=granularity_min)
    deadline = first_shortfall(now, T_now, ccfg, horizon_end)
    out["deadline"] = deadline.isoformat() if deadline else None

    if deadline is None:
        out["status"] = "idle"
        out["setpoint_temp"] = floor
        out["charge_start"] = None
        out["charge_end"] = None
        return out

    # Alleen plannen als de deadline binnen de lead-horizon valt. Anders zou
    # de regeling direct ná een opwarming meteen weer het goedkoopste blok
    # "nu" kiezen en het vat onnodig op charge_temp houden.
    lead = timedelta(hours=ccfg["lead_hours"])
    if deadline - now > lead:
        out["status"] = "wait"
        out["setpoint_temp"] = floor
        out["charge_start"] = None
        out["charge_end"] = None
        return out

    block = cheapest_charge_block(rows, granularity_min, ccfg, now, deadline)
    out["block"] = _block_out(block)
    if block is None:
        out["status"] = "idle"
        out["setpoint_temp"] = floor
        out["charge_start"] = None
        out["charge_end"] = None
        return out

    slack = timedelta(minutes=max(1, granularity_min))
    if block["start"] <= now + slack:
        cstart = now
        cend = max(block["end"], now + timedelta(hours=ccfg["charge_block_hours"]))
        out["status"] = "charge"
        out["setpoint_temp"] = charge_temp
        out["charge_start"] = cstart.isoformat()
        out["charge_end"] = cend.isoformat()
        out["charge_until"] = cend.isoformat()
    else:
        out["status"] = "schedule"
        out["setpoint_temp"] = floor
        out["charge_start"] = None
        out["charge_end"] = None
    return out


# --------------------------------------------------------------------------- #
# Statusfile
# --------------------------------------------------------------------------- #

def state_path() -> str:
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "remko-wkf70")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "dhw_comfort_state.json")


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
    """Publiceer het setpoint als het veranderd is; werk de statusfile bij."""
    setpoint = out.get("setpoint_temp")
    if setpoint is None:
        out["setpoint_changed"] = False
        return out

    last = state.get("last_sent_setpoint")
    changed = last is None or abs(float(last) - float(setpoint)) > 1e-6
    out["setpoint_changed"] = changed

    # Het opwarmvenster altijd in de statusfile bijwerken (ook als het
    # setpoint gelijk blijft), zodat een herstart midden in een blok klopt.
    if out.get("status") == "charge":
        state["charge_start"] = out.get("charge_start")
        state["charge_end"] = out.get("charge_end")
    else:
        state.pop("charge_start", None)
        state.pop("charge_end", None)

    if not changed:
        save_state(state)
        return out

    topic = energy_log.command_topic(mqtt_cfg)
    out["topic"] = topic
    out["payload"] = build_setpoint_payload(float(setpoint))

    if dry_run:
        out["mqtt_published"] = False
        out["mqtt_detail"] = "dry-run: niet gepubliceerd"
        save_state(state)
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
# Data (prijzen + voorspelling -> gecorrigeerde rijen)
# --------------------------------------------------------------------------- #

def dhw_cop_model(hp_cfg: dict) -> cop_mod.CopModel:
    """COP-model voor SWW (eigen aanvoertemperatuur/curve, zie heatpump.dhw)."""
    dhw = hp_cfg.get("dhw") or {}
    temp = int(dhw.get("temperature", 53))
    key = dhw.get("curve_key", f"cop_curve_w{temp}")
    curve = hp_cfg.get(key)
    if curve is not None:
        return cop_mod.CopModel(supply_temperature=temp, curve=curve)
    return cop_mod.CopModel(supply_temperature=temp)


def build_rows(cfg: dict) -> Tuple[List[dict], int, str]:
    """Gecorrigeerde prijsrijen (prijs/COP) uit prijzen + uurvoorspelling."""
    loc = cfg["location"]
    fc = cfg.get("forecast") or {}
    hp = cfg.get("heatpump") or {}
    prices_cfg = cfg.get("prices") or {}

    model = dhw_cop_model(hp)
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
    return rows, int(pr.get("granularity_min", 15)), pr.get("source", "")


# --------------------------------------------------------------------------- #
# Meten (MQTT)
# --------------------------------------------------------------------------- #

def read_tank(mqtt_cfg: dict, ccfg: dict, tz: ZoneInfo) -> Optional[float]:
    """Lees de SWW-temperatuur één keer via MQTT (of None bij timeout)."""
    ecfg = {
        "registers": [TANK_REGISTER],
        "response_timeout_seconds": ccfg["response_timeout_seconds"],
    }
    row = energy_log.collect_once(mqtt_cfg, ecfg, tz, ccfg.get("include_handshake", False))
    if not row:
        return None
    value = row.get("water_temp_c")
    return float(value) if value is not None else None


def _log(*parts) -> None:
    ts = datetime.now().strftime("%d-%m %H:%M:%S")
    print(f"[{ts}] " + " ".join(str(p) for p in parts), flush=True)


# --------------------------------------------------------------------------- #
# Watcher / service
# --------------------------------------------------------------------------- #

def watch(
    cfg: dict,
    tz: ZoneInfo,
    ccfg: dict,
    dry_run: bool = False,
) -> int:
    """Blijf draaien: elke `interval_minutes` meten, beslissen en bijsturen."""
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
        "SWW-comfort gestart — venster "
        f"{ccfg['window_from']}-{ccfg['window_to']}, min {ccfg['min_temp']:g} °C, "
        f"buffer {ccfg['buffer_temp']:g} °C, laden tot {ccfg['charge_temp']:g} °C; "
        f"meten elke {ccfg['interval_minutes']:g} min"
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
                    # de oude rijen blijven bruikbaar; niet elke cyclus opnieuw
                    rows_at = time.time()

        try:
            temp = read_tank(mqtt_cfg, ccfg, tz)
        except Exception as exc:  # noqa: BLE001
            _log("FOUT: temperatuur lezen mislukt:", exc)
            temp = None

        shower = None
        if temp is not None:
            shower = detect_shower(state, now, temp, ccfg)

        out = decide(ccfg, now, temp, rows, granularity, state)
        execute(out, mqtt_cfg, ccfg, state, dry_run)
        if shower is not None:
            state["last_shower_at"] = now.isoformat()
            state["showers"] = int(state.get("showers", 0)) + 1
            state["last_shower_drop"] = round(shower, 1)
        if temp is not None:
            state["last_temp"] = temp
            state["last_temp_at"] = now.isoformat()
        save_state(state)

        _log(_status_log(out, shower))
        stop.wait(ccfg["interval_minutes"] * 60.0)

    _log("SWW-comfort gestopt")
    return 0


def _status_log(out: dict, shower: Optional[float]) -> str:
    status = out.get("status")
    temp = out.get("tank_temp")
    temp_txt = f"{temp:.1f} °C" if isinstance(temp, (int, float)) else "?"
    parts = [f"vat {temp_txt}"]
    if out.get("floor_temp") is not None:
        parts.append(
            f"ondergrens {out['floor_temp']:g} °C"
            + (" (in venster)" if out.get("in_window") else " (buiten venster)")
        )
    if status == "charge":
        parts.append(f"LADEN -> setpoint {out['setpoint_temp']:g} °C")
    elif status == "hot":
        parts.append(f"warm genoeg -> setpoint {out['setpoint_temp']:g} °C")
    elif status == "schedule" and out.get("block"):
        b = out["block"]
        parts.append(
            f"opwarmblok gepland {_fmt_iso(b['start'])}-{_fmt_iso(b['end'])}"
            + (
                f" ({b['mean_corrected']:.4f} €/kWh warmte, COP {b['mean_cop']:.2f})"
                if b.get("mean_corrected") is not None
                else ""
            )
        )
    elif status == "wait":
        parts.append(f"wacht; deadline {_fmt_iso(out.get('deadline'))}")
    elif status == "idle":
        parts.append("niets te doen")
    elif status == "no_read":
        parts.append("GEEN meting (gateway antwoordt niet)")
    elif status == "no_data":
        parts.append("geen prijs-/temperatuurdata — alleen ondergrens")
    elif status == "disabled":
        parts.append("uit")
    if out.get("setpoint_changed") and out.get("topic"):
        parts.append(
            f"VERSTUURD {json.dumps(out.get('payload'), ensure_ascii=False)}"
            if out.get("mqtt_published")
            else f"NIET verstuurd ({out.get('mqtt_detail')})"
        )
    if shower is not None:
        parts.append(f"douche gedetecteerd (daling {shower:.1f} °C)")
    return " | ".join(parts)


def _fmt_iso(iso: Optional[str]) -> str:
    dt = _parse_dt(iso)
    if dt is None:
        return "?"
    return f"{app.DUTCH_DAYS[dt.weekday()][:3]} {dt:%d-%m %H:%M}"


# --------------------------------------------------------------------------- #
# Tekstuele weergave
# --------------------------------------------------------------------------- #

def render_human(out: dict) -> List[str]:
    lines: List[str] = []
    lines.append("SWW-comfort — vat op peil tussen de douchetijden")
    lines.append("=" * 64)
    if "now" in out:
        lines.append(f"Nu       : {app.fmt_dt(datetime.fromisoformat(out['now']))}")
    lines.append(
        f"Regeling : min {out.get('min_temp', DEFAULT_MIN_TEMP):g} °C "
        f"({out.get('window_from', DEFAULT_WINDOW_FROM)}–"
        f"{out.get('window_to', DEFAULT_WINDOW_TO)}), "
        f"buffer {out.get('buffer_temp', DEFAULT_BUFFER_TEMP):g} °C, "
        f"laden tot {out.get('charge_temp', DEFAULT_CHARGE_TEMP):g} °C"
    )
    temp = out.get("tank_temp")
    temp_txt = f"{temp:.1f} °C" if isinstance(temp, (int, float)) else "?"

    status = out.get("status")
    if status == "disabled":
        lines.append("Status   : uit (mqtt.dhw_comfort.enabled = false of mqtt uit)")
        return lines

    if status == "no_read":
        lines.append("Vat      : ? (gateway antwoordt niet — setpoint ongemoeid)")
        return lines

    lines.append(
        f"Vat      : {temp_txt}  ("
        + ("binnen" if out.get("in_window") else "buiten")
        + f" venster; ondergrens nu {out.get('floor_temp'):g} °C)"
    )
    if out.get("deadline"):
        dl = datetime.fromisoformat(out["deadline"])
        lines.append(
            f"Deadline : {app.fmt_dt(dl)} (zonder bijwarmen onder "
            f"{out.get('buffer_temp'):g} °C)"
        )
    b = out.get("block")
    if b:
        start_txt = app.fmt_dt(datetime.fromisoformat(b["start"]))
        end_txt = app.fmt_dt(datetime.fromisoformat(b["end"]))
        if b.get("mean_corrected") is not None:
            lines.append(
                f"Opwarmblok: {start_txt} – {end_txt} "
                f"(COP {app.nl(b['mean_cop'], 2)}, "
                f"{app.nl(b['mean_corrected'], 4)} €/kWh warmte)"
            )
        else:
            lines.append(f"Opwarmblok: {start_txt} – {end_txt}")

    sp = out.get("setpoint_temp")
    if sp is not None:
        sp_txt = f"{sp:g} °C"
        if out.get("setpoint_changed"):
            if out.get("mqtt_published"):
                lines.append(
                    f"Setpoint : {sp_txt} — VERSTUURD → {out.get('topic')} "
                    f"{json.dumps(out.get('payload'), ensure_ascii=False)}"
                )
            elif str(out.get("mqtt_detail", "")).startswith("dry-run"):
                lines.append(f"Setpoint : {sp_txt} (dry-run — niets gepubliceerd)")
            else:
                lines.append(
                    f"Setpoint : {sp_txt} — FOUT niet gepubliceerd "
                    f"({out.get('mqtt_detail')})"
                )
        else:
            lines.append(f"Setpoint : {sp_txt} (ongewijzigd)")

    status_txt = {
        "charge": "laden (opwarmblok actief)",
        "hot": "vat is warm genoeg — laten uitzakken",
        "schedule": "opwarmblok gepland",
        "wait": "wachten tot de deadline in de lead-horizon komt",
        "idle": "niets te doen",
        "no_data": "geen prijs-/temperatuurdata — alleen ondergrens aangehouden",
    }.get(status, status or "?")
    lines.append(f"Status   : {status_txt}")
    return lines


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="SWW-comfort: houd het boilervat op peil tussen de douchetijden"
    )
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.json"))
    ap.add_argument("--now", default=None, help="ISO-tijdstip voor 'nu' (testen)")
    ap.add_argument(
        "--temp", type=float, default=None,
        help="gemeten SWW-temperatuur (i.p.v. de gateway uitlezen)",
    )
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
    ccfg = comfort_cfg(cfg)
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
    except Exception as exc:  # noqa: BLE001 — nette melding, ga door met ondergrens
        print(f"WAARSCHUWING: prijzen/voorspelling ophalen mislukt: {exc}", file=sys.stderr)

    if args.temp is not None:
        temp = args.temp
    elif mqtt_cfg.get("enabled"):
        try:
            temp = read_tank(mqtt_cfg, ccfg, tz)
        except Exception as exc:  # noqa: BLE001
            print(f"WAARSCHUWING: temperatuur lezen mislukt: {exc}", file=sys.stderr)
            temp = None
    else:
        temp = None

    state = load_state()
    if args.temp is not None:
        # testinvoer: geen half opwarmblok uit een vorige run meenemen
        state.pop("charge_start", None)
        state.pop("charge_end", None)
    out = decide(ccfg, now, temp, rows, granularity, state)
    execute(out, mqtt_cfg, ccfg, state, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print("\n".join(render_human(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
