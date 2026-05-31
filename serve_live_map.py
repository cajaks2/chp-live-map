import argparse
import datetime as dt
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from generate_live_map import build_html, load_incidents
from scrape_chp_traffic import connect_database


class LiveMapHandler(BaseHTTPRequestHandler):
    database = Path("chp_traffic.sqlite")
    database_url = None
    hours = 24.0

    def do_GET(self):
        if self.path in {"/healthz", "/readyz"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return

        if self.path not in {"/", "/live_chp_map.html"}:
            self.send_error(404)
            return

        try:
            generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            incidents = load_incidents(self.database, self.hours, self.database_url)
            body = build_html(incidents, generated_at, self.hours).encode("utf-8")
        except Exception as exc:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"failed to render map: {exc}\n".encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("%s - - %s" % (self.address_string(), fmt % args))


def parse_args():
    parser = argparse.ArgumentParser(description="Serve the CHP live map from SQL.")
    parser.add_argument("--host", default=os.environ.get("HTTP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HTTP_PORT", "8080")))
    parser.add_argument("--database", type=Path, default=Path(os.environ.get("DATABASE", "chp_traffic.sqlite")))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--hours", type=float, default=float(os.environ.get("MAP_HOURS", "24")))
    return parser.parse_args()


def main():
    args = parse_args()
    LiveMapHandler.database = args.database
    LiveMapHandler.database_url = args.database_url
    LiveMapHandler.hours = args.hours
    with connect_database(args.database, args.database_url):
        pass
    server = ThreadingHTTPServer((args.host, args.port), LiveMapHandler)
    print(f"Serving CHP live map on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
