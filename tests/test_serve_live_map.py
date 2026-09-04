import json
import base64
import datetime as dt
import shutil
import subprocess

import pytest

from fastapi.testclient import TestClient

import serve_live_map
from comments import set_comment_status
from app import (
    ADMIN_SESSION_COOKIE,
    WebSettings,
    create_admin_session_token,
    create_app,
    service_worker_js,
    valid_admin_session_token,
)
from aircraft_tracking import save_position
from push_notifications import (
    deactivate_subscription,
    enqueue_incidents,
    enqueue_test_notification,
    process_pending,
    save_subscription,
)
from serve_live_map import (
    ASSET_CACHE_CONTROL,
    CONTENT_SECURITY_POLICY,
    DISCOVERY_CACHE_CONTROL,
    FAVICON_CACHE_CONTROL,
    INCIDENTS_CACHE_CONTROL,
    MAP_CACHE_CONTROL,
    prometheus_metrics,
)
from scrape_chp_traffic import (
    connect_database,
    insert_observation,
    store_scrape_run,
    upsert_active_event,
)


def make_client(database, **overrides):
    settings = WebSettings(
        database=database,
        database_url=None,
        hours=overrides.pop("hours", 72.0),
        base_path=overrides.pop("base_path", "/"),
        public_url=overrides.pop("public_url", "https://crestmap.us/"),
        google_analytics_id=overrides.pop("google_analytics_id", None),
        service_version=overrides.pop("service_version", "test-1"),
        **overrides,
    )
    return TestClient(create_app(settings))


def basic_auth(username="admin", password="secret"):
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


@pytest.mark.parametrize("analytics_id", [None, "G-TEST123"])
def test_analytics_on_all_public_views(tmp_path, analytics_id):
    database = tmp_path / "analytics.sqlite"
    conn = connect_database(database)
    conn.close()
    with make_client(database, google_analytics_id=analytics_id) as client:
        for path in ("/", "/summary", "/history", "/about"):
            response = client.get(path)
            assert response.status_code == 200
            if analytics_id is None:
                assert "googletagmanager.com/gtag/js" not in response.text
            else:
                head = response.text.split("</head>", 1)[0]
                assert head.count(
                    f'https://www.googletagmanager.com/gtag/js?id={analytics_id}'
                ) == 1
                assert response.text.count("gtag('config',") == 1
                assert f'gtag(\'config\', "{analytics_id}", common);' in head


