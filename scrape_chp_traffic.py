import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo

from ecs_logging import log_event, log_exception, run_main
from geo_bounds import clear_coordinates_outside_region_bounds, coordinates_in_region_bounds


CHP_TRAFFIC_URL = "https://cad.chp.ca.gov/Traffic.aspx"
CHP_MEDIA_XML_URL = "https://media.chp.ca.gov/sa_xml/sa.xml"
CHP_ROBOTS_URL = "https://cad.chp.ca.gov/robots.txt"
DEFAULT_USER_AGENT = "chp-live-map/0.1 (+https://crestmap.us/)"
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_CENTERS = ["LACC", "VTCC"]
FOREST_ROAD_KEYWORDS = [
    "angeles crest",
    "angeles forest",
    "upper big tujunga",
    "big tujunga canyon",
    "mt wilson",
    "mount wilson",
    "mt wilson red box",
    "red box",
    "san gabriel canyon",
    "glendora mountain",
    "glendora ridge",
    "mt baldy",
    "mount baldy",
    "san antonio canyon",
]
MALIBU_ROAD_KEYWORDS = [
    "pacific coast hwy",
    "pch",
    "ca-1",
    "ca 1",
    "sr1",
    "sr 1",
    "malibu canyon",
    "topanga canyon",
    "las virgenes",
    "kanan dume",
    "kanan",
    "decker canyon",
    "decker rd",
    "mulholland hwy",
    "latigo canyon",
    "encinal canyon",
    "corral canyon",
    "tuna canyon",
    "piuma rd",
    "stunt rd",
    "old topanga",
    "carbon canyon",
    "trancas canyon",
    "zuma beach",
    "point dume",
    "malibu rd",
    "cross creek",
    "webb way",
    "civic center way",
    "busch dr",
    "bonsall dr",
]
REGION_ROAD_KEYWORDS = {
    "forest": FOREST_ROAD_KEYWORDS,
    "malibu": MALIBU_ROAD_KEYWORDS,
}
DEFAULT_ROAD_KEYWORDS = FOREST_ROAD_KEYWORDS + MALIBU_ROAD_KEYWORDS
HIGHWAY_39_ALIASES = ["highway 39", "hwy 39", "ca-39", "ca 39", "sr39", "sr 39"]
ROUTE_ALIAS_PATTERNS = {
    "ca-1": re.compile(r"\bca[-\s]*1\b"),
    "ca 1": re.compile(r"\bca[-\s]*1\b"),
    "sr1": re.compile(r"\bsr\s*1\b"),
    "sr 1": re.compile(r"\bsr\s*1\b"),
    "ca-39": re.compile(r"\bca[-\s]*39\b"),
    "ca 39": re.compile(r"\bca[-\s]*39\b"),
    "sr39": re.compile(r"\bsr\s*39\b"),
    "sr 39": re.compile(r"\bsr\s*39\b"),
}
HIGHWAY_39_FOREST_CONTEXT = [
    "san gabriel canyon",
    "east fork",
    "crystal lake",
    "morris reservoir",
    "west fork",
    "north fork",
    "coldbrook",
    "soldier creek",
    "island mountain",
    "island rd",
    "islip",
    "mm ",
]
SCRAPER_START_TIME = time.time()


def metric_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metric_line(name, value, labels=None):
    if labels:
        label_text = ",".join(f'{key}="{metric_escape(label)}"' for key, label in labels.items())
        return f"{name}{{{label_text}}} {value}"
    return f"{name} {value}"


class ScraperMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.scrapes = {"success": 0, "failure": 0}
        self.source_attempts = {}
        self.source_compares = {"success": 0, "failure": 0, "mismatch": 0}
        self.http_status_counts = {}
        self.last = {}
        self.last_source_compare = {}

    def record_chp_http(self, method, route, status):
        key = (str(method), str(route), str(status))
        with self.lock:
            self.http_status_counts[key] = self.http_status_counts.get(key, 0) + 1

    def record_source_attempt(self, source, mode, outcome):
        key = (str(source), str(mode), str(outcome))
        with self.lock:
            self.source_attempts[key] = self.source_attempts.get(key, 0) + 1

    def record_success(
        self,
        observed_at,
        changed_rows,
        total_seen,
        active_seen,
        active_with_coords,
        region_counts,
        details_requested,
        details_skipped,
        duration_seconds,
        source_durations=None,
        source_bytes=None,
    ):
        with self.lock:
            self.scrapes["success"] += 1
            self.last = {
                "outcome": "success",
                "observed_at": observed_at,
                "duration_seconds": duration_seconds,
                "source_durations": source_durations or {"total": duration_seconds},
                "source_bytes": source_bytes or {},
                "observations_inserted": changed_rows,
                "total_seen": total_seen,
                "active_seen": active_seen,
                "active_with_coords": active_with_coords,
                "region_counts": region_counts or {},
                "details_requested": details_requested,
                "details_skipped": details_skipped,
                "error_type": "",
            }

    def record_failure(self, observed_at, duration_seconds, exc):
        with self.lock:
            self.scrapes["failure"] += 1
            self.last = {
                "outcome": "failure",
                "observed_at": observed_at,
                "duration_seconds": duration_seconds,
                "source_durations": {"total": duration_seconds},
                "source_bytes": {},
                "observations_inserted": 0,
                "total_seen": 0,
                "active_seen": 0,
                "active_with_coords": 0,
                "region_counts": {},
                "details_requested": 0,
                "details_skipped": 0,
                "error_type": exc.__class__.__name__,
            }

    def record_source_compare_success(
        self,
        observed_at,
        duration_seconds,
        cad_total_seen,
        cad_matched,
        cad_mapped,
        cad_region_counts,
        xml_total_seen,
        xml_matched,
        xml_mapped,
        xml_region_counts,
        overlap_matched,
        cad_only,
        xml_only,
    ):
        has_mismatch = cad_only > 0 or xml_only > 0
        with self.lock:
            self.source_compares["success"] += 1
            if has_mismatch:
                self.source_compares["mismatch"] += 1
            self.last_source_compare = {
                "outcome": "success",
                "observed_at": observed_at,
                "duration_seconds": duration_seconds,
                "cad_total_seen": cad_total_seen,
                "cad_matched": cad_matched,
                "cad_mapped": cad_mapped,
                "cad_region_counts": cad_region_counts or {},
                "xml_total_seen": xml_total_seen,
                "xml_matched": xml_matched,
                "xml_mapped": xml_mapped,
                "xml_region_counts": xml_region_counts or {},
                "overlap_matched": overlap_matched,
                "cad_only": cad_only,
                "xml_only": xml_only,
                "mismatch": 1 if has_mismatch else 0,
                "error_type": "",
            }

    def record_source_compare_failure(self, observed_at, duration_seconds, exc):
        with self.lock:
            self.source_compares["failure"] += 1
            self.last_source_compare = {
                "outcome": "failure",
                "observed_at": observed_at,
                "duration_seconds": duration_seconds,
                "cad_total_seen": 0,
                "cad_matched": 0,
                "cad_mapped": 0,
                "cad_region_counts": {},
                "xml_total_seen": 0,
                "xml_matched": 0,
                "xml_mapped": 0,
                "xml_region_counts": {},
                "overlap_matched": 0,
                "cad_only": 0,
                "xml_only": 0,
                "mismatch": 0,
                "error_type": exc.__class__.__name__,
            }

    def render(self):
        with self.lock:
            scrapes = dict(self.scrapes)
            source_attempts = dict(self.source_attempts)
            source_compares = dict(self.source_compares)
            http_counts = dict(self.http_status_counts)
            last = dict(self.last)
            last_source_compare = dict(self.last_source_compare)
        lines = [
            "# HELP chp_live_map_scraper_up Whether the scraper metrics service is running.",
            "# TYPE chp_live_map_scraper_up gauge",
            "chp_live_map_scraper_up 1",
            "# HELP chp_live_map_scraper_process_start_time_seconds Unix timestamp when the scraper process started.",
            "# TYPE chp_live_map_scraper_process_start_time_seconds gauge",
            metric_line("chp_live_map_scraper_process_start_time_seconds", f"{SCRAPER_START_TIME:.3f}"),
            "# HELP chp_live_map_scraper_scrapes_total Scrape attempts by outcome.",
            "# TYPE chp_live_map_scraper_scrapes_total counter",
        ]
        for outcome, count in sorted(scrapes.items()):
            lines.append(metric_line("chp_live_map_scraper_scrapes_total", count, {"outcome": outcome}))
        lines.extend(
            [
                "# HELP chp_live_map_scraper_source_attempts_total Scraper source attempts by source, mode, and outcome.",
                "# TYPE chp_live_map_scraper_source_attempts_total counter",
            ]
        )
        for (source, mode, outcome), count in sorted(source_attempts.items()):
            lines.append(
                metric_line(
                    "chp_live_map_scraper_source_attempts_total",
                    count,
                    {"source": source, "mode": mode, "outcome": outcome},
                )
            )
        lines.extend(
            [
                "# HELP chp_live_map_scraper_source_compare_runs_total Source comparison runs by outcome.",
                "# TYPE chp_live_map_scraper_source_compare_runs_total counter",
            ]
        )
        for outcome, count in sorted(source_compares.items()):
            lines.append(metric_line("chp_live_map_scraper_source_compare_runs_total", count, {"outcome": outcome}))
        lines.extend(
            [
                "# HELP chp_live_map_scraper_last_run_timestamp_seconds Unix timestamp of the latest scraper run.",
                "# TYPE chp_live_map_scraper_last_run_timestamp_seconds gauge",
                metric_line(
                    "chp_live_map_scraper_last_run_timestamp_seconds",
                    f"{parse_metric_timestamp(last.get('observed_at')):.3f}",
                    {"outcome": last.get("outcome", "none"), "error_type": last.get("error_type", "")},
                ),
                "# HELP chp_live_map_scraper_last_run_duration_seconds Duration of the latest scraper run.",
                "# TYPE chp_live_map_scraper_last_run_duration_seconds gauge",
                metric_line("chp_live_map_scraper_last_run_duration_seconds", last.get("duration_seconds", 0)),
                "# HELP chp_live_map_scraper_last_run_source_duration_seconds Duration of the latest scraper run grouped by source.",
                "# TYPE chp_live_map_scraper_last_run_source_duration_seconds gauge",
            ]
        )
        source_durations = dict(last.get("source_durations") or {})
        source_durations.setdefault("total", last.get("duration_seconds", 0))
        for source, duration_seconds in sorted(source_durations.items()):
            lines.append(
                metric_line(
                    "chp_live_map_scraper_last_run_source_duration_seconds",
                    duration_seconds,
                    {"source": source},
                )
            )
        lines.extend(
            [
                "# HELP chp_live_map_scraper_last_run_source_response_bytes Bytes downloaded by the latest scraper run grouped by source.",
                "# TYPE chp_live_map_scraper_last_run_source_response_bytes gauge",
            ]
        )
        source_bytes = dict(last.get("source_bytes") or {})
        for source in ("cad", "xml"):
            source_bytes.setdefault(source, 0)
        source_bytes["total"] = source_bytes.get("cad", 0) + source_bytes.get("xml", 0)
        for source, byte_count in sorted(source_bytes.items()):
            lines.append(
                metric_line(
                    "chp_live_map_scraper_last_run_source_response_bytes",
                    byte_count,
                    {"source": source},
                )
            )
        lines.extend(
            [
                "# HELP chp_live_map_scraper_last_run_incidents Incidents seen by the latest scraper run.",
                "# TYPE chp_live_map_scraper_last_run_incidents gauge",
                metric_line("chp_live_map_scraper_last_run_incidents", last.get("total_seen", 0), {"kind": "total_seen"}),
                metric_line("chp_live_map_scraper_last_run_incidents", last.get("active_seen", 0), {"kind": "matched"}),
                metric_line("chp_live_map_scraper_last_run_incidents", last.get("active_with_coords", 0), {"kind": "mapped"}),
                "# HELP chp_live_map_scraper_last_run_region_incidents Incidents matched by the latest scraper run, grouped by hidden region and coordinate availability.",
                "# TYPE chp_live_map_scraper_last_run_region_incidents gauge",
            ]
        )
        for region, counts in sorted((last.get("region_counts") or {}).items()):
            lines.append(
                metric_line(
                    "chp_live_map_scraper_last_run_region_incidents",
                    counts.get("matched", 0),
                    {"region": region, "kind": "matched"},
                )
            )
            lines.append(
                metric_line(
                    "chp_live_map_scraper_last_run_region_incidents",
                    counts.get("mapped", 0),
                    {"region": region, "kind": "mapped"},
                )
            )
        lines.extend(
            [
                "# HELP chp_live_map_scraper_source_compare_last_run_timestamp_seconds Unix timestamp of the latest source comparison run.",
                "# TYPE chp_live_map_scraper_source_compare_last_run_timestamp_seconds gauge",
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_timestamp_seconds",
                    f"{parse_metric_timestamp(last_source_compare.get('observed_at')):.3f}",
                    {
                        "outcome": last_source_compare.get("outcome", "none"),
                        "error_type": last_source_compare.get("error_type", ""),
                    },
                ),
                "# HELP chp_live_map_scraper_source_compare_last_run_duration_seconds Duration of the latest source comparison run.",
                "# TYPE chp_live_map_scraper_source_compare_last_run_duration_seconds gauge",
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_duration_seconds",
                    last_source_compare.get("duration_seconds", 0),
                ),
                "# HELP chp_live_map_scraper_source_compare_last_run_incidents Last source comparison incident counts by source/result.",
                "# TYPE chp_live_map_scraper_source_compare_last_run_incidents gauge",
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_incidents",
                    last_source_compare.get("cad_total_seen", 0),
                    {"source": "cad", "kind": "total_seen"},
                ),
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_incidents",
                    last_source_compare.get("cad_matched", 0),
                    {"source": "cad", "kind": "matched"},
                ),
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_incidents",
                    last_source_compare.get("cad_mapped", 0),
                    {"source": "cad", "kind": "mapped"},
                ),
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_incidents",
                    last_source_compare.get("xml_total_seen", 0),
                    {"source": "xml", "kind": "total_seen"},
                ),
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_incidents",
                    last_source_compare.get("xml_matched", 0),
                    {"source": "xml", "kind": "matched"},
                ),
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_incidents",
                    last_source_compare.get("xml_mapped", 0),
                    {"source": "xml", "kind": "mapped"},
                ),
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_incidents",
                    last_source_compare.get("overlap_matched", 0),
                    {"source": "comparison", "kind": "overlap"},
                ),
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_incidents",
                    last_source_compare.get("cad_only", 0),
                    {"source": "comparison", "kind": "cad_only"},
                ),
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_incidents",
                    last_source_compare.get("xml_only", 0),
                    {"source": "comparison", "kind": "xml_only"},
                ),
                metric_line(
                    "chp_live_map_scraper_source_compare_last_run_incidents",
                    last_source_compare.get("mismatch", 0),
                    {"source": "comparison", "kind": "mismatch"},
                ),
                "# HELP chp_live_map_scraper_source_compare_last_run_region_incidents Last source comparison incidents grouped by source, region, and coordinate availability.",
                "# TYPE chp_live_map_scraper_source_compare_last_run_region_incidents gauge",
            ]
        )
        for source, region_counts in (
            ("cad", last_source_compare.get("cad_region_counts") or {}),
            ("xml", last_source_compare.get("xml_region_counts") or {}),
        ):
            for region, counts in sorted(region_counts.items()):
                lines.append(
                    metric_line(
                        "chp_live_map_scraper_source_compare_last_run_region_incidents",
                        counts.get("matched", 0),
                        {"source": source, "region": region, "kind": "matched"},
                    )
                )
                lines.append(
                    metric_line(
                        "chp_live_map_scraper_source_compare_last_run_region_incidents",
                        counts.get("mapped", 0),
                        {"source": source, "region": region, "kind": "mapped"},
                    )
                )
        lines.extend(
            [
                "# HELP chp_live_map_scraper_last_run_observations_inserted Observation rows inserted by the latest scraper run.",
                "# TYPE chp_live_map_scraper_last_run_observations_inserted gauge",
                metric_line("chp_live_map_scraper_last_run_observations_inserted", last.get("observations_inserted", 0)),
                "# HELP chp_live_map_scraper_last_run_details Detail pages requested or skipped by the latest scraper run.",
                "# TYPE chp_live_map_scraper_last_run_details gauge",
                metric_line("chp_live_map_scraper_last_run_details", last.get("details_requested", 0), {"result": "requested"}),
                metric_line("chp_live_map_scraper_last_run_details", last.get("details_skipped", 0), {"result": "skipped"}),
                "# HELP chp_live_map_scraper_chp_http_requests_total Outbound CHP HTTP requests made by scraper, grouped by method, route, and status.",
                "# TYPE chp_live_map_scraper_chp_http_requests_total counter",
            ]
        )
        for (method, route, status), count in sorted(http_counts.items()):
            lines.append(
                metric_line(
                    "chp_live_map_scraper_chp_http_requests_total",
                    count,
                    {"method": method, "route": route, "status": status},
                )
            )
        lines.append("")
        return "\n".join(lines).encode("utf-8")


SCRAPER_METRICS = ScraperMetrics()


class ChpTrafficParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = {}
        self.spans = {}
        self.tables = {}
        self._span_id = None
        self._span_parts = []
        self._table_id = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "hidden" and attrs.get("name"):
            self.hidden[attrs["name"]] = attrs.get("value", "")
        if tag == "span" and attrs.get("id"):
            self._span_id = attrs["id"]
            self._span_parts = []
        if tag == "table" and attrs.get("id") in {"gvIncidents", "tblDetails"}:
            self._table_id = attrs["id"]
            self.tables.setdefault(self._table_id, [])
        if self._table_id and tag == "tr":
            self._row = []
        if self._table_id and tag in {"td", "th"}:
            self._cell = []

    def handle_data(self, data):
        if self._span_id:
            self._span_parts.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag == "span" and self._span_id:
            self.spans[self._span_id] = clean_text("".join(self._span_parts))
            self._span_id = None
            self._span_parts = []
        if self._table_id and tag in {"td", "th"} and self._cell is not None:
            self._row.append(clean_text("".join(self._cell)))
            self._cell = None
        if self._table_id and tag == "tr" and self._row:
            self.tables[self._table_id].append(self._row)
            self._row = None
        if self._table_id and tag == "table":
            self._table_id = None


