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
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import Response

from comments import (
    CommentValidationError,
    comment_status_counts,
    delete_comment,
    list_approved_comments,
    moderation_rows,
    set_comment_status,
    submit_comment,
)
import serve_live_map as web
from generate_live_map import (
    build_about_html,
    build_history_html,
    build_html,
    build_summary_html,
    include_linked_incident,
    incident_status,
    load_incident_by_key,
    load_incidents,
    load_last_scrape_run,
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
    if path in {"/admin/comments", f"{asset_base}/admin/comments"}:
        return "admin_comments"
    if path.startswith("/api/v1/incidents/") and path.endswith("/comments"):
        return "comments"
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


def admin_enabled(settings):
    return bool(settings.admin_username and settings.admin_password)


def admin_unauthorized(send_body=True):
    return html_response(
        "<!doctype html><title>Unauthorized</title><h1>Unauthorized</h1>",
        status_code=401,
        send_body=send_body,
        headers={"WWW-Authenticate": 'Basic realm="Crestmap comments", charset="UTF-8"'},
    )


def admin_authorized(request):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return False
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
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


def admin_path(settings):
    base = normalize_base_path(settings.base_path)
    return "/admin/comments" if base == "/" else f"{base}/admin/comments"


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
        if value and not any(value.startswith(expected) for expected in expected_origins):
            return False
    return True


def dispatch_request(request, send_body=True):
    settings = request.app.state.settings
    path = _path(request)
    base_path = normalize_base_path(settings.base_path)
    asset_base = "" if base_path == "/" else base_path
    map_paths = {"/", "/live_chp_map.html", base_path}
    summary_paths = {"/summary", f"{asset_base}/summary"}
    history_paths = {"/history", f"{asset_base}/history"}
    about_paths = {"/about", f"{asset_base}/about"}
    status_paths = {"/status.json", f"{asset_base}/status.json"}
    incidents_paths = {"/incidents.json", f"{asset_base}/incidents.json"}
    robots_paths = {"/robots.txt", f"{asset_base}/robots.txt"}
    sitemap_paths = {"/sitemap.xml", f"{asset_base}/sitemap.xml"}
    metrics_paths = {"/metrics", f"{asset_base}/metrics"}
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
                region=region,
                region_statuses=current_region_statuses,
                filters=summary_filters(request),
            ).encode("utf-8")
        elif path in history_paths:
            body = build_history_html(
                incidents,
                generated_at,
                hours,
                base_path=settings.base_path,
                public_url=settings.public_url,
                filters=history_filters(request),
                region=region,
                region_statuses=current_region_statuses,
            ).encode("utf-8")
        elif path in about_paths:
            body = build_about_html(
                incidents,
                generated_at,
                hours,
                base_path=settings.base_path,
                public_url=settings.public_url,
                region=region,
                region_statuses=current_region_statuses,
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
    return byte_response(body, "text/html; charset=utf-8", cache_control=web.MAP_CACHE_CONTROL, send_body=send_body)


def build_admin_comments_html(rows, counts, status, message="", admin_url="/admin/comments"):
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
        incident_url = f"/?region={html.escape(row.get('region') or 'forest')}&incident={html.escape(row['event_key'])}"
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
    .tab {{ padding: 8px 12px; border: 1px solid #d7ded2; border-radius: 8px; color: #35413a; text-decoration: none; font-weight: 800; background: #fff; }}
    .tab.is-active {{ color: #fff; border-color: #2b7c4a; background: #2b7c4a; }}
    .tab span {{ opacity: 0.8; }}
    .notice {{ margin-bottom: 12px; padding: 10px 12px; border: 1px solid #bdd4c0; border-radius: 8px; background: #edf7ee; color: #1f6840; font-weight: 700; }}
    .comment-card, .empty-admin {{ margin-bottom: 12px; padding: 14px; border: 1px solid #dce3d7; border-radius: 10px; background: #fff; box-shadow: 0 1px 2px rgba(21, 35, 25, 0.04); }}
    .comment-top {{ display: flex; justify-content: space-between; gap: 12px; }}
    .comment-id, .meta, .contact {{ color: #58645d; font-size: 13px; line-height: 1.35; }}
    .incident-link {{ color: #1f6840; overflow-wrap: anywhere; }}
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
    <nav class="tabs">{"".join(tabs)}</nav>
  </header>
  <main>
    {message_html}
    {"".join(cards)}
  </main>
</body>
</html>"""


def handle_admin_comments_get(request, send_body=True, message=""):
    settings = request.app.state.settings
    if not admin_enabled(settings):
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404, send_body=send_body)
    if not admin_authorized(request):
        return admin_unauthorized(send_body=send_body)
    status = admin_status_from_request(request)
    try:
        with writable_database_connection(request.app) as conn:
            rows = moderation_rows(conn, status=status, limit=100)
            counts = comment_status_counts(conn)
        body = build_admin_comments_html(
            rows,
            counts,
            status,
            message=message,
            admin_url=admin_path(settings),
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
        with writable_database_connection(request.app) as conn:
            if action == "approve":
                set_comment_status(conn, comment_id, "approved")
            elif action == "reject":
                set_comment_status(conn, comment_id, "rejected")
            else:
                delete_comment(conn, comment_id)
            conn.commit()
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
            conn.commit()
        web.log_event(
            "info",
            "Incident comment submitted for moderation",
            **{
                "event.action": "comments_submit",
                "event.outcome": "success",
                "chp.event_key": event_key,
                "chp.comment.status": "pending",
                **client_log_fields(request),
            },
        )
        return json_response(result, status_code=202, cache_control="no-store")
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
        if _path(request) == admin_path(settings):
            return handle_admin_comments_get(request, send_body=True)
        event_key = comment_event_key_from_path(_path(request))
        if event_key is not None:
            return handle_comments_get(request, event_key, send_body=True)
        return dispatch_request(request, send_body=True)

    @app.head("/{full_path:path}")
    def head_anything(request: Request, full_path: str):
        if _path(request) == admin_path(settings):
            return handle_admin_comments_get(request, send_body=False)
        event_key = comment_event_key_from_path(_path(request))
        if event_key is not None:
            return handle_comments_get(request, event_key, send_body=False)
        return dispatch_request(request, send_body=False)

    @app.post("/{full_path:path}")
    async def post_anything(request: Request, full_path: str):
        if _path(request) == admin_path(settings):
            return await handle_admin_comments_post(request)
        event_key = comment_event_key_from_path(_path(request))
        if event_key is not None:
            return await handle_comments_post(request, event_key)
        return byte_response(b"Not Found\n", "text/plain; charset=utf-8", status_code=404)

    return app


app = create_app()
