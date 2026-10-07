# SPP IMS Electricity Consumption Collector

[Slovensky](#spp-ims-zberac-spotreby-elektriny)

## Overview

A Python collector for 15-minute electricity consumption readings available through the SPP customer portal. It authenticates with OAuth 2.0 Authorization Code Flow and PKCE, resolves the configured delivery point by its EIC, fetches historical readings, calculates interval energy, and writes points to InfluxDB 1.8.

For each requested day, the collector expects 96 quarter-hour intervals. It reports missing intervals and writes any valid intervals it received, even when a day's dataset is incomplete. The InfluxDB measurement defaults to `spp_consumption`; points include `power_kw` and `energy_kwh` fields and `eic`, `contract_id`, `tariff`, and `source` tags.

Tariff classification uses local Europe/Bratislava time and the high-tariff windows configured in `spp_process.py`.

## Requirements

- Python 3.12 (the Docker image uses Python 3.12)
- Access to the SPP customer portal and the target EIC
- InfluxDB 1.8 reachable by the collector
- Docker Compose v2 for container deployment, or Python and pip for local execution

## Configuration

Create `config.json` beside `spp_process.py`. Start from [`config.json_example`](config.json_example):

```sh
cp config.json_example config.json
```

Edit the copied file before running the collector. Required settings:

| Setting | Description |
| --- | --- |
| `USER_EMAIL`, `USER_PASSWORD` | SPP customer portal credentials |
| `TARGET_EIC` | EIC of the delivery point to collect |
| `INFLUX_CONFIG.host`, `port`, `db`, `measurement` | InfluxDB connection and destination |
| `DEBUG_OUTPUT` | Enables verbose interval-level output when `true` |
| `DAYS_BACK` | Number of days to fetch |
| `END_DAYS_AGO` | How many days before today the range ends |
| `SCHEDULE_TIMES` | Non-empty array of local 24-hour `HH:MM` run times |

`INFLUX_CONFIG.username` and `INFLUX_CONFIG.password` are optional. If either is non-empty, the collector uses HTTP Basic Authentication; otherwise it connects anonymously. Example schedule and range settings are `SCHEDULE_TIMES: ["02:00", "14:00"]`, `DAYS_BACK: 5`, and `END_DAYS_AGO: 1`. That range fetches the five days from D-5 through D-1.

Keep `config.json` private. It is excluded from version control and mounted read-only in the Docker container. The program exits with an actionable message if the file is missing or invalid.

## Run With Python and Cron

Use this mode when you want the operating system's cron service to control when
ingestion runs. The script processes the configured date range once and exits;
cron starts a fresh process each time. `SCHEDULE_TIMES` is not used to schedule
these runs, but it must remain a non-empty array in `config.json`.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Add a cron entry, replacing `/path/to/spp` with the project directory. This
example runs at 02:00 and 14:00 in the host's local timezone:

```cron
0 2,14 * * * cd /path/to/spp && .venv/bin/python spp_process.py --process-once >> collector.log 2>&1
```

Ensure the host's timezone matches the intended schedule. Run manually with
`python spp_process.py --process-once`; show available options with
`python spp_process.py --help`.

## Run Continuously With Docker Compose

Use this mode when you want the collector to stay running and manage its own
schedule. After creating and editing `config.json`, start the container:

```sh
docker compose up -d --build
```

The container performs an ingestion at startup, then runs at each time in
`SCHEDULE_TIMES` using Europe/Bratislava time. No host cron entry is needed.
View logs or stop the service with:

```sh
docker compose logs -f spp-collector
docker compose down
```

## SPP IMS Zberač spotreby elektriny

## Prehľad

Python aplikácia na zber 15-minútových údajov o spotrebe elektriny dostupných cez zákaznícky portál SPP. Prihlasuje sa pomocou OAuth 2.0 Authorization Code Flow a PKCE, vyhľadá odberné miesto podľa nakonfigurovaného EIC, stiahne historické údaje, vypočíta energiu za interval a uloží body do InfluxDB 1.8.

Pre každý deň aplikácia očakáva 96 štvrťhodinových intervalov. Vypíše chýbajúce intervaly a uloží všetky prijaté platné intervaly, aj keď sú údaje za deň neúplné. Predvolený názov merania v InfluxDB je `spp_consumption`. Body obsahujú polia `power_kw` a `energy_kwh` a značky `eic`, `contract_id`, `tariff` a `source`.

Tarifné pásmo sa určuje podľa miestneho času v zóne Europe/Bratislava a časových okien vysokej tarify nastavených v súbore `spp_process.py`.

## Požiadavky

- Python 3.12 (Docker image používa Python 3.12)
- Prístup k zákazníckemu portálu SPP a EIC odberného miesta
- InfluxDB 1.8 dostupná zo zariadenia alebo kontajnera, v ktorom aplikácia beží
- Docker Compose v2 na spustenie kontajnera alebo Python a pip na lokálne spustenie

## Konfigurácia

Vytvorte súbor `config.json` vedľa `spp_process.py`. Ako základ použite [`config.json_example`](config.json_example):

```sh
cp config.json_example config.json
```

Pred spustením upravte skopírovaný súbor. Povinné nastavenia:

| Nastavenie | Popis |
| --- | --- |
| `USER_EMAIL`, `USER_PASSWORD` | Prihlasovacie údaje do zákazníckeho portálu SPP |
| `TARGET_EIC` | EIC odberného miesta, ktorého údaje sa majú zbierať |
| `INFLUX_CONFIG.host`, `port`, `db`, `measurement` | Pripojenie k InfluxDB a cieľová databáza/meranie |
| `DEBUG_OUTPUT` | Pri hodnote `true` vypisuje podrobné informácie o intervaloch |
| `DAYS_BACK` | Počet dní, za ktoré sa majú načítať údaje |
| `END_DAYS_AGO` | Počet dní pred dneškom, ktorým sa rozsah končí |
| `SCHEDULE_TIMES` | Neprázdne pole miestnych časov vo formáte 24-hodinového `HH:MM` |

`INFLUX_CONFIG.username` a `INFLUX_CONFIG.password` sú voliteľné. Ak je aspoň jedna z týchto hodnôt vyplnená, aplikácia použije HTTP Basic Authentication; inak sa pripojí bez autentifikácie. Príklad nastavení je `SCHEDULE_TIMES: ["02:00", "14:00"]`, `DAYS_BACK: 5` a `END_DAYS_AGO: 1`. Takýto rozsah načíta päť dní od D-5 po D-1.

Súbor `config.json` uchovávajte v bezpečí. Je vylúčený zo správy verzií a v Docker kontajneri sa pripája iba na čítanie. Ak súbor chýba alebo obsahuje neplatnú konfiguráciu, aplikácia skončí s vysvetľujúcou chybovou správou.

## Spustenie cez Python a cron

Tento režim použite, ak chcete, aby čas spustenia riadil systémový cron. Skript
spracuje nakonfigurovaný rozsah dát a skončí; cron pri každom spustení vytvorí
nový proces. `SCHEDULE_TIMES` sa v tomto režime nepoužíva na plánovanie, ale v
`config.json` musí zostať neprázdnym poľom.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Pridajte záznam do cronu a nahraďte `/path/to/spp` cestou k projektu. Tento
príklad spúšťa zber o 02:00 a 14:00 podľa miestneho času hostiteľa:

```cron
0 2,14 * * * cd /path/to/spp && .venv/bin/python spp_process.py --process-once >> collector.log 2>&1
```

Skontrolujte, či časové pásmo hostiteľa zodpovedá požadovanému plánu. Jednorazové
spustenie vykonáte príkazom `python spp_process.py --process-once`; dostupné
argumenty zobrazíte cez `python spp_process.py --help`.

## Nepretržitá prevádzka cez Docker Compose

Tento režim použite, ak má kontajner zostať spustený a sám plánovať zber. Po
vytvorení a úprave súboru `config.json` spustite:

```sh
docker compose up -d --build
```

Kontajner vykoná zber po štarte a potom v časoch uvedených v `SCHEDULE_TIMES`,
ktoré sa vyhodnocujú v časovom pásme Europe/Bratislava. Cron na hostiteľovi nie
je potrebný. Logy zobrazíte alebo službu zastavíte takto:

```sh
docker compose logs -f spp-collector
docker compose down
```
