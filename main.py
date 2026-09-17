#!/usr/bin/env python3
"""REMKO WKF 70 (NEO) compact — dynamisch stroomadvies.

Stroomlijn:
  1. COP(t_buiten) uit de specs van de WKF 70 NEO compact (EN 14511)
  2. uurvoorspelling buitentemperatuur via met.no
  3. dynamische kWh-prijzen via EnergyZero/EasyEnergy (publiek, geen key)
  4. gecorrigeerde prijs per slot = kWh-prijs / COP(buitentemp.)
  5. goedkoopste aaneengesloten blok van N uur (standaard 3)

Gebruik:  python3 main.py [--json] [--no-mqtt] [opties]
Zie README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo

import cop_model as cop_mod
import energyzero
import entsoe
import metno
import mqtt_out
from optimizer import build_corrected_rows, find_cheapest_blocks

DUTCH_DAYS = [
    "maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag",
]


def script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def nl(value: float, digits: int = 3) -> str:
    """Formatteer met komma als decimaal scheidingsteken."""
    return f"{value:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_dt(dt: datetime) -> str:
    day = DUTCH_DAYS[dt.weekday()]
    return f"{day} {dt:%d-%m} {dt:%H:%M} uur"


def fmt_slot(dt: datetime) -> str:
    return f"{DUTCH_DAYS[dt.weekday()][:3]} {dt:%d-%m %H:%M}"


def build_advice(cfg: dict, args, now: datetime) -> dict:
    """Haal data op en bereken het advies. Geeft een dict resultaat."""
    loc = cfg["location"]
    fc = cfg["forecast"]
    hp = cfg["heatpump"]
    opt = cfg["optimization"]
    tz = ZoneInfo(loc["timezone"])

    # 1) COP-model
    curves = {
        35: hp["cop_curve_w35"],
        45: hp["cop_curve_w45"],
        55: hp["cop_curve_w55"],
    }
    supply = args.supply_temperature or int(hp["supply_temperature"])
    if supply not in curves:
        raise ValueError(
            f"onbekende aanvoertemperatuur {supply} °C (kies 35, 45 of 55)"
        )
    try:
        model = cop_mod.CopModel(supply_temperature=supply, curve=curves[supply])
    except KeyError:
        model = cop_mod.CopModel(supply_temperature=supply)

    # 2) temperatuurvoorspelling (met.no)
    forecast = metno.fetch_hourly_forecast(
        lat=args.lat or loc["lat"],
        lon=args.lon or loc["lon"],
        tz=loc["timezone"],
        user_agent=fc["user_agent"],
        horizon_hours=int(fc["horizon_hours"]),
        cache_ttl_seconds=int(fc["cache_ttl_seconds"]),
    )
    temps_by_hour = metno.hourly_temperatures(forecast)

    # 3) stroomprijzen (keuze uit config: entsoe | energyzero)
    prices_cfg = cfg["prices"]
    source = prices_cfg.get("source", "entsoe")
    days_ahead = int(prices_cfg.get("days_ahead", 2))
    if source == "entsoe":
        ec = prices_cfg["entsoe"]
        pr = entsoe.fetch_prices(
            api_key=ec["api_key"],
            tz=loc["timezone"],
            days_ahead=days_ahead,
            in_domain=ec.get("in_domain", "10YNL----------L"),
            out_domain=ec.get("out_domain", "10YNL----------L"),
        )
    elif source == "energyzero":
        ez = prices_cfg["energyzero"]
        pr = energyzero.fetch_prices(
            tz=loc["timezone"],
            days_ahead=days_ahead,
            api_url=ez.get("api_url", energyzero.API_URL_DEFAULT),
            usage_type=int(ez.get("usage_type", 1)),
            incl_btw=bool(ez.get("incl_btw", True)),
        )
    else:
        raise ValueError(f"onbekende prijsbron: {source!r} (kies 'entsoe' of 'energyzero')")

    # optionele correcties op de kWh-prijs (bv. btw en/of vaste belasting)
    adj = prices_cfg.get("price_adjustments") or {}
    vat = float(adj.get("vat_pct", 0.0))
    tax = float(adj.get("fixed_tax_per_kwh", 0.0))
    if vat or tax:
        pr["slots"] = [
            (dt, price * (1.0 + vat / 100.0) + tax) for dt, price in pr["slots"]
        ]
        pr["source"] += f" (+btw {vat:g}%, vaste belasting {tax:g} €/kWh)"

    # 4) corrigeren per slot
    rows = build_corrected_rows(pr["slots"], temps_by_hour, model)
    if not rows:
        raise RuntimeError(
            "geen overlap tussen prijsdata en temperatuurvoorspelling — "
            "loopt de klok van de machine goed?"
        )

    # 5) goedkoopste blok van N uur
    blocks = find_cheapest_blocks(
        rows,
        granularity_min=pr["granularity_min"],
        block_hours=int(args.block_hours or opt["block_hours"]),
        only_future=bool(opt["only_future"]),
        top_n=int(opt["top_n"]),
        now=now,
    )

    return {
        "generated_at": now,
        "location": {
            "name": loc["name"],
            "lat": args.lat or loc["lat"],
            "lon": args.lon or loc["lon"],
            "timezone": loc["timezone"],
        },
        "heatpump": {
            "model": "REMKO WKF 70 (NEO) compact",
            "supply_temperature_c": supply,
            "cop_source": model.source,
        },
        "prices": {
            "source": pr["source"],
            "granularity_min": pr["granularity_min"],
            "horizon_start": rows[0]["dt_local"],
            "horizon_end": rows[-1]["dt_local"],
        },
        "rows": rows,
        "blocks": blocks,
        "best": blocks[0] if blocks else None,
    }


def render_human(result: dict) -> str:
    loc = result["location"]
    hp = result["heatpump"]
    pr = result["prices"]
    best = result["best"]

    lines: List[str] = []
    lines.append("REMKO WKF 70 (NEO) compact — dynamisch stroomadvies")
    lines.append("=" * 64)
    lines.append(
        f"Locatie : {loc['name']} ({loc['lat']}, {loc['lon']}) [{loc['timezone']}]"
    )
    lines.append(f"COP     : {hp['cop_source']}")
    lines.append(
        f"Prijzen : {pr['source']} — granulariteit {pr['granularity_min']} min"
    )
    lines.append(
        f"Bereik  : {fmt_dt(pr['horizon_start'])}  t/m  {fmt_dt(pr['horizon_end'])}"
    )
    lines.append("")

    lines.append("Per slot (prijs, verwachte buitentemp., COP, gecorrigeerde prijs):")
    lines.append("  Tijd                 | prijs €/kWh |  temp °C |  COP | €/kWh warmte")
    lines.append("-" * 72)
    for r in result["rows"]:
        lines.append(
            f"  {fmt_slot(r['dt_local'])}   |  {nl(r['price'], 3):>9}  | "
            f"{nl(r['temp'], 1):>7}  | {nl(r['cop'], 2):>4} |  {nl(r['corrected'], 3):>9}"
        )

    if best:
        lines.append("")
        lines.append("BESTE BLOK VAN 3 UUR (op gecorrigeerde prijs):")
        lines.append("-" * 64)
        lines.append(f"  Start : {fmt_dt(best['start'])}")
        lines.append(f"  Einde : {fmt_dt(best['end'])}")
        lines.append(f"  Stroomprijs gem.    : {nl(best['mean_price'], 3)} €/kWh")
        lines.append(f"  Buitentemperatuur gem.: {nl(best['mean_temp'], 1)} °C")
        lines.append(f"  COP gem.            : {nl(best['mean_cop'], 2)}")
        lines.append(
            f"  Gecorr. prijs gem.   : {nl(best['mean_corrected'], 4)} €/kWh warmte "
            f"(= stroomprijs / COP)"
        )

        if len(result["blocks"]) > 1:
            lines.append("")
            lines.append("Volgende beste blokken:")
            for i, b in enumerate(result["blocks"][1:], start=2):
                lines.append(
                    f"  {i}. {fmt_dt(b['start'])} – {fmt_dt(b['end'])}  "
                    f"→ {nl(b['mean_corrected'], 4)} €/kWh warmte"
                )
    else:
        lines.append("")
        lines.append("Geen volledig 3-uursblok gevonden binnen de (toekomstige) data.")
    return "\n".join(lines)


def mqtt_payloads(result: dict, cfg: dict) -> dict:
    """Bouw de payloads voor de MQTT-topics."""
    best = result["best"]
    advice = (
        {
            "recommended": True,
            "start": best["start"].isoformat(),
            "end": best["end"].isoformat(),
            "mean_price_eur_per_kwh": best["mean_price"],
            "mean_outside_temp_c": best["mean_temp"],
            "mean_cop": best["mean_cop"],
            "mean_corrected_eur_per_kwh_heat": best["mean_corrected"],
            "block_hours": best["block_hours"],
        }
        if best
        else {"recommended": False}
    )
    now_local = result["generated_at"]
    prices_rows = [
        {
            "time": r["dt_local"].isoformat(),
            "price_eur_per_kwh": r["price"],
            "temp_c": r["temp"],
            "cop": r["cop"],
            "corrected_eur_per_kwh_heat": r["corrected"],
        }
        for r in result["rows"]
        if r["dt_local"] >= now_local
    ]
    return {
        "advice": advice,
        "prices": prices_rows,
        "status": {
            "ok": True,
            "generated_at": now_local.isoformat(),
            "granularity_min": result["prices"]["granularity_min"],
            "supply_temperature_c": result["heatpump"]["supply_temperature_c"],
        },
    }


def to_serializable(result: dict) -> dict:
    out = dict(result)
    out["generated_at"] = result["generated_at"].isoformat()
    out["prices"] = dict(result["prices"])
    out["prices"]["horizon_start"] = out["prices"]["horizon_start"].isoformat()
    out["prices"]["horizon_end"] = out["prices"]["horizon_end"].isoformat()
    out["rows"] = [
        {
            "time": r["dt_local"].isoformat(),
            "price_eur_per_kwh": r["price"],
            "temp_c": r["temp"],
            "cop": r["cop"],
            "corrected_eur_per_kwh_heat": r["corrected"],
        }
        for r in result["rows"]
    ]
    out["blocks"] = [
        {
            "start": b["start"].isoformat(),
            "end": b["end"].isoformat(),
            "block_hours": b["block_hours"],
            "mean_price_eur_per_kwh": b["mean_price"],
            "mean_temp_c": b["mean_temp"],
            "mean_cop": b["mean_cop"],
            "mean_corrected_eur_per_kwh_heat": b["mean_corrected"],
        }
        for b in result["blocks"]
    ]
    out["best"] = out["blocks"][0] if out["blocks"] else None
    return out


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="REMKO WKF 70 dynamisch stroomadvies")
    ap.add_argument("--config", default=os.path.join(script_dir(), "config.json"))
    ap.add_argument("--json", action="store_true", help="JSON-output op stdout")
    ap.add_argument("--no-mqtt", action="store_true", help="MQTT-publicatie uitschakelen")
    ap.add_argument("--block-hours", type=int, default=None)
    ap.add_argument("--supply-temperature", type=int, default=None)
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--lon", type=float, default=None)
    ap.add_argument(
        "--now",
        default=None,
        help="ISO-tijdstip voor 'nu' (handig voor testen, bv. 2026-09-17T21:00:00+02:00)",
    )
    args = ap.parse_args(argv)

    cfg: dict = {}
    try:
        cfg = load_config(args.config)
        tz = ZoneInfo(cfg["location"]["timezone"])
        now_utc = datetime.now(tz) if not args.now else datetime.fromisoformat(args.now)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=tz)

        result = build_advice(cfg, args, now_utc)

        mqtt_ok = False
        if not args.no_mqtt and cfg.get("mqtt", {}).get("enabled"):
            payloads = mqtt_payloads(result, cfg)
            mqtt_ok = mqtt_out.publish(cfg["mqtt"], payloads)

        if args.json:
            out = to_serializable(result)
            out["mqtt"] = {"published": mqtt_ok}
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(render_human(result))
            print("")
            status_mqtt = "gepubliceerd" if mqtt_ok else "niet beschikbaar (paho-mqtt ontbreekt of uitgeschakeld)"
            print(f"MQTT: {status_mqtt}")
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI moet een nette foutmelding geven
        print(f"FOUT: {exc}", file=sys.stderr)
        if cfg.get("mqtt", {}).get("enabled"):
            try:
                mqtt_out.publish(
                    cfg["mqtt"],
                    {"status": {"ok": False, "error": str(exc)}},
                )
            except Exception:  # noqa: BLE001
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())