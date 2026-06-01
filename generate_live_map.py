import argparse
import datetime as dt
import hashlib
import html
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from ecs_logging import log_event, run_main


DEFAULT_CENTER = [34.32, -118.12]
DEFAULT_ZOOM = 10
HISTORY_PRESETS = [(24, "24h"), (72, "72h"), (168, "7d"), (720, "30d")]


def load_incidents(database, hours, database_url=None):
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
            WHERE e.status = 'active'
               OR e.first_seen >= %s
               OR e.last_seen >= %s
               OR e.cleared_at >= %s
            ORDER BY
                CASE WHEN e.status = 'active' THEN 0 ELSE 1 END,
                e.latest_observed_at DESC,
                e.incident_no DESC
            """,
            (cutoff, cutoff, cutoff),
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
            WHERE e.status = 'active'
               OR e.first_seen >= ?
               OR e.last_seen >= ?
               OR e.cleared_at >= ?
            ORDER BY
                CASE WHEN e.status = 'active' THEN 0 ELSE 1 END,
                e.latest_observed_at DESC,
                e.incident_no DESC
            """,
            (cutoff, cutoff, cutoff),
        ).fetchall()
    conn.close()
    incidents = []
    for row in rows:
        incident = dict(row)
        try:
            incident["detail_entries"] = json.loads(incident.pop("details_json") or "[]")
        except json.JSONDecodeError:
            incident["detail_entries"] = []
        incidents.append(incident)
    return incidents


def normalize_base_path(base_path):
    base = (base_path or "/").rstrip("/")
    return base or "/"


def metadata_urls(base_path, public_url):
    base = normalize_base_path(base_path)
    asset_base = "" if base == "/" else base
    fallback_url = base if base == "/" else f"{base}/"
    canonical_url = (public_url or fallback_url).rstrip("/") + "/"
    public_asset_base = canonical_url.rstrip("/") if public_url else asset_base
    return {
        "canonical": canonical_url,
        "favicon": f"{public_asset_base}/favicon.svg",
        "og_image": f"{public_asset_base}/og-image.svg",
    }


def history_controls(hours):
    current = int(hours)
    links = []
    for preset_hours, label in HISTORY_PRESETS:
        selected = preset_hours == current
        links.append(
            '<a class="range-tab{}" href="?hours={}"{}>{}</a>'.format(
                " is-active" if selected else "",
                preset_hours,
                ' aria-current="page"' if selected else "",
                html.escape(label),
            )
        )
    return "".join(links)


