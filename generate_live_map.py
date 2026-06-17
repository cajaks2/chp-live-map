import argparse
import datetime as dt
import hashlib
import html
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlencode

from ecs_logging import log_event, run_main
from geo_bounds import clear_coordinates_outside_region_bounds


DEFAULT_CENTER = [34.32, -118.12]
DEFAULT_ZOOM = 10
REGION_VIEWPORTS = {
    "forest": {"center": DEFAULT_CENTER, "zoom": DEFAULT_ZOOM},
    "malibu": {"center": [34.09, -118.78], "zoom": 10},
}
HISTORY_PRESETS = [(24, "24h"), (72, "72h"), (168, "7d"), (720, "30d")]
REGION_LABELS = {
    "forest": "Forest",
    "malibu": "Malibu",
}


def normalize_region(region):
    normalized = (region or "forest").casefold()
    return normalized if normalized in REGION_LABELS else "forest"


def region_label(region):
    return REGION_LABELS[normalize_region(region)]


def region_viewport(region):
    return REGION_VIEWPORTS[normalize_region(region)]


def load_incidents(database, hours, database_url=None, region="forest"):
    region = normalize_region(region)
    if not database_url and not database.exists():
        return []
    cutoff = (dt.datetime.now().astimezone() - dt.timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )
    if database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Postgres support requires psycopg. Install requirements.txt.") from exc
        conn = psycopg.connect(database_url, row_factory=dict_row)
        rows = conn.execute(
            """
            SELECT
                e.*,
                (
                    SELECT o.details_json
                    FROM observations o
                    WHERE o.event_key = e.event_key
                      AND o.status = 'active'
                    ORDER BY o.observed_at DESC, o.id DESC
                    LIMIT 1
                ) AS details_json
            FROM events e
            WHERE e.region = %s
              AND (
                  e.status = 'active'
                  OR e.first_seen >= %s
                  OR e.last_seen >= %s
                  OR e.cleared_at >= %s
              )
            ORDER BY
                CASE WHEN e.status = 'active' THEN 0 ELSE 1 END,
                e.latest_observed_at DESC,
                e.incident_no DESC
            """,
            (region, cutoff, cutoff, cutoff),
        ).fetchall()
    else:
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                e.*,
                (
                    SELECT o.details_json
                    FROM observations o
                    WHERE o.event_key = e.event_key
                      AND o.status = 'active'
                    ORDER BY o.observed_at DESC, o.id DESC
                    LIMIT 1
                ) AS details_json
            FROM events e
            WHERE e.region = ?
              AND (
                  e.status = 'active'
                  OR e.first_seen >= ?
                  OR e.last_seen >= ?
                  OR e.cleared_at >= ?
              )
            ORDER BY
                CASE WHEN e.status = 'active' THEN 0 ELSE 1 END,
                e.latest_observed_at DESC,
                e.incident_no DESC
            """,
            (region, cutoff, cutoff, cutoff),
        ).fetchall()
    conn.close()
    incidents = []
    for row in rows:
        incidents.append(hydrate_incident(row, region))
    return incidents


def hydrate_incident(row, region):
    incident = clear_coordinates_outside_region_bounds(dict(row), region)
    try:
        incident["detail_entries"] = json.loads(incident.pop("details_json") or "[]")
    except json.JSONDecodeError:
        incident["detail_entries"] = []
    return incident


def load_incident_by_key(database, event_key, database_url=None, region="forest"):
    region = normalize_region(region)
    if not event_key or (not database_url and not database.exists()):
        return None
    if database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Postgres support requires psycopg. Install requirements.txt.") from exc
        conn = psycopg.connect(database_url, row_factory=dict_row)
        row = conn.execute(
            """
            SELECT
                e.*,
                (
                    SELECT o.details_json
                    FROM observations o
                    WHERE o.event_key = e.event_key
                      AND o.status = 'active'
                    ORDER BY o.observed_at DESC, o.id DESC
                    LIMIT 1
                ) AS details_json
            FROM events e
            WHERE e.region = %s
              AND e.event_key = %s
            LIMIT 1
            """,
            (region, event_key),
        ).fetchone()
    else:
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                e.*,
                (
                    SELECT o.details_json
                    FROM observations o
                    WHERE o.event_key = e.event_key
                      AND o.status = 'active'
                    ORDER BY o.observed_at DESC, o.id DESC
                    LIMIT 1
                ) AS details_json
            FROM events e
            WHERE e.region = ?
              AND e.event_key = ?
            LIMIT 1
            """,
            (region, event_key),
        ).fetchone()
    conn.close()
    return hydrate_incident(row, region) if row else None


def include_linked_incident(incidents, linked_incident):
    if not linked_incident:
        return incidents
    if any(incident.get("event_key") == linked_incident.get("event_key") for incident in incidents):
        return incidents
    linked = dict(linked_incident)
    linked["_linked_outside_window"] = True
    return [linked, *incidents]


def normalize_base_path(base_path):
    base = (base_path or "/").rstrip("/")
    return base or "/"


def metadata_urls(base_path, public_url, favicon_params=None):
    base = normalize_base_path(base_path)
    asset_base = "" if base == "/" else base
    fallback_url = base if base == "/" else f"{base}/"
    canonical_url = (public_url or fallback_url).rstrip("/") + "/"
    public_asset_base = canonical_url.rstrip("/") if public_url else asset_base
    favicon_url = f"{public_asset_base}/favicon.svg"
    if favicon_params:
        favicon_url = f"{favicon_url}?{urlencode(favicon_params)}"
    return {
        "canonical": canonical_url,
        "favicon": favicon_url,
        "og_image": f"{public_asset_base}/og-image.png",
    }


def history_controls(hours, region="forest"):
    current = int(hours)
    links = []
    for preset_hours, label in HISTORY_PRESETS:
        selected = preset_hours == current
        links.append(
            '<a class="range-tab{}" href="{}"{}>{}</a>'.format(
                " is-active" if selected else "",
                html.escape(href_with_query("", hours=f"{preset_hours:g}", region=normalize_region(region))),
                ' aria-current="page"' if selected else "",
                html.escape(label),
            )
        )
    return "".join(links)


def app_path(base_path, suffix="/"):
    base = normalize_base_path(base_path)
    if suffix == "/":
        return "/" if base == "/" else f"{base}/"
    return suffix if base == "/" else f"{base}{suffix}"


def href_with_query(href, **params):
    clean_params = {
        key: value
        for key, value in params.items()
        if value is not None and value != ""
    }
    if not clean_params:
        return href
    separator = "&" if "?" in href else "?"
    return f"{href}{separator}{urlencode(clean_params)}"


def view_href(base_path, suffix, hours, region="forest"):
    return href_with_query(app_path(base_path, suffix), hours=f"{hours:g}", region=normalize_region(region))


def view_menu(base_path, current, hours, region="forest"):
    items = [
        ("map", "Map", "Current incidents", view_href(base_path, "/", hours, region)),
        ("summary", "Summary", "Counts + trends", view_href(base_path, "/summary", hours, region)),
        ("history", "History", "Search incidents", view_href(base_path, "/history", hours, region)),
        ("about", "About", "Source + cadence", view_href(base_path, "/about", hours, region)),
    ]
    rows = []
    for key, label, description, href in items:
        rows.append(
            '<a class="view-menu-row{}" href="{}">{} <span>{}</span></a>'.format(
                " is-active" if key == current else "",
                html.escape(href),
                html.escape(label),
                html.escape(description),
            )
        )
    return (
        '<details class="view-menu">'
        '<summary aria-label="Open navigation menu">...</summary>'
        '<div class="view-menu-popover">'
        + "".join(rows)
        + "</div></details>"
    )


def view_tabs(base_path, current, hours, region="forest"):
    items = [
        ("map", "Map", view_href(base_path, "/", hours, region)),
        ("summary", "Summary", view_href(base_path, "/summary", hours, region)),
        ("history", "History", view_href(base_path, "/history", hours, region)),
        ("about", "About", view_href(base_path, "/about", hours, region)),
    ]
    return "".join(
        '<a class="view-tab{}" href="{}"{}>{}</a>'.format(
            " is-active" if key == current else "",
            html.escape(href),
            ' aria-current="page"' if key == current else "",
            html.escape(label),
        )
        for key, label, href in items
    )


def region_tabs(base_path, current, hours, region="forest", region_statuses=None):
    region = normalize_region(region)
    region_statuses = region_statuses or {}
    tabs = []
    for key, label in REGION_LABELS.items():
        active_count = int((region_statuses.get(key) or {}).get("active_count", 0))
        active_label = "active incident" if active_count == 1 else "active incidents"
        tabs.append(
            '<a class="region-tab{}" href="{}"{}><span>{}</span><span class="region-active-count" aria-label="{}">{}</span></a>'.format(
            " is-active" if key == region else "",
            html.escape(view_href(base_path, "/", hours, key) if current == "map" else view_href(base_path, f"/{current}", hours, key)),
            ' aria-current="page"' if key == region else "",
            html.escape(label),
            html.escape(f"{active_count} {active_label}"),
            active_count,
        )
        )
    return "".join(tabs)


