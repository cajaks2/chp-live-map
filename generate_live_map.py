import argparse
import datetime as dt
import html
import json
import sqlite3
from pathlib import Path


DEFAULT_CENTER = [34.32, -118.12]
DEFAULT_ZOOM = 10


def load_active_incidents(database):
    if not database.exists():
        return []
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
          AND e.latitude IS NOT NULL
          AND e.longitude IS NOT NULL
        ORDER BY e.incident_date DESC, e.incident_time DESC, e.incident_no DESC
        """
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


def build_html(incidents, generated_at):
    data = json.dumps(incidents, ensure_ascii=False)
    title = f"CHP Forest Incidents ({len(incidents)} active)"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQ9um5Lj053hphD7uW9P4U5F9VAt5x0=" crossorigin="">
  <style>
    html, body {{
      height: 100%;
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #182026;
      background: #f6f7f4;
    }}
    #app {{
      display: grid;
      grid-template-columns: minmax(280px, 360px) 1fr;
      height: 100%;
    }}
    #sidebar {{
      overflow: auto;
      border-right: 1px solid #d8ddd2;
      background: #fbfcf8;
    }}
    header {{
      position: sticky;
      top: 0;
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
      display: grid;
      gap: 1px;
      background: #d8ddd2;
    }}
    .incident {{
      display: block;
      width: 100%;
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
      height: 100%;
      min-height: 420px;
    }}
    .empty {{
      padding: 18px;
      color: #58645d;
      font-size: 14px;
    }}
    .popup {{
      min-width: 260px;
      max-width: 360px;
    }}
    .popup h2 {{
      margin: 0 0 6px;
      font-size: 16px;
      letter-spacing: 0;
    }}
    .popup .line {{
      margin: 3px 0;
      font-size: 13px;
    }}
    .popup ol {{
      margin: 10px 0 0;
      padding-left: 20px;
      max-height: 220px;
      overflow: auto;
      font-size: 12px;
      line-height: 1.35;
    }}
    .popup li {{
      margin-bottom: 6px;
    }}
    @media (max-width: 760px) {{
      #app {{
        grid-template-columns: 1fr;
        grid-template-rows: 42vh 1fr;
      }}
      #sidebar {{
        order: 2;
        border-right: 0;
        border-top: 1px solid #d8ddd2;
      }}
      #map {{
        order: 1;
        min-height: 0;
      }}
    }}
  </style>
</head>
<body>
  <div id="app">
    <aside id="sidebar">
      <header>
        <h1>CHP Forest Incidents</h1>
        <div class="meta">{len(incidents)} active incidents</div>
        <div class="meta">Generated {html.escape(generated_at)}</div>
      </header>
      <div id="incident-list"></div>
    </aside>
    <main id="map"></main>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const incidents = {data};

    const map = L.map("map", {{ preferCanvas: true }}).setView({json.dumps(DEFAULT_CENTER)}, {DEFAULT_ZOOM});
    L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }}).addTo(map);

    const markers = new Map();
    const list = document.getElementById("incident-list");

    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({{
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }}[char]));
    }}

    function popupHtml(incident) {{
      const details = (incident.detail_entries || []).map((entry) => `
        <li><strong>${{escapeHtml(entry.time)}} ${{escapeHtml(entry.entry_no)}}</strong><br>${{escapeHtml(entry.text)}}</li>
      `).join("");
      return `
        <div class="popup">
          <h2>${{escapeHtml(incident.type || "CHP Incident")}}</h2>
          <div class="line"><strong>${{escapeHtml(incident.location)}}</strong></div>
          <div class="line">${{escapeHtml(incident.location_desc || "")}}</div>
          <div class="line">Incident ${{escapeHtml(incident.incident_no)}} · ${{escapeHtml(incident.incident_time)}} · ${{escapeHtml(incident.area)}}</div>
          <div class="line">First seen ${{escapeHtml(incident.first_seen)}}</div>
          <div class="line">Last seen ${{escapeHtml(incident.last_seen)}}</div>
          ${{details ? `<ol>${{details}}</ol>` : ""}}
        </div>
      `;
    }}

    function render() {{
      if (!incidents.length) {{
        list.innerHTML = '<div class="empty">No active matching CHP incidents with coordinates are currently stored.</div>';
        return;
      }}

      const bounds = [];
      incidents.forEach((incident) => {{
        const marker = L.circleMarker([incident.latitude, incident.longitude], {{
          radius: 8,
          color: "#8f1d21",
          weight: 2,
          fillColor: "#d94a38",
          fillOpacity: 0.85
        }}).addTo(map);
        marker.bindPopup(popupHtml(incident), {{ maxWidth: 390 }});
        markers.set(incident.event_key, marker);
        bounds.push([incident.latitude, incident.longitude]);

        const button = document.createElement("button");
        button.className = "incident";
        button.type = "button";
        button.innerHTML = `
          <strong>${{escapeHtml(incident.type || "CHP Incident")}}</strong>
          <span>${{escapeHtml(incident.location)}}</span>
          <span>${{escapeHtml(incident.incident_time)}} · ${{escapeHtml(incident.area)}} · #${{escapeHtml(incident.incident_no)}}</span>
        `;
        button.addEventListener("click", () => {{
          map.setView([incident.latitude, incident.longitude], Math.max(map.getZoom(), 13));
          marker.openPopup();
        }});
        list.appendChild(button);
      }});

      if (bounds.length === 1) {{
        map.setView(bounds[0], 13);
      }} else {{
        map.fitBounds(bounds, {{ padding: [32, 32] }});
      }}
    }}

    render();
  </script>
</body>
</html>
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a static live CHP incident map from the SQLite database."
    )
    parser.add_argument("--database", type=Path, default=Path("chp_traffic.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("live_chp_map.html"))
    return parser.parse_args()


def main():
    args = parse_args()
    generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    incidents = load_active_incidents(args.database)
    args.output.write_text(build_html(incidents, generated_at), encoding="utf-8")
    print(f"Wrote {args.output} with {len(incidents)} active incidents")


if __name__ == "__main__":
    main()
