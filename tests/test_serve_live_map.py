import threading
from urllib.request import HTTPError, urlopen
from urllib.request import Request

import serve_live_map
from serve_live_map import ASSET_CACHE_CONTROL, EcsHTTPServer, LiveMapHandler, MAP_CACHE_CONTROL
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
            headers={"X-Forwarded-For": "203.0.113.7, 10.42.0.63"},
        )
        with urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert "CHP Forest Incidents" in body
            assert '<link rel="icon" href="https://chp.flowy.us/favicon.svg" type="image/svg+xml">' in body
            assert '<meta property="og:image" content="https://chp.flowy.us/og-image.svg">' in body
            assert response.headers["Cache-Control"] == MAP_CACHE_CONTROL
            assert "Pragma" not in response.headers
            assert "Expires" not in response.headers

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
        assert chp_log["client.address"] == "203.0.113.7"
        assert chp_log["client.nat.ip"] == "127.0.0.1"
        assert chp_log["http.request.header.x_forwarded_for"] == "203.0.113.7, 10.42.0.63"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
