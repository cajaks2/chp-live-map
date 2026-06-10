import datetime as dt
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import scrape_chp_traffic
from scrape_chp_traffic import (
    DEFAULT_ROAD_KEYWORDS,
    ScraperMetricsHandler,
    build_user_agent,
    connect_database,
    event_key,
    incident_date_for_time,
    insert_observation,
    mark_cleared,
    matching_keywords,
    matching_regions,
    parse_incidents,
    parse_lat_lon,
    parse_lat_lon_from_detail_html,
    parse_page,
    fetch_details,
    should_fetch_details,
    store_scrape_run,
    touch_active_event,
    upsert_active_event,
)
from geo_bounds import clear_coordinates_outside_forest_bounds, coordinates_in_forest_bounds


def test_build_user_agent_optionally_includes_contact_email():
    assert build_user_agent() == "chp-live-map/0.1 (+https://crestmap.us/)"
    assert (
        build_user_agent("ops@example.com")
        == "chp-live-map/0.1 (+https://crestmap.us/; contact: ops@example.com)"
    )


def test_scraper_metrics_handler_serves_health_without_ecs_access_logs(monkeypatch):
    events = []
    monkeypatch.setattr(scrape_chp_traffic, "log_event", lambda *args, **kwargs: events.append((args, kwargs)))

    server = ThreadingHTTPServer(("127.0.0.1", 0), ScraperMetricsHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base_url}/healthz", timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"ok\n"

        with urlopen(f"{base_url}/metrics", timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert "chp_live_map_scraper_up 1" in body
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert events == []


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


def test_parser_keeps_repeated_detail_tables():
    parser = parse_page(
        """
        <table id="tblDetails">
          <tr><th>Time</th><th>No.</th><th>Detail</th></tr>
          <tr><td>3:54 PM</td><td>6</td><td>[41] LACORDS // WILL SEND CREW</td></tr>
        </table>
        <table id="tblDetails">
          <tr><th>Unit Information</th></tr>
          <tr><td>1:15 PM</td><td>13</td><td>Unit At Scene</td></tr>
        </table>
        """
    )

    assert parser.tables["tblDetails"] == [
        ["Time", "No.", "Detail"],
        ["3:54 PM", "6", "[41] LACORDS // WILL SEND CREW"],
        ["Unit Information"],
        ["1:15 PM", "13", "Unit At Scene"],
    ]


def test_fetch_details_preserves_detail_sections(monkeypatch):
    class FakeOpener:
        pass

    parser = parse_page('<input type="hidden" name="__VIEWSTATE" value="abc">')

    def fake_post_form(_opener, _url, _data, _timeout, _user_agent, _retries, _backoff, *_args):
        return """
        <span id="lblIncident">1520</span>
        <span id="lblType">Fatality</span>
        <span id="lblLocation">Angeles Forest Hwy</span>
        <span id="lblLocationDesc">MM15.3</span>
        <span id="lblLatLon">34.342694, -118.110713</span>
        <table id="tblDetails">
          <tr><th>Time</th><th>No.</th><th>Detail</th></tr>
          <tr><td>3:54 PM</td><td>6</td><td>[41] LACORDS // WILL SEND CREW</td></tr>
        </table>
        <table id="tblDetails">
          <tr><th>Unit Information</th></tr>
          <tr><td>1:15 PM</td><td>13</td><td>Unit At Scene</td></tr>
        </table>
        """

    monkeypatch.setattr("scrape_chp_traffic.post_form", fake_post_form)

    details = fetch_details(FakeOpener(), "LACC", parser, 0, 30, "test-agent", 0, 0)

    assert details["detail_entries"] == [
        {
            "section": "Detail Information",
            "time": "3:54 PM",
            "entry_no": "6",
            "text": "[41] LACORDS // WILL SEND CREW",
        },
        {
            "section": "Unit Information",
            "time": "1:15 PM",
            "entry_no": "13",
            "text": "Unit At Scene",
        },
    ]


def test_matching_keywords_checks_location_fields_case_insensitively():
    incident = {
        "type": "Traffic Hazard",
        "location": "Big Tujunga Canyon Rd",
        "location_desc": "",
        "area": "Altadena",
    }

    assert matching_keywords(incident, ["angeles crest", "big tujunga"]) == ["big tujunga"]


def test_default_keywords_do_not_match_bare_sr2_connector():
    incident = {
        "type": "Traffic Hazard",
        "location": "Sr2 N / Sr2 N Sr134 E Con",
        "location_desc": "NB 2 TRANS TO EB 134",
        "area": "Altadena",
    }

    assert matching_keywords(incident, DEFAULT_ROAD_KEYWORDS) == []


def test_default_keywords_match_highway_39_variants():
    incidents = [
        {"type": "Traffic Hazard", "location": "Highway 39 / East Fork Rd", "location_desc": "", "area": ""},
        {"type": "Traffic Hazard", "location": "CA-39 / MM 30.00", "location_desc": "", "area": ""},
        {"type": "Traffic Hazard", "location": "SR39 N / San Gabriel Canyon Rd", "location_desc": "", "area": ""},
    ]

    for incident in incidents:
        assert matching_keywords(incident, DEFAULT_ROAD_KEYWORDS)


def test_default_keywords_scope_highway_39_to_forest_context():
    incidents = [
        {"type": "Traffic Hazard", "location": "Hwy 39 / Arrow Hwy", "location_desc": "", "area": "Azusa"},
        {"type": "Traffic Hazard", "location": "CA-39 / I10", "location_desc": "", "area": "Covina"},
        {"type": "Traffic Hazard", "location": "SR 39 / Foothill Blvd", "location_desc": "", "area": "Glendora"},
    ]

    for incident in incidents:
        assert matching_keywords(incident, DEFAULT_ROAD_KEYWORDS) == []


def test_default_keywords_match_mt_baldy_variants():
    incidents = [
        {"type": "Traffic Hazard", "location": "Mt Baldy Rd / Glendora Ridge Rd", "location_desc": "", "area": ""},
        {"type": "Traffic Hazard", "location": "Mount Baldy Road / San Antonio Canyon", "location_desc": "", "area": ""},
    ]

    for incident in incidents:
        assert matching_keywords(incident, DEFAULT_ROAD_KEYWORDS)


def test_default_keywords_match_mt_wilson_without_red_box():
    incidents = [
        {"type": "Traffic Hazard", "location": "Mt Wilson Rd / Video Rd", "location_desc": "", "area": ""},
        {"type": "Traffic Hazard", "location": "Mount Wilson Road / Red Box Rd", "location_desc": "", "area": ""},
    ]

    for incident in incidents:
        assert matching_keywords(incident, DEFAULT_ROAD_KEYWORDS)


def test_matching_regions_classifies_malibu_roads_separately():
    incident = {
        "type": "Traffic Hazard",
        "location": "Pacific Coast Hwy / Malibu Canyon Rd",
        "location_desc": "",
        "area": "West Valley",
    }

    assert matching_regions(incident) == {
        "malibu": ["pacific coast hwy", "malibu canyon"],
    }


def test_parse_lat_lon_from_span_and_map_link():
    assert parse_lat_lon("34.30123, -118.11789") == (34.30123, -118.11789)
    assert parse_lat_lon_from_detail_html(
        '<a href="https://maps.google.com/?q=34.31111,-118.12222">Map</a>'
    ) == (34.31111, -118.12222)


def test_coordinate_bounds_keep_forest_points_and_reject_city_points():
    assert coordinates_in_forest_bounds(34.260464, -118.190693)
    assert coordinates_in_forest_bounds(34.378926, -117.690678)
    assert not coordinates_in_forest_bounds(34.129, -117.91)


def test_out_of_bounds_coordinates_are_cleared():
    record = {"latitude": 34.129, "longitude": -117.91}

    assert clear_coordinates_outside_forest_bounds(record) == {"latitude": None, "longitude": None}


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
    assert event["region"] == "forest"
    assert event["first_seen"] == observed_at
    assert event["last_seen"] == observed_at

    mark_cleared(conn, event, "2026-05-31T08:05:00-07:00")
    conn.commit()

    event = conn.execute("SELECT * FROM events WHERE event_key = ?", (row["event_key"],)).fetchone()
    observations = conn.execute(
        "SELECT region, status, details_json FROM observations WHERE event_key = ? ORDER BY id",
        (row["event_key"],),
    ).fetchall()
    details = conn.execute(
        "SELECT section, entry_time, entry_no, text FROM detail_entries WHERE event_key = ? ORDER BY id",
        (row["event_key"],),
    ).fetchall()
    assert event["status"] == "cleared"
    assert event["cleared_at"] == "2026-05-31T08:05:00-07:00"
    assert [observation["status"] for observation in observations] == ["active", "cleared"]
    assert observations[0]["region"] == "forest"
    assert json.loads(observations[0]["details_json"]) == row["detail_entries"]
    assert details[0]["section"] == "Detail Information"
    conn.close()


def test_unchanged_active_event_can_skip_detail_refetch_and_still_touch_last_seen(tmp_path):
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
        "type": "Traffic Hazard",
        "location": "Angeles Crest Hwy / Mt Wilson Red Box Rd",
        "location_desc": "",
        "area": "Altadena",
        "latitude": 34.30123,
        "longitude": -118.11789,
        "matched_keywords": "angeles crest",
        "details_hash": "abc123",
        "detail_entries": [{"time": "7:38 AM", "entry_no": "0001", "text": "Incident opened"}],
    }
    upsert_active_event(conn, row)
    conn.commit()

    previous = conn.execute("SELECT * FROM events WHERE event_key = ?", (row["event_key"],)).fetchone()
    incident = {
        "incident_time": "7:36 AM",
        "type": "Traffic Hazard",
        "location": "Angeles Crest Hwy / Mt Wilson Red Box Rd",
        "location_desc": "",
        "area": "Altadena",
    }

    assert not should_fetch_details(
        previous,
        incident,
        dt.datetime.fromisoformat("2026-05-31T08:02:00-07:00"),
        refresh_minutes=3,
    )
    touch_active_event(conn, previous, "2026-05-31T08:05:00-07:00")
    conn.commit()

    touched = conn.execute("SELECT * FROM events WHERE event_key = ?", (row["event_key"],)).fetchone()
    observations = conn.execute("SELECT COUNT(*) AS count FROM observations").fetchone()
    assert touched["last_seen"] == "2026-05-31T08:05:00-07:00"
    assert touched["latest_observed_at"] == "2026-05-31T08:05:00-07:00"
    assert touched["details_fetched_at"] == observed_at
    assert should_fetch_details(
        touched,
        incident,
        dt.datetime.fromisoformat("2026-05-31T08:05:00-07:00"),
        refresh_minutes=3,
    )
    assert observations["count"] == 0
    conn.close()