def clean_text(value):
    value = html.unescape(value or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def parse_metric_timestamp(value):
    if not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_page(text):
    parser = ChpTrafficParser()
    parser.feed(text)
    return parser


def record_response_status(stats, method, route, status):
    SCRAPER_METRICS.record_chp_http(method, route, status)
    if stats is None:
        return
    key = f"{method}:{route}:{status}"
    stats.setdefault("http_status_counts", {})
    stats["http_status_counts"][key] = stats["http_status_counts"].get(key, 0) + 1


def source_for_route(route):
    return "xml" if route == "media_xml" else "cad"


def record_response_bytes(stats, route, byte_count):
    if stats is None:
        return
    source = source_for_route(route)
    stats.setdefault("source_bytes", {})
    stats["source_bytes"][source] = stats["source_bytes"].get(source, 0) + byte_count


def request_text(opener, req, timeout, retries, backoff, stats=None, route="traffic"):
    for attempt in range(retries + 1):
        try:
            with opener.open(req, timeout=timeout) as response:
                record_response_status(stats, req.get_method(), route, response.status)
                body = response.read()
                record_response_bytes(stats, route, len(body))
                return body.decode("utf-8", errors="replace")
        except HTTPError as exc:
            record_response_status(stats, req.get_method(), route, exc.code)
            retryable = exc.code in {429, 500, 502, 503, 504}
            if not retryable or attempt >= retries:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else backoff * (2 ** attempt)
            time.sleep(delay)
        except URLError:
            record_response_status(stats, req.get_method(), route, "url_error")
            if attempt >= retries:
                raise
            time.sleep(backoff * (2 ** attempt))


def post_form(opener, url, data, timeout, user_agent, retries, backoff, stats=None, route="traffic"):
    req = Request(
        url,
        urlencode(data).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": user_agent,
            "Referer": url,
        },
    )
    return request_text(opener, req, timeout, retries, backoff, stats, route)


def get_page(opener, url, timeout, user_agent, retries, backoff, stats=None, route="traffic"):
    req = Request(url, headers={"User-Agent": user_agent})
    return request_text(opener, req, timeout, retries, backoff, stats, route)


def build_user_agent(contact_email=None):
    if contact_email:
        return f"chp-live-map/0.1 (+https://crestmap.us/; contact: {contact_email})"
    return DEFAULT_USER_AGENT


def robots_allows(user_agent, timeout):
    parser = urllib.robotparser.RobotFileParser(CHP_ROBOTS_URL)
    try:
        parser.read()
    except (OSError, HTTPError, URLError):
        return True
    return parser.can_fetch(user_agent, CHP_TRAFFIC_URL)


class ScraperMetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in {"/healthz", "/readyz"}:
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/metrics":
            body = SCRAPER_METRICS.render()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found\n")

    def log_message(self, _format, *_args):
        return


def start_metrics_server(host, port):
    server = ThreadingHTTPServer((host, port), ScraperMetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log_event(
        "info",
        "Scraper metrics server started",
        **{
            "event.action": "metrics_start",
            "event.outcome": "success",
            "server.address": host,
            "server.port": port,
        },
    )
    return server


def select_center(center, timeout, user_agent, retries, backoff, stats=None):
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    initial = parse_page(get_page(opener, CHP_TRAFFIC_URL, timeout, user_agent, retries, backoff, stats, "list"))
    data = {
        **initial.hidden,
        "ddlComCenter": center,
        "ddlSearches": "Choose One",
        "ddlResources": "Choose One",
        "btnCCGo": "OK",
    }
    page_text = post_form(opener, CHP_TRAFFIC_URL, data, timeout, user_agent, retries, backoff, stats, "list")
    return opener, parse_page(page_text)


def parse_incidents(center, parser):
    rows = parser.tables.get("gvIncidents", [])
    if not rows:
        return []
    headers = [normalize_header(h) for h in rows[0]]
    incidents = []
    for select_index, row in enumerate(rows[1:]):
        record = dict(zip(headers, row))
        incidents.append(
            {
                "center": center,
                "select_index": select_index,
                "incident_no": record.get("no", ""),
                "incident_time": record.get("time", ""),
                "type": record.get("type", ""),
                "location": record.get("location", ""),
                "location_desc": record.get("location_desc", ""),
                "area": record.get("area", ""),
            }
        )
    return incidents


def clean_xml_text(value):
    value = clean_text(value)
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].strip()
    return value


