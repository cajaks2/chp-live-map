import datetime as dt
import json
import uuid
from urllib.parse import urlsplit


REGIONS = {"forest", "malibu"}
CATEGORIES = {"collision", "hazard", "closure", "other"}
DEFAULT_REGIONS = sorted(REGIONS)
DEFAULT_CATEGORIES = sorted(CATEGORIES)
MAX_ENDPOINT_LENGTH = 2048
MAX_KEY_LENGTH = 512
MAX_DELIVERY_ATTEMPTS = 3


class PushValidationError(ValueError):
    pass


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _is_postgres(conn):
    return conn.__class__.__module__.startswith("psycopg")


def _placeholder(conn):
    return "%s" if _is_postgres(conn) else "?"


def normalize_choices(values, allowed, field):
    if values is None:
        return sorted(allowed)
    if not isinstance(values, list):
        raise PushValidationError(f"{field} must be an array")
    normalized = sorted({str(value).strip().lower() for value in values if str(value).strip()})
    if not normalized:
        raise PushValidationError(f"choose at least one {field[:-1]}")
    unknown = set(normalized) - allowed
    if unknown:
        raise PushValidationError(f"unsupported {field}: {', '.join(sorted(unknown))}")
    return normalized


def validate_subscription(payload):
    if not isinstance(payload, dict):
        raise PushValidationError("request body must be an object")
    subscription = payload.get("subscription")
    if not isinstance(subscription, dict):
        raise PushValidationError("subscription is required")
    endpoint = str(subscription.get("endpoint") or "").strip()
    parsed = urlsplit(endpoint)
    if not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH or parsed.scheme != "https" or not parsed.netloc:
        raise PushValidationError("subscription endpoint must be a valid HTTPS URL")
    keys = subscription.get("keys")
    if not isinstance(keys, dict):
        raise PushValidationError("subscription keys are required")
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not p256dh or not auth or len(p256dh) > MAX_KEY_LENGTH or len(auth) > MAX_KEY_LENGTH:
        raise PushValidationError("subscription keys are invalid")
    return {
        "endpoint": endpoint,
        "p256dh": p256dh,
        "auth": auth,
        "regions": normalize_choices(payload.get("regions"), REGIONS, "regions"),
        "categories": normalize_choices(payload.get("categories"), CATEGORIES, "categories"),
    }


def incident_category(incident_type):
    value = str(incident_type or "").lower()
    if any(token in value for token in ("collision", "hit and run", "injur")):
        return "collision"
    if any(token in value for token in ("closure", "closed", "weather", "snow", "chain control")):
        return "closure"
    if any(token in value for token in ("hazard", "debris", "animal", "tree", "rock", "flood")):
        return "hazard"
    return "other"


def save_subscription(conn, payload, user_agent=""):
    values = validate_subscription(payload)
    now = _now_iso()
    params = (
        values["endpoint"],
        values["p256dh"],
        values["auth"],
        json.dumps(values["regions"]),
        json.dumps(values["categories"]),
        str(user_agent or "")[:1000],
        now,
        now,
    )
    if _is_postgres(conn):
        row = conn.execute(
            """
            INSERT INTO push_subscriptions (
                endpoint, p256dh, auth, regions_json, categories_json, user_agent,
                created_at, updated_at, active
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (endpoint) DO UPDATE SET
                p256dh = EXCLUDED.p256dh,
                auth = EXCLUDED.auth,
                regions_json = EXCLUDED.regions_json,
                categories_json = EXCLUDED.categories_json,
                user_agent = EXCLUDED.user_agent,
                updated_at = EXCLUDED.updated_at,
                active = TRUE
            RETURNING id
            """,
            params,
        ).fetchone()
        subscription_id = row["id"]
    else:
        conn.execute(
            """
            INSERT INTO push_subscriptions (
                endpoint, p256dh, auth, regions_json, categories_json, user_agent,
                created_at, updated_at, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                regions_json = excluded.regions_json,
                categories_json = excluded.categories_json,
                user_agent = excluded.user_agent,
                updated_at = excluded.updated_at,
                active = 1
            """,
            params,
        )
        subscription_id = conn.execute(
            "SELECT id FROM push_subscriptions WHERE endpoint = ?", (values["endpoint"],)
        ).fetchone()["id"]
    return {"id": subscription_id, "regions": values["regions"], "categories": values["categories"]}


def subscription_preferences(conn, endpoint):
    if not endpoint:
        return None
    row = conn.execute(
        f"SELECT regions_json, categories_json, active FROM push_subscriptions WHERE endpoint = {_placeholder(conn)}",
        (endpoint,),
    ).fetchone()
    if not row or not row["active"]:
        return None
    return {
        "regions": json.loads(row["regions_json"]),
        "categories": json.loads(row["categories_json"]),
    }


