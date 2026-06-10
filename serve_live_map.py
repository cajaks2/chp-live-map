import argparse
import datetime as dt
import json
import os
import struct
import sys
import time
import zlib
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ecs_logging import log_event, log_exception, run_main
from generate_live_map import (
    build_about_html,
    build_history_html,
    build_html,
    build_summary_html,
    incident_status,
    load_incidents,
    normalize_base_path,
)
from scrape_chp_traffic import connect_database


MAP_CACHE_CONTROL = "public, max-age=30, s-maxage=60, stale-while-revalidate=120, stale-if-error=600"
INCIDENTS_CACHE_CONTROL = "public, max-age=15, s-maxage=30, stale-while-revalidate=60, stale-if-error=300"
ASSET_CACHE_CONTROL = "public, max-age=86400, stale-while-revalidate=604800"
FAVICON_CACHE_CONTROL = "public, max-age=30, s-maxage=30, stale-while-revalidate=60, stale-if-error=300"
DISCOVERY_CACHE_CONTROL = "public, max-age=300, s-maxage=300, stale-while-revalidate=600"
METRIC_REGIONS = ("forest", "malibu")
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://www.googletagmanager.com https://www.google-analytics.com; "
    "style-src 'self' 'unsafe-inline' https://unpkg.com; "
    "img-src 'self' data: https://unpkg.com https://*.tile.openstreetmap.org https://tile.openstreetmap.org https://www.google-analytics.com; "
    "connect-src 'self' https://www.google-analytics.com https://*.google-analytics.com; "
    "font-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'none'"
)
MIN_HISTORY_HOURS = 1.0
MAX_HISTORY_HOURS = 720.0
START_TIME = time.time()
HTTP_REQUESTS_TOTAL = defaultdict(int)
ACTIVE_MARKER_COLOR = "#d83b3b"
CLEAR_MARKER_COLOR = "#2f8a4e"
ACTIVE_MARKER_RGB = (216, 59, 59)
CLEAR_MARKER_RGB = (47, 138, 78)


def favicon_svg(active):
    marker_color = ACTIVE_MARKER_COLOR if active else CLEAR_MARKER_COLOR
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#18392b"/>
  <path d="M10 48 25 17l9 20 6-11 14 22Z" fill="#f4f7ee"/>
  <path d="M20 48 30 30l7 18Z" fill="#6fbf73"/>
  <circle cx="47" cy="17" r="7" fill="{marker_color}"/>
