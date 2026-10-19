#!/usr/bin/env python3
"""Remko-registermonitor: volg de registers die de warmtevraag bepalen.

Doel: **afleiden hoe de thermostaat (bv. Watts) de warmtevraag van de Remko
stuurt.** De vloerverwarming vraagt normaal via een mengklep/thermostaat warmte;
de Remko vertaalt dat naar register **5085** (`heat_water_temp_req`), de
bedrijfsmodus (5001) en het gedrag van de mengklep/pomp van kring HC1. Door die
registers met de tijd te loggen zie je wát er verandert op het moment dat de
thermostaat om warmte vraagt — en welke registers dus de "vraag" dragen.

Dit programma is **read-only**: het stuurt alleen een `FORCE_RESPONSE`-query
(zoals `energy_log.py`) en schrijft nooit een register. Het is bedoeld om naast
de bestaande services te draaien terwijl je aan de thermostaat draait.

Twee uitvoervormen:
  * een **live wijzigingenlog**: bij elke poll worden alleen de registers
    getoond die van waarde veranderden (`oud -> nieuw`), plus periodiek een
    korte statusregel. Zo zie je direct de causaliteit;
  * een **CSV-tijdreeks** (`~/.cache/remko-wkf70/heating_probe.csv`) met álle
    gekozen registers per sample, om later met `--report` de wijzigingen per
    register terug te lezen.

Registertypes (overgenomen uit de HA-integratie Altrec/remko_mqtt-ha):
  * temp / temp_inp : 16-bits signed, gedeeld door 10 (°C)
  * sensor_el       : raw x 100 (W)
  * overige sensoren: raw unsigned int (mode, teller, kWh, %, …)

Gebruik:
  python3 register_monitor.py --list                # toon de registercatalogus
  python3 register_monitor.py --once                # één sample, nette tabel
  python3 register_monitor.py --once --json         # idem als JSON
  python3 register_monitor.py                       # live wijzigingenlog (Ctrl-C)
  python3 register_monitor.py --interval 10         # sneller pollen
  python3 register_monitor.py --duration 1800       # 30 min meten en stoppen
  python3 register_monitor.py --no-csv              # niet naar CSV schrijven
  python3 register_monitor.py --report              # wijzigingen uit de CSV
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import energy_log
import main as app
import mqtt_out

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_INTERVAL = 15.0          # seconden tussen samples
DEFAULT_RESPONSE_TIMEOUT = 8.0   # wachten op het eerste antwoord van de gateway
DEFAULT_SETTLE = 1.5             # daarna nog even doorluisteren voor de rest
DEFAULT_HEARTBEAT = 300.0        # seconden tussen statusregels zonder wijziging
EPS = 1e-9

# --------------------------------------------------------------------------- #
# Registercatalogus: (register, naam, soort, eenheid)
#
# `soort` bepaalt de decodering én de weergave. Alleen de registers hieronder
# worden herkend; de gateway stuurt er veel meer, die negeren we.
# --------------------------------------------------------------------------- #

CATALOG: List[Tuple[int, str, str, str]] = [
    # --- modus / vraag ---
    (5001, "opmode", "opmode", ""),
    (1951, "main_mode", "main_mode", ""),
    (1972, "heating_circ_mode", "switch", ""),
    (1079, "dhw_opmode", "dhw_opmode", ""),
    (1946, "temp_adjust", "temp", "°C"),
    (1967, "system_separation", "mode", ""),
    (1271, "hc1_dewpoint_control", "mode", ""),
    (1285, "hc1_room_device", "mode", ""),
    # --- aanvoer / vraag ---
    (5085, "heat_water_temp_req", "temp", "°C"),
    (5190, "heating_actual_temp", "temp", "°C"),
    (5132, "hp_flow_temp", "temp", "°C"),
    (5581, "hp_return_temp", "temp", "°C"),
    (5124, "hc1_flow_temp", "temp", "°C"),
    (5123, "hc1_return_temp", "temp", "°C"),
    (5055, "mixed_temp", "temp", "°C"),
    (5036, "hc1_actual_temp", "temp", "°C"),
    (5035, "hc1_target_temp", "temp", "°C"),
    # --- ruimte / thermostaat (HC1) ---
    (5048, "hc1_room_temp", "temp", "°C"),
    (5077, "hc1_room_target_temp", "temp", "°C"),
    (5067, "hc1_room_humidity", "num", "%rH"),
    (5148, "hc1_room_climate", "mode", ""),
    (5068, "hc1_op_mode", "mode", ""),
    (5711, "hc1_hvac_mode", "mode", ""),
    (5156, "hc1_mixer_status", "mode", ""),
    (5157, "hc1_mixer_position", "num", "%"),
    (5799, "hc1_pump_demand", "bool", ""),
    (5577, "hc1_pump_speed_rel", "num", "%"),
    (5044, "hc1_pump_speed_abs", "num", "rpm"),
    (5059, "hc1_flow_rate", "num", "l/min"),
    (5582, "heating_flow_rate", "num", "l/min"),
    (5071, "hc1_power", "num", "kW"),
    (5718, "hc1_setpoint_offset", "temp", "°C"),
    # --- warmtegenerator ---
    (5051, "heat_gen_status", "bool", ""),
    (5049, "hp_state", "mode", ""),
    (5625, "compressor_status", "bool", ""),
    (5205, "compressor_frequency", "num", "Hz"),
    (5822, "compressor_starts", "counter", ""),
    (5824, "compressor_hours", "counter", "h"),
    (5320, "el_consumption", "power_el", "W"),
    (5321, "th_consumption", "power_el", "W"),
    (5162, "switch_valve", "bool", ""),
    (5151, "circulation_pump", "bool", ""),
    # --- buiten / buffer / bron ---
    (5032, "out_temp", "temp", "°C"),
    (5056, "damped_out_temp", "temp", "°C"),
    (5131, "buff_temp", "temp", "°C"),
    (5034, "actual_temp", "temp", "°C"),
    (5139, "liquid_temp", "temp", "°C"),
    (5145, "air_temp_mod", "temp", "°C"),
    (5146, "discharge_temp", "temp", "°C"),
    # --- SWW (context: om verwarming van SWW-laden te onderscheiden) ---
    (5039, "water_temp", "temp", "°C"),
    (1082, "water_temp_req", "temp", "°C"),
    # --- energie ---
    (5105, "energy_electric", "energy", "kWh"),
    (5374, "energy_heating", "energy", "kWh"),
    (5376, "energy_DHW_heating", "energy", "kWh"),
    (5600, "energy_environmental", "energy", "kWh"),
]

BY_ID: Dict[int, Tuple[str, str, str]] = {
    reg: (name, kind, unit) for reg, name, kind, unit in CATALOG
}
ID_BY_NAME: Dict[str, int] = {name: reg for reg, name, _, _ in CATALOG}

# Standaardset: de registers die samen de warmtevraag-keten vormen. Uit te
# breiden/te beperken via `mqtt.register_monitor.registers` of `--registers`.
DEFAULT_REGISTERS: List[int] = [
    5001, 1951, 1972, 1946,                                  # modus/vraag
    5085, 5190, 5132, 5581, 5124, 5123, 5055, 5036, 5035,    # aanvoer/retour
    5048, 5077, 5068, 5148, 5711, 5156, 5157, 5799, 5577,     # HC1/thermostaat
    5059, 5582, 5071, 5718,                                   # HC1 debiet/vermogen
    5051, 5049, 5625, 5205, 5822, 5320, 5321,                 # warmtegenerator
    5032, 5056, 5131, 5034,                                   # buiten/buffer/vloer
    5039, 1082,                                               # SWW-context
    5105, 5374, 5376,                                         # energie
]

# Labels uit de HA-integratie (register 5001 = opmodus, 1951 = ruimtemodus,
# 1079 = SWW-modus). De overige mode-registers tonen we als ruw getal: daarvan
# is de enum niet gedocumenteerd.
OPMODE_LABELS = {
    0: "Onbekend/uit", 1: "Gedwongen uit", 2: "Aftauen", 3: "Aftaupuffer",
    4: "SWW laden", 5: "Bufferenergie", 6: "Verwarmen", 7: "Koelen",
    8: "Zwembad", 9: "Nawerking", 10: "Standby", 11: "Estrichtrocknung",
    12: "Vorstbeveiliging", 13: "Testbedrijf", 14: "Blokkeersignaal",
    15: "Hygiënefunctie", 16: "Stille modus",
}
MAIN_MODE_LABELS = {0: "Onbekend", 1: "Auto", 2: "Verwarmen", 3: "Standby", 4: "Koelen"}
DHW_OPMODE_LABELS = {0: "Auto comfort", 1: "Auto eco", 2: "Alleen solar/PV", 3: "Uit"}

# Registers die in de periodieke statusregel komen (voor zover gekozen).
KEY_REGISTERS: List[int] = [
    5032, 5085, 5190, 5124, 5123, 5048, 5077, 5001, 5068, 5577, 5157,
]


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testbaar)
# --------------------------------------------------------------------------- #

def decode_raw(kind: str, value_hex) -> Optional[float]:
    """Decodeer één hex-waarde volgens het registertype (None bij onzin)."""
    try:
        raw = int(str(value_hex), 16)
    except (ValueError, TypeError):
        return None
    if kind in ("temp", "temp_k"):
        if raw >= 0x8000:
            raw -= 0x10000
        return raw / 10.0
    if kind == "power_el":
        return raw * 100.0
    return raw


def parse_values(payload: str) -> Dict[int, float]:
    """Zet een HOST2CLIENT-payload om naar {register: waarde}.

    Alleen registers uit de catalogus; onbekende registers, niet-JSON en
    ondecodeerbare waarden worden stil overgeslagen.
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
        if reg not in BY_ID:
            continue
        value = decode_raw(BY_ID[reg][1], val)
        if value is not None:
            out[reg] = value
    return out


