#!/usr/bin/env python3
"""Energie-logger voor de REMKO WKF 70 (NEO) via de gateway-MQTT.

Leest periodiek de energietellers en temperaturen uit de Remko-gateway en
schrijft ze als tijdreeks naar een CSV-bestand. Daarmee kun je later per
SWW-opwarmepisode de *echte* COP en de kosten bepalen — inclusief de
herverwarmingen die buiten onze boost-blokken vallen. Dat is precies wat je
nodig hebt om de setpoint-vraag (bv. 53 °C vs. 48 °C vs. 45 °C) te beslechten.

Protocol (overgenomen uit de Home Assistant-integratie Altrec/remko_mqtt-ha):
- data    : <node>/SMTID/HOST2CLIENT   payload {"values": {"<reg>": "<hex>", ...}}
- commando: <node>/SMTID/CLIENT2HOST   payload {"FORCE_RESPONSE": true,
            "query_list": [<reg>, ...]}  -> de gateway antwoordt op HOST2CLIENT.

Registers (het type bepaalt de decodering):
  5105 energy_electric (kWh)     5374 energy_heating (kWh)
  5376 energy_dhw (kWh)          5600 energy_environmental (kWh)
  5001 opmode                    5822 compressor_starts
  5032 out_temp                  5039 water_temp
  5085 heat_water_temp_req       5190 heating_actual_temp
  1082 water_temp_req
- energy / counter / mode: int(hex)
- temp: 16-bits signed, gedeeld door 10

Gebruik:
  python3 energy_log.py --once            # één sample lezen en tonen (verifiëren)
  python3 energy_log.py --dump            # 30 s alle MQTT-berichten tonen
  python3 energy_log.py                   # als service: elke 60 s een CSV-regel
  python3 energy_log.py --report          # samenvatting per SWW-opwarmepisode
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import main as app
import mqtt_out

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_INTERVAL = 60.0        # seconden tussen samples
DEFAULT_RESPONSE_TIMEOUT = 8.0  # seconden wachten op het antwoord van de gateway

# Vaste veldvolgorde -> stabiele CSV-kolommen.
# (register, kolomnaam, type)
FIELDS: List[Tuple[int, str, str]] = [
    (5105, "energy_electric_kwh", "energy"),
    (5374, "energy_heating_kwh", "energy"),
    (5376, "energy_dhw_kwh", "energy"),
    (5600, "energy_environmental_kwh", "energy"),
    (5001, "opmode", "mode"),
    (5032, "out_temp_c", "temp"),
    (5039, "water_temp_c", "temp"),
    (5085, "heat_water_temp_req_c", "temp"),
    (5190, "heating_actual_temp_c", "temp"),
    (1082, "water_temp_req_c", "temp"),
    (5822, "compressor_starts", "counter"),
]
REG_BY_ID: Dict[int, Tuple[str, str]] = {reg: (name, kind) for reg, name, kind in FIELDS}
ALL_REGISTERS: List[int] = [reg for reg, _, _ in FIELDS]

# Kolom met de op dat moment geldende, all-in variabele stroomprijs (€/kWh,
# inclusief price_adjustments). De logger schrijft hem bij elke sample mee,
# zodat het rapport de kosten kan uitrekenen zónder historische prijzen op te
# hoeven halen (de dag-ahead-API's leveren alleen vanaf vandaag vooruit).
PRICE_FIELD = "price_eur_per_kwh"

# De HA-integratie stuurt deze drie registers bij elke keep-alive mee als
# "handshake". Ze staan niet in de registermap; standaard schrijven we ze niet
# (de logger is in de basis read-only). Zet include_handshake/`--handshake`
# aan als de gateway zónder deze waarden niet antwoordt.
HANDSHAKE = {"5074": "0255", "5106": "0000", "5109": "0000"}


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testbaar)
# --------------------------------------------------------------------------- #

def decode(kind: str, value_hex) -> float:
    """Decodeer één hex-waarde volgens het registertype."""
    raw = int(str(value_hex), 16)
    if kind == "temp":
        if raw >= 0x8000:
            raw -= 0x10000
        return raw / 10.0
    return raw


def parse_values(payload: str) -> Dict[int, float]:
    """Zet een HOST2CLIENT-payload om naar {register: waarde}.

    Onbekende registers, niet-JSON en ondecodeerbare waarden worden stil
    overgeslagen — de gateway stuurt veel meer dan wij bijhouden.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, dict):
        return {}
    out: Dict[int, float] = {}
    for reg_str, val in values.items():
        try:
            reg = int(reg_str)
        except (ValueError, TypeError):
            continue
        if reg not in REG_BY_ID:
            continue
        _, kind = REG_BY_ID[reg]
        try:
            out[reg] = decode(kind, val)
        except (ValueError, TypeError):
            continue
    return out


