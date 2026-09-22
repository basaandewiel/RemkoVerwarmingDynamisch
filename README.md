# REMKO WKF 70 (NEO) compact — dynamisch stroomadvies voor stookseizoen

Bepaalt wanneer de warmtepomp de komende ~24–48 uur het beste een blok van
**3 uur** kan draaien, rekening houdend met:

1. de **COP** van de warmtepomp als functie van de **buitentemperatuur**
   (uit de officiële REMKO specs, zie onder),
2. de **temperatuurvoorspelling per uur** (met.no API),
3. de **dynamische stroomprijs** (ENTSO-E Transparency Platform, API-sleutel,
   day-ahead prijzen voor de Nederlandse biedingszone met **kwartier-resolutie**),
4. een **gecorrigeerde prijs** = `kWh-prijs / COP(verwachte buitentemp.)`
   (dat is de prijs per kWh *warmte* — op koude uren is de stroom misschien
   goedkoop, maar de warmtepomp dan juist duurder in gebruik),
5. het **goedkoopste aaneengesloten blok van 3 uur** (schuivend venster)
   op basis van die gecorrigeerde prijs,
6. (optioneel) hetzelfde voor **sanitair warm water (SWW)**: het water wordt
   opgewarmd tot een hogere temperatuur (standaard 53 °C), wat een veel lagere
   COP geeft — en dus een eigen (soms ander) goedkoopste blok.
   Dit wordt berekend met een aparte COP-curve, zie `heatpump.dhw` in de config.

Het resultaat kan optioneel via **MQTT** worden gepubliceerd (de MQTT-
integratie van jouw warmtepomp draait al; dit programma publiceert alleen
een advies op topics die jij in `config.json` instelt).

---

## Installatie

Vereisten: **Python 3.9+** (gebruikt alleen de standaardbibliotheek).

```bash
# optioneel: alleen nodig als je MQTT-publicatie wilt gebruiken
pip install paho-mqtt

python3 main.py
```

## Configuratie

Kopieer en pas `config.json` aan (`config.example.json` is een template
zonder API-key; `config.json` staat in `.gitignore`):

