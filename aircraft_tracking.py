import argparse
import datetime as dt
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scrape_chp_traffic import connect_database


OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
DEFAULT_BOUNDS = (33.60, 34.80, -119.10, -117.30)
DEFAULT_RESCUE_AIRCRAFT = {
    "ad3574": {"registration": "N950JE", "aircraft_type": "AS332L1"},
    "ad395a": {"registration": "N951LB", "aircraft_type": "AS332L1"},
    "ad3ce5": {"registration": "N952JH", "aircraft_type": "AS332L1"},
}
DISCOVERY_CALLSIGN_PATTERN = re.compile(r"(?:AIR\s*5|RESCUE|SHERIFF)", re.IGNORECASE)


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso_timestamp(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def timestamp_from_epoch(value):
    if value is None:
        return None
    return iso_timestamp(dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc))


def is_postgres(conn):
    return conn.__class__.__module__.startswith("psycopg")


def placeholder(conn):
    return "%s" if is_postgres(conn) else "?"


@dataclass
class TrackerSettings:
    database: str = "chp_traffic.sqlite"
    database_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    interval_seconds: int = 30
    retention_hours: int = 24
    bounds: tuple[float, float, float, float] = DEFAULT_BOUNDS

    @classmethod
    def from_env(cls):
        return cls(
            database=os.environ.get("DATABASE", "chp_traffic.sqlite"),
            database_url=os.environ.get("DATABASE_URL") or None,
            client_id=os.environ.get("OPENSKY_CLIENT_ID") or None,
            client_secret=os.environ.get("OPENSKY_CLIENT_SECRET") or None,
            interval_seconds=max(15, int(os.environ.get("AIRCRAFT_POLL_SECONDS", "30"))),
            retention_hours=max(1, int(os.environ.get("AIRCRAFT_RETENTION_HOURS", "24"))),
        )


class OpenSkyClient:
    def __init__(self, client_id, client_secret, timeout=15):
        if not client_id or not client_secret:
            raise ValueError("OpenSky client ID and secret are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.access_token = None
        self.token_expires_at = 0.0

    def authenticate(self):
        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode("utf-8")
        request = Request(
            OPENSKY_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        self.access_token = payload["access_token"]
        self.token_expires_at = time.time() + max(30, int(payload.get("expires_in", 300)) - 30)

    def states(self, bounds=DEFAULT_BOUNDS):
        if not self.access_token or time.time() >= self.token_expires_at:
            self.authenticate()
        lat_min, lat_max, lon_min, lon_max = bounds
        query = urlencode({"lamin": lat_min, "lamax": lat_max, "lomin": lon_min, "lomax": lon_max})
        request = Request(
            f"{OPENSKY_STATES_URL}?{query}",
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response), response.headers.get("X-Rate-Limit-Remaining")
        except HTTPError as exc:
            if exc.code != 401:
                raise
            self.access_token = None
            self.authenticate()
            request.headers["Authorization"] = f"Bearer {self.access_token}"
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response), response.headers.get("X-Rate-Limit-Remaining")


def parse_state_vector(state, fetched_at=None):
    if not isinstance(state, list) or len(state) < 17:
        return None
    icao24 = str(state[0] or "").strip().lower()
    identity = DEFAULT_RESCUE_AIRCRAFT.get(icao24)
    callsign = str(state[1] or "").strip()
    if not identity or state[5] is None or state[6] is None:
        return None
    observed_at = timestamp_from_epoch(state[3] or state[4])
    if not observed_at:
        return None
    return {
        "icao24": icao24,
        "registration": identity["registration"],
        "aircraft_type": identity["aircraft_type"],
        "display_name": "LASD rescue helicopter",
        "callsign": callsign,
        "observed_at": observed_at,
        "fetched_at": iso_timestamp(fetched_at or utc_now()),
        "longitude": float(state[5]),
        "latitude": float(state[6]),
        "baro_altitude_m": state[7],
        "on_ground": bool(state[8]),
        "velocity_mps": state[9],
        "true_track": state[10],
        "vertical_rate_mps": state[11],
        "geometric_altitude_m": state[13],
        "source": "opensky",
    }


def parse_states(payload, fetched_at=None):
    vectors = payload.get("states") or [] if isinstance(payload, dict) else []
    positions = [parse_state_vector(vector, fetched_at=fetched_at) for vector in vectors]
    positions = [position for position in positions if position]
    candidates = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) < 7:
            continue
        callsign = str(vector[1] or "").strip()
        icao24 = str(vector[0] or "").strip().lower()
        if callsign and icao24 not in DEFAULT_RESCUE_AIRCRAFT and DISCOVERY_CALLSIGN_PATTERN.search(callsign):
            candidates.append({"icao24": icao24, "callsign": callsign})
    return positions, candidates, len(vectors)