def format_value(reg: int, value: Optional[float]) -> str:
    """Mensvriendelijke weergave van één registerwaarde."""
    if value is None:
        return "?"
    _, kind, unit = BY_ID.get(reg, ("", "", ""))
    if kind in ("temp", "temp_k"):
        return f"{value:.1f} {unit}".strip()
    if kind == "power_el":
        return f"{value:.0f} W"
    if kind == "opmode":
        return OPMODE_LABELS.get(int(value), f"opmode{int(value)}")
    if kind == "main_mode":
        return MAIN_MODE_LABELS.get(int(value), f"mode{int(value)}")
    if kind == "dhw_opmode":
        return DHW_OPMODE_LABELS.get(int(value), f"dhwopmode{int(value)}")
    if kind in ("bool", "switch"):
        return "aan" if int(value) else "uit"
    # num / freq / counter / energy / mode
    text = str(int(value))
    return f"{text} {unit}".strip() if unit else text


def format_change(reg: int, old: Optional[float], new: Optional[float]) -> str:
    return f"{format_value(reg, old)} -> {format_value(reg, new)}"


def csv_columns(registers: List[int]) -> List[str]:
    """Vaste CSV-kolommen: timestamp + de register-namen (in catalogusorde)."""
    order = [reg for reg, _, _, _ in CATALOG if reg in set(registers)]
    return ["timestamp"] + [BY_ID[reg][0] for reg in order]


