import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import time
import urllib.robotparser
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from ecs_logging import log_event, run_main


CHP_TRAFFIC_URL = "https://cad.chp.ca.gov/Traffic.aspx"
CHP_ROBOTS_URL = "https://cad.chp.ca.gov/robots.txt"
DEFAULT_USER_AGENT = "chp-live-map/0.1 (+https://crestmap.us/)"
DEFAULT_CENTERS = ["LACC"]
DEFAULT_ROAD_KEYWORDS = [
    "angeles crest",
    "angeles forest",
    "upper big tujunga",
    "big tujunga canyon",
    "mt wilson red box",
    "red box",
    "san gabriel canyon",
    "glendora mountain",
    "glendora ridge",
]


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


def parse_page(text):
    parser = ChpTrafficParser()
    parser.feed(text)
    return parser


def record_response_status(stats, method, route, status):
    if stats is None:
        return
    key = f"{method}:{route}:{status}"
    stats.setdefault("http_status_counts", {})
    stats["http_status_counts"][key] = stats["http_status_counts"].get(key, 0) + 1


def request_text(opener, req, timeout, retries, backoff, stats=None, route="traffic"):
    for attempt in range(retries + 1):
        try:
            with opener.open(req, timeout=timeout) as response:
                record_response_status(stats, req.get_method(), route, response.status)
                return response.read().decode("utf-8", errors="replace")
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
    return [keyword for keyword in keywords if keyword.casefold() in haystack]


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
            details_hash TEXT
        );

        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
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
            entry_time TEXT,
            entry_no TEXT,
            text TEXT,
            FOREIGN KEY (event_key) REFERENCES events(event_key)
        );

        CREATE TABLE IF NOT EXISTS scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
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
    ensure_column_sqlite(conn, "scrape_runs", "active_with_coords", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_sqlite(conn, "scrape_runs", "details_requested", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_sqlite(conn, "scrape_runs", "details_skipped", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_sqlite(conn, "scrape_runs", "duration_seconds", "REAL NOT NULL DEFAULT 0")
    ensure_column_sqlite(conn, "scrape_runs", "http_status_counts", "TEXT NOT NULL DEFAULT '{}'")


def init_database_postgres(conn):
    statements = [
        """
        CREATE TABLE IF NOT EXISTS events (
            event_key TEXT PRIMARY KEY,
            center TEXT NOT NULL,
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
            details_hash TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS observations (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            event_key TEXT NOT NULL REFERENCES events(event_key),
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
            entry_time TEXT,
            entry_no TEXT,
            text TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            observed_at TEXT NOT NULL,
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
    ensure_column_postgres(conn, "scrape_runs", "active_with_coords", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_postgres(conn, "scrape_runs", "details_requested", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_postgres(conn, "scrape_runs", "details_skipped", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_postgres(conn, "scrape_runs", "duration_seconds", "DOUBLE PRECISION NOT NULL DEFAULT 0")
    ensure_column_postgres(conn, "scrape_runs", "http_status_counts", "TEXT NOT NULL DEFAULT '{}'")
    conn.commit()


def ensure_column_sqlite(conn, table, column, definition):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_column_postgres(conn, table, column, definition):
    conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")


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
    latest = event["latest_observed_at"] if event else None
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
    params = {**row, "first_seen": first_seen, "last_seen": row["observed_at"]}
    if is_postgres(conn):
        conn.execute(
            """
            INSERT INTO events (
                event_key, center, incident_date, incident_no, first_seen, last_seen,
                cleared_at, status, latest_observed_at, updated_as_of, incident_time,
                type, location, location_desc, area, latitude, longitude,
                matched_keywords, details_hash
            ) VALUES (
                %(event_key)s, %(center)s, %(incident_date)s, %(incident_no)s,
                %(first_seen)s, %(last_seen)s, NULL, 'active', %(observed_at)s,
                %(updated_as_of)s, %(incident_time)s, %(type)s, %(location)s,
                %(location_desc)s, %(area)s, %(latitude)s, %(longitude)s,
                %(matched_keywords)s, %(details_hash)s
            )
            ON CONFLICT(event_key) DO UPDATE SET
                last_seen = excluded.last_seen,
                cleared_at = NULL,
                status = 'active',
                latest_observed_at = excluded.latest_observed_at,
                updated_as_of = excluded.updated_as_of,
                incident_time = excluded.incident_time,
                type = excluded.type,
                location = excluded.location,
                location_desc = excluded.location_desc,
                area = excluded.area,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                matched_keywords = excluded.matched_keywords,
                details_hash = excluded.details_hash
            """,
            params,
        )
        return previous

    conn.execute(
        """
        INSERT INTO events (
            event_key, center, incident_date, incident_no, first_seen, last_seen,
            cleared_at, status, latest_observed_at, updated_as_of, incident_time,
            type, location, location_desc, area, latitude, longitude,
            matched_keywords, details_hash
        ) VALUES (
            :event_key, :center, :incident_date, :incident_no, :first_seen,
            :last_seen, NULL, 'active', :observed_at, :updated_as_of,
            :incident_time, :type, :location, :location_desc, :area, :latitude,
            :longitude, :matched_keywords, :details_hash
        )
        ON CONFLICT(event_key) DO UPDATE SET
            last_seen = excluded.last_seen,
            cleared_at = NULL,
            status = 'active',
            latest_observed_at = excluded.latest_observed_at,
            updated_as_of = excluded.updated_as_of,
            incident_time = excluded.incident_time,
            type = excluded.type,
            location = excluded.location,
            location_desc = excluded.location_desc,
            area = excluded.area,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            matched_keywords = excluded.matched_keywords,
            details_hash = excluded.details_hash
        """,
        params,
    )
    return previous


def insert_observation(conn, row, status):
    details_json = json.dumps(row["detail_entries"], ensure_ascii=False)
    if is_postgres(conn):
        conn.execute(
            """
            INSERT INTO observations (
                event_key, observed_at, status, updated_as_of, incident_time, type,
                location, location_desc, area, latitude, longitude, matched_keywords,
                details_hash, details_json
            ) VALUES (
                %(event_key)s, %(observed_at)s, %(status)s, %(updated_as_of)s,
                %(incident_time)s, %(type)s, %(location)s, %(location_desc)s, %(area)s,
                %(latitude)s, %(longitude)s, %(matched_keywords)s, %(details_hash)s,
                %(details_json)s
            )
            """,
            {**row, "status": status, "details_json": details_json},
        )
    else:
        conn.execute(
            """
        INSERT INTO observations (
            event_key, observed_at, status, updated_as_of, incident_time, type,
            location, location_desc, area, latitude, longitude, matched_keywords,
            details_hash, details_json
        ) VALUES (
            :event_key, :observed_at, :status, :updated_as_of, :incident_time,
            :type, :location, :location_desc, :area, :latitude, :longitude,
            :matched_keywords, :details_hash, :details_json
        )
        """,
            {**row, "status": status, "details_json": details_json},
        )
    for entry_index, entry in enumerate(row["detail_entries"], start=1):
        query = """
        INSERT INTO detail_entries (
            event_key, observed_at, entry_index, entry_time, entry_no, text
        ) VALUES ({})
        """.format("%s, %s, %s, %s, %s, %s" if is_postgres(conn) else "?, ?, ?, ?, ?, ?")
        conn.execute(
            query,
            (
                row["event_key"], row["observed_at"], entry_index,
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
):
    placeholder = (
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
        if is_postgres(conn)
        else "?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
    )
    conn.execute(
        f"""
        INSERT INTO scrape_runs (
            observed_at, centers, total_seen, active_seen, observations_inserted,
            active_with_coords, details_requested, details_skipped, duration_seconds,
            http_status_counts
        ) VALUES ({placeholder})
        """,
        (
            observed_at,
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


def scrape_once(args):
    started_at = time.monotonic()
    now = dt.datetime.now().astimezone()
    observed_at = now.isoformat(timespec="seconds")
    seen_keys = set()
    seen_with_coords = set()
    total_seen = 0
    details_requested = 0
    details_skipped = 0
    observations_inserted = 0
    stats = {"http_status_counts": {}}

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
                incident_date = incident_date_for_time(updated_at, incident["incident_time"])
                current_event_key = event_key(center, incident_date, incident["incident_no"])
                previous = row_for_event(conn, current_event_key)
                if not should_fetch_details(previous, incident, now, args.detail_refresh_minutes):
                    details_skipped += 1
                    seen_keys.add(current_event_key)
                    if previous["latitude"] is not None and previous["longitude"] is not None:
                        seen_with_coords.add(current_event_key)
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
                row = {
                    **merged,
                    "observed_at": observed_at,
                    "updated_as_of": updated_as_of,
                    "incident_date": incident_date,
                    "event_key": current_event_key,
                    "matched_keywords": ";".join(matches) if matches else "*",
                    "detail_entries": merged.get("detail_entries", []),
                }
                row["details_hash"] = details_hash(row)
                seen_keys.add(row["event_key"])
                if row["latitude"] is not None and row["longitude"] is not None:
                    seen_with_coords.add(row["event_key"])
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
        )
    return (
        observations_inserted,
        total_seen,
        len(seen_keys),
        len(seen_with_coords),
        details_requested,
        details_skipped,
        time.monotonic() - started_at,
        stats["http_status_counts"],
    )


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
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--detail-delay", type=float, default=0.2)
    parser.add_argument("--detail-refresh-minutes", type=float, default=3.0)
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
    while True:
        (
            changed_rows,
            total_seen,
            active_seen,
            active_with_coords,
            details_requested,
            details_skipped,
            duration_seconds,
            http_status_counts,
        ) = scrape_once(args)
        log_event(
            "info",
            "CHP scrape completed",
            **{
                "event.action": "scrape",
                "event.outcome": "success",
                "chp.total_seen": total_seen,
                "chp.active_seen": active_seen,
                "chp.active_with_coords": active_with_coords,
                "chp.observations_inserted": changed_rows,
                "chp.details_requested": details_requested,
                "chp.details_skipped": details_skipped,
                "chp.duration_seconds": round(duration_seconds, 3),
                "chp.http_status_counts": http_status_counts,
                "chp.centers": args.center,
                "http.request.header.user_agent": args.user_agent,
            },
        )
        if args.interval <= 0:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    run_main(main)
