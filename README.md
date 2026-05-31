# CHP Live Forest Map

Collect active CHP CAD traffic incidents for Angeles National Forest roads and render a static live map with click-in details.

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

This reads active incidents from `chp_traffic.sqlite` and writes `live_chp_map.html`. Open that file in a browser to view the current markers and click through the detail log.

## SQLite Tables

- `events`: one row per CHP incident, updated with current status and latest fields.
- `observations`: append-only status/detail snapshots when an incident is first seen, changes, or clears.
- `detail_entries`: normalized detail-log entries for each stored observation.
- `scrape_runs`: run metadata for basic monitoring.

Generated files such as `*.sqlite` and `live_chp_map.html` are intentionally ignored by git.
