"""COP-model voor de REMKO WKF 70 (NEO) compact warmtepomp.

Waarden volgens EN 14511 uit de officiële REMKO technische data /
installatiehandleiding (WKF 70 NEO compact):

    Buitenlucht | COP @ W35  | COP @ W45 | COP @ W55 | COP @ W53 (sww, geschat)
    ------------+------------+-----------+-----------+--------------------------
    +12 °C      | 5,10       |    –      |    –      | 3,27
    +10 °C      | 4,92       |    –      |    –      | 3,15
     +7 °C      | 4,62       | 3,60      | 2,80      | 2,96
     +2 °C      | 3,50       |    –      |    –      | 2,28
     −7 °C      | 2,80       | 2,60      | 1,70      | 1,88
    −15 °C      | 2,50       |    –      |    –      | 1,68

De W53-curve (sanitair warm water, opwarmen tot 53 °C) is een *schatting*:
lineaire interpolatie in de aanvoertemperatuur tussen de gemeten W45- en
W55-waarden (factor 0,8 = (53-45)/(55-45)), met de ratio-vorm van de W35-
curve over het buitentemperatuurbereik. Meetpunten: 12 °C → 5,10·0,641 = 3,27;
10 °C → 4,92·0,641 = 3,15; 7 °C → 2,96; 2 °C → 3,50·0,652 = 2,28;
−7 °C → 1,88; −15 °C → 2,50·0,671 = 1,68.

Tussen de meetpunten wordt lineair geïnterpoleerd; buiten het meetbereik
wordt op de dichtstbijzijnde waarde geklemd.
"""

from typing import Optional, Sequence, Tuple

CurvePoint = Tuple[float, float]  # (buitentemperatuur °C, COP)

# Standaardcurven (sleutel = aanvoertemperatuur in °C). Bron: REMKO WKF NEO
# compact serie, technische gegevens (kolom Heizleistung/Kompressorfrequenz/COP).
DEFAULT_CURVES: dict[int, Sequence[CurvePoint]] = {
    35: [
        (12.0, 5.10),
        (10.0, 4.92),
        (7.0, 4.62),
        (2.0, 3.50),
        (-7.0, 2.80),
        (-15.0, 2.50),
    ],
    45: [(7.0, 3.60), (-7.0, 2.60)],
    55: [(7.0, 2.80), (-7.0, 1.70)],
    # Schatting voor sanitair warm water (53 °C), zie docstring.
    53: [
        (12.0, 3.27),
        (10.0, 3.15),
        (7.0, 2.96),
        (2.0, 2.28),
        (-7.0, 1.88),
        (-15.0, 1.68),
    ],
}


class CopModel:
    """Interpoleert de COP uit een lijst meetpunten (buitentemp, COP)."""

    def __init__(
        self,
        supply_temperature: int = 35,
        curve: Optional[Sequence[CurvePoint]] = None,
    ) -> None:
        self.supply_temperature = supply_temperature
        self.points: Sequence[CurvePoint] = sorted(
            curve if curve is not None else DEFAULT_CURVES[supply_temperature],
            key=lambda p: p[0],
        )
        if len(self.points) < 2:
            raise ValueError("de COP-curve moet minstens 2 meetpunten bevatten")
        if any(cop <= 0 for _, cop in self.points):
            raise ValueError("COP-waarden moeten groter dan 0 zijn")
        # oplopend gesorteerd houden
        self._temps = [t for t, _ in self.points]
        self._cops = [c for _, c in self.points]

    def cop(self, outside_temp: float) -> float:
        """Geef de COP voor een buitentemperatuur (lineair geïnterpoleerd)."""
        temps, cops = self._temps, self._cops
        if outside_temp <= temps[0]:
            return cops[0]
        if outside_temp >= temps[-1]:
            return cops[-1]
        for i in range(len(self.points) - 1):
            t0, t1 = temps[i], temps[i + 1]
            if t0 <= outside_temp <= t1:
                if t1 == t0:
                    return cops[i]
                frac = (outside_temp - t0) / (t1 - t0)
                return cops[i] + frac * (cops[i + 1] - cops[i])
        raise AssertionError("ongeldige interpolatie-toestand")  # pragma: no cover

    @property
    def source(self) -> str:
        return f"REMKO WKF 70 NEO compact, aanvoertemperatuur {self.supply_temperature} °C (EN 14511)"