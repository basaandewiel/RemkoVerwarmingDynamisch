#!/usr/bin/env python3
"""SWW-boost: publiceer het start-commando op het moment dat het goedkoopste
3-uursblok voor sanitair warm water (SWW) begint, en zet aan het einde van
dat blok de gewenste boilertemperatuur terug naar de default (40 °C).

Twee manieren:
  A) one-shot via cron (elke 5-10 min): werkt prima, maar het commando gaat
     hooguit interval-minuten ná de blokstart uit.
     */5 * * * * cd ~/github/remkoverwarming && /usr/bin/python3 dhw_boost.py >> boost.log 2>&1

  B) --watch (aanbevolen): het script blijft draaien en wordt alleen wakker
     als er iets kan veranderen of gebeuren:
       * de blokstart zelf             -> dan wordt het commando verstuurd;
       * het einde van een geboost blok      -> de reset terug naar de
         default-temperatuur (zodra een boost verstuurd is, staat het
         blokeinde altijd als wake gepland);
       * de dagelijkse prijs-update    -> rond 13:30 verschijnen de
         dag-ahead-prijzen van de volgende dag, de enige keer dat het beste
         blok kan veranderen (--price-refresh-time, default 13:30). Zijn
         die prijzen daar nog niet (late publicatie), dan herberekent de
         watcher later nogmaals (--price-recheck-min, default 45 min),
         zolang het geplande blok nog niet in de LEAD-nadering zit;
       * (alleen zolang er nog géén blok bekend is, bijv. vertraagde
         prijzen) elke --retry-interval (default 30 min).
     Herberekenen om de paar minuten is bewust niet nodig: het DHW-water
     wordt dagelijks bijverwarmd en het 3-uursblok is tussen deze momenten
     stabiel. Te starten via systemd (bijlage in README) of nohup.

Werking (beide modi):
  1. berekent hetzelfde advies als main.py (zelfde config),
  2. als 'nu' binnen de eerste `trigger_minutes` van het beste SWW-blok valt,
     wordt het start-commando gepubliceerd op mqtt.control_topic (default
     topic_base zélf, bv. V04P26/SMTID/CLIENT2HOST — de gateway luistert
     daar, niet op een "/set"-subtopic). De waarde van register 1082 is
     afgeleid uit heatpump.dhw.temperature uit config.json: temperatuur
     &times; 10 als hexadecimaal getal (53 &deg;C &rarr; 530 decimal &rarr;
     "0212"), in het formaat dat de gateway accepteert:
     {"FORCE_RESPONSE": true, "values": {"1082": "0212"}}.
  3. aan het EINDE van het blok wordt de gewenste temperatuur teruggezet
     naar de default (mqtt.dhw_boost.default_temperature, default 40 &deg;C
     &rarr; 400 decimal &rarr; "0190"), zodat de boiler niet de rest van de
     dag door blijft verwarmen op duur stroom. Dit reset-commando gaat dan
     dus ook uit als het inschakel-commando is verstuurd.
  4. een statusfile in ~/.cache/remko-wkf70 onthoudt per blok-start welke
     commando's (boost én reset) er al verstuurd zijn, zodat er geen dubbele
     berichten tijdens hetzelfde blok uitgaan.
  5. er gaan maximaal `boosts_per_day` boosts per **rollend venster van 24 uur**
   uit (default 2, niet gebonden aan een kalenderdag). De volgende boost wordt
   pas gepland ná `min_gap_hours` uur ná het einde van de vorige, zodat een
   tweede opwarmperiode écht niet vlak na de eerste ligt (geen aangrenzende/
   naburige goedkoopste momenten). Het LAATSTE blok van het venster (het
   blok dat de daglimiet op `boosts_per_day` brengt) eindigt bovendien
   tussen 19:00 en 08:00 (volgende ochtend, instelbaar via
   `last_block_end_from`/`last_block_end_to`): de laatste opwarmperiode van
   de dag eindigt dan nooit midden op de dag — dat was het probleem toen de
   tweede boost telkens naar de volgende middag doorschoof. Is het venster
   vol, dan wacht de watcher tot het oudste blok er weer uit valt (het derde
   blok kan nooit binnen 24 u van de eerste twee starten). Wordt een boost
   gemist, dan mag de eerstvolgende alsnog gaan.

Het reset-commando wordt alleen gestuurd ná een verstuurd boost-commando
voor datzelfde blok; is het blok gemist, dan blijft de standaardwaarde
gewoon staan.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, time as dtime
from typing import List, Optional
from zoneinfo import ZoneInfo

import main as app
import mqtt_out
from optimizer import find_cheapest_blocks

DEFAULT_DHW_TEMP = 53.0
DEFAULT_RESET_TEMP = 40.0
DEFAULT_BOOSTS_PER_DAY = 2      # meerdere opwarmmomenten per dag
DEFAULT_BOOST_GAP_HOURS = 4.0   # min. uren tussen einde vorige boost en start volgende
DEFAULT_BOOST_BLOCK_HOURS = 1   # bloklengte van één SWW-boost (het blok voor de
                                # ruimteverwarming blijft optimization.block_hours,
                                # default 3 — mqtt.dhw_boost.block_hours)
LAST_BLOCK_END_FROM = "19:00"   # het LAATSTE boost-blok van het 24-u-venster
LAST_BLOCK_END_TO = "08:00"     # eindigt tussen 19:00 's avonds en 08:00 's ochtends
                                # (de boiler is dan 's avonds/nachts opgewarmd in
                                # plaats van dat de 2e opwarmperiode overdag
                                # eindigt — mqtt.dhw_boost.last_block_end_from/to)
AFTERNOON_FROM = "12:00"        # verplicht dagblok: één van de boosts per dag
AFTERNOON_TO = "23:00"          # start binnen dit venster (mqtt.dhw_boost.
                                # afternoon_from/to). Warm water als het buiten
                                # warm is (weinig verlies), vaak de laagste
                                # dagprijzen door zonneschijn, en een avondblok
                                # vangt ook de winddip op. Het eind-venster en de
                                # ochtend-start-grens gelden dáár niet.
ROLLING_WINDOW_HOURS = 24.0    # de boostlimiet telt per rollend 24-uursvenster


def build_boost_payload(dhw_temperature: float) -> dict:
    """1082-waarde = gewenste SWW-temperatuur ('C x 10) in hexadecimaal.

    Voorbeeld: 53 graden -> 530 decimal -> 0x212 -> "0212" (4 cijfers,
    zelfde formaat als het oorspronkelijke "0190" = 0x190 = 40 graden).

    FORCE_RESPONSE wordt door de REMKO-gateway geaccepteerd (net als in haar
    eigen query-berichten) en dwingt een directe status-reactie af, zodat de
    publicatie bevestigd wordt op HOST2CLIENT.
    """
    value_dec = int(round(dhw_temperature * 10.0))
    value_hex = format(value_dec, "04x")
    return {"FORCE_RESPONSE": True, "values": {"1082": value_hex}}

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
    # De REMKO-gateway luistert op CLIENT2HOST zélf (géén "/set"-subtopic):
    # topic = mqtt.control_topic, tenzij niet gezet -> topic_base.
    topic = (mqtt_cfg.get("control_topic") or base).strip()
    dhw_cfg = (cfg.get("heatpump") or {}).get("dhw") or {}
    dhw_temp = dhw_cfg.get("temperature", DEFAULT_DHW_TEMP)
    boost_payload = build_boost_payload(float(dhw_temp))
    reset_temp = boost_cfg.get("default_temperature", DEFAULT_RESET_TEMP)
    reset_payload = build_boost_payload(float(reset_temp))
    window_min = int(boost_cfg.get("trigger_minutes", 45))

    out: dict = {
        "now": now.isoformat(),
        "enabled": bool(mqtt_cfg.get("enabled")) and bool(boost_cfg.get("enabled", True)),
        "topic": topic,
        "payload": boost_payload,
        "reset_payload": reset_payload,
        # Boost/reset worden met QoS 1 verstuurd: de broker moet de ontvangst
        # bevestigen (PUBACK) voordat "VERSTUURD" getoond wordt.
        "qos": int(boost_cfg.get("qos", 1)),
        "retain": bool(boost_cfg.get("retain", False)),
    }

    if not out["enabled"]:
        out["status"] = "disabled"
        return out

    # 1) Openstaande reset? Een boost is verstuurd voor een blok dat inmiddels
    #    voorbij is, en de reset daarnaar is nog niet gedaan -> reset sturen.
    state = load_state()
    sent_start = state.get("last_sent_start")
    sent_end = state.get("last_sent_end")
    if sent_start and sent_end and state.get("last_reset_start") != sent_start:
        end_dt = datetime.fromisoformat(sent_end)
        if now >= end_dt:
            out["status"] = "reset"
            out["payload"] = reset_payload  # dit bericht moet nu de reset zijn
            # COP-gegevens van het oorspronkelijke boost-blok komen uit de
            # statusfile (slaat bij de boost op); anders ontbreken ze en toont
            # render_human ze niet.
            out["block"] = {
                "start": sent_start,
                "end": sent_end,
                "mean_cop": state.get("sent_mean_cop"),
                "mean_corrected_eur_per_kwh_heat": state.get("sent_mean_corrected"),
            }
            return out
        # Reset staat nog open maar het blok loopt nog: het blokeinde is dan
        # het eerstvolgende moment dat er iets te doen valt. Zonder deze
        # markering zou de watcher doorslapen naar het *volgende* blok (het
        # lopende blok voldoet door `only_future` niet meer als kandidaat) en
        # de reset te laat of helemaal niet versturen.
        out["pending_reset_at"] = sent_end

    advice = app.build_advice(cfg, _Args(), now)
    dhw = advice.get("dhw") or {}
    if not dhw.get("enabled") or not dhw.get("rows"):
        out["status"] = "no_dhw"
        return out

    # Late dag-ahead-publicatie detecteren: reikt de prijsdata nog niet tot in
    # de dag van morgen (terwijl days_ahead daar wél heen zou reiken), dan is
    # de nieuwe dag (nog) niet bekend. next_wake_time plant dan een extra
    # hercontrole, zodat een late publicatie binnen het uur verwerkt wordt.
    prices = advice.get("prices") or {}
    # Dagen die de prijsbron deze ronde niet opgehaald kreeg (ENTSO-E-CDN
    # flak, of een dag die nog niet gepubliceerd is). Het advies is
    # bruikbaar, maar met een kortere horizon — zichtbaar maken.
    price_warnings = prices.get("warnings") or []
    if price_warnings:
        out["price_warnings"] = list(price_warnings)
    horizon_end = prices.get("horizon_end")
    if isinstance(horizon_end, str):
        horizon_end = datetime.fromisoformat(horizon_end)
    days_ahead = int((cfg.get("prices") or {}).get("days_ahead", 3))
    out["next_day_missing"] = bool(
        days_ahead >= 2
        and horizon_end is not None
        and horizon_end.date() < now.date() + timedelta(days=1)
    )

    boosts_per_day = int(boost_cfg.get("boosts_per_day", DEFAULT_BOOSTS_PER_DAY))
    out["boosts_per_day"] = boosts_per_day
    out["min_gap_hours"] = float(boost_cfg.get("min_gap_hours", DEFAULT_BOOST_GAP_HOURS))
    out["last_block_end_from"] = boost_cfg.get(
        "last_block_end_from", LAST_BLOCK_END_FROM
    )
    out["last_block_end_to"] = boost_cfg.get("last_block_end_to", LAST_BLOCK_END_TO)
    out["afternoon_from"] = boost_cfg.get("afternoon_from", AFTERNOON_FROM)
    out["afternoon_to"] = boost_cfg.get("afternoon_to", AFTERNOON_TO)
    out["boosts_recent"] = _count_boosts_last_24h(state, now)

    # 24-uurslimiet bereikt (rollend venster, geen kalenderdag)? Dan geen
    # nieuw blok meer plannen: wakker worden zodra het oudste blok uit het
    # venster valt — dan mag de eerstvolgende boost weer.
    if out["boosts_recent"] >= boosts_per_day:
        oldest = _oldest_recent_boost(state, now)
        out["status"] = "boost_limit"
        out["limit_wake_at"] = (
            oldest + timedelta(hours=ROLLING_WINDOW_HOURS) + timedelta(seconds=2)
        ).isoformat()
        return out

    best = _next_boost_block(advice, boost_cfg, state, now)
    if not best:
        out["status"] = "no_block"
        return out

    out["block"] = {
        "start": best["start"].isoformat(),
        "end": best["end"].isoformat(),
        "mean_cop": best["mean_cop"],
        "mean_corrected_eur_per_kwh_heat": best["mean_corrected"],
        # True als dit het verplichte dagblok (afternoon_from–afternoon_to) is
        "dagblok": bool(best.get("dagblok")),
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
    out["status"] = _boost_pending_actions(state, start, boosts_per_day)
    return out


def _boost_starts(state: dict) -> List[datetime]:
    """Alle geregistreerde boost-startmomenten (uit state['daily_boosts'];
    de dag-sleutel is alleen voor opslag, de tĳdstempels tellen)."""
    starts: List[datetime] = []
    for iso_list in (state.get("daily_boosts") or {}).values():
        for iso in iso_list:
            try:
                starts.append(datetime.fromisoformat(iso))
            except ValueError:
                pass  # oud/onvolledig formaat negeren
    return starts


def _count_boosts_last_24h(state: dict, dt: datetime) -> int:
    """Aantal boost-starts binnen het rollende 24-uursvenster vóór `dt`
    (geen kalenderdaggrens: morgen-middenacht maakt het venster niet leeg)."""
    cutoff = dt - timedelta(hours=ROLLING_WINDOW_HOURS)
    return sum(1 for s in _boost_starts(state) if s >= cutoff)


def _oldest_recent_boost(state: dict, dt: datetime) -> datetime:
    """Het oudste boost-moment binnen het 24-uursvenster (vanaf dan mag er
    weer iets: dat blok valt eruit). Valt er niets uit, dan is `dt` het
    antwoord (geen extra beperking)."""
    cutoff = dt - timedelta(hours=ROLLING_WINDOW_HOURS)
    recent = [s for s in _boost_starts(state) if s >= cutoff]
    return min(recent) if recent else dt


def _record_boost(state: dict, start: datetime) -> None:
    """Registreer een verstuurde boost (moment van blokstart, voor de
    24-uurslimiet-boekhouding)."""
    day = start.date().isoformat()
    daily = state.setdefault("daily_boosts", {})
    daily.setdefault(day, []).append(start.isoformat())
    # oude dagen weggooien; de limiet telt alleen het 24-uursvenster
    recent = sorted(daily)[-7:]
    state["daily_boosts"] = {d: daily[d] for d in recent}


def _hhmm_minuten(hhmm: str) -> int:
    """'12:45' -> 765 minuten na middernacht (lokaal)."""
    hour, minute = (int(x) for x in str(hhmm).split(":"))
    return hour * 60 + minute


def next_deadline(t: datetime, hhmm: str) -> datetime:
    """Het eerstvolgende moment waarop lokale tijd `hhmm` bereikt wordt,
    streng ná `t`. Wordt gebruikt als start-grens voor het laatste
    boost-blok: dat hoort in de eerstvolgende nacht te liggen."""
    minuten = _hhmm_minuten(hhmm)
    candidate = t.replace(
        hour=minuten // 60, minute=minuten % 60, second=0, microsecond=0
    )
    return candidate if candidate > t else candidate + timedelta(days=1)


def _dagvenster(anker: datetime, van_hhmm: str, tot_hhmm: str):
    """Het venster [van, tot] op de kalenderdag van `anker`, in de tijdzone
    van `anker`. Geeft (None, None) als er zo'n venster niet bestaat
    (van > tot, of geen geldige HH:MM-waarde)."""
    try:
        van_min = _hhmm_minuten(van_hhmm)
        tot_min = _hhmm_minuten(tot_hhmm)
    except (TypeError, ValueError):
        return None, None
    if van_min > tot_min:
        return None, None
    tz = anker.tzinfo
    van = datetime.combine(anker.date(), dtime(van_min // 60, van_min % 60), tzinfo=tz)
    tot = datetime.combine(anker.date(), dtime(tot_min // 60, tot_min % 60), tzinfo=tz)
    return van, tot


def _dagboost_in_venster(
    state: dict, now: datetime, van_hhmm: str, tot_hhmm: str
) -> bool:
    """Zit er binnen het rollende 24-uursvenster al een boost die binnen het
    dagvenster [van, tot] (beide grenzen inbegrepen) startte? Dan is het
    verplichte dagblok van deze cyclus al gedekt en wordt er niet opnieuw
    één gepland. Bij een onbruikbaar venster: altijd 'gedekt' (nooit forceren)."""
    try:
        van_min = _hhmm_minuten(van_hhmm)
        tot_min = _hhmm_minuten(tot_hhmm)
    except (TypeError, ValueError):
        return True
    cutoff = now - timedelta(hours=ROLLING_WINDOW_HOURS)
    for s in _boost_starts(state):
        if s < cutoff:
            continue
        minuten = s.hour * 60 + s.minute
        if van_min <= tot_min:
            in_venster = van_min <= minuten <= tot_min
        else:  # rond middernacht, bijv. 22:00-02:00
            in_venster = minuten >= van_min or minuten <= tot_min
        if in_venster:
            return True
    return False


def find_boost_block(
    rows: list,
    granularity_min: int,
    block_hours: int,
    now: datetime,
    earliest_start: datetime,
    end_window: Optional[tuple] = None,
    start_before: Optional[datetime] = None,
):
    """Het goedkoopste boost-blok, met terugval op `start_before`.

    Als de start-grens geen enkele kandidaat oplevert (bijv. als `earliest_start`
    kort vóór het ochtend-venstereinde ligt, waardoor geen enkel blok meer
    én vóór de grens start én in het venster eindigt), dan wordt eerst één
    nacht verder geprobeerd en valt de grens uiteindelijk helemaal weg: liever
    een iets te laat blok dan helemaal geen boost.
    """
    attempts = [start_before]
    if start_before is not None:
        attempts += [start_before + timedelta(days=1), None]
    for before in attempts:
        blocks = find_cheapest_blocks(
            rows,
            granularity_min=granularity_min,
            block_hours=block_hours,
            only_future=True,
            top_n=1,
            now=now,
            earliest_start=earliest_start,
            end_window=end_window,
            start_before=before,
        )
        if blocks:
            return blocks[0]
    return None


def _next_boost_block(
    advice: dict, boost_cfg: dict, state: dict, now: datetime
):
    """Het goedkoopste beschikbare blok voor de VOLGENDE boost.

    Wanneer er al een boost verstuurd is, mag het volgende blok pas starten
    ná `min_gap_hours` uur ná het einde van die boost — anders zou de tweede
    opwarmperiode vlak na de eerste liggen (het advies kiest 'slim' hetzelfde
    of aangrenzende goedkope moment). Zonder eerdere boost is het gewoon het
    goedkoopste blok vanaf nu.

    Is dit het LAATSTE blok van het 24-uursvenster (het blok dat de
    daglimiet op `boosts_per_day` brengt), dan moet het bovendien eindigen
    tussen `last_block_end_from` en `last_block_end_to` (default 19:00 en
    08:00 de volgende ochtend): de laatste opwarmperiode eindigt dan nooit
    midden op de dag.

    Tenzij dit het verplichte DAGBLOK is: zit er al een boost in het
    24-uursvenster maar géén daarvan binnen `afternoon_from`–`afternoon_to`
    (default 12:00–23:00), dan wordt juist dáár gepland — één van de twee
    opwarmmomenten per dag hoort in de middag/avond. Het eind-venster en de
    start-grens hieronder gelden daar niet (dat venster begrenst het blok
    immers al tot dezelfde dag).

    Elke boost start bovendien vóór het ochtend-`last_block_end_to` (ook het
    vrij gekozen eerste blok). Zonder die grens glijdt het blok door naar het
    goedkoopste uur van de hele horizon zodra de prijzen van een nieuwe dag
    verschijnen — en dat gebeurt juist ook vlak vóór de blokstart: als de
    vorige boost net uit het 24-uursvenster is gevallen (recent=0, dus geen
    eind-venster meer) en de LEAD-wake opnieuw optimaliseert.
    """
    gap_hours = float(boost_cfg.get("min_gap_hours", DEFAULT_BOOST_GAP_HOURS))
    boosts_per_day = int(boost_cfg.get("boosts_per_day", DEFAULT_BOOSTS_PER_DAY))
    dhw = advice.get("dhw") or {}
    rows = dhw.get("rows") or []
    granularity_min = int((advice.get("prices") or {}).get("granularity_min", 15))
    # SWW-boosts hebben hun eigen bloklengte (default 1 u); het langere blok
    # voor de ruimteverwarming is optimization.block_hours (default 3).
    block_hours = int(boost_cfg.get("block_hours", DEFAULT_BOOST_BLOCK_HOURS))
    afternoon_from = boost_cfg.get("afternoon_from", AFTERNOON_FROM)
    afternoon_to = boost_cfg.get("afternoon_to", AFTERNOON_TO)

    earliest_start = now
    last_end = state.get("last_sent_end")
    if last_end:
        after_prev = datetime.fromisoformat(last_end) + timedelta(hours=gap_hours)
        if now < after_prev:
            earliest_start = after_prev

    # Laatste blok van het venster? (per_day=1 heeft geen 'eerste+laatste'
    # onderscheid -> dan blijft het enkele blok vrij.)
    recent = _count_boosts_last_24h(state, now)
    is_last = False
    if boosts_per_day >= 2:
        is_last = (recent + 1) == boosts_per_day

    end_to = boost_cfg.get("last_block_end_to", LAST_BLOCK_END_TO)
    end_window = None
    if is_last:
        end_window = (boost_cfg.get("last_block_end_from", LAST_BLOCK_END_FROM), end_to)
    # De start-grens geldt voor ÉLK boost-blok, ook het 'vrije' eerste: het
    # mag nooit later starten dan het ochtend-`last_block_end_to`. Want zodra
    # de vorige boost uit het 24-uursvenster is gevallen (recent=0) verdwijnt
    # het eind-venster — en dat gebeurt juist vlak voor de blokstart, als de
    # LEAD-wake opnieuw optimaliseert. Zonder deze grens schuift het blok dan
    # alsnog door naar het goedkoopste uur van de hele horizon.
    start_before = next_deadline(earliest_start, end_to)

    # Verplicht dagblok (afternoon_from–afternoon_to, default 12:00–23:00):
    # er zit wél al een boost in het venster, maar géén binnen dat dagvenster.
    # Dan wordt dit blok dáár gepland — pas ná een eerste boost, zodat het
    # allereerste blok van een cyclus (en boosts_per_day=1) vrij blijft, en
    # alleen als het venster op de dag van `earliest_start` nog past (aan het
    # eind van de avond schuift de verplichting gewoon door naar morgen).
    # Geen eind-venster en géén ochtend-start-grens: het dagvenster is zelf
    # al de grens (het loopt immers nooit verder dan dezelfde dag).
    if (
        boosts_per_day >= 2
        and recent >= 1
        and not _dagboost_in_venster(state, now, afternoon_from, afternoon_to)
    ):
        van, tot = _dagvenster(earliest_start, afternoon_from, afternoon_to)
        if van is not None:
            dag_start = max(earliest_start, van)
            if dag_start <= tot:
                blocks = find_cheapest_blocks(
                    rows,
                    granularity_min=granularity_min,
                    block_hours=block_hours,
                    only_future=True,
                    top_n=1,
                    now=now,
                    earliest_start=dag_start,
                    start_before=tot,
                )
                if blocks:
                    blocks[0]["dagblok"] = True
                    return blocks[0]
                # geen data tot in het venster -> gewoon de gewone weg

    best = find_boost_block(
        rows,
        granularity_min,
        block_hours,
        now,
        earliest_start,
        end_window=end_window,
        start_before=start_before,
    )
    if best:
        best["dagblok"] = bool(best.get("dagblok"))
    return best


def _boost_pending_actions(state: dict, start: datetime, boosts_per_day: int) -> str:
    """Nogmaals de beslisregels tegenover de statusfile, nú voordat er een
    boost voor een gepland blok verstuurd wordt.

    Geeft 'send' | 'already_sent' | 'boost_limit'. Dit gebeurt nadrukkelijk
    ZONDER het advies opnieuw te berekenen: een herberekening zou het
    (inmiddels gestarte) blok door `only_future` verliezen en de boost
    eindeloos doorschuiven.
    """
    if state.get("last_sent_start") == start.isoformat():
        return "already_sent"
    if _count_boosts_last_24h(state, start) >= boosts_per_day:
        return "boost_limit"
    return "send"


def execute(out: dict, mqtt_cfg: dict, dry_run: bool, force: bool) -> None:
    """Voer de beslissing uit: publiceer evt. en schrijf de statusfile."""
    if out["status"] not in ("send", "already_sent", "reset"):
        return
    if out["status"] == "already_sent" and not force:
        return
    if dry_run:
        out["mqtt_published"] = False
        out["mqtt_detail"] = "dry-run: niet gepubliceerd"
        return
    ok, detail = mqtt_out.publish_command(
        mqtt_cfg,
        out["topic"],
        out["payload"],
        qos=out.get("qos"),
        retain=out.get("retain"),
    )
    out["mqtt_published"] = ok
    out["mqtt_detail"] = detail
    if not ok:
        print(f"FOUT: MQTT-publicatie mislukt: {detail}", file=sys.stderr)
    if ok:
        state = load_state()
        if out["status"] == "reset":
            state["last_reset_start"] = out["block"]["start"]
            state["reset_at"] = out["now"]
        else:
            state["last_sent_start"] = out["block"]["start"]
            state["last_sent_end"] = out["block"]["end"]
            _record_boost(state, datetime.fromisoformat(out["block"]["start"]))
            state["sent_at"] = out["now"]
            if out["block"].get("mean_cop") is not None:
                state["sent_mean_cop"] = out["block"]["mean_cop"]
                state["sent_mean_corrected"] = out["block"]["mean_corrected_eur_per_kwh_heat"]
        save_state(state)


WATCH_START_MARGIN_SECONDS = 2.0   # wek ~2s NÁ de blokstart (gegarandeerd ≥ start)
LEAD_SECONDS = 120.0               # wek deze tijd VÓÓR de blokstart: dan kan de
                                   # (eventueel trage) herberekening in alle rust
                                   # klaar zijn, waarna in kleine stapjes tot de
                                   # start wordt gewacht (zie await_block_start)
RETRY_SECONDS = 30.0               # tussen pogingen als de broker niet bereikbaar is
MIN_SLEEP = 5.0                    # ondergrens slaap (voorkomt busy-loop)
PRICE_REFRESH_DEFAULT = "13:30"    # dagelijks moment waarop dag-ahead-prijzen binnenkomen
LATE_PUBCHECK_MIN = 45.0           # hercontrole ná de prijs-update als de nieuwe dag
                                   # ontbrak, zodat een late publicatie binnen het uur
                                   # wordt verwerkt (--price-recheck-min, default 45)
LATE_PUBCHECK_WINDOW_H = 4.0       # deze hercontroles alleen binnen dit venster ná de
                                   # refresh-tijd (niet 's nachts op een blok blijven waken)
RETRY_INTERVAL_DEFAULT = 1800.0    # fallback (30 min) zolang er nog geen blok bekend is


def _log(*parts) -> None:
    ts = datetime.now().strftime("%d-%m %H:%M:%S")
    print(f"[{ts}] " + " ".join(str(p) for p in parts), flush=True)


def _wait_log(out: dict, wake: datetime, now: datetime, recheck_min: float) -> str:
    """Watcher-melding bij status 'wait'.

    De hint over een late publicatie wordt alleen getoond als die hercontrole
    ook écht de eerstvolgende wake is (blokstart/reset gaan vóór) — anders
    belooft de melding een 'hercontrole later' die er nooit komt (de watcher
    wordt dan eerst wakker voor het blokeinde of de blokstart).
    """
    wait_txt = f"blokstart over {out['wait_minutes']:g} min"
    if out.get("next_day_missing") and wake == now + timedelta(minutes=recheck_min):
        return (
            wait_txt
            + " — prijzen van de nieuwe dag nog niet zichtbaar (late publicatie), "
            f"herberekening over {recheck_min:g} min"
        )
    return wait_txt


def next_price_refresh(now: datetime, tz: ZoneInfo, hhmm: str) -> datetime:
    """Eerstvolgende dagelijkse dag-ahead-publicatie (default vandaag 13:30)."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    candidate = datetime(now.year, now.month, now.day, hh, mm, tzinfo=tz)
    if now >= candidate:
        candidate += timedelta(days=1)
    return candidate


def next_wake_time(
    out: dict,
    now: datetime,
    tz: ZoneInfo,
    refresh_hhmm: str,
    recheck_min: float = LATE_PUBCHECK_MIN,
) -> datetime:
    """Het eerstvolgende moment dat iets nuttigs kan veranderen:
    - een openstaande reset: precies het einde van dat blok (hoogste prioriteit:
      zonder deze wake slaapt de watcher er met het 'volgende blok' voorbij);
    - blok komt eraan: min(blokstart - LEAD, eerstvolgende prijs-update ~13:30);
    - late dag-ahead-publicatie (de nieuwe dag ontbrak): ná `recheck_min` nog een
      hercontrole — alleen kort ná de refresh-tijd van vandaag en zolang het blok
      nog niet in de LEAD-nadering zit (anders kan de extra wake de geplande
      trigger verstoren);
    - boost is al verstuurd: het blok EINDE (dan volgt de reset naar default);
    - anders (nog geen blok/vertraagde prijzen): over RETRY_INTERVAL.
    """
    status = out.get("status")
    refresh = next_price_refresh(now, tz, refresh_hhmm)
    if out.get("pending_reset_at"):
        # Er is een boost verstuurd waarvan de reset nog niet gedaan is: het
        # einde van dát blok is het moment dat telt, vóór elk ander blok.
        return datetime.fromisoformat(out["pending_reset_at"])
    if status == "wait" and out.get("block"):
        start = datetime.fromisoformat(out["block"]["start"])
        lead_moment = start - timedelta(seconds=LEAD_SECONDS)
        if out.get("next_day_missing"):
            hh, mm = (int(x) for x in refresh_hhmm.split(":"))
            today_refresh = datetime(now.year, now.month, now.day, hh, mm, tzinfo=tz)
            window_end = today_refresh + timedelta(hours=LATE_PUBCHECK_WINDOW_H)
            if (
                now >= today_refresh
                and now < window_end
                and lead_moment > now + timedelta(minutes=recheck_min)
            ):
                return now + timedelta(minutes=recheck_min)
        # Vóór de start wakker worden (LEAD): een herberekening ná de start
        # zou het blok door `only_future` verliezen en naar het volgende blok
        # glijden (de oude 'start + 2s'-marge was te krap voor de trage
        # data-ophaal op de Pi).
        return min(lead_moment, refresh)
    if status == "already_sent" and out.get("block"):
        end = datetime.fromisoformat(out["block"]["end"])
        return end
    if status == "boost_limit":
        # 24-uurslimiet vol: wakker worden zodra het oudste blok uit het
        # rollende venster valt (dan mág er weer een boost).
        return datetime.fromisoformat(out["limit_wake_at"])
    return now + timedelta(seconds=RETRY_INTERVAL_DEFAULT)


def await_block_start(out: dict, cfg: dict, tz: ZoneInfo, dry_run: bool) -> bool:
    """Wacht in kleine stapjes tot de blokstart en verstuur dan.

    Bewust géén herberekening op het vuurmoment: zodra de start gepasseerd
    is, sluit `only_future` het net gestarte blok uit en zou een verse
    `decide()` dat blok verliezen en naar het volgende doorschuiven (zie
    26-09: 'blokstart bereikt, niets te versturen (status: wait)' na élke
    start). Dag-ahead-prijzen zijn stabiel, dus het geplande blok is geldig.

    Wel wordt vlak vóór het versturen tegen de statusfile gecontroleerd:
    is het blok al verstuurd, de daglimiet bereikt, of staat er een reset
    van een eerder blok klaar — dan gaat die respectievelijk niét of éérst.
    """
    start = datetime.fromisoformat(out["block"]["start"])
    while True:
        rest = (start - datetime.now(tz)).total_seconds()
        if rest <= 0:
            now = datetime.now(tz)
            state = load_state()
            # 1) reset van een eerder blok nog open? Die eerst (defensief: bij
            #    normaal verloop is de wake daarop al eerder uitgeraakt).
            sent_start = state.get("last_sent_start")
            sent_end = state.get("last_sent_end")
            if (
                sent_start
                and sent_end
                and state.get("last_reset_start") != sent_start
                and now >= datetime.fromisoformat(sent_end)
            ):
                fresh = decide(cfg, now)
                if fresh["status"] == "reset":
                    _log("blokstart bereikt — reset van het vorige blok staat "
                         "klaar, stuur de reset")
                    return send_with_retry(
                        fresh,
                        cfg.get("mqtt") or {},
                        tz,
                        dry_run,
                        state_key="last_reset_start",
                        at_key="reset_at",
                        label="reset naar default",
                    )
                _log("FOUT: reset staat open maar decide gaf", fresh["status"])
                return True
            # 2) al verstuurd / daglimiet bereikt? Dan niet nóg een boost.
            guard = _boost_pending_actions(
                state, start, out.get("boosts_per_day", DEFAULT_BOOSTS_PER_DAY)
            )
            if guard != "send":
                _log("blokstart bereikt, niets te versturen (", guard, ")")
                return True
            _log("blokstart bereikt — verstuur het boost-commando")
            return send_with_retry(out, cfg.get("mqtt") or {}, tz, dry_run)
        time.sleep(min(30.0, max(0.5, rest)))


def send_with_retry(
    out: dict,
    mqtt_cfg: dict,
    tz,
    dry_run: bool,
    state_key: str = "last_sent_start",
    at_key: str = "sent_at",
    label: str = "boost-commando",
) -> bool:
    """Publiceer, met retry zolang dat nodig is.

    Alleen het boost-commando heeft een uiterste verstuurtijdstip (het einde
    van het trigger-venster = blokeinde). Een reset terug naar de default
    moet juist áltijd blijven proberen (elke RETRY_SECONDS) tot de broker de
    ontvangst bevestigt: de boiler mag niet op de dure boost-temperatuur
    blijven hangen omdat de broker één keer even onbereikbaar was.
    """
    if dry_run:
        return True
    deadline = None
    if state_key == "last_sent_start" and out.get("block"):
        deadline = datetime.fromisoformat(out["block"]["end"])
    while True:
        ok, detail = mqtt_out.publish_command(
            mqtt_cfg,
            out["topic"],
            out["payload"],
            qos=out.get("qos"),
            retain=out.get("retain"),
        )
        out["mqtt_published"] = ok
        out["mqtt_detail"] = detail
        if ok:
            state = load_state()
            state[state_key] = out["block"]["start"]
            # Werkelijke publicatiemoment, niet het (mogelijk minuten oudere)
            # beslissingstijdstip uit `out["now"]` (de watcher wekt LEAD
            # seconden vóór de start en publiceert pas ná de start).
            state[at_key] = datetime.now(tz).isoformat()
            if state_key == "last_sent_start":
                state["last_sent_end"] = out["block"]["end"]
                _record_boost(state, datetime.fromisoformat(out["block"]["start"]))
                if out["block"].get("mean_cop") is not None:
                    state["sent_mean_cop"] = out["block"]["mean_cop"]
                    state["sent_mean_corrected"] = out["block"]["mean_corrected_eur_per_kwh_heat"]
            save_state(state)
            _log("VERSTUURD →", out["topic"],
                 json.dumps(out["payload"], ensure_ascii=False),
                 f"({label}, {detail})")
            return True
        _log("FOUT: publish mislukt —", detail,
             "— probeer opnieuw over", f"{RETRY_SECONDS:g}s")
        now = datetime.now(tz)
        if deadline and now >= deadline:
            _log("FOUT: kon niet versturen binnen het trigger-venster")
            return False
        time.sleep(RETRY_SECONDS)


def watch(
    cfg: dict,
    tz: ZoneInfo,
    refresh_hhmm: str,
    retry_interval: float,
    dry_run: bool,
    recheck_min: float = LATE_PUBCHECK_MIN,
) -> int:
    """Blijf draaien: slaap tot het relevante moment en verstuur dan precies.

    Samen met de status uit `decide` is elke wake een van vier dingen:
    - blokstart bereikt  -> verstuur het boost-commando;
    - blokeinde bereikt (reset staat open) -> zet terug naar de default;
    - dagelijkse prijs-update ~13:30 -> herbereken het advies (en, als de
      nieuwe dag nog ontbrak, `recheck_min` later nogmaals);
    - (alleen als er nog geen blok is) elke `retry_interval` seconden.
    """
    _log("SWW-boost watchdog gestart — herberekent alleen bij blokstart of",
         f"dagelijkse prijs-update {refresh_hhmm} (fallback elke {retry_interval/60:.0f} min)")
    seen_price_warnings: Optional[list] = None
    while True:
        try:
            now = datetime.now(tz)
            out = decide(cfg, now)
            status = out["status"]

            # Dagen die deze ronde niet opgehaald konden worden (ENTSO-E-CDN
            # flak of een dag die nog niet gepubliceerd is): het advies is
            # bruikbaar, maar met een kortere horizon. Eén keer per wake
            # melden, niet hinderlijk herhalen.
            warnings = out.get("price_warnings")
            if warnings and warnings != seen_price_warnings:
                _log("LET OP: prijzen ontbreken voor", "; ".join(warnings))
                seen_price_warnings = list(warnings)

            if status == "send":
                _log("blokstart bereikt — verstuur het boost-commando")
                if not send_with_retry(out, cfg.get("mqtt") or {}, tz, dry_run):
                    return 1
                continue  # statusfile voorkomt dubbele berichten; op naar het volgende blok

            if status == "reset":
                _log("blokeinde bereikt — zet gewenste temperatuur terug naar default")
                if not send_with_retry(
                    out,
                    cfg.get("mqtt") or {},
                    tz,
                    dry_run,
                    state_key="last_reset_start",
                    at_key="reset_at",
                    label="reset naar default",
                ):
                    return 1
                continue

            if status in ("no_dhw", "disabled"):
                _log("status:", status, "— niets te doen, stop")
                return 0

            if status == "already_sent":
                _log("status: al verstuurd voor dit blok")
            elif status == "boost_limit":
                _log("24-uurslimiet bereikt — volgende boost mogelijk om",
                     out.get("limit_wake_at", "?"))
            else:
                wake = next_wake_time(out, now, tz, refresh_hhmm, recheck_min)
                if status == "wait":
                    _log(_wait_log(out, wake, now, recheck_min))
                else:
                    _log(f"status: {status}")

            # Blok komt binnen LEAD-nadering: niet meer alleen slapen, maar in
            # kleine stapjes naar de start wachten en dan versturen zonder het
            # blok opnieuw te berekenen (anders glijdt het steeds door).
            if status == "wait" and out.get("block"):
                start = datetime.fromisoformat(out["block"]["start"])
                rest = (start - now).total_seconds()
                if rest <= LEAD_SECONDS:
                    rest_s = max(0.0, rest)
                    _log(f"blokstart over {rest_s/60:.1f} min — wacht in kleine stapjes")
                    if not await_block_start(out, cfg, tz, dry_run):
                        return 1
                    continue

            wake = next_wake_time(out, now, tz, refresh_hhmm, recheck_min)
            delay = max(MIN_SLEEP, (wake - now).total_seconds())
            _log("slaapt tot", wake.strftime("%a %d-%m %H:%M:%S"),
                 f"(+{delay/3600:.1f}u)" if delay >= 3600 else f"(+{delay/60:.0f}min)")
            time.sleep(delay)
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
    if "boosts_per_day" in out:
        lines.append(
            f"Plan    : {out['boosts_per_day']}x per 24 u, min. {out['min_gap_hours']:g} u "
            f"tussen de blokken; laatste blok eindigt "
            f"{out['last_block_end_from']}–{out['last_block_end_to']}; dagvenster "
            f"{out.get('afternoon_from', AFTERNOON_FROM)}–"
            f"{out.get('afternoon_to', AFTERNOON_TO)} — "
            f"{out.get('boosts_recent', 0)}/{out['boosts_per_day']} "
            "in de afgelopen 24 u"
        )
    if "block" in out:
        b = out["block"]
        start_txt = app.fmt_dt(datetime.fromisoformat(b["start"]))
        end_txt = app.fmt_dt(datetime.fromisoformat(b["end"]))
        dag_txt = " [dagvenster]" if b.get("dagblok") else ""
        if b.get("mean_cop") is not None and b.get("mean_corrected_eur_per_kwh_heat") is not None:
            lines.append(
                f"Beste SWW-blok : {start_txt} – {end_txt} "
                f"(COP {app.nl(b['mean_cop'], 2)}, "
                f"{app.nl(b['mean_corrected_eur_per_kwh_heat'], 4)} €/kWh warmte)"
                f"{dag_txt}"
            )
        else:
            lines.append(f"Beste SWW-blok : {start_txt} – {end_txt}{dag_txt}")

    status = out["status"]
    dry = out.get("mqtt_detail", "").startswith("dry-run")
    if status == "send":
        if dry:
            lines.append("Status  : dry-run — niets gepubliceerd")
        elif out.get("mqtt_published"):
            lines.append(
                f"Status  : VERSTUURD → {out['topic']} {json.dumps(out['payload'], ensure_ascii=False)}"
            )
        else:
            lines.append(
                f"Status  : FOUT — niet gepubliceerd ({out.get('mqtt_detail', 'onbekend')})"
            )
    elif status == "already_sent":
        lines.append("Status  : al verstuurd voor dit blok (geen dubbele berichten)")
    elif status == "boost_limit":
        n = out.get("boosts_per_day", DEFAULT_BOOSTS_PER_DAY)
        t = out.get("boosts_recent", n)
        when = out.get("limit_wake_at")
        msg = f"Status  : 24-uurslimiet bereikt ({t}/{n} in de afgelopen 24 u)"
        if when:
            msg += f" — volgende boost mogelijk om {app.fmt_dt(datetime.fromisoformat(when))}"
        lines.append(msg)
    elif status == "reset":
        if dry:
            lines.append("Status  : dry-run — reset niet gepubliceerd")
        elif out.get("mqtt_published"):
            lines.append(
                f"Status  : TERUGGEZET → {out['topic']} {json.dumps(out['payload'], ensure_ascii=False)} (blok voorbij)"
            )
        else:
            lines.append(
                f"Status  : FOUT — niet gepubliceerd ({out.get('mqtt_detail', 'onbekend')})"
            )
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
        help="blijf draaien en verstuur bij de blokstart",
    )
    ap.add_argument(
        "--price-refresh-time",
        default=PRICE_REFRESH_DEFAULT,
        help=f"dagelijks moment om het advies te herberekenen (default {PRICE_REFRESH_DEFAULT})",
    )
    ap.add_argument(
        "--retry-interval",
        type=float,
        default=RETRY_INTERVAL_DEFAULT,
        help="seconden tussen herberekeningen zolang er nog geen blok bekend is (default 1800)",
    )
    ap.add_argument(
        "--price-recheck-min",
        type=float,
        default=LATE_PUBCHECK_MIN,
        help="minuten ná de prijs-update nog een hercontrole als de nieuwe dag "
             f"ontbrak (late publicatie, default {LATE_PUBCHECK_MIN:g})",
    )
    args = ap.parse_args(argv)

    try:
        cfg = app.load_config(args.config)
        tz = ZoneInfo(cfg["location"]["timezone"])
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz)

        if args.watch and not args.now:
            return watch(
                cfg, tz, args.price_refresh_time, args.retry_interval, args.dry_run,
                args.price_recheck_min,
            )

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