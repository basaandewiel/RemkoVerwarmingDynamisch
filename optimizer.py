"""Kern van het advies: gecorrigeerde prijs = kWh-prijs / COP, en daaruit
het goedkoopste aaneengesloten blok van `block_hours` uur (schuivend venster).

Een slot met ontbrekende temperatuurvoorspelling wordt overgeslagen (kan
alleen aan de verste horizon voorkomen, waar ook geen prijzen meer zijn).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
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
    earliest_start: Optional[datetime] = None,
    end_window: Optional[Tuple[str, str]] = None,
) -> List[Dict]:
    """Zoek de goedkoopste aaneengesloten blokken van `block_hours` uur.

    `earliest_start` (optioneel): geen blok start vóór dit tijdstip. Handig
    voor meerdere boosts per dag: het volgende blok moet pas beginnen ná een
    minimale tussenruimte na het vorige (anders kiest het advies twee keer
    hetzelfde/naburige goedkoopste moment).

    `end_window` (optioneel): het blok moet eindigen binnen dit
    tijdsvenster, gegeven als twee "HH:MM"-tijden, bijv. ("19:00", "08:00")
    = tussen 19:00 's avonds en 08:00 de volgende ochtend (loopt dus over
    middernacht heen). Bedoeld voor het LAATSTE boost-blok van het
    24-uursvenster, zodat de laatste (nachtelijke) opwarmperiode nooit
    midden op de dag eindigt maar de boiler 's avonds/nachts vol is.
    """
    if len(rows) < 2:
        return []

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not only_future:
        now_utc = datetime.min.replace(tzinfo=timezone.utc)
    # earliest_start: elk blok mag pas beginnen op/na dit tijdstip (ook als
    # dat vóór 'now' ligt, dan houdt only_future de boel al af).
    earliest_utc = None
    if earliest_start is not None:
        earliest_utc = earliest_start.astimezone(timezone.utc)
    # end_window: het einde van het blok moet binnen dit (circulaire) venster
    # vallen, bijv. 19:00-08:00 -> einde ≥ 19:00 óf einde ≤ 08:00.
    end_from_t = end_to_t = None
    if end_window is not None:
        end_from_t = time(*[int(x) for x in end_window[0].split(":")])
        end_to_t = time(*[int(x) for x in end_window[1].split(":")])

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
        if earliest_utc is not None and window[0]["dt_utc"] < earliest_utc:
            continue
        if end_from_t is not None:
            end_dt = window[-1]["dt_local"] + slot_delta
            end_tod = end_dt.time()
            # circulair venster over middernacht: einde ná 'from' (19:00) of
            # vóór/op 'to' (08:00 de volgende ochtend)
            if not (end_tod >= end_from_t or end_tod <= end_to_t):
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