def test_detail_refetch_happens_for_changed_or_stale_event(tmp_path):
    conn = connect_database(tmp_path / "chp.sqlite")
    row = {
        "event_key": event_key("LACC", "2026-05-31", "0805"),
        "center": "LACC",
        "incident_date": "2026-05-31",
        "incident_no": "0805",
        "observed_at": "2026-05-31T08:00:00-07:00",
        "updated_as_of": "5/31/2026 8:00 AM",
        "incident_time": "7:36 AM",
        "type": "Traffic Hazard",
        "location": "Angeles Crest Hwy",
        "location_desc": "",
        "area": "Altadena",
        "latitude": None,
        "longitude": None,
        "matched_keywords": "angeles crest",
        "details_hash": "abc123",
        "detail_entries": [],
    }
    upsert_active_event(conn, row)
    conn.commit()
    previous = conn.execute("SELECT * FROM events WHERE event_key = ?", (row["event_key"],)).fetchone()

    assert should_fetch_details(
        previous,
        {**row, "location": "Angeles Forest Hwy"},
        dt.datetime.fromisoformat("2026-05-31T08:02:00-07:00"),
        refresh_minutes=3,
    )
    assert should_fetch_details(
        previous,
        row,
        dt.datetime.fromisoformat("2026-05-31T08:03:00-07:00"),
        refresh_minutes=3,
    )
    conn.close()