| Sleutel | Uitleg |
|---|---|
| `location.lat` / `location.lon` | **Vul hier jouw coördinaten in** (met.no heeft ze nodig). Default is een voorbeeld (Utrecht). Bv. postcode 4000-serie → `52.09, 5.12`; bepaal je eigen coördinaten via o.a. maps.google.com (rechtermuisknop → coördinaten). |
| `location.timezone` | IANA-tijdzone, default `Europe/Amsterdam`. |
| `forecast.horizon_hours` | Hoe ver de temperatuurvoorspelling wordt opgehaald (uurwaarden). 72 is ruim genoeg; de prijsdata reikt meestal maar ~48 uur. |
| `forecast.cache_ttl_seconds` | met.no verzoekt te cachen; 600 s (10 min) is netjes. |
| `forecast.user_agent` | **verplicht door met.no** — vul een herkenbare string in (bijv. met e-mailadres). |
| `heatpump.supply_temperature` | Aanvoertemperatuur ruimteverwarming: `35`, `45` of `55`. Kopieer de bijbehorende COP-curve naar `cop_curve` (zie hieronder). |
| `heatpump.cop_curve_w53` | Geschatte COP-curve voor warm water tot 53 °C (\*). |
| `heatpump.dhw.enabled` | `true` (default): bereken ook het advies voor sanitair warm water. |
| `heatpump.dhw.temperature` | Doeltemperatuur warmwaterboiler (default `53`). |
| `heatpump.dhw.curve_key` | Welke config-curve voor SWW wordt gebruikt (default `cop_curve_w53`). |
| `optimization.block_hours` | Lengte van het goedkoopste blok (default 3). |
| `optimization.only_future` | `true`: alleen blokken die nu of later starten. |
| `optimization.top_n` | Hoeveel beste blokken worden weergegeven. |
| `prices.source` | `entsoe` (default) of `energyzero` als alternatief. |
| `prices.days_ahead` | Hoeveel dagen vooruit plannen (day-ahead prijzen zijn meestal ~48 u bekend). |
| `prices.entsoe.api_key` | **Jouw persoonlijke ENTSO-E API-key** (gratis account op https://transparency.entsoe.eu → My Account → API). |
| `prices.entsoe.in_domain` / `out_domain` | Biedingszone; NL = `10YNL----------L`. |
| `prices.energyzero.*` | Alleen gebruikt als `source` = `energyzero` (gratis, zonder key, maar uurprijzen). |
| `prices.price_adjustments.vat_pct` | Btw-percentage op de groothandelsprijs (bv. `21`). Constante factor, verandert de blokkeuze niet. |
| `prices.price_adjustments.fixed_tax_per_kwh` | Vaste belasting per kWh (bv. energiebelasting €/kWh). **Verandert de blokkeuze wel** (want `(prijs + belasting)/COP`). Standaard 0,12 €/kWh in de config. |
| `mqtt.*` | MQTT-publicatie (broker, topics). Zet `enabled` op `false` om uit te schakelen. |
| `mqtt.control_topic` | Topic waarop het **SWW-boost-commando** wordt gepubliceerd (default `<topic_base>/set`). |
| `mqtt.dhw_boost.enabled` | Master-schakelaar voor het boost-commando. |
| `mqtt.dhw_boost.trigger_minutes` | Venster aan het begin van het SWW-blok (default 45) waarbinnen het commando verstuurd wordt. |
| `mqtt.dhw_boost.payload` | Wordt **afgeleid** uit `heatpump.dhw.temperature`: temperatuur × 10 als hex (53 °C → 530 → `"0212"`). Niet handmatig instellen. |

## COP-curve van de REMKO WKF 70 (NEO) compact

De waarden komen rechtstreeks uit de **REMKO technische data / MSR- en
installatiehandleiding** (volgens EN 14511; eerst kolom onder `Heizleistung /
Kompressorfrequenz / COP` voor het WKF 70 NEO compact — werkpunt
A7/W35, overgenomen in de NEO brochure):

| Buitenlucht | COP @ 35 °C | COP @ 45 °C | COP @ 55 °C | COP @ 53 °C (sww ⚠) |
|---|---|---|---|---|
| +12 °C | 5,10 | – | – | 3,27 |
| +10 °C | 4,92 | – | – | 3,15 |
|  +7 °C | 4,62 | 3,60 | 2,80 | 2,96 |
|  +2 °C | 3,50 | – | – | 2,28 |
|  −7 °C | 2,80 | 2,60 | 1,70 | 1,88 |
| −15 °C | 2,50 | – | – | 1,68 |

Tussen de meetpunten wordt **lineair geïnterpoleerd**; buiten het bereik
wordt de dichtstbijzijnde waarde gebruikt. Bij aanvoertemperatuur 35 °C
(vloerverwarming) is de curve compleet; bij 45/55 °C zijn er slechts twee
meetpunten bekend — kies dan bewust welke aanvoertemperatuur je werkelijk
gebruikt.

> ⚠ De kolom **53 °C** is een *schatting*: REMKO geeft geen meting voor
> precies 53 °C. De waarden zijn afgeleid door lineair te interpoleren tussen
> de gemeten **45 °C- en 55 °C-curven** (factor 0,8 = (53−45)/(55−45)) en die
> verhouding over het buitentemperatuurbereik te leggen:
> 12 °C → 5,10·0,641 = 3,27; 10 °C → 4,92·0,641 = 3,15; 7 °C → 2,96;
> 2 °C → 3,50·0,652 = 2,28; −7 °C → 1,88; −15 °C → 2,50·0,671 = 1,68.
> Heeft jouw installatie echte SWW-metingen (bijv. uit de
> warmwatermodus van de WKF), vervang dan de waarden in
> `heatpump.cop_curve_w53` — het programma rekent met elke curve.

> Tip: vervang de waarden in `config.json` gerust door de tabel uit *jouw*
> handleiding als die afwijkt; het programma werkt met elke COP-curve.

## Gebruik

```bash
# normaal
python3 main.py

# machine-leesbare output (voor automatisering)
python3 main.py --json

# zelf instellen wat "nu" is (handig voor testen)
python3 main.py --now "2026-09-17T21:00:00+02:00"

# overrides
python3 main.py --lat 52.37 --lon 4.90 --block-hours 3 --supply-temperature 35

# geen MQTT deze keer
python3 main.py --no-mqtt
```

### Automatisch periodiek draaien (cron)

```cron
# elke 10 minuten een nieuw advies (evt. alleen overdag)
*/10 * * * * cd /home/pi/remkoverwarming && /usr/bin/python3 main.py >> run.log 2>&1
```

## SWW-boost-commando via MQTT (`dhw_boost.py`)

Als `heatpump.dhw.enabled` aan staat, kan het programma op het moment dat
het **goedkoopste 3-uursblok voor sanitair warm water begint** een
start-commando naar de warmtepomp sturen: `{"values": {"1082": "<hex>"}}`
op `mqtt.control_topic` (default `<topic_base>/set`).

De waarde van register 1082 wordt **afgeleid** uit
`heatpump.dhw.temperature` uit config.json: de gewenste temperatuur
(°C × 10), uitgedrukt als hexadecimaal getal. Met 53 °C is dat
53 × 10 = 530 decimal = `0x212`, dus de payload wordt
`{"values": {"1082": "0212"}}`.

De *stop* wordt bewust niet verstuurd: de warmtepomp stopt zelf zodra de
boiler op temperatuur is (setpoint 53 °C).

Draai `dhw_boost.py` net zo vaak als je wilt (het is een one-shot-check die
niets doet als het niet de tijd is). Gebruik bijv. een cron-regel per 5 min:

```cron
*/5 * * * * cd /home/pi/remkoverwarming && /usr/bin/python3 dhw_boost.py >> boost.log 2>&1
```

Wat het script doet per run:

- berekent hetzelfde advies als `main.py` (zelfde config),
- is "nu" binnen de eerste `mqtt.dhw_boost.trigger_minutes` (default 45) van
  het beste SWW-blok, dan publiceert het het start-commando **eenmalig**;
- een statusfile in `~/.cache/remko-wkf70/dhw_boost_state.json` onthoudt per
  blok-start dat er al verstuurd is (geen dubbele berichten tijdens hetzelfde
  blok). Een 5-minuten-cron stuurt het commando dus binnen 5 minuten na de
  blokstart (testen: `python3 dhw_boost.py --now "2026-09-23T11:45:00+02:00" --dry-run`).

## Output & MQTT-topics

Tekstuitvoer toont per prijsslot: prijs, verwachte buitentemperatuur,
COP en de gecorrigeerde prijs, plus het beste blok van 3 uur (en top-N).
Is `heatpump.dhw.enabled` aan, dan komt daar een aparte sectie
**Sanitair warm water (SWW)** achteraan: een eigen per-slot tabel en het
beste 3-uursblok voor het opwarmen tot 53 °C (op basis van de SWW-COP).

Met MQTT ingeschakeld wordt gepubliceerd (paylod = JSON):

| Topic | Inhoud |
|---|---|
| `<topic_base>/advice` | Het beste blok voor ruimteverwarming: `start`, `end`, gemiddelde gecorrigeerde prijs e.d. |
| `<topic_base>/prices` | Alle (toekomstige) slots met prijs, temp, COP en gecorrigeerde prijs — je eigen sturing kan hierop filters toepassen. |
| `<topic_base>/dhw/advice` | Idem als `advice`, maar voor sanitair warm water tot 53 °C. |
| `<topic_base>/dhw/prices` | Idem als `prices`, maar met de SWW-COP en -gecorrigeerde prijzen. |
| `<topic_base>/status` | Statusberichtje (run geslaagd / foutmelding). |

## Prijsgranulariteit & ENTSO-E

De ENTSO-E-API wordt gevraagd met `documentType=A44` (day-ahead prijzen).
Voor Nederland levert de API de prijzen met **15-minuten-resolutie (PT15M,
96 punten/dag)** — dat sluit precies aan op "kWh-prijzen die per kwartier
variëren". Het programma past zich ook automatisch aan op uurdata (15/30/60
minuten) als dat nodig is.

