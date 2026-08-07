import json

from push_notifications import (
    PushValidationError,
    enqueue_incidents,
    enqueue_test_notification,
    incident_category,
    notification_areas,
    process_pending,
    save_subscription,
    subscription_preferences,
    validate_subscription,
)
from scrape_chp_traffic import connect_database, upsert_active_event


def event_row(event_key="LACC|2026-08-06|0123", region="forest", incident_type="Traffic Hazard"):
    return {
        "event_key": event_key,
        "center": "LACC",
        "incident_date": "2026-08-06",
        "incident_no": "0123",
        "observed_at": "2026-08-06T08:00:00-07:00",
        "updated_as_of": "2026-08-06T08:00:00-07:00",
        "incident_time": "8:00 AM",
        "type": incident_type,
        "location": "Angeles Crest Hwy / Mile Marker 30",
        "location_desc": "",
        "area": "Altadena",
        "latitude": 34.25,
        "longitude": -118.1,
        "matched_keywords": "angeles crest",
        "details_hash": "hash",
        "detail_entries": [],
        "region": region,
    }


def subscription_payload(endpoint="https://push.example.test/device-token", regions=None, categories=None):
    payload = {
        "subscription": {
            "endpoint": endpoint,
            "keys": {"p256dh": "public-device-key", "auth": "auth-secret"},
        }
    }
    if regions is not None:
        payload["regions"] = regions
    if categories is not None:
        payload["categories"] = categories
    return payload


def test_subscription_validation_and_preferences(tmp_path):
    database = tmp_path / "push.sqlite"
    conn = connect_database(database)

    saved = save_subscription(
        conn,
        subscription_payload(regions=["forest"], categories=["collision", "hazard"]),
        "iPhone Safari",
    )
    conn.commit()

    assert saved["regions"] == ["forest"]
    assert saved["categories"] == ["collision", "hazard"]
    assert subscription_preferences(conn, "https://push.example.test/device-token") == {
        "regions": ["forest"],
        "categories": ["collision", "hazard"],
    }
    row = conn.execute("SELECT * FROM push_subscriptions").fetchone()
    assert row["user_agent"] == "iPhone Safari"
    assert row["active"] == 1
    conn.close()

    try:
        validate_subscription(subscription_payload(endpoint="http://push.example.test/device"))
    except PushValidationError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("insecure push endpoint was accepted")


def test_incident_categories_cover_chp_labels():
    assert incident_category("Trfc Collision-Unkn Inj") == "collision"
    assert incident_category("Hit and Run No Injuries") == "collision"
    assert incident_category("Traffic Hazard") == "hazard"
    assert incident_category("Road Closure") == "closure"
    assert incident_category("Disabled Vehicle") == "other"


def test_crest_notification_area_includes_west_forest_and_excludes_baldy():
    crest = event_row()
    baldy = {
        **event_row(),
        "location": "Mount Baldy Rd / Shinn Rd",
        "matched_keywords": "mount baldy",
        "longitude": -117.68,
    }

    assert notification_areas(crest) == ["forest", "crest"]
    assert notification_areas(baldy) == ["forest"]


def test_crest_subscription_receives_only_west_forest_incidents(tmp_path):
    database = tmp_path / "push.sqlite"
    conn = connect_database(database)
    crest = event_row()
    baldy = {
        **event_row("LACC|2026-08-06|0124"),
        "location": "Mount Baldy Rd / Shinn Rd",
        "matched_keywords": "mount baldy",
        "longitude": -117.68,
    }
    upsert_active_event(conn, crest)
    upsert_active_event(conn, baldy)
    save_subscription(conn, subscription_payload("https://push.example.test/crest", ["crest"], ["hazard"]))
    save_subscription(conn, subscription_payload("https://push.example.test/all-forest", ["forest"], ["hazard"]))
    enqueue_incidents(conn, [crest, baldy], "https://crestmap.us/")
    conn.commit()

    calls = []
    stats = process_pending(conn, "private-key", "https://crestmap.us/", sender=lambda **kwargs: calls.append(kwargs))

    delivered = [
        (call["subscription_info"]["endpoint"], json.loads(call["data"])["event_key"])
        for call in calls
    ]
    assert stats == {"events": 2, "delivered": 3, "failed": 0, "expired": 0}
    assert delivered.count(("https://push.example.test/crest", crest["event_key"])) == 1
    assert ("https://push.example.test/crest", baldy["event_key"]) not in delivered
    assert ("https://push.example.test/all-forest", crest["event_key"]) in delivered
    assert ("https://push.example.test/all-forest", baldy["event_key"]) in delivered
    conn.close()


