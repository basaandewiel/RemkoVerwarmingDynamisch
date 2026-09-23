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
| `mqtt.control_topic` | Topic waarop het **SWW-boost-commando** wordt gepubliceerd. **Default = `topic_base` zelf** — de REMKO-gateway luistert op `V04P26/SMTID/CLIENT2HOST`, dus **niet** op een `/set`-subtopic. |
| `mqtt.dhw_boost.enabled` | Master-schakelaar voor het boost-commando. |
| `mqtt.dhw_boost.trigger_minutes` | Venster aan het begin van het SWW-blok (default 45) waarbinnen het commando verstuurd wordt. |
| `mqtt.dhw_boost.default_temperature` | Temperatuur (°C) **waar de boiler na het goedkoopste blok weer naar teruggezet** wordt (reset-commando aan het blokeinde), default 40 °C. |
| `mqtt.dhw_boost.qos` | QoS-niveau voor de boost/reset-commando's (default `1`). Met QoS 1 moet de broker de ontvangst bevestigen (PUBACK) **voordat** `VERSTUURD` wordt getoond; bij QoS 0 is er geen garantie. |
| `mqtt.dhw_boost.retain` | Retain-flag op het commando (default `false`). Zet op `true` als je het laatste commando in MQTT Explorer zichtbaar wilt houden (elke nieuwe boost/reset overschrijft dan de vorige). |
| `mqtt.dhw_boost.payload` | Wordt **afgeleid**: boost-setting = `heatpump.dhw.temperature` × 10 als hex, reset = `mqtt.dhw_boost.default_temperature` × 10 als hex (53 °C → `"0212"`, 40 °C → `"0190"`), in het formaat dat de gateway accepteert incl. `FORCE_RESPONSE`. Niet handmatig instellen. |

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
start-commando naar de warmtepomp sturen op `mqtt.control_topic`
(**default = `topic_base` zelf**, bv. `V04P26/SMTID/CLIENT2HOST` — de
gateway luistert daar, niet op een `/set`-subtopic):

```json
{"FORCE_RESPONSE": true, "values": {"1082": "0212"}}
```

De waarde van register 1082 wordt **afgeleid** uit
`heatpump.dhw.temperature` uit config.json: de gewenste temperatuur
(°C × 10), uitgedrukt als hexadecimaal getal. Met 53 °C is dat
53 × 10 = 530 decimal = `0x212`, dus register 1082 wordt `"0212"`.
`FORCE_RESPONSE` is het veld dat de gateway ook in haar eigen query's
gebruikt en dwingt een directe status-bevestiging af.

**Aan het einde van het blok** wordt de temperatuur teruggezet naar de
default uit `mqtt.dhw_boost.default_temperature` (default 40 °C):
40 × 10 = 400 decimal = `0x190` → register 1082 wordt `"0190"`. Zo
verwarmt de boiler niet de rest van de dag door op duur stroom. Dit
reset-commando gaat alleen uit ná een verstuurd boost-commando voor
hetzelfde blok; de *stop na het opwarmen* doet de warmtepomp zelf (setpoint).

Er gaat maximaal **één boost per lokale dag** uit (het water wordt één keer
per dag bijverwarmd). Is de eerste boost van de dag gemist, dan mag de
eerstvolgende alsnog gaan.

**Aanbevolen: `--watch`** — een continu draaiend proces dat vrijwel exact op
de blokstart verstuurt. Het wordt alleen wakker als er iets kan veranderen
of gebeuren:

- de **blokstart zelf** → dan wordt het start-commando verstuurd, en het
  **blokeinde** → de reset terug naar de default-temperatuur;
- de **dagelijkse prijs-update** rond 13:30 (`--price-refresh-time`,
  default `13:30`) → het moment waarop de day-ahead-prijzen van de volgende
  dag binnenkomen, de enige keer dat het beste blok kan veranderen;
- alleen zolang er **nog geen blok bekend** is (bijv. vertraagde
  prijspublicatie) elke `--retry-interval` (default 30 min).

