import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ecs_logging import log_event, log_exception, run_main
from geo_bounds import coordinates_in_region_bounds
from scrape_chp_traffic import (
    PACIFIC_TZ,
    REGION_ROAD_KEYWORDS,
    ScraperMetrics,
    connect_database,
    deliver_push_notifications,
    details_hash,
    format_incident_time,
    insert_observation,
    is_malibu_101_primary_roadway,
    log_discovered_incident,
    matching_regions,
    region_for_incident,
    store_scrape_run,
    start_metrics_server,
    upsert_active_event,
)


WILDWEB_API_TEMPLATE = "https://snknmqmon6.execute-api.us-west-2.amazonaws.com/centers/{center}/incidents"
WILDWEB_PAGE_TEMPLATE = "https://www.wildwebe.net/incidents?dc_Name={center}"
DEFAULT_CENTERS = ["CAANCC"]
DEFAULT_USER_AGENT = "crestmap-wildweb/0.1 (+https://crestmap.us/)"
WILDWEB_METRICS = ScraperMetrics(provider="wildweb", source_defaults=("api",))
ALLOWED_TYPES = {
    "medical aid",
    "miscellaneous",
    "motor vehicle accident",
    "wildfire",
    "wildland search/rescue/recovery",
}
IGNORED_NAMES = {
    "daily status",
    "extended staffing",
    "flight following",
    "duty coverage",
}
REGION_PLACE_KEYWORDS = {
    "forest": [
        "buckhorn campground",
        "charlton flats",
        "chilao",
        "coldbrook",
        "crystal lake",
        "gould mesa",
        "henninger flats",
        "mt lukens",
        "mount lukens",
        "rincon station",
        "san gabriel peak",
        "strawberry peak",
        "switzer",
        "vincent gap",
    ],
    "malibu": [
        "backbone trail",
        "circle x ranch",
        "leo carrillo",
        "malibu creek",
        "mishe mokwa",
        "paramount ranch",
        "point mugu",
        "santa monica mountains",
        "sycamore canyon",
    ],
}


