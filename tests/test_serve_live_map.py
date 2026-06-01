import threading
from urllib.request import HTTPError, urlopen
from urllib.request import Request

import serve_live_map
from serve_live_map import (
    ASSET_CACHE_CONTROL,
    DISCOVERY_CACHE_CONTROL,
    EcsHTTPServer,
    LiveMapHandler,
    MAP_CACHE_CONTROL,
)
from scrape_chp_traffic import connect_database


def test_live_map_handler_serves_health_base_path_and_404(tmp_path, monkeypatch):
    access_logs = []
    monkeypatch.setattr(serve_live_map, "log_event", lambda *args, **kwargs: access_logs.append((args, kwargs)))
    database = tmp_path / "chp.sqlite"
    connect_database(database).close()

    class TestHandler(LiveMapHandler):
        pass

    TestHandler.database = database
    TestHandler.database_url = None
    TestHandler.hours = 72.0
    TestHandler.base_path = "/chp"
    TestHandler.public_url = "https://chp.flowy.us/"

    server = EcsHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base_url}/healthz", timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"ok\n"

        request = Request(
            f"{base_url}/chp/",
            headers={
                "X-Forwarded-For": "203.0.113.7, 10.42.0.63",
                "CF-Connecting-IP": "198.51.100.8",
                "User-Agent": "test-browser/1.0",
            },
        )
        with urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert "CHP Forest Incidents" in body
            assert "in last 72h" in body
            assert '<link rel="icon" href="https://chp.flowy.us/favicon.svg" type="image/svg+xml">' in body
            assert '<meta property="og:image" content="https://chp.flowy.us/og-image.png">' in body
            assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
            assert "Pragma" not in response.headers
            assert "Expires" not in response.headers

        with urlopen(f"{base_url}/chp/?hours=24", timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert "in last 24h" in body
            assert '<a class="range-tab is-active" href="?hours=24" aria-current="page">24h</a>' in body

        with urlopen(f"{base_url}/chp/status.json?hours=24", timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/json; charset=utf-8"
            assert response.headers["Cache-Control"] == "private, max-age=15, stale-while-revalidate=30"
            assert '"active_count": 0' in body
            assert '"total_count": 0' in body
            assert '"version":' in body

        with urlopen(f"{base_url}/chp/?hours=9999", timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert "in last 720h" in body
            assert '<a class="range-tab is-active" href="?hours=720" aria-current="page">30d</a>' in body

        with urlopen(f"{base_url}/chp/favicon.svg", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "image/svg+xml"
            assert response.headers["Cache-Control"] == ASSET_CACHE_CONTROL
            assert b"<svg" in response.read()

        with urlopen(f"{base_url}/chp/og-image.svg", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "image/svg+xml"
            assert response.headers["Cache-Control"] == ASSET_CACHE_CONTROL
            assert b"CHP Forest Incidents" in response.read()

        with urlopen(f"{base_url}/chp/og-image.png", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "image/png"
            assert response.headers["Cache-Control"] == ASSET_CACHE_CONTROL
            assert response.read().startswith(b"\x89PNG\r\n\x1a\n")

        with urlopen(f"{base_url}/robots.txt", timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
            assert response.headers["Cache-Control"] == DISCOVERY_CACHE_CONTROL
            assert "User-agent: *" in body
            assert "Allow: /" in body
            assert "Sitemap: https://chp.flowy.us/sitemap.xml" in body

        with urlopen(f"{base_url}/sitemap.xml", timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/xml; charset=utf-8"
            assert response.headers["Cache-Control"] == DISCOVERY_CACHE_CONTROL
            assert "<loc>https://chp.flowy.us/</loc>" in body
            assert "<changefreq>hourly</changefreq>" in body

        with urlopen(f"{base_url}/metrics", timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
            assert response.headers["Cache-Control"] == "no-store"
            assert "chp_live_map_up 1" in body
            assert 'chp_live_map_incidents{status="total"} 0' in body
            assert "chp_live_map_http_requests_total" in body

        head_request = Request(f"{base_url}/chp/", method="HEAD")
        with urlopen(head_request, timeout=5) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
            assert response.read() == b""

        try:
            urlopen(f"{base_url}/missing", timeout=5)
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("expected /missing to return 404")

        logged_paths = [kwargs["url.path"] for _args, kwargs in access_logs]
        assert "/healthz" not in logged_paths
        assert "/chp/" in logged_paths
        assert "/missing" in logged_paths
        chp_log = next(kwargs for _args, kwargs in access_logs if kwargs["url.path"] == "/chp/")
        assert chp_log["client.address"] == "198.51.100.8"
        assert chp_log["client.nat.ip"] == "127.0.0.1"
        assert chp_log["http.request.header.x_forwarded_for"] == "203.0.113.7, 10.42.0.63"
        assert chp_log["http.request.header.cf_connecting_ip"] == "198.51.100.8"
        assert chp_log["http.request.header.user_agent"] == "test-browser/1.0"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
