# CHP Live Forest Map

Collect CHP CAD traffic incidents for Angeles National Forest roads and render a static live map with click-in details. The map defaults to a rolling 24-hour window: active incidents render red and cleared/non-active incidents render grey.

The CHP CAD site does not expose a documented public API for the detail logs this project needs, so `scrape_chp_traffic.py` follows the same public WebForms flow as the website:

1. Load `https://cad.chp.ca.gov/Traffic.aspx`.
2. Select one or more CHP communications centers.
3. Filter the active incident table to road keywords.
4. Open each matching incident's Details view.
5. Store current status and history in SQLite locally or Postgres in Kubernetes.

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

## SQL Tables

- `events`: one row per CHP incident, updated with current status and latest fields.
- `observations`: append-only status/detail snapshots when an incident is first seen, changes, or clears.
- `detail_entries`: normalized detail-log entries for each stored observation.
- `scrape_runs`: run metadata for basic monitoring.

Generated files such as `*.sqlite` and `live_chp_map.html` are intentionally ignored by git.