def parse_json_object(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_local_timestamp(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=PACIFIC_TZ)
    return parsed.astimezone(PACIFIC_TZ)


def parse_retrieved_timestamp(value, fallback):
    if not value:
        return fallback
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone()


def numeric_coordinate(value):
    if value in (None, "", "*******"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_coordinates(latitude, longitude):
    lat = numeric_coordinate(latitude)
    lon = numeric_coordinate(longitude)
    if lat is None or lon is None:
        return None, None
    lon = -abs(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def wildweb_region_matches(record):
    matches = matching_regions(record)
    haystack = " ".join(
        str(record.get(field) or "").casefold()
        for field in ("type", "location", "location_desc", "area")
    )
    for region, keywords in REGION_PLACE_KEYWORDS.items():
        place_matches = [keyword for keyword in keywords if keyword in haystack]
        if place_matches:
            matches.setdefault(region, []).extend(place_matches)
    return matches


def classify_region(record):
    if is_malibu_101_primary_roadway(record):
        return None, []
    latitude = record.get("latitude")
    longitude = record.get("longitude")
    if latitude is not None and longitude is not None:
        for region in REGION_ROAD_KEYWORDS:
            if coordinates_in_region_bounds(latitude, longitude, region):
                return region, ["coordinate boundary"]
        return None, []
    matches = wildweb_region_matches(record)
    region = region_for_incident(matches)
    return region, matches.get(region, []) if region else []


def source_status(item):
    fire_status = parse_json_object(item.get("fire_status"))
    if fire_status.get("out") and fire_status.get("out") != "*******":
        return "out", parse_local_timestamp(fire_status.get("out"))
    if fire_status.get("control") and fire_status.get("control") != "*******":
        return "controlled", None
    if fire_status.get("contain") and fire_status.get("contain") != "*******":
        return "contained", None
    return "listed", None


def ignored_record(item):
    incident_type = str(item.get("type") or "").strip().casefold()
    if incident_type not in ALLOWED_TYPES:
        return True
    name = str(item.get("name") or "").strip().casefold()
    return any(ignored in name for ignored in IGNORED_NAMES)


def detail_entries_for_item(item):
    entries = []
    web_comment = str(item.get("webComment") or "").strip()
    if web_comment and web_comment != "*******":
        entries.append(
            {
                "section": "WildWeb Information",
                "time": "",
                "entry_no": "1",
                "text": web_comment,
            }
        )
    fire_status = parse_json_object(item.get("fire_status"))
    for label, key in (("Contained", "contain"), ("Controlled", "control"), ("Out", "out")):
        value = fire_status.get(key)
        if value and value != "*******":
            parsed = parse_local_timestamp(value)
            entries.append(
                {
                    "section": "WildWeb Fire Status",
                    "time": format_incident_time(parsed) if parsed else "",
                    "entry_no": str(len(entries) + 1),
                    "text": f"{label}: {value}",
                }
            )
    return entries


def normalize_incident(item, center, retrieved_at, observed_at, max_age_hours=72, future_tolerance_minutes=5):
    if not isinstance(item, dict) or ignored_record(item):
        return None
    source_event_id = str(item.get("uuid") or "").strip()
    reported_at = parse_local_timestamp(item.get("date"))
    now = dt.datetime.fromisoformat(observed_at).astimezone(PACIFIC_TZ)
    if not source_event_id or reported_at is None:
        return None
    if reported_at > now + dt.timedelta(minutes=future_tolerance_minutes):
        return None
    if reported_at < now - dt.timedelta(hours=max_age_hours):
        return None

    latitude, longitude = normalize_coordinates(item.get("latitude"), item.get("longitude"))
    fiscal_data = parse_json_object(item.get("fiscal_data"))
    unit = str(fiscal_data.get("wfdssunit") or center).strip()
    incident_no = str(item.get("inc_num") or "").strip()
    display_no = f"{unit}-{incident_no}" if unit and incident_no else incident_no or source_event_id[:8]
    row = {
        "event_key": f"wildweb|{center}|{source_event_id}",
        "center": center,
        "source": "wildweb",
        "source_event_id": source_event_id,
        "source_url": WILDWEB_PAGE_TEMPLATE.format(center=center),
        "source_reported_at": reported_at.isoformat(timespec="seconds"),
        "coordinate_confidence": "exact" if latitude is not None and longitude is not None else "missing",
        "incident_date": reported_at.date().isoformat(),
        "incident_no": display_no,
        "observed_at": observed_at,
        "updated_as_of": retrieved_at.isoformat(timespec="seconds"),
        "incident_time": format_incident_time(reported_at),
        "type": str(item.get("type") or "").strip(),
        "location": str(item.get("location") or "").strip(),
        "location_desc": str(item.get("name") or "").strip(),
        "area": unit,
        "latitude": latitude,
        "longitude": longitude,
        "detail_entries": detail_entries_for_item(item),
    }
    region, matches = classify_region(row)
    if not region:
        return None
    row["region"] = region
    row["matched_keywords"] = ";".join(matches or ["coordinate boundary"])
    row["source_status"], out_at = source_status(item)
    row["status"] = "cleared" if row["source_status"] == "out" else "reported"
    row["cleared_at"] = out_at.isoformat(timespec="seconds") if out_at else None
    row["details_hash"] = details_hash(row)
    return row


def fetch_center(center, timeout, user_agent, retries=2, retry_backoff=2, stats=None, metrics=None):
    url = WILDWEB_API_TEMPLATE.format(center=center)
    last_error = None
    metrics = metrics or WILDWEB_METRICS
    for attempt in range(retries + 1):
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Origin": "https://www.wildwebe.net",
                "Referer": "https://www.wildwebe.net/",
                "User-Agent": user_agent,
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read()
                metrics.record_http("GET", "incidents", response.status)
                if stats is not None:
                    stats.setdefault("http_status_counts", {})[f"GET:wildweb:{response.status}"] = (
                        stats.setdefault("http_status_counts", {}).get(f"GET:wildweb:{response.status}", 0) + 1
                    )
                    stats["source_bytes"] = stats.get("source_bytes", 0) + len(body)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
                    raise ValueError("WildWeb response did not contain a center payload")
                return payload[0]
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError):
                status = exc.code
            elif isinstance(exc, TimeoutError):
                status = "timeout"
            elif isinstance(exc, URLError):
                status = "url_error"
            else:
                status = None
            if status is not None:
                metrics.record_http("GET", "incidents", status)
            if stats is not None and status is not None:
                key = f"GET:wildweb:{status}"
                stats.setdefault("http_status_counts", {})[key] = (
                    stats.setdefault("http_status_counts", {}).get(key, 0) + 1
                )
            if attempt < retries:
                time.sleep(retry_backoff * (2**attempt))
    raise RuntimeError(f"WildWeb request failed for {center}") from last_error


def archive_missing_events(conn, centers, seen_keys, observed_at, max_age_hours):
    placeholders = ",".join(["%s" if conn.__class__.__module__.startswith("psycopg") else "?"] * len(centers))
    rows = conn.execute(
        f"SELECT * FROM events WHERE source = 'wildweb' AND status = 'reported' AND center IN ({placeholders})",
        tuple(centers),
    ).fetchall()
    now = dt.datetime.fromisoformat(observed_at).astimezone(PACIFIC_TZ)
    changed = 0
    placeholder = "%s" if conn.__class__.__module__.startswith("psycopg") else "?"
    for event in rows:
        if event["event_key"] in seen_keys:
            continue
        reported_at = parse_local_timestamp(event["source_reported_at"])
        reason = "aged_out" if reported_at and now - reported_at > dt.timedelta(hours=max_age_hours) else "no_longer_listed"
        conn.execute(
            f"UPDATE events SET status = 'archived', source_status = {placeholder}, cleared_at = {placeholder}, latest_observed_at = {placeholder} WHERE event_key = {placeholder}",
            (reason, observed_at, observed_at, event["event_key"]),
        )
        archived = dict(event)
        archived["observed_at"] = observed_at
        archived["detail_entries"] = []
        insert_observation(conn, archived, "archived")
        changed += 1
    return changed


def scrape_once(args):
    started_at = time.monotonic()
    observed_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    stats = {"http_status_counts": {}, "source_bytes": 0}
    fetched = []
    retrieved_values = []
    for center in args.center:
        payload = fetch_center(
            center,
            args.timeout,
            args.user_agent,
            args.retries,
            args.retry_backoff,
            stats,
            WILDWEB_METRICS,
        )
        retrieved_at = parse_retrieved_timestamp(payload.get("retrieved"), dt.datetime.now().astimezone())
        retrieved_values.append(retrieved_at)
        fetched.extend((center, item, retrieved_at) for item in payload.get("data") or [])

    normalized = []
    for center, item, retrieved_at in fetched:
        row = normalize_incident(
            item,
            center,
            retrieved_at,
            observed_at,
            max_age_hours=args.max_age_hours,
            future_tolerance_minutes=args.future_tolerance_minutes,
        )
        if row:
            normalized.append(row)

    region_counts = {}
    for row in normalized:
        counts = region_counts.setdefault(row["region"], {"matched": 0, "mapped": 0})
        counts["matched"] += 1
        if row["latitude"] is not None and row["longitude"] is not None:
            counts["mapped"] += 1

    seen_keys = {row["event_key"] for row in normalized if row["status"] == "reported"}
    discovered = []
    observations_inserted = 0
    with connect_database(args.database, args.database_url) as conn:
        for row in normalized:
            previous = upsert_active_event(conn, row)
            if not previous and row["status"] == "reported":
                discovered.append(dict(row))
            if (
                not previous
                or previous["status"] != row["status"]
                or previous["source_status"] != row["source_status"]
                or previous["details_hash"] != row["details_hash"]
            ):
                insert_observation(conn, row, row["status"])
                observations_inserted += 1
        observations_inserted += archive_missing_events(
            conn,
            args.center,
            seen_keys,
            observed_at,
            args.max_age_hours,
        )
        mapped = sum(row["latitude"] is not None and row["longitude"] is not None for row in normalized)
        store_scrape_run(
            conn,
            observed_at,
            args.center,
            len(fetched),
            len(normalized),
            observations_inserted,
            mapped,
            0,
            0,
            time.monotonic() - started_at,
            stats["http_status_counts"],
            source="wildweb",
        )

    for row in discovered:
        log_discovered_incident(row)
    if args.notifications and discovered:
        deliver_push_notifications(args, discovered)
    return {
        "observed_at": observed_at,
        "total_seen": len(fetched),
        "matched": len(normalized),
        "mapped": sum(row["latitude"] is not None and row["longitude"] is not None for row in normalized),
        "changed": observations_inserted,
        "discovered": len(discovered),
        "duration_seconds": time.monotonic() - started_at,
        "source_bytes": stats["source_bytes"],
        "region_counts": region_counts,
        "retrieved_at": max(retrieved_values).isoformat(timespec="seconds") if retrieved_values else "",
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Collect selected WildWeb incidents for Crestmap.")
    parser.add_argument("--center", action="append", default=[])
    parser.add_argument("--database", type=Path, default=Path("chp_traffic.sqlite"))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--interval", type=int, default=int(os.environ.get("WILDWEB_INTERVAL_SECONDS", "0")))
    parser.add_argument("--metrics-host", default=os.environ.get("WILDWEB_METRICS_HOST"))
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.environ.get("WILDWEB_METRICS_PORT", "0")),
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-backoff", type=float, default=2)
    parser.add_argument("--max-age-hours", type=float, default=float(os.environ.get("WILDWEB_MAX_AGE_HOURS", "72")))
    parser.add_argument("--future-tolerance-minutes", type=float, default=5)
    parser.add_argument("--user-agent", default=os.environ.get("WILDWEB_USER_AGENT", DEFAULT_USER_AGENT))
    parser.add_argument(
        "--notifications",
        action="store_true",
        default=os.environ.get("WILDWEB_PUSH_NOTIFICATIONS", "").lower() in {"1", "true", "yes"},
    )
    args = parser.parse_args(argv)
    args.center = args.center or DEFAULT_CENTERS
    return args


def main():
    args = parse_args()
    metrics_server = None
    if args.metrics_port > 0:
        metrics_server = start_metrics_server(
            args.metrics_host or "127.0.0.1",
            args.metrics_port,
            WILDWEB_METRICS,
        )
    while True:
        started_at = time.monotonic()
        try:
            result = scrape_once(args)
            WILDWEB_METRICS.record_source_attempt("api", "primary", "success")
            WILDWEB_METRICS.record_success(
                result["observed_at"],
                result["changed"],
                result["total_seen"],
                result["matched"],
                result["mapped"],
                result["region_counts"],
                0,
                0,
                result["duration_seconds"],
                {"api": result["duration_seconds"], "total": result["duration_seconds"]},
                {"api": result["source_bytes"], "total": result["source_bytes"]},
            )
            log_event(
                "info",
                "WildWeb scrape completed",
                **{
                    "event.action": "scrape",
                    "event.outcome": "success",
                    "event.provider": "wildweb",
                    "wildweb.centers": args.center,
                    "wildweb.total_seen": result["total_seen"],
                    "wildweb.matched": result["matched"],
                    "wildweb.mapped": result["mapped"],
                    "wildweb.changed": result["changed"],
                    "wildweb.discovered": result["discovered"],
                    "wildweb.duration_seconds": round(result["duration_seconds"], 3),
                    "wildweb.retrieved_at": result["retrieved_at"],
                },
            )
        except Exception as exc:
            duration_seconds = time.monotonic() - started_at
            WILDWEB_METRICS.record_source_attempt("api", "primary", "failure")
            WILDWEB_METRICS.record_failure(
                dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                duration_seconds,
                exc,
            )
            log_exception(
                "WildWeb scrape failed",
                exc,
                **{
                    "event.action": "scrape",
                    "event.outcome": "failure",
                    "event.provider": "wildweb",
                    "wildweb.centers": args.center,
                },
            )
            if args.interval <= 0:
                raise
        if args.interval <= 0:
            break
        elapsed = time.monotonic() - started_at
        time.sleep(max(1, args.interval - elapsed))
    if metrics_server:
        metrics_server.shutdown()


if __name__ == "__main__":
    run_main(main)
