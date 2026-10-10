"""Dynamische stroomprijzen via de ENTSO-E Transparency Platform API.

Endpoint (nieuwe REST-API): https://web-api.tp.entsoe.eu/api
Authenticatie: query-parameter `securityToken` met je persoonlijke API-key.

Gebruikte documentsoort: A44 (day-ahead prijzen), proces Day Ahead (A01).
Voor Nederland (biedingszone 10YNL----------L) levert deze API de day-ahead
prijzen met 15-minuten-resolutie (PT15M, 96 punten/dag).

Prijzen worden geleverd in EUR/MWh (grootschalig, excl. btw) en hier
omgerekend naar EUR/kWh. Een constante btw/opslag verandert overigens niets
aan wélk blok het goedkoopst is; een *vaste* belasting per kWh (bijv. de
energiebelasting) wél — die kun je via `price_adjustments` in config.json
meenemen.

Returns (zelfde contract als energyzero.fetch_prices):
    {
      "slots": [(datetime_lokaal, prijs_eur_per_kwh), ...],   # oplopend
      "granularity_min": 15 | 30 | 60,
      "source": "ENTSO-E day-ahead ...",
    }
"""

from __future__ import annotations

import json
import os
import re
import time as _time
import urllib.error  # noqa: F401 — gebruikt voor HTTPError/URLError hieronder
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

API_URL = "https://web-api.tp.entsoe.eu/api"
DEFAULT_DOMAIN = "10YNL----------L"  # Nederland
DEFAULT_CACHE_TTL = 3600  # dag-ahead prijzen veranderen hooguit 1×/dag
REQUEST_TIMEOUT = 30  # seconden per HTTP-verzoek (ENTSO-E kan traag zijn)
# Wachttijd tussen retries van één dag (4 pogingen totaal = max ~50 s extra).
# Kort genoeg om binnen het trigger-venster van 45 min. te blijven.
RETRY_DELAYS = (5, 15, 30)


class _TransientDayError(RuntimeError):
    """Tijdelijke ophaalfout voor één dag (HTTP 5xx/408/429, timeout,
    verbindingsfout, half afgegeven XML). Waarschijnlijk vanzelf over; het
    deugt dus om opnieuw te proberen of op een oudere cache te terugvallen."""


class PricesNotAvailableError(RuntimeError):
    """Voor *geen enkele* gevraagde dag was prijsdata beschikbaar.

    Dit is normaal vóór het dagelijkse publicatiemoment van day-ahead-
    prijzen (de dag staat dan nog niet in de API). De watcher kan dit type
    apart herkennen en zijn herpoging afstemmen op dat publicatiemoment
    (i.p.v. elke minuut opnieuw te vragen)."""


def _is_transient_http(code: int) -> bool:
    """Is deze HTTP-status tijdelijk (dus waard om te herhalen)?

    De Transparency Platform zit achter een CDN dat ook niet-standaard
    statussen teruggeeft: 527 (Railgun) en 599 (non-standard connection
    reset) zijn allebei CDN-/netwerkproblemen, geen probleem met de key.
    Daarom: alles >= 500, plus de 4xx die expliciet om "later opnieuw" vragen.
    401/403 zijn géén tijdelijk — dat is een slechte api_key.
    """
    return code >= 500 or code in (408, 425, 429)


def _default_cache_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".cache", "remko-wkf70")


def _day_cache_path(cache_dir: str, day: date, in_domain: str) -> str:
    safe_domain = in_domain.replace("/", "_")
    return os.path.join(cache_dir, f"entsoe_{day:%Y%m%d}_{safe_domain}.json")


