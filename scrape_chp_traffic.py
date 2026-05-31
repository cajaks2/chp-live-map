import argparse
import datetime as dt
import hashlib
import html
import json
import re
import sqlite3
import time
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


CHP_TRAFFIC_URL = "https://cad.chp.ca.gov/Traffic.aspx"
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
    "sr2",
    "sr-2",
    "state route 2",
    "sr39",
    "sr-39",
    "state route 39",
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
            self.tables[self._table_id] = []
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


def post_form(opener, url, data, timeout):
    req = Request(
        url,
        urlencode(data).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0",
            "Referer": url,
        },
    )
    return opener.open(req, timeout=timeout).read().decode("utf-8", errors="replace")


def get_page(opener, url, timeout):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return opener.open(req, timeout=timeout).read().decode("utf-8", errors="replace")


def select_center(center, timeout):
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    initial = parse_page(get_page(opener, CHP_TRAFFIC_URL, timeout))
    data = {
        **initial.hidden,
        "ddlComCenter": center,
        "ddlSearches": "Choose One",
        "ddlResources": "Choose One",
        "btnCCGo": "OK",
    }
    page_text = post_form(opener, CHP_TRAFFIC_URL, data, timeout)
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


def fetch_details(opener, center, list_parser, select_index, timeout):
    data = {
        **list_parser.hidden,
        "__EVENTTARGET": "gvIncidents",
        "__EVENTARGUMENT": f"Select${select_index}",
        "ddlComCenter": center,
        "ddlSearches": "Choose One",
        "ddlResources": "Choose One",
    }
    detail_text = post_form(opener, CHP_TRAFFIC_URL, data, timeout)
    parser = parse_page(detail_text)
    spans = parser.spans
    lat, lon = parse_lat_lon(spans.get("lblLatLon", ""))
    if lat is None or lon is None:
        lat, lon = parse_lat_lon_from_detail_html(detail_text)
    detail_entries = []
    for row in parser.tables.get("tblDetails", [])[1:]:
        if len(row) >= 3:
            detail_entries.append(
                {
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


def connect_database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_database(conn)
    return conn


def init_database(conn):
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
            active_seen INTEGER NOT NULL,
            observations_inserted INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
        CREATE INDEX IF NOT EXISTS idx_events_center_status ON events(center, status);
        CREATE INDEX IF NOT EXISTS idx_observations_event ON observations(event_key, observed_at);
        """
    )


def row_for_event(conn, event_key_value):
    return conn.execute(
        "SELECT * FROM events WHERE event_key = ?", (event_key_value,)
    ).fetchone()


def upsert_active_event(conn, row):
    previous = row_for_event(conn, row["event_key"])
    first_seen = previous["first_seen"] if previous else row["observed_at"]
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
        {**row, "first_seen": first_seen, "last_seen": row["observed_at"]},
    )
    return previous


def insert_observation(conn, row, status):
    details_json = json.dumps(row["detail_entries"], ensure_ascii=False)
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
        conn.execute(
            """
            INSERT INTO detail_entries (
                event_key, observed_at, entry_index, entry_time, entry_no, text
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["event_key"],
                row["observed_at"],
                entry_index,
                entry.get("time"),
                entry.get("entry_no"),
                entry.get("text"),
            ),
        )


def mark_cleared(conn, event, observed_at):
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


def store_scrape_run(conn, observed_at, centers, active_seen, observations_inserted):
    conn.execute(
        """
        INSERT INTO scrape_runs (
            observed_at, centers, active_seen, observations_inserted
        ) VALUES (?, ?, ?, ?)
        """,
        (observed_at, ",".join(centers), active_seen, observations_inserted),
    )


def scrape_once(args):
    now = dt.datetime.now().astimezone()
    observed_at = now.isoformat(timespec="seconds")
    seen_keys = set()
    seen_with_coords = set()
    observations_inserted = 0

    with connect_database(args.database) as conn:
        for center in args.center:
            opener, list_parser = select_center(center, args.timeout)
            updated_at, updated_as_of = parse_updated_at(
                list_parser.spans.get("lblUpdated", ""), now
            )
            for incident in parse_incidents(center, list_parser):
                matches = matching_keywords(incident, args.road)
                if not args.all_roads and not matches:
                    continue
                time.sleep(args.detail_delay)
                details = fetch_details(
                    opener, center, list_parser, incident["select_index"], args.timeout
                )
                merged = {**incident, **{k: v for k, v in details.items() if v}}
                incident_date = incident_date_for_time(updated_at, merged["incident_time"])
                row = {
                    **merged,
                    "observed_at": observed_at,
                    "updated_as_of": updated_as_of,
                    "incident_date": incident_date,
                    "event_key": event_key(center, incident_date, merged["incident_no"]),
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

        for event in conn.execute(
            """
            SELECT * FROM events
            WHERE status = 'active'
              AND center IN ({})
            """.format(",".join("?" for _ in args.center)),
            tuple(args.center),
        ).fetchall():
            if event["event_key"] not in seen_keys:
                mark_cleared(conn, event, observed_at)
                observations_inserted += 1

        store_scrape_run(conn, observed_at, args.center, len(seen_keys), observations_inserted)
    return observations_inserted, len(seen_keys), len(seen_with_coords)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape public CHP CAD active incidents into SQLite."
    )
    parser.add_argument("--center", action="append", default=[], help="CHP center code, e.g. LACC. Repeatable.")
    parser.add_argument("--road", action="append", default=[], help="Road keyword to match. Repeatable.")
    parser.add_argument("--all-roads", action="store_true", help="Capture every incident in the selected centers.")
    parser.add_argument("--database", type=Path, default=Path("chp_traffic.sqlite"))
    parser.add_argument("--interval", type=int, default=0, help="Poll interval in seconds. Default runs once.")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--detail-delay", type=float, default=0.2)
    args = parser.parse_args()
    args.center = args.center or DEFAULT_CENTERS
    args.road = args.road or DEFAULT_ROAD_KEYWORDS
    return args


def main():
    args = parse_args()
    while True:
        changed_rows, active_seen, active_with_coords = scrape_once(args)
        print(
            f"{dt.datetime.now().isoformat(timespec='seconds')} "
            f"active_seen={active_seen} active_with_coords={active_with_coords} "
            f"observations_inserted={changed_rows}"
        )
        if args.interval <= 0:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