def incident_status(incidents, hours):
    mapped_count = len(
        [i for i in incidents if i.get("latitude") is not None and i.get("longitude") is not None]
    )
    active_count = len([i for i in incidents if i.get("status") == "active"])
    data_updated_at = max(
        [
            i.get("latest_observed_at") or i.get("last_seen") or i.get("first_seen") or ""
            for i in incidents
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
        for i in incidents
    ]
    version = hashlib.sha256(
        json.dumps(version_source, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "active_count": active_count,
        "total_count": len(incidents),
        "mapped_count": mapped_count,
        "hours": hours,
        "data_updated_at": data_updated_at,
        "version": version,
    }


def build_html(incidents, generated_at, hours, base_path="/", public_url=None):
    data = json.dumps(incidents, ensure_ascii=False)
    status = incident_status(incidents, hours)
    active_count = status["active_count"]
    mapped_count = status["mapped_count"]
    title = f"CHP Forest Incidents ({active_count} active, {status['total_count']} total)"
    description = (
        "Live CHP traffic incidents for Angeles Crest, Angeles Forest, Big Tujunga, "
        "Glendora Mountain, and nearby forest roads."
    )
    urls = metadata_urls(base_path, public_url)
    base = normalize_base_path(base_path)
    asset_base = "" if base == "/" else base
    if public_url:
        public_path = urlsplit(public_url).path.rstrip("/")
        status_endpoint = f"{public_path}/status.json" if public_path else "/status.json"
    else:
        status_endpoint = f"{asset_base}/status.json"
    structured_data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": f"{urls['canonical']}#website",
                "name": "CHP Forest Incidents",
                "url": urls["canonical"],
                "description": description,
                "inLanguage": "en-US",
            },
            {
                "@type": "WebApplication",
                "@id": f"{urls['canonical']}#app",
                "name": "CHP Forest Incidents",
                "url": urls["canonical"],
                "description": description,
                "applicationCategory": "MapApplication",
                "operatingSystem": "Any",
                "isAccessibleForFree": True,
                "areaServed": {
                    "@type": "Place",
                    "name": "Angeles National Forest and nearby Southern California mountain roads",
                },
                "about": [
                    "CHP CAD traffic incidents",
                    "Angeles Crest Highway",
                    "Angeles Forest Highway",
                    "Big Tujunga Canyon Road",
                    "Glendora Mountain Road",
                ],
            },
            {
                "@type": "Dataset",
                "@id": f"{urls['canonical']}#incident-history",
                "name": "CHP forest road incident history",
                "url": urls["canonical"],
                "description": (
                    "Rolling incident history collected from public CHP CAD pages for selected "
                    "Angeles National Forest roads. The scraper checks CHP about once a minute."
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
  <meta property="og:image:type" content="image/svg+xml">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{html.escape(title)}">
  <meta name="twitter:description" content="{html.escape(description)}">
  <meta name="twitter:image" content="{html.escape(urls["og_image"])}">
  <script type="application/ld+json">{structured_data_json}</script>
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
    .meta {{
      color: #58645d;
      font-size: 13px;
      line-height: 1.35;
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
    .incident-marker {{
      box-sizing: border-box;
      width: 22px;
      height: 22px;
      border: 3px solid #7a1a1d;
      border-radius: 999px;
      background: #d94a38;
      box-shadow: 0 1px 6px rgba(24, 32, 38, 0.32);
      cursor: pointer;
      pointer-events: auto;
      touch-action: manipulation;
      -webkit-tap-highlight-color: transparent;
    }}
    .incident-marker.is-cleared {{
      border-color: #5f6862;
      background: #b8bfba;
    }}
    .incident-marker.is-selected {{
      width: 28px;
      height: 28px;
      border-width: 4px;
      background: #f05a40;
      box-shadow: 0 2px 9px rgba(24, 32, 38, 0.42);
    }}
    .incident-marker.is-selected.is-cleared {{
      background: #9da5a0;
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
    .detail-panel {{
      padding: 18px;
    }}
    .detail-panel h2 {{
      margin: 0 0 6px;
      font-size: 18px;
      line-height: 1.25;
      letter-spacing: 0;
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
      background: #e4efe4;
      box-shadow: inset 3px 0 0 #277447;
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
      #incident-list-shell {{
        flex: 0 0 132px;
        min-height: 132px;
      }}
      #incident-list {{
        -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 10px, #000 calc(100% - 32px), transparent 100%);
        mask-image: linear-gradient(to bottom, transparent 0, #000 10px, #000 calc(100% - 32px), transparent 100%);
      }}
      #map {{
        height: 42vh;
        min-height: 280px;
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
        <h1>CHP Forest Incidents</h1>
        <div class="meta">{active_count} active · {len(incidents)} in last {hours:g}h · {mapped_count} mapped</div>
        <div class="meta">Last checked <time id="generated-at" datetime="{html.escape(generated_at)}">{html.escape(generated_at)}</time></div>
        <nav class="range-tabs" aria-label="History range">{history_controls(hours)}</nav>
        <details id="about-panel" class="about-panel" open>
          <summary>About this map</summary>
          <p class="about-blurb"><strong>What this is:</strong> a live mirror of public CHP CAD incidents for Angeles Crest, Angeles Forest, Big Tujunga, Glendora Mountain, and nearby forest roads. CHP is checked about once a minute; unchanged active incident details are refreshed about every 3 minutes. Cleared incidents stay visible inside the selected history window.</p>
          <a class="about-link" href="https://github.com/cajaks2/chp-live-map#readme" rel="noopener">Project README</a>
        </details>
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
    <main id="map"></main>
    <aside id="details"></aside>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const incidents = {data};
    const initialDataStatus = {json.dumps(status, ensure_ascii=False)};
    const statusEndpoint = "{html.escape(status_endpoint)}";

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
    }}).setView({json.dumps(DEFAULT_CENTER)}, {DEFAULT_ZOOM});
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
    const aboutPanel = document.getElementById("about-panel");
    window.chpLiveMap = {{ map, markers, incidents }};

    const mobileViewport = window.matchMedia("(max-width: 760px)");

    function syncAboutPanelForViewport() {{
      if (!aboutPanel) {{
        return;
      }}
      const storedState = window.localStorage.getItem("chp-about-panel");
      if (storedState === "open" || storedState === "closed") {{
        aboutPanel.open = storedState === "open";
        return;
      }}
      aboutPanel.open = !mobileViewport.matches;
    }}

    syncAboutPanelForViewport();
    mobileViewport.addEventListener("change", syncAboutPanelForViewport);
    aboutPanel?.addEventListener("toggle", () => {{
      window.localStorage.setItem("chp-about-panel", aboutPanel.open ? "open" : "closed");
    }});

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
      if (!generatedAt || !notice || !noticeText || !refreshButton || !dismissButton) {{
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
      const refresh = () => window.location.reload();
      refreshButton.addEventListener("click", refresh);
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
          url.searchParams.set("hours", new URLSearchParams(window.location.search).get("hours") || String(initialDataStatus.hours || 72));
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
          if (latest.version && latest.version !== initialDataStatus.version) {{
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
      const url = new URL(window.location.href);
      url.searchParams.set("incident", incident.event_key);
      window.history.replaceState({{ incident: incident.event_key }}, "", url);
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

    function markerIcon(incident, selected = false) {{
      const isActive = incident.status === "active";
      const size = selected ? 28 : 22;
      return L.divIcon({{
        className: ["incident-marker", isActive ? "is-active" : "is-cleared", selected ? "is-selected" : ""].join(" "),
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2]
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
        selectIncident(incident, {{ pan: false, revealDetails: true }});
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
      return `
        <div class="detail-panel">
          <div class="status-pill ${{statusClass}}">${{statusText}}</div>
          <h2>${{escapeHtml(incident.type || "CHP Incident")}}</h2>
          <div class="meta">${{escapeHtml(incident.location || "")}}</div>
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
        marker.setIcon(markerIcon(markerIncident, selected));
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

    function render() {{
      if (!incidents.length) {{
        list.innerHTML = '<div class="empty">No active matching CHP incidents are currently stored.</div>';
        detailsPanel.innerHTML = '<div class="empty">No active matching CHP incidents are currently stored.</div>';
        return;
      }}

      incidents.forEach((incident) => {{
        const hasCoords = incident.latitude != null && incident.longitude != null;
        const isActive = incident.status === "active";
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
          <span class="status-pill ${{isActive ? "status-active" : "status-cleared"}}">${{isActive ? "Active" : "Cleared"}}</span>
          <strong>${{escapeHtml(incident.type || "CHP Incident")}}</strong>
          <span>${{escapeHtml(incident.location)}}</span>
          <span>${{escapeHtml(formatIncidentWhen(incident))}} · ${{escapeHtml(incident.area)}} · #${{escapeHtml(incident.incident_no)}}${{hasCoords ? "" : " · no map pin"}}</span>
        `;
        button.addEventListener("click", () => selectIncident(incident));
        list.appendChild(button);
      }});

      setTimeout(() => map.invalidateSize(), 50);
      window.requestAnimationFrame(updateListScrollCue);
      const linkedIncident = incidentFromUrl();
      selectIncident(linkedIncident || incidents[0], {{
        pan: Boolean(linkedIncident),
        revealList: Boolean(linkedIncident),
        updateUrl: Boolean(linkedIncident)
      }});
    }}

    render();
    formatGeneratedAt();
    setupStaleRefresh();
    setupDoubleTapZoom();
    list.addEventListener("scroll", updateListScrollCue, {{ passive: true }});
    scrollIncidentsButton?.addEventListener("click", scrollIncidentListDown);
    window.addEventListener("resize", updateListScrollCue);
  </script>
</body>
</html>
"""


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