def _load_day_cache(
    path: str, ttl_seconds: int, allow_stale: bool = False
) -> Optional[List[Tuple[datetime, float]]]:
    """Lees een eerder opgehaalde dag uit de cache (None als verlopen/ongeldig).

    Met `allow_stale=True` wordt de leeftijd genegeerd: dat is de terugval
    wanneer de API net even onbereikbaar is. Dag-ahead prijzen veranderen
    hooguit één keer per dag, dus een paar uur oud is ruim bruikbaar en
    oneindig wachten op een herstelde API is dat niet.
    """
    try:
        if not os.path.exists(path):
            return None
        if not allow_stale and _time.time() - os.path.getmtime(path) >= ttl_seconds:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return [(datetime.fromisoformat(iso), float(price)) for iso, price in raw]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_day_cache(path: str, slots: List[Tuple[datetime, float]]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([(dt.isoformat(), price) for dt, price in slots], fh)
    except OSError:
        pass  # cache is een optimalisatie; een volle schijf mag niet crashen

_ACK_TAG = "Acknowledgement_MarketDocument"


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _find_child(el: ET.Element, name: str) -> Optional[ET.Element]:
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_duration_parts(resolution: str) -> int:
    """'PT15M'/'PT30M'/'PT60M'/'PT1H' -> aantal minuten."""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", resolution.strip().upper())
    if not m:
        raise ValueError(f"onbekende resolutie: {resolution!r}")
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    return hours * 60 + minutes


def _request_day(
    api_key: str,
    day: date,
    in_domain: str,
    out_domain: str,
    expected_start_utc: datetime,
    cache_dir: Optional[str] = None,
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL,
) -> List[Tuple[datetime, float]]:
    """Haal de day-ahead prijzen van één kalenderdag op (dt_utc, EUR/kWh).

    Met `cache_dir` wordt de opgehaalde dag tussen cachet (dag-ahead prijzen
    veranderen hooguit één keer per dag). Zo blijft elke wake van de watcher
    vrijwel instant, ook op een trage/overbelaste DNS-server.
    """
    cache_path = _day_cache_path(cache_dir, day, in_domain) if cache_dir else None
    if cache_path:
        cached = _load_day_cache(cache_path, cache_ttl_seconds)
        if cached is not None:
            return cached

    # Tijdelijke fouten (5xx/599/527, timeout) herhalen met korte pauzes; pas
    # als het nóg misgaat terugvallen op een oudere cachekopie van dezelfde dag.
    # De eerste poging gaat direct (geen pauze vóór een request die nog niet
    # mislukt is); de wachttijd zit steeds ná de mislukte poging.
    result: Optional[List[Tuple[datetime, float]]] = None
    last_error: Optional[Exception] = None
    for delay in (None, *RETRY_DELAYS):
        if delay:
            _time.sleep(delay)
        try:
            result = _fetch_day_once(api_key, day, in_domain, out_domain, expected_start_utc)
            break
        except _TransientDayError as exc:
            last_error = exc
    else:
        if cache_path:
            stale = _load_day_cache(cache_path, cache_ttl_seconds, allow_stale=True)
            if stale is not None:
                return stale
        raise last_error  # type: ignore[misc]  # altijd gezet in de else-tak

    if cache_path and result:
        # alleen niet-lege dagen cachen (een "nog niet gepubliceerde" dag
        # mag na de TTL gewoon opnieuw worden gevraagd)
        _save_day_cache(cache_path, result)
    return result


def _fetch_day_once(
    api_key: str,
    day: date,
    in_domain: str,
    out_domain: str,
    expected_start_utc: datetime,
) -> List[Tuple[datetime, float]]:
    """Eén HTTP-poging voor één dag: netwerkfouten en serverfouten worden
    als `_TransientDayError` gemeld (→ opnieuw proberen), een echte
    client-fout (401/403) meteen als `RuntimeError` (key/config-probleem)."""
    start_str = f"{day:%Y%m%d}0000"
    end_str = f"{day:%Y%m%d}2359"
    params = {
        "securityToken": api_key,
        "documentType": "A44",
        "in_Domain": in_domain,
        "out_Domain": out_domain,
        "periodStart": start_str,
        "periodEnd": end_str,
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/xml"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:  # noqa: S310
            xml_text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if _is_transient_http(exc.code):
            raise _TransientDayError(
                f"ENTSO-E HTTP-fout {exc.code} voor dag {day} (tijdelijk)"
            ) from exc
        raise RuntimeError(
            f"ENTSO-E HTTP-fout {exc.code} voor dag {day} — "
            "geldige key? te veel requests? (zie https://transparency.entsoe.eu)"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # read-timeout, DNS-flap, verbinding geweigerd: allemaal tijdelijk
        raise _TransientDayError(f"ENTSO-E netwerkfout voor dag {day}: {exc}") from exc

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise _TransientDayError(
            f"ENTSO-E gaf ongeldige XML terug voor dag {day}"
        ) from exc

    if _local(root.tag) == _ACK_TAG:
        # geen data (of authenticatiefout) voor deze dag
        code = _find_text(root, "code")
        text = _find_text(root, "text")
        if code in ("999",) and text and "authentic" in text.lower():
            raise RuntimeError(f"ENTSO-E authenticatie geweigerd: {text} (code {code})")
        return []  # dag (nog) niet gepubliceerd

    unit = _find_text(root, "price_Measure_Unit.name")
    conversion = 1.0 / 1000.0 if (unit or "MWH").upper() in ("MWH", "MWH1") else 1.0

    result: List[Tuple[datetime, float]] = []
    for ts in root.iter():
        if _local(ts.tag) != "TimeSeries":
            continue
        period = _find_child(ts, "Period")
        if period is None:
            continue
        interval = _find_child(period, "timeInterval")
        start = _parse_iso(_find_text(interval, "start"))
        if start != expected_start_utc:
            continue  # serie van een naastgelegen dag (API-lek) overslaan
        res = _find_text(period, "resolution") or "PT15M"
        slot_min = _parse_duration_parts(res)
        for point in period:
            if _local(point.tag) != "Point":
                continue
            position = _find_text(point, "position")
            amount = _find_text(point, "price.amount")
            if position is None or amount is None:
                continue  # punt zonder positie of prijs
            price_eur_kwh = float(amount) * conversion
            slot_utc = start + timedelta(minutes=(int(position) - 1) * slot_min)
            result.append((slot_utc, price_eur_kwh))
    return result


def _find_text(el: Optional[ET.Element], name: str) -> Optional[str]:
    if el is None:
        return None
    child = _find_child(el, name)
    if child is None:
        return None
    return (child.text or "").strip() or None


def fetch_prices(
    api_key: str,
    tz: str = "Europe/Amsterdam",
    days_ahead: int = 3,
    in_domain: str = DEFAULT_DOMAIN,
    out_domain: str = DEFAULT_DOMAIN,
    cache_dir: Optional[str] = None,
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL,
) -> Dict:
    """Haal day-ahead prijzen op voor vandaag .. vandaag+days_ahead-1.

    Antwoorden worden per dag ge-cachet op schijf (standaard 1 uur) zodat een
    continu draaiende watcher niet bij elke wake de API opnieuw hoeft te
    bevragen.
    """
    local_tz = ZoneInfo(tz)
    now_local = datetime.now(local_tz).date()
    cache_dir = cache_dir or _default_cache_dir()
    slots: List[Tuple[datetime, float]] = []
    warnings: List[str] = []
    for offset in range(days_ahead):
        day = now_local + timedelta(days=offset)
        expected_start_utc = datetime.combine(
            day, time.min, tzinfo=local_tz
        ).astimezone(timezone.utc)
        try:
            day_slots = _request_day(
                api_key,
                day,
                in_domain,
                out_domain,
                expected_start_utc,
                cache_dir=cache_dir,
                cache_ttl_seconds=cache_ttl_seconds,
            )
        except RuntimeError as exc:
            # Eén mislukte dag (CDN-storing, dag nog niet gepubliceerd, ...) mag
            # de andere dagen niet blokkeren: één bruikbare dag is al genoeg om te
            # plannen, en de ontbrekende dag komt bij de volgende wake weer mee.
            warnings.append(f"{day}: {exc}")
            continue
        slots.extend(day_slots)

    # sorteeren + dubbelen eruit, en omzetten naar lokale tijd
    slots = sorted(set(slots), key=lambda s: s[0])
    slots = [(dt.astimezone(local_tz), price) for dt, price in slots]
    if not slots:
        detail = f" — {warnings[-1]}" if warnings else ""
        raise PricesNotAvailableError(
            "ENTSO-E leverde geen day-ahead prijzen voor het gevraagde venster "
            "(nog niet gepubliceerd? verkeerde biedingszone?)" + detail
        )

    # granulariteit = meest voorkomende slotduur (een incidenteel ontbrekend
    # kwartier in nog-in-publicatie-zijnde data mag de pijl niet verleggen)
    diffs = [
        round((slots[i + 1][0] - slots[i][0]).total_seconds() / 60.0)
        for i in range(len(slots) - 1)
    ]
    granularity_min = Counter(diffs).most_common(1)[0][0] if diffs else 60

    return {
        "slots": slots,
        "granularity_min": granularity_min,
        "source": (
            f"ENTSO-E day-ahead (web-api.tp.entsoe.eu, zone {in_domain}, "
            "EUR/MWh → EUR/kWh, excl. btw)"
        ),
        # dagen die deze ronde niet opgehaald konden worden (CDN-storing,
        # nog niet gepubliceerd); de watcher logt dit zodat het zichtbaar is
        # dat er met een kortere horizon is gepland.
        "warnings": warnings,
    }
