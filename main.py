#!/usr/bin/env python3
"""REMKO WKF 70 (NEO) compact — dynamisch stroomadvies.

Stroomlijn:
  1. COP(t_buiten) uit de specs van de WKF 70 NEO compact (EN 14511)
  2. uurvoorspelling buitentemperatuur via met.no
  3. dynamische kWh-prijzen via EnergyZero/EasyEnergy (publiek, geen key)
  4. gecorrigeerde prijs per slot = kWh-prijs / COP(buitentemp.)
  5. goedkoopste aaneengesloten blok van N uur (standaard 3)
  6. (optioneel) idem voor sanitair warm water tot 53 °C (eigen COP-curve)

Gebruik:  python3 main.py [--json] [--no-mqtt] [opties]
Zie README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
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


def _append_best_block(
    lines: List[str], best: dict, blocks: List[dict], title: str
) -> None:
    if not best:
        lines.append("")
        lines.append("Geen volledig 3-uursblok gevonden binnen de (toekomstige) data.")
        return
    lines.append("")
    lines.append(title)
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
    if len(blocks) > 1:
        lines.append("")
        lines.append("Volgende beste blokken:")
        for i, b in enumerate(blocks[1:], start=2):
            lines.append(
                f"  {i}. {fmt_dt(b['start'])} – {fmt_dt(b['end'])}  "
                f"→ {nl(b['mean_corrected'], 4)} €/kWh warmte"
            )


def _trim_state_to(state: dict, moment: datetime) -> dict:
    """Kopie van de state zonder boosts die op `moment` al buiten het
    rollende 24-uursvenster vallen — voor het advies-plan na het bereiken
    van de daglimiet (de watcher wacht dan tot het oudste blok weg is)."""
    cutoff = moment - timedelta(hours=24)
    daily: dict = {}
    for _dag, iso_list in (state.get("daily_boosts") or {}).items():
        bewaard = []
        for iso in iso_list:
            try:
                if datetime.fromisoformat(iso) >= cutoff:
                    bewaard.append(iso)
            except ValueError:
                bewaard.append(iso)  # onbekend formaat: maar bewaren
        if bewaard:
            daily[_dag] = bewaard
    return dict(state, daily_boosts=daily)


def _dhw_boost_plan(
    cfg: dict,
    granularity_min: int,
    now: datetime,
    dhw_rows: list,
    state: Optional[dict] = None,
) -> Tuple[list, int, float, str, str, str, str]:
    """De SWW-boost-momenten die dhw_boost --watch gaat uitsturen.

    Het plan volgt dezelfde beslisregels als de watcher (_next_boost_block):
    het resterende aantal boosts binnen het rollend 24-uursvenster (niet
    opnieuw vanaf nul!), telkens pas ná `min_gap_hours` uur ná het einde van
    het vorige blok, één blok per cyclus verplicht binnen
    `afternoon_from`–`afternoon_to` (default 12:00–23:00), het laatste blok
    dat bovendien eindigt tussen `last_block_end_from` en
    `last_block_end_to` (default 19:00–08:00), en élk blok startend vóór
    dat ochtend-`last_block_end_to` zodat niets doorschuift naar een
    goedkopere dag verderop.

    Zonder meegegeven `state` wordt de statusfile van de watcher gelezen, zodat
    het getoonde tijdstip ook daadwerkelijk wordt uitgevoerd.

    Geeft (plan, boosts_per_day, min_gap_hours, last_block_end_from,
    last_block_end_to, afternoon_from, afternoon_to, dagvenster_gedekt):
    plan is de lijst gekozen blokken in oplopende volgorde;
    dagvenster_gedekt = True als er in dit 24-u-venster al een boost binnen
    afternoon_from–afternoon_to zit (en er dus terecht geen dagblok in het
    plan staat).
    """
    boost_cfg = (cfg.get("mqtt") or {}).get("dhw_boost") or {}
    # lazy import: dhw_boost importeert main, dus niet op module-niveau
    from dhw_boost import (
        DEFAULT_BOOSTS_PER_DAY,
        DEFAULT_BOOST_GAP_HOURS,
        LAST_BLOCK_END_FROM,
        LAST_BLOCK_END_TO,
        AFTERNOON_FROM,
        AFTERNOON_TO,
        _count_boosts_last_24h,
        _dagboost_in_venster,
        _next_boost_block,
        _oldest_recent_boost,
        _record_boost,
        load_state,
    )

    per_day = int(boost_cfg.get("boosts_per_day", DEFAULT_BOOSTS_PER_DAY))
    gap_hours = float(boost_cfg.get("min_gap_hours", DEFAULT_BOOST_GAP_HOURS))
    end_from = boost_cfg.get("last_block_end_from", LAST_BLOCK_END_FROM)
    end_to = boost_cfg.get("last_block_end_to", LAST_BLOCK_END_TO)
    dag_from = boost_cfg.get("afternoon_from", AFTERNOON_FROM)
    dag_to = boost_cfg.get("afternoon_to", AFTERNOON_TO)

    # De échte staat van de watcher: als die al een boost in het afgelopen
    # 24-uursvenster heeft, plant dit plan díe resterende blokken — precies
    # wat --watch ook gaat sturen.
    if state is None:
        state = load_state()
    else:
        state = dict(state)  # de state van de caller niet muteren

    step_now = now
    if _count_boosts_last_24h(state, step_now) >= per_day:
        # Daglimiet bereikt: de watcher wacht tot het oudste blok uit het
        # venster valt. Vanaf dat moment telt dit plan opnieuw op.
        step_now = _oldest_recent_boost(state, step_now) + timedelta(
            hours=24, seconds=2
        )
        state = _trim_state_to(state, step_now)

    # Heeft dit rollende 24-u-venster al een boost die binnen het dagvenster
    # (afternoon_from–afternoon_to) startte? Dan is de dagvenster-verplichting
    # al gedekt en plant dit plan (terecht) géén dagblok meer. Dat in de
    # uitvoer melden, zodat "waar is het blok om 12:00?" geen verrassing is.
    dag_gedekt = (
        _count_boosts_last_24h(state, step_now) >= 1
        and _dagboost_in_venster(state, step_now, dag_from, dag_to)
    )

    # Dezelfde beslisregels als de watcher, op een advies-dict dat alleen de
    # prijsrijen bevat (wat de planner nodig heeft).
    pseudo = {
        "dhw": {"rows": dhw_rows},
        "prices": {"granularity_min": granularity_min},
    }

    plan: List[dict] = []
    # data-horizon: het laatste moment waarop een blok nog kán eindigen in de
    # aanwezige prijzen. Eindigt een gekozen blok daar (of nét ervóór), dan
    # kan het optimum zomaar ná de horizon liggen — zie de hint in de output.
    slot_delta = timedelta(minutes=granularity_min)
    horizon_end = dhw_rows[-1]["dt_local"] + slot_delta if dhw_rows else None
    openstaand = max(0, per_day - _count_boosts_last_24h(state, step_now))
    for _ in range(openstaand):
        block = _next_boost_block(pseudo, boost_cfg, state, step_now)
        if not block:
            break
        if horizon_end is not None:
            block = dict(block, horizon_bound=block["end"] >= horizon_end)
        plan.append(block)
        # Alsof deze boost verstuurd is: de volgende keuze houdt dan rekening
        # met de 24-uurslimiet, de minimale afstand én of het dagblok al
        # gedekt is.
        _record_boost(state, block["start"])
        state["last_sent_end"] = block["end"].isoformat()
        step_now = block["end"]
    return (
        plan,
        per_day,
        gap_hours,
        end_from,
        end_to,
        dag_from,
        dag_to,
        dag_gedekt,
    )


def _fetch_prices_with_fallback(
    prices_cfg: dict, loc: dict, days_ahead: int
) -> Tuple[dict, str]:
    """Haal stroomprijzen op; bij totale mislukking de fallback-bron proberen.

    Primaire bron = `prices_cfg["source"]` ('entsoe' of 'energyzero').
    `prices_cfg["fallback_source"]` kiest de reservebron:
      - ontbrekend of "auto" -> de andere bekende bron (entsoe <-> energyzero);
      - expliciet "entsoe"/"energyzero" -> alleen die; false/null -> uit.
    Levert (pr, gebruikte_bron). Is er een fallback gebruikt, dan staat dat
    in `pr["source"]` en als waarschuwing in `pr["warnings"]` (wordt in het
    advies doorgegeven), zodat een vervangende bron nooit stilzwijgend
    meedraait. Falen beide bronnen: de fout van de primaire bron.
    """
    def _fetch(source: str) -> dict:
        if source == "entsoe":
            ec = prices_cfg["entsoe"]
            return entsoe.fetch_prices(
                api_key=ec["api_key"],
                tz=loc["timezone"],
                days_ahead=days_ahead,
                in_domain=ec.get("in_domain", "10YNL----------L"),
                out_domain=ec.get("out_domain", "10YNL----------L"),
                cache_ttl_seconds=int(ec.get("cache_ttl_seconds", entsoe.DEFAULT_CACHE_TTL)),
            )
        if source == "energyzero":
            ez = prices_cfg["energyzero"]
            return energyzero.fetch_prices(
                tz=loc["timezone"],
                days_ahead=days_ahead,
                api_url=ez.get("api_url", energyzero.API_URL_DEFAULT),
                usage_type=int(ez.get("usage_type", 1)),
                incl_btw=bool(ez.get("incl_btw", True)),
            )
        raise ValueError(f"onbekende prijsbron: {source!r} (kies 'entsoe' of 'energyzero')")

    source = prices_cfg.get("source", "entsoe")
    fallback = prices_cfg.get("fallback_source")
    candidates = [source]
    if fallback is not False and source in ("entsoe", "energyzero"):
        pinned = fallback if isinstance(fallback, str) and fallback in ("entsoe", "energyzero") else None
        candidates.append(pinned or ("energyzero" if source == "entsoe" else "entsoe"))
    candidates = list(dict.fromkeys(candidates))  # dubbelen eruit

    pr: Optional[dict] = None
    used: Optional[str] = None
    primary_error: Optional[Exception] = None
    for cand in candidates:
        try:
            pr = _fetch(cand)
            used = cand
            break
        except Exception as exc:  # noqa: BLE001 — de fallback vangt elke bronfout
            if primary_error is None:
                primary_error = exc
    if pr is None:
        # beide bronnen faalden: de fout van de PRIMAIRE bron (bv. een
        # entsoe.PricesNotAvailableError — de watcher stemt daar zijn
        # herpoging op af)
        raise primary_error  # type: ignore[misc]  # altijd gezet in de loop

    if used != candidates[0]:
        pr["warnings"] = list(pr.get("warnings") or []) + [
            f"bron {candidates[0]!r} faalde ({primary_error}); verder met {used!r}"
        ]
        pr["source"] += f" (fallback: {used})"
    return pr, used


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
        53: hp.get("cop_curve_w53"),
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

    # 1b) SWW-model: sanitair warm water met eigen aanvoertemperatuur (bv. 53 °C)
    dhw_cfg = hp.get("dhw") or {}
    dhw_enabled = bool(dhw_cfg.get("enabled", False))
    dhw_model = None
    if dhw_enabled:
        dhw_temp = int(dhw_cfg.get("temperature", 53))
        dhw_key = dhw_cfg.get("curve_key", f"cop_curve_w{dhw_temp}")
        dhw_curve = curves.get(dhw_temp) or hp.get(dhw_key)
        if dhw_curve is None:
            raise ValueError(
                f"geen COP-curve gevonden voor SWW-temperatuur {dhw_temp} °C "
                f"(voeg '{dhw_key}' toe aan heatpump in de config)"
            )
        dhw_model = cop_mod.CopModel(supply_temperature=dhw_temp, curve=dhw_curve)

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

    # 3) stroomprijzen (entsoe | energyzero, met automatische fallback)
    prices_cfg = cfg["prices"]
    days_ahead = int(prices_cfg.get("days_ahead", 3))
    pr, used = _fetch_prices_with_fallback(prices_cfg, loc, days_ahead)

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

    # 5b) SWW: zelfde prijzen en temperaturen, maar een andere COP-curve én
    #     een eigen, kortere bloklengte (mqtt.dhw_boost.block_hours, default
    #     1 u; het blok voor de ruimteverwarming is optimization.block_hours).
    dhw_boost_cfg = (cfg.get("mqtt") or {}).get("dhw_boost") or {}
    # lazy import: dhw_boost importeert main, dus niet op module-niveau
    from dhw_boost import DEFAULT_BOOST_BLOCK_HOURS

    dhw_bh = int(dhw_boost_cfg.get("block_hours", DEFAULT_BOOST_BLOCK_HOURS))
    dhw_rows = (
        build_corrected_rows(pr["slots"], temps_by_hour, dhw_model)
        if dhw_model
        else []
    )
    dhw_blocks = (
        find_cheapest_blocks(
            dhw_rows,
            granularity_min=pr["granularity_min"],
            block_hours=dhw_bh,
            only_future=bool(opt["only_future"]),
            top_n=int(opt["top_n"]),
            now=now,
        )
        if dhw_rows
        else []
    )

    # 5c) SWW-boost-plan: de gespreide opwarmmomenten die de booster uitstuurt.
    dhw_plan: List[dict] = []
    dhw_boosts_per_day: Optional[int] = None
    dhw_gap_hours: Optional[float] = None
    dhw_last_end_from: Optional[str] = None
    dhw_last_end_to: Optional[str] = None
    dhw_afternoon_from: Optional[str] = None
    dhw_afternoon_to: Optional[str] = None
    dhw_dagvenster_gedekt = False
    if dhw_rows:
        (
            dhw_plan,
            dhw_boosts_per_day,
            dhw_gap_hours,
            dhw_last_end_from,
            dhw_last_end_to,
            dhw_afternoon_from,
            dhw_afternoon_to,
            dhw_dagvenster_gedekt,
        ) = _dhw_boost_plan(
            cfg,
            pr["granularity_min"],
            now,
            dhw_rows,
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
            # dagen die deze ronde níet opgehaald konden worden + een evt.
            # fallback-noot; de SWW-watcher logt dit (price_warnings)
            "warnings": pr.get("warnings") or [],
        },
        "rows": rows,
        "blocks": blocks,
        "best": blocks[0] if blocks else None,
        "dhw": {
            "enabled": dhw_enabled,
            "temperature_c": dhw_model.supply_temperature if dhw_model else None,
            "cop_source": (
                dhw_model.source
                + " (geschat: interpolatie tussen W45- en W55-metwaarden)"
                if dhw_model
                else None
            ),
            "rows": dhw_rows,
            "blocks": dhw_blocks,
            "best": dhw_blocks[0] if dhw_blocks else None,
            "block_hours": dhw_bh,
            "plan": dhw_plan,
            "boosts_per_day": dhw_boosts_per_day,
            "min_gap_hours": dhw_gap_hours,
            "last_block_end_from": dhw_last_end_from,
            "last_block_end_to": dhw_last_end_to,
            "afternoon_from": dhw_afternoon_from,
            "afternoon_to": dhw_afternoon_to,
            # True als een boost in dit 24-u-venster al binnen het dagvenster
            # startte: dan plant het plan (terecht) geen dagblok meer.
            "dagvenster_gedekt": dhw_dagvenster_gedekt,
        },
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
    dhw = result.get("dhw")
    if dhw and dhw.get("enabled"):
        lines.append(f"SWW     : sanitair warm water tot {dhw['temperature_c']} °C — {dhw['cop_source']}")
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

    _append_best_block(
        lines,
        best,
        result["blocks"],
        "BESTE BLOK VAN 3 UUR (ruimteverwarming, op gecorrigeerde prijs):",
    )

    if dhw and dhw.get("rows"):
        lines.append("")
        lines.append("SANITAIR WARM WATER (SWW) — opwarmen tot 53 °C")
        lines.append("-" * 64)
        lines.append(f"COP     : {dhw['cop_source']}")
        lines.append("Per slot (prijs, verwachte buitentemp., COP, gecorrigeerde prijs):")
        lines.append("  Tijd                 | prijs €/kWh |  temp °C |  COP | €/kWh warmte")
        lines.append("-" * 72)
        for r in dhw["rows"]:
            lines.append(
                f"  {fmt_slot(r['dt_local'])}   |  {nl(r['price'], 3):>9}  | "
                f"{nl(r['temp'], 1):>7}  | {nl(r['cop'], 2):>4} |  {nl(r['corrected'], 3):>9}"
            )
        _append_best_block(
            lines,
            dhw["best"],
            # alleen het beste blok tonen hier: de echt te verwachten momenten
            # staan in het geplande boost-plan hieronder (de 'naast beste'
            # blokken liggen immers vaak vlak tegen het beste aan)
            dhw["blocks"][:1],
            f"BESTE BLOK VAN {dhw.get('block_hours') or 1:g} UUR VOOR SWW "
            "(op gecorrigeerde prijs):",
        )
        plan = dhw.get("plan") or []
        if plan:
            lines.append("")
            lines.append(
                "GEPLANDE SWW-BOOSTS"
                f" ({dhw.get('boosts_per_day')}x per 24 u, min. "
                f"{dhw.get('min_gap_hours') or 0.0:g} u tussen de blokken; "
                f"laatste blok eindigt {dhw.get('last_block_end_from') or '19:00'}–"
                f"{dhw.get('last_block_end_to') or '08:00'}; verplicht dagvenster "
                f"{dhw.get('afternoon_from') or '12:00'}–"
                f"{dhw.get('afternoon_to') or '23:00'}):"
            )
            lines.append("-" * 64)
            for i, b in enumerate(plan, start=1):
                dag = "  [dagvenster]" if b.get("dagblok") else ""
                lines.append(
                    f"  {i}. {fmt_dt(b['start'])} – {fmt_dt(b['end'])}  "
                    f"→ {nl(b['mean_corrected'], 4)} €/kWh warmte  "
                    f"(COP {nl(b['mean_cop'], 2)}){dag}"
                )
            if dhw.get("dagvenster_gedekt") and plan:
                lines.append("")
                lines.append(
                    "  (er is in dit 24-u-venster al een boost binnen het dagvenster "
                    f"{dhw.get('afternoon_from') or '12:00'}–"
                    f"{dhw.get('afternoon_to') or '23:00'} verstuurd; daarom staat "
                    "hier geen dagvenster-blok meer in — alleen nog het laatste "
                    "blok van de daglimiet.)"
                )
            if any(b.get("horizon_bound") for b in plan):
                lines.append("")
                lines.append(
                    "  (laatste blok ligt tegen de data-horizon aan: de day-ahead "
                    "prijzen van de dag daarop zijn nog niet gepubliceerd (≈13:30). "
                    "De watcher herberekent dàn en verschuift dit blok naar de "
                    "goedkoopste uren van de nieuwe dag.)"
                )
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


def mqtt_payloads_dhw(result: dict) -> dict:
    """Extra MQTT-payloads voor sanitair warm water (topics dhw/advice, dhw/prices)."""
    dhw = result.get("dhw")
    if not dhw or not dhw.get("enabled") or not dhw.get("rows"):
        return {}
    now_local = result["generated_at"]
    dhw_best = dhw["best"]
    return {
        "dhw/advice": (
            {
                "recommended": True,
                "start": dhw_best["start"].isoformat(),
                "end": dhw_best["end"].isoformat(),
                "mean_price_eur_per_kwh": dhw_best["mean_price"],
                "mean_outside_temp_c": dhw_best["mean_temp"],
                "mean_cop": dhw_best["mean_cop"],
                "mean_corrected_eur_per_kwh_heat": dhw_best["mean_corrected"],
                "block_hours": dhw_best["block_hours"],
            }
            if dhw_best
            else {"recommended": False}
        ),
        "dhw/prices": [
            {
                "time": r["dt_local"].isoformat(),
                "price_eur_per_kwh": r["price"],
                "temp_c": r["temp"],
                "cop": r["cop"],
                "corrected_eur_per_kwh_heat": r["corrected"],
            }
            for r in dhw["rows"]
            if r["dt_local"] >= now_local
        ],
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

    dhw = result.get("dhw")
    if dhw:
        out["dhw"] = {
            "enabled": dhw["enabled"],
            "temperature_c": dhw["temperature_c"],
            "cop_source": dhw["cop_source"],
            "rows": [
                {
                    "time": r["dt_local"].isoformat(),
                    "price_eur_per_kwh": r["price"],
                    "temp_c": r["temp"],
                    "cop": r["cop"],
                    "corrected_eur_per_kwh_heat": r["corrected"],
                }
                for r in dhw["rows"]
            ],
            "blocks": [
                {
                    "start": b["start"].isoformat(),
                    "end": b["end"].isoformat(),
                    "block_hours": b["block_hours"],
                    "mean_price_eur_per_kwh": b["mean_price"],
                    "mean_temp_c": b["mean_temp"],
                    "mean_cop": b["mean_cop"],
                    "mean_corrected_eur_per_kwh_heat": b["mean_corrected"],
                }
                for b in dhw["blocks"]
            ],
            "plan": [
                {
                    "start": b["start"].isoformat(),
                    "end": b["end"].isoformat(),
                    "block_hours": b["block_hours"],
                    "mean_price_eur_per_kwh": b["mean_price"],
                    "mean_temp_c": b["mean_temp"],
                    "mean_cop": b["mean_cop"],
                    "mean_corrected_eur_per_kwh_heat": b["mean_corrected"],
                    "horizon_bound": bool(b.get("horizon_bound")),
                }
                for b in dhw.get("plan") or []
            ],
            "boosts_per_day": dhw.get("boosts_per_day"),
            "min_gap_hours": dhw.get("min_gap_hours"),
            "last_block_end_from": dhw.get("last_block_end_from"),
            "last_block_end_to": dhw.get("last_block_end_to"),
        }
        out["dhw"]["best"] = out["dhw"]["blocks"][0] if out["dhw"]["blocks"] else None
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
            payloads.update(mqtt_payloads_dhw(result))
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