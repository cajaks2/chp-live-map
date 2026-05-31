import argparse
import datetime as dt
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ecs_logging import log_event, log_exception, run_main
from generate_live_map import build_html, load_incidents
from scrape_chp_traffic import connect_database


class LiveMapHandler(BaseHTTPRequestHandler):
    database = Path("chp_traffic.sqlite")
    database_url = None
    hours = 72.0
    base_path = "/"

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        base_path = self.base_path.rstrip("/") or "/"
        map_paths = {"/", "/live_chp_map.html", base_path}

        if path in {"/healthz", "/readyz"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return

        if path not in map_paths:
            self.send_error(404)
            return

        try:
            generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            incidents = load_incidents(self.database, self.hours, self.database_url)
            body = build_html(incidents, generated_at, self.hours).encode("utf-8")
        except Exception as exc:
            log_exception(
                "Failed to render CHP live map",
                exc,
                **{
                    "event.action": "http_request",
                    "event.outcome": "failure",
                    "http.request.method": self.command,
                    "url.path": self.path,
                    "client.address": self.client_address[0],
                    "http.response.status_code": 500,
                },
            )
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"failed to render map: {exc}\n".encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        status_code = None
        if args:
            try:
                status_code = int(args[1])
            except (IndexError, TypeError, ValueError):
                status_code = None
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path in {"/healthz", "/readyz"} and status_code and status_code < 500:
            return
        log_event(
            "info",
            "HTTP request completed",
            **{
                "event.action": "http_request",
                "event.outcome": "success" if status_code and status_code < 500 else "failure",
                "client.address": self.client_address[0],
                "http.request.method": self.command,
                "http.response.status_code": status_code,
                "url.path": self.path,
            },
        )


class EcsHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        _exc_type, exc, _tb = sys.exc_info()
        if exc is None:
            return
        log_exception(
            "HTTP server request handler failed",
            exc,
            **{
                "event.action": "http_request",
                "event.outcome": "failure",
                "client.address": client_address[0],
            },
        )


def parse_args():
    parser = argparse.ArgumentParser(description="Serve the CHP live map from SQL.")
    parser.add_argument("--host", default=os.environ.get("HTTP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HTTP_PORT", "8080")))
    parser.add_argument("--database", type=Path, default=Path(os.environ.get("DATABASE", "chp_traffic.sqlite")))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--hours", type=float, default=float(os.environ.get("MAP_HOURS", "72")))
    parser.add_argument("--base-path", default=os.environ.get("BASE_PATH", "/"))
    return parser.parse_args()


def main():
    args = parse_args()
    LiveMapHandler.database = args.database
    LiveMapHandler.database_url = args.database_url
    LiveMapHandler.hours = args.hours
    LiveMapHandler.base_path = args.base_path
    with connect_database(args.database, args.database_url):
        pass
    server = EcsHTTPServer((args.host, args.port), LiveMapHandler)
    log_event(
        "info",
        "Serving CHP live map",
        **{
            "event.action": "start",
            "network.transport": "tcp",
            "server.address": args.host,
            "server.port": args.port,
            "url.path": args.base_path,
            "chp.hours": args.hours,
        },
    )
    server.serve_forever()


if __name__ == "__main__":
    run_main(main)
