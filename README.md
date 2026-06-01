# CHP Live Forest Map

Collect CHP CAD traffic incidents for Angeles National Forest roads and render a static live map with click-in details. The map defaults to a rolling 72-hour window: active incidents render red and cleared/non-active incidents render grey.

The CHP CAD site does not expose a documented public API for the detail logs this project needs, so `scrape_chp_traffic.py` follows the same public WebForms flow as the website:

1. Load `https://cad.chp.ca.gov/Traffic.aspx`.
2. Select one or more CHP communications centers.
3. Filter the active incident table to road keywords.
4. Open each matching incident's Details view.
5. Store current status and history in SQLite locally or Postgres in Kubernetes.

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

The default container command serves the dynamic web app on port `8080`. In Kubernetes, scraping is handled by a separate CronJob.

For the pushed Kubernetes image workflow, use the Makefile:

```sh
make deploy VERSION=0.1.5
```

That runs tests, builds and pushes `cajaks2/chp-live-map:<version>` for `linux/amd64`, updates the Kubernetes manifest image tags and `SERVICE_VERSION`, applies the manifest, waits for the web rollout, and verifies the public `/chp/` page.

Useful individual targets:

```sh
make build VERSION=0.1.5
make update-manifest VERSION=0.1.5
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
- scraper CronJob that runs every minute
- web Deployment and service

Edit `POSTGRES_PASSWORD` and `DATABASE_URL` in the manifest before using it outside a local/private cluster.

## DigitalOcean Docker Compose

The public `crestmap.us` deployment can run directly on a single VM behind nginx, with `chp.flowy.us` retained as an alias:

```sh
cd /opt/chp-live-map
cp .env.example .env
docker compose up -d
```

The Compose stack runs Postgres, the web app on `127.0.0.1:8080`, a scraper loop that polls every minute, and a Postgres backup sidecar. nginx should remain the TLS front door and proxy `crestmap.us` and `chp.flowy.us` to `http://127.0.0.1:8080`.

Backups are written as compressed custom-format `pg_dump` files under `/opt/chp-live-map/backups/postgres` every six hours by default. Tune `BACKUP_INTERVAL_SECONDS` and `BACKUP_RETENTION_DAYS` in `.env`.

Files for that deployment live in `deploy/digitalocean/`.

The web service also exposes:

- `/status.json`: lightweight status/version check used by the browser to decide whether a refresh is useful.
- `/metrics`: Prometheus text-format metrics for process uptime, incident counts, data freshness, and HTTP request counters.

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

## SQL Tables

- `events`: one row per CHP incident, updated with current status and latest fields.
- `observations`: append-only status/detail snapshots when an incident is first seen, changes, or clears.
- `detail_entries`: normalized detail-log entries for each stored observation.
- `scrape_runs`: run metadata for basic monitoring.

Generated files such as `*.sqlite` and `live_chp_map.html` are intentionally ignored by git.