def save_position(conn, position):
    marker = placeholder(conn)
    columns = (
        "icao24", "registration", "aircraft_type", "display_name", "callsign", "observed_at",
        "fetched_at", "longitude", "latitude", "baro_altitude_m", "geometric_altitude_m",
        "velocity_mps", "true_track", "vertical_rate_mps", "on_ground", "source",
    )
    values = tuple(position[column] for column in columns)
    placeholders = ", ".join([marker] * len(columns))
    if is_postgres(conn):
        conn.execute(
            f"INSERT INTO aircraft_positions ({', '.join(columns)}) VALUES ({placeholders}) "
            "ON CONFLICT (icao24, observed_at) DO UPDATE SET "
            "fetched_at = EXCLUDED.fetched_at, callsign = EXCLUDED.callsign",
            values,
        )
    else:
        conn.execute(
            f"INSERT INTO aircraft_positions ({', '.join(columns)}) VALUES ({placeholders}) "
            "ON CONFLICT (icao24, observed_at) DO UPDATE SET "
            "fetched_at = excluded.fetched_at, callsign = excluded.callsign",
            values,
        )


def record_tracker_result(
    conn,
    attempted_at,
    success,
    rate_limit_remaining=None,
    aircraft_in_box=0,
    matched_aircraft=0,
    candidate_callsigns=0,
    error=None,
):
    marker = placeholder(conn)
    now = iso_timestamp(attempted_at)
    boolean = success if is_postgres(conn) else int(success)
    params = (
        1,
        "opensky",
        now,
        now if success else None,
        str(error or "")[:1000],
        int(rate_limit_remaining) if str(rate_limit_remaining or "").isdigit() else None,
        int(aircraft_in_box),
        int(matched_aircraft),
        int(candidate_callsigns),
        boolean,
        1,
        0 if success else 1,
    )
    placeholders = ", ".join([marker] * len(params))
    if is_postgres(conn):
        update = """
            provider = EXCLUDED.provider,
            last_attempt_at = EXCLUDED.last_attempt_at,
            last_success_at = COALESCE(EXCLUDED.last_success_at, aircraft_tracker_status.last_success_at),
            last_error = EXCLUDED.last_error,
            rate_limit_remaining = EXCLUDED.rate_limit_remaining,
            aircraft_in_box = EXCLUDED.aircraft_in_box,
            matched_aircraft = EXCLUDED.matched_aircraft,
            candidate_callsigns = EXCLUDED.candidate_callsigns,
            requests_total = aircraft_tracker_status.requests_total + 1,
            errors_total = aircraft_tracker_status.errors_total + CASE WHEN EXCLUDED.last_run_success THEN 0 ELSE 1 END,
            last_run_success = EXCLUDED.last_run_success
        """
    else:
        update = """
            provider = excluded.provider,
            last_attempt_at = excluded.last_attempt_at,
            last_success_at = COALESCE(excluded.last_success_at, aircraft_tracker_status.last_success_at),
            last_error = excluded.last_error,
            rate_limit_remaining = excluded.rate_limit_remaining,
            aircraft_in_box = excluded.aircraft_in_box,
            matched_aircraft = excluded.matched_aircraft,
            candidate_callsigns = excluded.candidate_callsigns,
            requests_total = aircraft_tracker_status.requests_total + 1,
            errors_total = aircraft_tracker_status.errors_total + CASE WHEN excluded.last_run_success THEN 0 ELSE 1 END,
            last_run_success = excluded.last_run_success
        """
    conn.execute(
        f"""
        INSERT INTO aircraft_tracker_status (
            id, provider, last_attempt_at, last_success_at, last_error, rate_limit_remaining,
            aircraft_in_box, matched_aircraft, candidate_callsigns, last_run_success,
            requests_total, errors_total
        ) VALUES ({placeholders})
        ON CONFLICT (id) DO UPDATE SET {update}
        """,
        params,
    )


def cleanup_positions(conn, before):
    conn.execute(
        f"DELETE FROM aircraft_positions WHERE observed_at < {placeholder(conn)}",
        (iso_timestamp(before),),
    )


