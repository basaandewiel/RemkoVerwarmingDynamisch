"""Temperatuurvoorspelling per uur via de met.no locationforecast API.

Documentatie: https://api.met.no/weatherapi/locationforecast/2.0/documentation
Regels met.no:
  * een herkenbare User-Agent is verplicht (config: forecast.user_agent),
  * antwoorden moeten lokaal worden gecachet (config: cache_ttl_seconds).

Retourneert een lijst van (datetime_lokaal, buitentemperatuur °C).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta
from typing import List, Tuple
from zoneinfo import ZoneInfo

API_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"


def _parse_iso(value: str) -> datetime:
    """Parse ISO-datum; ondersteunt 'Z' op alle Python >= 3.9."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _cache_path(cache_dir: str, lat: float, lon: float) -> str:
    name = f"metno_{lat:.4f}_{lon:.4f}.json"
    return os.path.join(cache_dir, name)


def fetch_hourly_forecast(
    lat: float,
    lon: float,
    tz: str,
    user_agent: str,
    horizon_hours: int = 72,
    cache_dir: str | None = None,
    cache_ttl_seconds: int = 600,
) -> List[Tuple[datetime, float]]:
    """Haal de uurvoorspelling op en retourneer (lokale tijd, temp °C)."""
    local_tz = ZoneInfo(tz)
    cache_dir = cache_dir or os.path.join(
        os.path.expanduser("~"), ".cache", "remko-wkf70"
    )
    path = _cache_path(cache_dir, lat, lon)

    data: dict | None = None
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < cache_ttl_seconds:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

    if data is None:
        url = f"{API_URL}?lat={lat:.5f}&lon={lon:.5f}"
        req = urllib.request.Request(
            url, headers={"User-Agent": user_agent, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    series = data["properties"]["timeseries"]
    if not series:
        raise RuntimeError("met.no gaf geen tijdsreeks terug")

    horizon = timedelta(hours=horizon_hours)
    start = _parse_iso(series[0]["time"])
    result: List[Tuple[datetime, float]] = []
    for item in series:
        ts_utc = _parse_iso(item["time"])
        if ts_utc - start > horizon:
            break
        temp = float(item["data"]["instant"]["details"]["air_temperature"])
        result.append((ts_utc.astimezone(local_tz), temp))
    return result


def hourly_temperatures(
    forecast: List[Tuple[datetime, float]],
) -> dict[int, float]:
    """Zet de voorspelling om naar {epoch_uur_start_lokaal: buitentemp}."""
    by_hour: dict[int, float] = {}
    for dt, temp in forecast:
        hour_start = dt.replace(minute=0, second=0, microsecond=0)
        by_hour[int(hour_start.timestamp())] = temp
    return by_hour