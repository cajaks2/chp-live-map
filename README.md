# CHP Live Forest Map

Collect CHP CAD traffic incidents for Angeles National Forest roads and render a static live map with click-in details. The map defaults to a rolling 24-hour window: active incidents render red and cleared/non-active incidents render grey.

The CHP CAD site does not expose a documented public API for the detail logs this project needs, so `scrape_chp_traffic.py` follows the same public WebForms flow as the website:

1. Load `https://cad.chp.ca.gov/Traffic.aspx`.
2. Select one or more CHP communications centers.
3. Filter the active incident table to road keywords.
4. Open each matching incident's Details view.
5. Store current status and history in SQLite.

## Requirements

- Python 3.10+
- Network access to `https://cad.chp.ca.gov`
- No third-party Python packages

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

## Generate Map

```sh
python3 generate_live_map.py
```

This reads incidents from `chp_traffic.sqlite` and writes `live_chp_map.html`. Open that file in a browser to view markers and click through the detail log.

Render a different time window:

```sh
python3 generate_live_map.py --hours 12
```

## Container

Build:

```sh
docker build -t chp-live-map:latest .
```

Run locally with persistent SQLite/map output:

```sh
mkdir -p /mnt/data/chp_map
docker run --rm -p 8080:8080 -v /mnt/data/chp_map:/mnt/data/chp_map chp-live-map:latest
```

The container scrapes once per minute, stores SQLite at `/mnt/data/chp_map/chp_traffic.sqlite`, writes `/mnt/data/chp_map/live_chp_map.html`, and serves it on port `8080`.

## Kubernetes

Apply the manifest:

```sh
kubectl apply -f k8s/chp-live-map.yaml
```

The manifest creates:

- namespace `chp-live-map`
- persistent volume claim `chp-live-map-data`
- deployment `chp-live-map`
- cluster service `chp-live-map`

The pod mounts its persistent volume at `/mnt/data/chp_map` for the SQLite database and generated HTML.

## SQLite Tables

- `events`: one row per CHP incident, updated with current status and latest fields.
- `observations`: append-only status/detail snapshots when an incident is first seen, changes, or clears.
- `detail_entries`: normalized detail-log entries for each stored observation.
- `scrape_runs`: run metadata for basic monitoring.

Generated files such as `*.sqlite` and `live_chp_map.html` are intentionally ignored by git.