def incident_status(incidents, hours):
    window_incidents = [
        incident for incident in incidents if not incident.get("_linked_outside_window")
    ]
    mapped_count = len(
        [i for i in window_incidents if i.get("latitude") is not None and i.get("longitude") is not None]
    )
    active_count = len([i for i in window_incidents if i.get("status") == "active"])
    data_updated_at = max(
        [
            i.get("latest_observed_at") or i.get("last_seen") or i.get("first_seen") or ""
            for i in window_incidents
        ],
        default="",
    )
    version_source = [
        {
            "event_key": i.get("event_key"),
            "status": i.get("status"),
            "incident_time": i.get("incident_time"),
            "type": i.get("type"),
            "location": i.get("location"),
            "location_desc": i.get("location_desc"),
            "area": i.get("area"),
            "latitude": i.get("latitude"),
            "longitude": i.get("longitude"),
            "details_hash": i.get("details_hash"),
            "cleared_at": i.get("cleared_at"),
        }
        for i in window_incidents
    ]
    version = hashlib.sha256(
        json.dumps(version_source, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "active_count": active_count,
        "total_count": len(window_incidents),
        "mapped_count": mapped_count,
        "hours": hours,
        "data_updated_at": data_updated_at,
        "version": version,
    }


def analytics_script(google_analytics_id=None):
    if not google_analytics_id:
        return ""
    escaped_id = html.escape(google_analytics_id, quote=True)
    js_id = json.dumps(google_analytics_id)
    return f"""  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id={escaped_id}"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date());

    gtag('config', {js_id});
  </script>
"""


def build_html(
    incidents,
    generated_at,
    hours,
    base_path="/",
    public_url=None,
    google_analytics_id=None,
    map_label="Forest",
    region="forest",
    region_statuses=None,
):
    region = normalize_region(region)
    map_label = region_label(region)
    viewport = region_viewport(region)
    status = {**incident_status(incidents, hours), "region": region}
    active_count = status["active_count"]
    mapped_count = status["mapped_count"]
    title = f"CHP {map_label} Incidents ({active_count} active, {status['total_count']} total)"
    if region == "forest":
        description = (
            "Live and historical CHP CAD traffic incidents for Angeles Crest, Angeles Forest, "
            "Big Tujunga, Glendora Mountain, and nearby forest roads in the forest."
        )
    else:
        description = (
            "Live and historical CHP CAD traffic incidents for Malibu canyon and coastal roads."
        )
    urls = metadata_urls(
        base_path,
        public_url,
        {"active": 1 if active_count else 0, "v": status["version"]},
    )
    base = normalize_base_path(base_path)
    asset_base = "" if base == "/" else base
    if public_url:
        public_path = urlsplit(public_url).path.rstrip("/")
        status_endpoint = f"{public_path}/status.json" if public_path else "/status.json"
        incidents_endpoint = f"{public_path}/incidents.json" if public_path else "/incidents.json"
    else:
        status_endpoint = f"{asset_base}/status.json"
        incidents_endpoint = f"{asset_base}/incidents.json"
    structured_data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": f"{urls['canonical']}#website",
                "name": f"CHP {map_label} Incidents",
                "url": urls["canonical"],
                "description": description,
                "inLanguage": "en-US",
            },
            {
                "@type": "WebApplication",
                "@id": f"{urls['canonical']}#app",
                "name": f"CHP {map_label} Incidents",
                "url": urls["canonical"],
                "description": description,
                "applicationCategory": "MapApplication",
                "operatingSystem": "Any",
                "isAccessibleForFree": True,
                "areaServed": {
                    "@type": "Place",
                    "name": (
                        "Angeles National Forest and nearby Southern California mountain roads"
                        if region == "forest"
                        else "Malibu canyon and coastal roads"
                    ),
                },
                "about": [
                    "CHP CAD traffic incidents",
                    *(
                        [
                            "Angeles Crest Highway",
                            "Angeles Forest Highway",
                            "Big Tujunga Canyon Road",
                            "Glendora Mountain Road",
                        ]
                        if region == "forest"
                        else [
                            "Pacific Coast Highway",
                            "Malibu Canyon Road",
                            "Topanga Canyon Boulevard",
                            "Las Virgenes Road",
                        ]
                    ),
                ],
            },
            {
                "@type": "Dataset",
                "@id": f"{urls['canonical']}#incident-history",
                "name": f"CHP {map_label.lower()} road incident history",
                "url": urls["canonical"],
                "description": (
                    f"Rolling incident history collected from public CHP CAD pages for selected "
                    f"{map_label.lower()} roads. The scraper checks CHP about once a minute."
                ),
                "temporalCoverage": f"last {hours:g} hours",
                "isAccessibleForFree": True,
                "license": "https://cad.chp.ca.gov/",
            },
        ],
    }
    structured_data_json = json.dumps(structured_data, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta name="description" content="{html.escape(description)}">
  <meta name="robots" content="index,follow,max-image-preview:large">
  <link rel="canonical" href="{html.escape(urls["canonical"])}">
  <link rel="icon" href="{html.escape(urls["favicon"])}" type="image/svg+xml">
  <meta property="og:type" content="website">
  <meta property="og:title" content="{html.escape(title)}">
  <meta property="og:description" content="{html.escape(description)}">
  <meta property="og:url" content="{html.escape(urls["canonical"])}">
  <meta property="og:image" content="{html.escape(urls["og_image"])}">
  <meta property="og:image:type" content="image/png">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{html.escape(title)}">
  <meta name="twitter:description" content="{html.escape(description)}">
  <meta name="twitter:image" content="{html.escape(urls["og_image"])}">
  <script type="application/ld+json">{structured_data_json}</script>
{analytics_script(google_analytics_id)}\
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQ9um5Lj053hphD7uW9P4U5F9VAt5x0=" crossorigin="">
  <style>
    .leaflet-container {{
      overflow: hidden;
      touch-action: none;
      -webkit-tap-highlight-color: transparent;
      -webkit-touch-callout: none;
      outline: none;
    }}
    #map:focus,
    .leaflet-container:focus {{
      outline: none;
    }}
    .leaflet-pane,
    .leaflet-tile,
    .leaflet-marker-icon,
    .leaflet-marker-shadow,
    .leaflet-tile-container,
    .leaflet-pane > svg,
    .leaflet-pane > canvas,
    .leaflet-zoom-box,
    .leaflet-image-layer,
    .leaflet-layer {{
      position: absolute;
      left: 0;
      top: 0;
    }}
    .leaflet-tile {{
      width: 256px;
      height: 256px;
      user-select: none;
      -webkit-user-drag: none;
    }}
    .leaflet-pane {{
      z-index: 400;
    }}
    .leaflet-tile-pane {{
      z-index: 200;
    }}
    .leaflet-overlay-pane {{
      z-index: 400;
    }}
    .leaflet-shadow-pane {{
      z-index: 500;
    }}
    .leaflet-marker-pane {{
      z-index: 600;
    }}
    .leaflet-tooltip-pane {{
      z-index: 650;
    }}
    .leaflet-popup-pane {{
      z-index: 700;
    }}
    .leaflet-control {{
      position: relative;
      z-index: 800;
      pointer-events: auto;
    }}
    .leaflet-top,
    .leaflet-bottom {{
      position: absolute;
      z-index: 1000;
      pointer-events: none;
    }}
    .leaflet-top {{
      top: 0;
    }}
    .leaflet-right {{
      right: 0;
    }}
    .leaflet-bottom {{
      bottom: 0;
    }}
    .leaflet-left {{
      left: 0;
    }}
    html, body {{
      height: 100%;
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #182026;
      background: #f6f7f4;
    }}
    #app {{
      display: grid;
      grid-template-columns: minmax(280px, 350px) minmax(340px, 1fr) minmax(300px, 390px);
      height: 100%;
    }}
    #sidebar {{
      display: flex;
      min-height: 0;
      overflow: hidden;
      flex-direction: column;
      border-right: 1px solid #d8ddd2;
      background: #fbfcf8;
    }}
    header {{
      flex: 0 0 auto;
      z-index: 2;
      padding: 16px 18px 14px;
      border-bottom: 1px solid #d8ddd2;
      background: #fbfcf8;
    }}
    h1 {{
      margin: 0 0 4px;
      font-size: 20px;
      line-height: 1.2;
      letter-spacing: 0;
    }}
    .title-row {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }}
    .title-row h1 {{
      min-width: 0;
    }}
    .view-menu {{
      position: relative;
      flex: 0 0 auto;
    }}
    .view-menu summary {{
      display: flex;
      align-items: center;
      justify-content: center;
      width: 34px;
      height: 34px;
      border: 1px solid #d8ddd2;
      border-radius: 7px;
      color: #182026;
      background: #ffffff;
      font-size: 16px;
      font-weight: 900;
      line-height: 1;
      cursor: pointer;
      list-style: none;
    }}
    .view-menu summary::-webkit-details-marker {{
      display: none;
    }}
    .view-menu-popover {{
      position: absolute;
      top: 40px;
      right: 0;
      z-index: 20;
      width: min(290px, calc(100vw - 36px));
      padding: 6px;
      border: 1px solid #d8ddd2;
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 10px 28px rgba(24, 32, 38, 0.18);
    }}
    .view-menu-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      min-height: 36px;
      padding: 0 8px;
      border-radius: 6px;
      color: #182026;
      font-size: 13px;
      font-weight: 800;
      text-decoration: none;
    }}
    .view-menu-row span {{
      color: #46534b;
      font-size: 12px;
      font-weight: 700;
    }}
    .view-menu-row.is-active,
    .view-menu-row:hover,
    .view-menu-row:focus {{
      color: #1f6840;
      background: #eef7ee;
      outline: none;
    }}
    .view-menu-row.is-active span,
    .view-menu-row:hover span,
    .view-menu-row:focus span {{
      color: #1f6840;
    }}
    .meta {{
      color: #58645d;
      font-size: 13px;
      line-height: 1.35;
    }}
    .checked-meta {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0 5px;
    }}
    .checked-meta time {{
      display: inline-flex;
      align-items: center;
    }}
    .range-tabs {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 3px;
      margin-top: 10px;
      padding: 3px;
      border: 1px solid #d8ddd2;
      border-radius: 8px;
      background: #eef1ea;
    }}
    .range-tab {{
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 28px;
      padding: 0 7px;
      border-radius: 5px;
      color: #3f4a44;
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
      text-align: center;
      text-decoration: none;
    }}
    .range-tab:hover,
    .range-tab:focus {{
      background: #ffffff;
      outline: none;
    }}
    .range-tab.is-active {{
      color: #ffffff;
      background: #277447;
    }}
    .region-tabs {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 3px;
      margin-top: 10px;
      padding: 3px;
      border: 1px solid #d8ddd2;
      border-radius: 8px;
      background: #eef1ea;
    }}
    .region-tab {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      min-height: 28px;
      padding: 0 7px;
      border-radius: 5px;
      color: #3f4a44;
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
      text-align: center;
      text-decoration: none;
    }}
    .region-tab:hover,
    .region-tab:focus {{
      background: #ffffff;
      outline: none;
    }}
    .region-tab.is-active {{
      color: #ffffff;
      background: #277447;
    }}
    .region-active-count {{
      min-width: 16px;
      padding: 2px 5px;
      border-radius: 999px;
      color: #3f4a44;
      background: rgba(255, 255, 255, 0.72);
      font-size: 10px;
      font-weight: 900;
      line-height: 1;
    }}
    .region-tab.is-active .region-active-count {{
      color: #1f6840;
      background: #ffffff;
    }}
    .secondary-tabs {{
      display: contents;
    }}
    .view-tabs {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 3px;
      margin-top: 10px;
      padding: 3px;
      border: 1px solid #d8ddd2;
      border-radius: 8px;
      background: #eef1ea;
    }}
    .view-tab {{
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 28px;
      padding: 0 7px;
      border-radius: 5px;
      color: #3f4a44;
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
      text-align: center;
      text-decoration: none;
    }}
    .view-tab:hover,
    .view-tab:focus {{
      background: #ffffff;
      outline: none;
    }}
    .view-tab.is-active {{
      color: #ffffff;
      background: #277447;
    }}
    .about-panel {{
      margin-top: 10px;
      padding: 9px 10px;
      border: 1px solid #d8ddd2;
      border-radius: 6px;
      color: #3f4a44;
      background: #f3f6ef;
      font-size: 12px;
      line-height: 1.35;
    }}
    .about-panel summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      color: #182026;
      font-weight: 800;
      cursor: pointer;
      list-style: none;
    }}
    .about-panel summary::-webkit-details-marker {{
      display: none;
    }}
    .about-panel summary::after {{
      content: "";
      width: 8px;
      height: 8px;
      margin-right: 2px;
      border-right: 2px solid currentColor;
      border-bottom: 2px solid currentColor;
      transform: rotate(45deg);
      transition: transform 0.16s ease;
    }}
    .about-panel[open] summary::after {{
      transform: translateY(3px) rotate(225deg);
    }}
    .about-blurb strong {{
      color: #182026;
    }}
    .about-blurb {{
      margin: 7px 0 0;
    }}
    .about-link {{
      display: inline-block;
      margin-top: 7px;
      color: #1f6840;
      font-weight: 800;
      text-decoration: underline;
      text-underline-offset: 2px;
    }}
    #stale-notice {{
      display: none;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      padding: 8px 9px;
      border: 1px solid #e4c56d;
      border-radius: 6px;
      color: #5c4614;
      background: #fff7d8;
      font-size: 12px;
      line-height: 1.3;
    }}
    #stale-notice.is-visible {{
      display: flex;
    }}
    #stale-notice span {{
      flex: 1 1 auto;
    }}
    #stale-notice button {{
      flex: 0 0 auto;
      min-height: 28px;
      padding: 4px 8px;
      border: 1px solid #c7a848;
      border-radius: 5px;
      color: #3d310f;
      background: #ffffff;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    .auto-refresh-control {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      color: #46534b;
      font-weight: 700;
      line-height: 1.35;
      cursor: pointer;
    }}
    .auto-refresh-control input {{
      width: 13px;
      height: 13px;
      margin: 0;
      accent-color: #277447;
    }}
    #incident-list-shell {{
      flex: 1 1 auto;
      min-height: 0;
      position: relative;
      overflow: hidden;
      background: #fbfcf8;
    }}
    #incident-list-shell.has-more-above {{
      box-shadow: inset 0 24px 18px -24px rgba(39, 62, 48, 0.42);
    }}
    #incident-list-shell.has-more-below {{
      box-shadow: inset 0 -30px 24px -24px rgba(39, 62, 48, 0.48);
    }}
    #incident-list-shell.has-more-above.has-more-below {{
      box-shadow:
        inset 0 24px 18px -24px rgba(39, 62, 48, 0.42),
        inset 0 -30px 24px -24px rgba(39, 62, 48, 0.48);
    }}
    #scroll-incidents {{
      display: none;
      position: absolute;
      left: 50%;
      bottom: 7px;
      z-index: 3;
      align-items: center;
      justify-content: center;
      width: 34px;
      height: 28px;
      border: 1px solid rgba(39, 116, 71, 0.4);
      border-radius: 999px;
      color: #277447;
      background: rgba(255, 255, 255, 0.94);
      box-shadow: 0 2px 8px rgba(24, 32, 38, 0.22);
      transform: translateX(-50%);
      cursor: pointer;
    }}
    #incident-list-shell.has-more-below #scroll-incidents {{
      display: flex;
    }}
    #scroll-incidents::before {{
      content: "";
      width: 9px;
      height: 9px;
      margin-top: -4px;
      border-right: 2px solid currentColor;
      border-bottom: 2px solid currentColor;
      transform: rotate(45deg);
    }}
    #scroll-incidents:focus {{
      outline: 2px solid rgba(39, 116, 71, 0.45);
      outline-offset: 2px;
    }}
    #incident-list {{
      height: 100%;
      overflow-y: auto;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
      scrollbar-width: thin;
      scrollbar-color: #8fa195 #eef1ea;
      background: #fbfcf8;
      -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 12px, #000 calc(100% - 24px), transparent 100%);
      mask-image: linear-gradient(to bottom, transparent 0, #000 12px, #000 calc(100% - 24px), transparent 100%);
    }}
    #incident-list::-webkit-scrollbar {{
      width: 9px;
    }}
    #incident-list::-webkit-scrollbar-track {{
      background: #eef1ea;
    }}
    #incident-list::-webkit-scrollbar-thumb {{
      border: 2px solid #eef1ea;
      border-radius: 999px;
      background: #8fa195;
    }}
    .incident {{
      display: block;
      width: 100%;
      padding: 13px 16px;
      border: 0;
      border-bottom: 1px solid #e2e6de;
      text-align: left;
      color: inherit;
      background: #ffffff;
      cursor: pointer;
    }}
    .incident:hover,
    .incident:focus {{
      background: #eef4ee;
      outline: none;
    }}
    .incident strong {{
      display: block;
      margin-bottom: 4px;
      font-size: 14px;
      line-height: 1.25;
    }}
    .incident span {{
      display: block;
      color: #58645d;
      font-size: 12px;
      line-height: 1.35;
    }}
    .incident .incident-heading {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }}
    .incident-marker {{
      box-sizing: border-box;
      position: absolute;
      display: block;
      width: 22px;
      height: 22px;
      background: transparent;
      border: 0;
      cursor: pointer;
      pointer-events: auto;
      touch-action: manipulation;
      -webkit-tap-highlight-color: transparent;
    }}
    .incident-marker-dot {{
      box-sizing: border-box;
      position: absolute;
      inset: 0;
      display: block;
      border: 3px solid #7a1a1d;
      border-radius: 999px;
      background: #d94a38;
      box-shadow: 0 1px 6px rgba(24, 32, 38, 0.32);
      pointer-events: none;
    }}
    .incident-marker.is-cleared .incident-marker-dot {{
      border-color: #5f6862;
      background: #b8bfba;
    }}
    .incident-marker.is-selected .incident-marker-dot {{
      background: #f05a40;
      box-shadow: 0 2px 9px rgba(24, 32, 38, 0.42);
    }}
    .incident-marker.is-selected.is-cleared .incident-marker-dot {{
      background: #9da5a0;
    }}
    .incident-marker.is-selected .incident-marker-dot::before {{
      content: "";
      position: absolute;
      inset: -9px;
      border: 3px solid rgba(216, 59, 59, 0.76);
      border-radius: 999px;
      box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.88), 0 2px 12px rgba(24, 32, 38, 0.3);
      pointer-events: none;
    }}
    .incident-marker.is-selected.is-cleared .incident-marker-dot::before {{
      border-color: rgba(31, 104, 64, 0.78);
    }}
    .incident-marker.is-pulsing .incident-marker-dot::after {{
      content: "";
      position: absolute;
      inset: -10px;
      border: 3px solid rgba(216, 59, 59, 0.65);
      border-radius: 999px;
      pointer-events: none;
      animation: selected-marker-pulse 900ms ease-out 1;
    }}
    .incident-marker.is-pulsing.is-cleared .incident-marker-dot::after {{
      border-color: rgba(31, 104, 64, 0.62);
    }}
    @keyframes selected-marker-pulse {{
      from {{
        opacity: 0.82;
        transform: scale(0.82);
      }}
      to {{
        opacity: 0;
        transform: scale(1.75);
      }}
    }}
    #map {{
      position: relative;
      height: 100%;
      min-height: 420px;
      overflow: hidden;
      background: #d9ded4;
      z-index: 0;
    }}
    #map::after {{
      content: "";
      position: absolute;
      inset: 0;
      z-index: 450;
      pointer-events: none;
      background:
        linear-gradient(90deg, rgba(217,222,212,0.72), rgba(247,248,244,0.72), rgba(217,222,212,0.72)),
        #d9ded4;
      background-size: 220% 100%;
      opacity: 0;
      transition: opacity 160ms ease;
    }}
    #map.is-loading::after {{
      opacity: 1;
      animation: mapLoading 1.1s linear infinite;
    }}
    @keyframes mapLoading {{
      from {{ background-position: 0 0; }}
      to {{ background-position: -220% 0; }}
    }}
    #map .leaflet-tile-pane {{
      opacity: 0;
      transition: opacity 160ms ease;
    }}
    #map.tiles-ready .leaflet-tile-pane {{
      opacity: 1;
    }}
    #details {{
      position: relative;
      z-index: 1;
      overflow: auto;
      border-left: 1px solid #d8ddd2;
      background: #ffffff;
    }}
    #details-cue {{
      display: none;
    }}
    .detail-panel {{
      padding: 18px;
    }}
    .detail-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 6px;
    }}
    .detail-title {{
      min-width: 0;
    }}
    .detail-actions {{
      flex: 0 0 auto;
      display: flex;
      align-items: flex-start;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .detail-panel h2 {{
      margin: 0 0 6px;
      font-size: 18px;
      line-height: 1.25;
      letter-spacing: 0;
    }}
    .share-incident,
    .default-view {{
      flex: 0 0 auto;
      min-height: 30px;
      padding: 5px 9px;
      border: 1px solid #cbd6cc;
      border-radius: 6px;
      color: #1f6840;
      background: #f8faf6;
      font: inherit;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }}
    .default-view {{
      color: #4f5b54;
    }}
    .share-incident:focus,
    .share-incident:hover,
    .default-view:focus,
    .default-view:hover {{
      border-color: #94b69a;
      background: #edf5ed;
      outline: none;
    }}
    .detail-section {{
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid #e5e8e1;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: 88px 1fr;
      gap: 7px 12px;
      font-size: 13px;
      line-height: 1.35;
    }}
    .detail-grid dt {{
      color: #58645d;
    }}
    .detail-grid dd {{
      margin: 0;
      overflow-wrap: anywhere;
    }}
    .detail-log {{
      margin: 10px 0 0;
      padding: 0;
      list-style: none;
      font-size: 13px;
      line-height: 1.35;
    }}
    .detail-log li {{
      padding: 9px 0;
      border-top: 1px solid #edf0ea;
    }}
    .detail-log li:first-child {{
      border-top: 0;
    }}
    .detail-log time {{
      display: block;
      margin-bottom: 3px;
      color: #58645d;
      font-size: 12px;
    }}
    .detail-subsection {{
      margin-top: 12px;
    }}
    .detail-subsection:first-child {{
      margin-top: 8px;
    }}
    .detail-subsection h3 {{
      margin: 0 0 6px;
      color: #3f4a44;
      font-size: 13px;
      line-height: 1.3;
      letter-spacing: 0;
    }}
    .incident[aria-current="true"] {{
      background: #d4e6d5;
      box-shadow: inset 4px 0 0 #1f6840;
    }}
    .incident .selected-pill {{
      display: none;
      flex: 0 0 auto;
      margin: 2px 0 0;
      padding: 0;
      border-radius: 999px;
      color: #1f6840;
      background: transparent;
      font-size: 11px;
      font-weight: 800;
      line-height: 1.35;
      vertical-align: top;
    }}
    .incident .status-pill {{
      display: inline-block;
      flex: 0 1 auto;
    }}
    .incident .linked-pill {{
      display: inline-block;
      flex: 0 0 auto;
      margin: 2px 0 0;
      color: #72510e;
      background: transparent;
      font-size: 11px;
      font-weight: 800;
      line-height: 1.35;
    }}
    .incident[aria-current="true"] .selected-pill {{
      display: inline-block;
    }}
    .status-pill {{
      display: inline-block;
      margin-bottom: 6px;
      padding: 2px 7px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      line-height: 1.35;
      text-transform: uppercase;
    }}
    .status-active {{
      color: #8f1d21;
      background: #fde7df;
    }}
    .status-cleared {{
      color: #59615c;
      background: #ecefed;
    }}
    .mapless {{
      color: #8a5b22;
      font-weight: 600;
    }}
    .empty {{
      padding: 18px;
      color: #58645d;
      font-size: 14px;
    }}
    @media (max-width: 760px) {{
      #app {{
        display: block;
        height: auto;
        min-height: 100%;
      }}
      #sidebar {{
        display: flex;
        max-height: none;
        overflow: hidden;
        border-right: 0;
        border-bottom: 1px solid #d8ddd2;
      }}
      header {{
        padding: 8px 12px 8px;
      }}
      h1 {{
        margin-bottom: 2px;
        font-size: 17px;
        line-height: 1.12;
      }}
      .title-row {{
        align-items: center;
        gap: 8px;
      }}
      .view-menu {{
        display: block;
      }}
      .view-menu summary {{
        width: 30px;
        height: 30px;
        border-radius: 6px;
        font-size: 15px;
      }}
      .view-menu-popover {{
        top: 36px;
        width: min(270px, calc(100vw - 24px));
      }}
      .meta {{
        font-size: 11px;
        line-height: 1.25;
      }}
      .checked-meta {{
        gap: 0 4px;
      }}
      .auto-refresh-control {{
        gap: 3px;
      }}
      .auto-refresh-control input {{
        width: 11px;
        height: 11px;
      }}
      .range-tabs,
      .region-tabs {{
        gap: 2px;
        margin-top: 5px;
        padding: 2px;
        border-radius: 7px;
      }}
      .range-tab,
      .region-tab {{
        min-height: 23px;
        padding: 0 5px;
        border-radius: 5px;
        font-size: 11px;
        font-weight: 800;
      }}
      .secondary-tabs {{
        display: contents;
        margin-top: 5px;
      }}
      .secondary-tabs .region-tabs {{
        margin-top: 5px;
      }}
      .view-tabs {{
        display: none;
      }}
      #incident-list-shell {{
        flex: 0 0 164px;
        flex-basis: clamp(150px, 23svh, 200px);
        min-height: 150px;
      }}
      #incident-list {{
        -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 10px, #000 calc(100% - 32px), transparent 100%);
        mask-image: linear-gradient(to bottom, transparent 0, #000 10px, #000 calc(100% - 32px), transparent 100%);
      }}
      .incident {{
        padding: 9px 12px;
      }}
      .incident strong {{
        font-size: 13px;
      }}
      .incident span {{
        font-size: 11px;
        line-height: 1.28;
      }}
      .status-pill {{
        margin-bottom: 4px;
        padding: 1px 7px;
        font-size: 10px;
      }}
      #map {{
        height: 45svh;
        min-height: 280px;
      }}
      #details-cue {{
        display: flex;
        position: absolute;
        left: 50%;
        bottom: var(--details-cue-bottom, max(26px, calc(env(safe-area-inset-bottom) + 10px)));
        z-index: 600;
        align-items: center;
        gap: 8px;
        min-height: 36px;
        padding: 7px 12px;
        border: 1px solid rgba(39, 116, 71, 0.36);
        border-radius: 999px;
        color: #1f6840;
        background: rgba(255, 255, 255, 0.94);
        box-shadow: 0 2px 10px rgba(24, 32, 38, 0.18);
        font: inherit;
        font-size: 12px;
        font-weight: 800;
        transform: translateX(-50%);
      }}
      #details-cue::after {{
        content: "";
        width: 8px;
        height: 8px;
        margin-top: -4px;
        border-right: 2px solid currentColor;
        border-bottom: 2px solid currentColor;
        transform: rotate(45deg);
      }}
      #details-cue:focus {{
        outline: 2px solid rgba(39, 116, 71, 0.45);
        outline-offset: 2px;
      }}
      #details {{
        border-left: 0;
        border-top: 1px solid #d8ddd2;
      }}
    }}
  </style>
