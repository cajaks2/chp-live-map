# CHP Live Forest Map

Collect CHP CAD traffic incidents for Angeles National Forest roads and render a live map with click-in details, summary reports, searchable history, and source/cadence notes. The map defaults to a rolling 72-hour window: active incidents render red and cleared/non-active incidents render grey.

The CHP CAD site does not expose a documented public API for the detail logs this project needs, so `scrape_chp_traffic.py` follows the same public WebForms flow as the website:

1. Load `https://cad.chp.ca.gov/Traffic.aspx`.
2. Select one or more CHP communications centers.
3. Filter the active incident table to road keywords.
4. Open each matching incident's Details view.
5. Store current status and history in SQLite locally or Postgres in production deployments.

The scraper is intentionally conservative:

- It defaults to the Los Angeles communications center only.
- It filters the incident list by forest-road keywords before opening detail pages.
- It uses a descriptive `User-Agent` with a public project URL.
- Set `CHP_CONTACT_EMAIL` or pass `--contact-email` to include a contact address in that `User-Agent`.
- It checks `robots.txt` before scraping unless `--no-respect-robots` is set.
- It retries transient HTTP failures with exponential backoff.
- It skips detail-page refetches for unchanged active incidents for 3 minutes by default.
- It records both total CHP incidents seen and filtered incidents acquired in `scrape_runs`.

## Requirements

- Python 3.10+
- Network access to `https://cad.chp.ca.gov`
- `psycopg` for Postgres deployments; install with `pip install -r requirements.txt`

The generated map uses Leaflet and OpenStreetMap tiles from public CDNs.

## Scrape Incidents

Run once with the default Los Angeles center and Angeles Crest/Forest corridor keywords:

```sh
python3 scrape_chp_traffic.py
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
python3 serve_live_map.py --port 8080
```

## Web App Views

The dynamic server exposes four human-facing views. Each accepts `?hours=` and preserves the selected window while moving between views:

- `/`: live incident map with selectable incidents and copyable incident links.
- `/summary`: counts, busiest roads, incident types, and recent changes for the selected window.
- `/history`: searchable/filterable incident history with links back to the map. Use `?hours=720` for the 30-day window.
- `/about`: source, scrape cadence, coverage, and caveat notes.

Direct incident links use the `incident` query parameter:

```text
https://crestmap.us/?hours=720&incident=LACC%7C2026-06-02%7C2780
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
.venv/bin/python -m pytest --cov=scrape_chp_traffic --cov=generate_live_map --cov=serve_live_map --cov=ecs_logging --cov-report=term-missing
```

## Container

Build:

```sh
docker build -t chp-live-map:latest .
```

Run locally against SQLite:

```sh
docker run --rm -p 8080:8080 -v "$PWD:/data" chp-live-map:latest \
  python3 /app/serve_live_map.py --database /data/chp_traffic.sqlite
```

The default container command serves the dynamic web app on port `8080`. In Kubernetes, scraping is handled by a separate long-lived scraper Deployment that polls every minute and exposes metrics on port `8081`.

For the pushed Kubernetes image workflow, use the Makefile:

```sh
make deploy VERSION=0.1.69
```

That runs tests, builds and pushes `cajaks2/chp-live-map:<version>` for `linux/amd64`, updates the Kubernetes manifest image tags and `SERVICE_VERSION`, applies the manifest, waits for the web rollout, and verifies the public `/chp/` page.

Useful individual targets:

```sh
make build VERSION=0.1.69
make update-manifest VERSION=0.1.69
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

The public `crestmap.us` deployment can run directly on a single VM behind nginx, with `chp.flowy.us` retained as an alias:

```sh
cd /opt/chp-live-map
cp .env.example .env
docker compose up -d
```

The VM needs Docker Compose and `make` installed for the checked-in deployment helpers.

The Compose stack runs Postgres, the web app on `127.0.0.1:8080`, a long-lived scraper service that polls every minute and exposes scraper metrics on `127.0.0.1:8081`, and a Postgres backup sidecar. nginx should remain the TLS front door and proxy `crestmap.us` and `chp.flowy.us` to `http://127.0.0.1:8080`.

Backups are written as compressed custom-format `pg_dump` files under `/opt/chp-live-map/backups/postgres` every six hours by default. Tune `BACKUP_INTERVAL_SECONDS` and `BACKUP_RETENTION_DAYS` in `.env`.

Optional GA4 analytics can be enabled by setting `GOOGLE_ANALYTICS_ID` in `.env` to a Measurement ID such as `G-XXXXXXXXXX`. Leave it blank to omit the Google Analytics script entirely.

Files for that deployment live in `deploy/digitalocean/`.

For app-only updates after changing `VERSION` in `.env`, avoid restarting dependencies:

```sh
cd /opt/chp-live-map
make deploy VERSION=0.1.69
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
| `chp_live_map_history_window_hours` | gauge | The history-window size used for `/metrics` incident gauges. In production this is `72`, matching the default map view; user-selected `?hours=` values only affect that page/status request, not this process-level metric. |
| `chp_live_map_data_updated_timestamp_seconds` | gauge | Unix timestamp of the newest observed incident data included in the metrics window. |
| `chp_live_map_http_requests_total{method,route,status}` | counter | HTTP requests handled by the web process, grouped by method, coarse route, and status code. |
| `chp_live_map_scrape_last_run_timestamp_seconds` | gauge | Unix timestamp for the latest completed CHP scrape. |
| `chp_live_map_scrape_last_run_duration_seconds` | gauge | Duration of the latest completed CHP scrape. |
| `chp_live_map_scrape_last_run_incidents{kind}` | gauge | Latest scrape incident counts: total CHP incidents seen, matched incidents acquired, and mapped matched incidents. |
| `chp_live_map_scrape_last_run_observations_inserted` | gauge | Observation rows inserted by the latest scrape. |
| `chp_live_map_scrape_last_run_details{result}` | gauge | Detail pages requested or skipped by the latest scrape. |
| `chp_live_map_scrape_chp_http_requests_total{method,route,status}` | counter | Outbound requests made by the scraper to CHP, grouped by method, list/detail route, and response status. |
| `chp_live_map_scraper_up` | gauge | `1` when the scraper service metrics endpoint is running. |
| `chp_live_map_scraper_scrapes_total{outcome}` | counter | Scrape attempts by success/failure from the long-lived scraper process. |
| `chp_live_map_scraper_last_run_timestamp_seconds{outcome,error_type}` | gauge | Timestamp of the latest scraper run from the scraper service. |
| `chp_live_map_scraper_last_run_incidents{kind}` | gauge | Latest scraper-service incident counts. |
| `chp_live_map_scraper_chp_http_requests_total{method,route,status}` | counter | Outbound CHP HTTP requests counted in the scraper process. |

## SQL Tables

- `events`: one row per CHP incident, updated with current status and latest fields.
- `observations`: append-only status/detail snapshots when an incident is first seen, changes, or clears.
- `detail_entries`: normalized detail-log entries for each stored observation.
- `scrape_runs`: run metadata for monitoring, including total CHP incidents seen, filtered incidents acquired, detail-page fetch counts, scrape duration, and outbound CHP response-code counts.

Generated files such as `*.sqlite` and `live_chp_map.html` are intentionally ignored by git.
