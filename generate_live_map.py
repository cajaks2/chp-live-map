import argparse
import datetime as dt
import html
import json
import os
import sqlite3
from pathlib import Path

from ecs_logging import log_event, run_main


DEFAULT_CENTER = [34.32, -118.12]
DEFAULT_ZOOM = 10


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


def build_html(incidents, generated_at, hours, base_path="/", public_url=None):
    data = json.dumps(incidents, ensure_ascii=False)
    mapped_count = len(
        [i for i in incidents if i.get("latitude") is not None and i.get("longitude") is not None]
    )
    active_count = len([i for i in incidents if i.get("status") == "active"])
    title = f"CHP Forest Incidents ({active_count} active, {len(incidents)} total)"
    description = (
        "Live CHP traffic incidents for Angeles Crest, Angeles Forest, Big Tujunga, "
        "Glendora Mountain, and nearby forest roads."
    )
    urls = metadata_urls(base_path, public_url)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta name="description" content="{html.escape(description)}">
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
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQ9um5Lj053hphD7uW9P4U5F9VAt5x0=" crossorigin="">
  <style>
    .leaflet-container {{
      overflow: hidden;
      touch-action: pan-x pan-y;
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
    #incident-list {{
      flex: 1 1 auto;
      min-height: 0;
      overflow-y: auto;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
      scrollbar-width: thin;
      scrollbar-color: #8fa195 #eef1ea;
      background: #d8ddd2;
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
      margin-bottom: 1px;
      padding: 13px 16px;
      border: 0;
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
        max-height: 34vh;
        overflow: hidden;
        border-right: 0;
        border-bottom: 1px solid #d8ddd2;
      }}
      #incident-list {{
        min-height: 92px;
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
        <div class="meta">Last updated <time id="generated-at" datetime="{html.escape(generated_at)}">{html.escape(generated_at)}</time></div>
      </header>
      <div id="incident-list"></div>
    </aside>
    <main id="map"></main>
    <aside id="details"></aside>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const incidents = {data};

    const mapEl = document.getElementById("map");
    mapEl.classList.add("is-loading");
    const map = L.map("map", {{
      preferCanvas: true,
      zoomControl: false,
      zoomAnimation: false,
      fadeAnimation: false,
      markerZoomAnimation: false
    }}).setView({json.dumps(DEFAULT_CENTER)}, {DEFAULT_ZOOM});
    const baseLayer = L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      subdomains: "abc",
      maxZoom: 19,
      keepBuffer: 8,
      updateWhenIdle: true,
      updateWhenZooming: false,
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
    const list = document.getElementById("incident-list");
    const detailsPanel = document.getElementById("details");
    window.chpLiveMap = {{ map, markers, incidents }};

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
      const date = new Date(generatedAt.dateTime);
      if (Number.isNaN(date.getTime())) {{
        return;
      }}
      generatedAt.textContent = date.toLocaleString([], {{
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit"
      }});
      generatedAt.title = generatedAt.dateTime;
    }}

    function detailHtml(incident) {{
      if (!incident) {{
        return '<div class="empty">Select an incident to view CHP detail entries.</div>';
      }}
      const isActive = incident.status === "active";
      const statusClass = isActive ? "status-active" : "status-cleared";
      const statusText = isActive ? "Active" : "Cleared";
      const details = (incident.detail_entries || []).map((entry) => `
        <li>
          <time>${{escapeHtml(entry.time)}} · Entry ${{escapeHtml(entry.entry_no)}}</time>
          <div>${{escapeHtml(entry.text)}}</div>
        </li>
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
            <div class="meta">CHP Detail Information</div>
            ${{details ? `<ol class="detail-log">${{details}}</ol>` : '<div class="empty">No detail entries captured.</div>'}}
          </section>
        </div>
      `;
    }}

    function selectIncident(incident, options = {{}}) {{
      detailsPanel.innerHTML = detailHtml(incident);
      document.querySelectorAll(".incident").forEach((button) => {{
        button.setAttribute("aria-current", button.dataset.eventKey === incident.event_key ? "true" : "false");
      }});
      markers.forEach((marker, eventKey) => {{
        const selected = eventKey === incident.event_key;
        const markerIncident = incidents.find((item) => item.event_key === eventKey);
        const isActive = markerIncident?.status === "active";
        marker.setStyle({{
          radius: selected ? 10 : 8,
          color: selected ? (isActive ? "#611113" : "#3f4642") : (isActive ? "#8f1d21" : "#6e7771"),
          weight: selected ? 3 : 2,
          fillColor: selected ? (isActive ? "#f05a40" : "#9da5a0") : (isActive ? "#d94a38" : "#b8bfba"),
          fillOpacity: selected ? 0.95 : 0.72
        }});
        if (selected) {{
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
          const marker = L.circleMarker([incident.latitude, incident.longitude], {{
            radius: 8,
            color: isActive ? "#8f1d21" : "#6e7771",
            weight: 2,
            fillColor: isActive ? "#d94a38" : "#b8bfba",
            fillOpacity: isActive ? 0.85 : 0.72
          }}).addTo(map);
          marker.on("click", () => selectIncident(incident, {{ pan: false, revealDetails: true }}));
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
          <span>${{escapeHtml(incident.incident_time)}} · ${{escapeHtml(incident.area)}} · #${{escapeHtml(incident.incident_no)}}${{hasCoords ? "" : " · no map pin"}}</span>
        `;
        button.addEventListener("click", () => selectIncident(incident));
        list.appendChild(button);
      }});

      setTimeout(() => map.invalidateSize(), 50);
      selectIncident(incidents[0], {{ pan: false }});
    }}

    render();
    formatGeneratedAt();
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