</head>
<body>
  <div id="app">
    <aside id="sidebar">
      <header>
        <div class="title-row">
          <h1>CHP {html.escape(map_label)} Incidents</h1>
          {view_menu(base_path, "map", hours, region)}
        </div>
        <div class="meta">{active_count} active · {status['total_count']} in last {hours:g}h · {mapped_count} mapped</div>
        <div class="meta checked-meta"><span>Last checked <time id="generated-at" datetime="{html.escape(generated_at)}">{html.escape(generated_at)}</time></span><span aria-hidden="true">·</span>
          <label class="auto-refresh-control" title="Automatically reload when new incident data is available">
            <input type="checkbox" id="auto-refresh-enabled">
            Auto refresh
          </label>
        </div>
        <nav class="range-tabs" aria-label="History range">{history_controls(hours, region)}</nav>
        <div class="secondary-tabs">
          <nav class="region-tabs" aria-label="Region">{region_tabs(base_path, "map", hours, region, region_statuses)}</nav>
          <nav class="view-tabs" aria-label="View navigation">{view_tabs(base_path, "map", hours, region)}</nav>
        </div>
        <div id="stale-notice" role="status">
          <span id="stale-notice-text">Data may be stale.</span>
          <button type="button" id="refresh-page">Refresh</button>
          <button type="button" id="dismiss-stale-notice" aria-label="Dismiss stale data notice">Dismiss</button>
        </div>
      </header>
      <div id="incident-list-shell">
        <div id="incident-list"></div>
        <button type="button" id="scroll-incidents" aria-label="Scroll incident list down"></button>
      </div>
    </aside>
    <main id="map"><button type="button" id="details-cue">Incident details below</button></main>
    <aside id="details"></aside>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const initialDataStatus = {json.dumps(status, ensure_ascii=False)};
    const statusEndpoint = "{html.escape(status_endpoint)}";
    const incidentsEndpoint = "{html.escape(incidents_endpoint)}";
    const currentRegion = "{html.escape(region)}";
    let incidents = [];
    let currentDataStatus = initialDataStatus;
    let selectedIncidentKey = new URLSearchParams(window.location.search).get("incident");

    const mapEl = document.getElementById("map");
    mapEl.classList.add("is-loading");
    const map = L.map("map", {{
      preferCanvas: false,
      tap: true,
      touchZoom: true,
      doubleClickZoom: true,
      keyboard: false,
      zoomControl: false,
      zoomAnimation: true,
      fadeAnimation: true,
      markerZoomAnimation: true
    }}).setView({json.dumps(viewport["center"])}, {viewport["zoom"]});
    const baseLayer = L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      subdomains: "abc",
      maxZoom: 19,
      keepBuffer: 8,
      updateWhenIdle: false,
      updateWhenZooming: true,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }});
    baseLayer.on("load", () => {{
      mapEl.classList.remove("is-loading");
      mapEl.classList.add("tiles-ready");
    }});
    baseLayer.on("loading", () => {{
      mapEl.classList.add("is-loading");
      window.clearTimeout(window.chpTileLoadingTimer);
      window.chpTileLoadingTimer = window.setTimeout(() => {{
        mapEl.classList.remove("is-loading");
        mapEl.classList.add("tiles-ready");
      }}, 1800);
    }});
    baseLayer.addTo(map);

    const markers = new Map();
    const listShell = document.getElementById("incident-list-shell");
    const list = document.getElementById("incident-list");
    const scrollIncidentsButton = document.getElementById("scroll-incidents");
    const detailsPanel = document.getElementById("details");
    const detailsCue = document.getElementById("details-cue");
    window.chpLiveMap = {{ map, markers, incidents, status: currentDataStatus }};

    const mobileViewport = window.matchMedia("(max-width: 760px)");

    function setupDoubleTapZoom() {{
      let lastTap = null;
      let touchStart = null;
      let multiTouchUntil = 0;

      mapEl.addEventListener("touchstart", (event) => {{
        if (event.touches.length !== 1) {{
          multiTouchUntil = Date.now() + 450;
          lastTap = null;
          touchStart = null;
          return;
        }}
        const touch = event.touches[0];
        touchStart = {{ x: touch.clientX, y: touch.clientY }};
      }}, {{ passive: true }});

      mapEl.addEventListener("touchmove", (event) => {{
        if (!touchStart || event.touches.length !== 1) {{
          return;
        }}
        const touch = event.touches[0];
        if (Math.hypot(touch.clientX - touchStart.x, touch.clientY - touchStart.y) > 12) {{
          touchStart = null;
          lastTap = null;
        }}
      }}, {{ passive: true }});

      mapEl.addEventListener("touchend", (event) => {{
        if (Date.now() < multiTouchUntil || event.touches.length > 0 || event.changedTouches.length !== 1) {{
          return;
        }}
        const touch = event.changedTouches[0];
        const now = Date.now();
        const currentTap = {{ x: touch.clientX, y: touch.clientY, time: now }};
        const isDoubleTap = lastTap
          && now - lastTap.time < 350
          && Math.hypot(touch.clientX - lastTap.x, touch.clientY - lastTap.y) < 32;

        if (isDoubleTap) {{
          event.preventDefault();
          const rect = mapEl.getBoundingClientRect();
          const point = L.point(touch.clientX - rect.left, touch.clientY - rect.top);
          const latLng = map.containerPointToLatLng(point);
          map.setZoomAround(latLng, Math.min(map.getZoom() + 1, map.getMaxZoom()), {{ animate: false }});
          lastTap = null;
          return;
        }}
        lastTap = currentTap;
      }}, {{ passive: false }});
    }}

    function updateDetailsCuePosition() {{
      if (!detailsCue || !mobileViewport.matches) {{
        mapEl.style.removeProperty("--details-cue-bottom");
        return;
      }}
      const viewport = window.visualViewport;
      const viewportTop = viewport ? viewport.offsetTop : 0;
      const viewportBottom = viewport ? viewport.offsetTop + viewport.height : window.innerHeight;
      const rect = mapEl.getBoundingClientRect();
      const visibleTop = Math.max(rect.top, viewportTop);
      const visibleBottom = Math.min(rect.bottom, viewportBottom);
      if (visibleBottom <= visibleTop) {{
        return;
      }}
      const targetBottomGap = 18;
      const targetY = Math.max(visibleTop + 44, visibleBottom - targetBottomGap - 36);
      const cueBottom = Math.max(18, Math.round(rect.bottom - targetY));
      mapEl.style.setProperty("--details-cue-bottom", `${{cueBottom}}px`);
    }}

    function setupDetailsCuePosition() {{
      updateDetailsCuePosition();
      window.addEventListener("scroll", updateDetailsCuePosition, {{ passive: true }});
      window.addEventListener("resize", updateDetailsCuePosition);
      mobileViewport.addEventListener("change", updateDetailsCuePosition);
      if (window.visualViewport) {{
        window.visualViewport.addEventListener("resize", updateDetailsCuePosition);
        window.visualViewport.addEventListener("scroll", updateDetailsCuePosition);
      }}
    }}

    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({{
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }}[char]));
    }}

    function formatGeneratedAt() {{
      const generatedAt = document.getElementById("generated-at");
      if (!generatedAt) {{
        return;
      }}
      const dateTime = generatedAt.getAttribute("datetime");
      const date = new Date(dateTime);
      if (Number.isNaN(date.getTime())) {{
        return;
      }}
      generatedAt.textContent = date.toLocaleString([], {{
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit"
      }});
      generatedAt.title = dateTime;
    }}

    function setCheckedAt(value) {{
      const generatedAt = document.getElementById("generated-at");
      if (!generatedAt || !value) {{
        return;
      }}
      generatedAt.setAttribute("datetime", value);
      formatGeneratedAt();
    }}

    function setupStaleRefresh() {{
      const generatedAt = document.getElementById("generated-at");
      const notice = document.getElementById("stale-notice");
      const noticeText = document.getElementById("stale-notice-text");
      const refreshButton = document.getElementById("refresh-page");
      const dismissButton = document.getElementById("dismiss-stale-notice");
      const autoRefreshToggle = document.getElementById("auto-refresh-enabled");
      if (!generatedAt || !notice || !noticeText || !refreshButton || !dismissButton || !autoRefreshToggle) {{
        return;
      }}
      const generatedTime = new Date(generatedAt.getAttribute("datetime")).getTime();
      if (Number.isNaN(generatedTime)) {{
        return;
      }}
      let dismissed = false;
      let checkInFlight = false;
      let lastCheckedAt = 0;
      let lastHealthyCheckAt = generatedTime;
      autoRefreshToggle.checked = window.localStorage.getItem("chp-auto-refresh") === "enabled";
      const refresh = () => fetchIncidentData({{ force: true, preserveViewport: true }});
      refreshButton.addEventListener("click", refresh);
      autoRefreshToggle.addEventListener("change", () => {{
        window.localStorage.setItem("chp-auto-refresh", autoRefreshToggle.checked ? "enabled" : "disabled");
      }});
      dismissButton.addEventListener("click", () => {{
        dismissed = true;
        notice.classList.remove("is-visible");
      }});
      const showNotice = (message) => {{
        noticeText.textContent = message;
        notice.classList.add("is-visible");
      }};
      const hideNotice = () => {{
        notice.classList.remove("is-visible");
      }};
      const checkForUpdates = async () => {{
        if (dismissed || checkInFlight) {{
          return;
        }}
        const now = Date.now();
        if (now - lastCheckedAt < 30000) {{
          return;
        }}
        checkInFlight = true;
        lastCheckedAt = now;
        try {{
          const url = new URL(statusEndpoint, window.location.origin);
          url.searchParams.set("hours", new URLSearchParams(window.location.search).get("hours") || String(currentDataStatus.hours || 72));
          url.searchParams.set("region", currentRegion);
          url.searchParams.set("check", String(now));
          const response = await fetch(url, {{
            cache: "no-store",
            headers: {{ "Accept": "application/json" }}
          }});
          if (!response.ok) {{
            return;
          }}
          const latest = await response.json();
          lastHealthyCheckAt = Date.now();
          if (latest.checked_at) {{
            setCheckedAt(latest.checked_at);
          }}
          updateRegionCounts(latest.region_statuses);
          if (latest.version && latest.version !== currentDataStatus.version) {{
            if (autoRefreshToggle.checked) {{
              await fetchIncidentData({{ force: true, preserveViewport: true, status: latest }});
              return;
            }}
            showNotice("New incident data is available.");
          }} else {{
            hideNotice();
          }}
        }} catch (_error) {{
          // Keep the UI quiet on transient network failures.
        }} finally {{
          checkInFlight = false;
        }}
      }};
      const update = () => {{
        if (dismissed) {{
          return;
        }}
        const now = Date.now();
        const pageAgeMs = now - generatedTime;
        const healthAgeMs = now - lastHealthyCheckAt;
        if (pageAgeMs > 60000 && document.visibilityState === "visible") {{
          checkForUpdates();
        }}
        if (healthAgeMs > 180000 && !notice.classList.contains("is-visible")) {{
          showNotice("Data may be stale. Background status checks are not confirming current data.");
        }}
      }};
      update();
      window.setInterval(update, 15000);
    }}

    function formatIncidentWhen(incident) {{
      const dateText = incident.incident_date || (incident.first_seen || "").slice(0, 10);
      if (!dateText) {{
        return incident.incident_time || "";
      }}
      const parsed = new Date(`${{dateText}}T12:00:00`);
      if (Number.isNaN(parsed.getTime())) {{
        return `${{dateText}} ${{incident.incident_time || ""}}`.trim();
      }}
      return `${{parsed.toLocaleDateString([], {{ month: "short", day: "numeric" }})}}, ${{incident.incident_time || ""}}`.trim();
    }}

    function formatRangeLabel(hours) {{
      const numericHours = Number(hours);
      if (numericHours === 168) {{
        return "7d";
      }}
      if (numericHours === 720) {{
        return "30d";
      }}
      if (Number.isFinite(numericHours)) {{
        return `${{Number.isInteger(numericHours) ? numericHours : hours}}h`;
      }}
      return "current range";
    }}

    function incidentFromUrl() {{
      const selectedKey = new URLSearchParams(window.location.search).get("incident");
      if (!selectedKey) {{
        return null;
      }}
      return incidents.find((incident) => incident.event_key === selectedKey) || null;
    }}

    function updateIncidentUrl(incident) {{
      if (!incident || !window.history?.replaceState) {{
        return;
      }}
      const url = incidentUrl(incident);
      window.history.replaceState({{ incident: incident.event_key }}, "", url);
    }}

    function defaultViewUrl() {{
      const url = new URL(window.location.href);
      url.searchParams.delete("incident");
      ["nocache", "verify", "align", "details", "tapcheck", "markertouch", "statusapi"].forEach((key) => {{
        url.searchParams.delete(key);
      }});
      return url;
    }}

    function showDefaultView() {{
      if (window.history?.replaceState) {{
        window.history.replaceState({{ region: currentRegion }}, "", defaultViewUrl());
      }}
      selectedIncidentKey = null;
      incidents = incidents.filter((incident) => !incident._linked_outside_window);
      render({{ updateUrl: false }});
    }}

    function ensureCurrentRegionUrl() {{
      if (!window.history?.replaceState) {{
        return;
      }}
      const url = new URL(window.location.href);
      if (url.searchParams.get("region") === currentRegion) {{
        return;
      }}
      url.searchParams.set("region", currentRegion);
      window.history.replaceState({{ region: currentRegion }}, "", url);
    }}

    function incidentUrl(incident) {{
      const url = new URL(window.location.href);
      url.searchParams.set("region", currentRegion);
      url.searchParams.set("incident", incident.event_key);
      ["nocache", "verify", "align", "details", "tapcheck", "markertouch", "statusapi"].forEach((key) => {{
        url.searchParams.delete(key);
      }});
      return url;
    }}

    async function copyIncidentLink(incident, button) {{
      const link = incidentUrl(incident).toString();
      try {{
        await navigator.clipboard.writeText(link);
        button.textContent = "Copied";
        window.setTimeout(() => {{
          button.textContent = "Copy link";
        }}, 1800);
      }} catch (_error) {{
        window.prompt("Copy incident link", link);
      }}
    }}

    function updateListScrollCue() {{
      if (!listShell || !list) {{
        return;
      }}
      const hasMoreAbove = list.scrollTop > 3;
      const hasMoreBelow = list.scrollTop + list.clientHeight < list.scrollHeight - 3;
      listShell.classList.toggle("has-more-above", hasMoreAbove);
      listShell.classList.toggle("has-more-below", hasMoreBelow);
      if (scrollIncidentsButton) {{
        scrollIncidentsButton.disabled = !hasMoreBelow;
      }}
    }}

    function scrollIncidentListDown() {{
      if (!list) {{
        return;
      }}
      const nextTop = Math.min(
        list.scrollTop + Math.max(88, Math.floor(list.clientHeight * 0.9)),
        list.scrollHeight - list.clientHeight
      );
      list.scrollTo({{ top: nextTop, behavior: "smooth" }});
      window.setTimeout(updateListScrollCue, 250);
    }}

    function markerIcon(incident, selected = false, pulsing = false) {{
      const isActive = incident.status === "active";
      const size = 22;
      return L.divIcon({{
        className: [
          "incident-marker",
          isActive ? "is-active" : "is-cleared",
          selected ? "is-selected" : "",
          pulsing ? "is-pulsing" : ""
        ].join(" "),
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
        html: '<span class="incident-marker-dot" aria-hidden="true"></span>'
      }});
    }}

    function bindMarkerInteraction(marker, incident) {{
      let lastSelect = 0;
      const selectFromMarker = (event) => {{
        if (event) {{
          L.DomEvent.stop(event);
        }}
        const now = Date.now();
        if (now - lastSelect < 350 || (event?.type === "click" && now - lastSelect < 700)) {{
          return;
        }}
        lastSelect = now;
        selectIncident(incident, {{ pan: false, revealDetails: true, pulse: true }});
      }};

      const bindElement = () => {{
        const element = marker.getElement();
        if (!element) {{
          return;
        }}
        L.DomEvent.disableClickPropagation(element);
        L.DomEvent.on(element, "touchend", selectFromMarker);
        L.DomEvent.on(element, "pointerup", selectFromMarker);
        L.DomEvent.on(element, "click", selectFromMarker);
      }};

      marker.on("click", selectFromMarker);
      marker.on("add", bindElement);
      bindElement();
    }}

    function detailHtml(incident) {{
      if (!incident) {{
        return '<div class="empty">Select an incident to view CHP detail entries.</div>';
      }}
      const isActive = incident.status === "active";
      const statusClass = isActive ? "status-active" : "status-cleared";
      const statusText = isActive ? "Active" : "Cleared";
      const groupedDetails = new Map();
      (incident.detail_entries || []).forEach((entry) => {{
        const fallbackSection = String(entry.text || "").startsWith("Unit ")
          ? "Unit Information"
          : "Detail Information";
        const section = entry.section || fallbackSection;
        if (!groupedDetails.has(section)) {{
          groupedDetails.set(section, []);
        }}
        groupedDetails.get(section).push(entry);
      }});
      const details = Array.from(groupedDetails.entries()).map(([section, entries]) => `
        <div class="detail-subsection">
          <h3>${{escapeHtml(section)}}</h3>
          <ol class="detail-log">
            ${{entries.map((entry) => `
              <li>
                <time>${{escapeHtml(entry.time)}} · Entry ${{escapeHtml(entry.entry_no)}}</time>
                <div>${{escapeHtml(entry.text)}}</div>
              </li>
            `).join("")}}
          </ol>
        </div>
      `).join("");
      const coordText = incident.latitude == null || incident.longitude == null
        ? '<span class="mapless">No coordinates exposed by CHP for this incident.</span>'
        : `${{escapeHtml(incident.latitude)}}, ${{escapeHtml(incident.longitude)}}`;
      const linkedNotice = incident._linked_outside_window
        ? `<div class="empty">This linked incident is outside the selected ${{escapeHtml(currentDataStatus.hours)}}h window.</div>`
        : "";
      const defaultButton = new URLSearchParams(window.location.search).get("incident")
        ? `<button type="button" class="default-view" data-default-view>Back to ${{escapeHtml(formatRangeLabel(currentDataStatus.hours))}}</button>`
        : "";
      return `
        <div class="detail-panel">
          <div class="detail-header">
            <div class="detail-title">
              <div class="status-pill ${{statusClass}}">${{statusText}}</div>
              <h2>${{escapeHtml(incident.type || "CHP Incident")}}</h2>
              <div class="meta">${{escapeHtml(incident.location || "")}}</div>
            </div>
            <div class="detail-actions">
              ${{defaultButton}}
              <button type="button" class="share-incident" data-share-incident="${{escapeHtml(incident.event_key)}}">Copy link</button>
            </div>
          </div>
          ${{linkedNotice}}
          <section class="detail-section">
            <dl class="detail-grid">
              <dt>Incident</dt><dd>${{escapeHtml(incident.incident_no)}}</dd>
              <dt>Time</dt><dd>${{escapeHtml(incident.incident_time)}}</dd>
              <dt>Area</dt><dd>${{escapeHtml(incident.area)}}</dd>
              <dt>Loc Desc</dt><dd>${{escapeHtml(incident.location_desc || "")}}</dd>
              <dt>Coords</dt><dd>${{coordText}}</dd>
              <dt>First Seen</dt><dd>${{escapeHtml(incident.first_seen)}}</dd>
              <dt>Last Seen</dt><dd>${{escapeHtml(incident.last_seen)}}</dd>
              ${{incident.cleared_at ? `<dt>Cleared</dt><dd>${{escapeHtml(incident.cleared_at)}}</dd>` : ""}}
            </dl>
          </section>
          <section class="detail-section">
            ${{details || '<div class="empty">No detail entries captured.</div>'}}
          </section>
        </div>
      `;
    }}

    function selectIncident(incident, options = {{}}) {{
      if (!incident) {{
        return;
      }}
      selectedIncidentKey = incident.event_key;
      detailsPanel.innerHTML = detailHtml(incident);
      document.querySelectorAll(".incident").forEach((button) => {{
        button.setAttribute("aria-current", button.dataset.eventKey === incident.event_key ? "true" : "false");
        if (options.revealList && button.dataset.eventKey === incident.event_key) {{
          button.scrollIntoView({{ block: "nearest" }});
        }}
      }});
      markers.forEach((marker, eventKey) => {{
        const selected = eventKey === incident.event_key;
        const markerIncident = incidents.find((item) => item.event_key === eventKey);
        if (!markerIncident) {{
          return;
        }}
        marker.setIcon(markerIcon(markerIncident, selected, selected && options.pulse));
        marker.setZIndexOffset(selected ? 1000 : 0);
        if (selected && marker.bringToFront) {{
          marker.bringToFront();
        }}
      }});
      const marker = markers.get(incident.event_key);
      if (marker && options.pan !== false) {{
        map.setView([incident.latitude, incident.longitude], Math.max(map.getZoom(), 13));
      }}
      if (options.revealDetails && window.matchMedia("(max-width: 760px)").matches) {{
        detailsPanel.scrollIntoView({{ behavior: "smooth", block: "start" }});
      }}
      if (options.updateUrl !== false) {{
        updateIncidentUrl(incident);
      }}
    }}

    detailsCue?.addEventListener("click", () => {{
      detailsPanel.scrollIntoView({{ behavior: "smooth", block: "start" }});
    }});

    detailsPanel.addEventListener("click", (event) => {{
      const defaultButton = event.target.closest("[data-default-view]");
      if (defaultButton) {{
        showDefaultView();
        return;
      }}
      const button = event.target.closest("[data-share-incident]");
      if (!button) {{
        return;
      }}
      const incident = incidents.find((item) => item.event_key === button.dataset.shareIncident);
      if (incident) {{
        copyIncidentLink(incident, button);
      }}
    }});

    function updateRegionCounts(regionStatuses) {{
      if (!regionStatuses) {{
        return;
      }}
      document.querySelectorAll(".region-tab").forEach((tab) => {{
        const region = new URL(tab.href).searchParams.get("region");
        const countEl = tab.querySelector(".region-active-count");
        if (!region || !countEl || !regionStatuses[region]) {{
          return;
        }}
        const activeCount = Number(regionStatuses[region].active_count || 0);
        countEl.textContent = String(activeCount);
        countEl.setAttribute(
          "aria-label",
          `${{activeCount}} active incident${{activeCount === 1 ? "" : "s"}}`
        );
      }});
    }}

    function updateSummary(status, regionStatuses = null) {{
      if (!status) {{
        return;
      }}
      const hours = Number(status.hours);
      const hoursLabel = Number.isInteger(hours) ? String(hours) : String(status.hours);
      const meta = document.querySelector("header .meta");
      if (meta) {{
        meta.textContent = `${{status.active_count}} active · ${{status.total_count}} in last ${{hoursLabel}}h · ${{status.mapped_count}} mapped`;
      }}
      updateRegionCounts(regionStatuses || status.region_statuses);
      currentDataStatus = status;
      window.chpLiveMap.status = status;
    }}

    function clearRenderedIncidents() {{
      markers.forEach((marker) => marker.remove());
      markers.clear();
      list.innerHTML = "";
    }}

    function render(options = {{}}) {{
      clearRenderedIncidents();
      window.chpLiveMap.incidents = incidents;
      if (!incidents.length) {{
        list.innerHTML = '<div class="empty">No active matching CHP incidents are currently stored.</div>';
        detailsPanel.innerHTML = '<div class="empty">No active matching CHP incidents are currently stored.</div>';
        ensureCurrentRegionUrl();
        updateListScrollCue();
        return;
      }}

      incidents.forEach((incident) => {{
        const hasCoords = incident.latitude != null && incident.longitude != null;
        const isActive = incident.status === "active";
        const linkedOutsideWindow = Boolean(incident._linked_outside_window);
        if (hasCoords) {{
          const marker = L.marker([incident.latitude, incident.longitude], {{
            icon: markerIcon(incident),
            keyboard: false,
            title: `${{incident.type || "CHP Incident"}} ${{incident.location || ""}}`.trim()
          }}).addTo(map);
          bindMarkerInteraction(marker, incident);
          markers.set(incident.event_key, marker);
        }}

        const button = document.createElement("button");
        button.className = "incident";
        button.type = "button";
        button.dataset.eventKey = incident.event_key;
        button.innerHTML = `
          <span class="incident-heading">
            <span class="status-pill ${{isActive ? "status-active" : "status-cleared"}}">${{isActive ? "Active" : "Cleared"}}</span>
            <span class="selected-pill">Open</span>
            ${{linkedOutsideWindow ? '<span class="linked-pill">Linked</span>' : ""}}
          </span>
          <strong>${{escapeHtml(incident.type || "CHP Incident")}}</strong>
          <span>${{escapeHtml(incident.location)}}</span>
          <span>${{escapeHtml(formatIncidentWhen(incident))}} · ${{escapeHtml(incident.area)}} · #${{escapeHtml(incident.incident_no)}}${{hasCoords ? "" : " · no map pin"}}</span>
        `;
        button.addEventListener("click", () => selectIncident(incident, {{ pulse: true }}));
        list.appendChild(button);
      }});

      setTimeout(() => map.invalidateSize(), 50);
      window.requestAnimationFrame(updateListScrollCue);
      const linkedIncident = incidentFromUrl();
      const preservedIncident = selectedIncidentKey
        ? incidents.find((incident) => incident.event_key === selectedIncidentKey)
        : null;
      const selectedIncident = linkedIncident || preservedIncident || incidents[0];
      selectIncident(selectedIncident, {{
        pan: Boolean(linkedIncident) && !options.preserveViewport,
        revealList: Boolean(linkedIncident),
        updateUrl: options.updateUrl !== false
      }});
    }}

    async function fetchIncidentData(options = {{}}) {{
      const hours = new URLSearchParams(window.location.search).get("hours") || String(currentDataStatus.hours || 72);
      const url = new URL(incidentsEndpoint, window.location.origin);
      url.searchParams.set("hours", hours);
      url.searchParams.set("region", currentRegion);
      if (selectedIncidentKey) {{
        url.searchParams.set("incident", selectedIncidentKey);
      }}
      const version = options.status?.version || currentDataStatus.version;
      if (version) {{
        url.searchParams.set("v", version);
      }}
      if (options.force) {{
        url.searchParams.set("check", String(Date.now()));
      }}
      const response = await fetch(url, {{
        cache: options.force ? "no-store" : "default",
        headers: {{ "Accept": "application/json" }}
      }});
      if (!response.ok) {{
        throw new Error(`incident API returned ${{response.status}}`);
      }}
      const payload = await response.json();
      incidents = payload.incidents || [];
      updateSummary(payload.status || options.status || currentDataStatus, payload.region_statuses);
      if (payload.checked_at) {{
        setCheckedAt(payload.checked_at);
      }}
      render({{ preserveViewport: Boolean(options.preserveViewport) }});
      document.getElementById("stale-notice")?.classList.remove("is-visible");
      return payload;
    }}

    fetchIncidentData().catch(() => {{
      render();
      const noticeText = document.getElementById("stale-notice-text");
      const notice = document.getElementById("stale-notice");
      if (noticeText && notice) {{
        noticeText.textContent = "Incident data could not be loaded.";
        notice.classList.add("is-visible");
      }}
    }});
    formatGeneratedAt();
    setupStaleRefresh();
    setupDoubleTapZoom();
    setupDetailsCuePosition();
    list.addEventListener("scroll", updateListScrollCue, {{ passive: true }});
    scrollIncidentsButton?.addEventListener("click", scrollIncidentListDown);
    window.addEventListener("resize", updateListScrollCue);
  </script>