def command_topic(mqtt_cfg: dict) -> str:
    """Het CLIENT2HOST-topic waarop commando's/query's worden gepubliceerd."""
    base = (mqtt_cfg.get("topic_base") or "remko/wkf70").rstrip("/")
    return (mqtt_cfg.get("control_topic") or base).strip()


def data_topic(mqtt_cfg: dict) -> str:
    """Het HOST2CLIENT-topic waarop de gateway de status publiceert."""
    explicit = (mqtt_cfg.get("data_topic") or "").strip()
    if explicit:
        return explicit
    cmd = command_topic(mqtt_cfg)
    if not cmd:
        return ""
    if "CLIENT2HOST" in cmd:
        return cmd.replace("CLIENT2HOST", "HOST2CLIENT")
    if "/" in cmd:
        return cmd.rsplit("/", 1)[0] + "/HOST2CLIENT"
    return cmd + "/HOST2CLIENT"


def build_query(registers: List[int], include_handshake: bool = False) -> dict:
    """Payload die een statusreactie op HOST2CLIENT afdwingt."""
    payload: dict = {"FORCE_RESPONSE": True, "query_list": list(registers)}
    if include_handshake:
        payload["values"] = dict(HANDSHAKE)
    return payload


def default_csv_path() -> str:
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "remko-wkf70")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "dhw_energy.csv")


def energy_cfg(cfg: dict) -> dict:
    """Lees de `mqtt.energy_log`-sectie met verstandige defaults."""
    mqtt_cfg = cfg.get("mqtt") or {}
    ec = mqtt_cfg.get("energy_log") or {}
    registers = ec.get("registers")
    regs = [int(r) for r in registers] if registers else list(ALL_REGISTERS)
    return {
        "enabled": bool(ec.get("enabled", True)),
        "interval_seconds": float(ec.get("interval_seconds", DEFAULT_INTERVAL)),
        "response_timeout_seconds": float(
            ec.get("response_timeout_seconds", DEFAULT_RESPONSE_TIMEOUT)
        ),
        "csv_path": os.path.expanduser(ec.get("csv_path") or default_csv_path()),
        "include_handshake": bool(ec.get("include_handshake", False)),
        "log_price": bool(ec.get("log_price", True)),
        "price_refresh_seconds": float(ec.get("price_refresh_seconds", 3600)),
        "price_days_ahead": int(ec.get("price_days_ahead", 2)),
        "registers": regs,
    }


def csv_columns(registers: List[int], include_price: bool = False) -> List[str]:
    """CSV-kolommen voor de opgegeven registers (in vaste FIELDS-volgorde).

    Met `include_price` komt de all-in stroomprijs als laatste kolom erbij.
    """
    cols = ["timestamp"] + [
        name for reg, name, _ in FIELDS if reg in set(registers)
    ]
    if include_price:
        cols.append(PRICE_FIELD)
    return cols


def _csv_header(path: str) -> List[str]:
    try:
        with open(path, "r", newline="", encoding="utf-8") as fh:
            first = fh.readline().rstrip("\r\n")
        return next(csv.reader([first])) if first else []
    except OSError:
        return []


