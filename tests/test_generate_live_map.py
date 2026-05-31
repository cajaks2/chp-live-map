import datetime as dt
import json

from generate_live_map import build_html, load_incidents
from scrape_chp_traffic import connect_database, insert_observation, upsert_active_event


def incident_row(event_key, status, latest_observed_at, incident_no):
    return {
        "event_key": event_key,
        "center": "LACC",
        "incident_date": "2026-05-31",
        "incident_no": incident_no,
        "observed_at": latest_observed_at,
        "updated_as_of": "5/31/2026 8:00 AM",
        "incident_time": "7:36 AM",
        "type": "Traffic Hazard" if status == "active" else "Disabled Vehicle",
        "location": "Angeles Forest Hwy",
        "location_desc": "Mile marker 12",
        "area": "Antelope Valley",
        "latitude": 34.31,
        "longitude": -118.12,
        "matched_keywords": "angeles forest",
        "details_hash": f"hash-{incident_no}",
        "detail_entries": [{"time": "7:37 AM", "entry_no": "0001", "text": f"{status} detail"}],
    }


def test_load_incidents_returns_active_first_with_detail_entries(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    active = incident_row("LACC|2026-05-31|0805", "active", dt.datetime.now().astimezone().isoformat(timespec="seconds"), "0805")
    cleared = incident_row("LACC|2026-05-31|0801", "cleared", dt.datetime.now().astimezone().isoformat(timespec="seconds"), "0801")

    upsert_active_event(conn, cleared)
    insert_observation(conn, cleared, "active")
    conn.execute(
        "UPDATE events SET status = 'cleared', cleared_at = ?, latest_observed_at = ? WHERE event_key = ?",
        (cleared["observed_at"], cleared["observed_at"], cleared["event_key"]),
    )
    upsert_active_event(conn, active)
    insert_observation(conn, active, "active")
    conn.commit()
    conn.close()

    incidents = load_incidents(database, 72)

    assert [incident["event_key"] for incident in incidents] == [
        "LACC|2026-05-31|0805",
        "LACC|2026-05-31|0801",
    ]
    assert incidents[0]["status"] == "active"
    assert incidents[0]["detail_entries"] == active["detail_entries"]
    assert incidents[1]["status"] == "cleared"


def test_build_html_embeds_counts_and_escaped_incident_data():
    incidents = [
        {
            "event_key": "LACC|2026-05-31|0805",
            "incident_no": "0805",
            "incident_time": "7:36 AM",
            "type": "Traffic <Hazard>",
            "location": "Angeles Forest Hwy",
            "location_desc": "Mile marker 12",
            "area": "Antelope Valley",
            "status": "active",
            "first_seen": "2026-05-31T08:00:00-07:00",
            "last_seen": "2026-05-31T08:00:00-07:00",
            "cleared_at": None,
            "latitude": 34.31,
            "longitude": -118.12,
            "detail_entries": [{"time": "7:37 AM", "entry_no": "0001", "text": "Tow requested"}],
        },
        {
            "event_key": "LACC|2026-05-31|0801",
            "incident_no": "0801",
            "incident_time": "7:10 AM",
            "type": "Disabled Vehicle",
            "location": "Big Tujunga Canyon Rd",
            "location_desc": "",
            "area": "Altadena",
            "status": "cleared",
            "first_seen": "2026-05-31T07:15:00-07:00",
            "last_seen": "2026-05-31T07:25:00-07:00",
            "cleared_at": "2026-05-31T07:25:00-07:00",
            "latitude": None,
            "longitude": None,
            "detail_entries": [],
        },
    ]

    html = build_html(incidents, "2026-05-31T08:05:00-07:00", 72)

    assert "CHP Forest Incidents (1 active, 2 total)" in html
    assert "1 active · 2 in last 72h · 1 mapped" in html
    assert "Traffic <Hazard>" in html
    assert "function escapeHtml" in html
    assert "no map pin" in html
    assert "window.chpLiveMap" in html
    assert ".setView([34.32, -118.12], 10)" in html
    assert "map.fitBounds" not in html
    assert json.dumps(incidents, ensure_ascii=False) in html