</body>
</html>
"""


def incident_road(incident):
    text = f"{incident.get('location') or ''} {incident.get('location_desc') or ''}".lower()
    if "angeles crest" in text or "red box" in text:
        return "Angeles Crest"
    if "angeles forest" in text:
        return "Angeles Forest"
    if "big tujunga" in text:
        return "Big Tujunga"
    if "glendora" in text:
        return "Glendora Mountain"
    if "mt wilson" in text or "mount wilson" in text:
        return "Mt Wilson"
    return "Other forest roads"


def format_when_short(incident):
    date_text = incident.get("incident_date") or (incident.get("first_seen") or "")[:10]
    time_text = incident.get("incident_time") or ""
    if not date_text:
        return time_text
    try:
        parsed = dt.datetime.fromisoformat(f"{date_text}T12:00:00")
        return f"{parsed.strftime('%b')} {parsed.day}, {time_text}".strip().rstrip(",")
    except ValueError:
        return f"{date_text} {time_text}".strip()


def count_by(items, key_fn):
    counts = {}
    for item in items:
        key = key_fn(item) or "Unknown"
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


def slugify_filter(value):
    return str(value or "").strip().lower().replace("&", "and").replace("/", "-").replace(" ", "-")


def option_tags(options, selected):
    return "".join(
        '<option value="{}"{}>{}</option>'.format(
            html.escape(value),
            ' selected' if value == selected else "",
            html.escape(label),
        )
        for value, label in options
    )


def filtered_history_incidents(incidents, filters):
    query = (filters.get("q") or "").strip().lower()
    road = filters.get("road") or "all"
    incident_type = filters.get("type") or "all"
    status = filters.get("status") or "all"
    mapped = filters.get("mapped") or "all"
    filtered = []
    for incident in incidents:
        haystack = " ".join(
            str(incident.get(field) or "")
            for field in ("incident_no", "type", "location", "location_desc", "area", "incident_time")
        ).lower()
        has_coords = incident.get("latitude") is not None and incident.get("longitude") is not None
        if query and query not in haystack:
            continue
        if road != "all" and slugify_filter(incident_road(incident)) != road:
            continue
        if incident_type != "all" and slugify_filter(incident.get("type") or "Unknown") != incident_type:
            continue
        if status != "all" and (incident.get("status") or "") != status:
            continue
        if mapped == "mapped" and not has_coords:
            continue
        if mapped == "unpinned" and has_coords:
            continue
        filtered.append(incident)
    return filtered


def report_rows(counts, limit=5):
    if not counts:
        return '<div class="empty-report">No incidents in this window.</div>'
    max_count = max(count for _label, count in counts) or 1
    rows = []
    visible_counts = counts if limit is None else counts[:limit]
    for label, count in visible_counts:
        rows.append(
            '<div class="bar-row"><span>{}</span><div class="bar"><i style="width: {}%;"></i></div><span>{}</span></div>'.format(
                html.escape(label),
                max(8, round((count / max_count) * 100)),
                count,
            )
        )
    return "".join(rows)


def incident_day_key(incident):
    date_text = incident.get("incident_date") or (incident.get("first_seen") or "")[:10]
    if not date_text:
        return "Unknown", "Unknown"
    try:
        parsed = dt.datetime.fromisoformat(f"{date_text}T12:00:00")
        return date_text, f"{parsed.strftime('%a')}, {parsed.strftime('%b')} {parsed.day}"
    except ValueError:
        return date_text, date_text


def incident_hour(incident):
    time_text = incident.get("incident_time") or ""
    try:
        return dt.datetime.strptime(time_text.strip(), "%I:%M %p").hour
    except ValueError:
        return None


def time_bucket_for_incident(incident):
    hour = incident_hour(incident)
    if hour is None:
        return "Unknown"
    if hour < 6:
        return "Overnight"
    if hour < 12:
        return "Morning"
    if hour < 18:
        return "Afternoon"
    return "Evening"


def daily_incident_counts(incidents):
    counts = {}
    labels = {}
    for incident in incidents:
        key, label = incident_day_key(incident)
        counts[key] = counts.get(key, 0) + 1
        labels[key] = label
    return [(labels[key], counts[key]) for key in sorted(counts)]


def time_bucket_counts(incidents):
    counts = {label: 0 for label in ("Overnight", "Morning", "Afternoon", "Evening", "Unknown")}
    for incident in incidents:
        counts[time_bucket_for_incident(incident)] += 1
    return [(label, count) for label, count in counts.items() if count]


def report_shell(
    title,
    subtitle,
    body,
    hours,
    base_path="/",
    public_url=None,
    current="summary",
    status=None,
    region="forest",
    region_statuses=None,
):
    region = normalize_region(region)
    label = region_label(region)
    status = status or {"active_count": 0, "version": "empty", "region": region}
    status = {**status, "region": region}
    urls = metadata_urls(
        base_path,
        public_url,
        {"active": 1 if status["active_count"] else 0, "v": status["version"]},
    )
    description = f"Summary and history views for CHP {label.lower()} road incidents."
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - CHP {html.escape(label)} Incidents</title>
  <meta name="description" content="{html.escape(description)}">
  <meta name="robots" content="index,follow,max-image-preview:large">
  <link rel="canonical" href="{html.escape(urls["canonical"])}">
  <link rel="icon" href="{html.escape(urls["favicon"])}" type="image/svg+xml">
  <style>
    html, body {{
      min-height: 100%;
      margin: 0;
      color: #182026;
      background: #f6f7f4;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    body {{
      display: flex;
      justify-content: center;
    }}
    #report-app {{
      width: min(100%, 860px);
      min-height: 100vh;
      background: #fbfcf8;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 5;
      padding: 8px 12px;
      border-bottom: 1px solid #d8ddd2;
      background: rgba(251, 252, 248, 0.98);
    }}
    .title-row {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }}
    .report-nav {{
      margin-top: 8px;
    }}
    h1 {{
      margin: 0 0 3px;
      font-size: 18px;
      line-height: 1.1;
    }}
    .meta {{
      color: #58645d;
      font-size: 11px;
      line-height: 1.25;
    }}
    .view-menu {{
      display: block;
      position: relative;
      flex: 0 0 auto;
    }}
    .view-menu summary {{
      display: flex;
      align-items: center;
      justify-content: center;
      width: 30px;
      height: 30px;
      border: 1px solid #d8ddd2;
      border-radius: 6px;
      background: #fff;
      font-size: 15px;
      font-weight: 900;
      cursor: pointer;
      list-style: none;
    }}
    .view-menu summary::-webkit-details-marker {{
      display: none;
    }}
    .view-menu-popover {{
      position: absolute;
      top: 36px;
      right: 0;
      z-index: 10;
      width: min(270px, calc(100vw - 24px));
      padding: 6px;
      border: 1px solid #d8ddd2;
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 10px 28px rgba(24, 32, 38, 0.18);
    }}
    .view-menu-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 36px;
      padding: 0 8px;
      border-radius: 6px;
      color: #182026;
      font-size: 13px;
      font-weight: 800;
      text-decoration: none;
    }}
    .view-menu-row span {{
      color: #46534b;
      font-size: 12px;
      font-weight: 700;
    }}
    .view-menu-row.is-active,
    .view-menu-row:hover,
    .view-menu-row:focus {{
      color: #1f6840;
      background: #eef7ee;
      outline: none;
    }}
    .view-tabs {{
      display: none;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 2px;
      margin-top: 0;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
    }}
    .view-tab {{
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 28px;
      padding: 0 3px;
      border-radius: 4px;
      color: #3f4a44;
      font-size: 11px;
      font-weight: 800;
      line-height: 1;
      text-align: center;
      text-decoration: none;
    }}
    .view-tab:hover,
    .view-tab:focus {{
      background: #ffffff;
      outline: none;
    }}
    .view-tab.is-active {{
      color: #1f6840;
      background: transparent;
      box-shadow: inset 0 -2px 0 #277447;
    }}
    .range-tabs {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 3px;
      margin-top: 0;
      padding: 3px;
      border: 1px solid #d8ddd2;
      border-radius: 8px;
      background: #eef1ea;
    }}
    .range-tab {{
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 23px;
      padding: 0 7px;
      border-radius: 5px;
      color: #3f4a44;
      font-size: 11px;
      font-weight: 800;
      line-height: 1;
      text-align: center;
      text-decoration: none;
    }}
    .range-tab:hover,
    .range-tab:focus {{
      background: #ffffff;
      outline: none;
    }}
    .range-tab.is-active {{
      color: #ffffff;
      background: #277447;
    }}
    .region-tabs {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 3px;
      margin-top: 10px;
      padding: 3px;
      border: 1px solid #d8ddd2;
      border-radius: 8px;
      background: #eef1ea;
    }}
    .region-tab {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 5px;
      min-height: 23px;
      padding: 0 7px;
      border-radius: 5px;
      color: #3f4a44;
      font-size: 11px;
      font-weight: 800;
      line-height: 1;
      text-align: center;
      text-decoration: none;
    }}
    .region-tab:hover,
    .region-tab:focus {{
      background: #ffffff;
      outline: none;
    }}
    .region-tab.is-active {{
      color: #ffffff;
      background: #277447;
    }}
    .region-active-count {{
      min-width: 14px;
      padding: 2px 4px;
      border-radius: 999px;
      color: #3f4a44;
      background: rgba(255, 255, 255, 0.72);
      font-size: 9px;
      font-weight: 900;
      line-height: 1;
    }}
    .region-tab.is-active .region-active-count {{
      color: #1f6840;
      background: #ffffff;
    }}
    .secondary-tabs {{
      display: contents;
      margin-top: 6px;
    }}
    .secondary-tabs .region-tabs {{
      margin-top: 6px;
    }}
    main {{
      padding: 14px 16px 30px;
    }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
    }}
    .kpi, .filter, .search-box {{
      border: 1px solid #d8ddd2;
      border-radius: 8px;
      background: #fff;
    }}
    .kpi {{
      min-height: 72px;
      padding: 12px;
    }}
    .kpi strong {{
      display: block;
      margin-bottom: 3px;
      font-size: 26px;
      line-height: 1;
    }}
    .kpi span, .empty-report {{
      color: #58645d;
      font-size: 13px;
      line-height: 1.35;
    }}
    .section {{
      margin-top: 15px;
      padding-top: 15px;
      border-top: 1px solid #d8ddd2;
    }}
    h2 {{
      margin: 0 0 9px;
      font-size: 20px;
      line-height: 1.2;
    }}
    .bar-row {{
      display: grid;
      grid-template-columns: minmax(96px, 150px) 1fr 32px;
      gap: 8px;
      align-items: center;
      min-height: 31px;
      color: #405047;
      font-size: 13px;
    }}
    .bar {{
      height: 9px;
      overflow: hidden;
      border-radius: 999px;
      background: #e5eae3;
    }}
    .bar i {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: #277447;
    }}
    .search-box {{
      display: flex;
      align-items: center;
      min-height: 42px;
      margin-top: 13px;
      padding: 0 12px;
      font-size: 14px;
      color: #182026;
      font: inherit;
      width: 100%;
    }}
    .search-box::placeholder {{
      color: #58645d;
    }}
    .filter-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
      margin-top: 10px;
    }}
    .filter {{
      min-height: 40px;
      padding: 9px 10px;
      color: #405047;
      font: inherit;
      font-size: 13px;
      font-weight: 800;
    }}
    .filter-actions {{
      display: flex;
      gap: 9px;
      margin-top: 10px;
    }}
    .filter-actions button,
    .filter-actions a {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      padding: 0 12px;
      border: 1px solid #cbd6cc;
      border-radius: 8px;
      color: #1f6840;
      background: #f8faf6;
      font: inherit;
      font-size: 13px;
      font-weight: 850;
      text-decoration: none;
      cursor: pointer;
    }}
    .filter-actions button {{
      color: #ffffff;
      border-color: #277447;
      background: #277447;
    }}
    .result {{
      padding: 13px 0;
      border-bottom: 1px solid #d8ddd2;
    }}
    .result strong {{
      display: block;
      margin-bottom: 4px;
      font-size: 16px;
      line-height: 1.2;
    }}
    .result span {{
      display: block;
      color: #58645d;
      font-size: 13px;
      line-height: 1.35;
    }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      margin-bottom: 7px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .status-active {{
      color: #8f1d21;
      background: #fde7df;
    }}
    .status-cleared {{
      color: #59615c;
      background: #ecefed;
    }}
    @media (min-width: 760px) {{
      #report-app {{
        margin: 18px;
        border: 1px solid #d8ddd2;
        border-radius: 10px;
        overflow: hidden;
        box-shadow: 0 8px 30px rgba(24, 32, 38, 0.08);
      }}
      main {{
        padding: 18px;
      }}
      header {{
        padding: 18px;
      }}
      h1 {{
        margin-bottom: 5px;
        font-size: 24px;
      }}
      .meta {{
        font-size: 14px;
        line-height: 1.35;
      }}
      .view-menu {{
        display: block;
      }}
      .kpi-grid {{
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }}
      .secondary-tabs {{
        display: contents;
      }}
      .view-tabs {{
        display: grid;
        gap: 3px;
        margin-top: 10px;
        padding: 3px;
        border: 1px solid #d8ddd2;
        border-radius: 8px;
        background: #eef1ea;
      }}
      .view-tab {{
        min-height: 34px;
        padding: 0 7px;
        border-radius: 5px;
        font-size: 13px;
      }}
      .range-tab,
      .region-tab {{
        min-height: 34px;
        font-size: 13px;
      }}
      .view-tab.is-active {{
        color: #ffffff;
        background: #277447;
        box-shadow: none;
      }}
    }}
    @media (min-width: 980px) {{
      body {{
        display: block;
      }}
      #report-app {{
        width: min(calc(100% - 48px), 1180px);
        margin: 24px auto;
      }}
      header {{
        padding: 22px 24px;
      }}
      .report-header-layout {{
        display: grid;
        grid-template-columns: minmax(280px, 1fr) minmax(430px, 520px);
        gap: 24px;
        align-items: start;
      }}
      .title-row {{
        min-height: 86px;
      }}
      h1 {{
        font-size: 30px;
      }}
      .meta {{
        font-size: 15px;
      }}
      .report-nav {{
        margin-top: 0;
      }}
      .view-tabs {{
        margin-top: 8px;
      }}
      main {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 16px;
        padding: 22px 24px 28px;
      }}
      main > form,
      main > .kpi-grid {{
        grid-column: 1 / -1;
      }}
      .report-history main > .section {{
        grid-column: 1 / -1;
      }}
      .section {{
        margin-top: 0;
        padding: 16px;
        border: 1px solid #d8ddd2;
        border-radius: 8px;
        background: #ffffff;
      }}
      .result:last-child {{
        border-bottom: 0;
      }}
    }}
  </style>
</head>
<body>
  <div id="report-app" class="report-{html.escape(current)}">
    <header>
      <div class="report-header-layout">
        <div class="title-row">
          <div>
            <h1>{html.escape(title)}</h1>
            <div class="meta">{html.escape(subtitle)}</div>
            <div class="meta">Window: last {hours:g}h</div>
          </div>
          {view_menu(base_path, current, hours, region)}
        </div>
        <div class="report-nav">
          <nav class="range-tabs" aria-label="History range">{history_controls(hours, region)}</nav>
          <div class="secondary-tabs">
            <nav class="region-tabs" aria-label="Region">{region_tabs(base_path, current, hours, region, region_statuses)}</nav>
            <nav class="view-tabs" aria-label="View navigation">{view_tabs(base_path, current, hours, region)}</nav>
          </div>
        </div>
      </div>
    </header>
    <main>{body}</main>
  </div>
</body>
</html>
"""


