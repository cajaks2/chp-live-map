# CHP Live Forest Map

Collect CHP CAD traffic incidents for Angeles National Forest roads and render a live map with click-in details, summary reports, searchable history, and source/cadence notes. The map defaults to a rolling 72-hour window: active incidents render red and cleared/non-active incidents render grey.

`scrape_chp_traffic.py` uses the public CHP media XML feed by default:

1. Fetch `https://media.chp.ca.gov/sa_xml/sa.xml`.
2. Filter active incidents to configured CHP centers, road keywords, and coordinate bounds.
3. Normalize incident, detail, and unit entries from the feed.
4. Store current status and history in SQLite locally or Postgres in production deployments.

The older CHP CAD WebForms scraper is still available with `--source-mode cad` for fallback/debugging, but production uses `CHP_SOURCE_MODE=xml`.

The scraper is intentionally conservative:

- It defaults to the Los Angeles and Ventura communications centers.
- It filters the incident list by forest/Malibu road keywords and coordinate bounds.
- It uses a descriptive `User-Agent` with a public project URL.
- Set `CHP_CONTACT_EMAIL` or pass `--contact-email` to include a contact address in that `User-Agent`.
- In CAD fallback mode, it checks `robots.txt` before scraping unless `--no-respect-robots` is set.
- It retries transient HTTP failures with exponential backoff.
- In CAD fallback mode, it skips detail-page refetches for unchanged active incidents for 3 minutes by default.
- It records both total CHP incidents seen and filtered incidents acquired in `scrape_runs`.

Default road keywords:

- `angeles crest`
- `angeles forest`
- `upper big tujunga`
- `big tujunga canyon`
- `mt wilson`, `mount wilson`, `mt wilson red box`
- `red box`
- `san gabriel canyon`
- scoped Highway 39 aliases: `highway 39`, `hwy 39`, `ca-39`, `ca 39`, `sr39`, `sr 39`
- `glendora mountain`
- `glendora ridge`
- `mt baldy`, `mount baldy`, `san antonio canyon`

Highway 39 aliases are only accepted when the CHP text also includes forest context such as San Gabriel Canyon, East Fork, Crystal Lake, Morris Reservoir, West Fork, Islip, or a mountain mile-marker. This avoids collecting far-south CA-39 incidents outside the forest.

Coordinates are also bounded to the forest area before map pins are shown. Incidents outside `34.15..34.56` latitude or `-118.36..-117.58` longitude stay in the list/history but are treated as unpinned.

The scraper also collects Malibu coast/canyon incidents into `region='malibu'`. The public web app defaults to `region=forest`, but users can switch to Malibu with the region selector or by linking `?region=malibu`. Malibu coordinates are bounded separately to `33.99..34.34` latitude and `-119.10..-118.45` longitude.

## Requirements

- Python 3.10+
- Network access to `https://media.chp.ca.gov`; CAD fallback mode also needs `https://cad.chp.ca.gov`
- `psycopg` for Postgres deployments; install with `pip install -r requirements.txt`

The generated map uses Leaflet and OpenStreetMap tiles from public CDNs.

## Scrape Incidents

Run once with the default Los Angeles/Ventura centers and Angeles Crest/Forest/Malibu corridor keywords:

```sh
python3 scrape_chp_traffic.py
```

Run the legacy CAD WebForms scraper instead:

```sh
python3 scrape_chp_traffic.py --source-mode cad
```

Poll every minute:

```sh
python3 scrape_chp_traffic.py --interval 60
```

Tune politeness controls:

```sh
python3 scrape_chp_traffic.py \
  --detail-delay 0.5 \
  --detail-refresh-minutes 3 \
  --retries 2 \
  --retry-backoff 2
```

Add or replace road keywords:

```sh
python3 scrape_chp_traffic.py --road "angeles crest" --road "sr-2" --road "upper big tujunga"
```

Add more CHP centers:

```sh
python3 scrape_chp_traffic.py --center LACC --center VTCC
```

By default the scraper writes `chp_traffic.sqlite`.

For Postgres:

```sh
DATABASE_URL=postgresql://chp_map:password@localhost:5432/chp_map python3 scrape_chp_traffic.py
```

## Generate Map

```sh
python3 generate_live_map.py
```

This reads incidents from `chp_traffic.sqlite` and writes `live_chp_map.html`. Open that file in a browser to view markers and click through the detail log.

Render a different time window:

```sh
python3 generate_live_map.py --hours 12
```

Serve dynamically from SQL instead of a prebuilt HTML file:

```sh
DATABASE=chp_traffic.sqlite .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8080
```

Production runs the same FastAPI app under gunicorn with a uvicorn worker.

## Web App Views

The dynamic server exposes four human-facing views. Each accepts `?hours=` and `?region=forest|malibu`, and preserves the selected window/region while moving between views:

- `/`: live incident map with selectable incidents and copyable incident links.
- `/summary`: counts, busiest roads, incident types, and recent changes for the selected window.
- `/history`: searchable/filterable incident history with links back to the map. Use `?hours=720` for the 30-day window.
- `/about`: source, scrape cadence, coverage, and caveat notes.

