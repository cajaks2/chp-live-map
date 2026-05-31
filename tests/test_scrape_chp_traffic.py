import datetime as dt
import json

from scrape_chp_traffic import (
    connect_database,
    event_key,
    incident_date_for_time,
    insert_observation,
    mark_cleared,
    matching_keywords,
    parse_incidents,
    parse_lat_lon,
    parse_lat_lon_from_detail_html,
    parse_page,
    upsert_active_event,
)


def test_parse_incidents_from_cad_table():
    parser = parse_page(
        """
        <table id="gvIncidents">
          <tr><th>No.</th><th>Time</th><th>Type</th><th>Location</th><th>Location Desc.</th><th>Area</th></tr>
          <tr><td>0805</td><td>7:36 AM</td><td>Trfc Collision-Unkn Inj</td><td>SR14 N / Angeles Forest Hwy</td><td>Angeles Forest</td><td>Antelope Valley</td></tr>
        </table>
        """
    )

    assert parse_incidents("LACC", parser) == [
        {
            "center": "LACC",
            "select_index": 0,
            "incident_no": "0805",
            "incident_time": "7:36 AM",
            "type": "Trfc Collision-Unkn Inj",
            "location": "SR14 N / Angeles Forest Hwy",
            "location_desc": "Angeles Forest",
            "area": "Antelope Valley",
        }
    ]


def test_matching_keywords_checks_location_fields_case_insensitively():
    incident = {
        "type": "Traffic Hazard",
        "location": "Big Tujunga Canyon Rd",
        "location_desc": "",
        "area": "Altadena",
    }

    assert matching_keywords(incident, ["angeles crest", "big tujunga"]) == ["big tujunga"]


def test_parse_lat_lon_from_span_and_map_link():
    assert parse_lat_lon("34.30123, -118.11789") == (34.30123, -118.11789)
    assert parse_lat_lon_from_detail_html(
        '<a href="https://maps.google.com/?q=34.31111,-118.12222">Map</a>'
    ) == (34.31111, -118.12222)


def test_incident_date_rolls_back_after_midnight():
    updated_at = dt.datetime(2026, 5, 31, 0, 3)

    assert incident_date_for_time(updated_at, "11:59 PM") == "2026-05-30"
    assert incident_date_for_time(updated_at, "12:01 AM") == "2026-05-31"


def test_sqlite_event_lifecycle_records_active_and_cleared_observations(tmp_path):
    conn = connect_database(tmp_path / "chp.sqlite")
    observed_at = "2026-05-31T08:00:00-07:00"
    row = {
        "event_key": event_key("LACC", "2026-05-31", "0805"),
        "center": "LACC",
        "incident_date": "2026-05-31",
        "incident_no": "0805",
        "observed_at": observed_at,
        "updated_as_of": "5/31/2026 8:00 AM",
        "incident_time": "7:36 AM",
        "type": "Trfc Collision-Unkn Inj",
        "location": "SR14 N / Sierra Hwy Ofr",
        "location_desc": "Angeles Forest Hwy",
        "area": "Antelope Valley",
        "latitude": 34.30123,
        "longitude": -118.11789,
        "matched_keywords": "angeles forest",
        "details_hash": "abc123",
        "detail_entries": [{"time": "7:38 AM", "entry_no": "0001", "text": "Incident opened"}],
    }

    previous = upsert_active_event(conn, row)
    insert_observation(conn, row, "active")
    conn.commit()

    assert previous is None
    event = conn.execute("SELECT * FROM events WHERE event_key = ?", (row["event_key"],)).fetchone()
    assert event["status"] == "active"
    assert event["first_seen"] == observed_at
    assert event["last_seen"] == observed_at

    mark_cleared(conn, event, "2026-05-31T08:05:00-07:00")
    conn.commit()

    event = conn.execute("SELECT * FROM events WHERE event_key = ?", (row["event_key"],)).fetchone()
    observations = conn.execute(
        "SELECT status, details_json FROM observations WHERE event_key = ? ORDER BY id",
        (row["event_key"],),
    ).fetchall()
    assert event["status"] == "cleared"
    assert event["cleared_at"] == "2026-05-31T08:05:00-07:00"
    assert [observation["status"] for observation in observations] == ["active", "cleared"]
    assert json.loads(observations[0]["details_json"]) == row["detail_entries"]
    conn.close()