def build_summary_html(
    incidents,
    generated_at,
    hours,
    base_path="/",
    public_url=None,
    region="forest",
    region_statuses=None,
):
    region = normalize_region(region)
    label = region_label(region)
    status = {**incident_status(incidents, hours), "region": region}
    active_count = status["active_count"]
    mapped_count = status["mapped_count"]
    cleared_count = status["total_count"] - active_count
    road_rows = report_rows(count_by(incidents, incident_road))
    type_rows = report_rows(count_by(incidents, lambda incident: incident.get("type") or "Unknown"))
    day_rows = report_rows(daily_incident_counts(incidents), limit=None)
    time_rows = report_rows(time_bucket_counts(incidents), limit=None)
    recent = sorted(
        incidents,
        key=lambda incident: incident.get("latest_observed_at") or incident.get("last_seen") or "",
        reverse=True,
    )[:5]
    recent_html = "".join(
        '<div class="result"><span class="status-pill {}">{}</span><strong>{}</strong><span>{}</span><span>{} · #{}</span></div>'.format(
            "status-active" if incident.get("status") == "active" else "status-cleared",
            "Active" if incident.get("status") == "active" else "Cleared",
            html.escape(incident.get("type") or "CHP Incident"),
            html.escape(incident.get("location") or ""),
            html.escape(format_when_short(incident)),
            html.escape(str(incident.get("incident_no") or "")),
        )
        for incident in recent
    ) or '<div class="empty-report">No recent incidents in this window.</div>'
    body = f"""
      <section class="kpi-grid" aria-label="Incident summary">
        <div class="kpi"><strong>{status["total_count"]}</strong><span>Incidents in window</span></div>
        <div class="kpi"><strong>{active_count}</strong><span>Currently active</span></div>
        <div class="kpi"><strong>{mapped_count}</strong><span>Mapped incidents</span></div>
        <div class="kpi"><strong>{cleared_count}</strong><span>Cleared incidents</span></div>
      </section>
      <section class="section">
        <h2>Busiest Roads</h2>
        {road_rows}
      </section>
      <section class="section">
        <h2>Incident Types</h2>
        {type_rows}
      </section>
      <section class="section">
        <h2>Incidents by Day</h2>
        {day_rows}
      </section>
      <section class="section">
        <h2>Time of Day</h2>
        {time_rows}
      </section>
      <section class="section">
        <h2>Recent Changes</h2>
        {recent_html}
      </section>
    """
    subtitle = f"{label} CHP activity · updated {generated_at}"
    return report_shell(
        "Summary",
        subtitle,
        body,
        hours,
        base_path,
        public_url,
        current="summary",
        status=status,
        region=region,
        region_statuses=region_statuses,
    )