def selected_registers(requested) -> List[int]:
    """Filter een gevraagde registerlijst op de catalogus, in catalogusorde.

    Zonder (geldige) opgave valt de selectie terug op `DEFAULT_REGISTERS`.
    """
    if not requested:
        return [reg for reg, _, _, _ in CATALOG if reg in set(DEFAULT_REGISTERS)]
    wanted = set()
    for item in requested:
        try:
            wanted.add(int(item))
        except (TypeError, ValueError):
            continue
    return [reg for reg, _, _, _ in CATALOG if reg in wanted]


def _f(value, default: float = float("nan")) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def snapshot_lines(values: Dict[int, float], registers: List[int],
                   header: str = "") -> List[str]:
    """Nette tabel van de laatste waarden van alle gekozen registers."""
    regs = [r for r in registers if r in values]
    if not regs:
        return [header] if header else []
    width = max(len(f"{r}  {BY_ID[r][0]}") for r in regs)
    lines = [header] if header else []
    for reg in regs:
        label = f"{reg:>5}  {BY_ID[reg][0]}"
        lines.append(f"  {label:<{width}}  {format_value(reg, values[reg])}")
    return lines


def heartbeat_line(values: Dict[int, float]) -> str:
    """Compacte statusregel met de kern-temperaturen/modi."""
    parts: List[str] = []
    for reg in KEY_REGISTERS:
        if reg in values:
            parts.append(f"{BY_ID[reg][0]}={format_value(reg, values[reg])}")
    return " | ".join(parts)


