import datetime as dt

from aircraft_tracking import (
    TrackerSettings,
    load_tracker_status,
    load_visible_aircraft,
    parse_states,
    run_once,
)
from scrape_chp_traffic import connect_database
from serve_live_map import prometheus_metrics


def state_vector(
    icao24="ad395a",
    callsign="AIR5",
    observed_at=1786152600,
    longitude=-118.12,
    latitude=34.31,
    on_ground=False,
):
    return [
        icao24,
        callsign,
        "United States",
        observed_at,
        observed_at,
        longitude,
        latitude,
        1200.0,
        on_ground,
        55.0,
        275.0,
        1.5,
        None,
        1250.0,
        None,
        False,
        0,
    ]


def test_parse_states_matches_verified_aircraft_and_discovers_callsigns():
    now = dt.datetime(2026, 8, 7, 21, 31, tzinfo=dt.timezone.utc)
    positions, candidates, count = parse_states(
        {
            "states": [
                state_vector(),
                state_vector(icao24="abcdef", callsign="SHERIFF12"),
                state_vector(icao24="123456", callsign="UAL100"),
            ]
        },
        fetched_at=now,
    )

    assert count == 3
    assert positions[0]["registration"] == "N951LB"
    assert positions[0]["icao24"] == "ad395a"
    assert candidates == [{"icao24": "abcdef", "callsign": "SHERIFF12"}]


def test_run_once_persists_delayed_position_and_tracker_status(tmp_path):
    now = dt.datetime(2026, 8, 7, 21, 35, tzinfo=dt.timezone.utc)
    observed_at = int((now - dt.timedelta(seconds=90)).timestamp())

    class FakeClient:
        def states(self, _bounds):
            return {"states": [state_vector(observed_at=observed_at)]}, "3988"

    database = tmp_path / "aircraft.sqlite"
    settings = TrackerSettings(database=database)
    result = run_once(settings, client=FakeClient(), now=now)

    assert len(result["positions"]) == 1
    conn = connect_database(database)
    visible = load_visible_aircraft(conn, now=now, delay_seconds=60, max_age_seconds=300)
    tracker = load_tracker_status(conn)
    conn.close()

    assert visible[0]["registration"] == "N951LB"
    assert visible[0]["altitude_ft"] == 4101
    assert visible[0]["speed_kt"] == 107
    assert visible[0]["age_seconds"] == 90
    assert visible[0]["mission_confirmed"] is False
    assert tracker["last_run_success"] == 1
    assert tracker["rate_limit_remaining"] == 3988
    assert tracker["requests_total"] == 1
    assert tracker["errors_total"] == 0
    metrics = prometheus_metrics(database, None, 72.0).decode("utf-8")
    assert "chp_live_map_aircraft_tracker_up 1" in metrics
    assert "chp_live_map_aircraft_tracker_rate_limit_remaining 3988" in metrics
    assert 'chp_live_map_aircraft_tracker_aircraft{kind="matched"} 1' in metrics


def test_visible_aircraft_respects_delay_and_stale_cutoff(tmp_path):
    now = dt.datetime(2026, 8, 7, 21, 35, tzinfo=dt.timezone.utc)

    class FakeClient:
        def __init__(self, observed_at):
            self.observed_at = observed_at

        def states(self, _bounds):
            return {"states": [state_vector(observed_at=int(self.observed_at.timestamp()))]}, "3900"

    database = tmp_path / "aircraft.sqlite"
    settings = TrackerSettings(database=database)
    run_once(settings, client=FakeClient(now - dt.timedelta(seconds=30)), now=now)
    conn = connect_database(database)
    assert load_visible_aircraft(conn, now=now, delay_seconds=60, max_age_seconds=300) == []
    conn.close()

    run_once(settings, client=FakeClient(now - dt.timedelta(seconds=301)), now=now)
    conn = connect_database(database)
    assert load_visible_aircraft(conn, now=now, delay_seconds=60, max_age_seconds=300) == []
    conn.close()
