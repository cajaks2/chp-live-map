import datetime as dt
import json

from generate_live_map import (
    build_about_html,
    build_history_html,
    build_html,
    build_summary_html,
    include_linked_incident,
    incident_location_lines,
    incident_status,
    load_incident_by_key,
    load_incidents,
)
from scrape_chp_traffic import (
    connect_database,
    insert_observation,
    mark_cleared,
    upsert_active_event,
)


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
    now = dt.datetime.now().astimezone()
    active = incident_row("LACC|2026-05-31|0805", "active", now.isoformat(timespec="seconds"), "0805")
    cleared = incident_row(
        "LACC|2026-05-31|0801",
        "cleared",
        (now - dt.timedelta(minutes=5)).isoformat(timespec="seconds"),
        "0801",
    )

    upsert_active_event(conn, cleared)
    insert_observation(conn, cleared, "active")
    mark_cleared(conn, cleared, now.isoformat(timespec="seconds"))
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
    assert incidents[1]["detail_entries"] == cleared["detail_entries"]
    assert load_incident_by_key(database, cleared["event_key"])["detail_entries"] == cleared["detail_entries"]


def test_build_html_labels_wildweb_report_without_claiming_it_is_active_or_cleared(tmp_path):
    database = tmp_path / "wildweb.sqlite"
    conn = connect_database(database)
    row = incident_row(
        "wildweb|CAANCC|report-1",
        "reported",
        dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "CAANF-3487",
    )
    row.update(
        {
            "center": "CAANCC",
            "source": "wildweb",
            "source_event_id": "report-1",
            "source_status": "listed",
            "source_url": "https://www.wildwebe.net/incidents?dc_Name=CAANCC",
            "source_reported_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "reported",
            "latitude": None,
            "longitude": None,
            "coordinate_confidence": "missing",
        }
    )
    upsert_active_event(conn, row)
    insert_observation(conn, row, "reported")
    conn.commit()
    conn.close()

    incidents = load_incidents(database, 72)
    html = build_html(incidents, dt.datetime.now().astimezone().isoformat(timespec="seconds"), 72, app_version="test")

    assert incidents[0]["status"] == "reported"
    assert incidents[0]["source"] == "wildweb"
    assert "1 WildWeb reported" in html
    assert '<span class="region-active-count" aria-label="0 active incidents">0</span>' in html
    assert "const reportedCount = Number(regionStatuses[region].reported_count || 0);" not in html
    assert '<span class="source-pill">${escapeHtml(sourceText)}</span>' in html
    assert 'listed: "Reported"' in html
    assert "No coordinates exposed by ${escapeHtml(sourceText)}" in html
    assert "function incidentDescription(incident)" in html
    assert "function incidentLocationLines(incident)" in html
    assert '<span class="incident-location-primary">${escapeHtml(locationLines.primary)}</span>' in html
    assert '<div class="detail-location-primary">${escapeHtml(locationLines.primary)}</div>' in html
    assert "<dt>Loc Desc</dt>" not in html
    assert "function wildWebReportedVisualAge(incident)" in html
    assert 'source !== "wildweb" || status !== "reported"' in html
    assert 'incident.source_reported_at || incident.first_seen' in html
    assert "(ageHours - 1) / 5" in html
    assert "opacity: 1 - (0.45 * fadeProgress)" in html
    assert "saturation: 1 - (0.88 * fadeProgress)" in html
    assert 'visualAge ? "incident is-wildweb-aging" : "incident"' in html
    assert '.incident.is-wildweb-aging:not([aria-current="true"])' in html
    assert '.incident.is-wildweb-aging[aria-current="true"] > *' in html
    assert ".incident-marker.is-wildweb-aging .incident-marker-core" in html


