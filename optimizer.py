"""Kern van het advies: gecorrigeerde prijs = kWh-prijs / COP, en daaruit
het goedkoopste aaneengesloten blok van `block_hours` uur (schuivend venster).

Een slot met ontbrekende temperatuurvoorspelling wordt overgeslagen (kan
alleen aan de verste horizon voorkomen, waar ook geen prijzen meer zijn).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from cop_model import CopModel


def temperature_for_slot(dt_local: datetime, temps_by_hour: Dict[int, float]) -> Optional[float]:
    """Buitentemperatuur voor het uur waarin het slot valt."""
    hour_start = dt_local.replace(minute=0, second=0, microsecond=0)
    key = int(hour_start.timestamp())
    if key in temps_by_hour:
        return temps_by_hour[key]
    # uiterste poging: dichtstbijzijnde beschikbare uurwaarde
    if temps_by_hour:
        return min(temps_by_hour.items(), key=lambda kv: abs(kv[0] - key))[1]
    return None


def build_corrected_rows(
    price_slots: List[Tuple[datetime, float]],
    temps_by_hour: Dict[int, float],
    cop_model: CopModel,
) -> List[Dict]:
    """Bereken per slot: temp, COP en gecorrigeerde prijs (= prijs / COP)."""
    rows: List[Dict] = []
    for dt_local, price in price_slots:
        temp = temperature_for_slot(dt_local, temps_by_hour)
        if temp is None:
            continue
        cop = cop_model.cop(temp)
        rows.append(
            {
                "dt_local": dt_local,
                "dt_utc": dt_local.astimezone(timezone.utc),
                "price": price,
                "temp": temp,
                "cop": cop,
                "corrected": price / cop,
            }
        )
    return rows


def find_cheapest_blocks(
    rows: List[Dict],
    granularity_min: int,
    block_hours: int = 3,
    only_future: bool = True,
    top_n: int = 3,
    now: Optional[datetime] = None,
) -> List[Dict]:
    """Zoek de goedkoopste aaneengesloten blokken van `block_hours` uur."""
    if len(rows) < 2:
        return []

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not only_future:
        now_utc = datetime.min.replace(tzinfo=timezone.utc)

    n_slots = max(1, round(block_hours * 60 / granularity_min))
    slot_delta = timedelta(minutes=granularity_min)

    blocks: List[Dict] = []
    for i in range(len(rows) - n_slots + 1):
        window = rows[i : i + n_slots]
        # aaneengesloten? (controle op UTC-basis, i.v.m. zomer/wintertijd)
        contiguous = all(
            window[j + 1]["dt_utc"] - window[j]["dt_utc"] == slot_delta
            for j in range(n_slots - 1)
        )
        if not contiguous:
            continue
        if window[0]["dt_utc"] < now_utc:
            continue

        mean_price = sum(r["price"] for r in window) / n_slots
        mean_temp = sum(r["temp"] for r in window) / n_slots
        mean_cop = sum(r["cop"] for r in window) / n_slots
        mean_corrected = sum(r["corrected"] for r in window) / n_slots
        blocks.append(
            {
                "start": window[0]["dt_local"],
                "end": window[-1]["dt_local"] + slot_delta,
                "block_hours": block_hours,
                "mean_price": mean_price,
                "mean_temp": mean_temp,
                "mean_cop": mean_cop,
                "mean_corrected": mean_corrected,
                "slots": window,
            }
        )

    blocks.sort(key=lambda b: b["mean_corrected"])
    return blocks[:top_n]