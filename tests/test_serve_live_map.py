import json

from fastapi.testclient import TestClient

import serve_live_map
from app import WebSettings, create_app
from serve_live_map import (
    ASSET_CACHE_CONTROL,
    CONTENT_SECURITY_POLICY,
    DISCOVERY_CACHE_CONTROL,
    FAVICON_CACHE_CONTROL,
    INCIDENTS_CACHE_CONTROL,
    MAP_CACHE_CONTROL,
    prometheus_metrics,
)
from scrape_chp_traffic import connect_database, store_scrape_run, upsert_active_event


def make_client(database, **overrides):
    settings = WebSettings(
        database=database,
        database_url=None,
        hours=overrides.pop("hours", 72.0),
        base_path=overrides.pop("base_path", "/"),
        public_url=overrides.pop("public_url", "https://crestmap.us/"),
        google_analytics_id=overrides.pop("google_analytics_id", None),
        **overrides,
    )
    return TestClient(create_app(settings))


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
        assert "CHP Forest Incidents" in body
        assert "in last 72h" in body
        assert '<link rel="icon" href="https://crestmap.us/favicon.svg?active=0&amp;v=' in body
        assert '<meta property="og:image" content="https://crestmap.us/og-image.png">' in body
        assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
        assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        assert "form-action 'self'" in response.headers["Content-Security-Policy"]
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
        assert "Summary - CHP Forest Incidents" in body
        assert "Busiest Roads" in body
        assert '<a class="range-tab is-active" href="?hours=24&amp;region=forest" aria-current="page">24h</a>' in body
        assert '<a class="view-tab is-active" href="/summary?hours=24&amp;region=forest" aria-current="page">Summary</a>' in body

        response = client.get("/history?hours=24")
        body = response.text
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
        assert "History - CHP Forest Incidents" in body
        assert "Search road, type, incident number" in body
        assert '<a class="range-tab is-active" href="?hours=24&amp;region=forest" aria-current="page">24h</a>' in body
        assert '<a class="view-tab is-active" href="/history?hours=24&amp;region=forest" aria-current="page">History</a>' in body
        assert '<select class="filter" name="status" aria-label="Status filter">' in body

        response = client.get("/history?hours=24&status=active&mapped=mapped")
        body = response.text
        assert response.status_code == 200
        assert '<option value="active" selected>Active</option>' in body
        assert '<option value="mapped" selected>Mapped only</option>' in body

        response = client.get("/about?hours=24")
        body = response.text
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
        assert "About - CHP Forest Incidents" in body
        assert "Update Cadence" in body
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
        assert payload["region_statuses"]["forest"]["active_count"] == 0
        assert payload["region_statuses"]["malibu"]["active_count"] == 0

        response = client.get("/incidents.json?hours=24")
        payload = response.json()
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        assert response.headers["Cache-Control"] == INCIDENTS_CACHE_CONTROL
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
        assert b"CHP Forest Incidents" in response.content

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
        assert "CHP Malibu Incidents" in body
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
    active_event = {
        "event_key": "LACC|2026-06-08|1234",
        "center": "LACC",
        "incident_date": "2026-06-08",
        "incident_no": "1234",
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
    }
    upsert_active_event(conn, active_event)
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