def build_history_html(
    incidents,
    generated_at,
    hours,
    base_path="/",
    public_url=None,
    filters=None,
    region="forest",
    region_statuses=None,
):
    region = normalize_region(region)
    label = region_label(region)
    status = {**incident_status(incidents, hours), "region": region}
    filters = filters or {}
    selected_road = filters.get("road") or "all"
    selected_type = filters.get("type") or "all"
    selected_status = filters.get("status") or "all"
    selected_mapped = filters.get("mapped") or "all"
    query = filters.get("q") or ""
    filtered_incidents = filtered_history_incidents(incidents, filters)
    road_options = [("all", "All roads")] + [
        (slugify_filter(label), label) for label, _count in count_by(incidents, incident_road)
    ]
    type_options = [("all", "All types")] + [
        (slugify_filter(label), label) for label, _count in count_by(incidents, lambda incident: incident.get("type") or "Unknown")
    ]
    status_options = [("all", "All statuses"), ("active", "Active"), ("cleared", "Cleared")]
    mapped_options = [("all", "Mapped + unpinned"), ("mapped", "Mapped only"), ("unpinned", "Unpinned only")]
    reset_href = href_with_query(app_path(base_path, "/history"), hours=f"{hours:g}", region=region)
    result_rows = "".join(
        '<div class="result"><span class="status-pill {}">{}</span><strong>{}</strong><span>{}</span><span>{} · {} · #{} · <a href="{}">Show on map</a></span></div>'.format(
            "status-active" if incident.get("status") == "active" else "status-cleared",
            "Active" if incident.get("status") == "active" else "Cleared",
            html.escape(incident.get("type") or "CHP Incident"),
            html.escape(incident.get("location") or ""),
            html.escape(format_when_short(incident)),
            html.escape(incident.get("area") or ""),
            html.escape(str(incident.get("incident_no") or "")),
            html.escape(
                href_with_query(
                    app_path(base_path, "/"),
                    hours=f"{hours:g}",
                    region=region,
                    incident=incident.get("event_key") or "",
                )
            ),
        )
        for incident in filtered_incidents
    ) or '<div class="empty-report">No incidents in this window.</div>'
    body = f"""
      <form method="get" action="{html.escape(app_path(base_path, "/history"))}" aria-label="History filters">
        <input type="hidden" name="hours" value="{hours:g}">
        <input type="hidden" name="region" value="{html.escape(region)}">
        <input class="search-box" type="search" name="q" value="{html.escape(query)}" placeholder="Search road, type, incident number...">
        <div class="filter-grid">
          <select class="filter" name="road" aria-label="Road filter">{option_tags(road_options, selected_road)}</select>
          <select class="filter" name="type" aria-label="Incident type filter">{option_tags(type_options, selected_type)}</select>
          <select class="filter" name="status" aria-label="Status filter">{option_tags(status_options, selected_status)}</select>
          <select class="filter" name="mapped" aria-label="Map pin filter">{option_tags(mapped_options, selected_mapped)}</select>
        </div>
        <div class="filter-actions">
          <button type="submit">Apply filters</button>
          <a href="{html.escape(reset_href)}">Reset</a>
        </div>
      </form>
      <section class="section">
        <div class="meta">{len(filtered_incidents)} of {len(incidents)} results · sorted newest first</div>
        {result_rows}
      </section>
    """
    subtitle = f"Search stored CHP {label.lower()} incidents · updated {generated_at}"
    return report_shell(
        "History",
        subtitle,
        body,
        hours,
        base_path,
        public_url,
        current="history",
        status=status,
        region=region,
        region_statuses=region_statuses,
    )


