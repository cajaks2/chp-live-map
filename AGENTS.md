# Repository Guidelines

## Project Structure & Module Organization
- `scrape_chp_traffic.py` follows the public CHP CAD WebForms flow, filters selected forest-road incidents, stores event history, and exposes scraper metrics when run as a service.
- `generate_live_map.py` renders the map, Summary, History, and About HTML views from stored incident rows.
- `serve_live_map.py` serves the dynamic web app, JSON status/incidents endpoints, health checks, assets, and Prometheus metrics.
- `ecs_logging.py` centralizes ECS JSON logging helpers.
- `tests/` contains pytest unit tests for scraping, rendering, serving, metrics, logging, and schema behavior.
- `k8s/` and `deploy/digitalocean/` contain production deployment manifests.
- `docs/` is for design notes and mockups. Generated runtime artifacts such as `*.sqlite`, `live_chp_map.html`, and logs should stay out of git.

## Build, Test, and Development Commands
- `python3 -m venv .venv && .venv/bin/python -m pip install -r requirements-dev.txt` creates the local dev environment.
- `make test` runs the unit suite using `.venv/bin/python`.
- `make coverage` runs pytest with statement coverage.
- `python3 scrape_chp_traffic.py --interval 60` runs the scraper loop locally against SQLite by default.
- `python3 serve_live_map.py --port 8080` serves the dynamic app locally.
- `docker buildx build --platform linux/amd64 -t cajaks2/chp-live-map:<version> --push .` builds the production image.

## Coding Style & Naming Conventions
- Python follows PEP 8 with 4-space indentation and snake_case names.
- Keep modules dependency-light. Prefer standard-library HTML/URL/JSON helpers over ad hoc string handling.
- Keep ECS logs structured and avoid logging `/healthz`, `/readyz`, and `/metrics` request noise.
- Preserve existing public URLs and query parameters when changing navigation; especially `hours` and `incident`.
- Generated HTML/CSS/JS is currently embedded in `generate_live_map.py`; keep edits scoped and covered by render/server tests.

## Testing Guidelines
- Use `pytest`; put tests in `tests/test_*.py`.
- Add focused tests for link/query behavior, scraper parsing, schema changes, metrics, and cache/header behavior.
- Keep test data synthetic and small. Do not commit live CHP snapshots or database dumps.
- Run `make test` before committing code or deployment changes.

## Deployment Notes
- Current Docker image repository is `cajaks2/chp-live-map`.
- Keep `deploy/digitalocean/docker-compose.yml` and `k8s/chp-live-map.yaml` image tags and `SERVICE_VERSION` values in sync when bumping versions.
- The DigitalOcean Compose deployment serves `crestmap.us` behind nginx and keeps `chp.flowy.us` as an alias.
- Compose runs Postgres, the web service, the long-lived scraper service, and a backup sidecar.
- For normal DigitalOcean app deploys, use `make -C deploy/digitalocean deploy VERSION=<version>` or `deploy/digitalocean/deploy-compose.sh`. These use `docker compose up -d --no-deps web scrape` so Postgres is not recreated and the public site has less downtime.
- Kubernetes uses a scraper Deployment with one replica, not a CronJob, so scraper metrics are scrapeable and duplicate scraper loops are avoided.

## Commit & Pull Request Guidelines
- Use short, descriptive commit messages such as `Preserve history window in map links`.
- Summaries should mention user-visible behavior, schema changes, deployment tag bumps, and test results.
- Do not commit personal contact emails or secrets. Configure `CHP_CONTACT_EMAIL`, `GOOGLE_ANALYTICS_ID`, database URLs, and passwords through environment files or cluster secrets.

## Data Source & Product Notes
- The scraper uses the public CHP CAD website because no documented public detail-log API is available.
- Be conservative with scraping: keep the one-minute list cadence, road filtering before detail fetches, robots.txt checks, retry backoff, and the active-detail refresh throttle.
- The app stores history indefinitely unless an explicit retention job is added; UI windows such as 72h and 30d only limit what is displayed.
- Active incidents render red, cleared incidents render grey, and incidents without coordinates remain visible in lists/history even when they cannot be pinned on the map.