</svg>
"""


def favicon_active(incidents):
    return any(incident.get("status") == "active" for incident in incidents)
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


def png_chunk(chunk_type, data):
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def make_og_image_png():
    width = 1200
    height = 630
    pixels = bytearray([0x18, 0x39, 0x2B] * width * height)

    def set_pixel(x, y, color):
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(color)

    def fill_rect(x1, y1, x2, y2, color):
        x1 = max(0, min(width, x1))
        x2 = max(0, min(width, x2))
        y1 = max(0, min(height, y1))
        y2 = max(0, min(height, y2))
        row = bytes(color) * max(0, x2 - x1)
        for y in range(y1, y2):
            offset = (y * width + x1) * 3
            pixels[offset : offset + len(row)] = row

    def fill_polygon(points, color):
        min_y = max(0, min(y for _, y in points))
        max_y = min(height - 1, max(y for _, y in points))
        for y in range(min_y, max_y + 1):
            intersections = []
            for index, (x1, y1) in enumerate(points):
                x2, y2 = points[(index + 1) % len(points)]
                if y1 == y2:
                    continue
                if min(y1, y2) <= y < max(y1, y2):
                    intersections.append(int(x1 + (y - y1) * (x2 - x1) / (y2 - y1)))
            intersections.sort()
            for start, end in zip(intersections[0::2], intersections[1::2]):
                fill_rect(start, y, end + 1, y + 1, color)

    def fill_circle(cx, cy, radius, color):
        radius_squared = radius * radius
        for y in range(cy - radius, cy + radius + 1):
            dy = y - cy
            span = int((radius_squared - dy * dy) ** 0.5)
            fill_rect(cx - span, y, cx + span + 1, y + 1, color)

    fill_rect(0, 0, width, height, (24, 57, 43))
    fill_polygon([(0, 500), (155, 235), (250, 385), (320, 285), (438, 455), (565, 210), (700, 455), (805, 300), (960, 495), (1200, 155), (1200, 630), (0, 630)], (45, 95, 70))
    fill_polygon([(0, 560), (190, 300), (280, 420), (355, 335), (460, 472), (595, 248), (730, 475), (820, 350), (975, 508), (1200, 220), (1200, 630), (0, 630)], (244, 247, 238))
    fill_polygon([(0, 630), (0, 530), (245, 465), (485, 550), (760, 578), (990, 540), (1200, 492), (1200, 630)], (111, 191, 115))
    fill_circle(917, 192, 87, (244, 247, 238))
    fill_polygon([(917, 355), (850, 230), (984, 230)], (244, 247, 238))
    fill_circle(917, 192, 52, (216, 59, 59))
    fill_circle(917, 192, 24, (244, 247, 238))

    raw = bytearray()
    for y in range(height):
        row_start = y * width * 3
        raw.append(0)
        raw.extend(pixels[row_start : row_start + width * 3])
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + png_chunk(b"IEND", b"")
    )


OG_IMAGE_PNG = make_og_image_png()


def make_touch_icon_png(marker_color=ACTIVE_MARKER_RGB):
    width = 180
    height = 180
    pixels = bytearray([0x18, 0x39, 0x2B] * width * height)

    def fill_rect(x1, y1, x2, y2, color):
        x1 = max(0, min(width, x1))
        x2 = max(0, min(width, x2))
        y1 = max(0, min(height, y1))
        y2 = max(0, min(height, y2))
        row = bytes(color) * max(0, x2 - x1)
        for y in range(y1, y2):
            offset = (y * width + x1) * 3
            pixels[offset : offset + len(row)] = row

    def fill_polygon(points, color):
        min_y = max(0, min(y for _, y in points))
        max_y = min(height - 1, max(y for _, y in points))
        for y in range(min_y, max_y + 1):
            intersections = []
            for index, (x1, y1) in enumerate(points):
                x2, y2 = points[(index + 1) % len(points)]
                if y1 == y2:
                    continue
                if min(y1, y2) <= y < max(y1, y2):
                    intersections.append(int(x1 + (y - y1) * (x2 - x1) / (y2 - y1)))
            intersections.sort()
            for start, end in zip(intersections[0::2], intersections[1::2]):
                fill_rect(start, y, end + 1, y + 1, color)

    def fill_circle(cx, cy, radius, color):
        radius_squared = radius * radius
        for y in range(cy - radius, cy + radius + 1):
            dy = y - cy
            span = int((radius_squared - dy * dy) ** 0.5)
            fill_rect(cx - span, y, cx + span + 1, y + 1, color)

    fill_rect(0, 0, width, height, (24, 57, 43))
    fill_polygon([(0, 122), (32, 72), (57, 118), (84, 56), (118, 126), (150, 88), (180, 126), (180, 180), (0, 180)], (244, 247, 238))
    fill_polygon([(0, 180), (0, 142), (48, 130), (78, 144), (112, 148), (152, 138), (180, 132), (180, 180)], (111, 191, 115))
    fill_circle(135, 58, 24, (244, 247, 238))
    fill_polygon([(135, 105), (116, 68), (154, 68)], (244, 247, 238))
    fill_circle(135, 58, 15, marker_color)
    fill_circle(135, 58, 7, (244, 247, 238))

    raw = bytearray()
    for y in range(height):
        row_start = y * width * 3
        raw.append(0)
        raw.extend(pixels[row_start : row_start + width * 3])
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + png_chunk(b"IEND", b"")
    )


APPLE_TOUCH_ICON_PNG = make_touch_icon_png()


def public_canonical_url(base_path, public_url):
    if public_url:
        return public_url.rstrip("/") + "/"
    base = normalize_base_path(base_path)
    return base if base == "/" else f"{base}/"


def robots_txt(base_path, public_url):
    canonical = public_canonical_url(base_path, public_url)
    return "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            f"Sitemap: {canonical}sitemap.xml",
            "",
        ]
    ).encode("utf-8")


def sitemap_xml(base_path, public_url):
    canonical = public_canonical_url(base_path, public_url)
    now = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{canonical}</loc>
    <lastmod>{now}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>
""".encode("utf-8")


def parse_timestamp(value):
    if not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def metric_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metric_line(name, value, labels=None):
    if labels:
        label_text = ",".join(f'{key}="{metric_escape(label)}"' for key, label in labels.items())
        return f"{name}{{{label_text}}} {value}"
    return f"{name} {value}"


