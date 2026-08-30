"""Revocable admin sessions; only token hashes are persisted."""

import base64
import hashlib
import hmac
import json
import secrets
import time

from comments import placeholder


SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_sessions (
    session_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    last_activity_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    absolute_expires_at BIGINT NOT NULL,
    remembered INTEGER NOT NULL,
    user_agent TEXT NOT NULL
)
"""


def admin_session_key(settings):
    # Changing either credential or the optional signing secret invalidates sessions.
    material = json.dumps([
        "crestmap-admin-session-v2", settings.admin_username,
        settings.admin_password, settings.admin_session_secret,
    ]).encode("utf-8")
    return hashlib.sha256(material).digest()


def create_admin_session_token(settings, now=None, lifetime_seconds=None):
    now = int(time.time() if now is None else now)
    lifetime = lifetime_seconds or max(1, settings.admin_session_hours) * 3600
    payload = json.dumps({
        "username": settings.admin_username,
        "expires_at": now + lifetime,
        "nonce": secrets.token_urlsafe(32),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(admin_session_key(settings), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def valid_admin_session_token(settings, token, now=None):
    if not token or len(token) > 2048 or "." not in token:
        return False
    try:
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(admin_session_key(settings), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(signature, expected):
            return False
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        now = int(time.time() if now is None else now)
        return (
            int(payload["expires_at"]) > now
            and bool(payload["nonce"])
            and secrets.compare_digest(str(payload["username"]), settings.admin_username or "")
        )
    except (ValueError, TypeError, KeyError, UnicodeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn, settings, remembered=False, user_agent="", now=None):
    now = int(time.time() if now is None else now)
    absolute_seconds = (
        max(1, settings.admin_remember_days) * 86400 if remembered
        else max(1, settings.admin_session_max_hours) * 3600
    )
    idle_seconds = absolute_seconds if remembered else max(1, settings.admin_session_hours) * 3600
    token = create_admin_session_token(settings, now, absolute_seconds)
    row = {
        "session_id": secrets.token_hex(16), "token_hash": token_hash(token),
        "username": settings.admin_username, "created_at": now, "last_activity_at": now,
        "expires_at": now + min(idle_seconds, absolute_seconds),
        "absolute_expires_at": now + absolute_seconds,
        "remembered": int(remembered), "user_agent": user_agent[:240],
    }
    ph = placeholder(conn)
    conn.execute(f"DELETE FROM admin_sessions WHERE expires_at <= {ph}", (now,))
    conn.execute(
        f"INSERT INTO admin_sessions ({', '.join(row)}) VALUES ({', '.join([ph] * len(row))})",
        tuple(row.values()),
    )
    return token, row


def load_session(conn, settings, token, now=None):
    now = int(time.time() if now is None else now)
    if not valid_admin_session_token(settings, token, now):
        return None
    ph = placeholder(conn)
    row = conn.execute(
        f"SELECT * FROM admin_sessions WHERE token_hash = {ph} AND username = {ph} "
        f"AND expires_at > {ph} AND absolute_expires_at > {ph}",
        (token_hash(token), settings.admin_username, now, now),
    ).fetchone()
    return dict(row) if row else None


def renew_session(conn, settings, token, now=None):
    now = int(time.time() if now is None else now)
    row = load_session(conn, settings, token, now)
    if not row:
        return None
    idle_seconds = (
        max(1, settings.admin_remember_days) * 86400 if row["remembered"]
        else max(1, settings.admin_session_hours) * 3600
    )
    expiry = min(row["absolute_expires_at"], now + idle_seconds)
    ph = placeholder(conn)
    # A revoked/expired session must not be resurrected by a concurrent renewal.
    result = conn.execute(
        f"UPDATE admin_sessions SET expires_at = {ph}, last_activity_at = {ph} "
        f"WHERE session_id = {ph} AND expires_at > {ph} AND absolute_expires_at > {ph}",
        (expiry, now, row["session_id"], now, now),
    )
    if result.rowcount != 1:
        return None
    return {**row, "expires_at": expiry, "last_activity_at": now}


def list_sessions(conn, settings, now=None):
    now = int(time.time() if now is None else now)
    ph = placeholder(conn)
    return [dict(row) for row in conn.execute(
        f"SELECT * FROM admin_sessions WHERE username = {ph} AND expires_at > {ph} "
        f"AND absolute_expires_at > {ph} ORDER BY last_activity_at DESC",
        (settings.admin_username, now, now),
    ).fetchall()]


def revoke_sessions(conn, settings, session_id=None, except_id=None):
    ph = placeholder(conn)
    query = f"DELETE FROM admin_sessions WHERE username = {ph}"
    params = [settings.admin_username]
    if session_id is not None:
        query += f" AND session_id = {ph}"
        params.append(session_id)
    if except_id is not None:
        query += f" AND session_id != {ph}"
        params.append(except_id)
    conn.execute(query, tuple(params))
