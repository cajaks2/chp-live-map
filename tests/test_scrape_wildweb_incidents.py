import datetime as dt
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import scrape_wildweb_incidents
from scrape_chp_traffic import ScraperMetrics, connect_database
from scrape_wildweb_incidents import normalize_incident


def wildweb_item(**overrides):
    item = {
        "uuid": "76a0be78-abd6-4334-9314-df4a81e60ebf",
        "date": "2026-08-11T11:33:00",
        "inc_num": "3487",
        "type": "Miscellaneous",
        "name": "ROAD HAZARD",
        "location": "ANGELES CREST HWY / RED BOX RD",
        "latitude": "34.300100",
        "longitude": "118.103200",
        "acres": None,
        "resources": [None],
        "webComment": None,
        "fire_status": '{"out": null, "contain": null, "control": null}',
        "fiscal_data": '{"inc_num": "3487", "wfdssunit": "CAANF"}',
    }
    item.update(overrides)
    return item


def normalize(item, observed_at="2026-08-11T12:00:00-07:00", max_age_hours=72):
    return normalize_incident(
        item,
        "CAANCC",
        dt.datetime.fromisoformat("2026-08-11T19:00:00+00:00"),
        observed_at,
        max_age_hours=max_age_hours,
    )


def test_fetch_center_records_wildweb_http_response_codes(monkeypatch):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps([{"retrieved": "2026-08-11T19:00:00Z", "data": []}]).encode()

    responses = [
        HTTPError("https://wildweb.example.test", 503, "unavailable", {}, None),
        FakeResponse(),
    ]

    def fake_urlopen(*_args, **_kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    metrics = ScraperMetrics(provider="wildweb", source_defaults=("api",))
    stats = {"http_status_counts": {}, "source_bytes": 0}
    monkeypatch.setattr(scrape_wildweb_incidents, "urlopen", fake_urlopen)

    payload = scrape_wildweb_incidents.fetch_center(
        "CAANCC",
        timeout=5,
        user_agent="test-agent",
        retries=1,
        retry_backoff=0,
        stats=stats,
        metrics=metrics,
    )

    assert payload["data"] == []
    body = metrics.render().decode("utf-8")
    assert (
        'chp_live_map_scraper_http_requests_total{provider="wildweb",method="GET",route="incidents",status="200"} 1'
        in body
    )
    assert (
        'chp_live_map_scraper_http_requests_total{provider="wildweb",method="GET",route="incidents",status="503"} 1'
        in body
    )
    assert "chp_live_map_scraper_chp_http_requests_total" not in body


def test_normalize_wildweb_incident_uses_shared_boundary_and_source_identity():
    row = normalize(wildweb_item())

    assert row["event_key"] == "wildweb|CAANCC|76a0be78-abd6-4334-9314-df4a81e60ebf"
    assert row["source"] == "wildweb"
    assert row["source_event_id"] == "76a0be78-abd6-4334-9314-df4a81e60ebf"
    assert row["status"] == "reported"
    assert row["source_status"] == "listed"
    assert row["region"] == "forest"
    assert row["longitude"] == -118.1032
    assert row["coordinate_confidence"] == "exact"
    assert row["incident_time"] == "11:33 AM"


def test_normalize_wildweb_incident_keeps_known_place_without_pin():
    row = normalize(
        wildweb_item(
            latitude=None,
            longitude=None,
            location="STRAWBERRY PEAK",
            name="SEARCH AND RESCUE",
            type="Wildland Search/Rescue/Recovery",
        )
    )

    assert row["region"] == "forest"
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["coordinate_confidence"] == "missing"
    assert "strawberry peak" in row["matched_keywords"]


def test_normalize_wildweb_incident_rejects_unknown_unpinned_and_out_of_bounds_rows():
    assert normalize(wildweb_item(latitude=None, longitude=None, location="UNKNOWN", name="UNKNOWN")) is None
    assert (
        normalize(
            wildweb_item(
                latitude="34.701000",
                longitude="118.572000",
                location="ANGELES CREST HWY",
            )
        )
        is None
    )


def test_normalize_wildweb_incident_rejects_us_101_inside_malibu_boundary():
    assert (
        normalize(
            wildweb_item(
                location="US101 W / KANAN RD OFR",
                name="TRAFFIC COLLISION",
                latitude="34.145531",
                longitude="118.756128",
            )
        )
        is None
    )


def test_normalize_wildweb_incident_rejects_old_future_and_administrative_rows():
    assert normalize(wildweb_item(date="2026-08-07T11:33:00")) is None
    assert normalize(wildweb_item(date="2026-08-11T12:06:00")) is None
    assert normalize(wildweb_item(type="Resource Order")) is None
    assert normalize(wildweb_item(type="Law Enforcement", name="*******", location="*******")) is None


def test_normalize_wildweb_out_time_is_explicitly_cleared():
    row = normalize(
        wildweb_item(
            type="Wildfire",
            name="GABRIEL",
            fire_status='{"out": "2026-08-11T11:55:00", "contain": null, "control": null}',
        )
    )

    assert row["status"] == "cleared"
    assert row["source_status"] == "out"
    assert row["cleared_at"] == "2026-08-11T11:55:00-07:00"
    assert row["detail_entries"][0]["text"].startswith("Out:")


def test_scrape_once_archives_missing_wildweb_report_without_calling_it_cleared(tmp_path, monkeypatch):
    database = tmp_path / "wildweb.sqlite"
    now = dt.datetime.now().astimezone()
    item = wildweb_item(date=now.replace(tzinfo=None).isoformat(timespec="seconds"))
    payloads = [
        {"retrieved": dt.datetime.now(dt.timezone.utc).isoformat(), "data": [item]},
        {"retrieved": dt.datetime.now(dt.timezone.utc).isoformat(), "data": []},
    ]
    monkeypatch.setattr(scrape_wildweb_incidents, "fetch_center", lambda *args, **kwargs: payloads.pop(0))
    monkeypatch.setattr(scrape_wildweb_incidents, "log_discovered_incident", lambda row: None)
    args = SimpleNamespace(
        center=["CAANCC"],
        timeout=5,
        user_agent="test-agent",
        retries=0,
        retry_backoff=0,
        max_age_hours=72,
        future_tolerance_minutes=5,
        database=database,
        database_url=None,
        notifications=False,
    )

    first = scrape_wildweb_incidents.scrape_once(args)
    conn = connect_database(database)
    stored = conn.execute("SELECT * FROM events").fetchone()
    assert first["discovered"] == 1
    assert stored["status"] == "reported"
    assert stored["source"] == "wildweb"
    conn.close()

    second = scrape_wildweb_incidents.scrape_once(args)
    conn = connect_database(database)
    stored = conn.execute("SELECT * FROM events").fetchone()
    assert second["changed"] == 1
    assert stored["status"] == "archived"
    assert stored["source_status"] == "no_longer_listed"
    assert stored["cleared_at"] is not None
    assert [
        row["status"]
        for row in conn.execute("SELECT status FROM observations ORDER BY id").fetchall()
    ] == ["reported", "archived"]
    conn.close()


def test_database_backfills_chp_source_identity(tmp_path):
    database = tmp_path / "schema.sqlite"
    conn = connect_database(database)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}

    assert {"source", "source_event_id", "source_status", "source_reported_at", "coordinate_confidence"} <= columns
    conn.close()