def test_incident_location_lines_prioritize_roads_for_chp_and_descriptions_for_wildweb():
    chp = {
        "source": "chp",
        "type": "Traffic Hazard",
        "location": "I210 W / Angeles Crest Hwy",
        "location_desc": "OFR",
    }
    wildweb = {
        "source": "wildweb",
        "type": "Miscellaneous",
        "location": "Angeles Forest Hwy MM 16.09",
        "location_desc": "PUBLIC ASSIST",
    }

    assert incident_location_lines(chp) == ("I210 W / Angeles Crest Hwy", "OFR")
    assert incident_location_lines(wildweb) == ("PUBLIC ASSIST", "Angeles Forest Hwy MM 16.09")


def test_load_incidents_sorts_wildweb_by_source_report_time_not_poll_time(tmp_path):
    database = tmp_path / "recency.sqlite"
    conn = connect_database(database)
    now = dt.datetime.now().astimezone()

    old_active = incident_row(
        "LACC|old-active",
        "active",
        (now - dt.timedelta(hours=36)).isoformat(timespec="seconds"),
        "1000",
    )
    recent_cleared = incident_row(
        "LACC|recent-cleared",
        "cleared",
        (now - dt.timedelta(hours=1)).isoformat(timespec="seconds"),
        "1001",
    )
    polled_wildweb = incident_row(
        "wildweb|CAANCC|older-report",
        "reported",
        now.isoformat(timespec="seconds"),
        "CAANF-1002",
    )
    polled_wildweb.update(
        {
            "center": "CAANCC",
            "source": "wildweb",
            "source_event_id": "older-report",
            "source_status": "listed",
            "source_reported_at": (now - dt.timedelta(hours=24)).isoformat(timespec="seconds"),
            "status": "reported",
        }
    )

    for row, status in (
        (old_active, "active"),
        (recent_cleared, "active"),
        (polled_wildweb, "reported"),
    ):
        upsert_active_event(conn, row)
        insert_observation(conn, row, status)
    conn.execute(
        "UPDATE events SET status = 'cleared', cleared_at = ? WHERE event_key = ?",
        (recent_cleared["observed_at"], recent_cleared["event_key"]),
    )
    conn.commit()
    conn.close()

    incidents = load_incidents(database, 72)

    assert [incident["event_key"] for incident in incidents] == [
        old_active["event_key"],
        recent_cleared["event_key"],
        polled_wildweb["event_key"],
    ]


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

    html = build_html([linked, current], "2026-05-31T08:05:00-07:00", 72, app_version="test-1")

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

    assert "Crestmap Malibu Incidents" in summary_html
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
        app_version="test-1",
    )

    assert "Crestmap Forest Incidents (1 current, 2 total)" in html
    assert 'http-equiv="Cache-Control"' not in html
    assert '<meta name="description" content="Live and historical CHP traffic incidents and WildWeb dispatch reports' in html
    assert '<meta property="og:description" content="Live and historical CHP traffic incidents and WildWeb dispatch reports' in html
    assert '<meta name="robots" content="index,follow,max-image-preview:large">' in html
    assert '<link rel="canonical" href="https://crestmap.us/">' in html
    assert '<link rel="icon" href="https://crestmap.us/favicon.svg?active=1&amp;v=' in html
    assert '<meta property="og:title" content="Crestmap Forest Incidents (1 current, 2 total)">' in html
    assert '<meta property="og:image" content="https://crestmap.us/og-image.png">' in html
    assert '<meta property="og:image:type" content="image/png">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert '<script type="application/ld+json">' in html
    assert "googletagmanager.com/gtag/js" not in html
    assert '"@type": "WebApplication"' in html
    assert '"applicationCategory": "MapApplication"' in html
    assert '"@type": "Dataset"' in html
    assert "Crestmap forest incident history" in html
    assert 'name="push_source" value="chp"' in html
    assert 'name="push_source" value="wildweb"' in html
    assert "WildWeb reports" in html
    assert 'const sources = selected("push_source")' in html
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
    assert "bottom: var(--details-cue-bottom, 14px)" in html
    assert "function updateDetailsCuePosition" in html
    assert "const cueBottom = Math.min(72, 14 + mapBelowViewport);" in html
    assert "const targetY = Math.max" not in html
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
    assert 'const appVersion = "test-1"' in html
    assert "reloadForAppVersion(latest.app_version)" in html
    assert 'reloadUrl.searchParams.set("app_version", latestVersion)' in html
    assert "window.location.replace(reloadUrl.href)" in html
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
    assert ".detail-header {\n      display: block;" in html
    assert "justify-content: flex-start;\n      gap: 8px;\n      flex-wrap: wrap;\n      margin-top: 10px;" in html
    assert html.index("${detailEntries ?") < html.index("<h3>Comments</h3>")
    assert html.index("<h3>Comments</h3>") < html.index("${trailingEntries ?")
    assert "data-hidden-details-toggle" in html
    assert "const adminMode = false" in html

    aircraft_html = build_html(
        incidents,
        "2026-05-31T08:05:00-07:00",
        72,
        aircraft_tracking_enabled=True,
    )
    assert 'data-aircraft-layer-toggle' in aircraft_html
    assert "Rescue helicopters" in aircraft_html
    assert 'const aircraftEndpoint = "/api/v1/aircraft"' in aircraft_html
    assert "function fetchAircraftData" in aircraft_html
    assert "Mission not confirmed" not in aircraft_html
    assert 'viewBox="0 0 32 24"' in aircraft_html
    assert "&#128641;" not in aircraft_html
    assert 'className: "aircraft-map-popup"' in aircraft_html
    assert "const aircraftTrails = new Map()" in aircraft_html
    assert '.aircraft-marker.is-stale' in aircraft_html
    assert 'Number(aircraft.age_seconds || 0) > 300 ? " is-stale"' in aircraft_html
    assert "L.polyline(trailPoints" in aircraft_html
    assert "opacity: 0.42" in aircraft_html
    assert "smoothFactor: 1.25" in aircraft_html

    admin_html = build_html(
        incidents,
        "2026-05-31T08:05:00-07:00",
        72,
        admin_mode=True,
    )
    assert "const adminMode = true" in admin_html
    assert "Checking hidden..." in admin_html
    assert "No hidden details" in admin_html
    assert "Show hidden (${entries.length})" in admin_html
    assert "/api/v1/incidents" in admin_html
    assert "/admin/incidents" not in admin_html
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
    assert "Crestmap Malibu Incidents" in malibu_html
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
    assert 'document.addEventListener("visibilitychange"' in html
    assert 'window.addEventListener("pageshow"' in html
    assert "event.persisted" in html
    assert "function setupStaleRefresh" in html
    assert "refreshAfterResume" in html
    assert "lastResumeRefreshAt < 15000" in html
    assert "function focusedCommentFormFor" in html
    assert "preserveFocusedComment: true" in html
    assert "detailsPanel.dataset.selectedIncidentKey === incident.event_key" in html
    assert '<label class="comment-field">' in html
    assert 'Contact <span class="comment-field-hint">(optional, not public)</span>' in html
    assert 'placeholder="Email or phone"' in html
    assert "@media (max-width: 420px)" in html
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
    assert 'aged_out: "is-wildweb-aged-out"' in html
    assert 'no_longer_listed: "is-wildweb-no-longer-listed"' in html
    assert ".incident-marker.is-wildweb-aged-out .incident-marker-core" in html
    assert "border-color: #967037" in html
    assert ".incident-marker.is-wildweb-no-longer-listed .incident-marker-core" in html
    assert "border-color: #596a72" in html
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
    assert "Summary - Crestmap Forest Incidents" in summary_html
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
    assert "History - Crestmap Forest Incidents" in history_html
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
    assert '<option value="active" selected>Active CHP</option>' in filtered_history_html
    assert '<option value="mapped" selected>Mapped only</option>' in filtered_history_html

    about_html = build_about_html(incidents, "2026-05-31T08:05:00-07:00", 72)
    assert "About - Crestmap Forest Incidents" in about_html
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
