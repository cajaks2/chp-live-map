import datetime as dt
import json
import os
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import Response

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
        return dispatch_request(request, send_body=True)

    @app.head("/{full_path:path}")
    def head_anything(request: Request, full_path: str):
        return dispatch_request(request, send_body=False)

    return app


app = create_app()
