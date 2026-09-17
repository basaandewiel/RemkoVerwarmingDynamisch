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

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

API_URL = "https://web-api.tp.entsoe.eu/api"
DEFAULT_DOMAIN = "10YNL----------L"  # Nederland

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
) -> List[Tuple[datetime, float]]:
    """Haal de day-ahead prijzen van één kalenderdag op (dt_utc, EUR/kWh)."""
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
        with urllib.request.urlopen(req, timeout=40) as resp:  # noqa: S310
            xml_text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"ENTSO-E HTTP-fout {exc.code} voor dag {day} — "
            "geldige key? te veel requests? (zie https://transparency.entsoe.eu)"
        ) from exc

    root = ET.fromstring(xml_text)
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
    days_ahead: int = 2,
    in_domain: str = DEFAULT_DOMAIN,
    out_domain: str = DEFAULT_DOMAIN,
) -> Dict:
    """Haal day-ahead prijzen op voor vandaag .. vandaag+days_ahead-1."""
    local_tz = ZoneInfo(tz)
    now_local = datetime.now(local_tz).date()
    slots: List[Tuple[datetime, float]] = []
    for offset in range(days_ahead):
        day = now_local + timedelta(days=offset)
        expected_start_utc = datetime.combine(
            day, time.min, tzinfo=local_tz
        ).astimezone(timezone.utc)
        try:
            day_slots = _request_day(api_key, day, in_domain, out_domain, expected_start_utc)
        except ET.ParseError as exc:
            raise RuntimeError(f"ENTSO-E gaf ongeldige XML terug voor dag {day}") from exc
        slots.extend(day_slots)

    # sorteeren + dubbelen eruit, en omzetten naar lokale tijd
    slots = sorted(set(slots), key=lambda s: s[0])
    slots = [(dt.astimezone(local_tz), price) for dt, price in slots]
    if not slots:
        raise RuntimeError(
            "ENTSO-E leverde geen day-ahead prijzen voor het gevraagde venster "
            "(nog niet gepubliceerd? verkeerde biedingszone?)"
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
    }