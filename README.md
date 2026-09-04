# Crestmap Live Incident Map

Collect CHP traffic incidents and matching WildWeb dispatch reports for Angeles National Forest and Malibu roads, then render a live map with click-in details, summary reports, searchable history, and source/cadence notes. The map defaults to a rolling 72-hour window: active CHP incidents render red, current WildWeb reports render amber, and cleared/archived records render grey.

`scrape_chp_traffic.py` uses the public CHP media XML feed by default:

1. Fetch `https://media.chp.ca.gov/sa_xml/sa.xml`.
2. Filter active incidents to configured CHP centers, road keywords, and coordinate bounds.
3. Normalize incident, detail, and unit entries from the feed.
4. Store current status and history in SQLite locally or Postgres in production deployments.

The older CHP CAD WebForms scraper is still available with `--source-mode cad` for fallback/debugging, but production uses `CHP_SOURCE_MODE=xml`.

`scrape_wildweb_incidents.py` polls the public CAANCC incident data used by [WildWeb](https://www.wildwebe.net/incidents?dc_Name=CAANCC) as a separate source. It stores WildWeb's stable incident UUID, source report time, source status, and source URL. It uses the same Forest and Malibu coordinate boundaries as the CHP collector. A report with coordinates outside those boundaries is rejected; a report without coordinates is included only when its road or a controlled list of known places matches one of the two regions. Unpinned reports remain available in the incident list and history.

WildWeb status is intentionally conservative:

- A currently listed record is `Reported`, or `Contained`/`Controlled` when WildWeb explicitly supplies that fire status.
- Only an explicit WildWeb `Out` timestamp is displayed as `Out`.
- A record that disappears from a successful feed response becomes `No longer listed`; this does not mean Crestmap confirmed it is over.
- A record older than `WILDWEB_MAX_AGE_HOURS` becomes `Archived`.
- The source's incident `date` is interpreted as Pacific local time. A feed-level `retrieved` timestamp without a time zone is interpreted as UTC.

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
- Network access to the WildWeb CAANCC incident data endpoint used by `wildwebe.net`
- `psycopg` for Postgres deployments; install with `pip install -r requirements.txt`

The generated map uses Leaflet and OpenStreetMap tiles from public CDNs.

See [CHANGELOG.md](CHANGELOG.md) for release notes reconstructed from the Git
history and deployment versions.

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

Run the WildWeb collector once, or poll every two minutes:

```sh
python3 scrape_wildweb_incidents.py
python3 scrape_wildweb_incidents.py --interval 120
```

WildWeb defaults to CAANCC and a 72-hour source window. Configure it with `WILDWEB_INTERVAL_SECONDS`, `WILDWEB_MAX_AGE_HOURS`, `WILDWEB_USER_AGENT`, and `WILDWEB_PUSH_NOTIFICATIONS`. `WILDWEB_METRICS_HOST` and `WILDWEB_METRICS_PORT` expose the same provider-labeled scraper metric families used by the CHP collector. Production collection enables WildWeb delivery, but each browser subscription remains CHP-only unless the user explicitly selects **WildWeb reports** in Alerts. The toggle warns that the two sources may describe the same incident.

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
.venv/bin/python -m pytest --cov=scrape_chp_traffic --cov=scrape_wildweb_incidents --cov=generate_live_map --cov=serve_live_map --cov=app --cov=ecs_logging --cov-report=term-missing
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

The default container command serves the dynamic FastAPI web app through gunicorn on port `8080`. In Kubernetes, CHP scraping, WildWeb polling, and rescue-aircraft tracking run as separate long-lived Deployments so any upstream feed can fail independently.

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
- CHP scraper Deployment that runs continuously, polls every minute, and exposes metrics
- WildWeb collector Deployment that runs continuously, polls every two minutes, and exposes metrics
- OpenSky aircraft tracker Deployment that polls every 30 seconds
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

The Compose stack runs Postgres, the web app on `127.0.0.1:8080`, separate long-lived CHP and WildWeb collectors, an OpenSky aircraft tracker, and a Postgres backup sidecar. nginx should remain the TLS front door and proxy `crestmap.us` to `http://127.0.0.1:8080`.

### LASD rescue-aircraft tracking

The optional map layer tracks verified LA County public-safety helicopters. The LASD rescue fleet includes N950JE, N951LB, and N952JH. The LA County Fire fleet includes N110LA, N120LA, N14LA, N15LA, N160LA, N17LA, N18LA, N190LA, N821LA, and N822LA, plus temporary Copter 17 registration N133LA during its 2026 transition. LACoFD describes this fleet as supporting paramedic transport, hoist rescue, and wildland firefighting; the two agencies are labeled separately in map popups.

Positions are fetched server-side from OpenSky and delayed by 60 seconds. A helicopter fades when its latest delayed position is more than five minutes old and disappears after 15 minutes; while it is visible, the map shows up to 30 minutes of delayed trail history. Configure the tracker with:

```sh
OPENSKY_CLIENT_ID=your-client-id
OPENSKY_CLIENT_SECRET=your-client-secret
AIRCRAFT_TRACKING_ENABLED=true
AIRCRAFT_POLL_SECONDS=30
AIRCRAFT_MAX_AGE_SECONDS=900
AIRCRAFT_TRAIL_AGE_SECONDS=1800
AIRCRAFT_RETENTION_HOURS=24
```

The browser reads delayed positions from `/api/v1/aircraft`; OpenSky credentials are never sent to clients. The tracker deletes database positions older than `AIRCRAFT_RETENTION_HOURS` after each successful poll.

Backups are written as compressed custom-format `pg_dump` files under `/opt/chp-live-map/backups/postgres` every six hours by default. Tune `BACKUP_INTERVAL_SECONDS` and `BACKUP_RETENTION_DAYS` in `.env`.

Optional GA4 analytics can be enabled by setting `GOOGLE_ANALYTICS_ID` in `.env` to the installation tag ID supplied by Google, such as `G-XXXXXXXXXX`. Leave it blank to omit the Google Analytics script entirely. See [Analytics operations](docs/analytics.md) for interaction events, pageview settings, the reporting property, and internal/developer test modes.

For Postgres-backed deployments, the web service uses a small connection pool. Tune `DATABASE_POOL_MIN` and `DATABASE_POOL_MAX` in `.env`; production defaults are `1` and `5`. `WEB_WORKERS` controls gunicorn worker count and defaults to `1` so process-local Prometheus counters and DB pool sizing remain predictable. If workers are raised later, total possible Postgres connections become `WEB_WORKERS * DATABASE_POOL_MAX`.

### Web Push notifications

The app supports standards-based Web Push, including Home Screen web apps on iOS and iPadOS 16.4 or newer. Subscribers can select Forest or Malibu roads and collision, hazard, closure/weather, or other incident categories. Subscriptions are anonymous and stored in the application database. The scraper queues and deduplicates delivery when it discovers a new incident.

Configure one stable VAPID key pair in the deployment environment:

```text
VAPID_PUBLIC_KEY=base64url-uncompressed-public-key
VAPID_PRIVATE_KEY=base64url-32-byte-private-key
VAPID_SUBJECT=mailto:ops@example.com
```

The public key is provided only to the web service; the private key is provided only to collector services that send notifications. Keep the same pair across deployments or existing browser subscriptions will need to be recreated. On iPhone and iPad, users add Crestmap to the Home Screen from Safari, open the Home Screen app, and tap **Alerts** before iOS will offer notification permission.

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

The checked-in helper `deploy/digitalocean/deploy-compose.sh` runs `make deploy`. The deploy target uses `docker compose up -d --no-deps web scrape wildweb aircraft` so Postgres stays running during normal app deploys and the visible site interruption window is smaller.

The web service also exposes:

- `/status.json?hours=72`: lightweight status/version check used by the browser to decide whether a refresh is useful.
- `/incidents.json?hours=72`: JSON payload for the selected incident window. This is the current compatibility endpoint and exposes the internal incident row shape.
- `?region=malibu`: public Malibu coast/canyon dataset selector supported by the map, summary, history, about, `/status.json`, and `/incidents.json`.
- Web `/metrics`: Prometheus text-format metrics for web process uptime, incident counts, data freshness, HTTP request counters, and DB-backed latest scrape data.
- CHP scraper `:8081/metrics`: provider-labeled Prometheus metrics emitted by the long-lived CHP collector, including scrape attempt counters and outbound CHP response-code counters.
- WildWeb scraper `:8082/metrics`: the same generic `chp_live_map_scraper_*` families with `provider="wildweb"` and `source="api"`.

Merge `deploy/digitalocean/prometheus-scrape.yml` into the host Prometheus `scrape_configs` so both collector targets are ingested. The checked-in Grafana dashboard includes a `provider` filter and keeps XML/CAD-only panels scoped to CHP.

Comment API:

```text
GET  /api/v1/incidents/{event_key}/comments
POST /api/v1/incidents/{event_key}/comments
```

The public `GET` endpoint returns approved comments. The public `POST` endpoint accepts anonymous comments and publishes them immediately by default. Comment bodies are plain text only, stripped of HTML, capped at 750 characters, and protected by a honeypot plus IP/user-agent rate limits. Optional contact information is stored for moderation but is never returned by the public API. Set `COMMENT_AUTO_APPROVE=false` to restore pre-publication moderation; this affects future submissions only.

Comments can also include up to three photos or one short MP4 video. Photos are resized to a
maximum 1920-pixel edge and converted to WebP in the visitor's browser before upload. Videos are
not encoded or transcoded; the browser verifies that they are playable MP4 files and enforces the
configured duration limit. Attachments upload directly to a private Cloudflare R2 bucket and inherit
the associated comment's moderation status when finalized. Rejecting or deleting a comment also
removes its objects from R2.

Enable uploads with an R2 API token that has Object Read & Write permission for the private bucket:

```bash
R2_ACCOUNT_ID=your-cloudflare-account-id
R2_ACCESS_KEY_ID=your-r2-access-key-id
R2_SECRET_ACCESS_KEY=your-r2-secret-access-key
R2_BUCKET=crestmap-media
R2_UPLOAD_TOKEN_SECRET=replace-with-an-independent-long-random-secret
```

Apply the bucket CORS policy in `deploy/cloudflare/r2-cors.json`, which allows signed browser reads
and direct PUTs from `https://crestmap.us`. Keep the bucket private; the app issues short-lived signed URLs for
uploads, moderator previews, and approved public media. If any required R2 variable is unset, the
media picker and upload endpoints remain disabled while text comments continue to work.

Moderate comments from the container or a local checkout:

```bash
python manage_comments.py list --status pending
python manage_comments.py approve 123
python manage_comments.py reject 123
python manage_comments.py delete 123
```

Or enable the web moderation GUI by setting both `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
When either value is unset, the admin routes return 404. When enabled, the public navigation
includes an admin login. A successful login creates a signed, HttpOnly, same-site session
cookie used by the comment moderator and the normal incident views. HTTP Basic auth remains
available for scripts. Authenticated requests keep the same public map, summary, history,
and about URLs while adding moderation navigation and protected incident options.

```text
GET  /admin/login
GET  /admin/comments
GET  /api/v1/incidents/{event_key}/hidden-details
```

On the DigitalOcean compose host, store the credentials in `/opt/chp-live-map/.env`
instead of committing them:

```bash
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-with-a-long-random-password
ADMIN_SESSION_SECRET=replace-with-a-separate-long-random-secret
ADMIN_SESSION_HOURS=8
ADMIN_SESSION_MAX_HOURS=24
ADMIN_REMEMBER_DAYS=30
```

### Admin sessions and remembered devices

- A normal login expires after `ADMIN_SESSION_HOURS` without interaction (8 hours
  by default). Interacting with an admin-enabled map, report, moderation page, or
  sessions page renews it, up to `ADMIN_SESSION_MAX_HOURS` from login (24 hours by
  default). Activity renewal is throttled to once per five minutes.
- Checking **Remember this device** at login keeps that browser signed in for up
  to `ADMIN_REMEMBER_DAYS` (30 days by default), including after closing and
  reopening it. This is a fixed maximum, not an indefinitely rolling login.
- Automatic incident/status polling, open background tabs, and passive page loads
  never extend a session. Only trusted interaction events in a visible page send
  the same-origin activity request.
- To use a longer fixed normal session instead, set both `ADMIN_SESSION_HOURS`
  and `ADMIN_SESSION_MAX_HOURS` to the same value, such as `168` for seven days.
- The DigitalOcean web service passes all three settings through from `.env`;
  Kubernetes sets them explicitly in the web Deployment.
- **Admin tools → Sessions / remembered devices** lists browser sessions and lets
  you revoke one, log out all other devices, or log out everywhere. Revocation is
  enforced on the next request; a copied cookie cannot revive a revoked session.
  The ordinary **Log out** button also revokes its server-side session.
- Session records live in SQLite/Postgres so they survive app restarts. Only a
  hash of each signed token is stored, along with timestamps and a truncated
  browser user-agent description. Cookies remain HttpOnly, SameSite=Strict, and
  Secure when served over HTTPS. Expired rows are cleaned up at login.
- Changing the admin username, password, or signing secret invalidates existing
  browser cookies. HTTP Basic authentication remains available for existing admin
  API clients and is not a remembered browser session; revoke that access by
  changing the credential.

The session-store upgrade adds the `admin_sessions` table to both database
schemas and requires existing admins to sign in once again. Take a database
backup before deploying the migration. No credentials are changed by the upgrade.

Prometheus metrics:

| Metric | Type | Meaning |
| --- | --- | --- |
| `chp_live_map_up` | gauge | `1` when the web process can render metrics. |
| `chp_live_map_process_start_time_seconds` | gauge | Unix timestamp for the current web process start time. |
| `chp_live_map_incidents{status="total"}` | gauge | Incident count in the default map history window. |
| `chp_live_map_incidents{status="active"}` | gauge | Active incident count in the default map history window. |
| `chp_live_map_incidents{status="reported"}` | gauge | Current WildWeb report count in the default map history window. |
| `chp_live_map_incidents{status="cleared"}` | gauge | Cleared incident count in the default map history window. |
| `chp_live_map_incidents{status="mapped"}` | gauge | Incidents with coordinates in the default map history window. |
| `chp_live_map_region_incidents{region,status}` | gauge | Incident counts in the default map history window, grouped by hidden collection region such as `forest` or `malibu`. |
| `chp_live_map_history_window_hours` | gauge | The history-window size used for `/metrics` incident gauges. In production this is `72`, matching the default map view; user-selected `?hours=` values only affect that page/status request, not this process-level metric. |
| `chp_live_map_data_updated_timestamp_seconds` | gauge | Unix timestamp of the newest observed incident data included in the metrics window. |
| `chp_live_map_http_requests_total{method,route,status}` | counter | HTTP requests handled by the web process, grouped by method, coarse route, and status code. |
| `chp_live_map_db_pool_connections{state}` | gauge | Web Postgres pool connections by `min`, `max`, `size`, `available`, and derived `in_use` states. |
| `chp_live_map_db_pool_requests_waiting` | gauge | Web requests currently waiting for a Postgres pool connection. |
| `chp_live_map_comments_submitted_total{outcome}` | counter | Comment submissions grouped by outcome such as `pending`, `rate_limited`, `honeypot`, or validation errors. |
| `chp_live_map_comments_pending` | gauge | Comments currently waiting for moderation. |
| `chp_live_map_push_subscriptions{status}` | gauge | Stored Web Push subscriptions, split into active and inactive records. |
| `chp_live_map_push_subscription_sources{source}` | gauge | Active subscriptions selecting CHP or WildWeb alerts. |
| `chp_live_map_push_subscription_areas{area}` | gauge | Active subscriptions selecting Forest, Crest/west, or Malibu. |
| `chp_live_map_push_subscription_categories{category}` | gauge | Active subscriptions selecting each incident category. |
| `chp_live_map_push_notification_events{region,category,status}` | gauge | Incident notification events split into pending and completed queue states. |
| `chp_live_map_push_deliveries{region,category,status}` | gauge | Incident push deliveries split into pending, delivered, and failed outcomes. |
| `chp_live_map_push_delivery_attempts{region,category}` | gauge | Total stored incident push attempts by region and category. |
| `chp_live_map_push_test_notifications{status}` | gauge | Test notifications split into pending, delivered, and failed outcomes. |
| `chp_live_map_push_last_delivery_timestamp_seconds` | gauge | Unix timestamp of the latest successful incident push delivery. |
| `chp_live_map_push_last_test_delivery_timestamp_seconds` | gauge | Unix timestamp of the latest successful test push delivery. |
| `chp_live_map_scrape_last_run_timestamp_seconds` | gauge | Unix timestamp for the latest completed CHP scrape. |
| `chp_live_map_scrape_last_run_duration_seconds` | gauge | Duration of the latest completed CHP scrape. |
| `chp_live_map_scrape_last_run_incidents{kind}` | gauge | Latest scrape incident counts: total CHP incidents seen, matched incidents acquired, and mapped matched incidents. |
| `chp_live_map_scrape_last_run_observations_inserted` | gauge | Observation rows inserted by the latest scrape. |
| `chp_live_map_scrape_last_run_details{result}` | gauge | Detail pages requested or skipped by the latest scrape. |
| `chp_live_map_scrape_chp_http_requests_total{method,route,status}` | counter | Outbound requests made by the scraper to CHP, grouped by method, list/detail route, and response status. |
| `chp_live_map_scraper_up{provider}` | gauge | `1` when a scraper metrics endpoint is running. `provider` is `chp` or `wildweb`. |
| `chp_live_map_scraper_scrapes_total{provider,outcome}` | counter | Scrape attempts by provider and success/failure. |
| `chp_live_map_scraper_source_attempts_total{provider,source,mode,outcome}` | counter | Source attempts from each collector. CHP uses `xml` or `cad`; WildWeb uses `api`. |
| `chp_live_map_scraper_xml_feed_age_seconds{provider,timestamp_source}` | gauge | CHP-only age in seconds of the media XML feed timestamp from the latest XML freshness check. `timestamp_source` is usually `http_last_modified`; it falls back to `incident_timestamp` if the header is absent. XML is treated as stale after `CHP_XML_MAX_AGE_MINUTES`, default `5`, and CAD is used as fallback. |
| `chp_live_map_scraper_xml_feed_timestamp_seconds{provider,timestamp_source}` | gauge | CHP-only Unix timestamp for the media XML feed timestamp used by the latest XML freshness check. |
| `chp_live_map_scraper_last_run_timestamp_seconds{provider,outcome,error_type}` | gauge | Timestamp of the latest run for each scraper provider. |
| `chp_live_map_scraper_last_run_duration_seconds{provider}` | gauge | Total duration of each provider's latest scraper run. |
| `chp_live_map_scraper_last_run_source_duration_seconds{provider,source}` | gauge | Latest fetch/runtime duration by provider and source. |
| `chp_live_map_scraper_last_run_source_response_bytes{provider,source}` | gauge | Bytes downloaded by each provider's latest run. |
| `chp_live_map_scraper_last_run_incidents{provider,kind}` | gauge | Latest incident counts for each scraper provider. |
| `chp_live_map_scraper_http_requests_total{provider,method,route,status}` | counter | Outbound source requests for CHP and WildWeb, grouped by provider, route, and HTTP status or transport outcome. |
| `chp_live_map_scraper_chp_http_requests_total{provider,method,route,status}` | counter | Compatibility alias for CHP outbound HTTP requests; new dashboards should use `chp_live_map_scraper_http_requests_total`. |

## SQL Tables

- `events`: one row per source incident, keyed by its source identity and updated with current status and latest fields.
- `observations`: append-only status/detail snapshots when an incident is first seen, changes, or clears.
- `detail_entries`: normalized detail-log entries for each stored observation.
- `scrape_runs`: run metadata for monitoring, including source, total incidents seen, filtered incidents acquired, detail-page fetch counts, scrape duration, and outbound response-code counts.
- `incident_comments`: user-submitted incident comments. Public submissions start as `pending`; only `approved` rows are shown publicly. Contact and IP metadata are moderation-only.

Generated files such as `*.sqlite` and `live_chp_map.html` are intentionally ignored by git.

### Temperature map layer

Forest and Malibu include an optional layer of subtle temperature labels in °F.
Use **Air temperature** in the navigation menu to toggle the layer. It samples 41
Forest locations and 31 Malibu locations, primarily along mapped roads, and reveals
more labels as you zoom. These are Open-Meteo **modeled air temperatures**, not station observations or
road-surface temperatures. Tap a label for terrain elevation, model valid time,
and attribution. Each small dot marks the sampled coordinate; its label is offset
beside it to distinguish air temperature from road-surface conditions. Labels yield space to incidents and the preference persists
across regions. Missing or over-one-hour-old estimates are hidden.

The web server batches fixed sample coordinates per region and caches results
for 15 minutes, with a one-minute retry backoff on upstream failures. Open-Meteo's
terrain elevation/downscaling is enabled; no constant lapse-rate correction or
interpolation between labels is applied. This does not establish a guaranteed
mountain-temperature accuracy; local station validation remains future work.

Local evaluation uses the keyless public API. Open-Meteo requires a paid plan for
commercial API use. Set `OPEN_METEO_API_KEY` in the web service environment to use
the customer endpoint; the key stays server-side. See
[Open-Meteo terms and pricing](https://open-meteo.com/en/pricing).