Direct incident links use the `incident` query parameter. Include `region` so links reopen in the same dataset:

```text
https://crestmap.us/?region=forest&hours=720&incident=LACC%7C2026-06-02%7C2780
```

If the linked incident is older than the default 72-hour map window, keep the wider `hours` value in the URL so the map loads that incident into its dataset.

## Tests

Install development dependencies and run the unit suite:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Run with statement coverage:

```sh
.venv/bin/python -m pytest --cov=scrape_chp_traffic --cov=generate_live_map --cov=serve_live_map --cov=app --cov=ecs_logging --cov-report=term-missing
```

## Container

Build:

```sh
docker build -t chp-live-map:latest .
```

Run locally against SQLite:

```sh
docker run --rm -p 8080:8080 -v "$PWD:/data" chp-live-map:latest \
  sh -c 'DATABASE=/data/chp_traffic.sqlite exec gunicorn app:app -k uvicorn.workers.UvicornWorker --workers 1 --bind 0.0.0.0:8080 --access-logfile /dev/null --error-logfile -'
```

The default container command serves the dynamic FastAPI web app through gunicorn on port `8080`. In Kubernetes, scraping is handled by a separate long-lived scraper Deployment that polls every minute and exposes metrics on port `8081`.

For the pushed Kubernetes image workflow, use the Makefile:

```sh
make deploy VERSION=0.1.90
```

That runs tests, builds and pushes `cajaks2/chp-live-map:<version>` for `linux/amd64`, updates the Kubernetes manifest image tags and `SERVICE_VERSION`, applies the manifest, waits for the web rollout, and verifies the public `crestmap.us` page.

Useful individual targets:

```sh
make build VERSION=0.1.90
make update-manifest VERSION=0.1.90
make apply
make rollout
make verify
make k8s-status
```

## Kubernetes

Apply the manifest:

```sh
kubectl apply -f k8s/chp-live-map.yaml
```

The manifest creates:

- namespace `chp-live-map`
- secret `chp-live-map-db`
- PVC `chp-live-map-postgres-data`
- Postgres StatefulSet and service
- scraper Deployment that runs continuously, polls every minute, and exposes metrics
- web Deployment and service

Edit `POSTGRES_PASSWORD` and `DATABASE_URL` in the manifest before using it outside a local/private cluster.

## DigitalOcean Docker Compose

The public `crestmap.us` deployment can run directly on a single VM behind nginx:

```sh
cd /opt/chp-live-map
cp .env.example .env
docker compose up -d
```

The VM needs Docker Compose and `make` installed for the checked-in deployment helpers.

The Compose stack runs Postgres, the web app on `127.0.0.1:8080`, a long-lived XML-mode scraper service that polls every minute and exposes scraper metrics on `127.0.0.1:8081`, and a Postgres backup sidecar. nginx should remain the TLS front door and proxy `crestmap.us` to `http://127.0.0.1:8080`.

Backups are written as compressed custom-format `pg_dump` files under `/opt/chp-live-map/backups/postgres` every six hours by default. Tune `BACKUP_INTERVAL_SECONDS` and `BACKUP_RETENTION_DAYS` in `.env`.

Optional GA4 analytics can be enabled by setting `GOOGLE_ANALYTICS_ID` in `.env` to a Measurement ID such as `G-XXXXXXXXXX`. Leave it blank to omit the Google Analytics script entirely.

For Postgres-backed deployments, the web service uses a small connection pool. Tune `DATABASE_POOL_MIN` and `DATABASE_POOL_MAX` in `.env`; production defaults are `1` and `5`. `WEB_WORKERS` controls gunicorn worker count and defaults to `1` so process-local Prometheus counters and DB pool sizing remain predictable. If workers are raised later, total possible Postgres connections become `WEB_WORKERS * DATABASE_POOL_MAX`.

Files for that deployment live in `deploy/digitalocean/`.

For app-only updates after changing `VERSION` in `.env`, avoid restarting dependencies:

```sh
cd /opt/chp-live-map
make deploy VERSION=0.1.90
```

The `deploy/digitalocean/Makefile` wraps common VM operations:

```sh
make ps
make health
make logs-web
make backup
```

The checked-in helper `deploy/digitalocean/deploy-compose.sh` runs `make deploy`. The deploy target uses `docker compose up -d --no-deps web scrape` so Postgres stays running during normal web/scraper deploys and the visible site interruption window is smaller.

The web service also exposes:

- `/status.json?hours=72`: lightweight status/version check used by the browser to decide whether a refresh is useful.
- `/incidents.json?hours=72`: JSON payload for the selected incident window. This is the current compatibility endpoint and exposes the internal incident row shape.
- `?region=malibu`: public Malibu coast/canyon dataset selector supported by the map, summary, history, about, `/status.json`, and `/incidents.json`.
- Web `/metrics`: Prometheus text-format metrics for web process uptime, incident counts, data freshness, HTTP request counters, and DB-backed latest scrape data.
- Scraper `:8081/metrics`: Prometheus text-format metrics emitted by the long-lived scraper service, including scrape attempt counters and outbound CHP response-code counters.