def detect_transitions(rows: List[dict], registers: List[int]) -> List[dict]:
    """Loop de CSV-rijen langs en geef elke waardewijziging per register terug."""
    events: List[dict] = []
    prev: Dict[int, float] = {}
    for row in rows:
        ts = row.get("timestamp", "")
        for reg in registers:
            value = _f(row.get(BY_ID[reg][0]))
            if value != value:  # NaN / leeg
                continue
            if reg in prev and abs(value - prev[reg]) > EPS:
                events.append(
                    {"timestamp": ts, "register": reg, "naam": BY_ID[reg][0],
                     "oud": prev[reg], "nieuw": value}
                )
            prev[reg] = value
    return events


def render_transitions(rows: List[dict], registers: List[int], path: str) -> List[str]:
    """Tekstrapport: alle wijzigingen uit de CSV, gegroepeerd per register."""
    lines = [f"Remko-registermonitor — wijzigingen uit {path}"]
    lines.append("=" * 72)
    if not rows:
        lines.append("  (geen rijen in de CSV)")
        return lines
    span = f"{rows[0].get('timestamp', '?')}  ..  {rows[-1].get('timestamp', '?')}"
    lines.append(f"  {len(rows)} samples over {span}")

    events = detect_transitions(rows, registers)
    if not events:
        lines.append("  Geen enkele wijziging gemeten — vraag lijkt constant.")
        return lines

    widths = {r: len(f"{r}  {BY_ID[r][0]}") for r in registers}
    w = max(widths.values()) if widths else 20
    lines.append("")
    lines.append("  Wijzigingen (chronologisch):")
    for e in events:
        label = f"{e['register']:>5}  {e['naam']}"
        lines.append(
            f"    {e['timestamp']}  {label:<{w}}  "
            f"{format_change(e['register'], e['oud'], e['nieuw'])}"
        )

    counts: Dict[int, int] = {}
    for e in events:
        counts[e["register"]] = counts.get(e["register"], 0) + 1
    lines.append("")
    lines.append("  Aantal wijzigingen per register:")
    for reg in registers:
        if reg in counts:
            label = f"{reg:>5}  {BY_ID[reg][0]}"
            lines.append(f"    {label:<{w}}  {counts[reg]}")
    return lines


def monitor_cfg(cfg: dict) -> dict:
    """Lees de `mqtt.register_monitor`-sectie met verstandige defaults."""
    mqtt_cfg = cfg.get("mqtt") or {}
    rm = mqtt_cfg.get("register_monitor") or {}
    registers = selected_registers(rm.get("registers") or [])
    return {
        "registers": registers,
        "interval_seconds": float(rm.get("interval_seconds", DEFAULT_INTERVAL)),
        "response_timeout_seconds": float(
            rm.get("response_timeout_seconds", DEFAULT_RESPONSE_TIMEOUT)
        ),
        "settle_seconds": float(rm.get("settle_seconds", DEFAULT_SETTLE)),
        "heartbeat_seconds": float(rm.get("heartbeat_seconds", DEFAULT_HEARTBEAT)),
        "csv_path": os.path.expanduser(
            rm.get("csv", rm.get("csv_path", default_csv_path()))
        ),
        "log_csv": bool(rm.get("log_csv", True)),
        "include_handshake": bool(rm.get("include_handshake", False)),
    }