def deactivate_subscription(conn, endpoint):
    now = _now_iso()
    cursor = conn.execute(
        f"UPDATE push_subscriptions SET active = {_placeholder(conn)}, updated_at = {_placeholder(conn)} "
        f"WHERE endpoint = {_placeholder(conn)}",
        ((False if _is_postgres(conn) else 0), now, endpoint),
    )
    return cursor.rowcount > 0


def incident_public_url(incident, public_url):
    base = (public_url or "https://crestmap.us/").rstrip("/") + "/"
    from urllib.parse import urlencode

    return f"{base}?{urlencode({'region': incident.get('region') or 'forest', 'incident': incident['event_key']})}"


def notification_payload(incident, public_url):
    incident_type = incident.get("type") or "CHP Incident"
    location = incident.get("location") or incident.get("location_desc") or "Location unavailable"
    area = incident.get("area") or ""
    region = incident.get("region") or "forest"
    return {
        "event_key": incident["event_key"],
        "region": region,
        "category": incident_category(incident_type),
        "title": f"New {incident_type}",
        "body": f"{location}{f' ({area})' if area else ''}",
        "url": incident_public_url(incident, public_url),
        "tag": f"chp-{incident['event_key']}",
    }


def enqueue_incidents(conn, incidents, public_url):
    created_at = _now_iso()
    for incident in incidents:
        payload = notification_payload(incident, public_url)
        params = (incident["event_key"], payload["region"], payload["category"], json.dumps(payload), created_at)
        if _is_postgres(conn):
            conn.execute(
                """
                INSERT INTO push_notification_events (event_key, region, category, payload_json, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (event_key) DO NOTHING
                """,
                params,
            )
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO push_notification_events
                    (event_key, region, category, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                params,
            )


def enqueue_test_notification(conn, endpoint, public_url):
    if not endpoint:
        raise PushValidationError("an active subscription is required")
    subscription = conn.execute(
        f"SELECT id FROM push_subscriptions WHERE endpoint = {_placeholder(conn)} "
        f"AND active = {_placeholder(conn)}",
        (endpoint, True if _is_postgres(conn) else 1),
    ).fetchone()
    if not subscription:
        raise PushValidationError("enable alerts on this device before sending a test")
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)).isoformat(timespec="seconds")
    recent = conn.execute(
        f"SELECT 1 FROM push_test_notifications WHERE subscription_id = {_placeholder(conn)} "
        f"AND created_at >= {_placeholder(conn)} LIMIT 1",
        (subscription["id"], cutoff),
    ).fetchone()
    if recent:
        raise PushValidationError("please wait a minute before sending another test")
    base = (public_url or "https://crestmap.us/").rstrip("/") + "/"
    event_key = f"push-test-{subscription['id']}-{uuid.uuid4().hex}"
    payload = {
        "event_key": event_key,
        "title": "Crestmap alerts are working",
        "body": "You’ll receive notifications here when a new incident matches your choices.",
        "url": base,
        "tag": event_key,
    }
    placeholders = ", ".join([_placeholder(conn)] * 4)
    conn.execute(
        f"""
        INSERT INTO push_test_notifications
            (event_key, subscription_id, payload_json, created_at)
        VALUES ({placeholders})
        """,
        (event_key, subscription["id"], json.dumps(payload), _now_iso()),
    )
    return {"queued": True, "event_key": event_key}


def _matching_subscriptions(conn, event):
    rows = conn.execute(
        "SELECT * FROM push_subscriptions WHERE active = {} AND created_at <= {}".format(
            "%s" if _is_postgres(conn) else "1",
            "%s" if _is_postgres(conn) else "?",
        ),
        ((True, event["created_at"]) if _is_postgres(conn) else (event["created_at"],)),
    ).fetchall()
    return [
        row
        for row in rows
        if event["region"] in json.loads(row["regions_json"])
        and event["category"] in json.loads(row["categories_json"])
    ]


def _delivery_row(conn, subscription_id, event_key):
    placeholder = _placeholder(conn)
    return conn.execute(
        f"SELECT * FROM push_deliveries WHERE subscription_id = {placeholder} AND event_key = {placeholder}",
        (subscription_id, event_key),
    ).fetchone()


def _record_delivery(conn, subscription_id, event_key, delivered, error=""):
    now = _now_iso()
    existing = _delivery_row(conn, subscription_id, event_key)
    delivered_at = now if delivered else None
    if existing:
        conn.execute(
            f"""
            UPDATE push_deliveries
            SET attempt_count = attempt_count + 1, delivered_at = {_placeholder(conn)},
                last_error = {_placeholder(conn)}, last_attempt_at = {_placeholder(conn)}
            WHERE id = {_placeholder(conn)}
            """,
            (delivered_at, str(error)[:1000], now, existing["id"]),
        )
    else:
        placeholders = ", ".join([_placeholder(conn)] * 7)
        conn.execute(
            f"""
            INSERT INTO push_deliveries (
                subscription_id, event_key, attempt_count, delivered_at,
                last_error, created_at, last_attempt_at
            ) VALUES ({placeholders})
            """,
            (subscription_id, event_key, 1, delivered_at, str(error)[:1000], now, now),
        )