def load_visible_aircraft(
    conn,
    now=None,
    delay_seconds=60,
    max_age_seconds=300,
    trail_age_seconds=1800,
):
    now = now or utc_now()
    newest = iso_timestamp(now - dt.timedelta(seconds=max(0, delay_seconds)))
    trail_age_seconds = max(max_age_seconds, trail_age_seconds)
    oldest = iso_timestamp(now - dt.timedelta(seconds=trail_age_seconds))
    marker = placeholder(conn)
    rows = conn.execute(
        f"""
        SELECT * FROM aircraft_positions
        WHERE observed_at <= {marker} AND observed_at >= {marker}
        ORDER BY observed_at DESC
        """,
        (newest, oldest),
    ).fetchall()
    aircraft = []
    positions = {}
    for raw_row in rows:
        row = dict(raw_row)
        positions.setdefault(row["icao24"], []).append(row)
    for aircraft_rows in positions.values():
        row = aircraft_rows[0]
        observed = dt.datetime.fromisoformat(row["observed_at"])
        age_seconds = max(0, int((now - observed).total_seconds()))
        if age_seconds > max_age_seconds or row["on_ground"]:
            continue
        trail = []
        for trail_row in reversed(aircraft_rows):
            if trail_row["on_ground"]:
                continue
            point = [trail_row["latitude"], trail_row["longitude"]]
            if not trail or trail[-1] != point:
                trail.append(point)
        aircraft.append(
            {
                "icao24": row["icao24"],
                "registration": row["registration"],
                "aircraft_type": row["aircraft_type"],
                "display_name": row["display_name"],
                "callsign": row["callsign"] or None,
                "observed_at": row["observed_at"],
                "age_seconds": age_seconds,
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "altitude_ft": round(float(row["geometric_altitude_m"] or row["baro_altitude_m"]) * 3.28084)
                if row["geometric_altitude_m"] is not None or row["baro_altitude_m"] is not None
                else None,
                "speed_kt": round(float(row["velocity_mps"]) * 1.94384)
                if row["velocity_mps"] is not None
                else None,
                "heading": round(float(row["true_track"])) if row["true_track"] is not None else None,
                "mission_confirmed": False,
                "trail": trail,
            }
        )
    return aircraft


def load_tracker_status(conn):
    row = conn.execute("SELECT * FROM aircraft_tracker_status WHERE id = 1").fetchone()
    return dict(row) if row else None


def run_once(settings, client=None, now=None):
    now = now or utc_now()
    client = client or OpenSkyClient(settings.client_id, settings.client_secret)
    conn = connect_database(settings.database, settings.database_url)
    try:
        try:
            payload, remaining = client.states(settings.bounds)
            positions, candidates, aircraft_in_box = parse_states(payload, fetched_at=now)
            for position in positions:
                save_position(conn, position)
            cleanup_positions(conn, now - dt.timedelta(hours=settings.retention_hours))
            record_tracker_result(
                conn,
                now,
                True,
                rate_limit_remaining=remaining,
                aircraft_in_box=aircraft_in_box,
                matched_aircraft=len(positions),
                candidate_callsigns=len(candidates),
            )
            conn.commit()
            return {"positions": positions, "candidates": candidates, "aircraft_in_box": aircraft_in_box}
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
            record_tracker_result(conn, now, False, error=exc)
            conn.commit()
            raise
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Track LASD rescue helicopters using OpenSky state vectors.")
    parser.add_argument("--once", action="store_true", help="Poll once and exit.")
    parser.add_argument("--interval", type=int, help="Seconds between polls.")
    args = parser.parse_args(argv)
    settings = TrackerSettings.from_env()
    if args.interval:
        settings.interval_seconds = max(15, args.interval)
    client = OpenSkyClient(settings.client_id, settings.client_secret)
    while True:
        started = time.monotonic()
        try:
            result = run_once(settings, client=client)
            print(
                json.dumps(
                    {
                        "message": "OpenSky aircraft poll completed",
                        "aircraft_in_box": result["aircraft_in_box"],
                        "matched_aircraft": len(result["positions"]),
                        "candidate_callsigns": len(result["candidates"]),
                        "candidates": result["candidates"],
                    }
                ),
                flush=True,
            )
        except Exception as exc:
            print(json.dumps({"message": "OpenSky aircraft poll failed", "error": str(exc)}), flush=True)
        if args.once:
            return
        elapsed = time.monotonic() - started
        time.sleep(max(1, settings.interval_seconds - elapsed))


if __name__ == "__main__":
    main()
