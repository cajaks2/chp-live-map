import base64
import datetime as dt
import html
import json
import os
import secrets
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import Response

from admin_sessions import (
    create_admin_session_token,
    create_session,
    list_sessions,
    load_session,
    renew_session,
    revoke_sessions,
    valid_admin_session_token,
)
from aircraft_tracking import load_tracker_status, load_visible_aircraft
from comments import (
    CommentValidationError,
    comment_status_counts,
    delete_comment,
    list_approved_comments,
    moderation_rows,
    set_comment_status,
    submit_comment,
)
from media import (
    MediaValidationError,
    R2MediaStore,
    approved_media_row,
    create_media_row,
    create_upload_token,
    finalize_media_row,
    media_for_comments,
    media_keys_for_comment,
    media_row,
    public_media,
    set_media_status,
    validate_upload,
    validate_upload_token,
)
from push_notifications import (
    DEFAULT_CATEGORIES,
    DEFAULT_REGIONS,
    DEFAULT_SOURCES,
    PushValidationError,
    deactivate_subscription,
    enqueue_test_notification,
    save_subscription,
    subscription_preferences,
)
import serve_live_map as web
from generate_live_map import (
    admin_activity_script,
    build_about_html,
    build_history_html,
    build_html,
    build_summary_html,
    include_linked_incident,
    incident_status,
    load_incident_by_key,
    load_incidents,
    load_last_scrape_run,
    load_removed_detail_entries,
    normalize_base_path,
    normalize_region,
    region_label,
)
from scrape_chp_traffic import connect_database


@dataclass
class WebSettings:
    database: Path = Path("chp_traffic.sqlite")
    database_url: str | None = None
    hours: float = 72.0
    base_path: str = "/"
    public_url: str | None = None
    google_analytics_id: str | None = None
    database_pool_min: int = 1
    database_pool_max: int = 5
    admin_username: str | None = None
    admin_password: str | None = None
    admin_session_secret: str | None = None
    admin_session_hours: int = 8
    admin_session_max_hours: int = 24
    admin_remember_days: int = 30
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket: str = "crestmap-media"
    r2_upload_token_secret: str | None = None
    r2_upload_ttl_seconds: int = 900
    media_max_image_bytes: int = 8 * 1024 * 1024
    media_max_video_bytes: int = 100 * 1024 * 1024
    media_max_video_seconds: int = 60
    vapid_public_key: str | None = None
    aircraft_tracking_enabled: bool = False
    aircraft_display_delay_seconds: int = 60
    aircraft_max_age_seconds: int = 900
    aircraft_trail_age_seconds: int = 1800
    service_version: str = "dev"

    @classmethod
    def from_env(cls):
        return cls(
            database=Path(os.environ.get("DATABASE", "chp_traffic.sqlite")),
            database_url=os.environ.get("DATABASE_URL") or None,
            hours=float(os.environ.get("MAP_HOURS", "72")),
            base_path=os.environ.get("BASE_PATH", "/"),
            public_url=os.environ.get("PUBLIC_URL") or None,
            google_analytics_id=os.environ.get("GOOGLE_ANALYTICS_ID") or None,
            database_pool_min=int(os.environ.get("DATABASE_POOL_MIN", "1")),
            database_pool_max=int(os.environ.get("DATABASE_POOL_MAX", "5")),
            admin_username=os.environ.get("ADMIN_USERNAME") or None,
            admin_password=os.environ.get("ADMIN_PASSWORD") or None,
            admin_session_secret=os.environ.get("ADMIN_SESSION_SECRET") or None,
            admin_session_hours=int(os.environ.get("ADMIN_SESSION_HOURS", "8")),
            admin_session_max_hours=int(os.environ.get("ADMIN_SESSION_MAX_HOURS", "24")),
            admin_remember_days=int(os.environ.get("ADMIN_REMEMBER_DAYS", "30")),
            r2_account_id=os.environ.get("R2_ACCOUNT_ID") or None,
            r2_access_key_id=os.environ.get("R2_ACCESS_KEY_ID") or None,
            r2_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY") or None,
            r2_bucket=os.environ.get("R2_BUCKET", "crestmap-media"),
            r2_upload_token_secret=os.environ.get("R2_UPLOAD_TOKEN_SECRET") or None,
            r2_upload_ttl_seconds=int(os.environ.get("R2_UPLOAD_TTL_SECONDS", "900")),
            media_max_image_bytes=int(os.environ.get("MEDIA_MAX_IMAGE_BYTES", str(8 * 1024 * 1024))),
            media_max_video_bytes=int(os.environ.get("MEDIA_MAX_VIDEO_BYTES", str(100 * 1024 * 1024))),
            media_max_video_seconds=int(os.environ.get("MEDIA_MAX_VIDEO_SECONDS", "60")),
            vapid_public_key=os.environ.get("VAPID_PUBLIC_KEY") or None,
            aircraft_tracking_enabled=os.environ.get("AIRCRAFT_TRACKING_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"},
            aircraft_display_delay_seconds=max(
                0, int(os.environ.get("AIRCRAFT_DISPLAY_DELAY_SECONDS", "60"))
            ),
            aircraft_max_age_seconds=max(60, int(os.environ.get("AIRCRAFT_MAX_AGE_SECONDS", "900"))),
            aircraft_trail_age_seconds=max(
                60, int(os.environ.get("AIRCRAFT_TRAIL_AGE_SECONDS", "1800"))
            ),
            service_version=os.environ.get("SERVICE_VERSION", "dev"),
        )


def media_enabled(settings):
    return all(
        [
            settings.r2_account_id,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_bucket,
            settings.r2_upload_token_secret,
        ]
    )


def build_media_store(settings):
    if not media_enabled(settings):
        return None
    return R2MediaStore(
        settings.r2_account_id,
        settings.r2_access_key_id,
        settings.r2_secret_access_key,
        settings.r2_bucket,
    )


def _pool_limits(settings):
    pool_min = max(0, settings.database_pool_min)
    pool_max = max(1, settings.database_pool_max)
    if pool_min > pool_max:
        pool_min = pool_max
    return pool_min, pool_max


def _path(request):
    return request.url.path.rstrip("/") or "/"


def _query(request):
    return parse_qs(request.url.query)


def request_target(request):
    if request.url.query:
        return f"{request.url.path}?{request.url.query}"
    return request.url.path


def requested_hours(request, settings):
    raw_hours = (_query(request).get("hours") or [None])[0]
    if raw_hours is None:
        return settings.hours
    try:
        hours = float(raw_hours)
    except (TypeError, ValueError):
        return settings.hours
    return min(max(hours, web.MIN_HISTORY_HOURS), web.MAX_HISTORY_HOURS)


def requested_region(request):
    return normalize_region((_query(request).get("region") or [None])[0])


def requested_incident_key(request):
    return (_query(request).get("incident") or [""])[0]


def history_filters(request):
    params = _query(request)
    return {
        "q": (params.get("q") or [""])[0],
        "road": (params.get("road") or ["all"])[0],
        "type": (params.get("type") or ["all"])[0],
        "status": (params.get("status") or ["all"])[0],
        "mapped": (params.get("mapped") or ["all"])[0],
    }


def summary_filters(request):
    params = _query(request)
    return {
        "type": (params.get("type") or ["all"])[0],
    }


def route_label(path, settings):
    path = path.rstrip("/") or "/"
    base_path = normalize_base_path(settings.base_path)
    asset_base = "" if base_path == "/" else base_path
    if path in {"/", "/live_chp_map.html", base_path}:
        return "map"
    if path in {"/summary", f"{asset_base}/summary"}:
        return "summary"
    if path in {"/history", f"{asset_base}/history"}:
        return "history"
    if path in {"/about", f"{asset_base}/about"}:
        return "about"
    if path in {"/status.json", f"{asset_base}/status.json"}:
        return "status"
    if path in {"/incidents.json", f"{asset_base}/incidents.json"}:
        return "incidents"
    if path in {"/api/v1/aircraft", f"{asset_base}/api/v1/aircraft"}:
        return "aircraft"
    if path in {"/admin/comments", f"{asset_base}/admin/comments"}:
        return "admin_comments"
    if path in {
        "/admin/login",
        f"{asset_base}/admin/login",
        "/admin/logout",
        f"{asset_base}/admin/logout",
        "/admin/session",
        f"{asset_base}/admin/session",
        f"{asset_base}/admin/session/activity",
        f"{asset_base}/admin/sessions",
    }:
        return "admin_session"
    if path.startswith("/api/v1/incidents/") and path.endswith("/hidden-details"):
        return "hidden_details"
    if path.startswith("/api/v1/incidents/") and path.endswith("/comments"):
        return "comments"
    if path.startswith("/api/v1/incidents/") and "/media/" in path:
        return "media_upload"
    if path.startswith("/api/v1/media/") or path.startswith(f"{asset_base}/admin/media/"):
        return "media"
    if path.startswith("/api/v1/push/"):
        return "push"
    if path in {"/manifest.webmanifest", f"{asset_base}/manifest.webmanifest", "/sw.js", f"{asset_base}/sw.js"}:
        return "pwa_asset"
    if path in {"/metrics", f"{asset_base}/metrics"}:
        return "metrics"
    if path in {"/healthz", "/readyz"}:
        return "health"
    if path in {"/robots.txt", f"{asset_base}/robots.txt"}:
        return "robots"
    if path in {"/sitemap.xml", f"{asset_base}/sitemap.xml"}:
        return "sitemap"
    if path.endswith(".svg") or path.endswith(".png") or path.endswith(".ico"):
        return "asset"
    return "other"