def default_csv_path() -> str:
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "remko-wkf70")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "heating_probe.csv")


def _log(*parts) -> None:
    stamp = datetime.now().strftime("%d-%m %H:%M:%S")
    print(f"[{stamp}]", *parts, flush=True)


# --------------------------------------------------------------------------- #
# MQTT
# --------------------------------------------------------------------------- #

class RegisterMonitor:
    """Pollt de gateway en toont/logt de wijzigingen per register.

    Read-only: er wordt alleen een `FORCE_RESPONSE`-query gepubliceerd.
    """

    def __init__(self, mqtt_cfg: dict, mcfg: dict, tz: ZoneInfo,
                 print_all: bool = False):
        self.mqtt_cfg = mqtt_cfg
        self.mcfg = mcfg
        self.tz = tz
        self.print_all = print_all
        self.registers = list(mcfg["registers"])
        self.cmd_topic = energy_log.command_topic(mqtt_cfg)
        self.data_topic = energy_log.data_topic(mqtt_cfg)
        self.qos = int(mqtt_cfg.get("qos", 0) or 0)
        self.query = energy_log.build_query(
            self.registers, mcfg["include_handshake"]
        )
        self.columns = csv_columns(self.registers)

        self._latest: Dict[int, float] = {}
        self._prev: Dict[int, float] = {}
        self._lock = threading.Lock()
        self._fresh = False
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._last_heartbeat = 0.0
        self.samples = 0
        self.failures = 0
        self._first = True

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
    def sample(self, client) -> Tuple[Dict[int, float], bool]:
        """Vraag één ronde op; geef (waarden, vers?) terug."""
        with self._lock:
            self._fresh = False
        client.publish(self.cmd_topic, json.dumps(self.query), qos=self.qos)
        deadline = time.time() + self.mcfg["response_timeout_seconds"]
        while time.time() < deadline and not self._stop.is_set():
            with self._lock:
                if self._fresh:
                    break
            time.sleep(0.1)
        with self._lock:
            fresh = self._fresh
        if fresh:
            # even doorluisteren zodat de gateway alle registers kan nasturen
            self._stop.wait(self.mcfg["settle_seconds"])
        with self._lock:
            self._fresh = False
            return dict(self._latest), fresh

    def stop(self, *_args) -> None:
        self._stop.set()

    def _emit(self, snapshot: Dict[int, float]) -> None:
        """Schrijf de CSV-regel en toon wijzigingen / statusregel."""
        row = {"timestamp": datetime.now(self.tz).isoformat(timespec="seconds")}
        for reg in self.registers:
            if reg in snapshot:
                row[BY_ID[reg][0]] = snapshot[reg]
        if self.mcfg["log_csv"]:
            energy_log.append_row(self.mcfg["csv_path"], self.columns, row)

        if self.print_all or self._first:
            if self._first:
                _log(
                    f"monitor gestart — {len(self.registers)} registers, "
                    f"{self.mcfg['interval_seconds']:g}s interval, "
                    + (f"CSV {self.mcfg['csv_path']}" if self.mcfg["log_csv"]
                       else "geen CSV")
                )
            for line in snapshot_lines(snapshot, self.registers):
                print(line, flush=True)
            self._first = False
            self._prev = dict(snapshot)
            self._last_heartbeat = time.time()
            return

        changes = [
            reg for reg in self.registers
            if reg in snapshot and reg in self._prev
            and abs(snapshot[reg] - self._prev[reg]) > EPS
        ]
        if changes:
            widths = {r: len(f"{r}  {BY_ID[r][0]}") for r in self.registers}
            w = max(widths.values()) if widths else 20
            for reg in changes:
                label = f"{reg:>5}  {BY_ID[reg][0]}"
                print(
                    f"[{datetime.now().strftime('%d-%m %H:%M:%S')}]  "
                    f"{label:<{w}}  "
                    f"{format_change(reg, self._prev[reg], snapshot[reg])}",
                    flush=True,
                )
            self._last_heartbeat = time.time()
        elif (time.time() - self._last_heartbeat) >= self.mcfg["heartbeat_seconds"]:
            _log(f"status: {heartbeat_line(snapshot)}")
            self._last_heartbeat = time.time()

        self._prev = dict(snapshot)
        self._first = False

    def run(self) -> int:
        mqtt = mqtt_out._paho()
        if mqtt is None:
            _log("FOUT: paho-mqtt niet geïnstalleerd")
            return 1
        if not self.mqtt_cfg.get("enabled"):
            _log("FOUT: mqtt.enabled = false")
            return 1

        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        client = None
        while not self._stop.is_set():
            try:
                client = energy_log._make_connected_client(mqtt, self.mqtt_cfg)
                client.on_connect = self.on_connect
                client.on_message = self.on_message
                client.on_disconnect = self.on_disconnect
                client.loop_start()
                break
            except OSError as exc:
                _log(f"verbinden mislukt ({exc}); opnieuw over 15 s")
                if self._stop.wait(15):
                    return 0
        if client is None:
            return 0

        # Wacht tot de subscribe-actie in on_connect gelopen is, zodat de eerste
        # query niet vóór de subscribe wordt gepubliceerd.
        self._connected.wait(10)

        try:
            while not self._stop.is_set():
                t0 = time.time()
                snapshot, fresh = self.sample(client)
                if fresh and snapshot:
                    self.samples += 1
                    self._emit(snapshot)
                else:
                    self.failures += 1
                    if self.failures in (1, 5) or self.failures % 30 == 0:
                        _log(
                            "geen antwoord van de gateway — controleer "
                            "mqtt.host/poort, het data-topic en probeer "
                            "`--once --handshake`"
                        )
                sleep = self.mcfg["interval_seconds"] - (time.time() - t0)
                if sleep > 0:
                    self._stop.wait(sleep)
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        _log(f"monitor gestopt ({self.samples} samples, {self.failures} timeouts)")
        return 0