def append_row(path: str, columns: List[str], row: dict) -> None:
    """Voeg één regel toe; schrijf de kop alleen als het bestand nieuw is.

    Verandert de kolomset (bv. doordat je `log_price` aan/uit zet), dan wordt
    het oude bestand opzij gezet als `<path>.<tijdstempel>.bak` en met de nieuwe
    kolommen opnieuw begonnen — anders zouden de regels scheef komen te staan.
    """
    if os.path.exists(path) and os.path.getsize(path) > 0:
        if _csv_header(path) != list(columns):
            bak = f"{path}.{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
            os.replace(path, bak)
    is_new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def read_rows(path: str) -> List[dict]:
    with open(path, "r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _f(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _mean(values: List[float]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None


def _mode(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return max(set(values), key=values.count)


class PriceBook:
    """All-in stroomprijzen per slot, om aan de samples te koppelen.

    `price_at(dt)` geeft de prijs van het slot dat op of vóór `dt` begint
    (een day-ahead-prijs geldt voor het interval dat erop volgt). Buiten het
    venster: None. De correcties (`price_adjustments`) worden meteen
    toegepast, zodat de gelogde prijs gelijk is aan wat het advies rekent.
    """

    def __init__(self) -> None:
        self._slots: List[Tuple[datetime, float]] = []
        self._dts: List[datetime] = []
        self.source = ""
        self.fetched_at: Optional[datetime] = None

    def set_slots(self, slots, source: str = "") -> None:
        self._slots = sorted((dt, float(p)) for dt, p in slots)
        self._dts = [dt for dt, _ in self._slots]
        self.source = source
        self.fetched_at = datetime.now()

    def refresh(self, cfg: dict, tz: ZoneInfo, days_ahead: int = 2) -> bool:
        """Haal (gecachte) day-ahead prijzen op en pas de correcties toe."""
        prices_cfg = cfg.get("prices") or {}
        loc = cfg.get("location") or {}
        if not prices_cfg:
            return False
        try:
            pr, _used = app._fetch_prices_with_fallback(prices_cfg, loc, days_ahead)
        except Exception:  # noqa: BLE001 — prijzen zijn optioneel voor de logger
            return False
        adj = prices_cfg.get("price_adjustments") or {}
        vat = float(adj.get("vat_pct", 0.0))
        tax = float(adj.get("fixed_tax_per_kwh", 0.0))
        slots = [
            (dt, price * (1.0 + vat / 100.0) + tax)
            for dt, price in pr["slots"]
        ]
        self._slots = sorted(slots)
        self._dts = [dt for dt, _ in self._slots]
        self.source = pr.get("source", "")
        self.fetched_at = datetime.now(tz)
        return bool(self._slots)

    def price_at(self, dt: datetime) -> Optional[float]:
        if not self._slots:
            return None
        i = bisect.bisect_right(self._dts, dt) - 1
        if i < 0:
            return None
        return self._slots[i][1]


def summarize_episodes(rows: List[dict], max_gap: int = 2) -> List[dict]:
    """Detecteer SWW-opwarmepisodes uit de tijdreeks.

    Een sample is 'actief' als de DHW-teller sinds het vorige sample steeg.
    Korte onderbrekingen (max `max_gap` samples) worden overbrugd. Per episode
    wordt de COP = ΔDHW / Δelektrisch berekend; is er een prijskolom, dan ook
    de kosten (Δelektrisch × het tarief van het bijbehorende interval) en de
    setpoint (`water_temp_req_c`, dus boost- vs. basistemperatuur).
    """
    episodes: List[dict] = []
    cur: Optional[dict] = None
    gap = 0
    prev: Optional[dict] = None

    for r in rows:
        ts = r.get("timestamp")
        dhw = _f(r.get("energy_dhw_kwh"))
        el = _f(r.get("energy_electric_kwh"))
        out = _f(r.get("out_temp_c"), default=float("nan"))
        setp = _f(r.get("water_temp_req_c"), default=float("nan"))
        price = _f(r.get("price_eur_per_kwh"), default=float("nan"))

        if prev is not None:
            d_dhw = dhw - prev["dhw"]
            d_el = el - prev["el"]
            if d_dhw > 1e-9:
                if cur is None:
                    cur = {
                        "start": prev["ts"],
                        "end": ts,
                        "dhw_kwh": 0.0,
                        "el_kwh": 0.0,
                        "cost_eur": 0.0,
                        "price_seen": False,
                        "out": [],
                        "setpoints": [],
                    }
                cur["end"] = ts
                cur["dhw_kwh"] += d_dhw
                d_el_pos = max(d_el, 0.0)
                cur["el_kwh"] += d_el_pos
                p = prev["price"]
                if p == p:  # geen NaN
                    cur["cost_eur"] += d_el_pos * p
                    cur["price_seen"] = True
                if out == out:
                    cur["out"].append(out)
                if setp == setp:
                    cur["setpoints"].append(setp)
                gap = 0
            elif cur is not None:
                gap += 1
                if gap > max_gap:
                    episodes.append(cur)
                    cur = None

        prev = {"ts": ts, "dhw": dhw, "el": el, "price": price}

    if cur is not None:
        episodes.append(cur)

    for ep in episodes:
        ep["cop"] = (ep["dhw_kwh"] / ep["el_kwh"]) if ep["el_kwh"] > 0 else None
        ep["minutes"] = _minutes_between(ep["start"], ep["end"])
        ep["out_mean"] = _mean(ep["out"])
        ep["cost_eur"] = ep["cost_eur"] if ep["price_seen"] else None
        ep["price_mean"] = (
            ep["cost_eur"] / ep["el_kwh"]
            if (ep["price_seen"] and ep["el_kwh"] > 0)
            else None
        )
        sp = _mode(ep["setpoints"])
        ep["setpoint"] = round(sp, 1) if sp is not None else None
        ep.pop("price_seen", None)
        ep.pop("out", None)
        ep.pop("setpoints", None)
    return episodes


def group_by_setpoint(episodes: List[dict]) -> List[Tuple[Optional[float], List[dict]]]:
    groups: Dict[Optional[float], List[dict]] = {}
    order: List[Optional[float]] = []
    for e in episodes:
        key = e.get("setpoint")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    return [(k, groups[k]) for k in order]


def _minutes_between(a, b) -> Optional[float]:
    try:
        da = datetime.fromisoformat(str(a))
        db = datetime.fromisoformat(str(b))
        return (db - da).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def render_report(episodes: List[dict], rows: List[dict]) -> List[str]:
    lines = [f"SWW-opwarmepisodes: {len(episodes)} (uit {len(rows)} samples)"]
    if not episodes:
        lines.append("  (nog geen DHW-tellerstijging gezien — draait de logger al?)")
        return lines

    total_dhw = sum(e["dhw_kwh"] for e in episodes)
    total_el = sum(e["el_kwh"] for e in episodes)
    overall = (total_dhw / total_el) if total_el > 0 else None
    has_cost = any(e["cost_eur"] is not None for e in episodes)

    for e in episodes:
        cop = f"{e['cop']:.2f}" if e["cop"] is not None else "?"
        out = f"{e['out_mean']:.1f} °C" if e["out_mean"] is not None else "?"
        mins = f"{e['minutes']:.0f} min" if e["minutes"] is not None else "?"
        extra = ""
        if e["cost_eur"] is not None:
            extra += f"  kosten €{e['cost_eur']:.3f}"
        if e["setpoint"] is not None:
            extra += f"  setpoint {e['setpoint']:g} °C"
        lines.append(
            f"  {e['start']} -> {e['end']}  ({mins})  "
            f"ΔDHW {e['dhw_kwh']:.2f} kWh  ΔEl {e['el_kwh']:.2f} kWh  "
            f"COP {cop}  buiten {out}{extra}"
        )

    overall_txt = f"{overall:.2f}" if overall is not None else "?"
    total_line = (
        f"  Totaal: ΔDHW {total_dhw:.1f} kWh  ΔEl {total_el:.1f} kWh  "
        f"gem. COP {overall_txt}"
    )
    if has_cost:
        total_cost = sum(e["cost_eur"] for e in episodes if e["cost_eur"] is not None)
        eff = (total_cost / total_el) if total_el > 0 else None
        eff_txt = f"{eff:.3f}" if eff is not None else "?"
        total_line += f"  kosten €{total_cost:.2f} (gem. {eff_txt} €/kWh)"
    lines.append(total_line)
    if not has_cost:
        lines.append(
            "  (geen prijzen in de CSV — zet mqtt.energy_log.log_price op true "
            "voor de kostanalyse)"
        )
        return lines

    # Per setpoint: hiermee vergelijk je de strategieën (bv. 53 vs. 48 vs. 45 °C)
    groups = group_by_setpoint(episodes)
    if any(k is not None for k, _ in groups):
        lines.append("  Per setpoint:")
        for sp, eps in groups:
            g_dhw = sum(e["dhw_kwh"] for e in eps)
            g_el = sum(e["el_kwh"] for e in eps)
            g_costs = [e["cost_eur"] for e in eps if e["cost_eur"] is not None]
            g_cop = (g_dhw / g_el) if g_el > 0 else None
            label = f"{sp:g} °C" if sp is not None else "onbekend"
            cop_txt = f"{g_cop:.2f}" if g_cop is not None else "?"
            part = (
                f"    {label:>9}: {len(eps):>3} episodes  ΔDHW {g_dhw:6.1f} kWh  "
                f"ΔEl {g_el:5.1f} kWh  COP {cop_txt}"
            )
            if g_costs:
                part += f"  kosten €{sum(g_costs):.2f}"
            lines.append(part)
    return lines


def _log(*parts) -> None:
    ts = datetime.now().strftime("%d-%m %H:%M:%S")
    print(f"[{ts}] " + " ".join(str(p) for p in parts), flush=True)


# --------------------------------------------------------------------------- #
# MQTT-client
# --------------------------------------------------------------------------- #

def _make_connected_client(mqtt, mqtt_cfg: dict):
    """Bouw een client en verbind (keepalive 60, automatische reconnect)."""
    client = mqtt_out._make_client(mqtt)
    username = mqtt_cfg.get("username")
    if username:
        client.username_pw_set(username, mqtt_cfg.get("password"))
    try:
        client.reconnect_delay_set(min_delay=5, max_delay=120)
    except Exception:  # noqa: BLE001 — oudere paho's
        pass
    client.connect(mqtt_cfg["host"], int(mqtt_cfg["port"]), keepalive=60)
    return client


class EnergyLogger:
    """Draait als service: sample -> CSV-regel, met signaal-afhandeling."""

    def __init__(self, cfg: dict, mqtt_cfg: dict, ecfg: dict, tz: ZoneInfo,
                 dry_run: bool = False):
        self.cfg = cfg
        self.mqtt_cfg = mqtt_cfg
        self.ecfg = ecfg
        self.tz = tz
        self.dry_run = dry_run
        self.cmd_topic = command_topic(mqtt_cfg)
        self.data_topic = data_topic(mqtt_cfg)
        self.qos = int(mqtt_cfg.get("qos", 0) or 0)
        self.query = build_query(ecfg["registers"], ecfg["include_handshake"])
        self.columns = csv_columns(ecfg["registers"], include_price=ecfg["log_price"])
        self.path = ecfg["csv_path"]
        self.pricebook = PriceBook() if ecfg["log_price"] else None
        self._next_price_refresh = 0.0

        self._latest: Dict[int, float] = {}
        self._lock = threading.Lock()
        self._fresh = False
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._count = 0
        self._count_fail = 0

    def _refresh_prices(self, cfg: dict) -> None:
        """Ververs de prijzen (met dezelfde bron/fallback als het advies)."""
        if self.pricebook is None:
            return
        if self.pricebook.refresh(cfg, self.tz, self.ecfg["price_days_ahead"]):
            self._next_price_refresh = time.time() + self.ecfg["price_refresh_seconds"]
            _log(f"prijzen bijgewerkt ({self.pricebook.source})")
        else:
            # geen (volledige) prijzen: blijf het met het normale interval
            # opnieuw proberen, maar spam het log niet vol
            self._next_price_refresh = time.time() + self.ecfg["price_refresh_seconds"]
            _log("geen prijzen beschikbaar — kolom blijft leeg (energie wordt wel gelogd)")

    # --- paho callbacks ---
    def on_connect(self, client, userdata, flags, rc, properties=None):
        self._connected.set()
        client.subscribe(self.data_topic, qos=self.qos)
        _log(f"verbonden; subscribe op {self.data_topic}")

    def on_disconnect(self, *args):
        self._connected.clear()

    def on_message(self, client, userdata, msg):
        if msg.topic != self.data_topic:
            return
        try:
            payload = msg.payload.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return
        values = parse_values(payload)
        if not values:
            return
        with self._lock:
            self._latest.update(values)
            self._fresh = True

    # --- sampling ---
    def _collect(self, client) -> Optional[dict]:
        client.publish(self.cmd_topic, json.dumps(self.query), qos=self.qos)
        deadline = time.time() + self.ecfg["response_timeout_seconds"]
        while time.time() < deadline and not self._stop.is_set():
            with self._lock:
                if self._fresh:
                    break
            time.sleep(0.2)
        with self._lock:
            if not self._fresh:
                return None
            self._fresh = False
            snapshot = dict(self._latest)
        row = {"timestamp": datetime.now(self.tz).isoformat(timespec="seconds")}
        for reg, name, _ in FIELDS:
            if reg in snapshot:
                row[name] = snapshot[reg]
        if self.pricebook is not None:
            p = self.pricebook.price_at(datetime.now(self.tz))
            if p is not None:
                row[PRICE_FIELD] = round(p, 5)
        return row

    def stop(self, *_args) -> None:
        self._stop.set()

    def run(self) -> int:
        mqtt = mqtt_out._paho()
        if mqtt is None:
            _log("FOUT: paho-mqtt niet geïnstalleerd")
            return 1
        if not self.mqtt_cfg.get("enabled"):
            _log("FOUT: mqtt.enabled = false")
            return 1
        if not self.data_topic:
            _log("FOUT: geen data-topic (mqtt.control_topic/topic_base ontbreekt)")
            return 1

        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        client = None
        while not self._stop.is_set():
            try:
                client = _make_connected_client(mqtt, self.mqtt_cfg)
                client.on_connect = self.on_connect
                client.on_message = self.on_message
                client.on_disconnect = self.on_disconnect
                client.loop_start()
                break
            except OSError as exc:
                _log(f"verbinden mislukt ({exc}); opnieuw over 30 s")
                if self._stop.wait(30):
                    return 0
        if client is None:
            return 0

        _log(
            f"energie-logger gestart — elke {self.ecfg['interval_seconds']:g}s "
            f"-> {self.path}"
        )
        self._refresh_prices(self.cfg)
        try:
            while not self._stop.is_set():
                if self.pricebook is not None and time.time() >= self._next_price_refresh:
                    self._refresh_prices(self.cfg)
                t0 = time.time()
                row = self._collect(client)
                if row is not None:
                    if not self.dry_run:
                        append_row(self.path, self.columns, row)
                    self._count += 1
                    if self._count == 1 or self._count % 15 == 0:
                        _log(f"sample #{self._count} weggeschreven")
                else:
                    self._count_fail = getattr(self, "_count_fail", 0) + 1
                    if self._count_fail in (1, 5) or self._count_fail % 30 == 0:
                        _log(
                            "geen antwoord van de gateway binnen "
                            f"{self.ecfg['response_timeout_seconds']:g}s — "
                            "probeer `--once --handshake` om te zien of de "
                            "handshake-registers nodig zijn"
                        )
                sleep = self.ecfg["interval_seconds"] - (time.time() - t0)
                if sleep > 0:
                    self._stop.wait(sleep)
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        _log("energie-logger gestopt")
        return 0


def collect_once(mqtt_cfg: dict, ecfg: dict, tz: ZoneInfo,
                 include_handshake: bool = False) -> Optional[dict]:
    """Lees één sample en geef {kolom: waarde} terug (of None bij timeout)."""
    mqtt = mqtt_out._paho()
    if mqtt is None:
        raise RuntimeError("paho-mqtt niet geïnstalleerd")
    dt = data_topic(mqtt_cfg)
    ct = command_topic(mqtt_cfg)
    query = build_query(ecfg["registers"], include_handshake)
    latest: Dict[int, float] = {}
    got = threading.Event()

    client = _make_connected_client(mqtt, mqtt_cfg)

    def on_connect(c, u, flags, rc, properties=None):
        c.subscribe(dt, qos=int(mqtt_cfg.get("qos", 0) or 0))
        c.publish(ct, json.dumps(query), qos=int(mqtt_cfg.get("qos", 0) or 0))

    def on_message(c, u, msg):
        if msg.topic != dt:
            return
        values = parse_values(msg.payload.decode("utf-8", "replace"))
        if values:
            latest.update(values)
            got.set()

    client.on_connect = on_connect
    client.on_message = on_message
    client.loop_start()
    got.wait(ecfg["response_timeout_seconds"])
    client.loop_stop()
    client.disconnect()

    if not latest:
        return None
    row = {"timestamp": datetime.now(tz).isoformat(timespec="seconds")}
    for reg, name, _ in FIELDS:
        if reg in latest:
            row[name] = latest[reg]
    return row


def dump_messages(mqtt_cfg: dict, seconds: float, ecfg: dict) -> int:
    """Toon `seconds` lang álle MQTT-berichten onder de gateway-node."""
    mqtt = mqtt_out._paho()
    if mqtt is None:
        raise RuntimeError("paho-mqtt niet geïnstalleerd")
    dt = data_topic(mqtt_cfg)
    ct = command_topic(mqtt_cfg)
    prefix = dt.rsplit("/", 1)[0] if "/" in dt else dt
    wildcard = prefix + "/#"
    query = build_query(ecfg["registers"], ecfg["include_handshake"])

    client = _make_connected_client(mqtt, mqtt_cfg)

    def on_connect(c, u, flags, rc, properties=None):
        c.subscribe(wildcard, qos=int(mqtt_cfg.get("qos", 0) or 0))
        c.publish(ct, json.dumps(query), qos=int(mqtt_cfg.get("qos", 0) or 0))
        _log(f"dump gestart op {wildcard} ({seconds:g}s)")

    def on_message(c, u, msg):
        try:
            payload = msg.payload.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            payload = repr(msg.payload)
        print(f"{msg.topic}  {payload}", flush=True)

    client.on_connect = on_connect
    client.on_message = on_message
    client.loop_start()
    time.sleep(seconds)
    client.loop_stop()
    client.disconnect()
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="REMKO WKF 70 energie-logger (MQTT -> CSV tijdreeks)"
    )
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.json"))
    ap.add_argument("--csv", default=None, help="CSV-pad (default uit config)")
    ap.add_argument("--interval", type=float, default=None, help="seconden tussen samples")
    ap.add_argument(
        "--once", action="store_true", help="één sample lezen en tonen, dan stoppen"
    )
    ap.add_argument(
        "--dump", type=float, nargs="?", const=30.0, default=None,
        help="N seconden alle MQTT-berichten tonen (default 30)",
    )
    ap.add_argument("--report", action="store_true", help="samenvatting van het CSV-bestand")
    ap.add_argument("--handshake", action="store_true",
                    help="de 3 handshake-registers meesturen in de query")
    ap.add_argument("--no-price", action="store_true",
                    help="geen stroomprijs-kolom loggen (alleen energietellers)")
    ap.add_argument("--dry-run", action="store_true", help="niet naar CSV schrijven")
    ap.add_argument("--json", action="store_true", help="JSON-uitvoer bij --once/--report")
    args = ap.parse_args(argv)

    try:
        cfg = app.load_config(args.config)
    except OSError as exc:
        print(f"FOUT: config niet leesbaar: {exc}", file=sys.stderr)
        return 1

    mqtt_cfg = cfg.get("mqtt") or {}
    ecfg = energy_cfg(cfg)
    if args.csv:
        ecfg["csv_path"] = os.path.expanduser(args.csv)
    if args.interval is not None:
        ecfg["interval_seconds"] = args.interval
    if args.handshake:
        ecfg["include_handshake"] = True
    if args.no_price:
        ecfg["log_price"] = False

    tz = ZoneInfo((cfg.get("location") or {}).get("timezone", "Europe/Amsterdam"))

    if args.report:
        path = ecfg["csv_path"]
        if not os.path.exists(path):
            print(f"FOUT: geen CSV-bestand op {path}", file=sys.stderr)
            return 1
        rows = read_rows(path)
        episodes = summarize_episodes(rows)
        if args.json:
            print(json.dumps(episodes, ensure_ascii=False, indent=2, default=str))
        else:
            print("\n".join(render_report(episodes, rows)))
        return 0

    if not mqtt_cfg.get("enabled"):
        print("FOUT: mqtt.enabled = false in config.json", file=sys.stderr)
        return 1

    try:
        if args.dump is not None:
            return dump_messages(mqtt_cfg, args.dump, ecfg)
        if args.once:
            row = collect_once(mqtt_cfg, ecfg, tz, ecfg["include_handshake"])
            if row is None:
                print(
                    "geen antwoord binnen de timeout — controleer mqtt.host/poort, "
                    "het data-topic en probeer `--once --handshake`",
                    file=sys.stderr,
                )
                return 1
            if args.json:
                print(json.dumps(row, ensure_ascii=False, indent=2))
            else:
                for key, val in row.items():
                    print(f"{key:24s} {val}")
            return 0

        if not ecfg["enabled"]:
            print("energie-logger staat uit (mqtt.energy_log.enabled = false)")
            return 0
        return EnergyLogger(cfg, mqtt_cfg, ecfg, tz, dry_run=args.dry_run).run()
    except Exception as exc:  # noqa: BLE001 — nette foutmelding vanuit systemd
        print(f"FOUT: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