def client_log_fields(request):
    headers = request.headers
    forwarded_for = headers.get("x-forwarded-for", "")
    forwarded_ip = forwarded_for.split(",", 1)[0].strip()
    cloudflare_ip = headers.get("cf-connecting-ip", "").strip()
    real_ip = headers.get("x-real-ip", "").strip()
    socket_ip = request.client.host if request.client else ""
    client_ip = cloudflare_ip or forwarded_ip or real_ip or socket_ip
    fields = {"client.address": client_ip}
    user_agent = headers.get("user-agent", "").strip()
    if socket_ip and socket_ip != client_ip:
        fields["client.nat.ip"] = socket_ip
    if forwarded_for:
        fields["http.request.header.x_forwarded_for"] = forwarded_for
    if cloudflare_ip:
        fields["http.request.header.cf_connecting_ip"] = cloudflare_ip
    if user_agent:
        fields["http.request.header.user_agent"] = user_agent
    cloudflare_geo_headers = {
        "cf-ipcountry": ("http.request.header.cf_ipcountry", "client.geo.country_iso_code"),
        "cf-ipcontinent": ("http.request.header.cf_ipcontinent", "client.geo.continent_code"),
        "cf-ipcity": ("http.request.header.cf_ipcity", "client.geo.city_name"),
        "cf-region": ("http.request.header.cf_region", "client.geo.region_name"),
        "cf-region-code": ("http.request.header.cf_region_code", "client.geo.region_iso_code"),
        "cf-postal-code": ("http.request.header.cf_postal_code", "client.geo.postal_code"),
        "cf-timezone": ("http.request.header.cf_timezone", "client.geo.timezone"),
        "cf-iplatitude": ("http.request.header.cf_iplatitude", "client.geo.location.lat"),
        "cf-iplongitude": ("http.request.header.cf_iplongitude", "client.geo.location.lon"),
        "cf-ray": ("http.request.header.cf_ray", None),
    }
    for header, (raw_field, ecs_field) in cloudflare_geo_headers.items():
        value = headers.get(header, "").strip()
        if not value:
            continue
        fields[raw_field] = value
        if ecs_field:
            fields[ecs_field] = value
    return fields


@contextmanager
def database_connection(app):
    pool = getattr(app.state, "database_pool", None)
    if pool is None:
        yield None
        return
    with pool.connection() as conn:
        yield conn


@contextmanager
def writable_database_connection(app):
    settings = app.state.settings
    pool = getattr(app.state, "database_pool", None)
    if pool is not None:
        with pool.connection() as conn:
            yield conn
        return
    conn = connect_database(settings.database, settings.database_url)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def region_statuses(settings, hours, conn=None):
    statuses = {}
    for metric_region in web.METRIC_REGIONS:
        incidents = load_incidents(
            settings.database,
            hours,
            settings.database_url,
            region=metric_region,
            conn=conn,
        )
        statuses[metric_region] = incident_status(incidents, hours)
    return statuses


def byte_response(body, content_type, status_code=200, cache_control=None, send_body=True):
    if not send_body:
        body = b""
    headers = {}
    if cache_control:
        headers["Cache-Control"] = cache_control
    if send_body:
        headers["Content-Length"] = str(len(body))
    return Response(body, status_code=status_code, media_type=content_type, headers=headers)


def json_response(payload, status_code=200, cache_control=None, send_body=True):
    body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return byte_response(
        body,
        "application/json; charset=utf-8",
        status_code=status_code,
        cache_control=cache_control,
        send_body=send_body,
    )


def html_response(body, status_code=200, cache_control="no-store", send_body=True, headers=None):
    response = byte_response(
        body.encode("utf-8"),
        "text/html; charset=utf-8",
        status_code=status_code,
        cache_control=cache_control,
        send_body=send_body,
    )
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


def api_error(message, code="error", status_code=400, send_body=True):
    return json_response(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
        cache_control="no-store",
        send_body=send_body,
    )


def comment_event_key_from_path(path):
    prefix = "/api/v1/incidents/"
    suffix = "/comments"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    return unquote(path[len(prefix) : -len(suffix)])


def media_upload_path_parts(path):
    prefix = "/api/v1/incidents/"
    if not path.startswith(prefix):
        return None
    remainder = path[len(prefix) :]
    if remainder.endswith("/media/uploads"):
        return unquote(remainder[: -len("/media/uploads")]), "create", None
    marker = "/media/"
    if marker not in remainder or not remainder.endswith("/finalize"):
        return None
    event_key, media_part = remainder.split(marker, 1)
    try:
        media_id = int(media_part[: -len("/finalize")])
    except ValueError:
        return None
    return unquote(event_key), "finalize", media_id


def media_id_from_path(path, admin=False, settings=None):
    if admin and settings is not None:
        base = normalize_base_path(settings.base_path)
        prefix = "/admin/media/" if base == "/" else f"{base}/admin/media/"
    else:
        prefix = "/admin/media/" if admin else "/api/v1/media/"
    if not path.startswith(prefix):
        return None
    try:
        return int(path[len(prefix) :])
    except ValueError:
        return None


def admin_enabled(settings):
    return bool(settings.admin_username and settings.admin_password)


ADMIN_SESSION_COOKIE = "crestmap_admin_session"
ADMIN_LOGIN_ATTEMPT_LIMIT = 10
ADMIN_LOGIN_WINDOW_SECONDS = 15 * 60


def admin_login_path(settings):
    base = normalize_base_path(settings.base_path)
    return "/admin/login" if base == "/" else f"{base}/admin/login"


def admin_logout_path(settings):
    base = normalize_base_path(settings.base_path)
    return "/admin/logout" if base == "/" else f"{base}/admin/logout"


def admin_session_path(settings):
    base = normalize_base_path(settings.base_path)
    return "/admin/session" if base == "/" else f"{base}/admin/session"


def admin_sessions_path(settings):
    return admin_session_path(settings) + "s"


def current_admin_session(request):
    if not hasattr(request.state, "admin_session"):
        settings = request.app.state.settings
        token = request.cookies.get(ADMIN_SESSION_COOKIE, "")
        request.state.admin_session = None
        if admin_enabled(settings) and valid_admin_session_token(settings, token):
            with writable_database_connection(request.app) as conn:
                request.state.admin_session = load_session(conn, settings, token)
    return request.state.admin_session


def set_admin_session_cookie(response, request, token, expires_at):
    response.set_cookie(
        ADMIN_SESSION_COOKIE, token,
        max_age=max(0, expires_at - int(time.time())), path="/",
        secure=request_uses_https(request), httponly=True, samesite="strict",
    )


def request_uses_https(request):
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    return forwarded_proto == "https" or request.url.scheme == "https"


def admin_unauthorized(send_body=True):
    return api_error("admin authentication required", "unauthorized", 401, send_body=send_body)


def admin_authorized(request):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return False
    if current_admin_session(request):
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1], validate=True).decode("utf-8")
        except Exception:
            return False
        username, separator, password = decoded.partition(":")
        return bool(
            separator
            and secrets.compare_digest(username, settings.admin_username or "")
            and secrets.compare_digest(password, settings.admin_password or "")
        )
    return False


def safe_admin_next(settings, value):
    fallback = map_path(settings)
    if not value or not value.startswith("/") or value.startswith("//"):
        return fallback
    parsed = urlsplit(value)
    base = normalize_base_path(settings.base_path)
    within_base = base == "/" or parsed.path == base or parsed.path.startswith(f"{base}/")
    if parsed.scheme or parsed.netloc or "\\" in parsed.path or not within_base:
        return fallback
    if parsed.path in {admin_login_path(settings), admin_logout_path(settings)}:
        return fallback
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def admin_login_redirect(request):
    settings = request.app.state.settings
    location = f"{admin_login_path(settings)}?{urlencode({'next': request_target(request)})}"
    return Response(status_code=303, headers={"Location": location, "Cache-Control": "no-store"})


def admin_login_attempt_key(request):
    return str(client_log_fields(request).get("client.address") or "unknown")


def admin_login_attempts(request, record_failure=False):
    now = time.time()
    key = admin_login_attempt_key(request)
    attempts = request.app.state.admin_login_attempts
    recent = [
        attempted_at
        for attempted_at in attempts.get(key, [])
        if now - attempted_at < ADMIN_LOGIN_WINDOW_SECONDS
    ]
    if record_failure:
        recent.append(now)
    attempts[key] = recent
    return len(recent)


def admin_path(settings):
    base = normalize_base_path(settings.base_path)
    return "/admin/comments" if base == "/" else f"{base}/admin/comments"


def map_path(settings):
    base = normalize_base_path(settings.base_path)
    return "/" if base == "/" else base


def hidden_event_key_from_path(path):
    prefix = "/api/v1/incidents/"
    suffix = "/hidden-details"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    return unquote(path[len(prefix) : -len(suffix)])


def admin_status_from_request(request):
    status = (_query(request).get("status") or ["pending"])[0]
    return status if status in {"pending", "approved", "rejected"} else "pending"


def same_origin_admin_post(request):
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    settings = request.app.state.settings
    expected_origins = set()
    if settings.public_url:
        public = urlsplit(settings.public_url)
        if public.scheme and public.netloc:
            expected_origins.add(f"{public.scheme}://{public.netloc}")
    host = request.headers.get("host", request.url.netloc)
    expected_origins.add(f"{request.url.scheme}://{host}")
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or host).split(",", 1)[0].strip()
    if forwarded_proto and forwarded_host:
        expected_origins.add(f"{forwarded_proto}://{forwarded_host}")
    for value in (origin, referer):
        if value:
            parsed = urlsplit(value)
            if f"{parsed.scheme}://{parsed.netloc}" not in expected_origins:
                return False
    return True