def test_wildweb_main_publishes_shared_provider_labeled_scraper_metrics(monkeypatch):
    metrics = ScraperMetrics(provider="wildweb", source_defaults=("api",))
    args = SimpleNamespace(metrics_port=0, metrics_host=None, interval=0, center=["CAANCC"])
    monkeypatch.setattr(scrape_wildweb_incidents, "WILDWEB_METRICS", metrics)
    monkeypatch.setattr(scrape_wildweb_incidents, "parse_args", lambda: args)
    monkeypatch.setattr(
        scrape_wildweb_incidents,
        "scrape_once",
        lambda _args: {
            "observed_at": "2026-08-11T12:00:00-07:00",
            "total_seen": 25,
            "matched": 8,
            "mapped": 6,
            "changed": 2,
            "discovered": 1,
            "duration_seconds": 0.75,
            "source_bytes": 4096,
            "region_counts": {
                "forest": {"matched": 7, "mapped": 5},
                "malibu": {"matched": 1, "mapped": 1},
            },
            "retrieved_at": "2026-08-11T19:00:00+00:00",
        },
    )
    monkeypatch.setattr(scrape_wildweb_incidents, "log_event", lambda *args, **kwargs: None)

    scrape_wildweb_incidents.main()

    body = metrics.render().decode("utf-8")
    assert 'chp_live_map_scraper_up{provider="wildweb"} 1' in body
    assert (
        'chp_live_map_scraper_source_attempts_total{provider="wildweb",source="api",mode="primary",outcome="success"} 1'
        in body
    )
    assert (
        'chp_live_map_scraper_last_run_incidents{provider="wildweb",kind="matched"} 8'
        in body
    )
    assert (
        'chp_live_map_scraper_last_run_region_incidents{provider="wildweb",region="forest",kind="mapped"} 5'
        in body
    )
    assert (
        'chp_live_map_scraper_last_run_source_response_bytes{provider="wildweb",source="api"} 4096'
        in body
    )
    assert "chp_live_map_scraper_xml_feed_age_seconds" not in body
    assert "chp_live_map_scraper_chp_http_requests_total" not in body