def parse_media_xml_timestamp(value):
    raw = re.sub(r"\s+", " ", clean_xml_text(value)).strip()
    for fmt in ("%b %d %Y %I:%M%p", "%b %d %Y %I:%M %p"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


class StaleMediaXmlError(RuntimeError):
    pass


def latest_media_xml_timestamp(xml_text, centers):
    wanted = set(centers)
    root = ET.fromstring(xml_text)
    latest = None
    for dispatch in root.findall(".//Dispatch"):
        center = dispatch.attrib.get("ID", "")
        if center not in wanted:
            continue
        for log in dispatch.findall("Log"):
            values = [log.findtext("LogTime")]
            values.extend(detail.findtext("DetailTime") for detail in log.findall("./LogDetails/details"))
            values.extend(unit.findtext("UnitTime") for unit in log.findall("./LogDetails/units"))
            for value in values:
                parsed = parse_media_xml_timestamp(value)
                if parsed and (latest is None or parsed > latest):
                    latest = parsed
    return latest


def validate_media_xml_freshness(xml_text, args):
    max_age_minutes = getattr(args, "xml_max_age_minutes", None)
    if max_age_minutes is None or max_age_minutes <= 0:
        return None
    latest = latest_media_xml_timestamp(xml_text, args.center)
    if latest is None:
        raise StaleMediaXmlError("CHP media XML has no parseable incident timestamps")
    now = dt.datetime.now(PACIFIC_TZ).replace(tzinfo=None)
    age_minutes = (now - latest).total_seconds() / 60
    if age_minutes > max_age_minutes:
        raise StaleMediaXmlError(
            f"CHP media XML latest timestamp is {age_minutes:.1f} minutes old, above {max_age_minutes:g} minute limit"
        )
    return latest


def format_incident_time(value):
    if not value:
        return ""
    hour = value.strftime("%I").lstrip("0") or "0"
    return f"{hour}:{value.strftime('%M %p')}"


def parse_media_lat_lon(value):
    raw = clean_xml_text(value)
    match = re.search(r"(-?\d+)\s*:\s*(-?\d+)", raw)
    if not match:
        return parse_lat_lon(raw)
    lat = int(match.group(1)) / 1_000_000
    lon = int(match.group(2)) / 1_000_000
    if lon > 0:
        lon = -lon
    return lat, lon


def media_incident_no(center, incident_date, log_id):
    compact_date = incident_date[2:4] + incident_date[5:7] + incident_date[8:10]
    prefix = center[:-2] if center.endswith("CC") else center
    stem = str(log_id or "")
    for candidate in (compact_date + prefix, compact_date):
        if stem.startswith(candidate):
            incident_no = stem[len(candidate):]
            if incident_no.startswith("FSP") and incident_no[3:].isdigit():
                return incident_no[3:]
            return incident_no
    return stem


def parse_media_xml_incidents(xml_text, centers):
    wanted = set(centers)
    root = ET.fromstring(xml_text)
    incidents = []
    for dispatch in root.findall(".//Dispatch"):
        center = dispatch.attrib.get("ID", "")
        if center not in wanted:
            continue
        for log in dispatch.findall("Log"):
            log_time = parse_media_xml_timestamp(log.findtext("LogTime"))
            if log_time:
                incident_date = log_time.date().isoformat()
                incident_time = format_incident_time(log_time)
            else:
                incident_date = dt.datetime.now().date().isoformat()
                incident_time = clean_xml_text(log.findtext("LogTime"))
            detail_entries = []
            for detail in log.findall("./LogDetails/details"):
                detail_time = parse_media_xml_timestamp(detail.findtext("DetailTime"))
                text = clean_xml_text(detail.findtext("IncidentDetail"))
                entry_no_match = re.match(r"\[(\d+)\]\s*(.*)", text)
                detail_entries.append(
                    {
                        "section": "Detail Information",
                        "time": format_incident_time(detail_time) if detail_time else clean_xml_text(detail.findtext("DetailTime")),
                        "entry_no": entry_no_match.group(1) if entry_no_match else "",
                        "text": text,
                    }
                )
            for index, unit in enumerate(log.findall("./LogDetails/units"), start=1):
                unit_time = parse_media_xml_timestamp(unit.findtext("UnitTime"))
                detail_entries.append(
                    {
                        "section": "Unit Information",
                        "time": format_incident_time(unit_time) if unit_time else clean_xml_text(unit.findtext("UnitTime")),
                        "entry_no": str(index),
                        "text": clean_xml_text(unit.findtext("UnitDetail")),
                    }
                )
            latitude, longitude = parse_media_lat_lon(log.findtext("LATLON"))
            log_id = log.attrib.get("ID", "")
            incident_no = media_incident_no(center, incident_date, log_id)
            incidents.append(
                {
                    "center": center,
                    "incident_no": incident_no,
                    "incident_time": incident_time,
                    "type": clean_xml_text(log.findtext("LogType")),
                    "location": clean_xml_text(log.findtext("Location")),
                    "location_desc": clean_xml_text(log.findtext("LocationDesc")),
                    "area": clean_xml_text(log.findtext("Area")),
                    "latitude": latitude,
                    "longitude": longitude,
                    "incident_date": incident_date,
                    "event_key": event_key(center, incident_date, incident_no),
                    "detail_entries": detail_entries,
                    "xml_log_id": log_id,
                }
            )
    return incidents


def fetch_media_xml_incidents(args, stats):
    opener = build_opener()
    xml_text = get_page(
        opener,
        args.media_xml_url,
        args.timeout,
        args.user_agent,
        args.retries,
        args.retry_backoff,
        stats,
        "media_xml",
    )
    validate_media_xml_freshness(xml_text, args)
    return parse_media_xml_incidents(xml_text, args.center)


def filtered_xml_incident_keys(incidents, args):
    matched = set()
    mapped = set()
    region_counts = {region: {"matched": 0, "mapped": 0} for region in REGION_ROAD_KEYWORDS}
    for incident in incidents:
        matches = matching_keywords(incident, args.road)
        if not args.all_roads and not matches:
            continue
        region_matches = matching_regions(incident)
        region = region_for_incident(region_matches)
        if not region:
            continue
        if (
            incident.get("latitude") is not None
            and incident.get("longitude") is not None
            and not coordinates_in_region_bounds(incident.get("latitude"), incident.get("longitude"), region)
        ):
            continue
        matched.add(incident["event_key"])
        region_counts.setdefault(region, {"matched": 0, "mapped": 0})
        region_counts[region]["matched"] += 1
        if incident.get("latitude") is not None and incident.get("longitude") is not None:
            mapped.add(incident["event_key"])
            region_counts[region]["mapped"] += 1
    return matched, mapped, region_counts


def log_xml_shadow_comparison(
    args,
    observed_at,
    cad_total_seen,
    cad_keys,
    cad_mapped_keys,
    cad_region_counts,
    stats,
):
    started_at = time.monotonic()
    try:
        xml_incidents = fetch_media_xml_incidents(args, stats)
        xml_keys, xml_mapped_keys, xml_region_counts = filtered_xml_incident_keys(xml_incidents, args)
    except Exception as exc:
        duration_seconds = time.monotonic() - started_at
        SCRAPER_METRICS.record_source_compare_failure(observed_at, duration_seconds, exc)
        log_exception(
            "CHP XML shadow comparison failed",
            exc,
            **{
                "event.action": "xml_shadow_compare",
                "event.outcome": "failure",
                "event.duration": int(duration_seconds * 1_000_000_000),
                "chp.duration_seconds": round(duration_seconds, 3),
                "chp.source": "media_xml",
                "http.request.header.user_agent": args.user_agent,
            },
        )
        return duration_seconds

    cad_only = sorted(cad_keys - xml_keys)
    xml_only = sorted(xml_keys - cad_keys)
    duration_seconds = time.monotonic() - started_at
    SCRAPER_METRICS.record_source_compare_success(
        observed_at=observed_at,
        duration_seconds=duration_seconds,
        cad_total_seen=cad_total_seen,
        cad_matched=len(cad_keys),
        cad_mapped=len(cad_mapped_keys),
        cad_region_counts=cad_region_counts,
        xml_total_seen=len(xml_incidents),
        xml_matched=len(xml_keys),
        xml_mapped=len(xml_mapped_keys),
        xml_region_counts=xml_region_counts,
        overlap_matched=len(cad_keys & xml_keys),
        cad_only=len(cad_only),
        xml_only=len(xml_only),
    )
    log_event(
        "info",
        "CHP XML shadow comparison completed",
        **{
            "event.action": "xml_shadow_compare",
            "event.outcome": "success",
            "event.duration": int(duration_seconds * 1_000_000_000),
            "chp.source": "media_xml",
            "chp.observed_at": observed_at,
            "chp.centers": args.center,
            "chp.duration_seconds": round(duration_seconds, 3),
            "chp.cad_total_seen": cad_total_seen,
            "chp.cad_matched": len(cad_keys),
            "chp.cad_mapped": len(cad_mapped_keys),
            "chp.xml_total_seen": len(xml_incidents),
            "chp.xml_matched": len(xml_keys),
            "chp.xml_mapped": len(xml_mapped_keys),
            "chp.overlap_matched": len(cad_keys & xml_keys),
            "chp.cad_only": len(cad_only),
            "chp.xml_only": len(xml_only),
            "chp.cad_only_sample": cad_only[:5],
            "chp.xml_only_sample": xml_only[:5],
            "chp.cad_region_counts": cad_region_counts,
            "chp.xml_region_counts": xml_region_counts,
        },
    )
    return duration_seconds


def normalize_header(value):
    value = clean_text(value).lower().replace(".", "")
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def matching_keywords(incident, keywords):
    haystack = " ".join(
        [
            incident.get("type", ""),
            incident.get("location", ""),
            incident.get("location_desc", ""),
            incident.get("area", ""),
        ]
    ).casefold()
    matches = [keyword for keyword in keywords if keyword_matches(keyword, haystack)]
    if "tuna canyon" in matches and is_la_tuna_canyon_match(incident):
        matches = [match for match in matches if match != "tuna canyon"]
    has_highway_39 = any(keyword_matches(alias, haystack) for alias in HIGHWAY_39_ALIASES)
    has_highway_39_context = any(context in haystack for context in HIGHWAY_39_FOREST_CONTEXT)
    if has_highway_39 and has_highway_39_context:
        matches.append("highway 39")
    return matches


def keyword_matches(keyword, haystack):
    normalized = keyword.casefold()
    pattern = ROUTE_ALIAS_PATTERNS.get(normalized)
    if pattern:
        return bool(pattern.search(haystack))
    return normalized in haystack


def matching_regions(incident):
    matches = {}
    for region, keywords in REGION_ROAD_KEYWORDS.items():
        region_matches = matching_keywords(incident, keywords)
        if region == "malibu":
            if is_malibu_false_positive(incident):
                region_matches = []
            elif is_la_tuna_canyon_match(incident):
                region_matches = [match for match in region_matches if match != "tuna canyon"]
        if region_matches:
            matches[region] = region_matches
    return matches


def incident_match_text(incident):
    return " ".join(
        [
            incident.get("location", ""),
            incident.get("location_desc", ""),
            incident.get("area", ""),
        ]
    ).casefold()


def is_la_tuna_canyon_match(incident):
    haystack = incident_match_text(incident)
    return "la tuna canyon" in haystack


def is_malibu_false_positive(incident):
    haystack = incident_match_text(incident)
    if "south la" in haystack and (" pch" in f" {haystack}" or "pacific coast hwy" in haystack):
        return True
    if any(freeway in haystack for freeway in ("i110", "i 110", "i710", "i 710")) and (
        " pch" in f" {haystack}" or "pacific coast hwy" in haystack
    ):
        return True
    if "topanga canyon" in haystack and any(route in haystack for route in ("sr118", "sr 118", "ca118", "ca 118", " 118 ")):
        return True
    if "topanga canyon" in haystack and any(
        valley_text in haystack
        for valley_text in (
            "canoga park",
            "west valley",
            "topanga canyon blvd",
        )
    ):
        return True
    return False


def region_for_incident(region_matches, merged=None):
    if "forest" in region_matches:
        return "forest"
    if "malibu" in region_matches:
        return "malibu"
    return None
    return None


def fetch_details(opener, center, list_parser, select_index, timeout, user_agent, retries, backoff, stats=None):
    data = {
        **list_parser.hidden,
        "__EVENTTARGET": "gvIncidents",
        "__EVENTARGUMENT": f"Select${select_index}",
        "ddlComCenter": center,
        "ddlSearches": "Choose One",
        "ddlResources": "Choose One",
    }
    detail_text = post_form(opener, CHP_TRAFFIC_URL, data, timeout, user_agent, retries, backoff, stats, "detail")
    parser = parse_page(detail_text)
    spans = parser.spans
    lat, lon = parse_lat_lon(spans.get("lblLatLon", ""))
    if lat is None or lon is None:
        lat, lon = parse_lat_lon_from_detail_html(detail_text)
    detail_entries = []
    section = "Detail Information"
    for row in parser.tables.get("tblDetails", []):
        if len(row) == 1 and row[0]:
            section = row[0]
            continue
        if row and normalize_header(row[0]) == "time":
            section = "Detail Information"
            continue
        if len(row) >= 3:
            detail_entries.append(
                {
                    "section": section,
                    "time": row[0],
                    "entry_no": row[1],
                    "text": " ".join(row[2:]).strip(),
                }
            )
    return {
        "incident_no": spans.get("lblIncident", ""),
        "type": spans.get("lblType", ""),
        "location": spans.get("lblLocation", ""),
        "location_desc": spans.get("lblLocationDesc", ""),
        "latitude": lat,
        "longitude": lon,
        "detail_entries": detail_entries,
    }


def parse_lat_lon(value):
    match = re.search(r"(-?\d+\.\d+)[,\s]+(-?\d+\.\d+)", value or "")
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def parse_lat_lon_from_detail_html(detail_text):
    span_match = re.search(
        r'<span[^>]+id="lblLatLon"[^>]*>(.*?)</span>',
        detail_text or "",
        re.I | re.S,
    )
    if span_match:
        lat, lon = parse_lat_lon(html.unescape(span_match.group(1)))
        if lat is not None and lon is not None:
            return lat, lon

    maps_match = re.search(
        r"(?:maps/place/|[?&]q=)(-?\d+\.\d+)[,\s%20]+(-?\d+\.\d+)",
        detail_text or "",
        re.I,
    )
    if maps_match:
        return float(maps_match.group(1)), float(maps_match.group(2))
    return None, None


def parse_updated_at(updated_text, now):
    match = re.search(r"Updated as of\s+(.+)$", updated_text or "", re.I)
    if not match:
        return now.replace(tzinfo=None), updated_text or ""
    raw = match.group(1)
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p"):
        try:
            return dt.datetime.strptime(raw, fmt), raw
        except ValueError:
            pass
    return now.replace(tzinfo=None), raw


def incident_date_for_time(updated_at, incident_time):
    try:
        incident_t = dt.datetime.strptime(incident_time, "%I:%M %p").time()
    except ValueError:
        return updated_at.date().isoformat()
    incident_dt = dt.datetime.combine(updated_at.date(), incident_t)
    if incident_dt > updated_at + dt.timedelta(minutes=5):
        incident_dt -= dt.timedelta(days=1)
    return incident_dt.date().isoformat()


def event_key(center, incident_date, incident_no):
    return f"{center}|{incident_date}|{incident_no}"


def details_hash(row):
    payload = json.dumps(
        {
            "type": row["type"],
            "location": row["location"],
            "location_desc": row["location_desc"],
            "area": row["area"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "detail_entries": row["detail_entries"],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def connect_database(path=None, database_url=None):
    if database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Postgres support requires psycopg. Install requirements.txt.") from exc
        conn = psycopg.connect(database_url, row_factory=dict_row)
        init_database_postgres(conn)
        return conn

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_database_sqlite(conn)
    return conn


def init_database_sqlite(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_key TEXT PRIMARY KEY,
            center TEXT NOT NULL,
            region TEXT NOT NULL DEFAULT 'forest',
            incident_date TEXT NOT NULL,
            incident_no TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            cleared_at TEXT,
            status TEXT NOT NULL,
            latest_observed_at TEXT NOT NULL,
            updated_as_of TEXT,
            incident_time TEXT,
            type TEXT,
            location TEXT,
            location_desc TEXT,
            area TEXT,
            latitude REAL,
            longitude REAL,
            matched_keywords TEXT,
            details_hash TEXT,
            details_fetched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            region TEXT NOT NULL DEFAULT 'forest',
            observed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_as_of TEXT,
            incident_time TEXT,
            type TEXT,
            location TEXT,
            location_desc TEXT,
            area TEXT,
            latitude REAL,
            longitude REAL,
            matched_keywords TEXT,
            details_hash TEXT,
            details_json TEXT NOT NULL,
            FOREIGN KEY (event_key) REFERENCES events(event_key)
        );

        CREATE TABLE IF NOT EXISTS detail_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            entry_index INTEGER NOT NULL,
            section TEXT,
            entry_time TEXT,
            entry_no TEXT,
            text TEXT,
            FOREIGN KEY (event_key) REFERENCES events(event_key)
        );

        CREATE TABLE IF NOT EXISTS scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'unknown',
            centers TEXT NOT NULL,
            total_seen INTEGER NOT NULL DEFAULT 0,
            active_seen INTEGER NOT NULL,
            observations_inserted INTEGER NOT NULL,
            active_with_coords INTEGER NOT NULL DEFAULT 0,
            details_requested INTEGER NOT NULL DEFAULT 0,
            details_skipped INTEGER NOT NULL DEFAULT 0,
            duration_seconds REAL NOT NULL DEFAULT 0,
            http_status_counts TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
        CREATE INDEX IF NOT EXISTS idx_events_center_status ON events(center, status);
        CREATE INDEX IF NOT EXISTS idx_observations_event ON observations(event_key, observed_at);
        """
    )
    ensure_column_sqlite(conn, "scrape_runs", "total_seen", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_sqlite(conn, "scrape_runs", "source", "TEXT NOT NULL DEFAULT 'unknown'")
    ensure_column_sqlite(conn, "scrape_runs", "active_with_coords", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_sqlite(conn, "scrape_runs", "details_requested", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_sqlite(conn, "scrape_runs", "details_skipped", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_sqlite(conn, "scrape_runs", "duration_seconds", "REAL NOT NULL DEFAULT 0")
    ensure_column_sqlite(conn, "scrape_runs", "http_status_counts", "TEXT NOT NULL DEFAULT '{}'")
    ensure_column_sqlite(conn, "events", "details_fetched_at", "TEXT")
    ensure_column_sqlite(conn, "events", "region", "TEXT NOT NULL DEFAULT 'forest'")
    ensure_column_sqlite(conn, "observations", "region", "TEXT NOT NULL DEFAULT 'forest'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_region_status ON events(region, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_region_first_seen ON events(region, first_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_region_last_seen ON events(region, last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_region_cleared_at ON events(region, cleared_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detail_entries_event_observed ON detail_entries(event_key, observed_at)")
    ensure_column_sqlite(conn, "detail_entries", "section", "TEXT")
    backfill_detail_entry_sections(conn)


def init_database_postgres(conn):
    statements = [
        """
        CREATE TABLE IF NOT EXISTS events (
            event_key TEXT PRIMARY KEY,
            center TEXT NOT NULL,
            region TEXT NOT NULL DEFAULT 'forest',
            incident_date TEXT NOT NULL,
            incident_no TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            cleared_at TEXT,
            status TEXT NOT NULL,
            latest_observed_at TEXT NOT NULL,
            updated_as_of TEXT,
            incident_time TEXT,
            type TEXT,
            location TEXT,
            location_desc TEXT,
            area TEXT,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            matched_keywords TEXT,
            details_hash TEXT,
            details_fetched_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS observations (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            event_key TEXT NOT NULL REFERENCES events(event_key),
            region TEXT NOT NULL DEFAULT 'forest',
            observed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_as_of TEXT,
            incident_time TEXT,
            type TEXT,
            location TEXT,
            location_desc TEXT,
            area TEXT,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            matched_keywords TEXT,
            details_hash TEXT,
            details_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS detail_entries (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            event_key TEXT NOT NULL REFERENCES events(event_key),
            observed_at TEXT NOT NULL,
            entry_index INTEGER NOT NULL,
            section TEXT,
            entry_time TEXT,
            entry_no TEXT,
            text TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'unknown',
            centers TEXT NOT NULL,
            total_seen INTEGER NOT NULL DEFAULT 0,
            active_seen INTEGER NOT NULL,
            observations_inserted INTEGER NOT NULL,
            active_with_coords INTEGER NOT NULL DEFAULT 0,
            details_requested INTEGER NOT NULL DEFAULT 0,
            details_skipped INTEGER NOT NULL DEFAULT 0,
            duration_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
            http_status_counts TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)",
        "CREATE INDEX IF NOT EXISTS idx_events_center_status ON events(center, status)",
        "CREATE INDEX IF NOT EXISTS idx_observations_event ON observations(event_key, observed_at)",
    ]
    for statement in statements:
        conn.execute(statement)
    ensure_column_postgres(conn, "scrape_runs", "total_seen", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_postgres(conn, "scrape_runs", "source", "TEXT NOT NULL DEFAULT 'unknown'")
    ensure_column_postgres(conn, "scrape_runs", "active_with_coords", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_postgres(conn, "scrape_runs", "details_requested", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_postgres(conn, "scrape_runs", "details_skipped", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_postgres(conn, "scrape_runs", "duration_seconds", "DOUBLE PRECISION NOT NULL DEFAULT 0")
    ensure_column_postgres(conn, "scrape_runs", "http_status_counts", "TEXT NOT NULL DEFAULT '{}'")
    ensure_column_postgres(conn, "events", "details_fetched_at", "TEXT")
    ensure_column_postgres(conn, "events", "region", "TEXT NOT NULL DEFAULT 'forest'")
    ensure_column_postgres(conn, "observations", "region", "TEXT NOT NULL DEFAULT 'forest'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_region_status ON events(region, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_region_first_seen ON events(region, first_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_region_last_seen ON events(region, last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_region_cleared_at ON events(region, cleared_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detail_entries_event_observed ON detail_entries(event_key, observed_at)")
    ensure_column_postgres(conn, "detail_entries", "section", "TEXT")
    backfill_detail_entry_sections(conn)
    conn.commit()


def ensure_column_sqlite(conn, table, column, definition):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_column_postgres(conn, table, column, definition):
    conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")


def detail_entry_section(entry):
    return entry.get("section") or (
        "Unit Information" if str(entry.get("text") or "").startswith("Unit ") else "Detail Information"
    )


def backfill_detail_entry_sections(conn):
    if is_postgres(conn):
        missing = conn.execute(
            "SELECT 1 FROM detail_entries WHERE section IS NULL OR section = '' LIMIT 1"
        ).fetchone()
        if not missing:
            return
        observations = conn.execute(
            """
            SELECT DISTINCT o.event_key, o.observed_at, o.details_json
            FROM observations o
            JOIN detail_entries d
              ON d.event_key = o.event_key
             AND d.observed_at = o.observed_at
            WHERE o.details_json IS NOT NULL
              AND o.details_json <> '[]'
              AND (d.section IS NULL OR d.section = '')
            """
        ).fetchall()
        update_sql = """
            UPDATE detail_entries
            SET section = %s
            WHERE event_key = %s
              AND observed_at = %s
              AND entry_index = %s
              AND (section IS NULL OR section = '')
        """
    else:
        missing = conn.execute(
            "SELECT 1 FROM detail_entries WHERE section IS NULL OR section = '' LIMIT 1"
        ).fetchone()
        if not missing:
            return
        observations = conn.execute(
            """
            SELECT DISTINCT o.event_key, o.observed_at, o.details_json
            FROM observations o
            JOIN detail_entries d
              ON d.event_key = o.event_key
             AND d.observed_at = o.observed_at
            WHERE o.details_json IS NOT NULL
              AND o.details_json <> '[]'
              AND (d.section IS NULL OR d.section = '')
            """
        ).fetchall()
        update_sql = """
            UPDATE detail_entries
            SET section = ?
            WHERE event_key = ?
              AND observed_at = ?
              AND entry_index = ?
              AND (section IS NULL OR section = '')
        """

    for observation in observations:
        try:
            entries = json.loads(observation["details_json"] or "[]")
        except json.JSONDecodeError:
            continue
        for entry_index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            conn.execute(
                update_sql,
                (
                    detail_entry_section(entry),
                    observation["event_key"],
                    observation["observed_at"],
                    entry_index,
                ),
            )


def is_postgres(conn):
    return conn.__class__.__module__.startswith("psycopg")


def row_for_event(conn, event_key_value):
    if is_postgres(conn):
        return conn.execute(
            "SELECT * FROM events WHERE event_key = %s", (event_key_value,)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM events WHERE event_key = ?", (event_key_value,)
    ).fetchone()


def list_fields_match(event, incident):
    return all(
        (event[field] or "") == (incident.get(field) or "")
        for field in ("incident_time", "type", "location", "location_desc", "area")
    )


def observed_at_age_minutes(event, now):
    latest = event["details_fetched_at"] if event and "details_fetched_at" in event.keys() else None
    if not latest:
        return None
    try:
        latest_at = dt.datetime.fromisoformat(latest)
    except ValueError:
        return None
    if latest_at.tzinfo is None:
        latest_at = latest_at.replace(tzinfo=now.tzinfo)
    return (now - latest_at).total_seconds() / 60


def should_fetch_details(previous, incident, now, refresh_minutes):
    if not previous or previous["status"] != "active":
        return True
    if not previous["details_hash"] or not list_fields_match(previous, incident):
        return True
    age_minutes = observed_at_age_minutes(previous, now)
    return age_minutes is None or age_minutes >= refresh_minutes


def touch_active_event(conn, event, observed_at):
    if is_postgres(conn):
        conn.execute(
            """
            UPDATE events
            SET last_seen = %s,
                cleared_at = NULL,
                status = 'active',
                latest_observed_at = %s
            WHERE event_key = %s
            """,
            (observed_at, observed_at, event["event_key"]),
        )
    else:
        conn.execute(
            """
            UPDATE events
            SET last_seen = ?,
                cleared_at = NULL,
                status = 'active',
                latest_observed_at = ?
            WHERE event_key = ?
            """,
            (observed_at, observed_at, event["event_key"]),
        )


def upsert_active_event(conn, row):
    previous = row_for_event(conn, row["event_key"])
    first_seen = previous["first_seen"] if previous else row["observed_at"]
    params = {**row, "region": row.get("region", "forest"), "first_seen": first_seen, "last_seen": row["observed_at"]}
    if is_postgres(conn):
        conn.execute(
            """
            INSERT INTO events (
                event_key, center, region, incident_date, incident_no, first_seen, last_seen,
                cleared_at, status, latest_observed_at, updated_as_of, incident_time,
                type, location, location_desc, area, latitude, longitude,
                matched_keywords, details_hash, details_fetched_at
            ) VALUES (
                %(event_key)s, %(center)s, %(region)s, %(incident_date)s, %(incident_no)s,
                %(first_seen)s, %(last_seen)s, NULL, 'active', %(observed_at)s,
                %(updated_as_of)s, %(incident_time)s, %(type)s, %(location)s,
                %(location_desc)s, %(area)s, %(latitude)s, %(longitude)s,
                %(matched_keywords)s, %(details_hash)s, %(observed_at)s
            )
            ON CONFLICT(event_key) DO UPDATE SET
                last_seen = excluded.last_seen,
                cleared_at = NULL,
                status = 'active',
                latest_observed_at = excluded.latest_observed_at,
                region = excluded.region,
                updated_as_of = excluded.updated_as_of,
                incident_time = excluded.incident_time,
                type = excluded.type,
                location = excluded.location,
                location_desc = excluded.location_desc,
                area = excluded.area,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                matched_keywords = excluded.matched_keywords,
                details_hash = excluded.details_hash,
                details_fetched_at = excluded.details_fetched_at
            """,
            params,
        )
        return previous

    conn.execute(
        """
        INSERT INTO events (
            event_key, center, region, incident_date, incident_no, first_seen, last_seen,
            cleared_at, status, latest_observed_at, updated_as_of, incident_time,
            type, location, location_desc, area, latitude, longitude,
            matched_keywords, details_hash, details_fetched_at
        ) VALUES (
            :event_key, :center, :region, :incident_date, :incident_no, :first_seen,
            :last_seen, NULL, 'active', :observed_at, :updated_as_of,
            :incident_time, :type, :location, :location_desc, :area, :latitude,
            :longitude, :matched_keywords, :details_hash, :observed_at
        )
        ON CONFLICT(event_key) DO UPDATE SET
            last_seen = excluded.last_seen,
            cleared_at = NULL,
            status = 'active',
            latest_observed_at = excluded.latest_observed_at,
            region = excluded.region,
            updated_as_of = excluded.updated_as_of,
            incident_time = excluded.incident_time,
            type = excluded.type,
            location = excluded.location,
            location_desc = excluded.location_desc,
            area = excluded.area,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            matched_keywords = excluded.matched_keywords,
            details_hash = excluded.details_hash,
            details_fetched_at = excluded.details_fetched_at
        """,
        params,
    )
    return previous


def insert_observation(conn, row, status):
    details_json = json.dumps(row["detail_entries"], ensure_ascii=False)
    params = {**row, "region": row.get("region", "forest"), "status": status, "details_json": details_json}
    if is_postgres(conn):
        conn.execute(
            """
            INSERT INTO observations (
                event_key, region, observed_at, status, updated_as_of, incident_time, type,
                location, location_desc, area, latitude, longitude, matched_keywords,
                details_hash, details_json
            ) VALUES (
                %(event_key)s, %(region)s, %(observed_at)s, %(status)s, %(updated_as_of)s,
                %(incident_time)s, %(type)s, %(location)s, %(location_desc)s, %(area)s,
                %(latitude)s, %(longitude)s, %(matched_keywords)s, %(details_hash)s,
                %(details_json)s
            )
            """,
            params,
        )
    else:
        conn.execute(
            """
        INSERT INTO observations (
            event_key, region, observed_at, status, updated_as_of, incident_time, type,
            location, location_desc, area, latitude, longitude, matched_keywords,
            details_hash, details_json
        ) VALUES (
            :event_key, :region, :observed_at, :status, :updated_as_of, :incident_time,
            :type, :location, :location_desc, :area, :latitude, :longitude,
            :matched_keywords, :details_hash, :details_json
        )
        """,
            params,
        )
    for entry_index, entry in enumerate(row["detail_entries"], start=1):
        query = """
        INSERT INTO detail_entries (
            event_key, observed_at, entry_index, section, entry_time, entry_no, text
        ) VALUES ({})
        """.format("%s, %s, %s, %s, %s, %s, %s" if is_postgres(conn) else "?, ?, ?, ?, ?, ?, ?")
        conn.execute(
            query,
            (
                row["event_key"], row["observed_at"], entry_index,
                detail_entry_section(entry),
                entry.get("time"), entry.get("entry_no"), entry.get("text"),
            ),
        )


def mark_cleared(conn, event, observed_at):
    if is_postgres(conn):
        conn.execute(
            """
            UPDATE events
            SET status = 'cleared',
                cleared_at = %s,
                last_seen = %s,
                latest_observed_at = %s
            WHERE event_key = %s
            """,
            (observed_at, observed_at, observed_at, event["event_key"]),
        )
    else:
        conn.execute(
            """
        UPDATE events
        SET status = 'cleared',
            cleared_at = ?,
            last_seen = ?,
            latest_observed_at = ?
        WHERE event_key = ?
        """,
            (observed_at, observed_at, observed_at, event["event_key"]),
        )
    row = dict(event)
    row["observed_at"] = observed_at
    row["detail_entries"] = []
    insert_observation(conn, row, "cleared")


def store_scrape_run(
    conn,
    observed_at,
    centers,
    total_seen,
    active_seen,
    observations_inserted,
    active_with_coords,
    details_requested,
    details_skipped,
    duration_seconds,
    http_status_counts,
    source="unknown",
):
    placeholder = (
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
        if is_postgres(conn)
        else "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
    )
    conn.execute(
        f"""
        INSERT INTO scrape_runs (
            observed_at, source, centers, total_seen, active_seen, observations_inserted,
            active_with_coords, details_requested, details_skipped, duration_seconds,
            http_status_counts
        ) VALUES ({placeholder})
        """,
        (
            observed_at,
            source,
            ",".join(centers),
            total_seen,
            active_seen,
            observations_inserted,
            active_with_coords,
            details_requested,
            details_skipped,
            duration_seconds,
            json.dumps(http_status_counts, sort_keys=True),
        ),
    )


def scrape_once_xml(args):
    started_at = time.monotonic()
    now = dt.datetime.now().astimezone()
    observed_at = now.isoformat(timespec="seconds")
    seen_keys = set()
    seen_with_coords = set()
    region_seen_keys = {region: set() for region in REGION_ROAD_KEYWORDS}
    region_seen_with_coords = {region: set() for region in REGION_ROAD_KEYWORDS}
    observations_inserted = 0
    stats = {"http_status_counts": {}, "source_bytes": {}}

    if args.respect_robots and not robots_allows(args.user_agent, args.timeout):
        raise RuntimeError(f"robots.txt disallows scraping {CHP_TRAFFIC_URL} for {args.user_agent}")

    xml_started_at = time.monotonic()
    xml_incidents = fetch_media_xml_incidents(args, stats)
    xml_duration_seconds = time.monotonic() - xml_started_at
    total_seen = len(xml_incidents)

    with connect_database(args.database, args.database_url) as conn:
        for incident in xml_incidents:
            matches = matching_keywords(incident, args.road)
            if not args.all_roads and not matches:
                continue
            region_matches = matching_regions(incident)
            region = region_for_incident(region_matches)
            if not region:
                continue
            if (
                incident.get("latitude") is not None
                and incident.get("longitude") is not None
                and not coordinates_in_region_bounds(incident.get("latitude"), incident.get("longitude"), region)
            ):
                continue
            merged = dict(incident)
            clear_coordinates_outside_region_bounds(merged, region)
            row = {
                **merged,
                "region": region,
                "observed_at": observed_at,
                "updated_as_of": observed_at,
                "matched_keywords": ";".join(region_matches.get(region) or matches or ["*"]),
                "detail_entries": merged.get("detail_entries", []),
            }
            row["details_hash"] = details_hash(row)
            seen_keys.add(row["event_key"])
            region_seen_keys.setdefault(region, set()).add(row["event_key"])
            if row["latitude"] is not None and row["longitude"] is not None:
                seen_with_coords.add(row["event_key"])
                region_seen_with_coords.setdefault(region, set()).add(row["event_key"])
            previous = upsert_active_event(conn, row)
            if (
                not previous
                or previous["status"] != "active"
                or previous["details_hash"] != row["details_hash"]
            ):
                insert_observation(conn, row, "active")
                observations_inserted += 1

        if is_postgres(conn):
            active_events = conn.execute(
                """
                SELECT * FROM events
                WHERE status = 'active'
                  AND center = ANY(%s)
                """,
                (args.center,),
            ).fetchall()
        else:
            active_events = conn.execute(
                """
                SELECT * FROM events
                WHERE status = 'active'
                  AND center IN ({})
                """.format(",".join("?" for _ in args.center)),
                tuple(args.center),
            ).fetchall()
        for event in active_events:
            if event["event_key"] not in seen_keys:
                mark_cleared(conn, event, observed_at)
                observations_inserted += 1

        store_scrape_run(
            conn,
            observed_at,
            args.center,
            total_seen,
            len(seen_keys),
            observations_inserted,
            len(seen_with_coords),
            0,
            0,
            time.monotonic() - started_at,
            stats["http_status_counts"],
            source="xml",
        )

    region_counts = {
        region: {
            "matched": len(region_seen_keys.get(region, set())),
            "mapped": len(region_seen_with_coords.get(region, set())),
        }
        for region in sorted(REGION_ROAD_KEYWORDS)
    }
    duration_seconds = time.monotonic() - started_at
    source_bytes = {
        "cad": 0,
        "xml": stats.get("source_bytes", {}).get("xml", 0),
    }
    source_bytes["total"] = source_bytes["xml"]
    return (
        observations_inserted,
        total_seen,
        len(seen_keys),
        len(seen_with_coords),
        region_counts,
        0,
        0,
        duration_seconds,
        {"cad": 0, "xml": xml_duration_seconds, "total": duration_seconds},
        source_bytes,
        stats["http_status_counts"],
        observed_at,
    )


def scrape_once_cad(args):
    started_at = time.monotonic()
    now = dt.datetime.now().astimezone()
    observed_at = now.isoformat(timespec="seconds")
    seen_keys = set()
    seen_with_coords = set()
    region_seen_keys = {region: set() for region in REGION_ROAD_KEYWORDS}
    region_seen_with_coords = {region: set() for region in REGION_ROAD_KEYWORDS}
    total_seen = 0
    details_requested = 0
    details_skipped = 0
    observations_inserted = 0
    stats = {"http_status_counts": {}, "source_bytes": {}}

    if args.respect_robots and not robots_allows(args.user_agent, args.timeout):
        raise RuntimeError(f"robots.txt disallows scraping {CHP_TRAFFIC_URL} for {args.user_agent}")

    with connect_database(args.database, args.database_url) as conn:
        for center in args.center:
            opener, list_parser = select_center(
                center, args.timeout, args.user_agent, args.retries, args.retry_backoff, stats
            )
            updated_at, updated_as_of = parse_updated_at(
                list_parser.spans.get("lblUpdated", ""), now
            )
            incidents = parse_incidents(center, list_parser)
            total_seen += len(incidents)
            for incident in incidents:
                matches = matching_keywords(incident, args.road)
                if not args.all_roads and not matches:
                    continue
                region_matches = matching_regions(incident)
                region = region_for_incident(region_matches)
                incident_date = incident_date_for_time(updated_at, incident["incident_time"])
                current_event_key = event_key(center, incident_date, incident["incident_no"])
                previous = row_for_event(conn, current_event_key)
                if region and not should_fetch_details(previous, incident, now, args.detail_refresh_minutes):
                    details_skipped += 1
                    seen_keys.add(current_event_key)
                    region_seen_keys.setdefault(region, set()).add(current_event_key)
                    if previous["latitude"] is not None and previous["longitude"] is not None:
                        seen_with_coords.add(current_event_key)
                        region_seen_with_coords.setdefault(region, set()).add(current_event_key)
                    touch_active_event(conn, previous, observed_at)
                    continue

                if details_requested:
                    time.sleep(args.detail_delay)
                details_requested += 1
                details = fetch_details(
                    opener,
                    center,
                    list_parser,
                    incident["select_index"],
                    args.timeout,
                    args.user_agent,
                    args.retries,
                    args.retry_backoff,
                    stats,
                )
                merged = {
                    **incident,
                    **{
                        k: v
                        for k, v in details.items()
                        if v not in ("", None) or k in {"latitude", "longitude", "detail_entries"}
                    },
                }
                region = region or region_for_incident(region_matches, merged)
                if not region:
                    continue
                if (
                    region == "malibu"
                    and merged.get("latitude") is not None
                    and merged.get("longitude") is not None
                    and not coordinates_in_region_bounds(merged.get("latitude"), merged.get("longitude"), region)
                ):
                    continue
                clear_coordinates_outside_region_bounds(merged, region)
                matched_keywords = region_matches.get(region) or ["tuna canyon"]
                row = {
                    **merged,
                    "region": region,
                    "observed_at": observed_at,
                    "updated_as_of": updated_as_of,
                    "incident_date": incident_date,
                    "event_key": current_event_key,
                    "matched_keywords": ";".join(matched_keywords) if matched_keywords else "*",
                    "detail_entries": merged.get("detail_entries", []),
                }
                row["details_hash"] = details_hash(row)
                seen_keys.add(row["event_key"])
                region_seen_keys.setdefault(region, set()).add(row["event_key"])
                if row["latitude"] is not None and row["longitude"] is not None:
                    seen_with_coords.add(row["event_key"])
                    region_seen_with_coords.setdefault(region, set()).add(row["event_key"])
                previous = upsert_active_event(conn, row)
                if (
                    not previous
                    or previous["status"] != "active"
                    or previous["details_hash"] != row["details_hash"]
                ):
                    insert_observation(conn, row, "active")
                    observations_inserted += 1

        if is_postgres(conn):
            active_events = conn.execute(
                """
                SELECT * FROM events
                WHERE status = 'active'
                  AND center = ANY(%s)
                """,
                (args.center,),
            ).fetchall()
        else:
            active_events = conn.execute(
                """
                SELECT * FROM events
                WHERE status = 'active'
                  AND center IN ({})
                """.format(",".join("?" for _ in args.center)),
                tuple(args.center),
            ).fetchall()
        for event in active_events:
            if event["event_key"] not in seen_keys:
                mark_cleared(conn, event, observed_at)
                observations_inserted += 1

        store_scrape_run(
            conn,
            observed_at,
            args.center,
            total_seen,
            len(seen_keys),
            observations_inserted,
            len(seen_with_coords),
            details_requested,
            details_skipped,
            time.monotonic() - started_at,
            stats["http_status_counts"],
            source="cad",
        )
    region_counts = {
        region: {
            "matched": len(region_seen_keys.get(region, set())),
            "mapped": len(region_seen_with_coords.get(region, set())),
        }
        for region in sorted(REGION_ROAD_KEYWORDS)
    }
    cad_duration_seconds = time.monotonic() - started_at
    xml_duration_seconds = 0
    if args.xml_shadow_compare:
        xml_duration_seconds = log_xml_shadow_comparison(
            args,
            observed_at,
            total_seen,
            seen_keys,
            seen_with_coords,
            region_counts,
            stats,
        ) or 0
    duration_seconds = time.monotonic() - started_at
    source_durations = {
        "cad": cad_duration_seconds,
        "xml": xml_duration_seconds,
        "total": duration_seconds,
    }
    source_bytes = {
        "cad": stats.get("source_bytes", {}).get("cad", 0),
        "xml": stats.get("source_bytes", {}).get("xml", 0),
    }
    source_bytes["total"] = source_bytes["cad"] + source_bytes["xml"]
    return (
        observations_inserted,
        total_seen,
        len(seen_keys),
        len(seen_with_coords),
        region_counts,
        details_requested,
        details_skipped,
        duration_seconds,
        source_durations,
        source_bytes,
        stats["http_status_counts"],
        observed_at,
    )


def scrape_once(args):
    if args.source_mode == "cad":
        return scrape_once_cad(args)

    try:
        return scrape_once_xml(args)
    except (ET.ParseError, StaleMediaXmlError) as exc:
        is_stale = isinstance(exc, StaleMediaXmlError)
        log_exception(
            "CHP XML scrape is stale; falling back to CAD" if is_stale else "CHP XML scrape returned malformed XML; falling back to CAD",
            exc,
            **{
                "event.action": "scrape_fallback",
                "event.outcome": "failure",
                "chp.source": "media_xml",
                "chp.fallback_source": "cad",
                "chp.xml_error_type": type(exc).__name__,
                "http.request.header.user_agent": args.user_agent,
            },
        )
        return scrape_once_cad(args)


def source_attempts_for_result(args, source_durations=None, source_bytes=None):
    source_mode = getattr(args, "source_mode", "xml")
    if source_mode == "cad":
        return [("cad", "primary", "success")]

    durations = source_durations or {}
    byte_counts = source_bytes or {}
    cad_used = durations.get("cad", 0) > 0 or byte_counts.get("cad", 0) > 0
    if cad_used:
        return [
            ("xml", "primary", "failure"),
            ("cad", "fallback", "success"),
        ]
    return [("xml", "primary", "success")]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape public CHP CAD active incidents into SQLite."
    )
    parser.add_argument("--center", action="append", default=[], help="CHP center code, e.g. LACC. Repeatable.")
    parser.add_argument("--road", action="append", default=[], help="Road keyword to match. Repeatable.")
    parser.add_argument("--all-roads", action="store_true", help="Capture every incident in the selected centers.")
    parser.add_argument("--database", type=Path, default=Path("chp_traffic.sqlite"))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--interval", type=int, default=0, help="Poll interval in seconds. Default runs once.")
    parser.add_argument("--metrics-host", default=os.environ.get("SCRAPER_METRICS_HOST"))
    parser.add_argument("--metrics-port", type=int, default=int(os.environ.get("SCRAPER_METRICS_PORT", "0")))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--detail-delay", type=float, default=0.2)
    parser.add_argument("--detail-refresh-minutes", type=float, default=3.0)
    parser.add_argument("--media-xml-url", default=os.environ.get("CHP_MEDIA_XML_URL", CHP_MEDIA_XML_URL))
    parser.add_argument(
        "--xml-max-age-minutes",
        type=float,
        default=float(os.environ.get("CHP_XML_MAX_AGE_MINUTES", "30")),
        help="Treat the media XML feed as stale and fall back to CAD when its newest timestamp is older than this many minutes. Set 0 to disable.",
    )
    parser.add_argument(
        "--source-mode",
        choices=("cad", "xml"),
        default=os.environ.get("CHP_SOURCE_MODE", "xml"),
        help="Incident source to write to the database. cad uses the CHP CAD WebForms flow; xml uses the CHP media XML feed.",
    )
    parser.add_argument(
        "--xml-shadow-compare",
        action="store_true",
        default=os.environ.get("CHP_XML_SHADOW_COMPARE", "").lower() in {"1", "true", "yes"},
        help="Fetch the CHP media XML feed and log comparison stats without writing XML rows.",
    )
    parser.add_argument("--contact-email", default=os.environ.get("CHP_CONTACT_EMAIL"))
    parser.add_argument("--user-agent", default=os.environ.get("CHP_USER_AGENT"))
    parser.add_argument("--retries", type=int, default=int(os.environ.get("CHP_RETRIES", "2")))
    parser.add_argument("--retry-backoff", type=float, default=float(os.environ.get("CHP_RETRY_BACKOFF", "2")))
    parser.add_argument("--no-respect-robots", action="store_false", dest="respect_robots")
    parser.set_defaults(respect_robots=True)
    args = parser.parse_args()
    if not args.user_agent:
        args.user_agent = build_user_agent(args.contact_email)
    args.center = args.center or DEFAULT_CENTERS
    args.road = args.road or DEFAULT_ROAD_KEYWORDS
    return args


def main():
    args = parse_args()
    metrics_server = None
    if args.metrics_port > 0:
        metrics_server = start_metrics_server(args.metrics_host or "127.0.0.1", args.metrics_port)
    while True:
        started_at = time.monotonic()
        observed_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            (
                changed_rows,
                total_seen,
                active_seen,
                active_with_coords,
                region_counts,
                details_requested,
                details_skipped,
                duration_seconds,
                source_durations,
                source_bytes,
                http_status_counts,
                scrape_observed_at,
            ) = scrape_once(args)
            for source, mode, outcome in source_attempts_for_result(args, source_durations, source_bytes):
                SCRAPER_METRICS.record_source_attempt(source, mode, outcome)
            SCRAPER_METRICS.record_success(
                scrape_observed_at,
                changed_rows,
                total_seen,
                active_seen,
                active_with_coords,
                region_counts,
                details_requested,
                details_skipped,
                duration_seconds,
                source_durations,
                source_bytes,
            )
            log_event(
                "info",
                "CHP scrape completed",
                **{
                    "event.action": "scrape",
                    "event.outcome": "success",
                    "chp.total_seen": total_seen,
                    "chp.active_seen": active_seen,
                    "chp.active_with_coords": active_with_coords,
                    "chp.region_counts": region_counts,
                    "chp.observations_inserted": changed_rows,
                    "chp.details_requested": details_requested,
                    "chp.details_skipped": details_skipped,
                    "chp.duration_seconds": round(duration_seconds, 3),
                    "chp.source_durations": {
                        source: round(duration, 3) for source, duration in source_durations.items()
                    },
                    "chp.source_bytes": source_bytes,
                    "chp.http_status_counts": http_status_counts,
                    "chp.centers": args.center,
                    "http.request.header.user_agent": args.user_agent,
                },
            )
        except Exception as exc:
            duration_seconds = time.monotonic() - started_at
            SCRAPER_METRICS.record_source_attempt(args.source_mode, "primary", "failure")
            SCRAPER_METRICS.record_failure(observed_at, duration_seconds, exc)
            log_exception(
                "CHP scrape failed",
                exc,
                **{
                    "event.action": "scrape",
                    "event.outcome": "failure",
                    "chp.duration_seconds": round(duration_seconds, 3),
                    "chp.centers": args.center,
                    "http.request.header.user_agent": args.user_agent,
                },
            )
            if args.interval <= 0:
                raise
        if args.interval <= 0:
            break
        time.sleep(args.interval)
    if metrics_server:
        metrics_server.shutdown()


if __name__ == "__main__":
    run_main(main)