def scrape_run_metrics(database, database_url):
    if not database_url and not database.exists():
        return []
    with connect_database(database, database_url) as conn:
        latest = conn.execute(
            "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        status_rows = conn.execute(
            "SELECT http_status_counts FROM scrape_runs"
        ).fetchall()

    lines = [
        "# HELP chp_live_map_scrape_last_run_timestamp_seconds Unix timestamp of the latest completed CHP scrape.",
        "# TYPE chp_live_map_scrape_last_run_timestamp_seconds gauge",
    ]
    if latest:
        lines.extend(
            [
                metric_line(
                    "chp_live_map_scrape_last_run_timestamp_seconds",
                    f"{parse_timestamp(latest['observed_at']):.3f}",
                ),
                "# HELP chp_live_map_scrape_last_run_duration_seconds Duration of the latest completed CHP scrape.",
                "# TYPE chp_live_map_scrape_last_run_duration_seconds gauge",
                metric_line("chp_live_map_scrape_last_run_duration_seconds", latest["duration_seconds"]),
                "# HELP chp_live_map_scrape_last_run_incidents Incidents seen by the latest scrape.",
                "# TYPE chp_live_map_scrape_last_run_incidents gauge",
                metric_line("chp_live_map_scrape_last_run_incidents", latest["total_seen"], {"kind": "total_seen"}),
                metric_line("chp_live_map_scrape_last_run_incidents", latest["active_seen"], {"kind": "matched"}),
                metric_line("chp_live_map_scrape_last_run_incidents", latest["active_with_coords"], {"kind": "mapped"}),
                "# HELP chp_live_map_scrape_last_run_observations_inserted Observation rows inserted by the latest scrape.",
                "# TYPE chp_live_map_scrape_last_run_observations_inserted gauge",
                metric_line("chp_live_map_scrape_last_run_observations_inserted", latest["observations_inserted"]),
                "# HELP chp_live_map_scrape_last_run_details Detail pages requested or skipped by the latest scrape.",
                "# TYPE chp_live_map_scrape_last_run_details gauge",
                metric_line("chp_live_map_scrape_last_run_details", latest["details_requested"], {"result": "requested"}),
                metric_line("chp_live_map_scrape_last_run_details", latest["details_skipped"], {"result": "skipped"}),
            ]
        )
    else:
        lines.append("chp_live_map_scrape_last_run_timestamp_seconds 0")

    totals = defaultdict(int)
    for row in status_rows:
        try:
            counts = json.loads(row["http_status_counts"] or "{}")
        except (TypeError, ValueError):
            continue
        for key, count in counts.items():
            method, route, status = (key.split(":", 2) + ["", "", ""])[:3]
            totals[(method, route, status)] += int(count)
    lines.extend(
        [
            "# HELP chp_live_map_scrape_chp_http_requests_total Outbound CHP HTTP requests made by scraper, grouped by method, route, and status.",
            "# TYPE chp_live_map_scrape_chp_http_requests_total counter",
        ]
    )
    for (method, route, status), count in sorted(totals.items()):
        lines.append(
            metric_line(
                "chp_live_map_scrape_chp_http_requests_total",
                count,
                {"method": method, "route": route, "status": status},
            )
        )
    return lines