def test_sqlite_scrape_runs_store_total_seen_and_migrate_existing_table(tmp_path):
    database = tmp_path / "chp.sqlite"
    old_conn = sqlite3.connect(database)
    old_conn.execute(
        """
        CREATE TABLE scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            centers TEXT NOT NULL,
            active_seen INTEGER NOT NULL,
            observations_inserted INTEGER NOT NULL
        )
        """
    )
    old_conn.commit()
    old_conn.close()

    conn = connect_database(database)
    store_scrape_run(
        conn,
        "2026-05-31T08:00:00-07:00",
        ["LACC"],
        total_seen=12,
        active_seen=2,
        observations_inserted=1,
        active_with_coords=1,
        details_requested=2,
        details_skipped=3,
        duration_seconds=1.25,
        http_status_counts={"GET:list:200": 1, "POST:detail:200": 2},
    )
    conn.commit()

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(scrape_runs)")}
    run = conn.execute("SELECT * FROM scrape_runs").fetchone()
    assert "total_seen" in columns
    assert "http_status_counts" in columns
    assert run["total_seen"] == 12
    assert run["active_seen"] == 2
    assert run["observations_inserted"] == 1
    assert run["active_with_coords"] == 1
    assert run["details_requested"] == 2
    assert run["details_skipped"] == 3
    assert run["duration_seconds"] == 1.25
    assert json.loads(run["http_status_counts"]) == {"GET:list:200": 1, "POST:detail:200": 2}
    conn.close()