def build_admin_login_html(settings, next_path, error=""):
    error_html = f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Crestmap Admin Login</title>
  <style>
    body {{ min-height: 100vh; margin: 0; display: grid; place-items: center; padding: 20px; box-sizing: border-box; color: #1d252a; background: #f2f5ef; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ box-sizing: border-box; width: min(100%, 380px); padding: 24px; border: 1px solid #d6ded2; border-radius: 14px; background: #fff; box-shadow: 0 10px 30px rgba(24, 42, 29, 0.09); }}
    h1 {{ margin: 0 0 6px; font-size: 25px; }}
    p {{ margin: 0 0 20px; color: #59655d; line-height: 1.45; }}
    label {{ display: block; margin-top: 13px; color: #35413a; font-size: 13px; font-weight: 800; }}
    input {{ width: 100%; min-height: 42px; margin-top: 6px; padding: 8px 10px; box-sizing: border-box; border: 1px solid #cbd5ca; border-radius: 8px; font: inherit; }}
    input:focus {{ border-color: #2b7c4a; outline: 3px solid rgba(43, 124, 74, 0.15); }}
    .remember {{ display: flex; align-items: center; gap: 10px; line-height: 1.4; }}
    .remember input {{ width: 20px; min-height: 20px; margin: 0; flex-shrink: 0; }}
    button {{ width: 100%; min-height: 44px; margin-top: 18px; border: 0; border-radius: 8px; color: #fff; background: #2b7c4a; font: inherit; font-weight: 850; cursor: pointer; }}
    .error {{ margin-bottom: 14px; padding: 10px 12px; border: 1px solid #e1b2b2; border-radius: 8px; color: #8f2525; background: #fff1f1; }}
    .back {{ display: block; margin-top: 16px; color: #2b6f45; text-align: center; }}
  </style>
</head>
<body>
  <main>
    <h1>Admin login</h1>
    <p>Sign in to moderate comments and view incident details removed from later CHP snapshots.</p>
    {error_html}
    <form method="post" action="{html.escape(admin_login_path(settings))}">
      <input type="hidden" name="next" value="{html.escape(next_path)}">
      <label>Username<input name="username" autocomplete="username" required autofocus></label>
      <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
      <label class="remember"><input type="checkbox" name="remember" value="yes">Remember this device for {max(1, settings.admin_remember_days)} days</label>
      <button type="submit">Sign in</button>
    </form>
    <a class="back" href="{html.escape(map_path(settings))}">Back to map</a>
  </main>
</body>
</html>"""


def handle_admin_login_get(request, send_body=True, error=""):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404, send_body=send_body)
    next_path = safe_admin_next(settings, (_query(request).get("next") or [""])[0])
    if admin_authorized(request):
        return Response(status_code=303, headers={"Location": next_path, "Cache-Control": "no-store"})
    return html_response(
        build_admin_login_html(settings, next_path, error),
        status_code=401 if error else 200,
        cache_control="no-store",
        send_body=send_body,
    )


def handle_admin_session_get(request, send_body=True):
    settings = request.app.state.settings
    authenticated = admin_enabled(settings) and admin_authorized(request)
    return json_response(
        {
            "authenticated": authenticated,
            "admin_incidents_url": map_path(settings) if authenticated else None,
        },
        cache_control="no-store",
        send_body=send_body,
    )


async def handle_admin_login_post(request):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404)
    if not same_origin_admin_post(request):
        return byte_response(b"Forbidden\n", "text/plain; charset=utf-8", status_code=403)
    if admin_login_attempts(request) >= ADMIN_LOGIN_ATTEMPT_LIMIT:
        next_path = safe_admin_next(settings, (_query(request).get("next") or [""])[0])
        return html_response(
            build_admin_login_html(settings, next_path, "Too many failed attempts. Try again later."),
            status_code=429,
            cache_control="no-store",
        )
    raw_body = (await request.body()).decode("utf-8", errors="replace")
    fields = {key: values[-1] for key, values in parse_qs(raw_body).items()}
    next_path = safe_admin_next(settings, fields.get("next"))
    username = fields.get("username") or ""
    password = fields.get("password") or ""
    valid = secrets.compare_digest(username, settings.admin_username or "") and secrets.compare_digest(
        password, settings.admin_password or ""
    )
    if not valid:
        admin_login_attempts(request, record_failure=True)
        web.log_event(
            "warning",
            "Admin login rejected",
            **{
                "event.action": "admin_login",
                "event.outcome": "failure",
                **client_log_fields(request),
            },
        )
        return html_response(
            build_admin_login_html(settings, next_path, "Incorrect username or password."),
            status_code=401,
            cache_control="no-store",
        )
    request.app.state.admin_login_attempts.pop(admin_login_attempt_key(request), None)
    with writable_database_connection(request.app) as conn:
        previous = load_session(conn, settings, request.cookies.get(ADMIN_SESSION_COOKIE, ""))
        if previous:
            revoke_sessions(conn, settings, session_id=previous["session_id"])
        token, session = create_session(
            conn, settings, remembered=fields.get("remember") == "yes",
            user_agent=request.headers.get("user-agent", ""),
        )
    response = Response(status_code=303, headers={"Location": next_path, "Cache-Control": "no-store"})
    set_admin_session_cookie(response, request, token, session["expires_at"])
    web.log_event(
        "info",
        "Admin login completed",
        **{
            "event.action": "admin_login",
            "event.outcome": "success",
            **client_log_fields(request),
        },
    )
    return response


async def handle_admin_logout_post(request):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404)
    if not same_origin_admin_post(request):
        return byte_response(b"Forbidden\n", "text/plain; charset=utf-8", status_code=403)
    session = current_admin_session(request)
    if session:
        with writable_database_connection(request.app) as conn:
            revoke_sessions(conn, settings, session_id=session["session_id"])
    response = Response(status_code=303, headers={"Location": map_path(settings), "Cache-Control": "no-store"})
    response.delete_cookie(
        ADMIN_SESSION_COOKIE,
        path="/",
        secure=request_uses_https(request),
        httponly=True,
        samesite="strict",
    )
    return response


def handle_admin_activity_post(request):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404)
    if request.headers.get("x-crestmap-activity") != "1" or not same_origin_admin_post(request):
        return byte_response(b"Forbidden\n", "text/plain; charset=utf-8", status_code=403)
    token = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    with writable_database_connection(request.app) as conn:
        session = renew_session(conn, settings, token)
    if not session:
        return admin_unauthorized()
    response = json_response({"authenticated": True}, cache_control="no-store")
    set_admin_session_cookie(response, request, token, session["expires_at"])
    return response


def handle_admin_sessions_get(request, send_body=True):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404)
    if not admin_authorized(request):
        return admin_login_redirect(request)
    current = current_admin_session(request)
    with writable_database_connection(request.app) as conn:
        sessions = list_sessions(conn, settings)
    action = html.escape(admin_sessions_path(settings))
    cards = []
    for session in sessions:
        is_current = current and session["session_id"] == current["session_id"]
        label = "This device" if is_current else "Other device"
        mode = "Remembered" if session["remembered"] else "Standard"
        def timestamp(field):
            return dt.datetime.fromtimestamp(session[field], dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        cards.append(f"""<article>
          <h2>{label} · {mode}</h2>
          <p class="device">{html.escape(session['user_agent'] or 'Unknown browser')}</p>
          <p>Signed in: {timestamp('created_at')}<br>
          Last interaction: {timestamp('last_activity_at')}<br>
          Expires: {timestamp('expires_at')}<br>
          Maximum lifetime ends: {timestamp('absolute_expires_at')}</p>
          <form method="post" action="{action}">
            <input type="hidden" name="session_id" value="{session['session_id']}">
            <button name="action" value="revoke">{'Log out this device' if is_current else 'Revoke session'}</button>
          </form></article>""")
    body = f"""<!doctype html><html lang="en"><head>
      <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Crestmap Admin Sessions</title>
      <style>
      body {{ font: 16px system-ui, sans-serif; background: #f2f5ef; color: #1d252a; margin: 0; padding: 20px; }}
      main {{ max-width: 720px; margin: auto; }}
      article {{ background: white; border: 1px solid #d6ded2; border-radius: 12px; padding: 16px; margin: 16px 0; }}
      h2 {{ font-size: 18px; }} p {{ line-height: 1.5; }} .device {{ overflow-wrap: anywhere; }}
      button, a {{ font: inherit; }} button {{ min-height: 44px; margin: 4px 0; padding: 8px 12px; cursor: pointer; }}
      </style></head><body><main>
      <h1>Admin sessions</h1>
      <p><a href="{html.escape(admin_path(settings))}">Back to admin tools</a></p>
      <p>Standard sessions renew only when you interact with an admin-enabled page.
      Remembered devices have a fixed maximum lifetime. Revoking a session signs that browser out on its next request.</p>
      <form method="post" action="{action}">
        <button name="action" value="others">Log out all other devices</button>
        <button name="action" value="all">Log out everywhere</button>
      </form>
      {''.join(cards) or '<p>No browser sessions.</p>'}
      {admin_activity_script(settings.base_path)}
      </main></body></html>"""
    return html_response(body, cache_control="no-store", send_body=send_body)


async def handle_admin_sessions_post(request):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404)
    if not admin_authorized(request):
        return admin_unauthorized()
    if not (request.headers.get("origin") or request.headers.get("referer")) or not same_origin_admin_post(request):
        return byte_response(b"Forbidden\n", "text/plain; charset=utf-8", status_code=403)
    fields = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    action = (fields.get("action") or [""])[-1]
    target = (fields.get("session_id") or [""])[-1]
    current = current_admin_session(request)
    if action not in {"all", "others", "revoke"} or (action == "revoke" and not target):
        return byte_response(b"Invalid action\n", "text/plain; charset=utf-8", status_code=400)
    with writable_database_connection(request.app) as conn:
        revoke_sessions(
            conn, settings, session_id=target if action == "revoke" else None,
            except_id=current["session_id"] if action == "others" and current else None,
        )
    logged_out = action == "all" or (action == "revoke" and current and target == current["session_id"])
    response = Response(status_code=303, headers={
        "Location": map_path(settings) if logged_out else admin_sessions_path(settings),
        "Cache-Control": "no-store",
    })
    if logged_out:
        response.delete_cookie(ADMIN_SESSION_COOKIE, path="/", secure=request_uses_https(request), httponly=True, samesite="strict")
    return response


def push_config_path(settings):
    base = normalize_base_path(settings.base_path)
    return "/api/v1/push/config" if base == "/" else f"{base}/api/v1/push/config"


def push_subscription_path(settings):
    base = normalize_base_path(settings.base_path)
    return "/api/v1/push/subscription" if base == "/" else f"{base}/api/v1/push/subscription"


def pwa_manifest(settings):
    base = normalize_base_path(settings.base_path)
    start_url = "/" if base == "/" else f"{base}/"
    return {
        "id": start_url,
        "name": "Crestmap Incidents",
        "short_name": "Crestmap",
        "description": "Live CHP incidents and WildWeb dispatch reports for Angeles forest and Malibu roads.",
        "start_url": start_url,
        "scope": start_url,
        "display": "standalone",
        "background_color": "#f6f7f4",
        "theme_color": "#18392b",
        "icons": [
            {
                "src": f"{base.rstrip('/')}/apple-touch-icon-180x180.png" or "/apple-touch-icon-180x180.png",
                "sizes": "180x180",
                "type": "image/png",
                "purpose": "any",
            }
        ],
    }


SERVICE_WORKER_JS = r"""const DEFAULT_URL = "/";
const RELEASE_VERSION = "__CRESTMAP_RELEASE_VERSION__";
const CACHE_PREFIX = "crestmap-app-shell-";
const CACHE_NAME = `${CACHE_PREFIX}${RELEASE_VERSION}`;
const SHELL_ASSETS = [
  "/manifest.webmanifest",
  "/apple-touch-icon-180x180.png",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
];
const SHELL_ASSET_URLS = new Set(SHELL_ASSETS.map((url) => new URL(url, self.location.origin).href));
const BADGE_DATABASE = "crestmap-notification-state";
const BADGE_STORE = "state";
const BADGE_KEY = "unread-count";
const MAX_BADGE_COUNT = 99;

async function cacheApplicationShell() {
  try {
    const cache = await caches.open(CACHE_NAME);
    const shellUrl = `${DEFAULT_URL}?app-shell=${encodeURIComponent(RELEASE_VERSION)}`;
    const shellResponse = await fetch(shellUrl, { cache: "reload", credentials: "omit" });
    if (!shellResponse.ok) throw new Error(`Could not cache Crestmap shell (${shellResponse.status})`);
    await cache.put(DEFAULT_URL, shellResponse);
    await Promise.all(SHELL_ASSETS.map(async (url) => {
      const response = await fetch(url, { cache: "reload", credentials: "omit", mode: "cors" });
      if (!response.ok) throw new Error(`Could not cache ${url} (${response.status})`);
      await cache.put(url, response);
    }));
  } catch (error) {
    await caches.delete(CACHE_NAME);
    throw error;
  }
}

async function handleNavigation(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    const cacheControl = response.headers.get("Cache-Control") || "";
    if (response.ok && !cacheControl.includes("no-store")) {
      await cache.put(request, response.clone());
    }
    if (response.ok) return response;
    return (await cache.match(request)) || (await cache.match(DEFAULT_URL)) || response;
  } catch (_error) {
    return (await cache.match(request)) || (await cache.match(DEFAULT_URL));
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(cacheApplicationShell().then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
      .map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  if (event.request.mode === "navigate") {
    event.respondWith(handleNavigation(event.request));
    return;
  }
  if (SHELL_ASSET_URLS.has(event.request.url)) {
    event.respondWith(caches.match(event.request, { ignoreVary: true })
      .then((cached) => cached || fetch(event.request)));
  }
});

function openBadgeDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(BADGE_DATABASE, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(BADGE_STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function changeBadgeCount(reset = false) {
  const database = await openBadgeDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(BADGE_STORE, "readwrite");
    const store = transaction.objectStore(BADGE_STORE);
    const request = store.get(BADGE_KEY);
    let nextCount = 0;
    request.onsuccess = () => {
      nextCount = reset ? 0 : Math.min(Number(request.result || 0) + 1, MAX_BADGE_COUNT);
      store.put(nextCount, BADGE_KEY);
    };
    transaction.oncomplete = () => {
      database.close();
      resolve(nextCount);
    };
    transaction.onerror = () => {
      database.close();
      reject(transaction.error);
    };
  });
}

async function incrementAppBadge() {
  const count = await changeBadgeCount(false);
  if ("setAppBadge" in navigator) await navigator.setAppBadge(count);
}

async function clearAppBadge() {
  await changeBadgeCount(true);
  if ("clearAppBadge" in navigator) await navigator.clearAppBadge();
}

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_error) {
    payload = { title: "New incident", body: "Open Crestmap for details.", url: DEFAULT_URL };
  }
  const title = payload.title || "New incident";
  const options = {
    body: payload.body || "Open Crestmap for details.",
    icon: "/apple-touch-icon.png",
    badge: "/apple-touch-icon.png",
    tag: payload.tag || (payload.event_key ? `chp-${payload.event_key}` : "chp-incident"),
    renotify: true,
    data: { url: payload.url || DEFAULT_URL }
  };
  event.waitUntil(Promise.all([
    self.registration.showNotification(title, options),
    incrementAppBadge().catch(() => {})
  ]));
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "CLEAR_BADGE") event.waitUntil(clearAppBadge().catch(() => {}));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || DEFAULT_URL, self.location.origin).href;
  event.waitUntil((async () => {
    await clearAppBadge().catch(() => {});
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if ("navigate" in client) await client.navigate(target);
      return client.focus();
    }
    return clients.openWindow(target);
  })());
});
"""


def service_worker_js(settings):
    return SERVICE_WORKER_JS.replace(
        '"__CRESTMAP_RELEASE_VERSION__"',
        json.dumps(settings.service_version),
    )


def handle_push_config_get(request, send_body=True):
    settings = request.app.state.settings
    return json_response(
        {
            "enabled": bool(settings.vapid_public_key),
            "public_key": settings.vapid_public_key,
            "defaults": {
                "sources": DEFAULT_SOURCES,
                "regions": DEFAULT_REGIONS,
                "categories": DEFAULT_CATEGORIES,
            },
        },
        cache_control="no-store",
        send_body=send_body,
    )


async def handle_push_subscription_post(request):
    settings = request.app.state.settings
    if not settings.vapid_public_key:
        return api_error("push notifications are not configured", "push_unavailable", 503)
    if not same_origin_admin_post(request):
        return api_error("cross-origin request rejected", "forbidden", 403)
    try:
        payload = await request.json()
    except Exception:
        return api_error("invalid JSON body", "invalid_json", 400)
    if not isinstance(payload, dict):
        return api_error("request body must be an object", "invalid_request", 400)
    action = str(payload.get("action") or "subscribe").lower()
    subscription = payload.get("subscription") or {}
    endpoint = str(subscription.get("endpoint") or "") if isinstance(subscription, dict) else ""
    try:
        with writable_database_connection(request.app) as conn:
            if action in {"subscribe", "update"}:
                result = save_subscription(conn, payload, request.headers.get("user-agent", ""))
                return json_response({"subscribed": True, **result}, 201 if action == "subscribe" else 200)
            if action == "unsubscribe":
                deactivate_subscription(conn, endpoint)
                return json_response({"subscribed": False})
            if action == "status":
                preferences = subscription_preferences(conn, endpoint)
                return json_response({"subscribed": bool(preferences), "preferences": preferences})
            if action == "test":
                result = enqueue_test_notification(conn, endpoint, settings.public_url)
                return json_response(result, 202)
    except PushValidationError as exc:
        return api_error(str(exc), "invalid_subscription", 422)
    except Exception as exc:
        web.log_exception("Push subscription request failed", exc, **{"event.action": "push_subscription"})
        return api_error("push subscription could not be saved", "push_failed", 500)
    return api_error("unsupported action", "invalid_action", 422)


def dispatch_request(request, send_body=True):
    settings = request.app.state.settings
    path = _path(request)
    base_path = normalize_base_path(settings.base_path)
    asset_base = "" if base_path == "/" else base_path
    admin_mode = admin_enabled(settings) and admin_authorized(request)
    legacy_admin_incidents_path = "/admin/incidents" if base_path == "/" else f"{base_path}/admin/incidents"
    if path == legacy_admin_incidents_path:
        query = request.url.query
        location = map_path(settings) + (f"?{query}" if query else "")
        return Response(status_code=308, headers={"Location": location, "Cache-Control": "no-store"})
    map_paths = {"/", "/live_chp_map.html", base_path}
    summary_paths = {"/summary", f"{asset_base}/summary"}
    history_paths = {"/history", f"{asset_base}/history"}
    about_paths = {"/about", f"{asset_base}/about"}
    status_paths = {"/status.json", f"{asset_base}/status.json"}
    incidents_paths = {"/incidents.json", f"{asset_base}/incidents.json"}
    aircraft_paths = {"/api/v1/aircraft", f"{asset_base}/api/v1/aircraft"}
    robots_paths = {"/robots.txt", f"{asset_base}/robots.txt"}
    sitemap_paths = {"/sitemap.xml", f"{asset_base}/sitemap.xml"}
    metrics_paths = {"/metrics", f"{asset_base}/metrics"}
    manifest_paths = {"/manifest.webmanifest", f"{asset_base}/manifest.webmanifest"}
    service_worker_paths = {"/sw.js", f"{asset_base}/sw.js"}
    favicon_svg_paths = {"/favicon.svg", f"{asset_base}/favicon.svg"}
    favicon_ico_paths = {"/favicon.ico", f"{asset_base}/favicon.ico"}
    apple_touch_icon_paths = {
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
        "/apple-touch-icon-120x120.png",
        "/apple-touch-icon-120x120-precomposed.png",
        "/apple-touch-icon-152x152.png",
        "/apple-touch-icon-152x152-precomposed.png",
        "/apple-touch-icon-167x167.png",
        "/apple-touch-icon-167x167-precomposed.png",
        "/apple-touch-icon-180x180.png",
        "/apple-touch-icon-180x180-precomposed.png",
    }
    asset_paths = {
        f"{asset_base}/og-image.svg": ("image/svg+xml", web.OG_IMAGE_SVG.encode("utf-8")),
        f"{asset_base}/og-image.png": ("image/png", web.OG_IMAGE_PNG),
        "/og-image.png": ("image/png", web.OG_IMAGE_PNG),
        **{asset_path: ("image/png", web.APPLE_TOUCH_ICON_PNG) for asset_path in apple_touch_icon_paths},
        **{
            f"{asset_base}{asset_path}": ("image/png", web.APPLE_TOUCH_ICON_PNG)
            for asset_path in apple_touch_icon_paths
            if asset_base
        },
    }

    if path in {"/healthz", "/readyz"}:
        return byte_response(b"ok\n", "text/plain; charset=utf-8", send_body=send_body)

    if path in manifest_paths:
        return byte_response(
            json.dumps(pwa_manifest(settings), sort_keys=True).encode("utf-8"),
            "application/manifest+json; charset=utf-8",
            cache_control=web.ASSET_CACHE_CONTROL,
            send_body=send_body,
        )

    if path in service_worker_paths:
        response = byte_response(
            service_worker_js(settings).encode("utf-8"),
            "application/javascript; charset=utf-8",
            cache_control="no-cache",
            send_body=send_body,
        )
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    if path in favicon_svg_paths or path in favicon_ico_paths:
        try:
            with database_connection(request.app) as conn:
                active = web.favicon_active(
                    load_incidents(settings.database, settings.hours, settings.database_url, conn=conn)
                )
        except Exception as exc:
            web.log_exception(
                "Failed to render dynamic favicon",
                exc,
                **{
                    "event.action": "http_request",
                    "event.outcome": "failure",
                    "http.request.method": request.method,
                    "url.path": request_target(request),
                    "http.response.status_code": 500,
                    **client_log_fields(request),
                },
            )
            active = False
        if path in favicon_svg_paths:
            body = web.favicon_svg(active).encode("utf-8")
            content_type = "image/svg+xml"
        else:
            marker_color = web.ACTIVE_MARKER_RGB if active else web.CLEAR_MARKER_RGB
            body = web.make_touch_icon_png(marker_color)
            content_type = "image/png"
        return byte_response(body, content_type, cache_control=web.FAVICON_CACHE_CONTROL, send_body=send_body)

    if path in asset_paths:
        content_type, body = asset_paths[path]
        return byte_response(body, content_type, cache_control=web.ASSET_CACHE_CONTROL, send_body=send_body)

    if path in robots_paths:
        return byte_response(
            web.robots_txt(settings.base_path, settings.public_url),
            "text/plain; charset=utf-8",
            cache_control=web.DISCOVERY_CACHE_CONTROL,
            send_body=send_body,
        )

    if path in sitemap_paths:
        return byte_response(
            web.sitemap_xml(settings.base_path, settings.public_url),
            "application/xml; charset=utf-8",
            cache_control=web.DISCOVERY_CACHE_CONTROL,
            send_body=send_body,
        )

    if path in metrics_paths:
        try:
            with database_connection(request.app) as conn:
                pool = getattr(request.app.state, "database_pool", None)
                pool_stats = pool.get_stats() if pool is not None else None
                body = web.prometheus_metrics(
                    settings.database,
                    settings.database_url,
                    settings.hours,
                    conn=conn,
                    pool_stats=pool_stats,
                )
        except Exception as exc:
            web.log_exception(
                "Failed to render Prometheus metrics",
                exc,
                **{
                    "event.action": "http_request",
                    "event.outcome": "failure",
                    "http.request.method": request.method,
                    "url.path": request_target(request),
                    "http.response.status_code": 500,
                    **client_log_fields(request),
                },
            )
            return byte_response(
                b"failed to render metrics\n",
                "text/plain; charset=utf-8",
                status_code=500,
                send_body=send_body,
            )
        return byte_response(
            body,
            "text/plain; version=0.0.4; charset=utf-8",
            cache_control="no-store",
            send_body=send_body,
        )

    region = requested_region(request)

    if path in status_paths:
        try:
            hours = requested_hours(request, settings)
            with database_connection(request.app) as conn:
                incidents = load_incidents(settings.database, hours, settings.database_url, region=region, conn=conn)
                last_scrape = load_last_scrape_run(settings.database, settings.database_url, conn=conn)
                payload = {
                    **incident_status(incidents, hours),
                    "app_version": settings.service_version,
                    "region": region,
                    "region_statuses": region_statuses(settings, hours, conn=conn),
                    "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "last_scrape": last_scrape,
                }
        except Exception as exc:
            web.log_exception(
                "Failed to render CHP status",
                exc,
                **{
                    "event.action": "http_request",
                    "event.outcome": "failure",
                    "http.request.method": request.method,
                    "url.path": request_target(request),
                    "http.response.status_code": 500,
                    **client_log_fields(request),
                },
            )
            return byte_response(
                b'{"error":"failed to render status"}\n',
                "application/json; charset=utf-8",
                status_code=500,
                send_body=send_body,
            )
        return json_response(
            payload,
            cache_control="private, max-age=15, stale-while-revalidate=30",
            send_body=send_body,
        )

    if path in incidents_paths:
        try:
            hours = requested_hours(request, settings)
            with database_connection(request.app) as conn:
                incidents = load_incidents(settings.database, hours, settings.database_url, region=region, conn=conn)
                last_scrape = load_last_scrape_run(settings.database, settings.database_url, conn=conn)
                linked_incident = load_incident_by_key(
                    settings.database,
                    requested_incident_key(request),
                    settings.database_url,
                    region=region,
                    conn=conn,
                )
                current_region_statuses = region_statuses(settings, hours, conn=conn)
            incidents = include_linked_incident(incidents, linked_incident)
            payload = {
                "incidents": incidents,
                "status": {**incident_status(incidents, hours), "region": region},
                "region_statuses": current_region_statuses,
                "region": region,
                "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "last_scrape": last_scrape,
            }
        except Exception as exc:
            web.log_exception(
                "Failed to render CHP incidents API",
                exc,
                **{
                    "event.action": "http_request",
                    "event.outcome": "failure",
                    "http.request.method": request.method,
                    "url.path": request_target(request),
                    "http.response.status_code": 500,
                    **client_log_fields(request),
                },
            )
            return byte_response(
                b'{"error":"failed to render incidents"}\n',
                "application/json; charset=utf-8",
                status_code=500,
                send_body=send_body,
            )
        return json_response(payload, cache_control=web.INCIDENTS_CACHE_CONTROL, send_body=send_body)

    if path in aircraft_paths:
        now = dt.datetime.now(dt.timezone.utc)
        try:
            with writable_database_connection(request.app) as conn:
                aircraft = (
                    load_visible_aircraft(
                        conn,
                        now=now,
                        delay_seconds=settings.aircraft_display_delay_seconds,
                        max_age_seconds=settings.aircraft_max_age_seconds,
                        trail_age_seconds=settings.aircraft_trail_age_seconds,
                    )
                    if settings.aircraft_tracking_enabled and conn is not None
                    else []
                )
                tracker = load_tracker_status(conn) if conn is not None else None
        except Exception as exc:
            web.log_exception("Failed to render aircraft API", exc, **{"event.action": "aircraft_api"})
            return api_error("aircraft data is unavailable", "aircraft_unavailable", 503, send_body)
        return json_response(
            {
                "enabled": settings.aircraft_tracking_enabled,
                "aircraft": aircraft,
                "checked_at": now.isoformat(timespec="seconds"),
                "display_delay_seconds": settings.aircraft_display_delay_seconds,
                "max_age_seconds": settings.aircraft_max_age_seconds,
                "trail_age_seconds": settings.aircraft_trail_age_seconds,
                "tracker": {
                    "provider": tracker.get("provider"),
                    "last_success_at": tracker.get("last_success_at"),
                    "last_run_success": bool(tracker.get("last_run_success")),
                }
                if tracker
                else None,
            },
            cache_control="private, max-age=10",
            send_body=send_body,
        )

    if path not in map_paths and path not in summary_paths and path not in history_paths and path not in about_paths:
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404, send_body=send_body)

    try:
        generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        hours = requested_hours(request, settings)
        with database_connection(request.app) as conn:
            current_region_statuses = region_statuses(settings, hours, conn=conn)
            incidents = load_incidents(settings.database, hours, settings.database_url, region=region, conn=conn)
            last_scrape = load_last_scrape_run(settings.database, settings.database_url, conn=conn)
            linked_incident = load_incident_by_key(
                settings.database,
                requested_incident_key(request),
                settings.database_url,
                region=region,
                conn=conn,
            )
        incidents = include_linked_incident(incidents, linked_incident)
        if path in summary_paths:
            body = build_summary_html(
                incidents,
                generated_at,
                hours,
                base_path=settings.base_path,
                public_url=settings.public_url,
                google_analytics_id=settings.google_analytics_id,
                region=region,
                region_statuses=current_region_statuses,
                filters=summary_filters(request),
                admin_mode=admin_mode,
            ).encode("utf-8")
        elif path in history_paths:
            body = build_history_html(
                incidents,
                generated_at,
                hours,
                base_path=settings.base_path,
                public_url=settings.public_url,
                google_analytics_id=settings.google_analytics_id,
                filters=history_filters(request),
                region=region,
                region_statuses=current_region_statuses,
                admin_mode=admin_mode,
            ).encode("utf-8")
        elif path in about_paths:
            body = build_about_html(
                incidents,
                generated_at,
                hours,
                base_path=settings.base_path,
                public_url=settings.public_url,
                google_analytics_id=settings.google_analytics_id,
                region=region,
                region_statuses=current_region_statuses,
                admin_mode=admin_mode,
            ).encode("utf-8")
        else:
            body = build_html(
                incidents,
                generated_at,
                hours,
                base_path=settings.base_path,
                public_url=settings.public_url,
                google_analytics_id=settings.google_analytics_id,
                map_label=region_label(region),
                region=region,
                region_statuses=current_region_statuses,
                last_scrape=last_scrape,
                admin_mode=admin_mode,
                admin_details_base="/api/v1/incidents",
                media_enabled=media_enabled(settings),
                media_max_video_bytes=settings.media_max_video_bytes,
                media_max_video_seconds=settings.media_max_video_seconds,
                aircraft_tracking_enabled=settings.aircraft_tracking_enabled,
                app_version=settings.service_version,
            ).encode("utf-8")
    except Exception as exc:
        web.log_exception(
            "Failed to render CHP live map",
            exc,
            **{
                "event.action": "http_request",
                "event.outcome": "failure",
                "http.request.method": request.method,
                "url.path": request_target(request),
                "http.response.status_code": 500,
                **client_log_fields(request),
            },
        )
        return byte_response(
            f"failed to render map: {exc}\n".encode("utf-8"),
            "text/plain; charset=utf-8",
            status_code=500,
            send_body=send_body,
        )
    cache_control = "no-store" if admin_mode else web.MAP_CACHE_CONTROL
    response = byte_response(body, "text/html; charset=utf-8", cache_control=cache_control, send_body=send_body)
    response.headers["Vary"] = "Cookie, Authorization"
    return response


def handle_admin_hidden_details_get(request, event_key, send_body=True):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404, send_body=send_body)
    if not admin_authorized(request):
        return admin_unauthorized(send_body=send_body)
    region = requested_region(request)
    try:
        with database_connection(request.app) as conn:
            entries = load_removed_detail_entries(
                settings.database,
                event_key,
                settings.database_url,
                region=region,
                conn=conn,
            )
    except Exception as exc:
        web.log_exception(
            "Failed to load admin hidden incident details",
            exc,
            **{
                "event.action": "admin_hidden_details",
                "event.outcome": "failure",
                "url.path": request_target(request),
                **client_log_fields(request),
            },
        )
        return api_error("failed to load hidden details", status_code=500, send_body=send_body)
    if entries is None:
        return api_error("incident not found", "not_found", 404, send_body=send_body)
    return json_response(
        {"meta": {"event_key": event_key, "count": len(entries)}, "data": entries},
        cache_control="no-store",
        send_body=send_body,
    )


def build_admin_comments_html(
    rows,
    counts,
    status,
    message="",
    admin_url="/admin/comments",
    admin_incidents_url="/",
    admin_logout_url="/admin/logout",
    admin_sessions_url="/admin/sessions",
    activity_script="",
):
    tabs = []
    for tab_status, label in (("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")):
        count = counts.get(tab_status, 0)
        selected = tab_status == status
        tabs.append(
            '<a class="tab{}" href="{}?status={}">{} <span>{}</span></a>'.format(
                " is-active" if selected else "",
                html.escape(admin_url),
                html.escape(tab_status),
                html.escape(label),
                count,
            )
        )
    cards = []
    for row in rows:
        incident_url = (
            f"{html.escape(admin_incidents_url)}?region={html.escape(row.get('region') or 'forest')}"
            f"&incident={html.escape(row['event_key'])}"
        )
        actions = []
        if row["status"] != "approved":
            actions.append(("approve", "Approve"))
        if row["status"] != "rejected":
            actions.append(("reject", "Reject"))
        actions.append(("delete", "Delete"))
        action_html = "".join(
            f"""
            <form method="post" action="{html.escape(admin_url)}">
              <input type="hidden" name="id" value="{int(row['id'])}">
              <input type="hidden" name="status" value="{html.escape(status)}">
              <button class="action {html.escape(action)}" name="action" value="{html.escape(action)}">{html.escape(label)}</button>
            </form>
            """
            for action, label in actions
        )
        meta = " · ".join(
            part
            for part in [
                row.get("created_at") or "",
                row.get("display_name") or "Anonymous",
            ]
            if part
        )
        submitter_ip = row.get("cf_connecting_ip") or "unknown"
        submitter_country = row.get("cf_country") or "unknown"
        incident_title = " · ".join(
            part
            for part in [
                row.get("type") or "Unknown incident",
                row.get("location") or "",
                f"#{row.get('incident_no')}" if row.get("incident_no") else "",
            ]
            if part
        )
        contact = (
            f'<div class="contact">Contact: {html.escape(row["contact"])}</div>'
            if row.get("contact")
            else ""
        )
        media_items = []
        for attachment in row.get("media", []):
            media_url = html.escape(attachment["url"])
            if attachment["kind"] == "image":
                preview = f'<img src="{media_url}" alt="Submitted incident photo" loading="lazy">'
            else:
                preview = f'<video src="{media_url}" controls preload="metadata" playsinline></video>'
            size_mb = attachment["size"] / (1024 * 1024)
            media_items.append(
                f'<div class="media-item">{preview}<div class="media-meta">'
                f'{html.escape(attachment["filename"])} · {size_mb:.1f} MB</div></div>'
            )
        media_html = f'<div class="media-grid">{"".join(media_items)}</div>' if media_items else ""
        cards.append(
            f"""
            <article class="comment-card">
              <div class="comment-top">
                <div>
                  <div class="comment-id">#{int(row['id'])} · {html.escape(row['status'])}</div>
                  <h2>{html.escape(incident_title)}</h2>
                  <a class="incident-link" href="{incident_url}" target="_blank" rel="noreferrer">{html.escape(row['event_key'])}</a>
                </div>
                <div class="actions">{action_html}</div>
              </div>
              <div class="meta">{html.escape(meta)}</div>
              <div class="contact">Submitter IP: {html.escape(submitter_ip)} · Country: {html.escape(submitter_country)}</div>
              {contact}
              <p>{html.escape(row.get("body") or "")}</p>
              {media_html}
              <details>
                <summary>User agent</summary>
                <code>{html.escape(row.get("user_agent") or "")}</code>
              </details>
            </article>
            """
        )
    if not cards:
        cards.append('<div class="empty-admin">No comments in this queue.</div>')
    message_html = f'<div class="notice">{html.escape(message)}</div>' if message else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Crestmap Comment Moderation</title>
  <style>
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #1d252a; background: #f6f8f3; }}
    header, main {{ max-width: 1040px; margin: 0 auto; padding: 18px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-end; }}
    h1 {{ margin: 0; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 4px 0; font-size: 18px; letter-spacing: 0; }}
    .tabs {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .tabs form {{ margin: 0; }}
    .tab {{ padding: 8px 12px; border: 1px solid #d7ded2; border-radius: 8px; color: #35413a; text-decoration: none; font: inherit; font-weight: 800; background: #fff; cursor: pointer; }}
    .tab.is-active {{ color: #fff; border-color: #2b7c4a; background: #2b7c4a; }}
    .tab span {{ opacity: 0.8; }}
    .notice {{ margin-bottom: 12px; padding: 10px 12px; border: 1px solid #bdd4c0; border-radius: 8px; background: #edf7ee; color: #1f6840; font-weight: 700; }}
    .comment-card, .empty-admin {{ margin-bottom: 12px; padding: 14px; border: 1px solid #dce3d7; border-radius: 10px; background: #fff; box-shadow: 0 1px 2px rgba(21, 35, 25, 0.04); }}
    .comment-top {{ display: flex; justify-content: space-between; gap: 12px; }}
    .comment-id, .meta, .contact {{ color: #58645d; font-size: 13px; line-height: 1.35; }}
    .incident-link {{ color: #1f6840; overflow-wrap: anywhere; }}
    .media-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 12px 0; }}
    .media-item {{ overflow: hidden; border: 1px solid #dce3d7; border-radius: 9px; background: #f7f9f5; }}
    .media-item img, .media-item video {{ display: block; width: 100%; max-height: 320px; object-fit: contain; background: #111; }}
    .media-meta {{ padding: 7px 9px; color: #58645d; font-size: 12px; overflow-wrap: anywhere; }}
    p {{ white-space: pre-wrap; line-height: 1.45; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; align-content: flex-start; }}
    form {{ margin: 0; }}
    .action {{ min-height: 32px; padding: 6px 10px; border: 1px solid #ccd8cc; border-radius: 7px; font: inherit; font-weight: 800; cursor: pointer; background: #f8faf6; }}
    .approve {{ color: #fff; border-color: #2b7c4a; background: #2b7c4a; }}
    .reject {{ color: #72510e; border-color: #dfc06c; background: #fff7d8; }}
    .delete {{ color: #9f2525; border-color: #e2b9b9; background: #fff1f1; }}
    details {{ margin-top: 10px; color: #58645d; }}
    code {{ display: block; margin-top: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }}
    @media (max-width: 720px) {{
      header, .comment-top {{ display: block; }}
      .actions {{ justify-content: flex-start; margin-top: 10px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Comment Moderation</h1>
      <div class="meta">Only approved comments are shown publicly.</div>
    </div>
    <nav class="tabs">
      <a class="tab" href="{html.escape(admin_incidents_url)}">Incident map</a>
      <a class="tab" href="{html.escape(admin_sessions_url)}">Sessions / remembered devices</a>
      {"".join(tabs)}
      <form method="post" action="{html.escape(admin_logout_url)}"><button class="tab" type="submit">Log out</button></form>
    </nav>
  </header>
  <main>
    {message_html}
    {"".join(cards)}
  </main>
  {activity_script}
</body>
</html>"""


def handle_admin_comments_get(request, send_body=True, message=""):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404, send_body=send_body)
    if not admin_authorized(request):
        return admin_login_redirect(request)
    status = admin_status_from_request(request)
    try:
        with writable_database_connection(request.app) as conn:
            rows = moderation_rows(conn, status=status, limit=100)
            attachments = media_for_comments(conn, [row["id"] for row in rows])
            for row in rows:
                row["media"] = [
                    public_media(
                        media,
                        admin=True,
                        admin_base=f"{admin_path(settings).rsplit('/', 1)[0]}/media",
                    )
                    for media in attachments.get(int(row["id"]), [])
                    if media["status"] in {"pending", "approved"}
                ]
            counts = comment_status_counts(conn)
        body = build_admin_comments_html(
            rows,
            counts,
            status,
            message=message,
            admin_url=admin_path(settings),
            admin_incidents_url=map_path(settings),
            admin_logout_url=admin_logout_path(settings),
            admin_sessions_url=admin_sessions_path(settings),
            activity_script=admin_activity_script(settings.base_path),
        )
        return html_response(body, cache_control="no-store", send_body=send_body)
    except Exception as exc:
        web.log_exception(
            "Failed to render comment moderation",
            exc,
            **{
                "event.action": "admin_comments",
                "event.outcome": "failure",
                "http.request.method": request.method,
                "url.path": request_target(request),
                "http.response.status_code": 500,
                **client_log_fields(request),
            },
        )
        return byte_response(b"failed to render admin comments\n", "text/plain; charset=utf-8", status_code=500)


async def handle_admin_comments_post(request):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404)
    if not admin_authorized(request):
        return admin_unauthorized()
    if not same_origin_admin_post(request):
        return byte_response(b"Forbidden\n", "text/plain; charset=utf-8", status_code=403)
    raw_body = (await request.body()).decode("utf-8", errors="replace")
    fields = {key: values[-1] for key, values in parse_qs(raw_body).items()}
    action = fields.get("action")
    status = fields.get("status") if fields.get("status") in {"pending", "approved", "rejected"} else "pending"
    try:
        comment_id = int(fields.get("id", ""))
    except ValueError:
        return byte_response(b"Bad Request\n", "text/plain; charset=utf-8", status_code=400)
    if action not in {"approve", "reject", "delete"}:
        return byte_response(b"Bad Request\n", "text/plain; charset=utf-8", status_code=400)
    try:
        object_keys = []
        with writable_database_connection(request.app) as conn:
            if action == "approve":
                set_comment_status(conn, comment_id, "approved")
                set_media_status(conn, comment_id, "approved")
            elif action == "reject":
                object_keys = media_keys_for_comment(conn, comment_id)
                set_comment_status(conn, comment_id, "rejected")
                set_media_status(conn, comment_id, "rejected")
            else:
                object_keys = media_keys_for_comment(conn, comment_id)
                delete_comment(conn, comment_id)
            conn.commit()
        store = request.app.state.media_store
        if store is not None:
            for object_key in object_keys:
                try:
                    store.delete(object_key)
                except Exception as exc:
                    web.log_exception(
                        "Failed to remove moderated media from R2",
                        exc,
                        **{
                            "event.action": "media_delete",
                            "event.outcome": "failure",
                            "chp.comment.id": comment_id,
                        },
                    )
        web.log_event(
            "info",
            "Moderated incident comment",
            **{
                "event.action": "admin_comments",
                "event.outcome": "success",
                "chp.comment.id": comment_id,
                "chp.comment.action": action,
                **client_log_fields(request),
            },
        )
    except Exception as exc:
        web.log_exception(
            "Failed to moderate incident comment",
            exc,
            **{
                "event.action": "admin_comments",
                "event.outcome": "failure",
                "chp.comment.id": comment_id,
                "chp.comment.action": action,
                "http.response.status_code": 500,
                **client_log_fields(request),
            },
        )
        return byte_response(b"failed to moderate comment\n", "text/plain; charset=utf-8", status_code=500)
    action_label = {"approve": "approved", "reject": "rejected", "delete": "deleted"}[action]
    return handle_admin_comments_get(request, message=f"Comment #{comment_id} {action_label}.")


def handle_comments_get(request, event_key, send_body=True):
    try:
        with writable_database_connection(request.app) as conn:
            comments = list_approved_comments(conn, event_key)
            attachments = media_for_comments(
                conn, [comment["id"] for comment in comments], statuses=["approved"]
            )
            for comment in comments:
                comment["media"] = [
                    public_media(row) for row in attachments.get(int(comment["id"]), [])
                ]
        return json_response(
            {"meta": {"event_key": event_key, "status": "approved"}, "data": comments},
            cache_control="private, max-age=30, stale-while-revalidate=60",
            send_body=send_body,
        )
    except Exception as exc:
        web.log_exception(
            "Failed to render incident comments",
            exc,
            **{
                "event.action": "comments_get",
                "event.outcome": "failure",
                "http.request.method": request.method,
                "url.path": request_target(request),
                "http.response.status_code": 500,
                **client_log_fields(request),
            },
        )
        return api_error("failed to render comments", "server_error", 500, send_body=send_body)


async def handle_comments_post(request, event_key):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise CommentValidationError("JSON body must be an object.", "invalid_json")
    except CommentValidationError as exc:
        web.COMMENT_SUBMISSIONS_TOTAL[exc.code] += 1
        return api_error(str(exc), exc.code, 400)
    except Exception:
        web.COMMENT_SUBMISSIONS_TOTAL["invalid_json"] += 1
        return api_error("Invalid JSON body.", "invalid_json", 400)
    try:
        with writable_database_connection(request.app) as conn:
            result = submit_comment(
                conn,
                event_key,
                payload,
                request.headers,
                request.client.host if request.client else "",
            )
            try:
                requested_media = int(payload.get("media_count") or 0)
            except (TypeError, ValueError):
                raise CommentValidationError("Invalid media count.", "invalid_media_count") from None
            if requested_media:
                settings = request.app.state.settings
                if not media_enabled(settings):
                    raise CommentValidationError("Media uploads are not available.", "media_disabled")
                if requested_media < 1 or requested_media > 3:
                    raise CommentValidationError("Choose up to three photos or one video.", "invalid_media_count")
                expires_at = int(time.time()) + settings.r2_upload_ttl_seconds
                result["upload_token"] = create_upload_token(
                    settings.r2_upload_token_secret, result["id"], event_key, expires_at
                )
                result["upload_expires_at"] = expires_at
            conn.commit()
        comment_status = result["status"]
        web.log_event(
            "info",
            "Incident comment published automatically"
            if comment_status == "approved"
            else "Incident comment submitted for moderation",
            **{
                "event.action": "comments_submit",
                "event.outcome": "success",
                "chp.event_key": event_key,
                "chp.comment.status": comment_status,
                **client_log_fields(request),
            },
        )
        return json_response(
            result,
            status_code=201 if comment_status == "approved" else 202,
            cache_control="no-store",
        )
    except CommentValidationError as exc:
        web.COMMENT_SUBMISSIONS_TOTAL[exc.code] += 1
        status_code = 404 if exc.code == "not_found" else 429 if exc.code == "rate_limited" else 400
        web.log_event(
            "info",
            "Incident comment rejected",
            **{
                "event.action": "comments_submit",
                "event.outcome": "failure",
                "chp.event_key": event_key,
                "chp.comment.reject_reason": exc.code,
                "http.response.status_code": status_code,
                **client_log_fields(request),
            },
        )
        return api_error(str(exc), exc.code, status_code)
    except Exception as exc:
        web.COMMENT_SUBMISSIONS_TOTAL["server_error"] += 1
        web.log_exception(
            "Failed to submit incident comment",
            exc,
            **{
                "event.action": "comments_submit",
                "event.outcome": "failure",
                "chp.event_key": event_key,
                "http.response.status_code": 500,
                **client_log_fields(request),
            },
        )
        return api_error("failed to submit comment", "server_error", 500)


async def handle_media_upload_post(request, event_key, action, media_id=None):
    settings = request.app.state.settings
    store = request.app.state.media_store
    if not media_enabled(settings) or store is None:
        return api_error("Media uploads are not available.", "media_disabled", 503)
    try:
        payload = await request.json()
        comment_id = int(payload.get("comment_id"))
        validate_upload_token(
            settings.r2_upload_token_secret,
            payload.get("upload_token"),
            comment_id,
            event_key,
        )
        if action == "create":
            upload = validate_upload(
                payload,
                settings.media_max_image_bytes,
                settings.media_max_video_bytes,
                settings.media_max_video_seconds,
            )
            with writable_database_connection(request.app) as conn:
                row = create_media_row(conn, comment_id, event_key, upload)
                conn.commit()
            return json_response(
                {
                    "id": row["id"],
                    "method": "PUT",
                    "upload_url": store.presigned_url(
                        "PUT",
                        row["object_key"],
                        expires=settings.r2_upload_ttl_seconds,
                        content_type=row["content_type"],
                    ),
                    "headers": {"Content-Type": row["content_type"]},
                },
                status_code=201,
                cache_control="no-store",
            )
        with writable_database_connection(request.app) as conn:
            row = media_row(conn, media_id, comment_id)
            if not row or row["event_key"] != event_key:
                raise MediaValidationError("Upload was not found.", "upload_not_found")
            uploaded = store.head(row["object_key"])
            finalized = finalize_media_row(
                conn,
                media_id,
                comment_id,
                uploaded["size"],
                uploaded["content_type"],
            )
            conn.commit()
        return json_response(
            {"id": media_id, "status": finalized["status"]},
            cache_control="no-store",
        )
    except MediaValidationError as exc:
        status_code = 404 if exc.code == "upload_not_found" else 400
        return api_error(str(exc), exc.code, status_code)
    except (TypeError, ValueError):
        return api_error("Invalid media upload request.", "invalid_media", 400)
    except Exception as exc:
        web.log_exception(
            "Failed to process incident media",
            exc,
            **{
                "event.action": "media_upload",
                "event.outcome": "failure",
                "chp.event_key": event_key,
                "http.response.status_code": 500,
                **client_log_fields(request),
            },
        )
        return api_error("failed to process media upload", "server_error", 500)


def handle_media_get(request, media_id, admin=False, send_body=True):
    settings = request.app.state.settings
    store = request.app.state.media_store
    if store is None:
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404, send_body=send_body)
    if admin and (not admin_enabled(settings) or not admin_authorized(request)):
        return admin_unauthorized(send_body=send_body)
    try:
        with writable_database_connection(request.app) as conn:
            row = media_row(conn, media_id) if admin else approved_media_row(conn, media_id)
        if not row:
            return byte_response(
                b"Not Found\n", "text/plain; charset=utf-8", status_code=404, send_body=send_body
            )
        response = Response(b"" if not send_body else None, status_code=302)
        response.headers["Location"] = store.presigned_url("GET", row["object_key"], expires=900)
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        web.log_exception("Failed to create media view URL", exc, **{"event.action": "media_view"})
        return byte_response(b"media unavailable\n", "text/plain; charset=utf-8", status_code=500)


def create_app(settings=None):
    settings = settings or WebSettings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings
        with connect_database(settings.database, settings.database_url):
            pass
        pool_min, pool_max = _pool_limits(settings)
        app.state.database_pool = None
        if settings.database_url:
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError("Postgres pooling requires psycopg_pool. Install requirements.txt.") from exc
            app.state.database_pool = ConnectionPool(
                settings.database_url,
                min_size=pool_min,
                max_size=pool_max,
                kwargs={"row_factory": dict_row},
            )
        web.log_event(
            "info",
            "Serving CHP live map",
            **{
                "event.action": "start",
                "network.transport": "tcp",
                "url.path": settings.base_path,
                "chp.hours": settings.hours,
                "database.pool.min": pool_min if settings.database_url else 0,
                "database.pool.max": pool_max if settings.database_url else 0,
                "server.framework": "fastapi",
            },
        )
        try:
            yield
        finally:
            pool = getattr(app.state, "database_pool", None)
            if pool is not None:
                pool.close()
                app.state.database_pool = None

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.database_pool = None
    app.state.admin_login_attempts = {}
    app.state.media_store = build_media_store(settings)

    @app.middleware("http")
    async def ecs_access_log_middleware(request, call_next):
        started_at = time.monotonic()
        status_code = 500
        response = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_seconds = time.monotonic() - started_at
            path = _path(request)
            route = route_label(path, settings)
            web.HTTP_REQUESTS_TOTAL[(request.method, route, str(status_code))] += 1
            if duration_seconds >= 1.0:
                web.log_event(
                    "warning",
                    "Slow HTTP request completed",
                    **{
                        "event.action": "http_request",
                        "event.duration": int(duration_seconds * 1_000_000_000),
                        "event.outcome": "success" if status_code < 400 else "failure",
                        "http.request.method": request.method,
                        "http.response.status_code": status_code,
                        "url.path": request_target(request),
                        "chp.route": route,
                        **client_log_fields(request),
                    },
                )
            if path not in {"/healthz", "/readyz", "/metrics"} or status_code >= 500:
                web.log_event(
                    "info",
                    "HTTP request completed",
                    **{
                        "event.action": "http_request",
                        "event.outcome": "success" if status_code < 400 else "failure",
                        "http.request.method": request.method,
                        "http.response.status_code": status_code,
                        "url.path": request_target(request),
                        **client_log_fields(request),
                    },
                )

    @app.middleware("http")
    async def security_headers_middleware(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = web.CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
        return response

    @app.get("/{full_path:path}")
    def get_anything(request: Request, full_path: str):
        if _path(request) == push_config_path(settings):
            return handle_push_config_get(request, send_body=True)
        public_media_id = media_id_from_path(_path(request))
        if public_media_id is not None:
            return handle_media_get(request, public_media_id, send_body=True)
        admin_media_id = media_id_from_path(_path(request), admin=True, settings=settings)
        if admin_media_id is not None:
            return handle_media_get(request, admin_media_id, admin=True, send_body=True)
        if _path(request) == admin_login_path(settings):
            return handle_admin_login_get(request, send_body=True)
        if _path(request) == admin_session_path(settings):
            return handle_admin_session_get(request, send_body=True)
        if _path(request) == admin_sessions_path(settings):
            return handle_admin_sessions_get(request, send_body=True)
        if _path(request) == admin_path(settings):
            return handle_admin_comments_get(request, send_body=True)
        hidden_event_key = hidden_event_key_from_path(_path(request))
        if hidden_event_key is not None:
            return handle_admin_hidden_details_get(request, hidden_event_key, send_body=True)
        event_key = comment_event_key_from_path(_path(request))
        if event_key is not None:
            return handle_comments_get(request, event_key, send_body=True)
        return dispatch_request(request, send_body=True)

    @app.head("/{full_path:path}")
    def head_anything(request: Request, full_path: str):
        if _path(request) == push_config_path(settings):
            return handle_push_config_get(request, send_body=False)
        public_media_id = media_id_from_path(_path(request))
        if public_media_id is not None:
            return handle_media_get(request, public_media_id, send_body=False)
        admin_media_id = media_id_from_path(_path(request), admin=True, settings=settings)
        if admin_media_id is not None:
            return handle_media_get(request, admin_media_id, admin=True, send_body=False)
        if _path(request) == admin_login_path(settings):
            return handle_admin_login_get(request, send_body=False)
        if _path(request) == admin_session_path(settings):
            return handle_admin_session_get(request, send_body=False)
        if _path(request) == admin_sessions_path(settings):
            return handle_admin_sessions_get(request, send_body=False)
        if _path(request) == admin_path(settings):
            return handle_admin_comments_get(request, send_body=False)
        hidden_event_key = hidden_event_key_from_path(_path(request))
        if hidden_event_key is not None:
            return handle_admin_hidden_details_get(request, hidden_event_key, send_body=False)
        event_key = comment_event_key_from_path(_path(request))
        if event_key is not None:
            return handle_comments_get(request, event_key, send_body=False)
        return dispatch_request(request, send_body=False)

    @app.post("/{full_path:path}")
    async def post_anything(request: Request, full_path: str):
        if _path(request) == push_subscription_path(settings):
            return await handle_push_subscription_post(request)
        if _path(request) == admin_login_path(settings):
            return await handle_admin_login_post(request)
        if _path(request) == admin_logout_path(settings):
            return await handle_admin_logout_post(request)
        if _path(request) == admin_session_path(settings) + "/activity":
            return handle_admin_activity_post(request)
        if _path(request) == admin_sessions_path(settings):
            return await handle_admin_sessions_post(request)
        if _path(request) == admin_path(settings):
            return await handle_admin_comments_post(request)
        media_parts = media_upload_path_parts(_path(request))
        if media_parts is not None:
            return await handle_media_upload_post(request, *media_parts)
        event_key = comment_event_key_from_path(_path(request))
        if event_key is not None:
            return await handle_comments_post(request, event_key)
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404)

    return app


app = create_app()