def test_service_worker_serves_cached_shell_when_origin_is_unreachable():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the service-worker runtime test")

    source = service_worker_js(type("Settings", (), {"service_version": "offline-test"})())
    script = f"""
const workerSource = {json.dumps(source)};
const listeners = {{}};
const stored = new Map();
const deleted = [];
const keyFor = (request) => new URL(typeof request === "string" ? request : request.url, self.location.origin).href;
const cache = {{
  async put(request, response) {{ stored.set(keyFor(request), response.clone()); }},
  async match(request) {{ const response = stored.get(keyFor(request)); return response && response.clone(); }}
}};
globalThis.self = {{
  location: {{ origin: "https://crestmap.us" }},
  clients: {{ claim: async () => {{}} }},
  skipWaiting: async () => {{}},
  addEventListener: (name, callback) => {{ listeners[name] = callback; }}
}};
globalThis.caches = {{
  open: async () => cache,
  keys: async () => ["crestmap-app-shell-old", "crestmap-app-shell-offline-test"],
  delete: async (name) => {{ deleted.push(name); return true; }},
  match: async (request) => cache.match(request)
}};
let online = true;
globalThis.fetch = async (request) => {{
  const url = new URL(typeof request === "string" ? request : request.url, self.location.origin);
  if (!online && url.origin === self.location.origin) throw new Error("origin unreachable");
  return new Response(url.pathname === "/" ? "<html><title>Cached Crestmap</title></html>" : "asset", {{
    status: 200,
    headers: {{ "Cache-Control": "public, max-age=30" }}
  }});
}};
eval(workerSource);
async function lifecycle(name) {{
  let pending;
  listeners[name]({{ waitUntil: (promise) => {{ pending = promise; }} }});
  await pending;
}}
(async () => {{
  await lifecycle("install");
  await lifecycle("activate");
  online = false;
  let navigation;
  listeners.fetch({{
    request: {{ method: "GET", mode: "navigate", url: "https://crestmap.us/?region=malibu" }},
    respondWith: (promise) => {{ navigation = promise; }}
  }});
  const response = await navigation;
  const body = await response.text();
  if (!body.includes("Cached Crestmap")) throw new Error("cached shell was not returned");
  if (stored.size !== 5) throw new Error(`expected 5 cached shell entries, got ${{stored.size}}`);
  if (deleted.join(",") !== "crestmap-app-shell-old") throw new Error("old cache was not retired");
  console.log("offline navigation passed");
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        [node, "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offline navigation passed"


def test_service_worker_csp_allows_pinned_leaflet_downloads():
    connect_policy = next(
        directive
        for directive in CONTENT_SECURITY_POLICY.split("; ")
        if directive.startswith("connect-src ")
    )

    assert "https://unpkg.com" in connect_policy.split()


def sample_event(event_key="LACC|2026-06-08|1234", region="forest"):
    return {
        "event_key": event_key,
        "center": event_key.split("|", 1)[0],
        "incident_date": "2026-06-08",
        "incident_no": event_key.rsplit("|", 1)[-1],
        "observed_at": "2026-06-08T12:34:00-07:00",
        "updated_as_of": "6/8/2026 12:34 PM",
        "incident_time": "12:34 PM",
        "type": "Traffic Hazard",
        "location": "Angeles Crest Hwy",
        "location_desc": "Mile marker 30",
        "area": "Altadena",
        "latitude": 34.25,
        "longitude": -118.1,
        "matched_keywords": "angeles crest",
        "details_hash": "hash-1234",
        "detail_entries": [],
        "region": region,
    }


def test_live_map_handler_serves_health_base_path_and_404(tmp_path, monkeypatch):
    access_logs = []
    monkeypatch.setattr(serve_live_map, "log_event", lambda *args, **kwargs: access_logs.append((args, kwargs)))
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    store_scrape_run(
        conn,
        "2026-05-31T08:00:00-07:00",
        ["LACC"],
        total_seen=12,
        active_seen=2,
        observations_inserted=1,
        active_with_coords=1,
        details_requested=2,
        details_skipped=3,
        duration_seconds=1.25,
        http_status_counts={"GET:list:200": 1, "POST:detail:200": 2},
        source="cad",
    )
    conn.commit()
    conn.close()

    with make_client(database) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.content == b"ok\n"

        response = client.get(
            "/",
            headers={
                "X-Forwarded-For": "203.0.113.7, 10.42.0.63",
                "CF-Connecting-IP": "198.51.100.8",
                "CF-IPCountry": "US",
                "CF-IPContinent": "NA",
                "CF-IPCity": "Los Angeles",
                "CF-Region": "California",
                "CF-Region-Code": "CA",
                "CF-Postal-Code": "90012",
                "CF-Timezone": "America/Los_Angeles",
                "CF-IPLatitude": "34.0522",
                "CF-IPLongitude": "-118.2437",
                "CF-Ray": "8abc123def-LAX",
                "User-Agent": "test-browser/1.0",
            },
        )
        body = response.text
        assert response.status_code == 200
        assert "Crestmap Forest Incidents" in body
        assert "in last 72h" in body
        assert 'Last scrape <time id="last-scrape-at" datetime="2026-05-31T08:00:00-07:00">' in body
        assert '<span class="source-label">(CHP CAD)</span>' in body
        assert '<link rel="icon" href="https://crestmap.us/favicon.svg?active=0&amp;v=' in body
        assert '<link rel="manifest" href="/manifest.webmanifest">' in body
        assert 'id="ios-push-tutorial"' in body
        assert 'Remind me in 7 days' in body
        assert 'if (iosDevice && !standalone)' in body
        assert 'setVisible(tutorial, true);' in body
        assert 'const registrationPromise = serviceWorkerSupported' in body
        assert 'navigator.serviceWorker.register(serviceWorkerUrl, { scope: "/" })' in body
        assert '<meta property="og:image" content="https://crestmap.us/og-image.png">' in body
        assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
        assert "s-maxage" not in response.headers["Cache-Control"]
        assert response.headers["Vary"] == "Cookie, Authorization"
        assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        assert "form-action 'self'" in response.headers["Content-Security-Policy"]
        assert "https://cameras.alertcalifornia.org" in response.headers["Content-Security-Policy"]
        assert response.headers["Content-Security-Policy"].count("https://cameras.alertcalifornia.org") == 2
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["Permissions-Policy"] == "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
        assert "Pragma" not in response.headers
        assert "Expires" not in response.headers

        response = client.get("/?hours=24")
        body = response.text
        assert response.status_code == 200
        assert "in last 24h" in body
        assert '<a class="range-tab is-active" href="?hours=24&amp;region=forest" aria-current="page">24h</a>' in body
        assert 'href="/summary?hours=24&amp;region=forest"' in body
        assert 'href="/history?hours=24&amp;region=forest"' in body
        assert 'href="/?hours=24&amp;region=malibu"' in body

        response = client.get("/summary?hours=24")
        body = response.text
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
        assert "Summary - Crestmap Forest Incidents" in body
        assert "Busiest Roads" in body
        assert '<a class="range-tab is-active" href="?hours=24&amp;region=forest" aria-current="page">24h</a>' in body
        assert '<a class="view-tab is-active" href="/summary?hours=24&amp;region=forest" aria-current="page">Summary</a>' in body

        response = client.get("/history?hours=24")
        body = response.text
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
        assert "History - Crestmap Forest Incidents" in body
        assert "Search road, type, incident number" in body
        assert '<a class="range-tab is-active" href="?hours=24&amp;region=forest" aria-current="page">24h</a>' in body
        assert '<a class="view-tab is-active" href="/history?hours=24&amp;region=forest" aria-current="page">History</a>' in body
        assert '<select class="filter" name="status" aria-label="Status filter">' in body

        response = client.get("/history?hours=24&status=active&mapped=mapped")
        body = response.text
        assert response.status_code == 200
        assert '<option value="active" selected>Active CHP</option>' in body
        assert '<option value="mapped" selected>Mapped only</option>' in body

        response = client.get("/about?hours=24")
        body = response.text
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
        assert "About - Crestmap Forest Incidents" in body
        assert "Update Cadence" in body
        assert 'id="push-notifications"' in body
        assert "Manage alert choices" in body
        assert 'value="crest"> Crest + west forest only' in body
        assert 'class="push-launcher"' not in body
        assert '<button type="button" class="view-menu-row" data-open-push-settings>' in body
        assert '<span class="view-menu-label">Alerts</span>' in body
        assert 'class="header-alert-button"' in body
        assert "data-header-push-launcher" in body
        assert 'button.classList.toggle("needs-install", needsInstall);' in body
        assert 'id="ios-push-onboarding"' in body
        assert "Edit Actions" in body
        assert "Open as Web App" in body
        assert '<a class="range-tab is-active" href="?hours=24&amp;region=forest" aria-current="page">24h</a>' in body
        assert '<a class="view-tab is-active" href="/about?hours=24&amp;region=forest" aria-current="page">About</a>' in body

        response = client.get("/status.json?hours=24")
        body = response.text
        payload = response.json()
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        assert response.headers["Cache-Control"] == "private, max-age=15, stale-while-revalidate=30"
        assert '"active_count": 0' in body
        assert '"region": "forest"' in body
        assert '"total_count": 0' in body
        assert '"version":' in body
        assert payload["app_version"] == "test-1"
        assert payload["region_statuses"]["forest"]["active_count"] == 0
        assert payload["region_statuses"]["malibu"]["active_count"] == 0
        assert payload["last_scrape"]["observed_at"] == "2026-05-31T08:00:00-07:00"
        assert payload["last_scrape"]["source"] == "cad"

        response = client.get("/incidents.json?hours=24")
        payload = response.json()
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        assert response.headers["Cache-Control"] == INCIDENTS_CACHE_CONTROL
        assert "s-maxage" not in response.headers["Cache-Control"]
        assert payload["incidents"] == []
        assert payload["status"]["active_count"] == 0
        assert payload["status"]["total_count"] == 0
        assert payload["status"]["hours"] == 24.0
        assert payload["region"] == "forest"
        assert payload["status"]["region"] == "forest"
        assert payload["region_statuses"]["forest"]["active_count"] == 0
        assert payload["region_statuses"]["malibu"]["active_count"] == 0
        assert "checked_at" in payload

        response = client.get("/incidents.json?hours=24&region=malibu%27%3Bdrop%20table%20events%3B--")
        payload = response.json()
        assert response.status_code == 200
        assert payload["region"] == "forest"
        assert payload["incidents"] == []

        response = client.get("/?hours=9999")
        body = response.text
        assert response.status_code == 200
        assert "in last 720h" in body
        assert '<a class="range-tab is-active" href="?hours=720&amp;region=forest" aria-current="page">30d</a>' in body

        response = client.get("/favicon.svg")
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "image/svg+xml"
        assert response.headers["Cache-Control"] == FAVICON_CACHE_CONTROL
        assert b"<svg" in response.content
        assert b"#2f8a4e" in response.content

        response = client.get("/og-image.svg")
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "image/svg+xml"
        assert response.headers["Cache-Control"] == ASSET_CACHE_CONTROL
        assert b"Crestmap Incidents" in response.content

        response = client.get("/og-image.png")
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "image/png"
        assert response.headers["Cache-Control"] == ASSET_CACHE_CONTROL
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")

        response = client.get("/favicon.ico")
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "image/png"
        assert response.headers["Cache-Control"] == FAVICON_CACHE_CONTROL
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")

        for touch_path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            response = client.get(touch_path)
            assert response.status_code == 200
            assert response.headers["Content-Type"] == "image/png"
            assert response.headers["Cache-Control"] == ASSET_CACHE_CONTROL
            assert response.content.startswith(b"\x89PNG\r\n\x1a\n")

        response = client.get("/manifest.webmanifest")
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/manifest+json; charset=utf-8"
        assert response.json()["display"] == "standalone"
        assert response.json()["name"] == "Crestmap Incidents"

        response = client.get("/sw.js")
        assert response.status_code == 200
        assert response.headers["Service-Worker-Allowed"] == "/"
        assert 'self.addEventListener("push"' in response.text
        assert "MAX_BADGE_COUNT = 99" in response.text
        assert "navigator.setAppBadge(count)" in response.text
        assert "navigator.clearAppBadge()" in response.text
        assert 'event.data?.type === "CLEAR_BADGE"' in response.text
        assert 'const CACHE_NAME = `${CACHE_PREFIX}${RELEASE_VERSION}`;' in response.text
        assert '"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"' in response.text
        assert '"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"' in response.text
        assert 'self.addEventListener("install"' in response.text
        assert 'self.addEventListener("activate"' in response.text
        assert 'self.addEventListener("fetch"' in response.text
        assert 'event.request.mode === "navigate"' in response.text
        assert 'cache.match(DEFAULT_URL)' in response.text
        assert 'const RELEASE_VERSION = "test-1";' in response.text

        response = client.get("/robots.txt")
        body = response.text
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
        assert response.headers["Cache-Control"] == DISCOVERY_CACHE_CONTROL
        assert "User-agent: *" in body
        assert "Allow: /" in body
        assert "Sitemap: https://crestmap.us/sitemap.xml" in body

        response = client.get("/sitemap.xml")
        body = response.text
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/xml; charset=utf-8"
        assert response.headers["Cache-Control"] == DISCOVERY_CACHE_CONTROL
        assert "<loc>https://crestmap.us/</loc>" in body
        assert "<changefreq>hourly</changefreq>" in body

        response = client.get("/metrics")
        body = response.text
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
        assert response.headers["Cache-Control"] == "no-store"
        assert "chp_live_map_up 1" in body
        assert 'chp_live_map_incidents{status="total"} 0' in body
        assert 'chp_live_map_region_incidents{region="forest",status="total"} 0' in body
        assert 'chp_live_map_region_incidents{region="malibu",status="total"} 0' in body
        assert "chp_live_map_scrape_last_run_incidents" not in body
        assert "chp_live_map_scrape_last_run_details" not in body
        assert "chp_live_map_scrape_chp_http_requests_total" not in body
        assert "chp_live_map_http_requests_total" in body
        assert "chp_live_map_db_pool_connections" not in body
        assert "chp_live_map_comments_pending 0" in body
        assert 'chp_live_map_push_subscriptions{status="active"} 0' in body
        assert 'chp_live_map_push_subscription_sources{source="chp"} 0' in body
        assert 'chp_live_map_push_subscription_sources{source="wildweb"} 0' in body
        assert 'chp_live_map_push_subscription_areas{area="crest"} 0' in body
        assert 'chp_live_map_push_deliveries{region="forest",category="hazard",status="delivered"} 0' in body
        assert 'chp_live_map_push_test_notifications{status="failed"} 0' in body

        response = client.head("/")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
        assert response.content == b""

        response = client.get("/missing")
        assert response.status_code == 404

        response = client.get("/malibu")
        assert response.status_code == 404

    logged_paths = [kwargs["url.path"] for _args, kwargs in access_logs]
    assert "/healthz" not in logged_paths
    assert "/metrics" not in logged_paths
    assert "/" in logged_paths
    assert "/missing" in logged_paths
    missing_logs = [kwargs for _args, kwargs in access_logs if kwargs["url.path"] == "/missing"]
    assert len(missing_logs) == 1
    missing_log = missing_logs[0]
    assert missing_log["http.response.status_code"] == 404
    assert missing_log["event.outcome"] == "failure"
    chp_log = next(
        kwargs
        for _args, kwargs in access_logs
        if kwargs["url.path"] == "/" and kwargs["event.action"] == "http_request"
    )
    assert chp_log["client.address"] == "198.51.100.8"
    assert chp_log["client.nat.ip"] == "testclient"
    assert chp_log["http.request.header.x_forwarded_for"] == "203.0.113.7, 10.42.0.63"
    assert chp_log["http.request.header.cf_connecting_ip"] == "198.51.100.8"
    assert chp_log["http.request.header.cf_ipcountry"] == "US"
    assert chp_log["http.request.header.cf_ipcontinent"] == "NA"
    assert chp_log["http.request.header.cf_ipcity"] == "Los Angeles"
    assert chp_log["http.request.header.cf_region"] == "California"
    assert chp_log["http.request.header.cf_region_code"] == "CA"
    assert chp_log["http.request.header.cf_postal_code"] == "90012"
    assert chp_log["http.request.header.cf_timezone"] == "America/Los_Angeles"
    assert chp_log["http.request.header.cf_iplatitude"] == "34.0522"
    assert chp_log["http.request.header.cf_iplongitude"] == "-118.2437"
    assert chp_log["http.request.header.cf_ray"] == "8abc123def-LAX"
    assert chp_log["client.geo.country_iso_code"] == "US"
    assert chp_log["client.geo.continent_code"] == "NA"
    assert chp_log["client.geo.city_name"] == "Los Angeles"
    assert chp_log["client.geo.region_name"] == "California"
    assert chp_log["client.geo.region_iso_code"] == "CA"
    assert chp_log["client.geo.postal_code"] == "90012"
    assert chp_log["client.geo.timezone"] == "America/Los_Angeles"
    assert chp_log["client.geo.location.lat"] == "34.0522"
    assert chp_log["client.geo.location.lon"] == "-118.2437"
    assert chp_log["http.request.header.user_agent"] == "test-browser/1.0"


def test_prometheus_metrics_include_pool_stats(tmp_path):
    body = prometheus_metrics(
        tmp_path / "missing.sqlite",
        None,
        72.0,
        pool_stats={
            "pool_min": 1,
            "pool_max": 5,
            "pool_size": 3,
            "pool_available": 2,
            "requests_waiting": 4,
        },
    ).decode("utf-8")

    assert 'chp_live_map_db_pool_connections{state="min"} 1' in body
    assert 'chp_live_map_db_pool_connections{state="max"} 5' in body
    assert 'chp_live_map_db_pool_connections{state="size"} 3' in body
    assert 'chp_live_map_db_pool_connections{state="available"} 2' in body
    assert 'chp_live_map_db_pool_connections{state="in_use"} 1' in body
    assert "chp_live_map_db_pool_requests_waiting 4" in body


def test_aircraft_api_returns_delayed_verified_position(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    observed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=90)).isoformat(timespec="seconds")
    save_position(
        conn,
        {
            "icao24": "ad395a",
            "registration": "N951LB",
            "aircraft_type": "AS332L1",
            "display_name": "LASD rescue helicopter",
            "callsign": "AIR5",
            "observed_at": observed_at,
            "fetched_at": observed_at,
            "longitude": -118.12,
            "latitude": 34.31,
            "baro_altitude_m": 1200.0,
            "geometric_altitude_m": 1250.0,
            "velocity_mps": 55.0,
            "true_track": 275.0,
            "vertical_rate_mps": 1.5,
            "on_ground": False,
            "source": "opensky",
        },
    )
    conn.commit()
    conn.close()

    with make_client(database, aircraft_tracking_enabled=True) as client:
        response = client.get("/api/v1/aircraft")
        map_response = client.get("/")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, max-age=10"
    assert response.json()["aircraft"][0]["registration"] == "N951LB"
    assert response.json()["aircraft"][0]["mission_confirmed"] is False
    assert response.json()["aircraft"][0]["trail"] == [[34.31, -118.12]]
    assert response.json()["trail_age_seconds"] == 1800
    assert 'data-aircraft-layer-toggle' in map_response.text


def test_prometheus_metrics_include_push_breakdowns(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    incident = sample_event()
    active_endpoint = "https://web.push.apple.com/active-device"
    inactive_endpoint = "https://web.push.apple.com/inactive-device"
    upsert_active_event(conn, incident)
    save_subscription(
        conn,
        {
            "subscription": {
                "endpoint": active_endpoint,
                "keys": {"p256dh": "active-public-key", "auth": "active-auth"},
            },
            "regions": ["crest"],
            "categories": ["closure", "hazard"],
        },
    )
    save_subscription(
        conn,
        {
            "subscription": {
                "endpoint": inactive_endpoint,
                "keys": {"p256dh": "inactive-public-key", "auth": "inactive-auth"},
            },
            "regions": ["forest"],
            "categories": ["collision"],
        },
    )
    deactivate_subscription(conn, inactive_endpoint)
    enqueue_incidents(conn, [incident], "https://crestmap.us/")
    process_pending(conn, "private-key", "https://crestmap.us/", sender=lambda **_kwargs: None)
    enqueue_test_notification(conn, active_endpoint, "https://crestmap.us/")
    process_pending(conn, "private-key", "https://crestmap.us/", sender=lambda **_kwargs: None)
    conn.commit()
    conn.close()

    body = prometheus_metrics(database, None, 72.0).decode("utf-8")

    assert 'chp_live_map_push_subscriptions{status="active"} 1' in body
    assert 'chp_live_map_push_subscriptions{status="inactive"} 1' in body
    assert 'chp_live_map_push_subscription_sources{source="chp"} 1' in body
    assert 'chp_live_map_push_subscription_sources{source="wildweb"} 0' in body
    assert 'chp_live_map_push_subscription_areas{area="crest"} 1' in body
    assert 'chp_live_map_push_subscription_areas{area="forest"} 0' in body
    assert 'chp_live_map_push_subscription_categories{category="closure"} 1' in body
    assert 'chp_live_map_push_subscription_categories{category="hazard"} 1' in body
    assert 'chp_live_map_push_notification_events{region="forest",category="hazard",status="completed"} 1' in body
    assert 'chp_live_map_push_deliveries{region="forest",category="hazard",status="delivered"} 1' in body
    assert 'chp_live_map_push_delivery_attempts{region="forest",category="hazard"} 1' in body
    assert 'chp_live_map_push_test_notifications{status="delivered"} 1' in body
    assert "chp_live_map_push_last_delivery_timestamp_seconds 0.000" not in body
    assert "chp_live_map_push_last_test_delivery_timestamp_seconds 0.000" not in body


def test_push_subscription_api_saves_preferences_and_unsubscribes(tmp_path):
    database = tmp_path / "chp.sqlite"
    payload = {
        "subscription": {
            "endpoint": "https://web.push.apple.com/device-token",
            "keys": {"p256dh": "device-public-key", "auth": "device-auth-secret"},
        },
        "regions": ["forest"],
        "categories": ["collision", "closure"],
    }
    with make_client(database, vapid_public_key="public-vapid-key") as client:
        config = client.get("/api/v1/push/config")
        assert config.status_code == 200
        assert config.json()["enabled"] is True
        assert config.json()["public_key"] == "public-vapid-key"
        assert config.json()["defaults"]["sources"] == ["chp"]
        assert config.json()["defaults"]["regions"] == ["forest", "malibu"]

        response = client.post("/api/v1/push/subscription", json=payload)
        assert response.status_code == 201
        assert response.json()["subscribed"] is True
        assert response.json()["sources"] == ["chp"]
        assert response.json()["regions"] == ["forest"]

        status = client.post(
            "/api/v1/push/subscription",
            json={"action": "status", "subscription": payload["subscription"]},
        )
        assert status.status_code == 200
        assert status.json() == {
            "subscribed": True,
            "preferences": {
                "sources": ["chp"],
                "regions": ["forest"],
                "categories": ["closure", "collision"],
            },
        }

        test_push = client.post(
            "/api/v1/push/subscription",
            json={"action": "test", "subscription": payload["subscription"]},
        )
        assert test_push.status_code == 202
        assert test_push.json()["queued"] is True

        disabled = client.post(
            "/api/v1/push/subscription",
            json={"action": "unsubscribe", "subscription": payload["subscription"]},
        )
        assert disabled.status_code == 200
        assert disabled.json()["subscribed"] is False

        status = client.post(
            "/api/v1/push/subscription",
            json={"action": "status", "subscription": payload["subscription"]},
        )
        assert status.json() == {"subscribed": False, "preferences": None}


def test_public_malibu_region_is_available_without_auth(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    upsert_active_event(
        conn,
        {
            "event_key": "LACC|2026-06-11|0867",
            "center": "LACC",
            "incident_date": "2026-06-11",
            "incident_no": "0867",
            "observed_at": "2026-06-11T17:07:03+00:00",
            "updated_as_of": "2026-06-11T17:07:00+00:00",
            "incident_time": "10:07 AM",
            "type": "Traffic Hazard",
            "location": "Las Virgenes Rd / Piuma Rd",
            "location_desc": "SB LAS VIRGENES RD JSO PIUMA RD",
            "area": "West Valley",
            "latitude": 34.082133,
            "longitude": -118.704535,
            "matched_keywords": "las virgenes;piuma rd",
            "details_hash": "hash",
            "detail_entries": [],
            "region": "malibu",
        },
    )
    conn.commit()
    conn.close()

    with make_client(database) as client:
        response = client.get("/?region=malibu&hours=24")
        body = response.text
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
        assert "Crestmap Malibu Incidents" in body
        assert 'href="/?hours=24&amp;region=forest"' in body
        assert (
            'href="/?hours=24&amp;region=malibu" aria-current="page"><span>Malibu</span><span class="region-active-count" aria-label="1 active incident">1</span></a>'
            in body
        )
        assert 'const currentRegion = "malibu"' in body

        response = client.get("/incidents.json?region=malibu&hours=24")
        payload = response.json()
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == INCIDENTS_CACHE_CONTROL
        assert payload["region"] == "malibu"
        assert payload["status"]["region"] == "malibu"
        assert payload["status"]["total_count"] == 1
        assert payload["region_statuses"]["forest"]["active_count"] == 0
        assert payload["region_statuses"]["malibu"]["active_count"] == 1
        assert payload["incidents"][0]["region"] == "malibu"
        assert payload["incidents"][0]["location"] == "Las Virgenes Rd / Piuma Rd"


def test_live_map_handler_serves_red_favicon_when_active(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    upsert_active_event(conn, sample_event())
    conn.commit()
    conn.close()

    with make_client(database) as client:
        response = client.get("/favicon.svg")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == FAVICON_CACHE_CONTROL
        assert b"#d83b3b" in response.content
        assert b"#2f8a4e" not in response.content

        response = client.get("/?hours=72")
        body = response.text
        assert "1 active" in body
        assert '<link rel="icon" href="https://crestmap.us/favicon.svg?active=1&amp;v=' in body


def test_incident_comments_are_published_automatically(tmp_path):
    database = tmp_path / "chp.sqlite"
    event_key = "LACC|2026-06-08|1234"
    conn = connect_database(database)
    upsert_active_event(conn, sample_event(event_key))
    conn.commit()
    conn.close()

    with make_client(database) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Comments" in response.text
        assert "Post comment" in response.text

        response = client.get(f"/api/v1/incidents/{event_key}/comments")
        assert response.status_code == 200
        assert response.json()["data"] == []

        response = client.post(
            f"/api/v1/incidents/{event_key}/comments",
            json={
                "display_name": "<b>Alice</b>",
                "body": "<script>alert(1)</script> Road is still icy.",
                "contact": "alice@example.test",
                "website": "",
            },
            headers={
                "CF-Connecting-IP": "198.51.100.99",
                "CF-IPCountry": "US",
                "User-Agent": "comment-test/1.0",
            },
        )
        assert response.status_code == 201
        assert response.json()["status"] == "approved"
        assert response.json()["message"] == "Comment published."

        response = client.get(f"/api/v1/incidents/{event_key}/comments")
        assert response.status_code == 200
        assert len(response.json()["data"]) == 1

    conn = connect_database(database)
    row = conn.execute("SELECT * FROM incident_comments").fetchone()
    assert row["status"] == "approved"
    assert row["approved_at"] == row["created_at"]
    assert row["display_name"] == "Alice"
    assert row["body"] == "alert(1) Road is still icy."
    assert row["contact"] == "alice@example.test"
    assert row["cf_connecting_ip"] == "198.51.100.99"
    assert row["cf_country"] == "US"
    assert row["ip_hash"]
    conn.close()

    with make_client(database) as client:
        response = client.get(f"/api/v1/incidents/{event_key}/comments")
        assert response.status_code == 200
        payload = response.json()
        assert payload["data"] == [
            {
                "id": row["id"],
                "event_key": event_key,
                "display_name": "Alice",
                "body": "alert(1) Road is still icy.",
                "category": None,
                "created_at": row["created_at"],
                "media": [],
            }
        ]
        assert "contact" not in payload["data"][0]


def test_incident_media_upload_is_published_with_comment(tmp_path):
    class FakeMediaStore:
        def __init__(self):
            self.deleted = []
            self.size = 123
            self.content_type = "image/webp"

        def presigned_url(self, method, object_key, expires=900, now=None, content_type=None):
            return f"https://r2.example.test/{object_key}?method={method}"

        def head(self, object_key):
            return {"size": self.size, "content_type": self.content_type}

        def delete(self, object_key):
            self.deleted.append(object_key)

    database = tmp_path / "chp.sqlite"
    event_key = "LACC|2026-06-08|5678"
    conn = connect_database(database)
    upsert_active_event(conn, sample_event(event_key))
    conn.commit()
    conn.close()

    with make_client(
        database,
        admin_username="admin",
        admin_password="secret",
        r2_account_id="account",
        r2_access_key_id="access",
        r2_secret_access_key="secret-key",
        r2_upload_token_secret="upload-secret",
    ) as client:
        fake_store = FakeMediaStore()
        client.app.state.media_store = fake_store

        map_response = client.get("/")
        assert 'name="media_files"' in map_response.text
        assert "Photos are compressed before upload" in map_response.text

        comment_response = client.post(
            f"/api/v1/incidents/{event_key}/comments",
            json={"body": "Photo of a fallen tree.", "media_count": 1},
        )
        assert comment_response.status_code == 201
        comment = comment_response.json()
        assert comment["upload_token"]

        upload_response = client.post(
            f"/api/v1/incidents/{event_key}/media/uploads",
            json={
                "comment_id": comment["id"],
                "upload_token": comment["upload_token"],
                "filename": "tree.webp",
                "content_type": "image/webp",
                "size": 123,
            },
        )
        assert upload_response.status_code == 201
        upload = upload_response.json()
        assert upload["method"] == "PUT"
        assert upload["upload_url"].startswith("https://r2.example.test/comments/")
        assert upload["headers"] == {"Content-Type": "image/webp"}

        finalize_response = client.post(
            f"/api/v1/incidents/{event_key}/media/{upload['id']}/finalize",
            json={"comment_id": comment["id"], "upload_token": comment["upload_token"]},
        )
        assert finalize_response.status_code == 200
        assert finalize_response.json()["status"] == "approved"

        response = client.get(f"/api/v1/media/{upload['id']}", follow_redirects=False)
        assert response.status_code == 302

        admin_page = client.get("/admin/comments?status=approved", headers=basic_auth())
        assert admin_page.status_code == 200
        assert f'/admin/media/{upload["id"]}' in admin_page.text
        assert "tree.webp" in admin_page.text

        public_comments = client.get(f"/api/v1/incidents/{event_key}/comments").json()["data"]
        assert public_comments[0]["media"] == [
            {
                "id": upload["id"],
                "kind": "image",
                "content_type": "image/webp",
                "filename": "tree.webp",
                "size": 123,
                "duration_seconds": None,
                "url": f"/api/v1/media/{upload['id']}",
            }
        ]
        response = client.get(f"/api/v1/media/{upload['id']}", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("?method=GET")

        rejected = client.post(
            "/admin/comments",
            data={"id": comment["id"], "status": "approved", "action": "reject"},
            headers=basic_auth(),
        )
        assert rejected.status_code == 200
        assert len(fake_store.deleted) == 1
        assert client.get(f"/api/v1/incidents/{event_key}/comments").json()["data"] == []


def test_comment_honeypot_and_rate_limit(tmp_path):
    database = tmp_path / "chp.sqlite"
    event_key = "LACC|2026-06-08|1234"
    conn = connect_database(database)
    upsert_active_event(conn, sample_event(event_key))
    conn.commit()
    conn.close()
    headers = {"CF-Connecting-IP": "198.51.100.42", "User-Agent": "rate-test/1.0"}

    with make_client(database) as client:
        response = client.post(
            f"/api/v1/incidents/{event_key}/comments",
            json={"body": "Spam", "website": "https://bot.example"},
            headers=headers,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "honeypot"

        for index in range(3):
            response = client.post(
                f"/api/v1/incidents/{event_key}/comments",
                json={"body": f"Valid comment {index}"},
                headers=headers,
            )
            assert response.status_code == 201

        response = client.post(
            f"/api/v1/incidents/{event_key}/comments",
            json={"body": "One too many"},
            headers=headers,
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"


def test_comment_for_missing_incident_returns_404(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    conn.close()

    with make_client(database) as client:
        response = client.post(
            "/api/v1/incidents/LACC%7C2026-06-08%7C9999/comments",
            json={"body": "Where did it go?"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_admin_comments_disabled_without_credentials(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    conn.close()

    with make_client(database) as client:
        response = client.get("/admin/comments")
        assert response.status_code == 404
        assert client.get("/admin/login").status_code == 404
        response = client.get("/admin/incidents", follow_redirects=False)
        assert response.status_code == 308
        assert response.headers["Location"] == "/"


def test_admin_comments_redirects_to_login_and_rejects_wrong_basic_auth(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    conn.close()

    with make_client(database, admin_username="admin", admin_password="secret") as client:
        response = client.get("/admin/comments", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["Location"] == "/admin/login?next=%2Fadmin%2Fcomments"

        response = client.get(
            "/admin/comments",
            headers=basic_auth("admin", "wrong"),
            follow_redirects=False,
        )
        assert response.status_code == 303

        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "secret"},
            headers={"Origin": "http://testserver", "X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
        assert "Secure" in response.headers["Set-Cookie"]


def test_admin_login_rate_limits_repeated_failures(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    conn.close()

    with make_client(database, admin_username="admin", admin_password="secret") as client:
        for _ in range(10):
            response = client.post(
                "/admin/login",
                data={"username": "admin", "password": "wrong"},
                headers={"Origin": "http://testserver", "CF-Connecting-IP": "198.51.100.77"},
            )
            assert response.status_code == 401
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "secret"},
            headers={"Origin": "http://testserver", "CF-Connecting-IP": "198.51.100.77"},
        )
        assert response.status_code == 429
        assert "Too many failed attempts" in response.text


def test_admin_login_cookie_tamper_expiry_and_logout(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    conn.close()
    settings = WebSettings(
        database=database,
        public_url="https://crestmap.us/",
        admin_username="admin",
        admin_password="secret",
        admin_session_secret="separate-session-secret",
        admin_session_hours=8,
    )

    token = create_admin_session_token(settings, now=1000)
    assert valid_admin_session_token(settings, token, now=1001)
    assert not valid_admin_session_token(settings, token + "tampered", now=1001)
    assert not valid_admin_session_token(settings, token, now=1000 + 8 * 3600)

    with TestClient(create_app(settings)) as client:
        response = client.get("/")
        assert "/admin/login" in response.text
        assert "Admin login" in response.text
        assert "const adminMode = false" in response.text
        assert "window.location.replace(destination)" not in response.text

        response = client.get("/admin/session")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {"authenticated": False, "admin_incidents_url": None}

        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "wrong", "next": "/admin/comments"},
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 401
        assert "Incorrect username or password." in response.text

        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "secret", "next": "https://evil.example/"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["Location"] == "/"
        cookie = response.headers["Set-Cookie"]
        assert f"{ADMIN_SESSION_COOKIE}=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Max-Age=28800" in cookie

        response = client.get("/admin/session")
        assert response.status_code == 200
        assert response.json() == {
            "authenticated": True,
            "admin_incidents_url": "/",
        }

        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Vary"] == "Cookie, Authorization"
        assert "const adminMode = true" in response.text
        assert "Admin tools" in response.text

        response = client.get("/summary")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "Admin tools" in response.text

        response = client.get("/admin/comments")
        assert response.status_code == 200
        assert "Log out" in response.text

        response = client.post(
            "/admin/logout",
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["Location"] == "/"

        response = client.get("/admin/comments", follow_redirects=False)
        assert response.status_code == 303


def test_admin_incident_map_reveals_only_details_removed_from_latest_snapshot(tmp_path):
    database = tmp_path / "chp.sqlite"
    event_key = "LACC|2026-06-08|1234"
    retained = {
        "section": "Detail Information",
        "time": "12:35 PM",
        "entry_no": "0002",
        "text": "This row remains visible",
    }
    removed = {
        "section": "Detail Information",
        "time": "12:34 PM",
        "entry_no": "0001",
        "text": "This row disappeared later",
    }
    conn = connect_database(database)
    first = {
        **sample_event(event_key),
        "observed_at": "2026-06-08T12:34:00-07:00",
        "details_hash": "first",
        "detail_entries": [removed, retained],
    }
    upsert_active_event(conn, first)
    insert_observation(conn, first, "active")
    second = {
        **sample_event(event_key),
        "observed_at": "2026-06-08T12:36:00-07:00",
        "details_hash": "second",
        "detail_entries": [retained],
    }
    upsert_active_event(conn, second)
    insert_observation(conn, second, "active")
    conn.commit()
    conn.close()

    with make_client(database, admin_username="admin", admin_password="secret") as client:
        public = client.get("/incidents.json")
        assert public.status_code == 200
        public_entries = public.json()["incidents"][0]["detail_entries"]
        assert [entry["text"] for entry in public_entries] == ["This row remains visible"]

        response = client.get("/", headers=basic_auth())
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "Checking hidden..." in response.text
        assert "No hidden details" in response.text
        assert "Show hidden (${entries.length})" in response.text
        assert "This row disappeared later" not in response.text

        old_hidden_url = f"/admin/incidents/{event_key}/hidden-details?region=forest"
        assert client.get(old_hidden_url, headers=basic_auth()).status_code == 404

        hidden_url = f"/api/v1/incidents/{event_key}/hidden-details?region=forest"
        response = client.get(hidden_url)
        assert response.status_code == 401

        response = client.get(hidden_url, headers=basic_auth())
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        payload = response.json()
        assert payload["meta"] == {"event_key": event_key, "count": 1}
        assert payload["data"][0]["text"] == "This row disappeared later"
        assert payload["data"][0]["first_seen"] == "2026-06-08T12:34:00-07:00"
        assert payload["data"][0]["last_seen"] == "2026-06-08T12:34:00-07:00"
        assert payload["data"][0]["snapshot_count"] == 1


def test_admin_comments_approves_pending_comment(tmp_path):
    database = tmp_path / "chp.sqlite"
    event_key = "LACC|2026-06-08|1234"
    conn = connect_database(database)
    upsert_active_event(conn, sample_event(event_key))
    conn.commit()
    conn.close()

    with make_client(database, admin_username="admin", admin_password="secret") as client:
        response = client.post(
            f"/api/v1/incidents/{event_key}/comments",
            json={"display_name": "Bob", "body": "Chains required at the top."},
            headers={"CF-Connecting-IP": "198.51.100.50", "User-Agent": "admin-test/1.0"},
        )
        assert response.status_code == 201

        conn = connect_database(database)
        conn.execute("UPDATE incident_comments SET status = 'pending', approved_at = NULL")
        conn.commit()
        conn.close()

        response = client.get("/admin/comments", headers=basic_auth())
        assert response.status_code == 200
        body = response.text
        assert "Comment Moderation" in body
        assert "Chains required at the top." in body
        assert "Approve" in body
        assert "Reject" in body
        assert "Delete" in body
        assert "Submitter IP: 198.51.100.50" in body

        conn = connect_database(database)
        row = conn.execute("SELECT id FROM incident_comments").fetchone()
        conn.close()

        response = client.post(
            "/admin/comments",
            data={"id": str(row["id"]), "action": "approve", "status": "pending"},
            headers={**basic_auth(), "Origin": "https://crestmap.us"},
        )
        assert response.status_code == 200
        assert f"Comment #{row['id']} approved." in response.text

        response = client.get(f"/api/v1/incidents/{event_key}/comments")
        assert response.status_code == 200
        comments = response.json()["data"]
        assert len(comments) == 1
        assert comments[0]["display_name"] == "Bob"
        assert comments[0]["body"] == "Chains required at the top."


def test_admin_comments_rejects_cross_origin_post(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    conn.close()

    with make_client(database, admin_username="admin", admin_password="secret") as client:
        response = client.post(
            "/admin/comments",
            data={"id": "1", "action": "delete", "status": "pending"},
            headers={**basic_auth(), "Origin": "https://evil.example"},
        )
        assert response.status_code == 403