def test_detail_entry_sections_are_backfilled_from_observation_json(tmp_path):
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
        "type": "Traffic Hazard",
        "location": "Angeles Crest Hwy",
        "location_desc": "",
        "area": "Altadena",
        "latitude": None,
        "longitude": None,
        "matched_keywords": "angeles crest",
        "details_hash": "abc123",
        "detail_entries": [
            {
                "section": "Detail Information",
                "time": "7:38 AM",
                "entry_no": "2",
                "text": "Incident opened",
            },
            {
                "section": "Unit Information",
                "time": "7:39 AM",
                "entry_no": "1",
                "text": "Unit Assigned",
            },
        ],
    }
    upsert_active_event(conn, row)
    insert_observation(conn, row, "active")
    conn.execute("UPDATE detail_entries SET section = NULL")
    conn.commit()
    conn.close()

    conn = connect_database(tmp_path / "chp.sqlite")
    details = conn.execute(
        "SELECT section FROM detail_entries WHERE event_key = ? ORDER BY entry_index",
        (row["event_key"],),
    ).fetchall()
    assert [detail["section"] for detail in details] == ["Detail Information", "Unit Information"]
    conn.close()


def test_existing_detail_entries_table_adds_section_column(tmp_path):
    database = tmp_path / "chp.sqlite"
    old_conn = sqlite3.connect(database)
    old_conn.execute(
        """
        CREATE TABLE detail_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            entry_index INTEGER NOT NULL,
            entry_time TEXT,
            entry_no TEXT,
            text TEXT
        )
        """
    )
    old_conn.commit()
    old_conn.close()

    conn = connect_database(database)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(detail_entries)")}
    assert "section" in columns
    conn.close()
