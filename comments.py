import datetime as dt
import hashlib
import html
import os
import re
from collections import Counter


MAX_COMMENT_LENGTH = int(os.environ.get("COMMENT_MAX_LENGTH", "750"))
MIN_COMMENT_LENGTH = int(os.environ.get("COMMENT_MIN_LENGTH", "3"))
RATE_LIMIT_WINDOW_MINUTES = int(os.environ.get("COMMENT_RATE_LIMIT_WINDOW_MINUTES", "10"))
RATE_LIMIT_WINDOW_COUNT = int(os.environ.get("COMMENT_RATE_LIMIT_WINDOW_COUNT", "3"))
RATE_LIMIT_DAY_COUNT = int(os.environ.get("COMMENT_RATE_LIMIT_DAY_COUNT", "10"))
IP_HASH_SALT = os.environ.get("COMMENT_IP_HASH_SALT", "")
COMMENT_SUBMISSIONS_TOTAL = Counter()


class CommentValidationError(ValueError):
    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.code = code


def is_postgres(conn):
    return conn.__class__.__module__.startswith("psycopg")


def placeholder(conn):
    return "%s" if is_postgres(conn) else "?"


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def strip_html(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]*>", " ", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_optional(value, max_length):
    value = strip_html(value)
    return value[:max_length] if value else None


def clean_comment_body(value):
    body = strip_html(value)
    if len(body) < MIN_COMMENT_LENGTH:
        raise CommentValidationError("Comment is too short.", "too_short")
    if len(body) > MAX_COMMENT_LENGTH:
        raise CommentValidationError(f"Comment must be {MAX_COMMENT_LENGTH} characters or fewer.", "too_long")
    return body


def hash_ip(ip_address):
    if not ip_address:
        return None
    digest = hashlib.sha256(f"{IP_HASH_SALT}:{ip_address}".encode("utf-8")).hexdigest()
    return digest


