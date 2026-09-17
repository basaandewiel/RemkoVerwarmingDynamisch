"""Dynamische stroomprijzen via de publieke EnergyZero/EasyEnergy API.

Endpoint: https://api.energyzero.nl/v1/energyprices
Documentatie/gebruik: veel gebruikt in de Home Assistant-community; gratis,
zonder API-key.

De API wordt gevraagd met interval=4 (kwartier). Voor Nederland geeft de API
momenteel echter *uurprijzen* terug; de functie past zich automatisch aan de
aangeleverde granulariteit aan (15/30/60 min).

Returns dict:
    {
      "slots": [(datetime_lokaal, prijs_eur_per_kwh), ...],   # oplopend in tijd
      "granularity_min": 15 | 30 | 60,
      "source": "EnergyZero ...",
    }
Slots die buiten het gevraagde venster vallen (bijv. een randwaarde op de
grensmiddernacht) worden genegeerd.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from statistics import mode
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

API_URL_DEFAULT = "https://api.energyzero.nl/v1/energyprices"

KNOWN_GRANULARITIES = (15, 30, 60)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_prices(
    tz: str,
    days_ahead: int = 2,
    api_url: str = API_URL_DEFAULT,
    usage_type: int = 1,
    incl_btw: bool = True,
) -> Dict:
    """Haal day-ahead prijzen op voor vandaag .. vandaag+days_ahead dagen."""
    local_tz = ZoneInfo(tz)
    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end_utc = start_utc + timedelta(days=days_ahead)

    params = {
        "fromDate": start_utc.strftime("%Y-%m-%dT00:00:00.000Z"),
        "tillDate": end_utc.strftime("%Y-%m-%dT00:00:00.000Z"),
        "interval": 4,  # 4 slots per uur gevraagd
        "usageType": usage_type,
        "inclBtw": "true" if incl_btw else "false",
    }
    url = f"{api_url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8"))

    raw = payload.get("Prices") or payload.get("prices") or []
    if not raw:
        raise RuntimeError(
            f"EnergyZero-API gaf geen prijzen terug ({url}); "
            "klopt de API-URL of zijn de prijzen al gepubliceerd?"
        )

    slots: List[Tuple[datetime, float]] = []
    for item in raw:
        dt_utc = _parse_iso(item["readingDate"])
        if dt_utc < start_utc or dt_utc >= end_utc:  # randwaarden weggooien
            continue
        slots.append((dt_utc.astimezone(local_tz), float(item["price"])))
    slots.sort(key=lambda s: s[0])
    if not slots:
        raise RuntimeError("EnergyZero-API leverde geen geldige slots binnen het venster")

    granularity_min = _detect_granularity(slots)
    return {
        "slots": slots,
        "granularity_min": granularity_min,
        "source": f"EnergyZero (day-ahead, {'incl. btw' if incl_btw else 'excl. btw'})",
    }


def _detect_granularity(slots: List[Tuple[datetime, float]]) -> int:
    """Leid de slotduur af uit de tijdverschillen tussen opeenvolgende slots."""
    if len(slots) < 2:
        return 60
    diffs = [
        round((slots[i + 1][0] - slots[i][0]).total_seconds() / 60.0)
        for i in range(len(slots) - 1)
    ]
    if not diffs:
        return 60
    typical = int(mode(diffs))
    # dichtstbijzijnde herkende granulariteit
    best = min(KNOWN_GRANULARITIES, key=lambda g: abs(g - typical))
    return best