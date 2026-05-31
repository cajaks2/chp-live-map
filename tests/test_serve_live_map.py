import threading
from urllib.request import HTTPError, urlopen

from serve_live_map import EcsHTTPServer, LiveMapHandler
from scrape_chp_traffic import connect_database


def test_live_map_handler_serves_health_base_path_and_404(tmp_path):
    database = tmp_path / "chp.sqlite"
    connect_database(database).close()

    class TestHandler(LiveMapHandler):
        pass

    TestHandler.database = database
    TestHandler.database_url = None
    TestHandler.hours = 72.0
    TestHandler.base_path = "/chp"

    server = EcsHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base_url}/healthz", timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"ok\n"

        with urlopen(f"{base_url}/chp/", timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert "CHP Forest Incidents" in body
            assert response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
            assert response.headers["Pragma"] == "no-cache"
            assert response.headers["Expires"] == "0"

        try:
            urlopen(f"{base_url}/missing", timeout=5)
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("expected /missing to return 404")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