Herberekenen om de paar minuten is bewust niet nodig: het DHW-water wordt
elke dag bijverwarmd en het 3-uursblok is tussen deze momenten stabiel.

### Op een Raspberry Pi (systemd) — aanbevolen

De map `deploy/` bevat een systemd-unit en een installatiescript dat de
gebruikersnaam en map zelf invult en een venv opzet (nodig op Raspberry Pi
OS Bookworm: `pip install` zonder venv wordt geblokkeerd door PEP 668).

```bash
# op de Pi: clone of kopieer de repo naar /home/pi/remkoverwarming, daarna:
bash deploy/install.sh

# vul hierna je eigen config aan (API-key, coördinaten, mqtt-host):
nano config.json
sudo systemctl restart remko-sww-boost

# controle:
systemctl status remko-sww-boost
journalctl -u remko-sww-boost -f
```

Het script maakt `config.json` (uit `config.example.json`) aan als die
ontbreekt, installeert dependencies in `venv/`, en start de service met
`Restart=on-failure`. Je eigen `config.json` met API-key zet je gemakkelijk
over vanaf een andere machine: `scp config.json pi@<ip>:~/remkoverwarming/`.

Handmatig (zonder installatiescript) kan ook, bijv. als de service onder
jouw eigen user moet draaien:

```ini
# /etc/systemd/system/remko-sww-boost.service
[Unit]
Description=REMKO WKF SWW-boost (verstuur commando bij start goedkoopste blok)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/remkoverwarming
ExecStart=/home/pi/remkoverwarming/venv/bin/python3 dhw_boost.py --watch
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now remko-sww-boost
journalctl -u remko-sww-boost -f
```

Het proces blijft draaien en stuurt per blok precies één start-commando; de
statusfile in `~/.cache/remko-wkf70/dhw_boost_state.json` voorkomt dubbele
berichten. Is de broker even bezet, dan probeert het om de 30 s opnieuw
zolang het trigger-venster loopt.

> **Zie je het bericht niet in MQTT Explorer?** Boost/reset worden met
> **QoS 1** verzonden en het script wacht op de broker-bevestiging (PUBACK):
> zolang de regel `VERSTUURD` niet verschijnt (of juist `FOUT` toont), heeft
> de broker het bericht niet ontvangen of niet bevestigd. Controleer dan:
> 1. MQTT Explorer met een **wildcard-subscription** `V04P26/SMTID/#`
>    (het commando staat op `V04P26/SMTID/CLIENT2HOST`, de status met
>    register 1082 staat op `V04P26/SMTID/HOST2CLIENT`);
> 2. of Explorer op **dezelfde broker/poort** is aangesloten als
>    `mqtt.host`/`mqtt.port`;
> 3. of je ná het versturen subscribe't — bij `retain: false` is een bericht
>    alleen zichtbaar terwijl er live gesubscribe wordt (of zet
>    `mqtt.dhw_boost.retain` op `true` om het laatste commando vast te houden).
>    Snel testen vanaf de CLI:
>    `mosquitto_sub -h 192.168.1.1 -t 'V04P26/SMTID/CLIENT2HOST/#' -v`

**Alternatief: via cron** — een one-shot-check die niets doet als het niet
de tijd is, maar het commando gaat dan hooguit een cron-interval ná de
blokstart uit:

```cron
*/5 * * * * cd /home/pi/remkoverwarming && /usr/bin/python3 dhw_boost.py >> boost.log 2>&1
```

Wat het script per run doet:

- berekent hetzelfde advies als `main.py` (zelfde config),
- is "nu" binnen de eerste `mqtt.dhw_boost.trigger_minutes` (default 45) van
  het beste SWW-blok, dan publiceert het het start-commando **eenmalig**;
- een statusfile in `~/.cache/remko-wkf70/dhw_boost_state.json` onthoudt per
  blok-start dat er al verstuurd is (geen dubbele berichten tijdens hetzelfde
  blok). Testen zonder te versturen:
  `python3 dhw_boost.py --now "2026-09-23T11:45:00+02:00" --dry-run`,
  of `--watch --now ... --dry-run` voor één watch-cyclus.

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