def collect_once(mqtt_cfg: dict, mcfg: dict) -> Optional[Dict[int, float]]:
    """Lees één sample en geef {register: waarde} terug (of None bij timeout)."""
    mqtt = mqtt_out._paho()
    if mqtt is None:
        raise RuntimeError("paho-mqtt niet geïnstalleerd")
    dt = energy_log.data_topic(mqtt_cfg)
    ct = energy_log.command_topic(mqtt_cfg)
    qos = int(mqtt_cfg.get("qos", 0) or 0)
    query = energy_log.build_query(mcfg["registers"], mcfg["include_handshake"])
    latest: Dict[int, float] = {}
    got = threading.Event()

    client = energy_log._make_connected_client(mqtt, mqtt_cfg)

    def on_connect(c, u, flags, rc, properties=None):
        c.subscribe(dt, qos=qos)
        c.publish(ct, json.dumps(query), qos=qos)

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
    got.wait(mcfg["response_timeout_seconds"])
    if latest:
        time.sleep(mcfg["settle_seconds"])  # rest van de registers binnenhalen
    client.loop_stop()
    client.disconnect()
    return latest or None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print_catalog() -> None:
    print("Registercatalogus (register  naam  soort  eenheid):")
    for reg, name, kind, unit in CATALOG:
        default = " *" if reg in DEFAULT_REGISTERS else "  "
        print(f"  {reg:>5}  {name:<24} {kind:<12} {unit:<6}{default}")
    print("\n  * = standaard gekozen door de monitor")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Remko-registermonitor: volg de registers van de warmtevraag"
    )
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.json"))
    ap.add_argument("--list", action="store_true",
                    help="toon de registercatalogus en stop")
    ap.add_argument("--once", action="store_true",
                    help="één sample lezen en tonen, dan stoppen")
    ap.add_argument("--report", action="store_true",
                    help="wijzigingen uit de CSV samenvatten")
    ap.add_argument("--registers", default=None,
                    help="komma-lijst van registers (bv. 5001,5085,1972)")
    ap.add_argument("--interval", type=float, default=None,
                    help="seconden tussen samples (default 15)")
    ap.add_argument("--duration", type=float, default=None,
                    help="stop na N seconden (default: tot Ctrl-C)")
    ap.add_argument("--csv", default=None, help="CSV-pad (default uit config)")
    ap.add_argument("--no-csv", action="store_true", help="niet naar CSV schrijven")
    ap.add_argument("--all", action="store_true",
                    help="toon elke sample de volledige tabel")
    ap.add_argument("--handshake", action="store_true",
                    help="de 3 handshake-registers meesturen in de query")
    ap.add_argument("--json", action="store_true",
                    help="JSON-uitvoer bij --once/--report")
    args = ap.parse_args(argv)

    if args.list:
        _print_catalog()
        return 0

    try:
        cfg = app.load_config(args.config)
    except OSError as exc:
        print(f"FOUT: config niet leesbaar: {exc}", file=sys.stderr)
        return 1

    mqtt_cfg = cfg.get("mqtt") or {}
    mcfg = monitor_cfg(cfg)
    if args.registers:
        mcfg["registers"] = selected_registers(
            [p for p in args.registers.replace(" ", "").split(",") if p]
        )
    if args.interval is not None:
        mcfg["interval_seconds"] = args.interval
    if args.csv:
        mcfg["csv_path"] = os.path.expanduser(args.csv)
    if args.no_csv:
        mcfg["log_csv"] = False
    if args.handshake:
        mcfg["include_handshake"] = True

    tz = ZoneInfo((cfg.get("location") or {}).get("timezone", "Europe/Amsterdam"))

    if args.report:
        path = mcfg["csv_path"]
        if not os.path.exists(path):
            print(f"FOUT: geen CSV-bestand op {path}", file=sys.stderr)
            return 1
        rows = energy_log.read_rows(path)
        registers = selected_registers(
            [ID_BY_NAME[c] for c in rows[0] if c in ID_BY_NAME]
        ) if rows else mcfg["registers"]
        if args.json:
            print(json.dumps(detect_transitions(rows, registers),
                             ensure_ascii=False, indent=2, default=str))
        else:
            print("\n".join(render_transitions(rows, registers, path)))
        return 0

    if not mqtt_cfg.get("enabled"):
        print("FOUT: mqtt.enabled = false in config.json", file=sys.stderr)
        return 1

    try:
        if args.once:
            values = collect_once(mqtt_cfg, mcfg)
            if values is None:
                print(
                    "geen antwoord binnen de timeout — controleer mqtt.host/poort, "
                    "het data-topic en probeer `--once --handshake`",
                    file=sys.stderr,
                )
                return 1
            if args.json:
                payload = {str(r): values[r] for r in mcfg["registers"] if r in values}
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for line in snapshot_lines(
                    values, mcfg["registers"],
                    f"Remko-registers  ({datetime.now(tz):%d-%m %H:%M:%S})",
                ):
                    print(line)
            return 0

        monitor = RegisterMonitor(mqtt_cfg, mcfg, tz, print_all=args.all)
        if args.duration:
            timer = threading.Timer(args.duration, monitor.stop)
            timer.daemon = True
            timer.start()
        return monitor.run()
    except Exception as exc:  # noqa: BLE001 — nette foutmelding vanaf de CLI
        print(f"FOUT: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