def client_ip_from_headers(headers, fallback=""):
    cloudflare_ip = (headers.get("cf-connecting-ip") or "").strip()
    forwarded_for = (headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    real_ip = (headers.get("x-real-ip") or "").strip()
    return cloudflare_ip or forwarded_for or real_ip or fallback or ""


def event_exists(conn, event_key):
    ph = placeholder(conn)
    row = conn.execute(f"SELECT 1 FROM events WHERE event_key = {ph} LIMIT 1", (event_key,)).fetchone()
    return bool(row)


def public_comment(row):
    data = dict(row)
    return {
        "id": data["id"],
        "event_key": data["event_key"],
        "display_name": data.get("display_name") or "Anonymous",
        "body": data["body"],
        "category": data.get("category"),
        "created_at": data["created_at"],
    }


def list_approved_comments(conn, event_key):
    ph = placeholder(conn)
    rows = conn.execute(
        f"""
        SELECT id, event_key, display_name, body, category, created_at
        FROM incident_comments
        WHERE event_key = {ph}
          AND status = 'approved'
        ORDER BY created_at ASC, id ASC
        """,
        (event_key,),
    ).fetchall()
    return [public_comment(row) for row in rows]


def rate_limit_count(conn, ip_hash, user_agent, since):
    if not ip_hash:
        return 0
    ph = placeholder(conn)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM incident_comments
        WHERE ip_hash = {ph}
          AND COALESCE(user_agent, '') = {ph}
          AND created_at >= {ph}
        """,
        (ip_hash, user_agent or "", since),
    ).fetchone()
    return int(dict(row)["count"] if row else 0)


def validate_rate_limit(conn, ip_hash, user_agent, now):
    window_since = (now - dt.timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)).isoformat(timespec="seconds")
    day_since = (now - dt.timedelta(days=1)).isoformat(timespec="seconds")
    if rate_limit_count(conn, ip_hash, user_agent, window_since) >= RATE_LIMIT_WINDOW_COUNT:
        raise CommentValidationError("Too many comments recently. Try again later.", "rate_limited")
    if rate_limit_count(conn, ip_hash, user_agent, day_since) >= RATE_LIMIT_DAY_COUNT:
        raise CommentValidationError("Daily comment limit reached. Try again later.", "rate_limited")


def submit_comment(conn, event_key, payload, headers, client_host=""):
    if not event_exists(conn, event_key):
        raise CommentValidationError("Incident not found.", "not_found")
    honeypot = str(payload.get("website") or payload.get("url") or payload.get("company") or "").strip()
    if honeypot:
        raise CommentValidationError("Comment rejected.", "honeypot")
    body = clean_comment_body(payload.get("body"))
    display_name = clean_optional(payload.get("display_name") or payload.get("name"), 80)
    category = clean_optional(payload.get("category"), 40)
    contact = clean_optional(payload.get("contact"), 200)
    user_agent = clean_optional(headers.get("user-agent"), 500) or ""
    cf_connecting_ip = clean_optional(headers.get("cf-connecting-ip"), 64)
    cf_country = clean_optional(headers.get("cf-ipcountry"), 8)
    ip_address = client_ip_from_headers(headers, client_host)
    ip_hash = hash_ip(ip_address)
    now = dt.datetime.now(dt.timezone.utc)
    validate_rate_limit(conn, ip_hash, user_agent, now)
    created_at = now.isoformat(timespec="seconds")
    values = (
        event_key,
        "pending",
        display_name,
        body,
        category,
        contact,
        created_at,
        ip_hash,
        user_agent,
        cf_connecting_ip,
        cf_country,
        honeypot or None,
    )
    if is_postgres(conn):
        row = conn.execute(
            """
            INSERT INTO incident_comments (
                event_key, status, display_name, body, category, contact, created_at,
                ip_hash, user_agent, cf_connecting_ip, cf_country, honeypot_value
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            values,
        ).fetchone()
        comment_id = dict(row)["id"]
    else:
        cursor = conn.execute(
            """
            INSERT INTO incident_comments (
                event_key, status, display_name, body, category, contact, created_at,
                ip_hash, user_agent, cf_connecting_ip, cf_country, honeypot_value
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        comment_id = cursor.lastrowid
    COMMENT_SUBMISSIONS_TOTAL["pending"] += 1
    return {"id": comment_id, "status": "pending", "message": "Comment submitted for review."}


def set_comment_status(conn, comment_id, status):
    if status not in {"approved", "rejected"}:
        raise ValueError("status must be approved or rejected")
    timestamp_column = "approved_at" if status == "approved" else "rejected_at"
    ph = placeholder(conn)
    query = f"""
        UPDATE incident_comments
        SET status = {ph},
            {timestamp_column} = {ph}
        WHERE id = {ph}
    """
    conn.execute(query, (status, now_iso(), comment_id))


def delete_comment(conn, comment_id):
    ph = placeholder(conn)
    conn.execute(f"DELETE FROM incident_comments WHERE id = {ph}", (comment_id,))


def moderation_rows(conn, status="pending", limit=50):
    ph = placeholder(conn)
    rows = conn.execute(
        f"""
        SELECT c.id, c.event_key, c.status, c.display_name, c.body, c.category, c.contact,
               c.created_at, c.cf_country, c.user_agent, c.cf_connecting_ip,
               e.type, e.location, e.area, e.incident_no, e.region
        FROM incident_comments c
        LEFT JOIN events e ON e.event_key = c.event_key
        WHERE c.status = {ph}
        ORDER BY c.created_at ASC, c.id ASC
        LIMIT {int(limit)}
        """,
        (status,),
    ).fetchall()
    return [dict(row) for row in rows]


def comment_status_counts(conn):
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM incident_comments
        GROUP BY status
        """
    ).fetchall()
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    for row in rows:
        data = dict(row)
        counts[data["status"]] = int(data["count"])
    return counts


def pending_count(conn):
    row = conn.execute("SELECT COUNT(*) AS count FROM incident_comments WHERE status = 'pending'").fetchone()
    return int(dict(row)["count"] if row else 0)
