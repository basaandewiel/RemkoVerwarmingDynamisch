#!/usr/bin/env python3
"""SWW-boost: publiceer het start-commando op het moment dat het goedkoopste
3-uursblok voor sanitair warm water (SWW) begint.

Twee manieren:
  A) one-shot via cron (elke 5-10 min): werkt prima, maar het commando gaat
     hooguit interval-minuten ná de blokstart uit.
     */5 * * * * cd ~/github/remkoverwarming && /usr/bin/python3 dhw_boost.py >> boost.log 2>&1

  B) --watch (aanbevolen): het script blijft draaien, slaapt tot vlak voor
     de blokstart en verstuurt dan vrijwel exact op tijd. Bij elke wake
     herberekent het het advies, want het beste blok kan verschuiven zodra
     nieuwe day-ahead-prijzen binnenkomen. Te starten via systemd
     (bijlage in README) of nohup.

Werking (beide modi):
  1. berekent hetzelfde advies als main.py (zelfde config),
  2. als 'nu' binnen de eerste `trigger_minutes` van het beste SWW-blok valt,
     wordt het start-commando gepubliceerd op mqtt.control_topic (default
     <topic_base>/set). De waarde van register 1082 is afgeleid uit
     heatpump.dhw.temperature uit config.json: temperatuur &times; 10 als
     hexadecimaal getal (53 &deg;C &rarr; 530 decimal &rarr; "0212").
  3. een statusfile in ~/.cache/remko-wkf70 onthoudt per blok-start of er al
     verstuurd is, zodat er geen dubbele berichten tijdens hetzelfde blok
     uitgaan.

Het *stop*-commando wordt bewust niet verstuurd: de warmtepomp stopt zelf
wanneer de boiler op temperatuur is (setpoint 53 °C).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import List
from zoneinfo import ZoneInfo

import main as app
import mqtt_out

DEFAULT_DHW_TEMP = 53.0


def build_boost_payload(dhw_temperature: float) -> dict:
    """1082-waarde = gewenste SWW-temperatuur ('C x 10) in hexadecimaal.

    Voorbeeld: 53 graden -> 530 decimal -> 0x212 -> "0212" (4 cijfers,
    zelfde formaat als het oorspronkelijke "0190" = 0x190 = 40 graden).
    """
    value_dec = int(round(dhw_temperature * 10.0))
    value_hex = format(value_dec, "04x")
    return {"values": {"1082": value_hex}}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def state_path() -> str:
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "remko-wkf70")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "dhw_boost_state.json")


def load_state() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    with open(state_path(), "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


class _Args:
    """Vervangt de CLI-overrides zodat build_advice default config gebruikt."""

    supply_temperature = None
    block_hours = None
    lat = None
    lon = None


def decide(cfg: dict, now: datetime) -> dict:
    """Bepaal wat er gedaan moet worden. Geeft een dict voor output/uitvoering."""
    mqtt_cfg = cfg.get("mqtt") or {}
    boost_cfg = mqtt_cfg.get("dhw_boost") or {}
    base = mqtt_cfg.get("topic_base", "remko/wkf70").rstrip("/")
    topic = (mqtt_cfg.get("control_topic") or f"{base}/set").strip()
    dhw_cfg = (cfg.get("heatpump") or {}).get("dhw") or {}
    dhw_temp = dhw_cfg.get("temperature", DEFAULT_DHW_TEMP)
    payload = build_boost_payload(float(dhw_temp))
    window_min = int(boost_cfg.get("trigger_minutes", 45))

    out: dict = {
        "now": now.isoformat(),
        "enabled": bool(mqtt_cfg.get("enabled")) and bool(boost_cfg.get("enabled", True)),
        "topic": topic,
        "payload": payload,
    }

    if not out["enabled"]:
        out["status"] = "disabled"
        return out

    dhw = app.build_advice(cfg, _Args(), now).get("dhw") or {}
    if not dhw.get("enabled") or not dhw.get("rows"):
        out["status"] = "no_dhw"
        return out

    best = dhw.get("best")
    if not best:
        out["status"] = "no_block"
        return out

    out["block"] = {
        "start": best["start"].isoformat(),
        "end": best["end"].isoformat(),
        "mean_cop": best["mean_cop"],
        "mean_corrected_eur_per_kwh_heat": best["mean_corrected"],
    }

    start, end = best["start"], best["end"]
    window_end = min(end, start + timedelta(minutes=window_min))
    if now < start:
        out["status"] = "wait"
        out["wait_minutes"] = round((start - now).total_seconds() / 60.0, 1)
        return out
    if now >= end:
        out["status"] = "passed"
        return out
    if now >= window_end:
        # het blok loopt al, maar het trigger-venster is voorbij
        out["status"] = "missed"
        return out

    # nu valt binnen het trigger-venster aan het begin van het blok
    already = load_state().get("last_sent_start") == start.isoformat()
    out["status"] = "already_sent" if already else "send"
    return out


def execute(out: dict, mqtt_cfg: dict, dry_run: bool, force: bool) -> None:
    """Voer de beslissing uit: publiceer evt. en schrijf de statusfile."""
    if out["status"] not in ("send", "already_sent"):
        return
    if out["status"] == "already_sent" and not force:
        return
    if dry_run:
        return
    ok = mqtt_out.publish_command(mqtt_cfg, out["topic"], out["payload"])
    out["mqtt_published"] = ok
    if ok:
        save_state(
            {
                "last_sent_start": out["block"]["start"],
                "sent_at": out["now"],
            }
        )


WATCH_START_MARGIN_SECONDS = 60.0  # wek ~1 min vóór de blokstart op
RETRY_SECONDS = 30.0               # tussen pogingen als de broker niet bereikbaar is


def _log(*parts) -> None:
    ts = datetime.now().strftime("%d-%m %H:%M:%S")
    print(f"[{ts}] " + " ".join(str(p) for p in parts), flush=True)


def compute_sleep(out: dict, now: datetime, interval: float) -> float:
    """Aantal seconden slapen tot de volgende nuttige wake-up."""
    if out.get("status") == "wait" and out.get("block"):
        start = datetime.fromisoformat(out["block"]["start"])
        delta = (start - now).total_seconds() - WATCH_START_MARGIN_SECONDS
        if delta > 0:
            return max(10.0, min(delta, interval))
        return 10.0  # blokstart is héél dichtbij: vaak wakker worden
    if out.get("status") == "already_sent":
        return min(interval, 30.0)
    return float(interval)


def send_with_retry(out: dict, mqtt_cfg: dict, tz, dry_run: bool) -> bool:
    """Publiceer, met retry zolang we nog binnen het trigger-venster zitten."""
    if dry_run:
        return True
    window_end = (
        datetime.fromisoformat(out["block"]["end"]) if out.get("block") else None
    )
    while True:
        ok = mqtt_out.publish_command(mqtt_cfg, out["topic"], out["payload"])
        if ok:
            save_state(
                {
                    "last_sent_start": out["block"]["start"],
                    "sent_at": out["now"],
                }
            )
            _log("VERSTUURD →", out["topic"], json.dumps(out["payload"], ensure_ascii=False))
            return True
        _log("FOUT: publish mislukt, probeer opnieuw over", f"{RETRY_SECONDS:g}s")
        now = datetime.now(tz)
        if window_end and now >= window_end:
            _log("FOUT: kon niet versturen binnen het trigger-venster")
            return False
        time.sleep(RETRY_SECONDS)


def watch(cfg: dict, tz, interval: float, dry_run: bool) -> int:
    """Blijf draaien: slaap tot (vlak voor) de blokstart, verstuur dan precies."""
    _log("SWW-boost watchdog gestart — herberekent elke max", f"{interval:.0f}s")
    while True:
        try:
            now = datetime.now(tz)
            out = decide(cfg, now)
            status = out["status"]

            if status == "send":
                _log("blokstart bereikt — verstuur het boost-commando")
                if not send_with_retry(out, cfg.get("mqtt") or {}, tz, dry_run):
                    return 1
                continue  # statusfile voorkomt dubbele berichten; op naar het volgende blok

            if status in ("no_dhw", "disabled"):
                _log("status:", status, "— niets te doen, stop")
                return 0

            if status == "already_sent":
                _log("status: al verstuurd voor dit blok")
            else:
                extra = (
                    f"blokstart over {out['wait_minutes']:g} min"
                    if status == "wait"
                    else f"status: {status}"
                )
                _log(extra)
            time.sleep(compute_sleep(out, now, interval))
        except KeyboardInterrupt:
            _log("gestopt")
            return 130
        except Exception as exc:  # noqa: BLE001 — de daemon moet blijven draaien
            _log("FOUT (ga verder):", exc)
            time.sleep(60)


def render_human(out: dict) -> List[str]:
    lines: List[str] = []
    lines.append("SWW-boost — dynamisch stroomadvies")
    lines.append("=" * 64)
    lines.append(f"Nu      : {app.fmt_dt(datetime.fromisoformat(out['now']))}")
    if "block" in out:
        b = out["block"]
        lines.append(
            f"Beste SWW-blok : {app.fmt_dt(datetime.fromisoformat(b['start']))} – "
            f"{app.fmt_dt(datetime.fromisoformat(b['end']))} "
            f"(COP {app.nl(b['mean_cop'], 2)}, "
            f"{app.nl(b['mean_corrected_eur_per_kwh_heat'], 4)} €/kWh warmte)"
        )

    status = out["status"]
    if status == "send":
        lines.append(
            f"Status  : VERSTUURD → {out['topic']} {json.dumps(out['payload'], ensure_ascii=False)}"
        )
    elif status == "already_sent":
        lines.append("Status  : al verstuurd voor dit blok (geen dubbele berichten)")
    elif status == "wait":
        lines.append(
            f"Status  : wachten (blok begint over {out['wait_minutes']:g} min)"
        )
    elif status == "missed":
        lines.append("Status  : trigger-venster inmiddels voorbij, niets verstuurd")
    elif status == "passed":
        lines.append("Status  : blok is voorbij, niets verstuurd")
    elif status == "no_block":
        lines.append("Status  : geen SWW-blok in de (toekomstige) data")
    elif status == "no_dhw":
        lines.append("Status  : SWW staat uit (heatpump.dhw.enabled = false)")
    elif status == "disabled":
        lines.append("Status  : boost staat uit (mqtt.dhw_boost.enabled = false of mqtt uit)")
    return lines


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SWW-boost: MQTT-commando bij start goedkoopste blok")
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.json"))
    ap.add_argument("--now", default=None, help="ISO-tijdstip voor 'nu' (testen)")
    ap.add_argument("--dry-run", action="store_true", help="niets publiceren, alleen tonen")
    ap.add_argument("--force", action="store_true", help="ondanks statusfile opnieuw versturen")
    ap.add_argument("--json", action="store_true", help="JSON-output op stdout")
    ap.add_argument(
        "--watch",
        action="store_true",
        help="blijf draaien en verstuur vrijwel exact bij de blokstart",
    )
    ap.add_argument(
        "--watch-interval",
        type=float,
        default=300.0,
        help="max seconden tussen herberekeningen (default 300)",
    )
    args = ap.parse_args(argv)

    try:
        cfg = app.load_config(args.config)
        tz = ZoneInfo(cfg["location"]["timezone"])
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz)

        if args.watch and not args.now:
            return watch(cfg, tz, args.watch_interval, args.dry_run)

        out = decide(cfg, now)
        execute(out, cfg.get("mqtt") or {}, dry_run=args.dry_run, force=args.force)

        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print("\n".join(render_human(out)))
        return 0
    except Exception as exc:  # noqa: BLE001 — nette foutmelding vanuit cron
        print(f"FOUT: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())