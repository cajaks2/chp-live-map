import argparse
import datetime as dt
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ecs_logging import log_event, log_exception, run_main
from generate_live_map import build_html, incident_status, load_incidents, normalize_base_path
from scrape_chp_traffic import connect_database


MAP_CACHE_CONTROL = "public, max-age=30, s-maxage=60, stale-while-revalidate=120, stale-if-error=600"
ASSET_CACHE_CONTROL = "public, max-age=86400, stale-while-revalidate=604800"
MIN_HISTORY_HOURS = 1.0
MAX_HISTORY_HOURS = 720.0
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#18392b"/>
  <path d="M10 48 25 17l9 20 6-11 14 22Z" fill="#f4f7ee"/>
  <path d="M20 48 30 30l7 18Z" fill="#6fbf73"/>
  <circle cx="47" cy="17" r="7" fill="#d83b3b"/>
</svg>
"""
OG_IMAGE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630">
  <rect width="1200" height="630" fill="#18392b"/>
  <path d="M0 492 160 235l93 151 70-104 116 171 128-243 135 243 103-151 157 190 238-334v472H0Z" fill="#2d5f46"/>
  <path d="M0 555 188 300l93 121 73-86 104 137 136-223 136 224 92-122 154 157 224-289v411H0Z" fill="#f4f7ee"/>
  <path d="M0 630h1200V494c-137 55-284 83-439 83-201 0-373-38-516-114C148 504 66 526 0 529Z" fill="#6fbf73"/>
  <circle cx="917" cy="192" r="52" fill="#d83b3b"/>
  <path d="M917 106c48 0 87 39 87 87 0 68-87 162-87 162s-87-94-87-162c0-48 39-87 87-87Zm0 45a42 42 0 1 0 0 84 42 42 0 0 0 0-84Z" fill="#f4f7ee"/>
  <text x="74" y="142" fill="#f4f7ee" font-family="Inter, Arial, sans-serif" font-size="62" font-weight="700">CHP Forest Incidents</text>
  <text x="78" y="218" fill="#d7e7d4" font-family="Inter, Arial, sans-serif" font-size="34">Live traffic incidents for Angeles Crest and nearby forest roads</text>
</svg>
"""


class LiveMapHandler(BaseHTTPRequestHandler):
    database = Path("chp_traffic.sqlite")
    database_url = None
    hours = 72.0
    base_path = "/"
    public_url = None

    def requested_hours(self):
        params = parse_qs(urlsplit(self.path).query)
        raw_hours = (params.get("hours") or [None])[0]
        if raw_hours is None:
            return self.hours
        try:
            hours = float(raw_hours)
        except (TypeError, ValueError):
            return self.hours
        return min(max(hours, MIN_HISTORY_HOURS), MAX_HISTORY_HOURS)

    def client_log_fields(self):
        forwarded_for = self.headers.get("X-Forwarded-For", "")
        forwarded_ip = forwarded_for.split(",", 1)[0].strip()
        real_ip = self.headers.get("X-Real-IP", "").strip()
        client_ip = forwarded_ip or real_ip or self.client_address[0]
        fields = {"client.address": client_ip}
        if self.client_address[0] != client_ip:
            fields["client.nat.ip"] = self.client_address[0]
        if forwarded_for:
            fields["http.request.header.x_forwarded_for"] = forwarded_for
        return fields

    def do_HEAD(self):
        self.serve_request(send_body=False)

    def do_GET(self):
        self.serve_request(send_body=True)

    def serve_request(self, send_body):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        base_path = normalize_base_path(self.base_path)
        map_paths = {"/", "/live_chp_map.html", base_path}
        status_paths = {"/status.json", f"{'' if base_path == '/' else base_path}/status.json"}
        asset_base = "" if base_path == "/" else base_path
        asset_paths = {
            f"{asset_base}/favicon.svg": ("image/svg+xml", FAVICON_SVG.encode("utf-8")),
            "/favicon.svg": ("image/svg+xml", FAVICON_SVG.encode("utf-8")),
            f"{asset_base}/og-image.svg": ("image/svg+xml", OG_IMAGE_SVG.encode("utf-8")),
        }

        if path in {"/healthz", "/readyz"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if send_body:
                self.wfile.write(b"ok\n")
            return

        if path in asset_paths:
            content_type, body = asset_paths[path]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", ASSET_CACHE_CONTROL)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return

        if path in status_paths:
            try:
                hours = self.requested_hours()
                incidents = load_incidents(self.database, hours, self.database_url)
                payload = {
                    **incident_status(incidents, hours),
                    "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                body = json.dumps(payload, sort_keys=True).encode("utf-8")
            except Exception as exc:
                log_exception(
                    "Failed to render CHP status",
                    exc,
                    **{
                        "event.action": "http_request",
                        "event.outcome": "failure",
                        "http.request.method": self.command,
                        "url.path": self.path,
                        "http.response.status_code": 500,
                        **self.client_log_fields(),
                    },
                )
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                if send_body:
                    self.wfile.write(b'{"error":"failed to render status"}\n')
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "private, max-age=15, stale-while-revalidate=30")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return

        if path not in map_paths:
            self.send_error(404)
            return

        try:
            generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            hours = self.requested_hours()
            incidents = load_incidents(self.database, hours, self.database_url)
            body = build_html(
                incidents,
                generated_at,
                hours,
                base_path=self.base_path,
                public_url=self.public_url,
            ).encode("utf-8")
        except Exception as exc:
            log_exception(
                "Failed to render CHP live map",
                exc,
                **{
                    "event.action": "http_request",
                    "event.outcome": "failure",
                    "http.request.method": self.command,
                    "url.path": self.path,
                    "http.response.status_code": 500,
                    **self.client_log_fields(),
                },
            )
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"failed to render map: {exc}\n".encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", MAP_CACHE_CONTROL)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
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
                "http.request.method": self.command,
                "http.response.status_code": status_code,
                "url.path": self.path,
                **self.client_log_fields(),
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
    parser.add_argument("--public-url", default=os.environ.get("PUBLIC_URL"))
    return parser.parse_args()


def main():
    args = parse_args()
    LiveMapHandler.database = args.database
    LiveMapHandler.database_url = args.database_url
    LiveMapHandler.hours = args.hours
    LiveMapHandler.base_path = args.base_path
    LiveMapHandler.public_url = args.public_url
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