def prometheus_metrics(database, database_url, hours):
    incidents = load_incidents(database, hours, database_url)
    status = incident_status(incidents, hours)
    active_count = status["active_count"]
    cleared_count = status["total_count"] - active_count
    lines = [
        "# HELP chp_live_map_up Whether the CHP live map web process is running.",
        "# TYPE chp_live_map_up gauge",
        "chp_live_map_up 1",
        "# HELP chp_live_map_process_start_time_seconds Unix timestamp when the web process started.",
        "# TYPE chp_live_map_process_start_time_seconds gauge",
        metric_line("chp_live_map_process_start_time_seconds", f"{START_TIME:.3f}"),
        "# HELP chp_live_map_incidents Incidents in the selected history window.",
        "# TYPE chp_live_map_incidents gauge",
        metric_line("chp_live_map_incidents", status["total_count"], {"status": "total"}),
        metric_line("chp_live_map_incidents", active_count, {"status": "active"}),
        metric_line("chp_live_map_incidents", cleared_count, {"status": "cleared"}),
        metric_line("chp_live_map_incidents", status["mapped_count"], {"status": "mapped"}),
        "# HELP chp_live_map_region_incidents Incidents in the selected history window, grouped by hidden collection region.",
        "# TYPE chp_live_map_region_incidents gauge",
    ]
    for region in METRIC_REGIONS:
        region_status = incident_status(load_incidents(database, hours, database_url, region=region), hours)
        region_active_count = region_status["active_count"]
        region_cleared_count = region_status["total_count"] - region_active_count
        lines.extend(
            [
                metric_line(
                    "chp_live_map_region_incidents",
                    region_status["total_count"],
                    {"region": region, "status": "total"},
                ),
                metric_line(
                    "chp_live_map_region_incidents",
                    region_active_count,
                    {"region": region, "status": "active"},
                ),
                metric_line(
                    "chp_live_map_region_incidents",
                    region_cleared_count,
                    {"region": region, "status": "cleared"},
                ),
                metric_line(
                    "chp_live_map_region_incidents",
                    region_status["mapped_count"],
                    {"region": region, "status": "mapped"},
                ),
            ]
        )
    lines.extend(
        [
        "# HELP chp_live_map_history_window_hours History window used for map metrics.",
        "# TYPE chp_live_map_history_window_hours gauge",
        metric_line("chp_live_map_history_window_hours", status["hours"]),
        "# HELP chp_live_map_data_updated_timestamp_seconds Latest observed incident data timestamp.",
        "# TYPE chp_live_map_data_updated_timestamp_seconds gauge",
        metric_line(
            "chp_live_map_data_updated_timestamp_seconds",
            f"{parse_timestamp(status['data_updated_at']):.3f}",
        ),
        "# HELP chp_live_map_http_requests_total HTTP requests served by method, route, and status.",
        "# TYPE chp_live_map_http_requests_total counter",
        ]
    )
    lines.extend(scrape_run_metrics(database, database_url))
    for (method, route, status_code), count in sorted(HTTP_REQUESTS_TOTAL.items()):
        lines.append(
            metric_line(
                "chp_live_map_http_requests_total",
                count,
                {"method": method, "route": route, "status": status_code},
            )
        )
    lines.append("")
    return "\n".join(lines).encode("utf-8")