A formal `/api/v1/...` API with stable envelopes, per-incident endpoints, pagination, and schema documentation is planned but not implemented yet.

Prometheus metrics:

| Metric | Type | Meaning |
| --- | --- | --- |
| `chp_live_map_up` | gauge | `1` when the web process can render metrics. |
| `chp_live_map_process_start_time_seconds` | gauge | Unix timestamp for the current web process start time. |
| `chp_live_map_incidents{status="total"}` | gauge | Incident count in the default map history window. |
| `chp_live_map_incidents{status="active"}` | gauge | Active incident count in the default map history window. |
| `chp_live_map_incidents{status="cleared"}` | gauge | Cleared incident count in the default map history window. |
| `chp_live_map_incidents{status="mapped"}` | gauge | Incidents with coordinates in the default map history window. |
| `chp_live_map_region_incidents{region,status}` | gauge | Incident counts in the default map history window, grouped by hidden collection region such as `forest` or `malibu`. |
| `chp_live_map_history_window_hours` | gauge | The history-window size used for `/metrics` incident gauges. In production this is `72`, matching the default map view; user-selected `?hours=` values only affect that page/status request, not this process-level metric. |
| `chp_live_map_data_updated_timestamp_seconds` | gauge | Unix timestamp of the newest observed incident data included in the metrics window. |
| `chp_live_map_http_requests_total{method,route,status}` | counter | HTTP requests handled by the web process, grouped by method, coarse route, and status code. |
| `chp_live_map_db_pool_connections{state}` | gauge | Web Postgres pool connections by `min`, `max`, `size`, `available`, and derived `in_use` states. |
| `chp_live_map_db_pool_requests_waiting` | gauge | Web requests currently waiting for a Postgres pool connection. |
| `chp_live_map_scrape_last_run_timestamp_seconds` | gauge | Unix timestamp for the latest completed CHP scrape. |
| `chp_live_map_scrape_last_run_duration_seconds` | gauge | Duration of the latest completed CHP scrape. |
| `chp_live_map_scrape_last_run_incidents{kind}` | gauge | Latest scrape incident counts: total CHP incidents seen, matched incidents acquired, and mapped matched incidents. |
| `chp_live_map_scrape_last_run_observations_inserted` | gauge | Observation rows inserted by the latest scrape. |
| `chp_live_map_scrape_last_run_details{result}` | gauge | Detail pages requested or skipped by the latest scrape. |
| `chp_live_map_scrape_chp_http_requests_total{method,route,status}` | counter | Outbound requests made by the scraper to CHP, grouped by method, list/detail route, and response status. |
| `chp_live_map_scraper_up` | gauge | `1` when the scraper service metrics endpoint is running. |
| `chp_live_map_scraper_scrapes_total{outcome}` | counter | Scrape attempts by success/failure from the long-lived scraper process. |
| `chp_live_map_scraper_source_attempts_total{source,mode,outcome}` | counter | Source attempts from the scraper process. `source` is `xml` or `cad`; `mode` is `primary` or `fallback`; `outcome` is `success` or `failure`. |
| `chp_live_map_scraper_xml_feed_age_seconds{timestamp_source}` | gauge | Age in seconds of the media XML feed timestamp from the latest XML freshness check. `timestamp_source` is usually `http_last_modified`; it falls back to `incident_timestamp` if the header is absent. XML is treated as stale after `CHP_XML_MAX_AGE_MINUTES`, default `5`, and CAD is used as fallback. |
| `chp_live_map_scraper_xml_feed_timestamp_seconds{timestamp_source}` | gauge | Unix timestamp for the media XML feed timestamp used by the latest XML freshness check. |
| `chp_live_map_scraper_last_run_timestamp_seconds{outcome,error_type}` | gauge | Timestamp of the latest scraper run from the scraper service. |
| `chp_live_map_scraper_last_run_duration_seconds` | gauge | Total duration of the latest scraper-service run. |
| `chp_live_map_scraper_last_run_source_duration_seconds{source}` | gauge | Latest scraper-service fetch/runtime duration by source, currently `xml` or `cad`. |
| `chp_live_map_scraper_last_run_source_response_bytes{source}` | gauge | Bytes downloaded by the latest scraper-service run by source. |
| `chp_live_map_scraper_last_run_incidents{kind}` | gauge | Latest scraper-service incident counts. |
| `chp_live_map_scraper_chp_http_requests_total{method,route,status}` | counter | Outbound CHP HTTP requests counted in the scraper process. |

## SQL Tables

- `events`: one row per CHP incident, updated with current status and latest fields.
- `observations`: append-only status/detail snapshots when an incident is first seen, changes, or clears.
- `detail_entries`: normalized detail-log entries for each stored observation.
- `scrape_runs`: run metadata for monitoring, including total CHP incidents seen, filtered incidents acquired, detail-page fetch counts, scrape duration, and outbound CHP response-code counts.

Generated files such as `*.sqlite` and `live_chp_map.html` are intentionally ignored by git.