De ENTSO-E-prijzen zijn **grootschalige day-ahead prijzen, exclusief btw en
energiebelasting** (in EUR/MWh, hier omgerekend naar €/kWh). Wil je een
realistische inschatting van je variabele stroomprijs, vul dan in
`config.json` onder `price_adjustments` het btw-percentage en/of de vaste
belasting per kWh in:

- een **constante factor** (btw) is voor het advies niet van belang: die
  telt bij elk slot even zwaar mee en verandert niet wélk blok gekozen wordt;
- een **vaste belasting per kWh** wél: die telt juist zwaarder in koude uren
  (lage COP), dus neem die mee als je tarief zo in elkaar zit.

> Zet `prices.source` op `energyzero` als je liever de archivering van
> EnergyZero gebruikt (gratis, zonder key, maar uurprijzen).

## API-toegang & -voorwaarden

- **ENTSO-E Transparency Platform**: gratis account + persoonlijke API-key
  (https://transparency.entsoe.eu). Authenticatie via `securityToken`.
  **Config-bestand met je key niet openbaar maken** (`.gitignore` opnemen).
- **met.no**: verplicht een herkenbare `User-Agent`; resultaten worden lokaal
  gecachet (TTL 10 min). Zie https://api.met.no/conditions_service/
- **EnergyZero**: publieke API van Energie Zero B.V. / EasyEnergy, gratis
  zonder key. Zie https://www.energyzero.nl/
- Respecteer cache-intervallen; loop geen eindeloze tests.