def _http_status(exc):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def process_pending(conn, vapid_private_key, vapid_subject, sender=None, limit=20):
    if not vapid_private_key:
        return {"events": 0, "delivered": 0, "failed": 0, "expired": 0}
    if sender is None:
        from pywebpush import webpush

        sender = webpush
    vapid_subject = (vapid_subject or "https://crestmap.us").rstrip("/")
    test_events = conn.execute(
        f"""
        SELECT test.*, subscriptions.endpoint, subscriptions.p256dh, subscriptions.auth
        FROM push_test_notifications test
        JOIN push_subscriptions subscriptions ON subscriptions.id = test.subscription_id
        WHERE test.delivered_at IS NULL AND test.attempt_count < {MAX_DELIVERY_ATTEMPTS}
          AND subscriptions.active = {_placeholder(conn)}
        ORDER BY test.created_at LIMIT {int(limit)}
        """,
        (True if _is_postgres(conn) else 1,),
    ).fetchall()
    events = conn.execute(
        f"SELECT * FROM push_notification_events WHERE completed_at IS NULL ORDER BY created_at LIMIT {int(limit)}"
    ).fetchall()
    stats = {"events": len(test_events) + len(events), "delivered": 0, "failed": 0, "expired": 0}
    for event in test_events:
        subscription_info = {
            "endpoint": event["endpoint"],
            "keys": {"p256dh": event["p256dh"], "auth": event["auth"]},
        }
        delivered_at = None
        error = ""
        try:
            sender(
                subscription_info=subscription_info,
                data=event["payload_json"],
                vapid_private_key=vapid_private_key,
                vapid_claims={"sub": vapid_subject},
                ttl=300,
            )
        except Exception as exc:
            status = _http_status(exc)
            if status in {404, 410}:
                deactivate_subscription(conn, event["endpoint"])
                stats["expired"] += 1
            else:
                stats["failed"] += 1
            error = f"{type(exc).__name__}: {exc}"
        else:
            delivered_at = _now_iso()
            stats["delivered"] += 1
        conn.execute(
            f"""
            UPDATE push_test_notifications
            SET attempt_count = attempt_count + 1, delivered_at = {_placeholder(conn)},
                last_error = {_placeholder(conn)}, last_attempt_at = {_placeholder(conn)}
            WHERE event_key = {_placeholder(conn)}
            """,
            (delivered_at, error[:1000], _now_iso(), event["event_key"]),
        )
    for event in events:
        subscriptions = _matching_subscriptions(conn, event)
        for subscription in subscriptions:
            delivery = _delivery_row(conn, subscription["id"], event["event_key"])
            if delivery and (delivery["delivered_at"] or delivery["attempt_count"] >= MAX_DELIVERY_ATTEMPTS):
                continue
            subscription_info = {
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            }
            try:
                sender(
                    subscription_info=subscription_info,
                    data=event["payload_json"],
                    vapid_private_key=vapid_private_key,
                    vapid_claims={"sub": vapid_subject},
                    ttl=300,
                )
            except Exception as exc:
                status = _http_status(exc)
                if status in {404, 410}:
                    deactivate_subscription(conn, subscription["endpoint"])
                    stats["expired"] += 1
                else:
                    stats["failed"] += 1
                _record_delivery(conn, subscription["id"], event["event_key"], False, f"{type(exc).__name__}: {exc}")
            else:
                _record_delivery(conn, subscription["id"], event["event_key"], True)
                stats["delivered"] += 1

        outstanding = False
        for subscription in _matching_subscriptions(conn, event):
            delivery = _delivery_row(conn, subscription["id"], event["event_key"])
            if not delivery or (not delivery["delivered_at"] and delivery["attempt_count"] < MAX_DELIVERY_ATTEMPTS):
                outstanding = True
                break
        if not outstanding:
            conn.execute(
                f"UPDATE push_notification_events SET completed_at = {_placeholder(conn)} WHERE event_key = {_placeholder(conn)}",
                (_now_iso(), event["event_key"]),
            )
    return stats


def deliver_incidents(database, database_url, incidents, public_url, vapid_private_key, vapid_subject, sender=None):
    from scrape_chp_traffic import connect_database

    with connect_database(database, database_url) as conn:
        enqueue_incidents(conn, incidents, public_url)
        return process_pending(conn, vapid_private_key, vapid_subject, sender=sender)
