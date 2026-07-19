import datetime as dt
import json

from generate_live_map import (
    build_about_html,
    build_history_html,
    build_html,
    build_summary_html,
    include_linked_incident,
    incident_status,
    load_incident_by_key,
    load_incidents,
)
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


def test_load_incident_by_key_finds_incident_outside_window(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    old_seen = (dt.datetime.now().astimezone() - dt.timedelta(days=45)).isoformat(timespec="seconds")
    old = incident_row("LACC|2026-05-31|1883", "cleared", old_seen, "1883")

    upsert_active_event(conn, old)
    insert_observation(conn, old, "active")
    conn.execute(
        """
        UPDATE events
        SET status = 'cleared',
            first_seen = ?,
            last_seen = ?,
            cleared_at = ?,
            latest_observed_at = ?
        WHERE event_key = ?
        """,
        (old_seen, old_seen, old_seen, old_seen, old["event_key"]),
    )
    conn.commit()
    conn.close()

    assert load_incidents(database, 72) == []
    linked = load_incident_by_key(database, old["event_key"])
    incidents = include_linked_incident([], linked)

    assert linked["event_key"] == old["event_key"]
    assert linked["detail_entries"] == old["detail_entries"]
    assert incidents[0]["_linked_outside_window"] is True
    assert incident_status(incidents, 72)["total_count"] == 0


def test_build_html_does_not_count_linked_incident_in_window_total():
    current = incident_row("LACC|2026-05-31|0805", "cleared", "2026-05-31T08:00:00-07:00", "0805")
    linked = incident_row("LACC|2026-05-30|1883", "cleared", "2026-05-30T08:00:00-07:00", "1883")
    linked["_linked_outside_window"] = True

    html = build_html([linked, current], "2026-05-31T08:05:00-07:00", 72)

    assert "0 active · 1 in last 72h · 1 mapped" in html
    assert "Linked" in html
    assert "Back to ${escapeHtml(formatRangeLabel(currentDataStatus.hours))}" in html


def test_load_incidents_clears_out_of_bounds_coordinates(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    row = incident_row(
        "LACC|2026-05-31|0805",
        "active",
        dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "0805",
    )
    row["latitude"] = 34.129
    row["longitude"] = -117.91

    upsert_active_event(conn, row)
    insert_observation(conn, row, "active")
    conn.commit()
    conn.close()

    incidents = load_incidents(database, 72)

    assert incidents[0]["latitude"] is None
    assert incidents[0]["longitude"] is None


def test_load_incidents_filters_to_forest_region_by_default(tmp_path):
    database = tmp_path / "chp.sqlite"
    conn = connect_database(database)
    forest = incident_row(
        "LACC|2026-05-31|0805",
        "active",
        dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "0805",
    )
    malibu = incident_row(
        "LACC|2026-05-31|0806",
        "active",
        dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "0806",
    )
    malibu.update(
        {
            "region": "malibu",
            "location": "Pacific Coast Hwy / Malibu Canyon Rd",
            "latitude": 34.035,
            "longitude": -118.68,
            "matched_keywords": "pacific coast hwy;malibu canyon",
        }
    )

    upsert_active_event(conn, forest)
    insert_observation(conn, forest, "active")
    upsert_active_event(conn, malibu)
    insert_observation(conn, malibu, "active")
    conn.commit()
    conn.close()

    forest_incidents = load_incidents(database, 72)
    malibu_incidents = load_incidents(database, 72, region="malibu")
    malicious_region_incidents = load_incidents(database, 72, region="malibu'; DROP TABLE events; --")

    assert [incident["event_key"] for incident in forest_incidents] == [forest["event_key"]]
    assert [incident["event_key"] for incident in malibu_incidents] == [malibu["event_key"]]
    assert [incident["event_key"] for incident in malicious_region_incidents] == [forest["event_key"]]
    assert malibu_incidents[0]["latitude"] == 34.035
    assert malibu_incidents[0]["longitude"] == -118.68


def test_summary_uses_malibu_road_buckets_for_malibu_region():
    incidents = [
        {
            **incident_row("LACC|2026-05-31|0805", "cleared", "2026-05-31T08:00:00-07:00", "0805"),
            "region": "malibu",
            "type": "Trfc Collision-No Inj",
            "location": "Pacific Coast Hwy / Malibu Canyon Rd",
            "location_desc": "",
            "area": "West Valley",
            "latitude": 34.035,
            "longitude": -118.68,
        },
        {
            **incident_row("LACC|2026-05-31|0806", "cleared", "2026-05-31T08:00:00-07:00", "0806"),
            "region": "malibu",
            "type": "Trfc Collision-Unkn Inj",
            "location": "Topanga Canyon Blvd / Piuma Rd",
            "location_desc": "",
            "area": "West Valley",
            "latitude": 34.09,
            "longitude": -118.62,
        },
    ]

    summary_html = build_summary_html(
        incidents,
        "2026-05-31T08:05:00-07:00",
        72,
        region="malibu",
        filters={"type": "family:collision"},
    )

    assert "CHP Malibu Incidents" in summary_html
    assert "Pacific Coast Hwy" in summary_html
    assert "Topanga Canyon" in summary_html
    assert "Other forest roads" not in summary_html


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
            "detail_entries": [
                {
                    "section": "Detail Information",
                    "time": "7:37 AM",
                    "entry_no": "0001",
                    "text": "Tow requested",
                },
                {
                    "section": "Unit Information",
                    "time": "7:39 AM",
                    "entry_no": "0002",
                    "text": "Unit Assigned",
                },
            ],
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

    html = build_html(
        incidents,
        "2026-05-31T08:05:00-07:00",
        72,
        base_path="/",
        public_url="https://crestmap.us/",
        region_statuses={
            "forest": {"active_count": 1},
            "malibu": {"active_count": 0},
        },
    )

    assert "CHP Forest Incidents (1 active, 2 total)" in html
    assert 'http-equiv="Cache-Control"' not in html
    assert '<meta name="description" content="Live and historical CHP CAD traffic incidents' in html
    assert '<meta property="og:description" content="Live and historical CHP CAD traffic incidents' in html
    assert '<meta name="robots" content="index,follow,max-image-preview:large">' in html
    assert '<link rel="canonical" href="https://crestmap.us/">' in html
    assert '<link rel="icon" href="https://crestmap.us/favicon.svg?active=1&amp;v=' in html
    assert '<meta property="og:title" content="CHP Forest Incidents (1 active, 2 total)">' in html
    assert '<meta property="og:image" content="https://crestmap.us/og-image.png">' in html
    assert '<meta property="og:image:type" content="image/png">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert '<script type="application/ld+json">' in html
    assert "googletagmanager.com/gtag/js" not in html
    assert '"@type": "WebApplication"' in html
    assert '"applicationCategory": "MapApplication"' in html
    assert '"@type": "Dataset"' in html
    assert "CHP forest road incident history" in html
    assert "scrollbar-width: thin" in html
    assert "view-menu" in html
    assert 'href="/summary?hours=72&amp;region=forest"' in html
    assert 'href="/history?hours=72&amp;region=forest"' in html
    assert 'id="incident-list-shell"' in html
    assert "flex-basis: clamp(150px, 23svh, 200px)" in html
    assert "min-height: 150px" in html
    assert 'id="scroll-incidents"' in html
    assert "has-more-below #scroll-incidents" in html
    assert "function updateListScrollCue" in html
    assert "function scrollIncidentListDown" in html
    assert 'id="details-cue"' in html
    assert "Incident details below" in html
    assert "data-default-view" in html
    assert "linked-pill" in html
    assert "This linked incident is outside the selected" in html
    assert "height: 45svh" in html
    assert "bottom: var(--details-cue-bottom, max(26px, calc(env(safe-area-inset-bottom) + 10px)))" in html
    assert "function updateDetailsCuePosition" in html
    assert "window.visualViewport.addEventListener" in html
    assert "selected-pill" in html
    assert '<span class="selected-pill">Open</span>' in html
    assert "background: #d4e6d5" in html
    assert "data-share-incident" in html
    assert "Copy link" in html
    assert "navigator.clipboard.writeText" in html
    assert "function incidentUrl" in html
    assert 'scrollIncidentsButton?.addEventListener("click", scrollIncidentListDown)' in html
    assert 'list.addEventListener("scroll", updateListScrollCue' in html
    assert "mask-image: linear-gradient(to bottom" in html
    assert "overscroll-behavior: contain" in html
    assert "background: #fbfcf8" in html
    assert "border-bottom: 1px solid #e2e6de" in html
    assert "align-items: center" in html
    assert "justify-content: center" in html
    assert '<details id="about-panel" class="about-panel" open>' not in html
    assert "<summary>About this map</summary>" not in html
    assert 'window.localStorage.getItem("chp-about-panel")' not in html
    assert 'window.localStorage.setItem("chp-about-panel"' not in html
    assert '<nav class="range-tabs" aria-label="History range">' in html
    assert '<nav class="view-tabs" aria-label="View navigation">' in html
    assert '<nav class="region-tabs" aria-label="Region">' in html
    assert '<a class="region-tab is-active" href="/?hours=72&amp;region=forest" aria-current="page"><span>Forest</span><span class="region-active-count" aria-label="1 active incident">1</span></a>' in html
    assert '<a class="region-tab" href="/?hours=72&amp;region=malibu"><span>Malibu</span><span class="region-active-count" aria-label="0 active incidents">0</span></a>' in html
    assert "region-active-count" in html
    assert '<a class="view-tab is-active" href="/?hours=72&amp;region=forest" aria-current="page">Map</a>' in html
    assert '<a class="view-tab" href="/summary?hours=72&amp;region=forest">Summary</a>' in html
    assert '<a class="view-tab" href="/history?hours=72&amp;region=forest">History</a>' in html
    assert '<a class="view-tab" href="/about?hours=72&amp;region=forest">About</a>' in html
    assert '<a class="range-tab is-active" href="?hours=72&amp;region=forest" aria-current="page">72h</a>' in html
    assert '<a class="range-tab" href="?hours=720&amp;region=forest">30d</a>' in html
    assert "1 active · 2 in last 72h · 1 mapped" in html
    assert 'View last updated <time id="generated-at" datetime="2026-05-31T08:05:00-07:00">' in html
    assert "const initialDataStatus" in html
    assert '"region": "forest"' in html
    assert 'const statusEndpoint = "/status.json"' in html
    assert 'const incidentsEndpoint = "/incidents.json"' in html
    assert 'const currentRegion = "forest"' in html
    assert "let incidents = []" in html
    assert "fetchIncidentData()" in html
    assert 'url.searchParams.set("v", version)' in html
    assert "window.location.reload" not in html
    assert "Traffic <Hazard>" not in html
    assert "Traffic \\u003cHazard" not in html
    assert "function formatGeneratedAt" in html
    assert 'element.getAttribute("datetime")' in html
    assert "function setLastScrape" in html
    assert "function formatIncidentWhen" in html
    assert 'new URLSearchParams(window.location.search).get("incident")' in html
    assert 'url.searchParams.set("incident", incident.event_key)' in html
    assert 'url.searchParams.set("region", currentRegion)' in html
    assert "function ensureCurrentRegionUrl" in html
    assert "ensureCurrentRegionUrl();" in html
    assert "function updateIncidentUrl" in html
    assert "const linkedIncident = incidentFromUrl();" in html
    assert "revealList: Boolean(linkedIncident)" in html
    assert "updateUrl: options.updateUrl !== false" in html
    assert "${escapeHtml(formatIncidentWhen(incident))}" in html
    assert "Detail Information" in html
    assert "Unit Information" in html
    assert "detail-subsection" in html
    assert 'id="stale-notice"' in html
    assert 'id="stale-notice-text"' in html
    assert 'id="dismiss-stale-notice"' in html
    assert 'id="auto-refresh-enabled"' in html
    assert "Auto refresh" in html
    assert "refresh-options" not in html
    assert "chp-auto-refresh" in html
    assert "autoRefreshToggle.checked" in html
    assert "autoRefreshToggle.addEventListener" in html

    analytics_html = build_html(
        incidents,
        "2026-05-31T08:05:00-07:00",
        72,
        google_analytics_id="G-TEST123",
    )
    assert "Google tag (gtag.js)" in analytics_html
    assert "https://www.googletagmanager.com/gtag/js?id=G-TEST123" in analytics_html
    assert 'gtag(\'config\', "G-TEST123");' in analytics_html

    malibu_html = build_html(
        incidents,
        "2026-05-31T08:05:00-07:00",
        72,
        region="malibu",
    )
    assert "CHP Malibu Incidents" in malibu_html
    assert 'href="/summary?hours=72&amp;region=malibu"' in malibu_html
    assert 'const currentRegion = "malibu"' in malibu_html
    assert ".setView([34.09, -118.78], 10)" in malibu_html
    assert ".setView([34.32, -118.12], 10)" not in malibu_html
    assert "Automatically reload when new incident data is available" in html
    assert "let dismissed = false" in html
    assert "async () =>" in html
    assert "fetch(url" in html
    assert "latest.version !== currentDataStatus.version" in html
    assert "await fetchIncidentData" in html
    assert "New incident data is available." in html
    assert "Background status checks are not confirming current data." in html
    assert "function setCheckedAt" in html
    assert 'dismissButton.addEventListener("click"' in html
    assert "function setupStaleRefresh" in html
    assert "healthAgeMs > 180000" in html
    assert "function focusedCommentFormFor" in html
    assert "preserveFocusedComment: true" in html
    assert "detailsPanel.dataset.selectedIncidentKey === incident.event_key" in html
    assert "function escapeHtml" in html
    assert "no map pin" in html
    assert "window.chpLiveMap" in html
    assert "touch-action: none" in html
    assert "-webkit-tap-highlight-color: transparent" in html
    assert "@media (max-width: 760px)" in html
    assert "padding: 8px 12px 8px;" in html
    assert ".view-menu {\n        display: block;" in html
    assert ".view-tabs {\n        display: none;" in html
    assert "flex-basis: clamp(150px, 23svh, 200px);" in html
    assert "height: 45svh;" in html
    assert "tap: true" in html
    assert "touchZoom: true" in html
    assert "doubleClickZoom: true" in html
    assert "keyboard: false" in html
    assert "preferCanvas: false" in html
    assert "markerZoomAnimation: true" in html
    assert "updateWhenZooming: true" in html
    assert "function markerIcon" in html
    assert "const size = 22;" in html
    assert "const size = selected ? 28 : 22" not in html
    assert "position: absolute;" in html
    assert "incident-marker-dot" in html
    assert ".incident-marker.is-selected .incident-marker-dot::before" in html
    assert ".incident-marker.is-pulsing .incident-marker-dot::after" in html
    assert "@keyframes selected-marker-pulse" in html
    assert "selected ? \"is-selected\" : \"\"" in html
    assert "pulsing ? \"is-pulsing\" : \"\"" in html
    assert "selected && options.pulse" in html
    assert "function bindMarkerInteraction" in html
    assert 'L.DomEvent.on(element, "touchend", selectFromMarker)' in html
    assert 'L.DomEvent.on(element, "pointerup", selectFromMarker)' in html
    assert 'selectIncident(incident, { pan: false, revealDetails: true, pulse: true });' in html
    assert "L.marker([incident.latitude, incident.longitude]" in html
    assert "L.circleMarker" not in html
    assert "function setupDoubleTapZoom" in html
    assert "setupDoubleTapZoom();" in html

    summary_html = build_summary_html(incidents, "2026-05-31T08:05:00-07:00", 72)
    assert "Summary - CHP Forest Incidents" in summary_html
    assert "Busiest Roads" in summary_html
    assert "Incident Types" in summary_html
    assert "Incidents by Day" in summary_html
    assert "Time of Day" in summary_html
    assert "Fri, May 29: 0" in summary_html
    assert "Sat, May 30: 0" in summary_html
    assert "Sun, May 31" in summary_html
    assert 'class="bar-column is-zero"' in summary_html
    assert "Morning" in summary_html
    assert "2</strong><span>Incidents in window" in summary_html
    assert 'class="bar-chart"' in summary_html
    assert 'class="bar-chart bar-chart-compact"' in summary_html
    assert 'class="bar-column"' in summary_html
    assert "bar-row" not in summary_html
    assert '<select class="filter" name="type" aria-label="Incident type filter">' in summary_html
    assert '<option value="family:collision">Traffic collisions / accidents</option>' in summary_html
    assert '<nav class="range-tabs" aria-label="History range">' in summary_html
    assert '<a class="range-tab is-active" href="?hours=72&amp;region=forest" aria-current="page">72h</a>' in summary_html
    assert 'class="view-tab is-active" href="/summary?hours=72&amp;region=forest" aria-current="page">Summary</a>' in summary_html

    collision_incidents = [
        {**incidents[0], "type": "Trfc Collision-Unkn Inj", "event_key": "LACC|2026-05-31|0810"},
        {**incidents[1], "type": "Traffic Hazard", "event_key": "LACC|2026-05-31|0811"},
    ]
    filtered_summary_html = build_summary_html(
        collision_incidents,
        "2026-05-31T08:05:00-07:00",
        72,
        filters={"type": "family:collision"},
    )
    assert "1 of 2 incidents shown" in filtered_summary_html
    assert '<option value="family:collision" selected>Traffic collisions / accidents</option>' in filtered_summary_html
    assert "Trfc Collision-Unkn Inj" in filtered_summary_html
    assert "<strong>Traffic Hazard</strong>" not in filtered_summary_html
    assert '<a class="range-tab is-active" href="?hours=72&amp;region=forest&amp;type=family%3Acollision" aria-current="page">72h</a>' in filtered_summary_html

    history_html = build_history_html(incidents, "2026-05-31T08:05:00-07:00", 72)
    assert "History - CHP Forest Incidents" in history_html
    assert "Search road, type, incident number" in history_html
    assert "2 of 2 results" in history_html
    assert '<select class="filter" name="road" aria-label="Road filter">' in history_html
    assert '<select class="filter" name="type" aria-label="Incident type filter">' in history_html
    assert '<select class="filter" name="status" aria-label="Status filter">' in history_html
    assert '<select class="filter" name="mapped" aria-label="Map pin filter">' in history_html
    assert "Apply filters" in history_html
    assert "Show on map" in history_html
    assert 'href="/?hours=72&amp;region=forest&amp;incident=LACC%7C2026-05-31%7C0805">Show on map</a>' in history_html
    assert '<nav class="range-tabs" aria-label="History range">' in history_html
    assert '<input type="hidden" name="region" value="forest">' in history_html
    assert '<a class="range-tab is-active" href="?hours=72&amp;region=forest" aria-current="page">72h</a>' in history_html
    assert 'class="view-tab is-active" href="/history?hours=72&amp;region=forest" aria-current="page">History</a>' in history_html

    filtered_history_html = build_history_html(
        incidents,
        "2026-05-31T08:05:00-07:00",
        72,
        filters={"status": "active", "mapped": "mapped"},
    )
    assert "1 of 2 results" in filtered_history_html
    assert "Traffic &lt;Hazard&gt;" in filtered_history_html
    assert "<strong>Disabled Vehicle</strong>" not in filtered_history_html
    assert '<option value="active" selected>Active</option>' in filtered_history_html
    assert '<option value="mapped" selected>Mapped only</option>' in filtered_history_html

    about_html = build_about_html(incidents, "2026-05-31T08:05:00-07:00", 72)
    assert "About - CHP Forest Incidents" in about_html
    assert "What This Is" in about_html
    assert "Update Cadence" in about_html
    assert "CHP CAD source" in about_html
    assert '<a class="range-tab is-active" href="?hours=72&amp;region=forest" aria-current="page">72h</a>' in about_html
    assert 'class="view-tab is-active" href="/about?hours=72&amp;region=forest" aria-current="page">About</a>' in about_html
    assert "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" in html
    assert "basemaps.cartocdn.com/light_all" not in html
    assert ".setView([34.32, -118.12], 10)" in html
    assert "map.fitBounds" not in html
    assert json.dumps(incidents, ensure_ascii=False) not in html


def test_incident_status_ignores_observation_timestamp_for_version():
    first = [
        {
            **incident_row("LACC|2026-05-31|0805", "active", "2026-05-31T08:00:00-07:00", "0805"),
            "status": "active",
            "latest_observed_at": "2026-05-31T08:00:00-07:00",
        }
    ]
    second = [dict(first[0], latest_observed_at="2026-05-31T08:01:00-07:00")]

    first_status = incident_status(first, 72)
    second_status = incident_status(second, 72)

    assert first_status["active_count"] == 1
    assert first_status["total_count"] == 1
    assert first_status["mapped_count"] == 1
    assert first_status["data_updated_at"] == "2026-05-31T08:00:00-07:00"
    assert first_status["version"] == second_status["version"]


def test_incident_status_changes_when_incident_content_changes():
    first = [
        {
            **incident_row("LACC|2026-05-31|0805", "active", "2026-05-31T08:00:00-07:00", "0805"),
            "status": "active",
            "latest_observed_at": "2026-05-31T08:00:00-07:00",
        }
    ]
    second = [dict(first[0], details_hash="new-detail-hash")]

    assert incident_status(first, 72)["version"] != incident_status(second, 72)["version"]