def test_delivery_filters_preferences_and_deduplicates(tmp_path):
    database = tmp_path / "push.sqlite"
    conn = connect_database(database)
    incident = event_row()
    upsert_active_event(conn, incident)
    save_subscription(conn, subscription_payload(regions=["forest"], categories=["hazard"]))
    save_subscription(
        conn,
        subscription_payload("https://push.example.test/malibu", ["malibu"], ["hazard"]),
    )
    enqueue_incidents(conn, [incident], "https://crestmap.us/")
    conn.commit()

    calls = []

    def sender(**kwargs):
        calls.append(kwargs)

    first = process_pending(conn, "private-key", "mailto:test@example.test", sender=sender)
    second = process_pending(conn, "private-key", "mailto:test@example.test", sender=sender)
    conn.commit()

    assert first == {"events": 1, "delivered": 1, "failed": 0, "expired": 0}
    assert second == {"events": 0, "delivered": 0, "failed": 0, "expired": 0}
    assert len(calls) == 1
    payload = json.loads(calls[0]["data"])
    assert payload["title"] == "New Traffic Hazard"
    assert payload["region"] == "forest"
    assert "incident=LACC%7C2026-08-06%7C0123" in payload["url"]
    assert calls[0]["ttl"] == 300
    assert calls[0]["vapid_claims"] == {"sub": "mailto:test@example.test"}
    assert conn.execute("SELECT COUNT(*) AS count FROM push_deliveries").fetchone()["count"] == 1
    assert conn.execute("SELECT completed_at FROM push_notification_events").fetchone()["completed_at"]
    conn.close()


def test_test_notification_targets_only_requesting_subscription(tmp_path):
    database = tmp_path / "push.sqlite"
    conn = connect_database(database)
    endpoint = "https://push.example.test/requesting-device"
    save_subscription(conn, subscription_payload(endpoint, ["forest"], ["collision"]))
    save_subscription(conn, subscription_payload("https://push.example.test/other-device"))
    result = enqueue_test_notification(conn, endpoint, "https://crestmap.us/")
    conn.commit()

    calls = []
    stats = process_pending(conn, "private-key", "https://crestmap.us/", sender=lambda **kwargs: calls.append(kwargs))

    assert result["queued"] is True
    assert stats == {"events": 1, "delivered": 1, "failed": 0, "expired": 0}
    assert len(calls) == 1
    assert calls[0]["subscription_info"]["endpoint"] == endpoint
    assert calls[0]["vapid_claims"] == {"sub": "https://crestmap.us"}
    payload = json.loads(calls[0]["data"])
    assert payload["title"] == "Crestmap alerts are working"
    assert payload["url"] == "https://crestmap.us/"
    conn.close()


def test_transient_push_failure_retries(tmp_path):
    database = tmp_path / "push.sqlite"
    conn = connect_database(database)
    incident = event_row()
    upsert_active_event(conn, incident)
    save_subscription(conn, subscription_payload(regions=["forest"], categories=["hazard"]))
    enqueue_incidents(conn, [incident], "https://crestmap.us/")
    conn.commit()

    attempts = []

    def sender(**_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("temporary push outage")

    first = process_pending(conn, "private-key", "mailto:test@example.test", sender=sender)
    second = process_pending(conn, "private-key", "mailto:test@example.test", sender=sender)
    conn.commit()

    assert first["failed"] == 1
    assert second["delivered"] == 1
    delivery = conn.execute("SELECT * FROM push_deliveries").fetchone()
    assert delivery["attempt_count"] == 2
    assert delivery["delivered_at"]
    conn.close()