def build_about_html(
    incidents,
    generated_at,
    hours,
    base_path="/",
    public_url=None,
    region="forest",
    region_statuses=None,
):
    region = normalize_region(region)
    label = region_label(region)
    status = {**incident_status(incidents, hours), "region": region}
    if region == "forest":
        scope_text = "Angeles Crest, Angeles Forest, Big Tujunga, Glendora Mountain, and nearby forest roads"
    else:
        scope_text = "Malibu canyon and coastal roads including PCH-adjacent CHP incidents"
    body = f"""
      <section class="section" style="margin-top: 0; padding-top: 0; border-top: 0;">
        <h2>What This Is</h2>
        <p class="empty-report">Crestmap is a live mirror of public <a href="https://cad.chp.ca.gov/Traffic.aspx" rel="noopener">CHP CAD traffic incidents</a> for {html.escape(scope_text)}.</p>
      </section>
      <section class="kpi-grid" aria-label="Current data status" style="margin-top: 14px;">
        <div class="kpi"><strong>{status["total_count"]}</strong><span>Incidents in this window</span></div>
        <div class="kpi"><strong>{status["active_count"]}</strong><span>Currently active</span></div>
        <div class="kpi"><strong>{status["mapped_count"]}</strong><span>Mapped incidents</span></div>
        <div class="kpi"><strong>1m</strong><span>Approximate CHP check cadence</span></div>
      </section>
      <section class="section">
        <h2>Update Cadence</h2>
        <div class="result"><strong>Incident list</strong><span>Checked against CHP about once per minute.</span></div>
        <div class="result"><strong>Active incident details</strong><span>Unchanged active incidents are refreshed about every 3 minutes.</span></div>
        <div class="result"><strong>History</strong><span>Cleared incidents stay in the database and are shown when they fall inside the selected time window.</span></div>
      </section>
      <section class="section">
        <h2>Project Links</h2>
        <div class="result"><strong>CHP CAD source</strong><span><a href="https://cad.chp.ca.gov/Traffic.aspx" rel="noopener">cad.chp.ca.gov/Traffic.aspx</a></span></div>
        <div class="result"><strong>Project README</strong><span><a href="https://github.com/cajaks2/chp-live-map#readme" rel="noopener">github.com/cajaks2/chp-live-map</a></span></div>
      </section>
    """
    subtitle = f"{label} source, update cadence, and project context · updated {generated_at}"
    return report_shell(
        "About",
        subtitle,
        body,
        hours,
        base_path,
        public_url,
        current="about",
        status=status,
        region=region,
        region_statuses=region_statuses,
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a static live CHP incident map from the SQLite database."
    )
    parser.add_argument("--database", type=Path, default=Path("chp_traffic.sqlite"))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--output", type=Path, default=Path("live_chp_map.html"))
    parser.add_argument("--hours", type=float, default=72.0)
    return parser.parse_args()


def main():
    args = parse_args()
    generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    incidents = load_incidents(args.database, args.hours, args.database_url)
    args.output.write_text(build_html(incidents, generated_at, args.hours), encoding="utf-8")
    active_count = len([i for i in incidents if i.get("status") == "active"])
    log_event(
        "info",
        "Generated CHP live map",
        **{
            "event.action": "generate_map",
            "event.outcome": "success",
            "file.path": str(args.output),
            "chp.active_count": active_count,
            "chp.total_count": len(incidents),
            "chp.hours": args.hours,
        },
    )


if __name__ == "__main__":
    run_main(main)
