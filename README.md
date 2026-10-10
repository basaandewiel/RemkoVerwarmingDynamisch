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
7. (optioneel) het **meten van de echte COP** per SWW-opwarmepisode via de
   kWh-tellers van de gateway (`energy_log.py`): zo kun je de gekozen
   setpoint/boost-strategie later op échte kosten bijstellen in plaats van op
   een schatting.
8. (optioneel, alternatief voor de boost) de **SWW-comfortregeling**
   (`dhw_comfort.py`): houdt het boilervat tussen 07:00 en 22:00 op peil
   (minimaal 42 °C, met een buffer van ~47 °C zodat er ná een douche nog
   42 °C over is) en plant de opwarmmomenten op basis van de échte
   vattemperatuur, de uurvoorspelling, de COP en de kwartierprijzen.

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
| `optimization.block_hours` | Lengte van het goedkoopste blok voor de **ruimteverwarming** (default 3). |
| `optimization.only_future` | `true`: alleen blokken die nu of later starten. |
| `optimization.top_n` | Hoeveel beste blokken worden weergegeven. |
| `prices.source` | `entsoe` (default) of `energyzero` als alternatief. |
| `prices.fallback_source` | Reserve-prijsbron als de primaire faalt: `"auto"` (default → de andere bekende bron, entsoe ↔ energyzero), een vaste bronnaam, of `false` om de fallback uit te zetten. Een gebruikte fallback staat altijd zichtbaar in de output (bron + waarschuwing). |
| `prices.days_ahead` | Hoeveel dagen vooruit plannen (day-ahead prijzen zijn meestal ~48 u bekend, default 3). Dag +1 +2 zijn pas net gepubliceerd als je tweede SWW-boost ná middernacht valt — met 2 kan het tweede blok zomaar op de laatste uren van de horizon klem komen te zitten (bijv. 21:00-00:00), terwijl de goedkopere vroege ochtend van de dag erop onzichtbaar blijft. |
| `prices.entsoe.api_key` | **Jouw persoonlijke ENTSO-E API-key** (gratis account op https://transparency.entsoe.eu → My Account → API). |
| `prices.entsoe.in_domain` / `out_domain` | Biedingszone; NL = `10YNL----------L`. |
| `prices.entsoe.cache_ttl_seconds` | Houdt de opgehaalde day-ahead prijzen per dag op schijf (default 3600 s). Prijzen veranderen hooguit 1×/dag, dus een watcher hoeft niet bij elke wake de API te bevragen — scheelt aanzienlijk op een trage/overbelaste DNS-server. Bij een API-storing wordt deze cache (ook als hij ouder is dan de TTL) als terugvaloptie gebruikt. |
| `prices.energyzero.*` | Gebruikt als `source` of `fallback_source` `energyzero` is (gratis, zonder key, maar uurprijzen). |
| `prices.price_adjustments.vat_pct` | Btw-percentage op de groothandelsprijs (bv. `21`). Constante factor, verandert de blokkeuze niet. |
| `prices.price_adjustments.fixed_tax_per_kwh` | Vaste belasting per kWh (bv. energiebelasting €/kWh). **Verandert de blokkeuze wel** (want `(prijs + belasting)/COP`). Standaard 0,12 €/kWh in de config. |
| `mqtt.*` | MQTT-publicatie (broker, topics). Zet `enabled` op `false` om uit te schakelen. |
| `mqtt.control_topic` | Topic waarop het **SWW-boost-commando** wordt gepubliceerd. **Default = `topic_base` zelf** — de REMKO-gateway luistert op `V04P26/SMTID/CLIENT2HOST`, dus **niet** op een `/set`-subtopic. |
| `mqtt.dhw_boost.enabled` | Master-schakelaar voor het boost-commando. |
| `mqtt.dhw_boost.trigger_minutes` | Venster aan het begin van het SWW-blok (default 45) waarbinnen het commando verstuurd wordt. |
| `mqtt.dhw_boost.default_temperature` | Temperatuur (°C) **waar de boiler na het goedkoopste blok weer naar teruggezet** wordt (reset-commando aan het blokeinde), default 40 °C. |
| `mqtt.dhw_boost.boosts_per_day` | Maximaal aantal boosts per **rollend 24-uursvenster** (default 2). Géén kalenderdag-grens: de tweede boost mag gewoon op een andere dag vallen, mits er altijd ~2 opwarmmomenten binnen 24 uur plaatsvinden. |
| `mqtt.dhw_boost.min_gap_hours` | Minimum uren tussen het **einde van de vorige boost** en de **start van de volgende** (default 4). Zorgt dat een tweede opwarmperiode niet vlak na de eerste ligt. |
| `mqtt.dhw_boost.block_hours` | Lengte van één SWW-boost-blok (default **1**, los van `optimization.block_hours` voor de ruimteverwarming). Een korte boost volstaat voor SWW: het water hoeft niet 3 uur na te verwarmen, en blokken passen zo makkelijker in het goedkope avond/nacht-venster. |
| `mqtt.dhw_boost.last_block_end_from` / `last_block_end_to` | Het **laatste** boost-blok van het 24-uursvenster eindigt tussen deze tijden (default `"19:00"` en `"08:00"`, de volgende ochtend). Zonder deze eis glijdt de laatste opwarmperiode met het goedkoopste-blok-advies naar de volgende middag en is de boiler overdag 'leeg' in plaats van 's avonds/nachts vol. Daarnaast **start élk** boost-blok vóór `last_block_end_to`: het blok hoort in de eerstvolgende nacht te liggen en schuift niet door naar een goedkopere dag verderop. |
| `mqtt.dhw_boost.afternoon_from` / `afternoon_to` | **Verplicht dagvenster-blok** (default `"12:00"` en `"23:00"`): **per kalenderdag** start één van de boosts binnen dit venster zodra er al een boost in het 24-u-venster zit én er op díe dag nog geen binnen dit tijdsvenster startte (een boost van gisteren telt niet meer; het startuur `afternoon_to` zelf telt ook niet — om 23:00 start is het avond/nacht-blok). Zo komt er altijd midden op de dag/avond warm water — als het buiten warm is (weinig verlies) en vaak als de dagprijzen door zonneschijn het laagst zijn. Het eerste blok van een cyclus en het enkele blok bij `boosts_per_day=1` blijven vrij; het eind-venster en de ochtend-start-grens gelden voor het dagvenster-blok niet. Aan het einde van de avond (ná `afternoon_to`) schuift de verplichting door naar de volgende dag. |
| `mqtt.dhw_boost.qos` | QoS-niveau voor de boost/reset-commando's (default `1`). Met QoS 1 moet de broker de ontvangst bevestigen (PUBACK) **voordat** `VERSTUURD` wordt getoond; bij QoS 0 is er geen garantie. |
| `mqtt.dhw_boost.retain` | Retain-flag op het commando (default `false`). Zet op `true` als je het laatste commando in MQTT Explorer zichtbaar wilt houden (elke nieuwe boost/reset overschrijft dan de vorige). |
| `mqtt.dhw_boost.payload` | Wordt **afgeleid**: boost-setting = `heatpump.dhw.temperature` × 10 als hex, reset = `mqtt.dhw_boost.default_temperature` × 10 als hex (53 °C → `"0212"`, 40 °C → `"0190"`), in het formaat dat de gateway accepteert incl. `FORCE_RESPONSE`. Niet handmatig instellen. |
| `mqtt.data_topic` | Topic waarop de gateway de **status** publiceert. Wordt **afgeleid** uit `control_topic` (`…/CLIENT2HOST` → `…/HOST2CLIENT`, dus `V04P26/SMTID/HOST2CLIENT`). Alleen nodig als je gateway een afwijkend schema gebruikt. |
| `mqtt.energy_log.enabled` | `true` (default): de energie-logger leest de tellers uit (zie **Energie-logger**). |
| `mqtt.energy_log.interval_seconds` | Seconden tussen samples (default 60). 60 s is ruim genoeg: de tellers zijn cumulatief, dus ook een langzamer tempo meet de energie per opwarmepisode prima. |
| `mqtt.energy_log.response_timeout_seconds` | Hoe lang op het antwoord van de gateway wordt gewacht (default 8). |
| `mqtt.energy_log.csv_path` | Pad van de CSV-tijdreeks (default `~/.cache/remko-wkf70/dhw_energy.csv`). |
| `mqtt.energy_log.include_handshake` | `false` (default; logger is dan read-only). Zet op `true` als de gateway **niet** antwoordt zonder de 3 handshake-registers (5074/5106/5109) mee te sturen — test eerst met `energy_log.py --once --handshake`. |
| `mqtt.energy_log.log_price` | `true` (default): schrijf bij elke sample ook de **all-in stroomprijs** (`price_eur_per_kwh`, inclusief `prices.price_adjustments`) mee als kolom, zodat `--report` de kosten kan uitrekenen zonder historische prijzen op te hoeven halen. |
| `mqtt.energy_log.price_refresh_seconds` | Hoe vaak (seconden) de dag-ahead prijzen worden opgehaald/bijgewerkt (default 3600). Gebruikt dezelfde bron + fallback als het advies. |
| `mqtt.energy_log.price_days_ahead` | Hoeveel dagen vooruit prijzen worden opgehaald voor de logkolom (default 2). |
| `mqtt.energy_log.registers` | Welke registers worden gelogd (default de energietellers 5105/5374/5376 + temperaturen/opmode). |

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

Er gaan maximaal **`mqtt.dhw_boost.boosts_per_day`** boosts per **rollend
24-uursvenster** uit (default 2 — één keer per dag bijverwarmen is vaak te
weinig voor SWW; de tweede boost hoeft dus **niet op dezelfde kalenderdag**
te vallen). De volgende boost wordt pas gepland **ná `min_gap_hours` uur ná
het einde** van de vorige (default 4 u), zodat een tweede opwarmperiode écht
niet vlak na de eerste ligt — het advies zou anders twee keer (bijna)
hetzelfde goedkope moment kiezen. Het **laatste** blok van het venster (het
blok dat de daglimiet op `boosts_per_day` brengt) moet bovendien **eindigen
tussen `last_block_end_from` en `last_block_end_to`** (default 19:00 en
08:00 de volgende ochtend): de laatste opwarmperiode van de dag loopt dan
'tot 's avonds laat/begin van de nacht' in plaats van dat de tweede boost
met het goedkoopste-blok-advies naar de volgende middag doorschuift (zoals
toen het water maar 1× per dag leek op te warmen). Zit het venster vol, dan
wacht de watcher tot het oudste blok er weer uit valt; een derde boost kan
dus nooit binnen 24 uur na de eerste twee starten. Wordt een boost gemist,
dan mag de eerstvolgende alsnog gaan.

Er is daarnaast een verplichting **per kalenderdag**: één van de
opwarmmomenten start binnen het dagvenster `afternoon_from`–`afternoon_to`
(default 12:00–23:00). Heeft de dag waarop de volgende boost gepland wordt
nog géén boost binnen dat venster (een boost van gisteren telt dus niet
meer), dan wordt dat blok daar verplicht gepland: elke dag valt zo één
opwarmmoment in de middag/avond. Het dagvenster-blok is het goedkoopste
blok *binnen dat venster* (niet het hele-verkenning-optimum), zodat er
altijd warm water klaar is voor de avond — ook op windstille uren — en de
zonne-dip van de dagprijzen wordt meegepakt. Een boost die om precies
`afternoon_to` (23:00) start telt niet als dagvenster-blok: dat is het
avond/nacht-blok. De regel geldt pas ná een eerste boost (het allereerste
blok van een cyclus en het enkele blok bij `boosts_per_day=1` blijven vrij),
en alleen als het venster op de dag van de eerstvolgende boost nog haalbaar
is: om 23:30 is de verplichting voor vandaag voorbij en telt morgen opnieuw.
Het eind-venster en de ochtend-start-grens hierboven gelden voor dit
dagvenster-blok niet — het dagvenster begrenst het blok immers al tot
dezelfde dag.

De prijzen zijn pas een dag vooruit bekend, dus het goedkoopste blok van
morgen kan goedkoper zijn dan dat van vanavond. Zonder meer zou de planner
dan wachten — met tientallen uren koud water als gevolg (gemeten: 36 u tussen
twee boosts, terwijl de besparing €0,08 per opwarming was). Daarom **start
élk boost-blok vóór het ochtend-`last_block_end_to`** (default 08:00): het
optimum kijkt dan maximaal tot de eerstvolgende nacht. Past er binnen die
grens geen blok (bijv. doordat het pas ná 07:00 mag starten), dan wordt één
nacht verder gekeken en valt de grens uiteindelijk weg — liever een iets te
laat blok dan helemaal geen boost.

**Aanbevolen: `--watch`** — een continu draaiend proces dat vrijwel exact op
de blokstart verstuurt. Het wordt alleen wakker als er iets kan veranderen
of gebeuren:

- de **blokstart zelf** → dan wordt het start-commando verstuurd, en het
  **blokeinde** → de reset terug naar de default-temperatuur;
- de **dagelijkse prijs-update** rond 13:30 (`--price-refresh-time`,
  default `13:30`) → het moment waarop de day-ahead-prijzen van de volgende
  dag binnenkomen, de enige keer dat het beste blok kan veranderen. Zijn die
  prijzen daar nog **niet** (late publicatie bij ENTSO-E), dan wekt de
  watcher later nogmaals om te herberekenen (`--price-recheck-min`, default
  45 min) — alleen zolang het geplande blok nog niet op het punt staat te
  starten, zodat de trigger niet verstoord wordt;
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
sudo systemctl restart remko-sww-boost remko-energy-log

# controle:
systemctl status remko-sww-boost remko-energy-log
journalctl -u remko-sww-boost -f
journalctl -u remko-energy-log -f
```

Het script maakt `config.json` (uit `config.example.json`) aan als die
ontbreekt, installeert dependencies in `venv/`, en start de service met
`Restart=on-failure`. Je eigen `config.json` met API-key zet je gemakkelijk
over vanaf een andere machine: `scp config.json pi@<ip>:~/remkoverwarming/`.

**Handmatige runs op de Pi** moeten via de venv-Python (de systeem-Python
heeft paho-mqtt niet, en Bookworm blokkeert `pip install --user` / PEP 668):
`~/remkoverwarming/venv/bin/python3 main.py` (of eerst
`source ~/remkoverwarming/venv/bin/activate`). Anders toont `main.py`
`MQTT: niet beschikbaar (paho-mqtt ontbreekt of uitgeschakeld)`.

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
berichten. Na een verstuurde boost wordt de watcher precies op het
**blokeinde** wakker om de temperatuur terug te zetten naar de default
(`mqtt.dhw_boost.default_temperature`). Is de broker even bezet, dan probeert
het om de 30 s opnieuw: voor het boost-commando zolang het trigger-venster
loopt, voor de reset gewoon net zolang tot de broker de ontvangst bevestigt
(een boiler die op dure stroom op de boost-temperatuur blijft hangen wil je
niet).

De watcher wordt **~2 minuten vóór** de blokstart wakker (de data-ophaal kan
op een trage DNS-server tientallen seconden duren) en wacht daarna in kleine
stapjes tot de start. **Zie je telkens `blokstart over ~15 min` en schuift
die tijd nooit af?** Dan was de wake op `start + 2 s` te laat: tegen de tijd
dat de herberekening klaar was, sloot `only_future` het net gestarte blok uit
en koos het script het volgende. Los het op door de nieuwste versie te
draaien (fix: wake-ahead + wachten in stapjes) **en** `prices.entsoe.cache_ttl_seconds`
in de config (of `rm -f ~/.cache/remko-wkf70/entsoe_*.json` om een oude
test-cache te wissen).

> **Krijg je `ENTSO-E HTTP-fout 599/527` of `The read operation timed out` in
> het log?** Bijna altijd een storing of een overbelaste CDN tussen jou en de
> Transparency Platform — **niet** je API-key (die zou `401/403` geven). De
> watcher logt `FOUT (ga verder)` en probeert het gewoon opnieuw; de boost zelf
> gaat daarna gewoon door (zoals in het log: fouten om 04:15, gewoon een plan
> om 04:24). Wat het script nu doet om hier minder last van te hebben:
> 1. **4 pogingen per dag** met korte tussenpauzes (5/15/30 s) in plaats van
>    meteen opgeven;
> 2. **terugval op de laatst bekende prijzen**: is de API onbereikbaar en ligt
>    er een cachekopie van die dag op schijf, dan wordt die gebruikt in plaats
>    van niets — dag-ahead prijzen veranderen hooguit één keer per dag, dus dat
>    is prima bruikbaar;
> 3. **één dag die mislukt blokkeert de rest niet**: als vandaag binnenkomt maar
>    morgen niet, wordt er gewoon gepland met wat er is (het log toont dan één
>    regel `LET OP: prijzen ontbreken voor ...`) en bij de volgende wake komt de
>    ontbrekende dag alsnog mee.
>
> Blijft het storingsgedrag aanhouden, dan is de watcher waarschijnlijk de
> oorzaak: het pollt 3 dagen vooruit en start telkens opnieuw na een fout. Laat
> hem dan een paar uur met rust of verhoog `prices.entsoe.cache_ttl_seconds`
> (bijv. 7200).

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

## SWW-comfortregeling (`dhw_comfort.py`) — vat op peil tussen de douchetijden

Een **eenvoudiger alternatief** voor de vaste boost-planner hierboven. In
plaats van "twee keer per dag een uur naar 53 °C" regelt dit programma op de
**échte** vattemperatuur:

- tussen `window_from` en `window_to` (default **07:00–22:00**) is het de
  bedoeling dat er altijd genoeg warm water is om te douchen; in de praktijk
  moet het vat **vóór de douche minimaal `min_temp` (default 42 °C)** zijn;
- buiten dat venster mag het kouder zijn;
- de opwarmmomenten worden gepland met de **uurvoorspelling
  (buitentemperatuur)**, de **COP van de Remko** en de **kwartierprijzen**.

**Waarom een buffer van 47 °C?** Een douchebeurt verlaagt het vat met ongeveer
`shower_drop_c` (default 5 °C). Pas als het vat vóór de douche ≥ 47 °C is,
blijft er ná de douche nog ≥ 42 °C over. De regeling mikt daarom in het venster
op `buffer_temp` (default **47 °C**). Douchebeurten zijn onvoorspelbaar; de
regeling leest continu de echte temperatuur, dus een onverwachte daling wordt
meteen gezien en de volgende meting bijgeregeld — je hoeft **geen
doucheschema** op te geven.

**Zo werkt het** (elke `interval_minutes`, default 5 min):

1. lees de SWW-temperatuur (register 5039) via MQTT;
2. voorspel de temperatuur vooruit met de gemiddelde afkoeling
   (`cooling_c_per_hour`, default 0,3 °C/h);
3. zoek de eerstvolgende **deadline**: het moment binnen het venster waarop
   het vat zonder bijwarmen onder de buffer zou zakken;
4. valt die deadline binnen `lead_hours` (default 4 u), dan zoekt de regeling
   in `[nu, deadline]` het **goedkoopste opwarmblok** op basis van
   `prijs / COP(buitentemp.)` en zet op dat moment register **1082** naar
   `charge_temp` (default 52 °C);
5. buiten zo'n blok staat het setpoint op de ondergrens: `buffer_temp`
   (47 °C) **binnen** het venster — de warmtepomp houdt het vat dan zelf op
   peil — en `off_temp` (default 35 °C) daarbuiten, zodat het vat rustig
   uitzakt;
6. is het vat `charge_temp` of warmer, dan gaat het setpoint terug naar de
   ondergrens (geen onnodig lang op 52 °C blijven hangen).

Met `allow_outside_window: true` (default) mag er ook **buiten** 07:00–22:00
worden opgewarmd: 's nachts is de stroom vaak goedkoper, en zo is het vat om
07:00 al op temperatuur. De regeling begint pas met een blok als de deadline in
de lead-horizon valt — anders zou ze direct ná een opwarming meteen weer het
goedkoopste blok "nu" kiezen en het vat onnodig warm houden.

**Config** (`mqtt.dhw_comfort`, zie `config.example.json`):

| Sleutel | Default | Betekenis |
|---|---|---|
| `window_from` / `window_to` | `07:00` / `22:00` | comfortvenster (halve-open: het einduur telt niet mee) |
| `min_temp` | `42` | harde ondergrens (°C) |
| `buffer_temp` | `47` | streefwaarde in het venster (na één douche nog ≥ 42 °C) |
| `charge_temp` | `52` | setpoint tijdens een opwarmblok |
| `off_temp` | `35` | setpoint buiten het venster (geen actieve verwarming) |
| `tank_liters` | `300` | boilerinhoud (alleen voor rapportage) |
| `cooling_c_per_hour` | `0.3` | gemiddelde afkoeling per uur |
| `shower_drop_c` | `5.0` | temperatuurdaling per douchebeurt |
| `shower_detect_c` | `2.0` | extra daling t.o.v. de verwachte afkoeling = douche (alleen voor het log) |
| `charge_block_hours` | `1.0` | lengte van een opwarmblok |
| `lead_hours` | `4.0` | hoeveel uur vóór de deadline er mag worden opgewarmd |
| `allow_outside_window` | `true` | mag er ook buiten 07:00–22:00 worden opgewarmd? |
| `interval_minutes` | `5` | tijd tussen metingen/beslissingen |
| `response_timeout_seconds` | `8` | wachten op het antwoord van de gateway |
| `rows_refresh_seconds` | `1800` | hoe vaak prijzen/voorspelling worden opgehaald |
| `price_days_ahead` | `3` | prijshorizon |
| `qos` / `retain` | `1` / `false` | MQTT-optieken voor het setpoint-commando |

Gebruik:

```bash
# één beslissing, en publiceren (echte vattemperatuur via MQTT):
~/remkoverwarming/venv/bin/python3 dhw_comfort.py

# niets publiceren, alleen tonen:
~/remkoverwarming/venv/bin/python3 dhw_comfort.py --dry-run

# testen met een vaste temperatuur en een vast tijdstip:
~/remkoverwarming/venv/bin/python3 dhw_comfort.py \
    --temp 45 --now 2026-10-11T08:00:00+02:00 --dry-run

# als continue service:
~/remkoverwarming/venv/bin/python3 dhw_comfort.py --watch
```

Het commando op register 1082 heeft hetzelfde wire-formaat als de boost
(`{"FORCE_RESPONSE": true, "values": {"1082": "0208"}}` = 52 °C). Er wordt
alleen gepubliceerd als het setpoint **verandert**; de statusfile
`~/.cache/remko-wkf70/dhw_comfort_state.json` onthoudt het laatst gezette
setpoint én of er een opwarmblok loopt (ook na een herstart).

> **Belangrijk — niet tegelijk met `dhw_boost.py`.** Beide regelen register
> 1082, dus ze mogen **niet naast elkaar** draaien. Wil je de comfortregeling
> in plaats van de vaste boost, zet dan in `config.json`
> `mqtt.dhw_boost.enabled` op `false` en `mqtt.dhw_comfort.enabled` op `true`,
> en:
>
> ```bash
> sudo systemctl disable --now remko-sww-boost
> sudo systemctl enable --now remko-dhw-comfort
> journalctl -u remko-dhw-comfort -f
> ```

`deploy/install.sh` installeert de unit `remko-dhw-comfort` al mee, maar start
hem bewust **niet** automatisch (vanwege dat conflict). Handmatig:

```ini
# /etc/systemd/system/remko-dhw-comfort.service
[Unit]
Description=REMKO WKF SWW-comfortregeling (vat op peil 07:00-22:00 via MQTT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/remkoverwarming
ExecStart=/home/pi/remkoverwarming/venv/bin/python3 dhw_comfort.py --watch
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

## Energie-logger (`energy_log.py`) — echte COP meten

De REMKO-gateway publiceert **kWh-tellers** over MQTT. Die kun je gebruiken om
de COP van een SWW-opwarmepisode te *meten* in plaats van te schatten — en
daarmee de setpoint-vraag (53 °C vs. 48 °C vs. 45 °C) op échte kosten te
beslechten. `energy_log.py` draait als aparte service, leest periodiek de
tellers uit en schrijft een **tijdreeks-CSV**.

**Registermap** (bevestigd via de HA-integratie
[`Altrec/remko_mqtt-ha`](https://github.com/Altrec/remko_mqtt-ha); de waarden
komen overeen met de sensoren in Home Assistant):

| Register | Sensor | Betekenis |
|---|---|---|
| **5105** | Electr. energy heatpump | elektrisch verbruik (kWh, noemer van de COP) |
| **5374** | Energy heating | thermisch, ruimteverwarming (kWh) |
| **5376** | Energy DHW heating | thermisch, sanitair warm water (kWh) |
| 5600 | Environmental energy | warmte uit de buitenlucht (kWh) |
| 5001 | — | opmodus (`4` = SWW laden) |
| 5032 / 5039 | — | buitentemperatuur / boilertemperatuur |
| 5085 / 5190 / 1082 | — | heating-water req./actual, SWW-setpoint |
| 5822 | — | compressorstarts |

Per opwarmepisode geldt dan
`COP = Δ(Energy DHW heating) / Δ(Electr. energy heatpump)`.
Tijdens SWW-laden staat de ruimteverwarming doorgaans stil, dus de
elektrische delta is vrijwel volledig aan SWW toe te rekenen.

```bash
# verifieer eerst de verbinding en lees één sample (aanbevolen vóór de service):
~/remkoverwarming/venv/bin/python3 energy_log.py --once

# antwoordt de gateway niet? probeer dan de handshake-registers mee te sturen:
~/remkoverwarming/venv/bin/python3 energy_log.py --once --handshake

# twijfel je over topic/registers: 30 s alle MQTT-berichten onder de node tonen:
~/remkoverwarming/venv/bin/python3 energy_log.py --dump

# samenvatting per SWW-opwarmepisode (ΔDHW, ΔEl, COP, buitentemp, kosten, setpoint):
~/remkoverwarming/venv/bin/python3 energy_log.py --report
```

De CSV (`~/.cache/remko-wkf70/dhw_energy.csv`) heeft de vaste kolommen
`timestamp, energy_electric_kwh, energy_heating_kwh, energy_dhw_kwh, …,
opmode, out_temp_c, water_temp_c, …, price_eur_per_kwh` en groeit met
~1 regel/minuut (~100 kB/dag). Verandert de kolomset (bv. `log_price` aan/uit),
dan zet de logger het oude bestand opzij als `dhw_energy.csv.<tijd>.bak` en
begint met de nieuwe kolommen — de regels blijven zo altijd uitgelijnd.

### Kosten: 53 vs. 48 vs. 45 °C op échte cijfers

Met `log_price` (default aan) schrijft de logger bij elke sample de **all-in
stroomprijs** mee (dezelfde bron + `price_adjustments` als het advies). `--report`
rekent dan per episode de kosten uit (`Δelektrisch × tarief` van het bijbehorende
interval) én groepeert op **setpoint** (register 1082: de boost-temperatuur of de
basis-stand). Zo zie je na een paar weken direct welke strategie het goedkoopst
is:

```
  Totaal: ΔDHW 12.3 kWh  ΔEl 3.9 kWh  gem. COP 3.15  kosten €0.98 (gem. 0.251 €/kWh)
  Per setpoint:
        53 °C:   8 episodes  ΔDHW   12.3 kWh  ΔEl   3.9 kWh  COP 3.15  kosten €0.98
        45 °C:   9 episodes  ΔDHW   13.1 kWh  ΔEl   3.4 kWh  COP 3.85  kosten €0.92
```

Let op: lagere setpoint = hogere COP, maar de boiler koelt ook sneller af →
vaker spontaan herverwarmen. De **tijdreeks** (niet alleen de boosts) vangt die
extra cycli mee; daarom is een continue meting nodig in plaats van meten bij
blokstart/-einde. Zet `mqtt.energy_log.log_price` op `false` (of draai eenmalig
met `--no-price`) als je alleen energie wilt loggen.

> **Waarom niet per boost meten?** De 53-vs-45-afweging hangt juist af van de
> **spontane herverwarmingen** tússen onze blokken (bij een lage setpoint zakt
> de boiler vaker onder de drempel). Een continue tijdreeks vangt díe ook, een
> meting alleen bij blokstart/-einde niet.

**Protocoldetails.** De logger *subscribet* op `<node>/SMTID/HOST2CLIENT` en
stuurt elke `interval_seconds` een query naar `<node>/SMTID/CLIENT2HOST`:
`{"FORCE_RESPONSE": true, "query_list": [5105, 5374, …]}`. Standaard worden
géén registers geschreven (read-only); alleen met `include_handshake` /
`--handshake` gaan de drie handshake-registers mee die de HA-integratie ook
altijd meestuurt.

Draait op de Pi automatisch mee: `deploy/install.sh` installeert naast
`remko-sww-boost` ook de unit `remko-energy-log` (`energy_log.py`). Handmatig:

```ini
# /etc/systemd/system/remko-energy-log.service
[Unit]
Description=REMKO WKF energie-logger (MQTT -> CSV tijdreeks voor echte COP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/remkoverwarming
ExecStart=/home/pi/remkoverwarming/venv/bin/python3 energy_log.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now remko-energy-log
journalctl -u remko-energy-log -f
```

## Output & MQTT-topics

Tekstuitvoer toont per prijsslot: prijs, verwachte buitentemperatuur,
COP en de gecorrigeerde prijs, plus het beste blok van 3 uur (en top-N).
Is `heatpump.dhw.enabled` aan, dan komt daar een aparte sectie
**Sanitair warm water (SWW)** achteraan: een eigen per-slot tabel, het
beste 3-uursblok voor het opwarmen tot 53 °C (op basis van de SWW-COP)
én de **geplande SWW-boosts**: de (resterende) opwarmmomenten die
`dhw_boost` gaat uitsturen, telkens min. `min_gap_hours` uur na het
einde van de vorige, per kalenderdag één boost verplicht binnen het dagvenster
`afternoon_from`–`afternoon_to` (het blok wordt dan gemarkeerd met
`[dagvenster]`), met het laatste blok dat eindigt tussen
`last_block_end_from` en `last_block_end_to` terwijl élk blok start vóór
`last_block_end_to` (dus écht gespreid, over een rollend 24-uursvenster, en
nooit doorgeschoven naar een goedkopere dag verderop). Het plan volgt de
**statusfile van de watcher**: is er al een boost verstuurd, dan toont het
de resterende boosts van dát venster (niet opnieuw vanaf nul).

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

### Automatische fallback tussen prijsbronnen

Faalt de primaire prijsbron volledig (bv. ENTSO-E geeft op een ochtend nog
geen day-ahead data terwijl EnergyZero die dag al wél serveert), dan probeert
het advies automatisch de andere bron (`prices.fallback_source`, default
`"auto"`). Zo blijft de watcher plannen zodra de ene bron uitvalt — let op:
beide bronnen zijn dezelfde Nederlandse day-ahead-veiling, dus is de
veilinguitkomst zélf (nog) nergens gepubliceerd, dan faalt ook de fallback.
Een gebruikte fallback is altijd zichtbaar: de bronnaam in de output en een
waarschuwing in de logs ("bron 'entsoe' faalde (...); verder met 'energyzero'").
Zet `"fallback_source": false` om de fallback uit te zetten.

## API-toegang & -voorwaarden

- **ENTSO-E Transparency Platform**: gratis account + persoonlijke API-key
  (https://transparency.entsoe.eu). Authenticatie via `securityToken`.
  **Config-bestand met je key niet openbaar maken** (`.gitignore` opnemen).
- **met.no**: verplicht een herkenbare `User-Agent`; resultaten worden lokaal
  gecachet (TTL 10 min). Zie https://api.met.no/conditions_service/
- **EnergyZero**: publieke API van Energie Zero B.V. / EasyEnergy, gratis
  zonder key. Zie https://www.energyzero.nl/
- Respecteer cache-intervallen; loop geen eindeloze tests.