class LiveMapHandler(BaseHTTPRequestHandler):
    database = Path("chp_traffic.sqlite")
    database_url = None
    hours = 72.0
    base_path = "/"
    public_url = None
    google_analytics_id = None

    def send_response(self, code, message=None):
        self._last_status_code = int(code)
        super().send_response(code, message)

    def end_headers(self):
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=(), usb=()")
        super().end_headers()

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

    def history_filters(self):
        params = parse_qs(urlsplit(self.path).query)
        return {
            "q": (params.get("q") or [""])[0],
            "road": (params.get("road") or ["all"])[0],
            "type": (params.get("type") or ["all"])[0],
            "status": (params.get("status") or ["all"])[0],
            "mapped": (params.get("mapped") or ["all"])[0],
        }

    def client_log_fields(self):
        forwarded_for = self.headers.get("X-Forwarded-For", "")
        forwarded_ip = forwarded_for.split(",", 1)[0].strip()
        cloudflare_ip = self.headers.get("CF-Connecting-IP", "").strip()
        real_ip = self.headers.get("X-Real-IP", "").strip()
        client_ip = cloudflare_ip or forwarded_ip or real_ip or self.client_address[0]
        fields = {"client.address": client_ip}
        user_agent = self.headers.get("User-Agent", "").strip()
        if self.client_address[0] != client_ip:
            fields["client.nat.ip"] = self.client_address[0]
        if forwarded_for:
            fields["http.request.header.x_forwarded_for"] = forwarded_for
        if cloudflare_ip:
            fields["http.request.header.cf_connecting_ip"] = cloudflare_ip
        if user_agent:
            fields["http.request.header.user_agent"] = user_agent
        cloudflare_geo_headers = {
            "CF-IPCountry": (
                "http.request.header.cf_ipcountry",
                "client.geo.country_iso_code",
            ),
            "CF-IPContinent": (
                "http.request.header.cf_ipcontinent",
                "client.geo.continent_code",
            ),
            "CF-IPCity": (
                "http.request.header.cf_ipcity",
                "client.geo.city_name",
            ),
            "CF-Region": (
                "http.request.header.cf_region",
                "client.geo.region_name",
            ),
            "CF-Region-Code": (
                "http.request.header.cf_region_code",
                "client.geo.region_iso_code",
            ),
            "CF-Postal-Code": (
                "http.request.header.cf_postal_code",
                "client.geo.postal_code",
            ),
            "CF-Timezone": (
                "http.request.header.cf_timezone",
                "client.geo.timezone",
            ),
            "CF-IPLatitude": (
                "http.request.header.cf_iplatitude",
                "client.geo.location.lat",
            ),
            "CF-IPLongitude": (
                "http.request.header.cf_iplongitude",
                "client.geo.location.lon",
            ),
            "CF-Ray": (
                "http.request.header.cf_ray",
                None,
            ),
        }
        for header, (raw_field, ecs_field) in cloudflare_geo_headers.items():
            value = self.headers.get(header, "").strip()
            if not value:
                continue
            fields[raw_field] = value
            if ecs_field:
                fields[ecs_field] = value
        return fields

    def route_label(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        base_path = normalize_base_path(self.base_path)
        asset_base = "" if base_path == "/" else base_path
        if path in {"/", "/live_chp_map.html", base_path}:
            return "map"
        if path in {"/summary", f"{asset_base}/summary"}:
            return "summary"
        if path in {"/history", f"{asset_base}/history"}:
            return "history"
        if path in {"/about", f"{asset_base}/about"}:
            return "about"
        if path in {"/status.json", f"{asset_base}/status.json"}:
            return "status"
        if path in {"/incidents.json", f"{asset_base}/incidents.json"}:
            return "incidents"
        if path in {"/metrics", f"{asset_base}/metrics"}:
            return "metrics"
        if path in {"/healthz", "/readyz"}:
            return "health"
        if path in {"/robots.txt", f"{asset_base}/robots.txt"}:
            return "robots"
        if path in {"/sitemap.xml", f"{asset_base}/sitemap.xml"}:
            return "sitemap"
        if path.endswith(".svg") or path.endswith(".png") or path.endswith(".ico"):
            return "asset"
        return "other"

    def do_HEAD(self):
        self.serve_request(send_body=False)

    def do_GET(self):
        self.serve_request(send_body=True)

    def serve_request(self, send_body):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        base_path = normalize_base_path(self.base_path)
        map_paths = {"/", "/live_chp_map.html", base_path}
        summary_paths = {"/summary", f"{'' if base_path == '/' else base_path}/summary"}
        history_paths = {"/history", f"{'' if base_path == '/' else base_path}/history"}
        about_paths = {"/about", f"{'' if base_path == '/' else base_path}/about"}
        status_paths = {"/status.json", f"{'' if base_path == '/' else base_path}/status.json"}
        incidents_paths = {"/incidents.json", f"{'' if base_path == '/' else base_path}/incidents.json"}
        asset_base = "" if base_path == "/" else base_path
        robots_paths = {"/robots.txt", f"{asset_base}/robots.txt"}
        sitemap_paths = {"/sitemap.xml", f"{asset_base}/sitemap.xml"}
        metrics_paths = {"/metrics", f"{asset_base}/metrics"}
        favicon_svg_paths = {"/favicon.svg", f"{asset_base}/favicon.svg"}
        favicon_ico_paths = {"/favicon.ico", f"{asset_base}/favicon.ico"}
        apple_touch_icon_paths = {
            "/apple-touch-icon.png",
            "/apple-touch-icon-precomposed.png",
            "/apple-touch-icon-120x120.png",
            "/apple-touch-icon-120x120-precomposed.png",
            "/apple-touch-icon-152x152.png",
            "/apple-touch-icon-152x152-precomposed.png",
            "/apple-touch-icon-167x167.png",
            "/apple-touch-icon-167x167-precomposed.png",
            "/apple-touch-icon-180x180.png",
            "/apple-touch-icon-180x180-precomposed.png",
        }
        asset_paths = {
            f"{asset_base}/og-image.svg": ("image/svg+xml", OG_IMAGE_SVG.encode("utf-8")),
            f"{asset_base}/og-image.png": ("image/png", OG_IMAGE_PNG),
            "/og-image.png": ("image/png", OG_IMAGE_PNG),
            **{path: ("image/png", APPLE_TOUCH_ICON_PNG) for path in apple_touch_icon_paths},
            **{f"{asset_base}{path}": ("image/png", APPLE_TOUCH_ICON_PNG) for path in apple_touch_icon_paths if asset_base},
        }

        if path in {"/healthz", "/readyz"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if send_body:
                self.wfile.write(b"ok\n")
            return

        if path in favicon_svg_paths or path in favicon_ico_paths:
            try:
                active = favicon_active(load_incidents(self.database, self.hours, self.database_url))
            except Exception as exc:
                log_exception(
                    "Failed to render dynamic favicon",
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
                active = False
            if path in favicon_svg_paths:
                body = favicon_svg(active).encode("utf-8")
                content_type = "image/svg+xml"
            else:
                marker_color = ACTIVE_MARKER_RGB if active else CLEAR_MARKER_RGB
                body = make_touch_icon_png(marker_color)
                content_type = "image/png"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", FAVICON_CACHE_CONTROL)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
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

        if path in robots_paths:
            body = robots_txt(self.base_path, self.public_url)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", DISCOVERY_CACHE_CONTROL)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return

        if path in sitemap_paths:
            body = sitemap_xml(self.base_path, self.public_url)
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Cache-Control", DISCOVERY_CACHE_CONTROL)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return

        if path in metrics_paths:
            try:
                body = prometheus_metrics(self.database, self.database_url, self.hours)
            except Exception as exc:
                log_exception(
                    "Failed to render Prometheus metrics",
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
                if send_body:
                    self.wfile.write(b"failed to render metrics\n")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
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

        if path in incidents_paths:
            try:
                hours = self.requested_hours()
                incidents = load_incidents(self.database, hours, self.database_url)
                payload = {
                    "incidents": incidents,
                    "status": incident_status(incidents, hours),
                    "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
            except Exception as exc:
                log_exception(
                    "Failed to render CHP incidents API",
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
                    self.wfile.write(b'{"error":"failed to render incidents"}\n')
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", INCIDENTS_CACHE_CONTROL)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return

        if path not in map_paths and path not in summary_paths and path not in history_paths and path not in about_paths:
            self.send_error(404)
            return

        try:
            generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            hours = self.requested_hours()
            incidents = load_incidents(self.database, hours, self.database_url)
            if path in summary_paths:
                body = build_summary_html(
                    incidents,
                    generated_at,
                    hours,
                    base_path=self.base_path,
                    public_url=self.public_url,
                ).encode("utf-8")
            elif path in history_paths:
                body = build_history_html(
                    incidents,
                    generated_at,
                    hours,
                    base_path=self.base_path,
                    public_url=self.public_url,
                    filters=self.history_filters(),
                ).encode("utf-8")
            elif path in about_paths:
                body = build_about_html(
                    incidents,
                    generated_at,
                    hours,
                    base_path=self.base_path,
                    public_url=self.public_url,
                ).encode("utf-8")
            else:
                body = build_html(
                    incidents,
                    generated_at,
                    hours,
                    base_path=self.base_path,
                    public_url=self.public_url,
                    google_analytics_id=self.google_analytics_id,
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
                try:
                    status_code = int(args[0])
                except (IndexError, TypeError, ValueError):
                    status_code = None
        if status_code is None:
            status_code = getattr(self, "_last_status_code", None)
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if status_code:
            HTTP_REQUESTS_TOTAL[(self.command, self.route_label(), str(status_code))] += 1
        if path in {"/healthz", "/readyz", "/metrics"} and status_code and status_code < 500:
            return
        log_event(
            "info",
            "HTTP request completed",
            **{
                "event.action": "http_request",
                "event.outcome": "success" if status_code and status_code < 400 else "failure",
                "http.request.method": self.command,
                "http.response.status_code": status_code,
                "url.path": self.path,
                **self.client_log_fields(),
            },
        )

    def log_error(self, fmt, *args):
        # send_error() calls this before the final access log; keep one ECS event per request.
        return


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
    parser.add_argument("--google-analytics-id", default=os.environ.get("GOOGLE_ANALYTICS_ID"))
    return parser.parse_args()


def main():
    args = parse_args()
    LiveMapHandler.database = args.database
    LiveMapHandler.database_url = args.database_url
    LiveMapHandler.hours = args.hours
    LiveMapHandler.base_path = args.base_path
    LiveMapHandler.public_url = args.public_url
    LiveMapHandler.google_analytics_id = args.google_analytics_id
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
