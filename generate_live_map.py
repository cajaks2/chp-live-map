import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlencode

from ecs_logging import log_event, run_main
from temperature_ui import TEMPERATURE_CSS, temperature_script
from road_weather_ui import ROAD_WEATHER_CSS, road_weather_script
from geo_bounds import REGION_BOUNDS, clear_coordinates_outside_region_bounds
from mile_markers import MILE_MARKERS


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

# Simplified local geometry for the principal Malibu corridors. These lines are
# deliberately small enough to ship in the map HTML and remain usable when the
# raster tile service is unreachable. Coordinates are derived from OpenStreetMap
# road geometry and retain the normal on-map OpenStreetMap attribution.
MALIBU_OFFLINE_ROADS = {
    "Pacific Coast Highway": (
        (
            (34.0125, -118.4975), (34.0261, -118.5155), (34.0387, -118.5417),
            (34.0386, -118.5561), (34.0421, -118.5704), (34.0388, -118.5847),
            (34.0394, -118.6035), (34.0382, -118.6227), (34.0386, -118.6452),
            (34.0394, -118.6708), (34.0342, -118.6874), (34.0338, -118.7151),
            (34.0332, -118.7422), (34.0261, -118.7634), (34.0246, -118.7849),
            (34.0205, -118.7962), (34.0166, -118.8191), (34.0224, -118.8305),
            (34.0325, -118.8457), (34.0367, -118.8618), (34.0405, -118.8871),
            (34.0470, -118.9249), (34.0451, -118.9389), (34.0534, -118.9639),
            (34.0613, -118.9836), (34.0667, -118.9998), (34.0670, -119.0085),
            (34.0794, -119.0277), (34.0854, -119.0478), (34.0939, -119.0689),
            (34.1072, -119.0793),
        ),
    ),
    "Topanga Canyon Boulevard": (
        (
            (34.0401, -118.5793), (34.0499, -118.5801), (34.0571, -118.5834),
            (34.0742, -118.5885), (34.0824, -118.5988), (34.0929, -118.6017),
            (34.1022, -118.5915), (34.1106, -118.5922), (34.1228, -118.5904),
            (34.1244, -118.5991), (34.1367, -118.5991), (34.1417, -118.6082),
            (34.1461, -118.6056), (34.1719, -118.6059), (34.1829, -118.6059),
            (34.2258, -118.6059), (34.2458, -118.6072), (34.2785, -118.6045),
        ),
    ),
    "Malibu Canyon / Las Virgenes": (
        (
            (34.0347, -118.7034), (34.0445, -118.6925), (34.0537, -118.6966),
            (34.0616, -118.6943), (34.0707, -118.7069),
        ),
        (
            (34.0802, -118.7037), (34.0931, -118.7092), (34.1054, -118.7118),
            (34.1173, -118.7085), (34.1277, -118.7039), (34.1413, -118.6999),
        ),
    ),
    "Kanan Road": (
        (
            (34.0277, -118.7995), (34.0449, -118.7977), (34.0589, -118.7988),
            (34.0747, -118.8149), (34.0873, -118.8168), (34.0980, -118.8108),
            (34.1073, -118.8048), (34.1151, -118.8012), (34.1199, -118.7923),
            (34.1209, -118.7783), (34.1304, -118.7632), (34.1422, -118.7620),
        ),
    ),
    "Yerba Buena Road": (
        (
            (34.0531, -118.9631), (34.0587, -118.9652), (34.0643, -118.9634),
            (34.0688, -118.9580), (34.0735, -118.9577), (34.0770, -118.9513),
            (34.0860, -118.9506), (34.0912, -118.9463), (34.0960, -118.9483),
            (34.0986, -118.9440), (34.1072, -118.9426), (34.1107, -118.9388),
            (34.1091, -118.9338), (34.1129, -118.9293), (34.1108, -118.9267),
            (34.1144, -118.9184), (34.1110, -118.9175), (34.1076, -118.9111),
            (34.1102, -118.9065), (34.1049, -118.8923),
        ),
    ),
    "Little Sycamore Canyon Road": (
        (
            (34.0888, -118.8805), (34.1038, -118.8887), (34.1049, -118.8923),
        ),
    ),
    "Mulholland Highway": (
        (
            (34.0451, -118.9349), (34.0563, -118.9374), (34.0578, -118.9344),
            (34.0730, -118.9281), (34.0844, -118.9179), (34.0866, -118.9062),
            (34.0907, -118.9038), (34.0864, -118.9023), (34.0874, -118.8972),
            (34.0892, -118.8909), (34.0945, -118.8873), (34.0873, -118.8858),
            (34.0886, -118.8734), (34.0931, -118.8640), (34.0999, -118.8614),
            (34.1016, -118.8631), (34.1044, -118.8584), (34.1043, -118.8455),
            (34.1000, -118.8433), (34.0969, -118.8325), (34.0931, -118.8314),
            (34.0972, -118.8051),
        ),
    ),
    "Decker Road": (
        (
            (34.0415, -118.8944), (34.0501, -118.8976), (34.0580, -118.8969),
            (34.0682, -118.8943), (34.0751, -118.8824), (34.0835, -118.8782),
            (34.0886, -118.8734),
        ),
    ),
    "Encinal Canyon Road": (
        (
            (34.0943, -118.8284), (34.0876, -118.8396), (34.0875, -118.8520),
            (34.0826, -118.8682), (34.0775, -118.8777), (34.0654, -118.8761),
            (34.0609, -118.8745), (34.0576, -118.8695), (34.0458, -118.8748),
            (34.0404, -118.8852),
        ),
    ),
    "Latigo Canyon Road": (
        (
            (34.0300, -118.7547), (34.0376, -118.7657), (34.0413, -118.7723),
            (34.0540, -118.7741), (34.0633, -118.7780), (34.0679, -118.7805),
            (34.0731, -118.7844), (34.0802, -118.7982), (34.0858, -118.8040),
            (34.0921, -118.8119), (34.0896, -118.8154),
        ),
    ),
    "Tuna Canyon Road": (
        (
            (34.0774, -118.6064), (34.0714, -118.6049), (34.0681, -118.6111),
            (34.0645, -118.6165), (34.0605, -118.6176), (34.0591, -118.6022),
            (34.0526, -118.5972), (34.0469, -118.5940), (34.0395, -118.5895),
        ),
    ),
}


def normalize_region(region):
    normalized = (region or "forest").casefold()
    return normalized if normalized in REGION_LABELS else "forest"


def region_label(region):
    return REGION_LABELS[normalize_region(region)]


def region_viewport(region):
    return REGION_VIEWPORTS[normalize_region(region)]


def load_incidents(database, hours, database_url=None, region="forest", conn=None):
    region = normalize_region(region)
    if conn is None and not database_url and not database.exists():
        return []
    cutoff = (dt.datetime.now().astimezone() - dt.timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )
    should_close = False
    if conn is not None:
        placeholder = "%s" if database_url else "?"
    elif database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Postgres support requires psycopg. Install requirements.txt.") from exc
        conn = psycopg.connect(database_url, row_factory=dict_row)
        should_close = True
        placeholder = "%s"
    else:
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        should_close = True
        placeholder = "?"
    rows = conn.execute(
        f"""
        SELECT
            e.*,
            (
                SELECT o.details_json
                FROM observations o
                WHERE o.event_key = e.event_key
                  AND TRIM(o.details_json) NOT IN ('', '[]')
                ORDER BY o.observed_at DESC, o.id DESC
                LIMIT 1
            ) AS details_json
        FROM events e
        WHERE e.region = {placeholder}
          AND (
              e.status = 'active'
              OR e.first_seen >= {placeholder}
              OR e.last_seen >= {placeholder}
              OR e.cleared_at >= {placeholder}
          )
        ORDER BY
            CASE WHEN e.status = 'active' THEN 0 ELSE 1 END,
            e.latest_observed_at DESC,
            e.incident_no DESC
        """,
        (region, cutoff, cutoff, cutoff),
    ).fetchall()
    if should_close:
        conn.close()
    incidents = []
    for row in rows:
        incidents.append(hydrate_incident(row, region))
    incidents.sort(key=lambda incident: str(incident.get("incident_no") or ""), reverse=True)
    incidents.sort(key=incident_recency, reverse=True)
    incidents.sort(key=lambda incident: 0 if incident.get("status") == "active" else 1)
    return incidents


def hydrate_incident(row, region):
    incident = clear_coordinates_outside_region_bounds(dict(row), region)
    incident["source"] = incident.get("source") or "chp"
    incident["source_event_id"] = incident.get("source_event_id") or incident.get("event_key")
    incident["coordinate_confidence"] = incident.get("coordinate_confidence") or (
        "exact" if incident.get("latitude") is not None and incident.get("longitude") is not None else "missing"
    )
    try:
        incident["detail_entries"] = json.loads(incident.pop("details_json") or "[]")
    except json.JSONDecodeError:
        incident["detail_entries"] = []
    return incident


def load_last_scrape_run(database, database_url=None, conn=None):
    if conn is None and not database_url and not database.exists():
        return None
    should_close = False
    if conn is not None:
        placeholder = "%s" if database_url else "?"
    elif database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Postgres support requires psycopg. Install requirements.txt.") from exc
        conn = psycopg.connect(database_url, row_factory=dict_row)
        should_close = True
        placeholder = "%s"
    else:
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        should_close = True
        placeholder = "?"
    try:
        row = conn.execute(
            """
            SELECT observed_at, source, total_seen, active_seen, active_with_coords, duration_seconds
            FROM scrape_runs
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    except Exception:
        row = None
    if should_close:
        conn.close()
    return dict(row) if row else None


def load_incident_by_key(database, event_key, database_url=None, region="forest", conn=None):
    region = normalize_region(region)
    if not event_key or (conn is None and not database_url and not database.exists()):
        return None
    should_close = False
    if conn is not None:
        placeholder = "%s" if database_url else "?"
    elif database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Postgres support requires psycopg. Install requirements.txt.") from exc
        conn = psycopg.connect(database_url, row_factory=dict_row)
        should_close = True
        placeholder = "%s"
    else:
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        should_close = True
        placeholder = "?"
    row = conn.execute(
        f"""
        SELECT
            e.*,
            (
                SELECT o.details_json
                FROM observations o
                WHERE o.event_key = e.event_key
                  AND TRIM(o.details_json) NOT IN ('', '[]')
                ORDER BY o.observed_at DESC, o.id DESC
                LIMIT 1
            ) AS details_json
        FROM events e
        WHERE e.region = {placeholder}
          AND e.event_key = {placeholder}
        LIMIT 1
        """,
        (region, event_key),
    ).fetchone()
    if should_close:
        conn.close()
    return hydrate_incident(row, region) if row else None


def load_removed_detail_entries(database, event_key, database_url=None, region="forest", conn=None):
    region = normalize_region(region)
    if not event_key or (conn is None and not database_url and not database.exists()):
        return None
    should_close = False
    if conn is not None:
        placeholder = "%s" if database_url else "?"
    elif database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Postgres support requires psycopg. Install requirements.txt.") from exc
        conn = psycopg.connect(database_url, row_factory=dict_row)
        should_close = True
        placeholder = "%s"
    else:
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        should_close = True
        placeholder = "?"
    exists = conn.execute(
        f"SELECT 1 FROM events WHERE event_key = {placeholder} AND region = {placeholder}",
        (event_key, region),
    ).fetchone()
    rows = []
    if exists:
        rows = conn.execute(
            f"""
            SELECT observed_at, details_json
            FROM observations
            WHERE event_key = {placeholder}
              AND status = 'active'
            ORDER BY observed_at DESC, id DESC
            """,
            (event_key,),
        ).fetchall()
    if should_close:
        conn.close()
    if not exists:
        return None

    snapshots = []
    for row in rows:
        try:
            entries = json.loads(row["details_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            entries = []
        snapshots.append((row["observed_at"], entries))
    current_entries = snapshots[0][1] if snapshots else []

    def entry_key(entry):
        return (
            str(entry.get("section") or ""),
            str(entry.get("time") or ""),
            str(entry.get("entry_no") or ""),
            str(entry.get("text") or ""),
        )

    current_keys = {entry_key(entry) for entry in current_entries}
    history = {}
    for observed_at, entries in reversed(snapshots):
        snapshot_keys = set()
        for entry in entries:
            key = entry_key(entry)
            if key in snapshot_keys:
                continue
            snapshot_keys.add(key)
            item = history.setdefault(
                key,
                {
                    "section": entry.get("section") or "",
                    "time": entry.get("time") or "",
                    "entry_no": entry.get("entry_no") or "",
                    "text": entry.get("text") or "",
                    "first_seen": observed_at,
                    "last_seen": observed_at,
                    "snapshot_count": 0,
                },
            )
            item["last_seen"] = observed_at
            item["snapshot_count"] += 1
    removed = [item for key, item in history.items() if key not in current_keys]
    return sorted(removed, key=lambda item: (item["last_seen"], item["entry_no"]), reverse=True)


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


def history_controls(hours, region="forest", extra_params=None):
    current = int(hours)
    extra_params = extra_params or {}
    links = []
    for preset_hours, label in HISTORY_PRESETS:
        selected = preset_hours == current
        links.append(
            '<a class="range-tab{}" href="{}"{}>{}</a>'.format(
                " is-active" if selected else "",
                html.escape(
                    href_with_query(
                        "",
                        hours=f"{preset_hours:g}",
                        region=normalize_region(region),
                        **extra_params,
                    )
                ),
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


def pwa_head_html(base_path):
    return (
        f'  <link rel="manifest" href="{html.escape(app_path(base_path, "/manifest.webmanifest"))}">\n'
        f'  <link rel="apple-touch-icon" href="{html.escape(app_path(base_path, "/apple-touch-icon-180x180.png"))}">\n'
        '  <meta name="theme-color" content="#18392b">\n'
        '  <meta name="apple-mobile-web-app-capable" content="yes">\n'
        '  <meta name="apple-mobile-web-app-title" content="Crestmap">'
    )


def push_ui_css():
    return """
    .view-header-actions {
      display: flex;
      flex: 0 0 auto;
      align-items: center;
      gap: 7px;
    }
    .header-alert-button {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 34px;
      padding: 0 10px;
      border: 1px solid #d8ddd2;
      border-radius: 7px;
      color: #31523e;
      background: #fff;
      font: inherit;
      font-size: 12px;
      font-weight: 850;
      cursor: pointer;
    }
    .header-alert-button[hidden] { display: none; }
    .header-alert-status {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: #d39a32;
      box-shadow: 0 0 0 2px #f7ead0;
    }
    .header-alert-button.is-enabled {
      color: #1f6840;
      border-color: #b8d1bd;
      background: #f2f8f2;
    }
    .header-alert-button.is-enabled .header-alert-status {
      background: #2d7d4d;
      box-shadow: 0 0 0 2px #dbeade;
    }
    .header-alert-button.needs-install {
      color: #8f1d21;
      border-color: #e2b4ae;
      background: #fff7f5;
    }
    .header-alert-button.needs-install .header-alert-status {
      background: #b3262d;
      box-shadow: 0 0 0 2px #f3d8d4;
    }
    .push-overlay {
      display: none;
      position: fixed;
      inset: 0;
      z-index: 5000;
      align-items: center;
      justify-content: center;
      overflow-y: auto;
      padding: max(16px, env(safe-area-inset-top)) 14px max(16px, env(safe-area-inset-bottom));
      background: rgba(14, 24, 19, 0.62);
    }
    .push-overlay.is-visible { display: flex; }
    .push-card {
      width: min(100%, 480px);
      max-height: calc(100vh - 32px);
      overflow-y: auto;
      padding: 20px;
      border-radius: 14px;
      color: #182026;
      background: #fbfcf8;
      box-shadow: 0 18px 55px rgba(0, 0, 0, 0.34);
    }
    .push-card h2 { margin: 0 32px 8px 0; font-size: 23px; line-height: 1.15; }
    .push-card p { margin: 8px 0; color: #46534b; font-size: 14px; line-height: 1.45; }
    .push-card ol { margin: 12px 0; padding-left: 24px; color: #33443a; font-size: 14px; line-height: 1.55; }
    .push-card-close {
      float: right;
      width: 34px;
      height: 34px;
      border: 0;
      border-radius: 999px;
      color: #35443c;
      background: #e9eee7;
      font-size: 20px;
      cursor: pointer;
    }
    .push-choice-group { margin: 15px 0; padding: 0; border: 0; }
    .push-choice-group legend { margin-bottom: 7px; font-size: 14px; font-weight: 850; }
    .push-choice-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }
    .push-choice {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 39px;
      padding: 0 10px;
      border: 1px solid #d8ddd2;
      border-radius: 8px;
      background: #fff;
      font-size: 13px;
      font-weight: 750;
    }
    .push-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
    .push-actions button {
      min-height: 40px;
      padding: 0 13px;
      border: 1px solid #277447;
      border-radius: 8px;
      color: #fff;
      background: #277447;
      font: inherit;
      font-size: 13px;
      font-weight: 850;
      cursor: pointer;
    }
    .push-actions button.is-secondary { color: #31523e; border-color: #cbd6cc; background: #fff; }
    .push-actions button.is-danger { color: #8f1d21; border-color: #e5bbb5; background: #fff8f6; }
    .push-actions button:disabled { opacity: 0.55; cursor: wait; }
    .push-status { min-height: 20px; margin-top: 12px; color: #365443; font-size: 13px; font-weight: 750; }
    .push-help-link { color: #1f6840; font-weight: 800; }
    @media (max-width: 520px) {
      .push-overlay { align-items: flex-end; padding: 0; }
      .push-card { width: 100%; max-height: 88vh; padding: 19px 17px max(19px, env(safe-area-inset-bottom)); border-radius: 16px 16px 0 0; }
      .push-choice-grid { grid-template-columns: 1fr; }
    }
    """


def push_ui_html(base_path):
    about_href = app_path(base_path, "/about") + "#push-notifications"
    return f"""
  <div class="push-overlay" id="ios-push-tutorial" role="dialog" aria-modal="true" aria-labelledby="ios-push-title">
    <section class="push-card">
      <button type="button" class="push-card-close" data-dismiss-ios-tutorial aria-label="Close">&times;</button>
      <h2 id="ios-push-title">Get incident alerts on iPhone</h2>
      <p>Install Crestmap as a Home Screen web app first. On current iOS versions, the option can be buried in Safari's menus:</p>
      <ol>
        <li>Open Crestmap in <strong>Safari</strong>, not an in-app browser.</li>
        <li>With Compact tabs, tap <strong>More (&hellip;)</strong>, then <strong>Share</strong>. With Bottom or Top tabs, tap Safari's <strong>Share</strong> button directly.</li>
        <li>Scroll down the Share sheet and tap <strong>Add to Home Screen</strong>.</li>
        <li>If it is missing, scroll to the very bottom, tap <strong>Edit Actions</strong>, then add <strong>Add to Home Screen</strong>.</li>
        <li>Turn on <strong>Open as Web App</strong>, then tap <strong>Add</strong>.</li>
        <li>Open Crestmap from its new Home Screen icon. Open <strong>Alerts</strong> from the menu and choose what you want.</li>
      </ol>
      <p><a class="push-help-link" href="{html.escape(about_href)}">How notifications work</a></p>
      <div class="push-actions">
        <button type="button" data-dismiss-ios-tutorial>Got it</button>
        <button type="button" class="is-secondary" data-dismiss-ios-tutorial>Remind me in 7 days</button>
      </div>
    </section>
  </div>
  <div class="push-overlay" id="ios-push-onboarding" role="dialog" aria-modal="true" aria-labelledby="ios-push-onboarding-title">
    <section class="push-card">
      <button type="button" class="push-card-close" data-dismiss-push-onboarding aria-label="Close">&times;</button>
      <h2 id="ios-push-onboarding-title">Turn on incident alerts?</h2>
      <p>Crestmap can notify this iPhone when it discovers a new incident matching the areas and categories you choose.</p>
      <p>You stay in control: choose all Forest roads, Crest + west forest only, or Malibu roads; select incident types; and turn alerts off here at any time.</p>
      <div class="push-actions">
        <button type="button" id="push-onboarding-setup">Choose alerts</button>
        <button type="button" class="is-secondary" data-dismiss-push-onboarding>Not now</button>
      </div>
      <p><a class="push-help-link" href="{html.escape(about_href)}">How notifications work</a></p>
    </section>
  </div>
  <div class="push-overlay" id="push-settings" role="dialog" aria-modal="true" aria-labelledby="push-settings-title">
    <section class="push-card">
      <button type="button" class="push-card-close" data-close-push-settings aria-label="Close">&times;</button>
      <h2 id="push-settings-title">Incident alerts</h2>
      <p id="push-settings-intro">Choose which newly discovered incidents should notify this device.</p>
      <form id="push-preferences-form">
        <fieldset class="push-choice-group">
          <legend>Sources</legend>
          <div class="push-choice-grid">
            <label class="push-choice"><input type="checkbox" name="push_source" value="chp"> CHP incidents</label>
            <label class="push-choice"><input type="checkbox" name="push_source" value="wildweb"> WildWeb reports <span class="comment-field-hint">(may overlap CHP)</span></label>
          </div>
        </fieldset>
        <fieldset class="push-choice-group">
          <legend>Areas</legend>
          <div class="push-choice-grid">
            <label class="push-choice"><input type="checkbox" name="push_region" value="forest"> All Angeles Forest roads</label>
            <label class="push-choice"><input type="checkbox" name="push_region" value="crest"> Crest + west forest only</label>
            <label class="push-choice"><input type="checkbox" name="push_region" value="malibu"> Malibu roads</label>
          </div>
        </fieldset>
        <fieldset class="push-choice-group">
          <legend>Incident categories</legend>
          <div class="push-choice-grid">
            <label class="push-choice"><input type="checkbox" name="push_category" value="collision"> Collisions</label>
            <label class="push-choice"><input type="checkbox" name="push_category" value="hazard"> Traffic hazards</label>
            <label class="push-choice"><input type="checkbox" name="push_category" value="closure"> Closures + weather</label>
            <label class="push-choice"><input type="checkbox" name="push_category" value="other"> Other incidents</label>
          </div>
        </fieldset>
        <div class="push-actions">
          <button type="submit" id="push-save">Enable alerts</button>
          <button type="button" class="is-secondary" id="push-test" hidden>Send test notification</button>
          <button type="button" class="is-danger" id="push-disable" hidden>Turn off alerts</button>
          <button type="button" class="is-secondary" data-close-push-settings>Cancel</button>
        </div>
        <div class="push-status" id="push-status" role="status"></div>
      </form>
      <p><a class="push-help-link" href="{html.escape(about_href)}">Learn about delivery and privacy</a></p>
    </section>
  </div>
"""


def push_ui_script(base_path):
    config_endpoint = app_path(base_path, "/api/v1/push/config")
    subscription_endpoint = app_path(base_path, "/api/v1/push/subscription")
    service_worker = app_path(base_path, "/sw.js")
    return f"""
  <script>
  (() => {{
    const configEndpoint = {json.dumps(config_endpoint)};
    const subscriptionEndpoint = {json.dumps(subscription_endpoint)};
    const serviceWorkerUrl = {json.dumps(service_worker)};
    const tutorial = document.getElementById("ios-push-tutorial");
    const onboarding = document.getElementById("ios-push-onboarding");
    const onboardingSetup = document.getElementById("push-onboarding-setup");
    const settings = document.getElementById("push-settings");
    const form = document.getElementById("push-preferences-form");
    const status = document.getElementById("push-status");
    const saveButton = document.getElementById("push-save");
    const testButton = document.getElementById("push-test");
    const disableButton = document.getElementById("push-disable");
    const launchers = document.querySelectorAll("[data-open-push-settings]");
    const headerLaunchers = document.querySelectorAll("[data-header-push-launcher]");
    const standalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
    const ua = navigator.userAgent;
    const iosDevice = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
    const safari = /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(ua);
    const serviceWorkerSupported = "serviceWorker" in navigator;
    const supported = serviceWorkerSupported && "PushManager" in window && "Notification" in window;
    let pushConfig = null;
    let registration = null;
    let currentSubscription = null;
    let serverSubscribed = false;
    const registrationPromise = serviceWorkerSupported
      ? navigator.serviceWorker.register(serviceWorkerUrl, {{ scope: "/" }})
          .then(() => navigator.serviceWorker.ready)
          .catch(() => null)
      : Promise.resolve(null);

    function setVisible(element, visible) {{
      element?.classList.toggle("is-visible", visible);
      document.documentElement.style.overflow = visible ? "hidden" : "";
    }}
    function selected(name) {{ return [...form.querySelectorAll(`input[name="${{name}}"]:checked`)].map(input => input.value); }}
    function selectValues(name, values) {{
      form.querySelectorAll(`input[name="${{name}}"]`).forEach(input => {{ input.checked = values.includes(input.value); }});
    }}
    form?.querySelectorAll('input[name="push_region"]').forEach(input => input.addEventListener("change", () => {{
      if (!input.checked) return;
      if (input.value === "forest") form.querySelector('input[name="push_region"][value="crest"]').checked = false;
      if (input.value === "crest") form.querySelector('input[name="push_region"][value="forest"]').checked = false;
    }}));
    function setBusy(busy) {{ saveButton.disabled = busy; testButton.disabled = busy; disableButton.disabled = busy; }}
    function renderHeaderAlertState() {{
      headerLaunchers.forEach(button => {{
        const needsInstall = iosDevice && !standalone;
        button.classList.toggle("needs-install", needsInstall);
        button.classList.toggle("is-enabled", !needsInstall && serverSubscribed);
        button.setAttribute("aria-label", needsInstall ? "Install the Home Screen app to enable alerts" : (serverSubscribed ? "Alerts enabled; manage alert choices" : "Set up incident alerts"));
        button.title = needsInstall ? "Install the Home Screen app for alerts" : (serverSubscribed ? "Alerts enabled" : "Alerts are not enabled");
      }});
    }}
    function applicationServerKey(value) {{
      const padding = "=".repeat((4 - value.length % 4) % 4);
      const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
      return Uint8Array.from(atob(base64), char => char.charCodeAt(0));
    }}
    async function postSubscription(action, subscription, preferences = {{}}) {{
      const response = await fetch(subscriptionEndpoint, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json", "Accept": "application/json" }},
        body: JSON.stringify({{ action, subscription: subscription?.toJSON ? subscription.toJSON() : subscription, ...preferences }})
      }});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error?.message || `Request failed (${{response.status}})`);
      return payload;
    }}
    async function clearNotificationBadge() {{
      if (!standalone || !registration) return;
      try {{
        if ("clearAppBadge" in navigator) await navigator.clearAppBadge();
        (registration.active || navigator.serviceWorker.controller)?.postMessage({{ type: "CLEAR_BADGE" }});
      }} catch (_error) {{}}
    }}
    async function refreshState() {{
      registration = registration || await registrationPromise;
      if (!registration) throw new Error("Crestmap offline support is unavailable in this browser.");
      await clearNotificationBadge();
      currentSubscription = await registration.pushManager.getSubscription();
      let preferences = pushConfig.defaults;
      if (currentSubscription) {{
        const state = await postSubscription("status", currentSubscription);
        serverSubscribed = state.subscribed;
        if (state.preferences) preferences = state.preferences;
      }} else {{
        serverSubscribed = false;
      }}
      selectValues("push_region", preferences.regions);
      selectValues("push_category", preferences.categories);
      selectValues("push_source", preferences.sources);
      saveButton.textContent = serverSubscribed ? "Save choices" : "Enable alerts";
      testButton.hidden = !serverSubscribed;
      disableButton.hidden = !serverSubscribed;
      status.textContent = serverSubscribed ? "Alerts are enabled on this device." : "Alerts are not enabled on this device.";
      renderHeaderAlertState();
    }}
    async function openSettings() {{
      if (iosDevice && !standalone) {{
        setVisible(settings, false);
        setVisible(tutorial, true);
        return;
      }}
      setVisible(settings, true);
      if (!supported || !pushConfig?.enabled) {{
        status.textContent = "Push notifications are not available in this browser yet.";
        saveButton.disabled = true;
        return;
      }}
      try {{ await refreshState(); }} catch (error) {{ status.textContent = error.message; }}
    }}
    launchers.forEach(button => button.addEventListener("click", () => {{
      button.closest("details")?.removeAttribute("open");
      openSettings();
    }}));
    document.querySelectorAll("[data-close-push-settings]").forEach(button => button.addEventListener("click", () => setVisible(settings, false)));
    settings?.addEventListener("click", event => {{ if (event.target === settings) setVisible(settings, false); }});
    document.querySelectorAll("[data-dismiss-ios-tutorial]").forEach(button => button.addEventListener("click", () => {{
      localStorage.setItem("crestmapIosPushTutorialUntil", String(Date.now() + 7 * 24 * 60 * 60 * 1000));
      setVisible(tutorial, false);
    }}));
    tutorial?.addEventListener("click", event => {{ if (event.target === tutorial) setVisible(tutorial, false); }});
    function deferOnboarding() {{
      localStorage.setItem("crestmapPushOnboardingUntil", String(Date.now() + 7 * 24 * 60 * 60 * 1000));
      setVisible(onboarding, false);
    }}
    document.querySelectorAll("[data-dismiss-push-onboarding]").forEach(button => button.addEventListener("click", deferOnboarding));
    onboarding?.addEventListener("click", event => {{ if (event.target === onboarding) deferOnboarding(); }});
    onboardingSetup?.addEventListener("click", () => {{
      deferOnboarding();
      openSettings();
    }});
    form?.addEventListener("submit", async event => {{
      event.preventDefault();
      const regions = selected("push_region");
      const categories = selected("push_category");
      const sources = selected("push_source");
      if (!sources.length || !regions.length || !categories.length) {{ status.textContent = "Choose at least one source, area, and incident category."; return; }}
      setBusy(true);
      try {{
        const permission = await Notification.requestPermission();
        if (permission !== "granted") throw new Error("Notification permission was not granted. You can change it in Settings → Notifications.");
        if (!registration) registration = await navigator.serviceWorker.ready;
        currentSubscription = await registration.pushManager.getSubscription();
        if (!currentSubscription) {{
          currentSubscription = await registration.pushManager.subscribe({{
            userVisibleOnly: true,
            applicationServerKey: applicationServerKey(pushConfig.public_key)
          }});
        }}
        const wasSubscribed = serverSubscribed;
        await postSubscription("subscribe", currentSubscription, {{ sources, regions, categories }});
        if (!wasSubscribed) window.crestmapTrack?.("alert_subscribe");
        serverSubscribed = true;
        renderHeaderAlertState();
        status.textContent = "Alerts enabled. Your choices were saved.";
        saveButton.textContent = "Save choices";
        testButton.hidden = false;
        disableButton.hidden = false;
      }} catch (error) {{ status.textContent = error.message; }} finally {{ setBusy(false); }}
    }});
    testButton?.addEventListener("click", async () => {{
      if (!currentSubscription) {{ status.textContent = "Enable alerts before sending a test."; return; }}
      setBusy(true);
      try {{
        await postSubscription("test", currentSubscription);
        status.textContent = "Test queued. It should arrive within about one minute.";
      }} catch (error) {{ status.textContent = error.message; }} finally {{ setBusy(false); }}
    }});
    disableButton?.addEventListener("click", async () => {{
      setBusy(true);
      try {{
        if (currentSubscription) {{
          await postSubscription("unsubscribe", currentSubscription);
          await currentSubscription.unsubscribe();
        }}
        currentSubscription = null;
        serverSubscribed = false;
        renderHeaderAlertState();
        status.textContent = "Alerts are turned off on this device.";
        saveButton.textContent = "Enable alerts";
        testButton.hidden = true;
        disableButton.hidden = true;
      }} catch (error) {{ status.textContent = error.message; }} finally {{ setBusy(false); }}
    }});

    fetch(configEndpoint, {{ headers: {{ "Accept": "application/json" }}, cache: "no-store" }})
      .then(response => response.json())
      .then(config => {{
        pushConfig = config;
        if (!config.enabled) return;
        launchers.forEach(button => button.hidden = false);
        renderHeaderAlertState();
        const dismissedUntil = Number(localStorage.getItem("crestmapIosPushTutorialUntil") || 0);
        if (iosDevice && safari && !standalone && Date.now() >= dismissedUntil) {{
          window.setTimeout(() => setVisible(tutorial, true), 900);
        }}
        if (supported && !(iosDevice && !standalone)) {{
          const onboardingUntil = Number(localStorage.getItem("crestmapPushOnboardingUntil") || 0);
          refreshState()
            .then(() => {{
              if (iosDevice && standalone && !serverSubscribed && Date.now() >= onboardingUntil) window.setTimeout(() => setVisible(onboarding, true), 650);
            }})
            .catch(() => {{}});
        }}
      }})
      .catch(() => {{}});
  }})();
  </script>
"""


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


def admin_activity_script(base_path):
    endpoint = json.dumps(app_path(base_path, "/admin/session/activity"))
    return f"""<script>
    (() => {{
      let lastRenewal = 0;
      let renewing = false;
      let expired = false;
      const renewFromInteraction = (event) => {{
        if (!event.isTrusted || document.visibilityState !== "visible" || expired || renewing) return;
        const now = Date.now();
        if (now - lastRenewal < 5 * 60 * 1000) return;
        renewing = true;
        lastRenewal = now;
        fetch({endpoint}, {{
          method: "POST", credentials: "same-origin", cache: "no-store", keepalive: true,
          headers: {{ "X-Crestmap-Activity": "1" }}
        }}).then((response) => {{
          if (response.status === 401) expired = true;
        }}).catch(() => {{}}).finally(() => {{ renewing = false; }});
      }};
      for (const name of ["pointerdown", "keydown", "touchstart", "wheel"]) {{
        document.addEventListener(name, renewFromInteraction, {{ passive: true }});
      }}
    }})();
    </script>"""


def view_menu_script():
    """Anchor the fixed menu outside clipped panels, within Safari's visible viewport."""
    return """<script>
    (() => {
      const menu = document.querySelector(".view-menu");
      const trigger = menu.querySelector("summary");
      const panel = menu.querySelector(".view-menu-popover");
      const positionMenu = () => {
        if (!menu.open) return;
        const viewport = window.visualViewport;
        const left = viewport?.offsetLeft || 0;
        const top = viewport?.offsetTop || 0;
        const width = viewport?.width || window.innerWidth;
        const height = viewport?.height || window.innerHeight;
        const anchor = trigger.getBoundingClientRect();
        const panelTop = Math.max(top + 12, Math.min(anchor.bottom + 4, top + height - 60));
        panel.style.maxWidth = `${Math.max(0, width - 24)}px`;
        const panelLeft = Math.max(left + 12, Math.min(
          anchor.right - panel.offsetWidth, left + width - panel.offsetWidth - 12
        ));
        panel.style.top = `${panelTop}px`;
        panel.style.left = `${panelLeft}px`;
        panel.style.right = "auto";
        panel.style.maxHeight = `${Math.max(0, top + height - panelTop - 12)}px`;
      };
      menu.addEventListener("toggle", positionMenu);
      window.addEventListener("resize", positionMenu, { passive: true });
      window.addEventListener("scroll", positionMenu, { passive: true });
      window.visualViewport?.addEventListener("resize", positionMenu, { passive: true });
      window.visualViewport?.addEventListener("scroll", positionMenu, { passive: true });
    })();
    </script>"""


def view_menu(base_path, current, hours, region="forest", admin_mode=False, aircraft_tracking_enabled=False):
    current_suffix = {
        "map": "/",
        "summary": "/summary",
        "history": "/history",
        "about": "/about",
    }.get(current, "/")
    current_href = view_href(base_path, current_suffix, hours, region)
    admin_href = app_path(base_path, "/admin/comments") if admin_mode else href_with_query(
        app_path(base_path, "/admin/login"),
        next=current_href,
    )
    items = [
        ("map", "Map", "Current incidents", view_href(base_path, "/", hours, region)),
        ("summary", "Summary", "Counts + trends", view_href(base_path, "/summary", hours, region)),
        ("history", "History", "Search incidents", view_href(base_path, "/history", hours, region)),
        ("about", "About", "Source + cadence", view_href(base_path, "/about", hours, region)),
        ("corners", "Corners", "Crash counts by corner", "https://crestmap.us/corners/"),
        ("alerts", "Alerts", "Push notification choices", None),
        (
            "admin",
            "Admin tools" if admin_mode else "Admin login",
            "Moderation + hidden details",
            admin_href,
        ),
    ]
    rows = []
    for key, label, description, href in items:
        if key == "alerts":
            rows.append(
                '<button type="button" class="view-menu-row" data-open-push-settings>'
                '<span class="view-menu-label">{}</span>'
                '<span class="view-menu-description">{}</span></button>'.format(
                    html.escape(label), html.escape(description)
                )
            )
            continue
        if key == "aircraft":
            rows.append(
                '<button type="button" class="view-menu-row is-active" data-aircraft-layer-toggle '
                'aria-pressed="true"><span class="view-menu-label">{}</span>'
                '<span class="view-menu-description">{}</span></button>'.format(
                    html.escape(label), html.escape(description)
                )
            )
            continue
        if key == "temperature":
            rows.append(
                '<button type="button" class="view-menu-row is-active" data-temperature-layer-toggle '
                'aria-pressed="true"><span class="view-menu-label">{}</span>'
                '<span class="view-menu-description">{}</span></button>'.format(
                    html.escape(label), html.escape(description)
                )
            )
            continue
        if key == "cameras":
            rows.append(
                '<button type="button" class="view-menu-row is-active" data-camera-layer-toggle '
                'aria-pressed="true"><span class="view-menu-label">{}</span>'
                '<span class="view-menu-description">{}</span></button>'.format(
                    html.escape(label), html.escape(description)
                )
            )
            continue
        if key == "mile-markers":
            rows.append(
                '<button type="button" class="view-menu-row is-active" data-mile-markers-toggle '
                'aria-pressed="true"><span class="view-menu-label">{}</span>'
                '<span class="view-menu-description">{}</span></button>'.format(
                    html.escape(label), html.escape(description)
                )
            )
            continue
        rows.append(
            '<a class="view-menu-row{}" href="{}">'
            '<span class="view-menu-label">{}</span>'
            '<span class="view-menu-description">{}</span></a>'.format(
                " is-active" if key == current else "",
                html.escape(href),
                html.escape(label),
                html.escape(description),
            )
        )
    return (
        '<div class="view-header-actions">'
        '<button type="button" class="header-alert-button" data-open-push-settings '
        'data-header-push-launcher hidden>'
        '<span>Alerts</span><span class="header-alert-status" aria-hidden="true"></span></button>'
        '<details class="view-menu">'
        '<summary aria-label="Open navigation menu">...</summary>'
        '<div class="view-menu-popover">'
        + "".join(rows)
        + "</div></details></div>"
        + view_menu_script()
        + (admin_activity_script(base_path) if admin_mode else "")
    )


def map_layer_menu(region="forest", aircraft_tracking_enabled=False):
    rows = [
        '<button type="button" class="view-menu-row is-active" data-incident-layer-toggle aria-label="Toggle incidents" '
        'aria-pressed="true"><span class="view-menu-label">Incidents</span>'
        '<span class="view-menu-description">Map pins</span><span class="map-layer-switch" aria-hidden="true"></span></button>',
        '<button type="button" class="view-menu-row is-active" data-road-weather-layer-toggle aria-label="Toggle road weather" '
        'aria-pressed="true"><span class="view-menu-label">Road weather</span>'
        '<span class="view-menu-description">Rain · snow · ice by elevation</span><span class="map-layer-switch" aria-hidden="true"></span></button>',
        '<button type="button" class="view-menu-row is-active" data-temperature-layer-toggle aria-label="Toggle air temperature" '
        'aria-pressed="true"><span class="view-menu-label">Air temperature</span>'
        '<span class="view-menu-description">Measured + estimated °F</span><span class="map-layer-switch" aria-hidden="true"></span></button>',
        '<button type="button" class="view-menu-row is-active" data-camera-layer-toggle aria-label="Toggle fire cameras" '
        'aria-pressed="true"><span class="view-menu-label">Fire cameras</span>'
        '<span class="view-menu-description">ALERTCalifornia cameras</span><span class="map-layer-switch" aria-hidden="true"></span></button>',
    ]
    if normalize_region(region) == "forest":
        rows.append(
            '<button type="button" class="view-menu-row is-active" data-mile-markers-toggle aria-label="Toggle mile markers" '
            'aria-pressed="true"><span class="view-menu-label">Mile markers</span>'
            '<span class="view-menu-description">More detail as you zoom</span><span class="map-layer-switch" aria-hidden="true"></span></button>'
        )
    if aircraft_tracking_enabled:
        rows.append(
            '<button type="button" class="view-menu-row is-active" data-aircraft-layer-toggle aria-label="Toggle rescue helicopters" '
            'aria-pressed="true"><span class="view-menu-label">Rescue helicopters</span>'
            '<span class="view-menu-description">Shown when airborne</span><span class="map-layer-switch" aria-hidden="true"></span></button>'
        )
    return (
        '<details class="map-layer-menu">'
        '<summary aria-label="Open map layers" title="Map layers">'
        '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        '<path d="M4 7h16M4 12h16M4 17h16"></path></svg></summary>'
        '<div class="map-layer-popover"><div class="map-layer-heading">'
        '<strong>Map layers</strong><span>Controls that only affect the map</span></div>'
        + "".join(rows)
        + '</div></details>'
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
        region_status = region_statuses.get(key) or {}
        active_count = int(region_status.get("active_count", 0))
        count_label = "active incident" if active_count == 1 else "active incidents"
        tabs.append(
            '<a class="region-tab{}" href="{}"{}><span>{}</span><span class="region-active-count" aria-label="{}">{}</span></a>'.format(
            " is-active" if key == region else "",
            html.escape(view_href(base_path, "/", hours, key) if current == "map" else view_href(base_path, f"/{current}", hours, key)),
            ' aria-current="page"' if key == region else "",
            html.escape(label),
            html.escape(f"{active_count} {count_label}"),
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
    reported_count = len([i for i in window_incidents if i.get("status") == "reported"])
    archived_count = len([i for i in window_incidents if i.get("status") == "archived"])
    cleared_count = len([i for i in window_incidents if i.get("status") == "cleared"])
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
            "source": i.get("source"),
            "source_status": i.get("source_status"),
            "source_reported_at": i.get("source_reported_at"),
        }
        for i in window_incidents
    ]
    version = hashlib.sha256(
        json.dumps(version_source, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "active_count": active_count,
        "reported_count": reported_count,
        "current_count": active_count + reported_count,
        "archived_count": archived_count,
        "cleared_count": cleared_count,
        "total_count": len(window_incidents),
        "mapped_count": mapped_count,
        "hours": hours,
        "data_updated_at": data_updated_at,
        "version": version,
    }


def analytics_script(google_analytics_id=None, region="forest", page="map", admin_mode=False):
    if not google_analytics_id:
        return ""
    escaped_id = html.escape(google_analytics_id, quote=True)
    js_id = json.dumps(google_analytics_id)
    analytics_region = json.dumps(normalize_region(region))
    analytics_page = json.dumps(page if page in {"map", "summary", "history", "about"} else "map")
    return f"""  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id={escaped_id}"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date());
    // ANALYTICS_HELPERS_START
    (() => {{
      const region = {analytics_region};
      const page = {analytics_page};
      const url = new URL(window.location.href);
      const modeKey = "crestmap-analytics-mode";
      const requestedMode = url.searchParams.get("analytics_mode");
      const validModes = ["internal", "developer", "visitor"];
      let mode = validModes.includes(requestedMode) ? requestedMode : "visitor";
      try {{
        if (validModes.includes(requestedMode)) localStorage.setItem(modeKey, requestedMode);
        else mode = localStorage.getItem(modeKey) || "visitor";
      }} catch (_error) {{ /* Tracking still works when storage is unavailable. */ }}
      if ({str(bool(admin_mode)).lower()}) mode = "internal";
      if (url.searchParams.has("analytics_mode")) {{
        url.searchParams.delete("analytics_mode");
        window.history.replaceState(window.history.state, "", url.toString());
      }}
      // Keep incident IDs, searches, form values and test markers out of page URLs.
      const cleanPageUrl = url.origin + url.pathname;
      let referrer = "";
      try {{
        if (document.referrer) {{
          const source = new URL(document.referrer);
          referrer = source.origin + source.pathname;
        }}
      }} catch (_error) {{}}
      const common = {{
        page_location: cleanPageUrl,
        page_referrer: referrer,
        page_title: `Crestmap ${{region}} ${{page}}`,
        map_region: region,
        page_type: page
      }};
      // Preserve conventional campaign labels without forwarding arbitrary URLs.
      for (const [query, field] of Object.entries({{
        utm_source: "campaign_source", utm_medium: "campaign_medium",
        utm_campaign: "campaign_name", utm_id: "campaign_id",
        utm_term: "campaign_term", utm_content: "campaign_content"
      }})) {{
        const value = url.searchParams.get(query);
        if (value && /^[a-z0-9_. ~-]{{1,100}}$/i.test(value)) common[field] = value;
      }}
      if (mode === "internal") common.traffic_type = "internal";
      if (mode === "developer") common.debug_mode = true;
      gtag('set', common);
      const allowedEvents = new Set([
        "incident_select", "camera_open", "camera_image_open", "region_change", "share", "alert_subscribe"
      ]);
      const allowedValues = {{
        incident_source: ["chp", "wildweb"],
        camera_status: ["online", "offline"],
        target_region: ["forest", "malibu"],
        method: ["copy_link"],
        content_type: ["incident"]
      }};
      window.crestmapTrack = (name, parameters = {{}}) => {{
        if (!allowedEvents.has(name)) return;
        const safe = {{ ...common }};
        for (const [key, values] of Object.entries(allowedValues)) {{
          if (values.includes(parameters[key])) safe[key] = parameters[key];
        }}
        gtag('event', name, safe);
      }};
      document.addEventListener("click", (event) => {{
        const link = event.target.closest?.("a.region-tab");
        if (!link) return;
        const target = new URL(link.href).searchParams.get("region");
        if (target !== region && ["forest", "malibu"].includes(target)) {{
          window.crestmapTrack("region_change", {{ target_region: target }});
        }}
      }});
      gtag('config', {js_id}, common);
    }})();
    // ANALYTICS_HELPERS_END
  </script>
"""


def scrape_source_label(source):
    labels = {"xml": "CHP XML", "cad": "CHP CAD", "wildweb": "WildWeb", "unknown": "unknown"}
    return labels.get((source or "unknown").casefold(), source or "unknown")


def incident_source_label(incident):
    return "WildWeb" if (incident.get("source") or "chp").casefold() == "wildweb" else "CHP"


def incident_status_label(incident):
    source = (incident.get("source") or "chp").casefold()
    status = (incident.get("status") or "").casefold()
    source_status = (incident.get("source_status") or "").casefold()
    if source == "wildweb":
        labels = {
            "listed": "Reported",
            "contained": "Contained",
            "controlled": "Controlled",
            "out": "Out",
            "no_longer_listed": "No longer listed",
            "aged_out": "Archived",
        }
        return labels.get(source_status, "Reported" if status == "reported" else "Archived")
    return "Active" if status == "active" else "Cleared"


def incident_status_class(incident):
    status = (incident.get("status") or "").casefold()
    return "status-active" if status == "active" else "status-reported" if status == "reported" else "status-cleared"


def incident_description(incident):
    description = str(incident.get("location_desc") or "").strip()
    if not description or not description.strip("* ._-/"):
        return ""
    normalized = " ".join(description.casefold().split())
    repeated_values = {
        " ".join(str(incident.get(field) or "").casefold().split())
        for field in ("type", "location")
    }
    return "" if normalized in repeated_values else description


def incident_location_lines(incident):
    location = str(incident.get("location") or "").strip()
    description = incident_description(incident)
    is_wildweb = (incident.get("source") or "chp").casefold() == "wildweb"
    if is_wildweb and description:
        return description, location
    return location or description, description if location else ""


def incident_recency(incident):
    value = (
        incident.get("source_reported_at")
        or incident.get("first_seen")
        or incident.get("latest_observed_at")
        or ""
    )
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def report_location_html(incident):
    primary, secondary = incident_location_lines(incident)
    parts = []
    if primary:
        parts.append(f'<span class="result-location-primary">{html.escape(primary)}</span>')
    if secondary:
        parts.append(f'<span class="result-location-secondary">{html.escape(secondary)}</span>')
    return "".join(parts)


def status_summary_text(status, hours):
    reported = int(status.get("reported_count", 0))
    prefix = f"{status['active_count']} active"
    if reported:
        prefix += f" · {reported} WildWeb"
    return f"{prefix} · {status['total_count']} in {hours:g}h · {status['mapped_count']} mapped"


def scrape_meta_html(last_scrape):
    if not last_scrape or not last_scrape.get("observed_at"):
        return '<span>Source update unavailable</span>'
    observed_at = html.escape(str(last_scrape.get("observed_at")))
    source = html.escape(scrape_source_label(last_scrape.get("source")))
    return (
        f'<span><span class="source-label">{source}</span> '
        f'<time id="last-scrape-at" datetime="{observed_at}">{observed_at}</time></span>'
    )


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
    last_scrape=None,
    admin_mode=False,
    admin_details_base="/api/v1/incidents",
    media_enabled=False,
    media_max_video_bytes=100 * 1024 * 1024,
    media_max_video_seconds=60,
    aircraft_tracking_enabled=False,
    app_version="dev",
):
    region = normalize_region(region)
    map_label = region_label(region)
    viewport = region_viewport(region)
    roadway_mile_markers = MILE_MARKERS if region == "forest" else {}
    offline_road_data = (
        {}
        if region == "forest"
        else {
            road: [[list(point) for point in line] for line in lines]
            for road, lines in MALIBU_OFFLINE_ROADS.items()
        }
    )
    lat_min, lat_max, lon_min, lon_max = REGION_BOUNDS[region]
    offline_region_outline = [
        [lat_min, lon_min], [lat_max, lon_min], [lat_max, lon_max],
        [lat_min, lon_max], [lat_min, lon_min],
    ]
    status = {**incident_status(incidents, hours), "region": region}
    active_count = status["active_count"]
    mapped_count = status["mapped_count"]
    title = f"Crestmap {map_label} Incidents ({status['current_count']} current, {status['total_count']} total)"
    if region == "forest":
        description = (
            "Live and historical CHP traffic incidents and WildWeb dispatch reports for Angeles Crest, "
            "Angeles Forest, Big Tujunga, Glendora Mountain, and nearby forest roads."
        )
    else:
        description = (
            "Live and historical CHP traffic incidents and matching WildWeb dispatch reports for Malibu canyon and coastal roads."
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
        aircraft_endpoint = f"{public_path}/api/v1/aircraft" if public_path else "/api/v1/aircraft"
    else:
        status_endpoint = f"{asset_base}/status.json"
        incidents_endpoint = f"{asset_base}/incidents.json"
        aircraft_endpoint = f"{asset_base}/api/v1/aircraft"
    camera_metadata_endpoint = "https://cameras.alertcalifornia.org/public-camera-data/all_cameras-v3.json"
    camera_data_base = "https://cameras.alertcalifornia.org/public-camera-data"
    camera_viewer_base = "https://cameras.alertcalifornia.org/"
    admin_details_base = admin_details_base.rstrip("/")
    media_form_markup = """
                <label class="comment-field media-picker">
                  <span>Add photos or a short video <span class="comment-field-hint">(optional)</span></span>
                  <input name="media_files" type="file" accept="image/jpeg,image/png,image/webp,video/mp4" multiple>
                  <span class="comment-field-hint">Up to 3 photos, or 1 MP4 video. Photos are compressed before upload.</span>
                </label>
                <div class="media-preview" data-media-preview></div>
    """ if media_enabled else ""
    structured_data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": f"{urls['canonical']}#website",
                "name": f"Crestmap {map_label} Incidents",
                "url": urls["canonical"],
                "description": description,
                "inLanguage": "en-US",
            },
            {
                "@type": "WebApplication",
                "@id": f"{urls['canonical']}#app",
                "name": f"Crestmap {map_label} Incidents",
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
                "name": f"Crestmap {map_label.lower()} incident history",
                "url": urls["canonical"],
                "description": (
                    f"Rolling incident history collected from public CHP and WildWeb sources for selected "
                    f"{map_label.lower()} roads and places."
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
{pwa_head_html(base_path)}
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
{analytics_script(google_analytics_id, region, "map", admin_mode)}\
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQ9um5Lj053hphD7uW9P4U5F9VAt5x0=" crossorigin="">
  <style>
{TEMPERATURE_CSS}
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
    .leaflet-zoom-animated {{
      transform-origin: 0 0;
    }}
    svg.leaflet-zoom-animated {{
      will-change: transform;
    }}
    .leaflet-zoom-anim .leaflet-zoom-animated {{
      transition: transform 0.25s cubic-bezier(0, 0, 0.25, 1);
    }}
    .leaflet-zoom-anim .leaflet-tile,
    .leaflet-pan-anim .leaflet-tile {{
      transition: none;
    }}
    .leaflet-zoom-anim .leaflet-zoom-hide {{
      visibility: hidden;
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
    #map .leaflet-control-attribution {{
      box-sizing: border-box;
      max-width: calc(100vw - 12px);
      margin: 0 4px 4px 0;
      padding: 2px 4px;
      border-radius: 4px;
      background: rgba(255, 255, 255, 0.82);
      font-size: 8px;
      line-height: 1.2;
      text-align: right;
      white-space: normal;
      overflow-wrap: anywhere;
      -webkit-text-size-adjust: none;
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
    .visually-hidden {{
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
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
      z-index: 4;
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
      position: fixed;
      top: 40px;
      right: 0;
      z-index: 20;
      width: min(290px, calc(100vw - 36px));
      box-sizing: border-box;
      max-height: calc(100svh - 64px);
      overflow-y: auto;
      overscroll-behavior: contain;
      touch-action: pan-y;
      -webkit-overflow-scrolling: touch;
      padding: 6px;
      padding-bottom: max(6px, env(safe-area-inset-bottom));
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
      width: 100%;
      border: 0;
      background: transparent;
      font-family: inherit;
      text-align: left;
      cursor: pointer;
      box-sizing: border-box;
    }}
    .view-menu-row .view-menu-label {{
      flex: 0 0 auto;
      color: inherit;
      font-size: 13px;
      font-weight: 800;
    }}
    .view-menu-row .view-menu-description {{
      min-width: 0;
      max-width: 65%;
      color: #46534b;
      font-size: 12px;
      font-weight: 700;
      line-height: 1.2;
      text-align: right;
      white-space: normal;
      overflow-wrap: anywhere;
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
    #connection-status {{
      display: flex;
      align-items: center;
      gap: 7px;
      margin-top: 8px;
      color: #3f4d45;
      font-size: 12px;
      font-weight: 700;
      line-height: 1.35;
    }}
    #connection-status::before {{
      content: "";
      flex: 0 0 auto;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #2f8a4e;
      box-shadow: 0 0 0 2px rgba(47, 138, 78, 0.14);
    }}
    #connection-status[data-state="reconnecting"]::before {{
      background: #c58b19;
      box-shadow: 0 0 0 2px rgba(197, 139, 25, 0.16);
    }}
    #connection-status[data-state="offline"] {{
      color: #6b4714;
    }}
    #connection-status[data-state="unavailable"] {{
      color: #6b4714;
    }}
    #connection-status[data-state="unavailable"]::before,
    #connection-status[data-state="offline"]::before {{
      background: #b76528;
      box-shadow: 0 0 0 2px rgba(183, 101, 40, 0.16);
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
    .incident.is-wildweb-aging {{
      transition: filter 180ms ease, background-color 180ms ease;
    }}
    .incident.is-wildweb-aging:not([aria-current="true"]) {{
      filter: saturate(var(--incident-age-saturation, 1));
    }}
    .incident.is-wildweb-aging[aria-current="true"] > * {{
      filter: saturate(var(--incident-age-saturation, 1));
      transition: filter 180ms ease;
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
    .incident .incident-location-primary {{
      margin: -1px 0 3px;
      color: #35453b;
      font-weight: 800;
    }}
    .incident .incident-location-secondary {{
      margin-bottom: 3px;
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
    .mile-marker {{
      border: 0;
      background: transparent;
      pointer-events: none;
    }}
    .mile-marker-content {{
      display: inline-flex;
      align-items: center;
      padding: 1px 3px;
      border: 1px solid rgba(56, 74, 62, 0.16);
      border-radius: 3px;
      color: rgba(42, 57, 47, 0.62);
      background: rgba(250, 251, 247, 0.58);
      box-shadow: none;
      font-size: 8px;
      font-weight: 700;
      line-height: 1;
      letter-spacing: 0;
      white-space: nowrap;
      transform: translate(2px, -50%);
    }}
    #locate-user {{
      box-sizing: border-box;
      position: absolute;
      top: 12px;
      right: 12px;
      z-index: 1000;
      display: grid;
      place-items: center;
      width: 36px;
      height: 36px;
      padding: 0;
      border: 1px solid rgba(42, 57, 47, 0.25);
      border-radius: 9px;
      color: #385142;
      background: rgba(255, 255, 255, 0.92);
      box-shadow: 0 2px 8px rgba(24, 32, 38, 0.16);
      cursor: pointer;
      backdrop-filter: blur(3px);
      -webkit-backdrop-filter: blur(3px);
    }}
    #locate-user:hover {{
      color: #1f6840;
      background: #ffffff;
    }}
    #locate-user:focus-visible {{
      outline: 2px solid rgba(39, 116, 71, 0.55);
      outline-offset: 2px;
    }}
    #locate-user.is-loading svg {{
      animation: locate-spin 900ms linear infinite;
    }}
    #locate-user svg {{
      display: block;
      width: 20px;
      height: 20px;
      fill: none;
      stroke: currentColor;
      stroke-width: 1.8;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}
    .map-layer-menu {{
      position: absolute; top: 12px; left: 12px; z-index: 1001;
    }}
    .map-layer-menu summary {{
      box-sizing: border-box; display: grid; place-items: center; width: 36px; height: 36px;
      padding: 0; border: 1px solid rgba(42,57,47,.25); border-radius: 9px;
      color: #385142; background: rgba(255,255,255,.96); box-shadow: 0 2px 8px rgba(24,32,38,.16);
      cursor: pointer; list-style: none;
    }}
    .map-layer-menu summary::-webkit-details-marker {{ display: none; }}
    .map-layer-menu[open] summary {{ color: #fff; border-color: #277447; background: #277447; }}
    .map-layer-menu summary:focus-visible {{ outline: 2px solid rgba(39,116,71,.55); outline-offset: 2px; }}
    .map-layer-menu summary svg {{ width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; }}
    .map-layer-popover {{
      position: absolute; top: 42px; left: 0; width: min(276px, calc(100vw - 80px)); overflow: hidden;
      border: 1px solid rgba(56,74,62,.22); border-radius: 13px; background: rgba(251,252,248,.98);
      box-shadow: 0 8px 24px rgba(24,32,38,.2); color: #344239;
    }}
    .map-layer-heading {{ padding: 11px 13px 9px; border-bottom: 1px solid #d8ddd2; }}
    .map-layer-heading strong, .map-layer-heading span {{ display: block; }}
    .map-layer-heading strong {{ font-size: 14px; }}
    .map-layer-heading span {{ margin-top: 2px; color: #687268; font-size: 10px; }}
    .map-layer-popover .view-menu-row {{
      min-height: 48px; border-radius: 0; border: 0; border-bottom: 1px solid #e1e5dc; box-shadow: none;
    }}
    .map-layer-popover .view-menu-row:last-child {{ border-bottom: 0; }}
    .map-layer-popover .view-menu-description {{ flex: 1 1 auto; }}
    .map-layer-switch {{
      position: relative; flex: 0 0 auto; width: 30px; height: 18px; border-radius: 999px;
      background: #b7c0b8; transition: background 150ms ease;
    }}
    .map-layer-switch::after {{
      content: ""; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px;
      border-radius: 50%; background: #fff; box-shadow: 0 1px 2px rgba(24,32,38,.25); transition: transform 150ms ease;
    }}
    .map-layer-popover .view-menu-row[aria-pressed="true"] .map-layer-switch {{ background: #277447; }}
    .map-layer-popover .view-menu-row[aria-pressed="true"] .map-layer-switch::after {{ transform: translateX(12px); }}
    #location-status {{
      position: absolute;
      top: 16px;
      right: 56px;
      z-index: 1000;
      max-width: min(230px, calc(100% - 76px));
      padding: 5px 8px;
      border: 1px solid rgba(42, 57, 47, 0.18);
      border-radius: 7px;
      color: #45534a;
      background: rgba(255, 255, 255, 0.92);
      box-shadow: 0 1px 5px rgba(24, 32, 38, 0.12);
      font-size: 11px;
      font-weight: 700;
      line-height: 1.25;
      pointer-events: none;
      backdrop-filter: blur(3px);
      -webkit-backdrop-filter: blur(3px);
    }}
    #location-status:empty {{
      display: none;
    }}
    .user-location-marker {{
      box-sizing: border-box;
      width: 18px;
      height: 18px;
      border: 3px solid #ffffff;
      border-radius: 50%;
      background: #2878e6;
      box-shadow: 0 1px 7px rgba(19, 66, 132, 0.48), 0 0 0 2px rgba(40, 120, 230, 0.24);
      pointer-events: none;
    }}
    @keyframes locate-spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .incident-marker-dot {{
      box-sizing: border-box;
      position: absolute;
      inset: 0;
      display: block;
      border-radius: 999px;
      pointer-events: none;
    }}
    .camera-marker {{
      background: transparent;
      border: 0;
    }}
    .camera-marker-symbol {{
      position: absolute;
      inset: 0;
      display: block;
      filter: drop-shadow(0 1px 3px rgba(24, 32, 38, 0.42));
      pointer-events: none;
    }}
    .camera-marker-symbol svg {{
      display: block;
      width: 30px;
      height: 30px;
      overflow: visible;
    }}
    .camera-marker-bearing {{
      transform: rotate(var(--camera-bearing, 0deg));
      transform-origin: 15px 15px;
    }}
    .camera-marker-arrow {{
      fill: #2477a6;
      stroke: #ffffff;
      stroke-width: 1.8;
      stroke-linejoin: round;
    }}
    .camera-marker-center {{
      fill: #ffffff;
      stroke: #185678;
      stroke-width: 1.4;
    }}
    .camera-marker.is-offline .camera-marker-arrow {{
      fill: #89928c;
      stroke: #ffffff;
    }}
    .camera-marker.is-offline .camera-marker-center {{
      stroke: #5f6862;
    }}
    .camera-marker.is-selected .camera-marker-symbol::before {{
      content: "";
      position: absolute;
      inset: -6px;
      border: 3px solid rgba(31, 104, 64, 0.78);
      border-radius: 999px;
      box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.88), 0 2px 12px rgba(24, 32, 38, 0.3);
    }}
    .camera-image-link {{
      display: block;
      width: 100%;
      margin-top: 12px;
      border-radius: 8px;
      cursor: zoom-in;
    }}
    .camera-image-link:focus-visible {{
      outline: 3px solid rgba(31, 104, 64, 0.42);
      outline-offset: 3px;
    }}
    .camera-image {{
      display: block;
      width: 100%;
      height: auto;
      margin: 0;
      border: 1px solid #d8ddd2;
      border-radius: 8px;
      background: #e8ece6;
    }}
    .camera-image.is-unavailable {{
      min-height: 150px;
      object-fit: contain;
    }}
    .camera-lightbox[hidden] {{
      display: none;
    }}
    .camera-lightbox {{
      position: fixed;
      inset: 0;
      z-index: 2400;
      display: grid;
      place-items: center;
      box-sizing: border-box;
      padding: 24px;
      background: rgba(10, 15, 12, 0.82);
      backdrop-filter: blur(4px);
    }}
    .camera-lightbox-panel {{
      display: flex;
      flex-direction: column;
      width: min(1180px, 100%);
      max-height: calc(100dvh - 48px);
      overflow: hidden;
      border: 1px solid rgba(255, 255, 255, 0.22);
      border-radius: 12px;
      background: #0c110e;
      box-shadow: 0 18px 60px rgba(0, 0, 0, 0.46);
    }}
    .camera-lightbox-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 14px;
      color: #f4f7ee;
      background: #18392b;
    }}
    .camera-lightbox-heading {{
      min-width: 0;
    }}
    .camera-lightbox-heading strong,
    .camera-lightbox-heading span {{
      display: block;
    }}
    .camera-lightbox-heading strong {{
      overflow: hidden;
      font-size: 15px;
      line-height: 1.25;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .camera-lightbox-heading span {{
      margin-top: 2px;
      color: #cfe0d3;
      font-size: 11px;
    }}
    .camera-lightbox-close {{
      flex: 0 0 auto;
      min-width: 44px;
      min-height: 38px;
      padding: 7px 12px;
      border: 1px solid rgba(255, 255, 255, 0.38);
      border-radius: 7px;
      color: #f4f7ee;
      background: rgba(255, 255, 255, 0.1);
      font: inherit;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }}
    .camera-lightbox-close:hover {{
      background: rgba(255, 255, 255, 0.18);
    }}
    .camera-lightbox-stage {{
      display: flex;
      flex: 1 1 auto;
      align-items: center;
      justify-content: center;
      min-height: 0;
      overflow: auto;
      background: #080b09;
      overscroll-behavior: contain;
    }}
    .camera-lightbox-image {{
      display: block;
      width: auto;
      max-width: 100%;
      height: auto;
      max-height: calc(100dvh - 112px);
      object-fit: contain;
      touch-action: pinch-zoom;
    }}
    body.camera-lightbox-open {{
      overflow: hidden;
    }}
    .camera-image-meta {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: 6px;
      color: #58645d;
      font-size: 11px;
      line-height: 1.35;
    }}
    .camera-credit {{
      margin: 8px 0 0;
      color: #46534b;
      font-size: 11px;
      font-weight: 700;
      line-height: 1.35;
    }}
    .camera-direction {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .camera-direction svg {{
      width: 16px;
      height: 16px;
      fill: #2477a6;
      transform: rotate(var(--camera-bearing, 0deg));
      transform-origin: 50% 50%;
    }}
    .incident-marker-core {{
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
    .incident-marker.is-cleared .incident-marker-core {{
      border-color: #5f6862;
      background: #b8bfba;
    }}
    .incident-marker.is-wildweb-no-longer-listed .incident-marker-core {{
      border-color: #596a72;
      background: #b8bfba;
    }}
    .incident-marker.is-wildweb-aged-out .incident-marker-core {{
      border-color: #967037;
      background: #b8bfba;
    }}
    .incident-marker.is-reported .incident-marker-core {{
      border-color: #805b12;
      background: #e5a72f;
    }}
    .incident-marker.is-wildweb-aging .incident-marker-core {{
      filter: saturate(var(--incident-age-saturation, 1));
      transition: filter 180ms ease;
    }}
    .incident-marker.is-selected .incident-marker-core {{
      background: #f05a40;
      box-shadow: 0 2px 9px rgba(24, 32, 38, 0.42);
    }}
    .incident-marker.is-selected.is-cleared .incident-marker-core {{
      background: #9da5a0;
    }}
    .incident-marker.is-selected.is-reported .incident-marker-core {{
      background: #f0b43f;
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
    .incident-marker.is-selected.is-reported .incident-marker-dot::before {{
      border-color: rgba(143, 96, 8, 0.78);
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
    .incident-marker.is-pulsing.is-reported .incident-marker-dot::after {{
      border-color: rgba(143, 96, 8, 0.62);
    }}
    .aircraft-marker {{
      display: flex;
      align-items: center;
      justify-content: center;
      border: 1px solid rgba(103, 77, 0, 0.72);
      border-radius: 50%;
      color: #d6a000;
      background: rgba(255, 220, 72, 0.58);
      box-shadow: 0 1px 4px rgba(24, 32, 38, 0.28);
      line-height: 1;
      backdrop-filter: blur(1px);
      -webkit-backdrop-filter: blur(1px);
    }}
    .aircraft-marker svg {{
      display: block;
      width: 19px;
      height: 15px;
      overflow: visible;
      fill: currentColor;
      stroke: currentColor;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}
    .aircraft-marker.is-stale {{
      opacity: 0.52;
      filter: saturate(0.62);
    }}
    .aircraft-popup-title {{
      margin-bottom: 3px;
      font-size: 13px;
      font-weight: 900;
    }}
    .aircraft-map-popup .leaflet-popup-content-wrapper {{
      border: 1px solid rgba(93, 104, 96, 0.28);
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.94);
      box-shadow: 0 3px 12px rgba(24, 32, 38, 0.22);
    }}
    .aircraft-map-popup .leaflet-popup-content {{
      min-width: 165px;
      max-width: 215px;
      margin: 9px 11px;
      font-size: 12px;
      line-height: 1.35;
    }}
    .aircraft-map-popup .leaflet-popup-tip {{
      background: rgba(255, 255, 255, 0.94);
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
    #map.using-offline-basemap .leaflet-tile-pane {{
      opacity: 0;
    }}
    #map {{
      background: #eef1e9;
    }}
    #map .offline-basemap-road {{
      filter: drop-shadow(0 1px 0 rgba(255, 255, 255, 0.85));
    }}
    #map .offline-basemap-label {{
      color: #4a5b50;
      background: rgba(246, 247, 244, 0.9);
      border: 0;
      box-shadow: none;
      font-size: 9px;
      font-weight: 800;
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
      display: block;
      margin-bottom: 6px;
    }}
    .detail-title {{
      min-width: 0;
    }}
    .detail-actions {{
      flex: 0 0 auto;
      display: flex;
      align-items: flex-start;
      justify-content: flex-start;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }}
    .detail-panel h2 {{
      margin: 0 0 6px;
      font-size: 18px;
      line-height: 1.25;
      letter-spacing: 0;
    }}
    .detail-location-primary {{
      margin: -1px 0 3px;
      color: #35453b;
      font-size: 14px;
      font-weight: 800;
      line-height: 1.3;
    }}
    .share-incident,
    .default-view,
    .hidden-details-toggle {{
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
    .hidden-details-toggle {{
      color: #7a4e09;
      border-color: #d8bd83;
      background: #fff8e8;
    }}
    .share-incident:focus,
    .share-incident:hover,
    .default-view:focus,
    .default-view:hover,
    .hidden-details-toggle:focus,
    .hidden-details-toggle:hover {{
      border-color: #94b69a;
      background: #edf5ed;
      outline: none;
    }}
    .detail-section {{
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid #e5e8e1;
    }}
    .hidden-detail-section {{
      padding: 12px;
      border: 1px solid #e1c890;
      border-radius: 8px;
      background: #fffaf0;
    }}
    .hidden-detail-note {{
      margin-bottom: 10px;
      color: #72510e;
      font-size: 12px;
      line-height: 1.4;
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
    .comments-list {{
      display: grid;
      gap: 10px;
      margin: 10px 0 0;
    }}
    .comment {{
      padding: 10px;
      border: 1px solid #e2e8de;
      border-radius: 8px;
      background: #fbfcfa;
    }}
    .comment-meta {{
      margin-bottom: 4px;
      color: #58645d;
      font-size: 12px;
      line-height: 1.35;
    }}
    .comment-body {{
      font-size: 13px;
      line-height: 1.4;
      white-space: pre-wrap;
    }}
    .comment-media {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 8px;
      margin-top: 9px;
    }}
    .comment-media img,
    .comment-media video {{
      display: block;
      width: 100%;
      max-height: 280px;
      border-radius: 7px;
      object-fit: contain;
      background: #111;
    }}
    .comment-form {{
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }}
    .comment-form-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      align-items: end;
      gap: 8px;
    }}
    .comment-field {{
      display: grid;
      min-width: 0;
      gap: 4px;
      color: #3f4a44;
      font-size: 12px;
      font-weight: 700;
      line-height: 1.3;
    }}
    .comment-field-hint {{
      color: #68736c;
      font-weight: 400;
    }}
    .comment-form input,
    .comment-form textarea {{
      box-sizing: border-box;
      width: 100%;
      min-width: 0;
      padding: 8px 9px;
      border: 1px solid #cfd8cf;
      border-radius: 7px;
      color: #1d252a;
      background: #ffffff;
      font: inherit;
      font-size: 13px;
      line-height: 1.35;
    }}
    .comment-form textarea {{
      min-height: 78px;
      resize: vertical;
    }}
    .comment-form input:focus,
    .comment-form textarea:focus {{
      border-color: #2b7c4a;
      outline: 2px solid rgba(43, 124, 74, 0.18);
      outline-offset: 0;
    }}
    .comment-honeypot {{
      position: absolute;
      left: -10000px;
      width: 1px;
      height: 1px;
      overflow: hidden;
    }}
    .comment-submit {{
      justify-self: start;
      min-height: 32px;
      padding: 6px 10px;
      border: 1px solid #2b7c4a;
      border-radius: 7px;
      color: #ffffff;
      background: #2b7c4a;
      font: inherit;
      font-size: 13px;
      font-weight: 800;
      cursor: pointer;
    }}
    .comment-submit:disabled {{
      cursor: wait;
      opacity: 0.72;
    }}
    .comment-status {{
      color: #58645d;
      font-size: 12px;
      line-height: 1.35;
    }}
    .media-picker input[type="file"] {{
      padding: 7px;
      background: #f8faf6;
    }}
    .media-preview {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 7px;
    }}
    .media-preview-item {{
      min-width: 0;
      padding: 7px;
      border: 1px solid #dce3d7;
      border-radius: 7px;
      color: #58645d;
      background: #f8faf6;
      font-size: 11px;
      overflow-wrap: anywhere;
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
    .status-reported {{
      color: #704b08;
      background: #fff0c9;
    }}
    .source-pill {{
      display: inline-block;
      margin: 0 0 6px 5px;
      padding: 2px 7px;
      border: 1px solid #cbd6cc;
      border-radius: 999px;
      color: #405047;
      background: #f8faf6;
      font-size: 10px;
      font-weight: 800;
      line-height: 1.35;
      text-transform: uppercase;
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
    @media (max-width: 420px) {{
      .comment-form-row {{
        grid-template-columns: minmax(0, 1fr);
      }}
    }}
    @media (max-width: 760px) {{
      .camera-lightbox {{
        padding: 0;
      }}
      .camera-lightbox-panel {{
        width: 100%;
        height: 100dvh;
        max-height: none;
        border: 0;
        border-radius: 0;
      }}
      .camera-lightbox-header {{
        padding: max(10px, env(safe-area-inset-top)) max(12px, env(safe-area-inset-right)) 10px max(12px, env(safe-area-inset-left));
      }}
      .camera-lightbox-image {{
        max-height: calc(100dvh - 70px - env(safe-area-inset-top));
      }}
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
      .map-layer-popover {{
        max-height: calc(45svh - 66px);
        overflow-x: hidden;
        overflow-y: auto;
        overscroll-behavior: contain;
        -webkit-overflow-scrolling: touch;
      }}
      #details-cue {{
        display: flex;
        position: absolute;
        left: 50%;
        bottom: var(--details-cue-bottom, 40px);
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
  <style>{push_ui_css()}</style>
  <style>{ROAD_WEATHER_CSS}</style>
</head>
<body>
  <div id="app">
    <aside id="sidebar">
      <header>
        <div class="title-row">
          <h1>Crestmap {html.escape(map_label)} Incidents</h1>
          {view_menu(base_path, "map", hours, region, admin_mode=admin_mode, aircraft_tracking_enabled=aircraft_tracking_enabled)}
        </div>
        <div class="meta">{html.escape(status_summary_text(status, hours))}</div>
        <div class="meta checked-meta"><span>Updated <time id="generated-at" datetime="{html.escape(generated_at)}">{html.escape(generated_at)}</time></span><span aria-hidden="true">·</span>
          <label class="auto-refresh-control" title="Automatically reload when new incident data is available">
            <input type="checkbox" id="auto-refresh-enabled">
            Auto refresh
          </label>
        </div>
        <div id="connection-status" data-state="online" role="status" aria-live="polite">Online</div>
        <nav class="range-tabs" aria-label="History range">{history_controls(hours, region)}</nav>
        <nav class="region-tabs" aria-label="Region">{region_tabs(base_path, "map", hours, region, region_statuses)}</nav>
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
    <main id="map">
      {map_layer_menu(region, aircraft_tracking_enabled)}
      <button type="button" id="locate-user" aria-label="Show my location" title="Show my location">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <circle cx="12" cy="12" r="5"></circle>
          <path d="M12 2v3M12 19v3M2 12h3M19 12h3"></path>
        </svg>
      </button>
      <span id="location-status" role="status" aria-live="polite"></span>
      <button type="button" id="details-cue">Incident details below</button>
    </main>
    <aside id="details"></aside>
  </div>
  <div id="camera-lightbox" class="camera-lightbox" role="dialog" aria-modal="true" aria-labelledby="camera-lightbox-title" hidden>
    <div class="camera-lightbox-panel">
      <div class="camera-lightbox-header">
        <div class="camera-lightbox-heading">
          <strong id="camera-lightbox-title">Camera view</strong>
          <span>ALERTCalifornia | UC San Diego</span>
        </div>
        <button type="button" class="camera-lightbox-close" data-camera-lightbox-close>Close</button>
      </div>
      <div class="camera-lightbox-stage">
        <img class="camera-lightbox-image" data-camera-lightbox-image alt="">
      </div>
    </div>
  </div>
{push_ui_html(base_path)}
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const initialDataStatus = {json.dumps(status, ensure_ascii=False)};
    const appVersion = {json.dumps(app_version)};
    const statusEndpoint = "{html.escape(status_endpoint)}";
    const incidentsEndpoint = "{html.escape(incidents_endpoint)}";
    const aircraftEndpoint = "{html.escape(aircraft_endpoint)}";
    const aircraftTrackingEnabled = {json.dumps(bool(aircraft_tracking_enabled))};
    const cameraMetadataEndpoint = "{camera_metadata_endpoint}";
    const cameraDataBase = "{camera_data_base}";
    const cameraViewerBase = "{camera_viewer_base}";
    const commentsBaseEndpoint = "/api/v1/incidents";
    const mediaEnabled = {json.dumps(bool(media_enabled))};
    const mediaMaxVideoBytes = {int(media_max_video_bytes)};
    const mediaMaxVideoSeconds = {int(media_max_video_seconds)};
    const adminMode = {json.dumps(bool(admin_mode))};
    const adminDetailsBase = "{html.escape(admin_details_base)}";
    const currentRegion = "{html.escape(region)}";
    const currentRegionBounds = {json.dumps([lat_min, lat_max, lon_min, lon_max])};
    const roadwayMileMarkers = {json.dumps(roadway_mile_markers, separators=(",", ":"))};
    const offlineRoadData = {json.dumps(offline_road_data, separators=(",", ":"))};
    const offlineRoads = currentRegion === "forest"
      ? Object.fromEntries(Object.entries(roadwayMileMarkers).map(([road, points]) => [
          road, [points.map(([, latitude, longitude]) => [latitude, longitude])]
        ]))
      : offlineRoadData;
    const offlineRegionOutline = {json.dumps(offline_region_outline, separators=(",", ":"))};
    let incidents = [];
    let currentDataStatus = initialDataStatus;
    let selectedIncidentKey = new URLSearchParams(window.location.search).get("incident");

    // OFFLINE_DATA_HELPERS_START
    const INCIDENT_SNAPSHOT_DATABASE = "crestmap-offline-data";
    const INCIDENT_SNAPSHOT_STORE = "incident-snapshots";

    function incidentSnapshotKey(region, hours) {{
      return `${{region}}:${{Number(hours)}}`;
    }}

    function incidentSnapshotRecord(payload, context) {{
      if (!payload || !Array.isArray(payload.incidents) || !payload.status) return null;
      const hours = Number(context.hours);
      return {{
        key: incidentSnapshotKey(context.region, hours),
        region: context.region,
        hours,
        saved_at: context.savedAt || new Date().toISOString(),
        payload
      }};
    }}

    function isUsableIncidentSnapshot(record, context) {{
      return Boolean(
        record && record.region === context.region &&
        Number(record.hours) === Number(context.hours) &&
        record.payload && Array.isArray(record.payload.incidents) && record.payload.status
      );
    }}

    function connectionStateFor({{ online, requestFailed, hasSnapshot, checking = false }}) {{
      if (online && checking) return {{ state: "reconnecting", hasSnapshot: Boolean(hasSnapshot) }};
      if (!online) return {{ state: "offline", hasSnapshot: Boolean(hasSnapshot) }};
      if (requestFailed) return {{ state: "unavailable", hasSnapshot: Boolean(hasSnapshot) }};
      return {{ state: "online", hasSnapshot: Boolean(hasSnapshot) }};
    }}
    // OFFLINE_DATA_HELPERS_END

    function currentSnapshotContext() {{
      return {{
        region: currentRegion,
        hours: Number(new URLSearchParams(window.location.search).get("hours") || currentDataStatus.hours || 72)
      }};
    }}

    function openIncidentSnapshotDatabase() {{
      return new Promise((resolve, reject) => {{
        if (!window.indexedDB) return reject(new Error("IndexedDB is unavailable"));
        const request = window.indexedDB.open(INCIDENT_SNAPSHOT_DATABASE, 1);
        request.onupgradeneeded = () => {{
          if (!request.result.objectStoreNames.contains(INCIDENT_SNAPSHOT_STORE)) {{
            request.result.createObjectStore(INCIDENT_SNAPSHOT_STORE, {{ keyPath: "key" }});
          }}
        }};
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error || new Error("Could not open offline data"));
      }});
    }}

    async function readIncidentSnapshot(context) {{
      const database = await openIncidentSnapshotDatabase();
      try {{
        return await new Promise((resolve, reject) => {{
          const transaction = database.transaction(INCIDENT_SNAPSHOT_STORE, "readonly");
          const request = transaction.objectStore(INCIDENT_SNAPSHOT_STORE).get(
            incidentSnapshotKey(context.region, context.hours)
          );
          request.onsuccess = () => resolve(request.result || null);
          request.onerror = () => reject(request.error || new Error("Could not read offline data"));
        }});
      }} finally {{
        database.close();
      }}
    }}

    async function saveIncidentSnapshot(payload, context) {{
      const record = incidentSnapshotRecord(payload, context);
      if (!record) return false;
      const database = await openIncidentSnapshotDatabase();
      try {{
        await new Promise((resolve, reject) => {{
          const transaction = database.transaction(INCIDENT_SNAPSHOT_STORE, "readwrite");
          transaction.objectStore(INCIDENT_SNAPSHOT_STORE).put(record);
          transaction.oncomplete = () => resolve();
          transaction.onerror = () => reject(transaction.error || new Error("Could not save offline data"));
        }});
        return true;
      }} finally {{
        database.close();
      }}
    }}

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
    const offlineBasemapPane = map.createPane("offlineBasemap");
    offlineBasemapPane.style.zIndex = "175";
    offlineBasemapPane.style.pointerEvents = "none";
    const offlineBasemapLayer = L.layerGroup().addTo(map);
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
      if (tileErrors >= 3) {{
        mapEl.classList.add("using-offline-basemap");
        if (connectionStatus?.dataset.state === "online") setConnectivityStatus("online");
      }} else if (navigator.onLine) {{
        mapEl.classList.remove("using-offline-basemap");
      }}
    }});
    let tileErrors = 0;
    baseLayer.on("tileerror", () => {{
      tileErrors += 1;
      if (tileErrors >= 3) {{
        mapEl.classList.add("using-offline-basemap");
        if (connectionStatus?.dataset.state === "online") setConnectivityStatus("online");
      }}
    }});
    baseLayer.on("loading", () => {{
      tileErrors = 0;
      mapEl.classList.add("is-loading");
      window.clearTimeout(window.chpTileLoadingTimer);
      window.chpTileLoadingTimer = window.setTimeout(() => {{
        mapEl.classList.remove("is-loading");
      }}, 1800);
    }});
    baseLayer.addTo(map);

    const mileMarkerPane = map.createPane("mileMarkers");
    mileMarkerPane.style.zIndex = "425";
    mileMarkerPane.style.pointerEvents = "none";
    const mileMarkerLayer = L.layerGroup().addTo(map);
    const cameraFovPane = map.createPane("cameraFov");
    cameraFovPane.style.zIndex = "390";
    cameraFovPane.style.pointerEvents = "none";
    const cameraMarkerPane = map.createPane("cameraMarkers");
    cameraMarkerPane.style.zIndex = "540";
    const cameraLayer = L.layerGroup().addTo(map);
    const cameraFovLayer = L.layerGroup().addTo(map);
    const userLocationPane = map.createPane("userLocation");
    userLocationPane.style.zIndex = "575";
    let mileMarkersVisible = currentRegion === "forest" &&
      window.localStorage.getItem("crestmap-mile-markers") !== "hidden";
    let userLocationMarker = null;
    let userAccuracyCircle = null;
    let userLocationWatchId = null;
    let userLocationCoordinates = null;
    let userLocationFollowing = false;
    let forceUserLocationRecenter = false;
    let userLocationRequested = false;

    const markers = new Map();
    let incidentLayerVisible = window.localStorage.getItem("crestmap-incident-layer") !== "hidden";
    let revealedIncidentKey = null;
    const cameraMarkers = new Map();
    const aircraftMarkers = new Map();
    const aircraftTrails = new Map();
    let aircraftLayerVisible = window.localStorage.getItem("crestmap-aircraft-layer") !== "hidden";
    let cameraLayerVisible = window.localStorage.getItem("crestmap-camera-layer") !== "hidden";
    let cameraLayerLoadFailed = false;
    const cameraMetadataRefreshMs = 5 * 60 * 1000;
    let cameraDataFetchedAt = 0;
    let cameraDataFetchInFlight = false;
    let cameras = [];
    let selectedCameraId = new URLSearchParams(window.location.search).get("camera");
    let selectedCamera = null;
    let cameraImageRefreshTimer = null;
    const listShell = document.getElementById("incident-list-shell");
    const list = document.getElementById("incident-list");
    const scrollIncidentsButton = document.getElementById("scroll-incidents");
    const detailsPanel = document.getElementById("details");
    const detailsCue = document.getElementById("details-cue");
    const appShell = document.getElementById("app");
    const cameraLayerToggle = document.querySelector("[data-camera-layer-toggle]");
    const cameraLightbox = document.getElementById("camera-lightbox");
    const cameraLightboxImage = cameraLightbox?.querySelector("[data-camera-lightbox-image]");
    const cameraLightboxTitle = document.getElementById("camera-lightbox-title");
    const cameraLightboxClose = cameraLightbox?.querySelector("[data-camera-lightbox-close]");
    const connectionStatus = document.getElementById("connection-status");
    let activeSnapshotSavedAt = null;
    window.chpLiveMap = {{ map, markers, cameraMarkers, aircraftMarkers, aircraftTrails, cameraLayer, cameraFovLayer, mileMarkerLayer, offlineBasemapLayer, incidents, cameras, status: currentDataStatus }};

    function readableSnapshotTime(value) {{
      const date = new Date(value || "");
      if (Number.isNaN(date.getTime())) return "an unknown time";
      return date.toLocaleString([], {{ month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }});
    }}

    function setConnectivityStatus(state, savedAt = activeSnapshotSavedAt) {{
      if (!connectionStatus) return;
      connectionStatus.dataset.state = state;
      if (state === "online") {{
        const onlineText = savedAt
          ? `Online — data checked ${{readableSnapshotTime(savedAt)}}`
          : "Online";
        connectionStatus.textContent = tileErrors >= 3
          ? `${{onlineText}} · map tiles unavailable; local road map shown`
          : onlineText;
        if (tileErrors < 3) mapEl.classList.remove("using-offline-basemap");
      }} else if (state === "reconnecting") {{
        connectionStatus.textContent = savedAt
          ? `Reconnecting — showing data saved ${{readableSnapshotTime(savedAt)}}`
          : "Reconnecting — no saved incident data for this view";
      }} else if (state === "unavailable") {{
        connectionStatus.textContent = savedAt
          ? `Crestmap unavailable — showing data saved ${{readableSnapshotTime(savedAt)}}`
          : "Crestmap unavailable — no saved incident data for this view";
        mapEl.classList.add("using-offline-basemap");
      }} else {{
        connectionStatus.textContent = savedAt
          ? `Offline — showing data saved ${{readableSnapshotTime(savedAt)}}`
          : "Offline — no saved incident data for this view";
        mapEl.classList.add("using-offline-basemap");
      }}
    }}

    function setupConnectivityStatus() {{
      setConnectivityStatus(connectionStateFor({{ online: navigator.onLine }}).state);
      window.addEventListener("offline", () => setConnectivityStatus("offline"));
      window.addEventListener("online", () => {{
        setConnectivityStatus(connectionStateFor({{ online: true, checking: true, hasSnapshot: Boolean(activeSnapshotSavedAt) }}).state);
        fetchIncidentData({{ force: true, preserveViewport: true }}).catch(() => {{
          setConnectivityStatus(connectionStateFor({{ online: navigator.onLine, requestFailed: true, hasSnapshot: Boolean(activeSnapshotSavedAt) }}).state);
        }});
      }});
    }}

    function renderOfflineBasemap() {{
      L.polyline(offlineRegionOutline, {{
        pane: "offlineBasemap",
        interactive: false,
        color: "#91a295",
        weight: 1,
        opacity: 0.72,
        dashArray: "5 5"
      }}).addTo(offlineBasemapLayer);
      Object.entries(offlineRoads).forEach(([road, lines]) => {{
        lines.forEach((points, lineIndex) => {{
          const roadLine = L.polyline(points, {{
            pane: "offlineBasemap",
            interactive: false,
            color: road === "Pacific Coast Highway" || road === "crest" ? "#61786a" : "#819087",
            weight: road === "Pacific Coast Highway" || road === "crest" ? 3 : 2,
            opacity: 0.9,
            className: "offline-basemap-road"
          }}).addTo(offlineBasemapLayer);
          if (lineIndex === 0 && points.length > 2) {{
            roadLine.bindTooltip(road.replaceAll("_", " "), {{
              pane: "offlineBasemap",
              permanent: true,
              direction: "center",
              className: "offline-basemap-label",
              opacity: 0.92
            }});
          }}
        }});
      }});
    }}

    function mileMarkerStep(zoom) {{
      if (zoom <= 11) return null;
      if (zoom === 12) return 5;
      if (zoom <= 15) return 1;
      return 0;
    }}

    function sampledMileMarkers(points, step) {{
      if (!step) return points;
      const nearest = new Map();
      points.forEach((point) => {{
        const target = Math.round(Number(point[0]) / step) * step;
        const distance = Math.abs(Number(point[0]) - target);
        const previous = nearest.get(target);
        if (!previous || distance < previous.distance) nearest.set(target, {{ point, distance }});
      }});
      return Array.from(nearest.values(), (entry) => entry.point);
    }}

    function mileMarkerLabel(mile) {{
      return Number.isInteger(Number(mile)) ? String(mile) : Number(mile).toFixed(2);
    }}

    function renderMileMarkers() {{
      mileMarkerLayer.clearLayers();
      if (!mileMarkersVisible || currentRegion !== "forest") return;
      const step = mileMarkerStep(map.getZoom());
      if (step === null) return;
      Object.entries(roadwayMileMarkers).forEach(([road, points]) => {{
        sampledMileMarkers(points, step).forEach(([mile, latitude, longitude]) => {{
          const label = mileMarkerLabel(mile);
          const roadLabel = {{
            crest: "ACH",
            forest: "AFH",
            big_tujunga: "BT",
            upper_big_tujunga: "UBT",
            glendora_mountain: "GMR",
            glendora_ridge: "GRR",
            highway_39: "SR39",
            mount_baldy: "MB"
          }}[road] || road.toUpperCase();
          L.marker([latitude, longitude], {{
            pane: "mileMarkers",
            interactive: false,
            keyboard: false,
            icon: L.divIcon({{
              className: `mile-marker is-${{road}}`,
              iconSize: [1, 1],
              iconAnchor: [0, 0],
              html: `<span class="mile-marker-content" aria-hidden="true">${{roadLabel}} ${{label}}</span>`
            }})
          }}).addTo(mileMarkerLayer);
        }});
      }});
    }}

    function setupMileMarkerLayer() {{
      const button = document.querySelector("[data-mile-markers-toggle]");
      if (currentRegion !== "forest" || !button) return;
      const updateButton = () => {{
        button.classList.toggle("is-active", mileMarkersVisible);
        button.setAttribute("aria-pressed", mileMarkersVisible ? "true" : "false");
        const description = button.querySelector(".view-menu-description");
        if (description) description.textContent = mileMarkersVisible ? "More detail as you zoom" : "Hidden";
      }};
      updateButton();
      renderMileMarkers();
      map.on("zoomend", renderMileMarkers);
      button.addEventListener("click", () => {{
        mileMarkersVisible = !mileMarkersVisible;
        window.localStorage.setItem("crestmap-mile-markers", mileMarkersVisible ? "shown" : "hidden");
        updateButton();
        renderMileMarkers();
      }});
    }}

    function setupIncidentLayer() {{
      const button = document.querySelector("[data-incident-layer-toggle]");
      if (!button) return;
      const updateButton = () => {{
        button.classList.toggle("is-active", incidentLayerVisible);
        button.setAttribute("aria-pressed", String(incidentLayerVisible));
        const description = button.querySelector(".view-menu-description");
        if (description) description.textContent = incidentLayerVisible ? "Map pins" : "Hidden from map";
      }};
      const updateMarkers = () => {{
        markers.forEach((marker, eventKey) => {{
          if (incidentLayerVisible || eventKey === revealedIncidentKey) marker.addTo(map);
          else marker.remove();
        }});
      }};
      updateButton();
      button.addEventListener("click", () => {{
        incidentLayerVisible = !incidentLayerVisible;
        revealedIncidentKey = null;
        window.localStorage.setItem("crestmap-incident-layer", incidentLayerVisible ? "shown" : "hidden");
        updateButton();
        updateMarkers();
      }});
    }}

    function setupMapLayerMenu() {{
      const menu = document.querySelector(".map-layer-menu");
      if (!menu) return;
      document.addEventListener("pointerdown", (event) => {{
        if (menu.open && !menu.contains(event.target)) menu.removeAttribute("open");
      }}, true);
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape" && menu.open) {{
          menu.removeAttribute("open");
          menu.querySelector("summary")?.focus();
        }}
      }});
    }}

    function setupUserLocation() {{
      const button = document.getElementById("locate-user");
      const statusElement = document.getElementById("location-status");
      if (!button || !statusElement) return;
      L.DomEvent.disableClickPropagation(button);

      const setStatus = (message) => {{
        statusElement.textContent = message;
        window.clearTimeout(window.crestmapLocationStatusTimer);
        if (message) {{
          window.crestmapLocationStatusTimer = window.setTimeout(() => {{
            statusElement.textContent = "";
          }}, 5000);
        }}
      }};

      if (!navigator.geolocation) {{
        button.disabled = true;
        button.title = "Location is not supported by this browser";
        return;
      }}

      const updateLocationLayers = (coordinates, accuracy) => {{
        if (accuracy > 0 && accuracy <= 5000) {{
          if (userAccuracyCircle) {{
            userAccuracyCircle.setLatLng(coordinates);
            userAccuracyCircle.setRadius(accuracy);
          }} else {{
            userAccuracyCircle = L.circle(coordinates, {{
              radius: accuracy,
              color: "#2878e6",
              weight: 1,
              opacity: 0.42,
              fillColor: "#5c9cef",
              fillOpacity: 0.1,
              interactive: false
            }}).addTo(map);
          }}
        }} else if (userAccuracyCircle) {{
          userAccuracyCircle.remove();
          userAccuracyCircle = null;
        }}

        if (userLocationMarker) {{
          userLocationMarker.setLatLng(coordinates);
        }} else {{
          userLocationMarker = L.marker(coordinates, {{
            pane: "userLocation",
            interactive: false,
            keyboard: false,
            zIndexOffset: 750,
            icon: L.divIcon({{
              className: "user-location-marker",
              iconSize: [18, 18],
              iconAnchor: [9, 9],
              html: '<span class="visually-hidden">Your location</span>'
            }})
          }}).addTo(map);
        }}
      }};

      const handlePosition = (position) => {{
        const latitude = Number(position.coords.latitude);
        const longitude = Number(position.coords.longitude);
        const accuracy = Math.max(0, Number(position.coords.accuracy || 0));
        const coordinates = [latitude, longitude];
        const firstFix = !userLocationCoordinates;
        userLocationCoordinates = coordinates;
        updateLocationLayers(coordinates, accuracy);

        if (userLocationFollowing) {{
          if (firstFix || forceUserLocationRecenter) {{
            map.flyTo(coordinates, Math.max(map.getZoom(), 13), {{ duration: 0.65 }});
          }} else {{
            map.panTo(coordinates, {{ animate: true, duration: 0.35 }});
          }}
          forceUserLocationRecenter = false;
        }}

        const accuracyText = accuracy > 0 ? ` · accurate to about ${{Math.round(accuracy)}} m` : "";
        if (firstFix && userLocationRequested) setStatus(`Location tracking on${{accuracyText}}`);
        button.title = userLocationFollowing ? "Following my location" : "Recenter on my location";
        button.classList.remove("is-loading");
        button.disabled = false;
      }};

      const handleLocationError = (error) => {{
        const messages = {{
          1: "Location permission was not granted.",
          2: "Your location is currently unavailable.",
          3: "Location request timed out."
        }};
        if (userLocationRequested) {{
          setStatus(messages[error.code] || "Could not determine your location.");
        }}
        if (error.code === 1) {{
          userLocationFollowing = false;
          if (userLocationWatchId !== null) {{
            navigator.geolocation.clearWatch(userLocationWatchId);
            userLocationWatchId = null;
          }}
        }}
        button.classList.remove("is-loading");
        button.disabled = false;
      }};

      const startLocationTracking = () => {{
        userLocationRequested = true;
        userLocationFollowing = true;
        forceUserLocationRecenter = true;
        button.classList.add("is-loading");
        button.disabled = true;
        setStatus("Finding your location…");
        if (userLocationWatchId !== null && !userLocationCoordinates) {{
          navigator.geolocation.clearWatch(userLocationWatchId);
          userLocationWatchId = null;
        }}
        if (userLocationWatchId === null) {{
          userLocationWatchId = navigator.geolocation.watchPosition(
            handlePosition,
            handleLocationError,
            {{ enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }}
          );
        }} else if (userLocationCoordinates) {{
          map.flyTo(userLocationCoordinates, Math.max(map.getZoom(), 13), {{ duration: 0.65 }});
          forceUserLocationRecenter = false;
          button.classList.remove("is-loading");
          button.disabled = false;
          button.title = "Following my location";
          setStatus("Following your location");
        }}
      }};

      button.addEventListener("click", startLocationTracking);

      if (navigator.permissions?.query) {{
        navigator.permissions.query({{ name: "geolocation" }}).then((permission) => {{
          if (permission.state !== "granted" || userLocationWatchId !== null) return;
          userLocationWatchId = navigator.geolocation.watchPosition(
            handlePosition,
            handleLocationError,
            {{ enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }}
          );
        }}).catch(() => {{
          // Permission checks are optional; the location button remains available.
        }});
      }}

      map.on("dragstart", pauseUserLocationFollowing);
      map.on("dblclick", pauseUserLocationFollowing);
      mapEl.addEventListener("wheel", pauseUserLocationFollowing, {{ passive: true }});
      mapEl.addEventListener("touchmove", pauseUserLocationFollowing, {{ passive: true }});
    }}

    function pauseUserLocationFollowing() {{
      if (!userLocationFollowing) return;
      userLocationFollowing = false;
      forceUserLocationRecenter = false;
      const button = document.getElementById("locate-user");
      if (button) button.title = "Recenter on my location";
    }}

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
          pauseUserLocationFollowing();
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
      const viewportBottom = viewport ? viewport.offsetTop + viewport.height : window.innerHeight;
      const rect = mapEl.getBoundingClientRect();
      const mapBelowViewport = Math.max(0, Math.round(rect.bottom - viewportBottom));
      // Keep the cue near the map's lower edge. iOS standalone mode can report a
      // temporarily short visual viewport, so cap how far that can lift the cue.
      const cueBottom = Math.min(98, 40 + mapBelowViewport);
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

    function formatTimeElement(element) {{
      if (!element) {{
        return;
      }}
      const dateTime = element.getAttribute("datetime");
      const date = new Date(dateTime);
      if (Number.isNaN(date.getTime())) {{
        return;
      }}
      element.textContent = date.toLocaleString([], {{
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit"
      }});
      element.title = dateTime;
    }}

    function formatGeneratedAt() {{
      formatTimeElement(document.getElementById("generated-at"));
      formatTimeElement(document.getElementById("last-scrape-at"));
    }}

    function setCheckedAt(value) {{
      const generatedAt = document.getElementById("generated-at");
      if (!generatedAt || !value) {{
        return;
      }}
      generatedAt.setAttribute("datetime", value);
      formatGeneratedAt();
    }}

    function setLastScrape(scrape) {{
      const lastScrapeAt = document.getElementById("last-scrape-at");
      if (!lastScrapeAt || !scrape || !scrape.observed_at) {{
        return;
      }}
      lastScrapeAt.setAttribute("datetime", scrape.observed_at);
      lastScrapeAt.textContent = scrape.observed_at;
      const sourceLabel = lastScrapeAt.parentElement?.querySelector(".source-label");
      if (sourceLabel && scrape.source) {{
        sourceLabel.textContent = `(${{String(scrape.source).toUpperCase()}})`;
      }}
      formatTimeElement(lastScrapeAt);
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
      let hiddenAt = document.visibilityState === "hidden" ? Date.now() : 0;
      let resumeRefreshInFlight = false;
      let lastResumeRefreshAt = 0;
      autoRefreshToggle.checked = window.localStorage.getItem("chp-auto-refresh") === "enabled";
      const refresh = () => {{
        setConnectivityStatus("reconnecting");
        fetchIncidentData({{ force: true, preserveViewport: true }}).catch(() => {{
          setConnectivityStatus(connectionStateFor({{ online: navigator.onLine, requestFailed: true, hasSnapshot: Boolean(activeSnapshotSavedAt) }}).state);
        }});
      }};
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
      const reloadForAppVersion = (latestVersion) => {{
        if (!latestVersion || latestVersion === appVersion) {{
          return false;
        }}
        const reloadKey = `crestmap-app-reload:${{latestVersion}}`;
        if (window.sessionStorage.getItem(reloadKey) === "attempted") {{
          return false;
        }}
        window.sessionStorage.setItem(reloadKey, "attempted");
        const reloadUrl = new URL(window.location.href);
        reloadUrl.searchParams.set("app_version", latestVersion);
        window.location.replace(reloadUrl.href);
        return true;
      }};
      const refreshAfterResume = async () => {{
        const now = Date.now();
        if (resumeRefreshInFlight || now - lastResumeRefreshAt < 15000) {{
          return;
        }}
        resumeRefreshInFlight = true;
        lastResumeRefreshAt = now;
        try {{
          const refreshed = await checkForUpdates({{ force: true, refreshData: true }});
          if (refreshed === false) return;
          dismissed = false;
          lastHealthyCheckAt = Date.now();
          hideNotice();
        }} catch (_error) {{
          // The normal stale checker will surface persistent connection failures.
        }} finally {{
          resumeRefreshInFlight = false;
        }}
      }};
      const checkForUpdates = async (options = {{}}) => {{
        const force = Boolean(options.force);
        if (checkInFlight) {{
          return;
        }}
        const now = Date.now();
        if (!force && now - lastCheckedAt < 30000) {{
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
          if (!response.ok) throw new Error(`status API returned ${{response.status}}`);
          const latest = await response.json();
          setConnectivityStatus("online", latest.checked_at || activeSnapshotSavedAt);
          lastHealthyCheckAt = Date.now();
          if (reloadForAppVersion(latest.app_version)) return true;
          if (latest.checked_at) {{
            setCheckedAt(latest.checked_at);
          }}
          setLastScrape(latest.last_scrape);
          updateRegionCounts(latest.region_statuses);
          if (options.refreshData || (latest.version && latest.version !== currentDataStatus.version)) {{
            if (options.refreshData || autoRefreshToggle.checked) {{
              await fetchIncidentData({{ force: true, preserveViewport: true, status: latest }});
              return true;
            }}
            if (!dismissed) {{
              showNotice("New incident data is available.");
            }}
          }} else {{
            hideNotice();
          }}
          return true;
        }} catch (_error) {{
          setConnectivityStatus(connectionStateFor({{ online: navigator.onLine, requestFailed: true, hasSnapshot: Boolean(activeSnapshotSavedAt) }}).state);
          return false;
        }} finally {{
          checkInFlight = false;
        }}
      }};
      const update = () => {{
        const now = Date.now();
        const pageAgeMs = now - generatedTime;
        const healthAgeMs = now - lastHealthyCheckAt;
        if (pageAgeMs > 60000 && document.visibilityState === "visible") {{
          checkForUpdates();
        }}
        if (!dismissed && healthAgeMs > 180000 &&
            connectionStatus?.dataset.state === "online" &&
            !notice.classList.contains("is-visible")) {{
          showNotice("Data may be stale. Background status checks are not confirming current data.");
        }}
      }};
      update();
      window.setInterval(update, 15000);
      document.addEventListener("visibilitychange", () => {{
        if (document.visibilityState === "hidden") {{
          hiddenAt = Date.now();
          return;
        }}
        if (hiddenAt) {{
          hiddenAt = 0;
          refreshAfterResume();
        }}
      }});
      window.addEventListener("pageshow", (event) => {{
        if (event.persisted) {{
          refreshAfterResume();
        }}
      }});
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

    function incidentSourceLabel(incident) {{
      return String(incident.source || "chp").toLowerCase() === "wildweb" ? "WildWeb" : "CHP";
    }}

    function incidentDescription(incident) {{
      const description = String(incident.location_desc || "").trim();
      if (!description || !description.replace(/[\\s*._-]/g, "")) {{
        return "";
      }}
      const normalize = (value) => String(value || "").trim().toLowerCase().replace(/\\s+/g, " ");
      const normalized = normalize(description);
      return [incident.type, incident.location].some((value) => normalize(value) === normalized)
        ? ""
        : description;
    }}

    function incidentLocationLines(incident) {{
      const location = String(incident.location || "").trim();
      const description = incidentDescription(incident);
      const isWildWeb = String(incident.source || "chp").toLowerCase() === "wildweb";
      if (isWildWeb && description) {{
        return {{ primary: description, secondary: location }};
      }}
      return {{
        primary: location || description,
        secondary: location ? description : ""
      }};
    }}

    function incidentStatusLabel(incident) {{
      const source = String(incident.source || "chp").toLowerCase();
      const status = String(incident.status || "").toLowerCase();
      const sourceStatus = String(incident.source_status || "").toLowerCase();
      if (source === "wildweb") {{
        return {{
          listed: "Reported",
          contained: "Contained",
          controlled: "Controlled",
          out: "Out",
          no_longer_listed: "No longer listed",
          aged_out: "Archived"
        }}[sourceStatus] || (status === "reported" ? "Reported" : "Archived");
      }}
      return status === "active" ? "Active" : "Cleared";
    }}

    function incidentStatusClass(incident) {{
      const status = String(incident.status || "").toLowerCase();
      return status === "active" ? "status-active" : status === "reported" ? "status-reported" : "status-cleared";
    }}

    function wildWebReportedVisualAge(incident) {{
      const source = String(incident.source || "").toLowerCase();
      const status = String(incident.status || "").toLowerCase();
      if (source !== "wildweb" || status !== "reported") {{
        return null;
      }}
      const reportedAt = Date.parse(String(incident.source_reported_at || incident.first_seen || ""));
      if (!Number.isFinite(reportedAt)) {{
        return {{ saturation: 1 }};
      }}
      const ageHours = Math.max(0, (Date.now() - reportedAt) / 3600000);
      const fadeProgress = Math.min(1, Math.max(0, (ageHours - 1) / 5));
      return {{
        saturation: 1 - fadeProgress
      }};
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
      url.searchParams.delete("camera");
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
      url.searchParams.delete("camera");
      ["nocache", "verify", "align", "details", "tapcheck", "markertouch", "statusapi"].forEach((key) => {{
        url.searchParams.delete(key);
      }});
      return url;
    }}

    async function copyIncidentLink(incident, button) {{
      const link = incidentUrl(incident).toString();
      try {{
        await navigator.clipboard.writeText(link);
        window.crestmapTrack?.("share", {{ method: "copy_link", content_type: "incident" }});
        button.textContent = "Copied";
        window.setTimeout(() => {{
          button.textContent = "Copy link";
        }}, 1800);
      }} catch (_error) {{
        window.prompt("Copy incident link", link);
      }}
    }}

    function commentsEndpoint(incident) {{
      return `${{commentsBaseEndpoint}}/${{encodeURIComponent(incident.event_key)}}/comments`;
    }}

    function formatCommentDate(value) {{
      if (!value) {{
        return "";
      }}
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) {{
        return value;
      }}
      return parsed.toLocaleString([], {{ month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }});
    }}

    function renderComments(container, comments) {{
      if (!container) {{
        return;
      }}
      if (!comments.length) {{
        container.innerHTML = '<div class="empty">No approved comments yet.</div>';
        return;
      }}
      container.innerHTML = `
        <div class="comments-list">
          ${{comments.map((comment) => `
            <article class="comment">
              <div class="comment-meta">${{escapeHtml(comment.display_name || "Anonymous")}} · ${{escapeHtml(formatCommentDate(comment.created_at))}}</div>
              <div class="comment-body">${{escapeHtml(comment.body || "")}}</div>
              ${{comment.media?.length ? `<div class="comment-media">${{comment.media.map((item) =>
                item.kind === "video"
                  ? `<video src="${{escapeHtml(item.url)}}" controls preload="metadata" playsinline></video>`
                  : `<img src="${{escapeHtml(item.url)}}" alt="Submitted incident photo" loading="lazy">`
              ).join("")}}</div>` : ""}}
            </article>
          `).join("")}}
        </div>
      `;
    }}

    function videoMetadata(file) {{
      return new Promise((resolve, reject) => {{
        const video = document.createElement("video");
        const url = URL.createObjectURL(file);
        video.preload = "metadata";
        video.onloadedmetadata = () => {{
          const duration = video.duration;
          URL.revokeObjectURL(url);
          Number.isFinite(duration) ? resolve(duration) : reject(new Error("Could not read video duration."));
        }};
        video.onerror = () => {{
          URL.revokeObjectURL(url);
          reject(new Error("Use a browser-compatible H.264/AAC MP4 video."));
        }};
        video.src = url;
      }});
    }}

    async function compressImage(file) {{
      const bitmap = await createImageBitmap(file);
      const scale = Math.min(1, 1920 / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      bitmap.close();
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/webp", 0.82));
      if (!blob) {{
        throw new Error("Photo compression failed.");
      }}
      const stem = file.name.replace(/\\.[^.]+$/, "") || "photo";
      return {{ blob, filename: `${{stem}}.webp`, contentType: "image/webp", durationSeconds: null }};
    }}

    async function prepareMediaFiles(input) {{
      const files = Array.from(input?.files || []);
      if (!files.length) {{
        return [];
      }}
      const videos = files.filter((file) => file.type === "video/mp4");
      if ((videos.length && files.length !== 1) || videos.length > 1) {{
        throw new Error("Choose up to 3 photos, or 1 MP4 video by itself.");
      }}
      if (!videos.length && files.length > 3) {{
        throw new Error("Choose no more than 3 photos.");
      }}
      if (videos.length) {{
        const file = videos[0];
        if (file.size > mediaMaxVideoBytes) {{
          throw new Error(`Video must be ${{Math.round(mediaMaxVideoBytes / (1024 * 1024))}} MB or smaller.`);
        }}
        const duration = await videoMetadata(file);
        if (duration > mediaMaxVideoSeconds) {{
          throw new Error(`Video must be ${{mediaMaxVideoSeconds}} seconds or shorter.`);
        }}
        return [{{ blob: file, filename: file.name, contentType: "video/mp4", durationSeconds: duration }}];
      }}
      if (files.some((file) => !["image/jpeg", "image/png", "image/webp"].includes(file.type))) {{
        throw new Error("Use JPEG, PNG, WebP, or MP4 files.");
      }}
      return Promise.all(files.map(compressImage));
    }}

    async function uploadCommentMedia(incident, comment, files, status) {{
      for (let index = 0; index < files.length; index += 1) {{
        const file = files[index];
        status.textContent = `Uploading ${{index + 1}} of ${{files.length}}...`;
        const createResponse = await fetch(
          `${{commentsBaseEndpoint}}/${{encodeURIComponent(incident.event_key)}}/media/uploads`,
          {{
            method: "POST",
            cache: "no-store",
            headers: {{ "Accept": "application/json", "Content-Type": "application/json" }},
            body: JSON.stringify({{
              comment_id: comment.id,
              upload_token: comment.upload_token,
              filename: file.filename,
              content_type: file.contentType,
              size: file.blob.size,
              duration_seconds: file.durationSeconds
            }})
          }}
        );
        const upload = await createResponse.json().catch(() => ({{}}));
        if (!createResponse.ok) {{
          throw new Error(upload.error?.message || "Could not prepare media upload.");
        }}
        const putResponse = await fetch(upload.upload_url, {{
          method: upload.method || "PUT",
          headers: upload.headers || {{}},
          body: file.blob
        }});
        if (!putResponse.ok) {{
          throw new Error(`Media upload failed (${{putResponse.status}}).`);
        }}
        const finalizeResponse = await fetch(
          `${{commentsBaseEndpoint}}/${{encodeURIComponent(incident.event_key)}}/media/${{upload.id}}/finalize`,
          {{
            method: "POST",
            cache: "no-store",
            headers: {{ "Accept": "application/json", "Content-Type": "application/json" }},
            body: JSON.stringify({{ comment_id: comment.id, upload_token: comment.upload_token }})
          }}
        );
        if (!finalizeResponse.ok) {{
          const finalized = await finalizeResponse.json().catch(() => ({{}}));
          throw new Error(finalized.error?.message || "Could not finish media upload.");
        }}
      }}
    }}

    async function loadComments(incident) {{
      const container = detailsPanel.querySelector(`[data-comments-for="${{CSS.escape(incident.event_key)}}"]`);
      if (!container) {{
        return;
      }}
      try {{
        const response = await fetch(commentsEndpoint(incident), {{
          cache: "no-store",
          headers: {{ "Accept": "application/json" }}
        }});
        if (!response.ok) {{
          throw new Error(`comments API returned ${{response.status}}`);
        }}
        const payload = await response.json();
        renderComments(container, payload.data || []);
      }} catch (_error) {{
        container.innerHTML = '<div class="empty">Comments could not be loaded.</div>';
      }}
    }}

    function focusedCommentFormFor(incident) {{
      const activeElement = document.activeElement;
      if (!activeElement || !detailsPanel.contains(activeElement)) {{
        return null;
      }}
      const form = activeElement.closest?.("[data-comment-form]");
      if (!form || form.dataset.commentForm !== incident.event_key) {{
        return null;
      }}
      return form;
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
      const markerState = incident.status === "active" ? "is-active" : incident.status === "reported" ? "is-reported" : "is-cleared";
      const sourceStatus = String(incident.source_status || "").toLowerCase();
      const visualAge = wildWebReportedVisualAge(incident);
      const wildwebEndState = String(incident.source || "").toLowerCase() === "wildweb" && markerState === "is-cleared"
        ? {{ aged_out: "is-wildweb-aged-out", no_longer_listed: "is-wildweb-no-longer-listed" }}[sourceStatus] || ""
        : "";
      const size = 22;
      return L.divIcon({{
        className: [
          "incident-marker",
          markerState,
          visualAge ? "is-wildweb-aging" : "",
          wildwebEndState,
          selected ? "is-selected" : "",
          pulsing ? "is-pulsing" : ""
        ].join(" "),
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
        html: `<span class="incident-marker-dot" aria-hidden="true"${{visualAge ? ` style="--incident-age-saturation: ${{visualAge.saturation.toFixed(3)}}"` : ""}}><span class="incident-marker-core"></span></span>`
      }});
    }}

    function normalizedBearing(value) {{
      const bearing = Number(value);
      return Number.isFinite(bearing) ? ((bearing % 360) + 360) % 360 : 0;
    }}

    function cameraDirectionLabel(value) {{
      const bearing = normalizedBearing(value);
      const labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
      return `${{labels[Math.round(bearing / 45) % labels.length]}} · ${{Math.round(bearing)}}°`;
    }}

    function cameraIsOnline(camera) {{
      const lastFrame = Number(camera.last_frame_ts || 0);
      if (!lastFrame) return false;
      return (Date.now() / 1000) - lastFrame < 15 * 60;
    }}

    function cameraIcon(camera, selected = false) {{
      const size = 30;
      const bearing = normalizedBearing(camera.az_current);
      const displayOffsetX = Number(camera.display_offset_x || 0);
      return L.divIcon({{
        className: `camera-marker${{cameraIsOnline(camera) ? "" : " is-offline"}}${{selected ? " is-selected" : ""}}`,
        iconSize: [size, size],
        iconAnchor: [size / 2 - displayOffsetX, size / 2],
        html: `<span class="camera-marker-symbol" style="--camera-bearing: ${{bearing}}deg" aria-hidden="true">
          <svg viewBox="0 0 30 30" focusable="false">
            <g class="camera-marker-bearing"><path class="camera-marker-arrow" d="M15 1.5 24.5 25 15 20.7 5.5 25Z"></path></g>
            <circle class="camera-marker-center" cx="15" cy="15" r="3.1"></circle>
          </svg>
        </span>`
      }});
    }}

    function destinationPoint(latitude, longitude, bearing, distanceMeters) {{
      const radius = 6371000;
      const angularDistance = distanceMeters / radius;
      const bearingRadians = normalizedBearing(bearing) * Math.PI / 180;
      const latitudeRadians = latitude * Math.PI / 180;
      const longitudeRadians = longitude * Math.PI / 180;
      const destinationLatitude = Math.asin(
        Math.sin(latitudeRadians) * Math.cos(angularDistance) +
        Math.cos(latitudeRadians) * Math.sin(angularDistance) * Math.cos(bearingRadians)
      );
      const destinationLongitude = longitudeRadians + Math.atan2(
        Math.sin(bearingRadians) * Math.sin(angularDistance) * Math.cos(latitudeRadians),
        Math.cos(angularDistance) - Math.sin(latitudeRadians) * Math.sin(destinationLatitude)
      );
      return [destinationLatitude * 180 / Math.PI, destinationLongitude * 180 / Math.PI];
    }}

    function cameraFovRadius() {{
      return Math.max(1200, 10000 / Math.pow(2, Math.max(0, map.getZoom() - 10)));
    }}

    function cameraDisplayLatLng(camera) {{
      const point = map.project([camera.latitude, camera.longitude], map.getZoom());
      point.x += Number(camera.display_offset_x || 0);
      return map.unproject(point, map.getZoom());
    }}

    function cameraSectorLatLngs(camera) {{
      const bearing = normalizedBearing(camera.az_current);
      const fieldOfView = Math.min(100, Math.max(15, Number(camera.fov) || 62.8));
      const start = bearing - fieldOfView / 2;
      const displayPosition = cameraDisplayLatLng(camera);
      const points = [[displayPosition.lat, displayPosition.lng]];
      for (let step = 0; step <= 12; step += 1) {{
        points.push(destinationPoint(
          displayPosition.lat,
          displayPosition.lng,
          start + fieldOfView * step / 12,
          cameraFovRadius()
        ));
      }}
      points.push([displayPosition.lat, displayPosition.lng]);
      return points;
    }}

    function renderSelectedCameraFov() {{
      cameraFovLayer.clearLayers();
      if (!cameraLayerVisible || !selectedCamera) return;
      L.polygon(cameraSectorLatLngs(selectedCamera), {{
        pane: "cameraFov",
        color: "#2477a6",
        weight: 2,
        opacity: 0.78,
        fillColor: "#49a0cc",
        fillOpacity: 0.2,
        interactive: false,
        lineJoin: "round"
      }}).addTo(cameraFovLayer);
    }}

    function cameraImageUrl(camera) {{
      return `${{cameraDataBase}}/${{encodeURIComponent(camera.id)}}/latest-frame.jpg?rqts=${{Math.floor(Date.now() / 1000)}}`;
    }}

    function cameraViewerUrl(camera) {{
      const url = new URL(cameraViewerBase);
      url.searchParams.set("id", camera.id);
      return url.toString();
    }}

    function formatCameraUpdatedAt(camera) {{
      const date = new Date(Number(camera.last_frame_ts || 0) * 1000);
      if (!Number(camera.last_frame_ts) || Number.isNaN(date.getTime())) return "No recent image";
      return date.toLocaleString([], {{ month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" }});
    }}

    function closeCameraLightbox() {{
      if (!cameraLightbox || cameraLightbox.hidden) return;
      cameraLightbox.hidden = true;
      delete cameraLightbox.dataset.cameraId;
      document.body.classList.remove("camera-lightbox-open");
      if (appShell) appShell.inert = false;
      const imageLink = detailsPanel.querySelector("[data-camera-image-link]");
      if (imageLink) imageLink.focus({{ preventScroll: true }});
    }}

    function openCameraLightbox(camera, imageUrl) {{
      if (!cameraLightbox || !cameraLightboxImage || !camera) return;
      cameraLightbox.dataset.cameraId = camera.id;
      cameraLightboxImage.src = imageUrl;
      cameraLightboxImage.alt = `Full-size current view from ${{camera.name || "ALERTCalifornia camera"}}`;
      if (cameraLightboxTitle) cameraLightboxTitle.textContent = camera.name || "Camera view";
      cameraLightbox.hidden = false;
      window.crestmapTrack?.("camera_image_open", {{ camera_status: cameraIsOnline(camera) ? "online" : "offline" }});
      document.body.classList.add("camera-lightbox-open");
      if (appShell) appShell.inert = true;
      cameraLightboxClose?.focus({{ preventScroll: true }});
    }}

    function bindCameraImageLightbox(camera) {{
      const imageLink = detailsPanel.querySelector("[data-camera-image-link]");
      if (!imageLink) return;
      imageLink.addEventListener("click", (event) => {{
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        openCameraLightbox(camera, imageLink.href);
      }});
    }}

    function cameraDetailHtml(camera) {{
      const online = cameraIsOnline(camera);
      const bearing = normalizedBearing(camera.az_current);
      const fieldOfView = Number(camera.fov);
      const elevation = Number(camera.elevation);
      return `
        <div class="detail-panel camera-detail-panel">
          <div class="detail-header">
            <div class="detail-title">
              <div class="status-pill ${{online ? "status-reported" : "status-cleared"}}">${{online ? "Live camera" : "Camera offline"}}</div>
              <div class="source-pill">ALERTCalifornia</div>
              <h2>${{escapeHtml(camera.name || "Camera")}}</h2>
              <div class="meta">${{escapeHtml(currentRegion === "malibu" ? "Malibu area" : "Forest area")}}</div>
            </div>
          </div>
          ${{online ? `<a class="camera-image-link" data-camera-image-link href="${{escapeHtml(cameraImageUrl(camera))}}" rel="noopener" target="_blank" aria-label="Open full-size current view from ${{escapeHtml(camera.name || "ALERTCalifornia camera")}}" title="Open full-size image"><img class="camera-image" data-camera-image src="${{escapeHtml(cameraImageUrl(camera))}}" alt="Current view from ${{escapeHtml(camera.name || "ALERTCalifornia camera")}}"></a>` : ""}}
          <div class="empty" data-camera-image-error${{online ? " hidden" : ""}}>Current camera image is unavailable.</div>
          <div class="camera-image-meta"><span>Updated ${{escapeHtml(formatCameraUpdatedAt(camera))}}</span><span>${{online ? "Refreshes every 10s" : "Offline"}}</span></div>
          <p class="camera-credit">ALERTCalifornia | UC San Diego</p>
          <section class="detail-section">
            <dl class="detail-grid">
              <dt>Direction</dt><dd><span class="camera-direction"><svg viewBox="0 0 16 16" style="--camera-bearing: ${{bearing}}deg" aria-hidden="true"><path d="M8 1 13 14 8 11.5 3 14Z"></path></svg>${{escapeHtml(cameraDirectionLabel(bearing))}}</span></dd>
              <dt>Field of view</dt><dd>${{Number.isFinite(fieldOfView) ? `${{fieldOfView.toFixed(1)}}°` : "Unknown"}}</dd>
              <dt>Coordinates</dt><dd>${{camera.latitude.toFixed(5)}}, ${{camera.longitude.toFixed(5)}}</dd>
              <dt>Elevation</dt><dd>${{Number.isFinite(elevation) ? `${{Math.round(elevation)}} m` : "Unknown"}}</dd>
              <dt>Source</dt><dd><a href="${{escapeHtml(cameraViewerUrl(camera))}}" rel="noopener" target="_blank">ALERTCalifornia</a></dd>
            </dl>
          </section>
          <p class="camera-credit">The blue map fan shows the camera’s current bearing and field of view. Imagery is displayed without cropping or alteration.</p>
        </div>
      `;
    }}

    function cameraUrl(camera) {{
      const url = new URL(window.location.href);
      url.searchParams.set("region", currentRegion);
      url.searchParams.set("camera", camera.id);
      url.searchParams.delete("incident");
      ["nocache", "verify", "align", "details", "tapcheck", "markertouch", "statusapi"].forEach((key) => url.searchParams.delete(key));
      return url;
    }}

    function refreshSelectedCameraImage() {{
      if (!selectedCamera || !cameraIsOnline(selectedCamera)) return;
      const image = detailsPanel.querySelector("[data-camera-image]");
      const imageLink = detailsPanel.querySelector("[data-camera-image-link]");
      const error = detailsPanel.querySelector("[data-camera-image-error]");
      if (!image) return;
      image.onerror = () => {{
        image.hidden = true;
        if (error) error.hidden = false;
      }};
      image.onload = () => {{
        image.hidden = false;
        if (error) error.hidden = true;
      }};
      const refreshedImageUrl = cameraImageUrl(selectedCamera);
      image.src = refreshedImageUrl;
      if (imageLink) imageLink.href = refreshedImageUrl;
      if (!cameraLightbox?.hidden && cameraLightbox.dataset.cameraId === selectedCamera.id && cameraLightboxImage) {{
        cameraLightboxImage.src = refreshedImageUrl;
      }}
    }}

    function stopCameraImageRefresh() {{
      if (cameraImageRefreshTimer) window.clearInterval(cameraImageRefreshTimer);
      cameraImageRefreshTimer = null;
    }}

    function clearCameraSelection() {{
      stopCameraImageRefresh();
      selectedCamera = null;
      selectedCameraId = null;
      cameraFovLayer.clearLayers();
      delete detailsPanel.dataset.selectedCameraId;
      cameraMarkers.forEach((marker, cameraId) => {{
        const camera = cameras.find((item) => item.id === cameraId);
        if (camera) marker.setIcon(cameraIcon(camera, false));
      }});
    }}

    function selectCamera(camera, options = {{}}) {{
      if (!camera) return;
      if (options.userInitiated) {{
        pauseUserLocationFollowing();
        window.crestmapTrack?.("camera_open", {{ camera_status: cameraIsOnline(camera) ? "online" : "offline" }});
      }}
      clearCameraSelection();
      selectedCamera = camera;
      selectedCameraId = camera.id;
      selectedIncidentKey = null;
      delete detailsPanel.dataset.selectedIncidentKey;
      detailsPanel.dataset.selectedCameraId = camera.id;
      detailsPanel.innerHTML = cameraDetailHtml(camera);
      bindCameraImageLightbox(camera);
      if (detailsCue) detailsCue.textContent = "Camera details below";
      document.querySelectorAll(".incident").forEach((button) => button.setAttribute("aria-current", "false"));
      markers.forEach((marker, eventKey) => {{
        const incident = incidents.find((item) => item.event_key === eventKey);
        if (incident) marker.setIcon(markerIcon(incident));
      }});
      cameraMarkers.forEach((marker, cameraId) => {{
        const markerCamera = cameras.find((item) => item.id === cameraId);
        if (markerCamera) marker.setIcon(cameraIcon(markerCamera, cameraId === camera.id));
        marker.setZIndexOffset(cameraId === camera.id ? 1200 : 500);
      }});
      renderSelectedCameraFov();
      refreshSelectedCameraImage();
      if (cameraIsOnline(camera)) cameraImageRefreshTimer = window.setInterval(refreshSelectedCameraImage, 10000);
      if (options.pan !== false) map.setView([camera.latitude, camera.longitude], Math.max(map.getZoom(), 12));
      if (options.revealDetails && window.matchMedia("(max-width: 760px)").matches) {{
        detailsPanel.scrollIntoView({{ behavior: "smooth", block: "start" }});
      }}
      if (options.updateUrl !== false && window.history?.replaceState) {{
        window.history.replaceState({{ camera: camera.id }}, "", cameraUrl(camera));
      }}
    }}

    function bindCameraMarkerInteraction(marker, camera) {{
      const selectFromMarker = (event) => {{
        if (event) L.DomEvent.stop(event);
        selectCamera(camera, {{ pan: false, revealDetails: true, userInitiated: true }});
      }};
      marker.on("click", selectFromMarker);
      marker.on("add", () => {{
        const element = marker.getElement();
        if (!element) return;
        L.DomEvent.disableClickPropagation(element);
      }});
    }}

    function cameraFromFeature(feature) {{
      const coordinates = feature?.geometry?.coordinates;
      const properties = feature?.properties || {{}};
      const longitude = Number(coordinates?.[0]);
      const latitude = Number(coordinates?.[1]);
      if (!properties.id || !properties.name || !Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
      const [latMin, latMax, lonMin, lonMax] = currentRegionBounds;
      if (latitude < latMin || latitude > latMax || longitude < lonMin || longitude > lonMax) return null;
      return {{
        id: String(properties.id),
        name: String(properties.name),
        latitude,
        longitude,
        elevation: Number(coordinates?.[2]),
        last_frame_ts: Number(properties.last_frame_ts || 0),
        az_current: normalizedBearing(properties.az_current),
        tilt_current: Number(properties.tilt_current || 0),
        fov: Number(properties.fov || 62.8)
      }};
    }}

    function offsetCollocatedCameras(cameraList) {{
      const groups = new Map();
      cameraList.forEach((camera) => {{
        const key = `${{camera.latitude.toFixed(5)}}:${{camera.longitude.toFixed(5)}}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(camera);
      }});
      groups.forEach((group) => {{
        group.forEach((camera, index) => {{
          camera.display_offset_x = (index - (group.length - 1) / 2) * 28;
        }});
      }});
      return cameraList;
    }}

    function clearCameraMarkers() {{
      cameraLayer.clearLayers();
      cameraMarkers.clear();
      cameraFovLayer.clearLayers();
    }}

    function renderCameras() {{
      clearCameraMarkers();
      if (!cameraLayerVisible) return;
      cameras.forEach((camera) => {{
        const marker = L.marker([camera.latitude, camera.longitude], {{
          pane: "cameraMarkers",
          icon: cameraIcon(camera, selectedCamera?.id === camera.id),
          keyboard: false,
          title: `${{camera.name}} · facing ${{cameraDirectionLabel(camera.az_current)}}`,
          zIndexOffset: selectedCamera?.id === camera.id ? 1200 : 500
        }}).addTo(cameraLayer);
        bindCameraMarkerInteraction(marker, camera);
        cameraMarkers.set(camera.id, marker);
      }});
      renderSelectedCameraFov();
    }}

    function updateCameraLayerButton() {{
      if (!cameraLayerToggle) return;
      cameraLayerToggle.classList.remove("is-loading");
      cameraLayerToggle.classList.toggle("is-active", cameraLayerVisible);
      cameraLayerToggle.setAttribute("aria-pressed", cameraLayerVisible ? "true" : "false");
      const action = cameraLayerVisible ? "Hide" : "Show";
      cameraLayerToggle.setAttribute("aria-label", `${{action}} ALERTCalifornia cameras`);
      cameraLayerToggle.title = `${{action}} ALERTCalifornia cameras${{cameras.length ? ` (${{cameras.length}})` : ""}}`;
      const description = cameraLayerToggle.querySelector(".view-menu-description");
      if (description) {{
        description.textContent = !cameraLayerVisible
          ? "Hidden"
          : cameraLayerLoadFailed
            ? "Unavailable"
            : cameras.length
              ? `${{cameras.length}} in area`
              : "Shown on map";
      }}
    }}

    async function fetchCameraData(options = {{}}) {{
      if (!cameraLayerVisible || cameraDataFetchInFlight) return;
      cameraDataFetchInFlight = true;
      cameraLayerToggle?.classList.add("is-loading");
      cameraLayerLoadFailed = false;
      const cameraToRestoreId = selectedCamera?.id || selectedCameraId ||
        new URLSearchParams(window.location.search).get("camera");
      try {{
        const response = await fetch(cameraMetadataEndpoint, {{
          cache: "no-store",
          headers: {{ "Accept": "application/json" }}
        }});
        if (!response.ok) throw new Error(`camera API returned ${{response.status}}`);
        const payload = await response.json();
        cameras = offsetCollocatedCameras(
          (payload.features || []).map(cameraFromFeature).filter(Boolean)
            .sort((left, right) => left.name.localeCompare(right.name))
        );
        cameraDataFetchedAt = Date.now();
        window.chpLiveMap.cameras = cameras;
        renderCameras();
        const cameraToRestore = cameras.find((camera) => camera.id === cameraToRestoreId);
        if (cameraToRestore) {{
          const selectionOptions = {{ updateUrl: false }};
          if (options.preserveViewport) selectionOptions.pan = false;
          selectCamera(cameraToRestore, selectionOptions);
        }}
      }} catch (_error) {{
        cameraLayerLoadFailed = true;
        if (!cameras.length) clearCameraMarkers();
      }} finally {{
        cameraDataFetchInFlight = false;
        updateCameraLayerButton();
      }}
    }}

    function refreshCameraDataIfStale() {{
      if (!cameraLayerVisible || document.visibilityState === "hidden") return;
      if (!cameraDataFetchedAt || Date.now() - cameraDataFetchedAt >= cameraMetadataRefreshMs) {{
        fetchCameraData({{ preserveViewport: true }});
      }}
    }}

    function setupCameraLayer() {{
      if (!cameraLayerToggle) return;
      cameraLightboxClose?.addEventListener("click", closeCameraLightbox);
      cameraLightbox?.addEventListener("click", (event) => {{
        if (event.target === cameraLightbox || event.target.classList.contains("camera-lightbox-stage")) {{
          closeCameraLightbox();
        }}
      }});
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape" && !cameraLightbox?.hidden) closeCameraLightbox();
      }});
      L.DomEvent.disableClickPropagation(cameraLayerToggle);
      updateCameraLayerButton();
      map.on("zoomend", renderSelectedCameraFov);
      cameraLayerToggle.addEventListener("click", () => {{
        cameraLayerVisible = !cameraLayerVisible;
        window.localStorage.setItem("crestmap-camera-layer", cameraLayerVisible ? "shown" : "hidden");
        if (!cameraLayerVisible) {{
          clearCameraSelection();
          clearCameraMarkers();
          const incident = incidents.find((item) => item.event_key === selectedIncidentKey) || incidents[0];
          if (incident) selectIncident(incident, {{ pan: false }});
          else showDefaultView();
        }} else if (cameras.length) {{
          renderCameras();
          refreshCameraDataIfStale();
        }} else {{
          fetchCameraData();
        }}
        updateCameraLayerButton();
      }});
      window.setInterval(refreshCameraDataIfStale, cameraMetadataRefreshMs);
      document.addEventListener("visibilitychange", () => {{
        if (document.visibilityState === "visible") refreshCameraDataIfStale();
      }});
      window.addEventListener("focus", refreshCameraDataIfStale);
      if (cameraLayerVisible) fetchCameraData();
    }}

    function aircraftIcon(aircraft) {{
      const size = 25;
      return L.divIcon({{
        className: `aircraft-marker${{Number(aircraft.age_seconds || 0) > 300 ? " is-stale" : ""}}`,
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
        html: `<svg viewBox="0 0 32 24" aria-hidden="true" focusable="false">
          <path d="M4 2.5h23" fill="none" stroke-width="2.4" />
          <path d="M14.5 3.5v4" fill="none" stroke-width="2.4" />
          <path d="M11 7.5h7.2c4.3 0 7.8 3.5 7.8 7.8v1.2H11a4.5 4.5 0 0 1 0-9Z" stroke="none" />
          <path d="m11 11.3-7.5-3-.8 2.1 8.3 5.2Z" stroke="none" />
          <path d="M3.2 6.7v6.2M1.2 9.8h4" fill="none" stroke-width="1.8" />
          <path d="M9.5 20.5h15M12 16.5v4M22 16.5v4" fill="none" stroke-width="2" />
          <path d="M19 9.6c2.2.3 4 2 4.7 4.1H19Z" fill="#fff4b8" stroke="none" />
        </svg>`
      }});
    }}

    function aircraftPopup(aircraft) {{
      const detail = [
        aircraft.callsign ? `Callsign ${{escapeHtml(aircraft.callsign)}}` : null,
        aircraft.altitude_ft != null ? `${{aircraft.altitude_ft.toLocaleString()}} ft` : null,
        aircraft.speed_kt != null ? `${{aircraft.speed_kt}} kt` : null,
        aircraft.heading != null ? `${{aircraft.heading}}&deg;` : null
      ].filter(Boolean).join(" · ");
      return `
        <div class="aircraft-popup-title">${{escapeHtml(aircraft.display_name || "County rescue helicopter")}}</div>
        <div>${{escapeHtml(aircraft.registration || aircraft.icao24)}} · ${{escapeHtml(aircraft.aircraft_type || "")}}</div>
        ${{detail ? `<div>${{detail}}</div>` : ""}}
        <div>Position ${{Math.max(1, Math.round(Number(aircraft.age_seconds || 0) / 60))}} min old</div>
      `;
    }}

    function clearAircraftMarkers() {{
      aircraftMarkers.forEach((marker) => marker.remove());
      aircraftMarkers.clear();
      aircraftTrails.forEach((trail) => trail.remove());
      aircraftTrails.clear();
    }}

    function renderAircraft(aircraft) {{
      clearAircraftMarkers();
      if (!aircraftLayerVisible) return;
      (aircraft || []).forEach((item) => {{
        if (item.latitude == null || item.longitude == null) return;
        const trailPoints = (item.trail || []).filter((point) =>
          Array.isArray(point) && point.length === 2 && point[0] != null && point[1] != null
        );
        if (trailPoints.length > 1) {{
          const trail = L.polyline(trailPoints, {{
            color: "#d6a000",
            weight: 3,
            opacity: 0.42,
            interactive: false,
            lineCap: "round",
            lineJoin: "round",
            smoothFactor: 1.25
          }}).addTo(map);
          aircraftTrails.set(item.icao24, trail);
        }}
        const marker = L.marker([item.latitude, item.longitude], {{
          icon: aircraftIcon(item),
          keyboard: false,
          title: `${{item.display_name || "County rescue helicopter"}} ${{item.registration || ""}}`.trim(),
          zIndexOffset: 1000
        }}).addTo(map);
        marker.bindPopup(aircraftPopup(item), {{
          className: "aircraft-map-popup",
          closeButton: true,
          maxWidth: 230
        }});
        aircraftMarkers.set(item.icao24, marker);
      }});
    }}

    async function fetchAircraftData() {{
      if (!aircraftTrackingEnabled || document.visibilityState !== "visible") return;
      try {{
        const response = await fetch(aircraftEndpoint, {{
          cache: "no-store",
          headers: {{ "Accept": "application/json" }}
        }});
        if (!response.ok) return;
        const payload = await response.json();
        renderAircraft(payload.aircraft || []);
      }} catch (_error) {{
        // Keep the last visible position during transient API failures.
      }}
    }}

    function setupAircraftLayer() {{
      const button = document.querySelector("[data-aircraft-layer-toggle]");
      if (!aircraftTrackingEnabled || !button) return;
      const updateButton = () => {{
        button.classList.toggle("is-active", aircraftLayerVisible);
        button.setAttribute("aria-pressed", aircraftLayerVisible ? "true" : "false");
        const description = button.querySelector(".view-menu-description");
        if (description) description.textContent = aircraftLayerVisible ? "Shown when airborne" : "Hidden";
      }};
      updateButton();
      button.addEventListener("click", () => {{
        aircraftLayerVisible = !aircraftLayerVisible;
        window.localStorage.setItem("crestmap-aircraft-layer", aircraftLayerVisible ? "shown" : "hidden");
        updateButton();
        if (aircraftLayerVisible) fetchAircraftData();
        else clearAircraftMarkers();
      }});
      fetchAircraftData();
      window.setInterval(fetchAircraftData, 30000);
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
        selectIncident(incident, {{ pan: false, revealDetails: true, pulse: true, userInitiated: true }});
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

    function renderDetailSections(sections, includeHistory = false) {{
      return sections.map(([section, entries]) => `
        <div class="detail-subsection">
          <h3>${{escapeHtml(section)}}</h3>
          <ol class="detail-log">
            ${{entries.map((entry) => `
              <li>
                <time>${{escapeHtml(entry.time)}} · Entry ${{escapeHtml(entry.entry_no)}}</time>
                <div>${{escapeHtml(entry.text)}}</div>
                ${{includeHistory ? `<div class="hidden-detail-note">Seen ${{escapeHtml(entry.first_seen)}} through ${{escapeHtml(entry.last_seen)}} · ${{escapeHtml(entry.snapshot_count)}} snapshot(s)</div>` : ""}}
              </li>
            `).join("")}}
          </ol>
        </div>
      `).join("");
    }}

    function detailHtml(incident) {{
      if (!incident) {{
        return '<div class="empty">Select an incident to view details.</div>';
      }}
      const statusClass = incidentStatusClass(incident);
      const statusText = incidentStatusLabel(incident);
      const sourceText = incidentSourceLabel(incident);
      const locationLines = incidentLocationLines(incident);
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
      const detailSections = Array.from(groupedDetails.entries())
        .filter(([section]) => section !== "Unit Information");
      const unitSections = Array.from(groupedDetails.entries())
        .filter(([section]) => section === "Unit Information");
      const detailEntries = renderDetailSections(detailSections);
      const unitEntries = renderDetailSections(unitSections);
      const noEntries = '<div class="empty">No detail entries captured.</div>';
      const trailingEntries = unitEntries || (!detailEntries ? noEntries : "");
      const coordText = incident.latitude == null || incident.longitude == null
        ? `<span class="mapless">No coordinates exposed by ${{escapeHtml(sourceText)}} for this incident.</span>`
        : `${{escapeHtml(incident.latitude)}}, ${{escapeHtml(incident.longitude)}}`;
      const linkedNotice = incident._linked_outside_window
        ? `<div class="empty">This linked incident is outside the selected ${{escapeHtml(currentDataStatus.hours)}}h window.</div>`
        : "";
      const defaultButton = new URLSearchParams(window.location.search).get("incident")
        ? `<button type="button" class="default-view" data-default-view>Back to ${{escapeHtml(formatRangeLabel(currentDataStatus.hours))}}</button>`
        : "";
      const hiddenDetailsButton = adminMode
        ? `<button type="button" class="hidden-details-toggle" data-hidden-details-toggle="${{escapeHtml(incident.event_key)}}" disabled>Checking hidden...</button>`
        : "";
      return `
        <div class="detail-panel">
          <div class="detail-header">
            <div class="detail-title">
              <div class="status-pill ${{statusClass}}">${{statusText}}</div>
              <div class="source-pill">${{escapeHtml(sourceText)}}</div>
              <h2>${{escapeHtml(incident.type || "Incident")}}</h2>
              ${{locationLines.primary ? `<div class="detail-location-primary">${{escapeHtml(locationLines.primary)}}</div>` : ""}}
              ${{locationLines.secondary ? `<div class="meta">${{escapeHtml(locationLines.secondary)}}</div>` : ""}}
            </div>
            <div class="detail-actions">
              ${{defaultButton}}
              ${{hiddenDetailsButton}}
              <button type="button" class="share-incident" data-share-incident="${{escapeHtml(incident.event_key)}}">Copy link</button>
            </div>
          </div>
          ${{linkedNotice}}
          <section class="detail-section">
            <dl class="detail-grid">
              <dt>Incident</dt><dd>${{escapeHtml(incident.incident_no)}}</dd>
              <dt>Reported</dt><dd>${{escapeHtml(formatIncidentWhen(incident))}}</dd>
              <dt>Source</dt><dd>${{incident.source_url ? `<a href="${{escapeHtml(incident.source_url)}}" rel="noopener" target="_blank">${{escapeHtml(sourceText)}}</a>` : escapeHtml(sourceText)}}</dd>
              <dt>Area</dt><dd>${{escapeHtml(incident.area)}}</dd>
              <dt>Coords</dt><dd>${{coordText}}</dd>
              <dt>Crestmap First Seen</dt><dd>${{escapeHtml(incident.first_seen)}}</dd>
              <dt>Crestmap Last Seen</dt><dd>${{escapeHtml(incident.last_seen)}}</dd>
              ${{incident.cleared_at ? `<dt>${{incident.source === "wildweb" && incident.source_status === "out" ? "Out" : incident.source === "wildweb" ? "Archived" : "Cleared"}}</dt><dd>${{escapeHtml(incident.cleared_at)}}</dd>` : ""}}
            </dl>
          </section>
          ${{detailEntries ? `<section class="detail-section">${{detailEntries}}</section>` : ""}}
          ${{adminMode ? '<section class="detail-section hidden-detail-section" data-hidden-details hidden><div class="empty">Loading previously seen details...</div></section>' : ""}}
          <section class="detail-section">
            <div class="detail-subsection">
              <h3>Comments</h3>
              <div data-comments-for="${{escapeHtml(incident.event_key)}}"><div class="empty">Loading comments...</div></div>
              <form class="comment-form" data-comment-form="${{escapeHtml(incident.event_key)}}">
                <div class="comment-form-row">
                  <label class="comment-field">
                    <span>Name <span class="comment-field-hint">(optional)</span></span>
                    <input name="display_name" autocomplete="name" maxlength="80" placeholder="Display name">
                  </label>
                  <label class="comment-field">
                    <span>Contact <span class="comment-field-hint">(optional, not public)</span></span>
                    <input name="contact" autocomplete="email" maxlength="200" placeholder="Email or phone">
                  </label>
                </div>
                <textarea name="body" maxlength="750" required placeholder="Add a comment"></textarea>
                {media_form_markup}
                <input class="comment-honeypot" name="website" tabindex="-1" autocomplete="off">
                <button type="submit" class="comment-submit">Post comment</button>
                <div class="comment-status" role="status"></div>
              </form>
            </div>
          </section>
          ${{trailingEntries ? `<section class="detail-section">${{trailingEntries}}</section>` : ""}}
        </div>
      `;
    }}

    async function loadHiddenDetails(incident, reveal = false) {{
      const selectedKey = detailsPanel.dataset.selectedIncidentKey;
      const hiddenButton = detailsPanel.querySelector("[data-hidden-details-toggle]");
      const container = detailsPanel.querySelector("[data-hidden-details]");
      if (!adminMode || !hiddenButton || !container || selectedKey !== incident.event_key) {{
        return;
      }}
      hiddenButton.disabled = true;
      hiddenButton.textContent = "Checking hidden...";
      try {{
        const url = `${{adminDetailsBase}}/${{encodeURIComponent(incident.event_key)}}/hidden-details?region=${{encodeURIComponent(currentRegion)}}`;
        const response = await fetch(url, {{
          cache: "no-store",
          credentials: "same-origin",
          headers: {{ "Accept": "application/json" }}
        }});
        if (!response.ok) {{
          throw new Error(`hidden details API returned ${{response.status}}`);
        }}
        const payload = await response.json();
        const entries = payload.data || [];
        if (detailsPanel.dataset.selectedIncidentKey !== incident.event_key) {{
          return;
        }}
        const grouped = new Map();
        entries.forEach((entry) => {{
          const section = entry.section || "Detail Information";
          if (!grouped.has(section)) {{
            grouped.set(section, []);
          }}
          grouped.get(section).push(entry);
        }});
        container.dataset.loaded = "true";
        container.dataset.hiddenCount = String(entries.length);
        if (!entries.length) {{
          container.innerHTML = '<div class="empty">No previously seen detail rows are hidden for this incident.</div>';
          container.hidden = true;
          hiddenButton.textContent = "No hidden details";
          hiddenButton.disabled = true;
          return;
        }}
        container.innerHTML = `<div class="hidden-detail-note">Previously seen in CHP snapshots but absent from the latest captured snapshot.</div>${{renderDetailSections(Array.from(grouped.entries()), true)}}`;
        container.hidden = !reveal;
        hiddenButton.textContent = reveal ? `Hide hidden (${{entries.length}})` : `Show hidden (${{entries.length}})`;
        hiddenButton.disabled = false;
      }} catch (_error) {{
        if (detailsPanel.dataset.selectedIncidentKey !== incident.event_key) {{
          return;
        }}
        container.innerHTML = '<div class="empty">Hidden details could not be loaded.</div>';
        container.hidden = true;
        hiddenButton.textContent = "Retry hidden check";
        hiddenButton.disabled = false;
      }}
    }}

    function selectIncident(incident, options = {{}}) {{
      if (!incident) {{
        return;
      }}
      clearCameraSelection();
      if (detailsCue) detailsCue.textContent = "Incident details below";
      if (options.userInitiated) {{
        pauseUserLocationFollowing();
        window.crestmapTrack?.("incident_select", {{ incident_source: incident.source || "chp" }});
      }}
      selectedIncidentKey = incident.event_key;
      const preserveFocusedComment = options.preserveFocusedComment === true
        && detailsPanel.dataset.selectedIncidentKey === incident.event_key
        && focusedCommentFormFor(incident);
      if (!preserveFocusedComment) {{
        detailsPanel.innerHTML = detailHtml(incident);
        detailsPanel.dataset.selectedIncidentKey = incident.event_key;
        loadComments(incident);
        if (adminMode) {{
          loadHiddenDetails(incident);
        }}
      }}
      document.querySelectorAll(".incident").forEach((button) => {{
        button.setAttribute("aria-current", button.dataset.eventKey === incident.event_key ? "true" : "false");
        if (options.revealList && button.dataset.eventKey === incident.event_key) {{
          button.scrollIntoView({{ block: "nearest" }});
        }}
      }});
      if (markers.size && !incidentLayerVisible && options.userInitiated) revealedIncidentKey = incident.event_key;
      markers.forEach((marker, eventKey) => {{
        const selected = eventKey === incident.event_key;
        const markerIncident = incidents.find((item) => item.event_key === eventKey);
        if (!markerIncident) {{
          return;
        }}
        marker.setIcon(markerIcon(markerIncident, selected, selected && options.pulse));
        marker.setZIndexOffset(selected ? 1000 : 0);
        if (!incidentLayerVisible) {{
          if (eventKey === revealedIncidentKey) marker.addTo(map);
          else marker.remove();
        }}
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

    detailsPanel.addEventListener("click", async (event) => {{
      const defaultButton = event.target.closest("[data-default-view]");
      if (defaultButton) {{
        showDefaultView();
        return;
      }}
      const hiddenButton = event.target.closest("[data-hidden-details-toggle]");
      if (hiddenButton) {{
        const incident = incidents.find((item) => item.event_key === hiddenButton.dataset.hiddenDetailsToggle);
        const container = detailsPanel.querySelector("[data-hidden-details]");
        if (!incident || !container) {{
          return;
        }}
        if (container.dataset.loaded === "true") {{
          const count = Number(container.dataset.hiddenCount || 0);
          if (!count) {{
            return;
          }}
          container.hidden = !container.hidden;
          hiddenButton.textContent = container.hidden ? `Show hidden (${{count}})` : `Hide hidden (${{count}})`;
          return;
        }}
        loadHiddenDetails(incident, true);
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

    detailsPanel.addEventListener("change", (event) => {{
      if (event.target.name !== "media_files") {{
        return;
      }}
      const preview = event.target.closest("form")?.querySelector("[data-media-preview]");
      if (!preview) {{
        return;
      }}
      preview.innerHTML = Array.from(event.target.files || []).map((file) =>
        `<div class="media-preview-item">${{escapeHtml(file.name)}}<br>${{(file.size / (1024 * 1024)).toFixed(1)}} MB</div>`
      ).join("");
    }});

    detailsPanel.addEventListener("submit", async (event) => {{
      const form = event.target.closest("[data-comment-form]");
      if (!form) {{
        return;
      }}
      event.preventDefault();
      const incident = incidents.find((item) => item.event_key === form.dataset.commentForm);
      if (!incident) {{
        return;
      }}
      const submit = form.querySelector(".comment-submit");
      const status = form.querySelector(".comment-status");
      const data = Object.fromEntries(new FormData(form).entries());
      delete data.media_files;
      submit.disabled = true;
      status.textContent = "Preparing...";
      let commentSubmitted = false;
      try {{
        const files = mediaEnabled
          ? await prepareMediaFiles(form.querySelector('input[name="media_files"]'))
          : [];
        data.media_count = files.length;
        status.textContent = "Submitting...";
        const response = await fetch(commentsEndpoint(incident), {{
          method: "POST",
          cache: "no-store",
          headers: {{
            "Accept": "application/json",
            "Content-Type": "application/json"
          }},
          body: JSON.stringify(data)
        }});
        const payload = await response.json().catch(() => ({{}}));
        if (!response.ok) {{
          throw new Error(payload.error?.message || `comment API returned ${{response.status}}`);
        }}
        commentSubmitted = true;
        if (files.length) {{
          await uploadCommentMedia(incident, payload, files, status);
        }}
        form.reset();
        const preview = form.querySelector("[data-media-preview]");
        if (preview) preview.innerHTML = "";
        status.textContent = payload.message || "Comment published.";
        if (payload.status === "approved") loadComments(incident);
      }} catch (error) {{
        status.textContent = commentSubmitted
          ? `Comment saved, but media failed: ${{error.message || "upload error"}}`
          : (error.message || "Comment could not be submitted.");
      }} finally {{
        submit.disabled = false;
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
        const reportedText = Number(status.reported_count || 0)
          ? ` · ${{status.reported_count}} WildWeb`
          : "";
        meta.textContent = `${{status.active_count}} active${{reportedText}} · ${{status.total_count}} in ${{hoursLabel}}h · ${{status.mapped_count}} mapped`;
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
        list.innerHTML = '<div class="empty">No matching incidents are currently stored.</div>';
        detailsPanel.innerHTML = '<div class="empty">No matching incidents are currently stored.</div>';
        ensureCurrentRegionUrl();
        updateListScrollCue();
        return;
      }}

      incidents.forEach((incident) => {{
        const hasCoords = incident.latitude != null && incident.longitude != null;
        const statusClass = incidentStatusClass(incident);
        const statusText = incidentStatusLabel(incident);
        const sourceText = incidentSourceLabel(incident);
        const locationLines = incidentLocationLines(incident);
        const visualAge = wildWebReportedVisualAge(incident);
        const linkedOutsideWindow = Boolean(incident._linked_outside_window);
        if (hasCoords) {{
          const marker = L.marker([incident.latitude, incident.longitude], {{
            icon: markerIcon(incident),
            keyboard: false,
            title: `${{incident.type || "Incident"}} ${{incident.location || ""}}`.trim()
          }});
          if (incidentLayerVisible || incident.event_key === revealedIncidentKey) marker.addTo(map);
          bindMarkerInteraction(marker, incident);
          markers.set(incident.event_key, marker);
        }}

        const button = document.createElement("button");
        button.className = visualAge ? "incident is-wildweb-aging" : "incident";
        button.type = "button";
        button.dataset.eventKey = incident.event_key;
        if (visualAge) {{
          button.style.setProperty("--incident-age-saturation", visualAge.saturation.toFixed(3));
        }}
        button.innerHTML = `
          <span class="incident-heading">
            <span class="status-pill ${{statusClass}}">${{statusText}}</span>
            <span class="source-pill">${{escapeHtml(sourceText)}}</span>
            <span class="selected-pill">Open</span>
            ${{linkedOutsideWindow ? '<span class="linked-pill">Linked</span>' : ""}}
          </span>
          <strong>${{escapeHtml(incident.type || "Incident")}}</strong>
          ${{locationLines.primary ? `<span class="incident-location-primary">${{escapeHtml(locationLines.primary)}}</span>` : ""}}
          ${{locationLines.secondary ? `<span class="incident-location-secondary">${{escapeHtml(locationLines.secondary)}}</span>` : ""}}
          <span>${{escapeHtml(formatIncidentWhen(incident))}} · ${{escapeHtml(incident.area)}} · #${{escapeHtml(incident.incident_no)}}${{hasCoords ? "" : " · no map pin"}}</span>
        `;
        button.addEventListener("click", () => selectIncident(incident, {{ pulse: true, userInitiated: true }}));
        list.appendChild(button);
      }});

      setTimeout(() => map.invalidateSize(), 50);
      window.requestAnimationFrame(updateListScrollCue);
      const linkedIncident = incidentFromUrl();
      const requestedCameraId = new URLSearchParams(window.location.search).get("camera");
      const linkedCamera = cameraLayerVisible
        ? cameras.find((camera) => camera.id === requestedCameraId)
        : null;
      if (linkedCamera) {{
        selectCamera(linkedCamera, {{ updateUrl: false }});
        return;
      }}
      const preservedIncident = selectedIncidentKey
        ? incidents.find((incident) => incident.event_key === selectedIncidentKey)
        : null;
      const selectedIncident = linkedIncident || preservedIncident || incidents[0];
      selectIncident(selectedIncident, {{
        pan: Boolean(linkedIncident) && !options.preserveViewport,
        revealList: Boolean(linkedIncident),
        preserveFocusedComment: Boolean(options.preserveFocusedComment),
        updateUrl: options.updateUrl !== false && !requestedCameraId
      }});
    }}

    function applyIncidentPayload(payload, options = {{}}) {{
      incidents = payload.incidents || [];
      updateSummary(payload.status || options.status || currentDataStatus, payload.region_statuses);
      if (payload.checked_at) setCheckedAt(payload.checked_at);
      setLastScrape(payload.last_scrape);
      render({{
        preserveViewport: Boolean(options.preserveViewport),
        preserveFocusedComment: true
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
      if (!Array.isArray(payload.incidents) || !payload.status) {{
        throw new Error("incident API returned an invalid snapshot");
      }}
      const context = currentSnapshotContext();
      const savedAt = payload.checked_at || new Date().toISOString();
      applyIncidentPayload(payload, options);
      activeSnapshotSavedAt = savedAt;
      saveIncidentSnapshot(payload, {{ ...context, savedAt }}).catch(() => {{}});
      setConnectivityStatus("online", savedAt);
      document.getElementById("stale-notice")?.classList.remove("is-visible");
      return payload;
    }}

    async function initializeIncidentData() {{
      const context = currentSnapshotContext();
      let snapshot = null;
      try {{
        const candidate = await readIncidentSnapshot(context);
        if (isUsableIncidentSnapshot(candidate, context)) snapshot = candidate;
      }} catch (_error) {{
        // Browsing remains available even when private storage is disabled.
      }}
      if (snapshot) {{
        activeSnapshotSavedAt = snapshot.payload.checked_at || snapshot.saved_at;
        applyIncidentPayload(snapshot.payload, {{ preserveViewport: true }});
        setConnectivityStatus(navigator.onLine ? "reconnecting" : "offline", activeSnapshotSavedAt);
      }}
      try {{
        await fetchIncidentData({{ preserveViewport: Boolean(snapshot) }});
      }} catch (_error) {{
        if (!snapshot) render();
        setConnectivityStatus(connectionStateFor({{ online: navigator.onLine, requestFailed: true, hasSnapshot: Boolean(activeSnapshotSavedAt) }}).state, activeSnapshotSavedAt);
        const noticeText = document.getElementById("stale-notice-text");
        const notice = document.getElementById("stale-notice");
        if (!snapshot && noticeText && notice) {{
          noticeText.textContent = "Incident data could not be loaded and no saved snapshot is available for this view.";
          notice.classList.add("is-visible");
        }}
      }}
    }}

    setupMapLayerMenu();
    setupIncidentLayer();
    initializeIncidentData();
    formatGeneratedAt();
    renderOfflineBasemap();
    setupConnectivityStatus();
    setupStaleRefresh();
    setupDoubleTapZoom();
    setupDetailsCuePosition();
    setupMileMarkerLayer();
    setupUserLocation();
    setupCameraLayer();
    setupAircraftLayer();
    {temperature_script(app_path(base_path, "/api/v1/temperature"))}
    {road_weather_script(app_path(base_path, "/api/v1/road-weather"))}
    list.addEventListener("scroll", updateListScrollCue, {{ passive: true }});
    scrollIncidentsButton?.addEventListener("click", scrollIncidentListDown);
    window.addEventListener("resize", updateListScrollCue);
  </script>
{push_ui_script(base_path)}
</body>
</html>
"""


def incident_road(incident):
    text = f"{incident.get('location') or ''} {incident.get('location_desc') or ''}".lower()
    region = normalize_region(incident.get("region"))
    if region == "malibu":
        if "pacific coast" in text or " pch" in f" {text}" or "sr1" in text or "sr 1" in text:
            return "Pacific Coast Hwy"
        if "malibu canyon" in text:
            return "Malibu Canyon"
        if "topanga canyon" in text:
            return "Topanga Canyon"
        if "las virgenes" in text:
            return "Las Virgenes"
        if "kanan" in text:
            return "Kanan"
        if "mulholland" in text:
            return "Mulholland"
        if "decker" in text:
            return "Decker"
        if "latigo" in text:
            return "Latigo Canyon"
        if "encinal" in text:
            return "Encinal Canyon"
        if "tuna canyon" in text:
            return "Tuna Canyon"
        if "piuma" in text:
            return "Piuma"
        if "stunt" in text:
            return "Stunt"
        if "old topanga" in text:
            return "Old Topanga"
        if "carbon canyon" in text:
            return "Carbon Canyon"
        if "trancas" in text:
            return "Trancas Canyon"
        return "Other Malibu roads"
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


def incident_type_family(incident):
    incident_type = (incident.get("type") or "").casefold()
    if "collision" in incident_type:
        return "collision"
    if "traffic hazard" in incident_type:
        return "traffic_hazard"
    if "animal" in incident_type:
        return "animal"
    if "fire" in incident_type:
        return "fire"
    return "other"


TYPE_FAMILY_OPTIONS = [
    ("family:collision", "Traffic collisions / accidents"),
    ("family:traffic_hazard", "Traffic hazards"),
    ("family:animal", "Animal hazards"),
    ("family:fire", "Fire incidents"),
    ("family:other", "Other incident types"),
]


def incident_matches_type_filter(incident, selected_type):
    selected_type = selected_type or "all"
    if selected_type == "all":
        return True
    if selected_type.startswith("family:"):
        return incident_type_family(incident) == selected_type.split(":", 1)[1]
    if selected_type.startswith("type:"):
        selected_type = selected_type.split(":", 1)[1]
    return slugify_filter(incident.get("type") or "Unknown") == selected_type


def filtered_summary_incidents(incidents, filters):
    selected_type = (filters or {}).get("type") or "all"
    return [incident for incident in incidents if incident_matches_type_filter(incident, selected_type)]


def summary_type_options(incidents):
    exact_options = [
        (f"type:{slugify_filter(label)}", label)
        for label, _count in count_by(incidents, lambda incident: incident.get("type") or "Unknown")
    ]
    return [("all", "All incident types"), *TYPE_FAMILY_OPTIONS, *exact_options]


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


def compact_chart_label_parts(label):
    parts = label.replace(",", "").split()
    if len(parts) >= 3 and parts[0][:3] in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}:
        day_name = {"Mon": "M", "Tue": "Tu", "Wed": "W", "Thu": "Th", "Fri": "F", "Sat": "Sa", "Sun": "Su"}[parts[0][:3]]
        return [day_name, parts[-1]]
    return [label]


def report_rows(counts, limit=5, compact=False):
    if not counts:
        return '<div class="empty-report">No incidents in this window.</div>'
    max_count = max(count for _label, count in counts) or 1
    rows = []
    visible_counts = counts if limit is None else counts[:limit]
    for label, count in visible_counts:
        escaped_label = html.escape(label)
        if compact:
            escaped_display_label = "".join(f"<span>{html.escape(part)}</span>" for part in compact_chart_label_parts(label))
        else:
            escaped_display_label = escaped_label
        title = html.escape(f"{label}: {count}")
        percent = 0 if count == 0 else max(8, round((count / max_count) * 100))
        zero_class = " is-zero" if count == 0 else ""
        rows.append(
            '<div class="bar-column{}" title="{}" aria-label="{}"><div class="bar" aria-hidden="true"><i style="height: {}%;"></i></div><strong>{}</strong></div>'.format(
                zero_class,
                title,
                title,
                percent,
                escaped_display_label,
            )
        )
    chart_class_name = "bar-chart bar-chart-compact" if compact else "bar-chart"
    wrap_class_name = "bar-chart-wrap bar-chart-wrap-compact" if compact else "bar-chart-wrap"
    mid_count = max_count // 2 if max_count > 1 else 0
    return (
        f'<div class="{wrap_class_name}">'
        f'<div class="bar-axis" aria-hidden="true"><span>{max_count}</span><span>{mid_count}</span><span>0</span></div>'
        f'<div class="{chart_class_name}">'
        + "".join(rows)
        + "</div></div>"
    )


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


def daily_window_dates(generated_at, hours):
    try:
        end = dt.datetime.fromisoformat(generated_at)
    except (TypeError, ValueError):
        end = dt.datetime.now().astimezone()
    day_count = max(1, math.ceil(float(hours) / 24))
    start_date = end.date() - dt.timedelta(days=day_count - 1)
    return [start_date + dt.timedelta(days=offset) for offset in range(day_count)]


def daily_label_for_date(date_value):
    return f"{date_value.strftime('%a')}, {date_value.strftime('%b')} {date_value.day}"


def daily_incident_counts(incidents, generated_at=None, hours=None):
    counts = {}
    labels = {}
    for incident in incidents:
        key, label = incident_day_key(incident)
        counts[key] = counts.get(key, 0) + 1
        labels[key] = label
    if generated_at is not None and hours is not None:
        for date_value in daily_window_dates(generated_at, hours):
            key = date_value.isoformat()
            counts.setdefault(key, 0)
            labels.setdefault(key, daily_label_for_date(date_value))
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
    extra_params=None,
    admin_mode=False,
    google_analytics_id=None,
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
    description = f"Summary and history views for Crestmap {label.lower()} incidents."
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
{analytics_script(google_analytics_id, region, current, admin_mode)}\
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - Crestmap {html.escape(label)} Incidents</title>
  <meta name="description" content="{html.escape(description)}">
  <meta name="robots" content="index,follow,max-image-preview:large">
  <link rel="canonical" href="{html.escape(urls["canonical"])}">
  <link rel="icon" href="{html.escape(urls["favicon"])}" type="image/svg+xml">
{pwa_head_html(base_path)}
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
      position: fixed;
      top: 36px;
      right: 0;
      z-index: 10;
      width: min(270px, calc(100vw - 24px));
      box-sizing: border-box;
      max-height: calc(100svh - 64px);
      overflow-y: auto;
      overscroll-behavior: contain;
      touch-action: pan-y;
      -webkit-overflow-scrolling: touch;
      padding: 6px;
      padding-bottom: max(6px, env(safe-area-inset-bottom));
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
      width: 100%;
      border: 0;
      background: transparent;
      font-family: inherit;
      text-align: left;
      cursor: pointer;
      box-sizing: border-box;
    }}
    .view-menu-row .view-menu-label {{
      flex: 0 0 auto;
      color: inherit;
      font-size: 13px;
      font-weight: 800;
    }}
    .view-menu-row .view-menu-description {{
      min-width: 0;
      max-width: 65%;
      color: #46534b;
      font-size: 12px;
      font-weight: 700;
      line-height: 1.2;
      text-align: right;
      white-space: normal;
      overflow-wrap: anywhere;
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
    .bar-chart-wrap {{
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr);
      gap: 8px;
      align-items: start;
      min-height: 190px;
    }}
    .bar-axis {{
      display: grid;
      grid-template-rows: auto 1fr auto 1fr auto;
      height: 120px;
      color: #58645d;
      font-size: 10px;
      font-weight: 800;
      line-height: 1;
      text-align: right;
    }}
    .bar-axis span:nth-child(2) {{
      grid-row: 3;
    }}
    .bar-axis span:nth-child(3) {{
      grid-row: 5;
    }}
    .bar-chart {{
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: 58px;
      gap: 10px;
      align-items: start;
      justify-content: start;
      overflow-x: auto;
      padding: 2px 2px 8px;
      scrollbar-width: thin;
    }}
    .bar-column {{
      display: grid;
      grid-template-rows: 120px auto;
      gap: 6px;
      align-items: start;
      min-width: 58px;
      color: #405047;
      font-size: 12px;
      text-align: center;
    }}
    .bar-column strong {{
      align-self: start;
      color: #182026;
      font-size: 11px;
      font-weight: 800;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }}
    .bar {{
      display: flex;
      align-items: end;
      justify-content: center;
      height: 120px;
      width: 32px;
      margin: 0 auto;
      overflow: hidden;
      border-radius: 6px 6px 3px 3px;
      background: #e5eae3;
    }}
    .bar i {{
      display: block;
      width: 100%;
      min-height: 3px;
      border-radius: inherit;
      background: #277447;
    }}
    .bar-column.is-zero .bar i {{
      min-height: 0;
    }}
    .bar-chart-wrap-compact {{
      grid-template-columns: 18px minmax(0, 1fr);
      gap: 5px;
      min-height: 138px;
    }}
    .bar-chart-wrap-compact .bar-axis {{
      height: 86px;
      font-size: 8px;
    }}
    .bar-chart-compact {{
      grid-auto-columns: minmax(14px, 1fr);
      gap: 1px;
      overflow-x: visible;
      padding-bottom: 2px;
    }}
    .bar-chart-compact .bar-column {{
      grid-template-rows: 86px auto;
      min-width: 14px;
      gap: 3px;
    }}
    .bar-chart-compact .bar {{
      width: 10px;
      height: 86px;
      border-radius: 3px 3px 2px 2px;
    }}
    .bar-chart-compact .bar-column strong {{
      font-size: 8px;
      line-height: 1.05;
      overflow-wrap: normal;
      word-break: normal;
    }}
    .bar-chart-compact .bar-column strong span {{
      display: block;
      font-size: inherit;
      line-height: inherit;
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
    .result .result-location-primary {{
      margin: -1px 0 3px;
      color: #35453b;
      font-weight: 800;
    }}
    .result .result-location-secondary {{
      margin-bottom: 3px;
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
    .status-reported {{
      color: #704b08;
      background: #fff0c9;
    }}
    .source-pill {{
      display: inline-flex;
      margin: 0 0 7px 5px;
      padding: 2px 7px;
      border: 1px solid #cbd6cc;
      border-radius: 999px;
      color: #405047;
      background: #f8faf6;
      font-size: 10px;
      font-weight: 800;
      line-height: 1.35;
      text-transform: uppercase;
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
  <style>{push_ui_css()}</style>
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
          {view_menu(base_path, current, hours, region, admin_mode=admin_mode)}
        </div>
        <div class="report-nav">
          <nav class="range-tabs" aria-label="History range">{history_controls(hours, region, extra_params)}</nav>
          <nav class="region-tabs" aria-label="Region">{region_tabs(base_path, current, hours, region, region_statuses)}</nav>
        </div>
      </div>
    </header>
    <main>{body}</main>
  </div>
{push_ui_html(base_path)}
{push_ui_script(base_path)}
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
    filters=None,
    admin_mode=False,
    google_analytics_id=None,
):
    region = normalize_region(region)
    label = region_label(region)
    filters = filters or {}
    selected_type = filters.get("type") or "all"
    filtered_incidents = filtered_summary_incidents(incidents, filters)
    active_filter_params = {} if selected_type == "all" else {"type": selected_type}
    status = {**incident_status(filtered_incidents, hours), "region": region}
    active_count = status["active_count"]
    mapped_count = status["mapped_count"]
    reported_count = status["reported_count"]
    cleared_count = status["cleared_count"] + status["archived_count"]
    road_rows = report_rows(count_by(filtered_incidents, incident_road))
    type_rows = report_rows(count_by(filtered_incidents, lambda incident: incident.get("type") or "Unknown"))
    day_rows = report_rows(daily_incident_counts(filtered_incidents, generated_at, hours), limit=None, compact=True)
    time_rows = report_rows(time_bucket_counts(filtered_incidents), limit=None)
    recent = sorted(
        filtered_incidents,
        key=incident_recency,
        reverse=True,
    )[:5]
    recent_html = "".join(
        '<div class="result"><span class="status-pill {}">{}</span><span class="source-pill">{}</span><strong>{}</strong>{}<span>{} · #{}</span></div>'.format(
            incident_status_class(incident),
            html.escape(incident_status_label(incident)),
            html.escape(incident_source_label(incident)),
            html.escape(incident.get("type") or "Incident"),
            report_location_html(incident),
            html.escape(format_when_short(incident)),
            html.escape(str(incident.get("incident_no") or "")),
        )
        for incident in recent
    ) or '<div class="empty-report">No recent incidents in this window.</div>'
    filter_summary = (
        f"{len(filtered_incidents)} of {len(incidents)} incidents"
        if selected_type != "all"
        else f"{len(incidents)} incidents"
    )
    reset_href = href_with_query(app_path(base_path, "/summary"), hours=f"{hours:g}", region=region)
    body = f"""
      <form method="get" action="{html.escape(app_path(base_path, "/summary"))}" aria-label="Summary filters">
        <input type="hidden" name="hours" value="{hours:g}">
        <input type="hidden" name="region" value="{html.escape(region)}">
        <div class="filter-grid filter-grid-summary">
          <select class="filter" name="type" aria-label="Incident type filter">{option_tags(summary_type_options(incidents), selected_type)}</select>
        </div>
        <div class="filter-actions">
          <button type="submit">Apply filter</button>
          <a href="{html.escape(reset_href)}">Reset</a>
        </div>
        <div class="meta">{html.escape(filter_summary)} shown in the selected window.</div>
      </form>
      <section class="kpi-grid" aria-label="Incident summary">
        <div class="kpi"><strong>{status["total_count"]}</strong><span>Incidents in window</span></div>
        <div class="kpi"><strong>{active_count}</strong><span>Active CHP incidents</span></div>
        {f'<div class="kpi"><strong>{reported_count}</strong><span>Current WildWeb reports</span></div>' if reported_count else ''}
        <div class="kpi"><strong>{mapped_count}</strong><span>Mapped incidents</span></div>
        <div class="kpi"><strong>{cleared_count}</strong><span>Cleared or archived</span></div>
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
    subtitle = f"{label} incident activity · updated {generated_at}"
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
        extra_params=active_filter_params,
        admin_mode=admin_mode,
        google_analytics_id=google_analytics_id,
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
    admin_mode=False,
    google_analytics_id=None,
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
    status_options = [
        ("all", "All statuses"),
        ("active", "Active CHP"),
        ("reported", "Reported by WildWeb"),
        ("cleared", "Cleared"),
        ("archived", "Archived / no longer listed"),
    ]
    mapped_options = [("all", "Mapped + unpinned"), ("mapped", "Mapped only"), ("unpinned", "Unpinned only")]
    reset_href = href_with_query(app_path(base_path, "/history"), hours=f"{hours:g}", region=region)
    result_rows = "".join(
        '<div class="result"><span class="status-pill {}">{}</span><span class="source-pill">{}</span><strong>{}</strong>{}<span>{} · {} · #{} · <a href="{}">Show on map</a></span></div>'.format(
            incident_status_class(incident),
            html.escape(incident_status_label(incident)),
            html.escape(incident_source_label(incident)),
            html.escape(incident.get("type") or "Incident"),
            report_location_html(incident),
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
    subtitle = f"Search stored {label.lower()} incidents · updated {generated_at}"
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
        admin_mode=admin_mode,
        google_analytics_id=google_analytics_id,
    )


def build_about_html(
    incidents,
    generated_at,
    hours,
    base_path="/",
    public_url=None,
    region="forest",
    region_statuses=None,
    admin_mode=False,
    google_analytics_id=None,
):
    region = normalize_region(region)
    label = region_label(region)
    status = {**incident_status(incidents, hours), "region": region}
    if region == "forest":
        scope_text = "Angeles Crest, Angeles Forest, Big Tujunga, Glendora Mountain, and nearby forest roads"
    else:
        scope_text = "Malibu canyon and coastal roads including PCH-adjacent incidents"
    body = f"""
      <section class="section" style="margin-top: 0; padding-top: 0; border-top: 0;">
        <h2>What This Is</h2>
        <p class="empty-report">Crestmap combines public <a href="https://cad.chp.ca.gov/Traffic.aspx" rel="noopener">CHP traffic incidents</a> with selected <a href="https://www.wildwebe.net/incidents?dc_Name=CAANCC" rel="noopener">WildWeb dispatch reports</a> for {html.escape(scope_text)}.</p>
      </section>
      <section class="kpi-grid" aria-label="Current data status" style="margin-top: 14px;">
        <div class="kpi"><strong>{status["total_count"]}</strong><span>Incidents in this window</span></div>
        <div class="kpi"><strong>{status["active_count"]}</strong><span>Active CHP incidents</span></div>
        <div class="kpi"><strong>{status["reported_count"]}</strong><span>Current WildWeb reports</span></div>
        <div class="kpi"><strong>{status["mapped_count"]}</strong><span>Mapped incidents</span></div>
        <div class="kpi"><strong>1–2m</strong><span>Approximate source cadence</span></div>
      </section>
      <section class="section">
        <h2>Update Cadence</h2>
        <div class="result"><strong>CHP</strong><span>Checked about once per minute.</span></div>
        <div class="result"><strong>WildWeb</strong><span>Checked independently about once every two minutes. Reports older than the configured collection window are archived.</span></div>
        <div class="result"><strong>Active incident details</strong><span>Unchanged active incidents are refreshed about every 3 minutes.</span></div>
        <div class="result"><strong>Status meaning</strong><span>CHP records use Active and Cleared. WildWeb records say Reported unless the source explicitly provides Contained, Controlled, or Out. No longer listed and Archived do not mean Crestmap independently confirmed the incident is over. Both use gray map dots; aged-out reports have a muted brown ring while reports removed from WildWeb have a slate ring.</span></div>
        <div class="result"><strong>History</strong><span>Cleared and archived records stay in the database and are shown when they fall inside the selected time window.</span></div>
      </section>
      <section class="section" id="push-notifications">
        <h2>Push Notifications</h2>
        <p class="empty-report">Crestmap can notify you when it discovers a new matching incident. CHP alerts are selected by default; WildWeb reports are a separate opt-in because the two sources can describe the same incident. Choose sources, areas, and incident categories in Alerts.</p>
        <div class="result"><strong>Crest + west forest</strong><span>Includes Angeles Crest, Angeles Forest, Big Tujunga, and Mount Wilson/Red Box incidents. It excludes San Gabriel Canyon, Glendora Mountain/Ridge, Mount Baldy, and San Antonio Canyon incidents.</span></div>
        <div class="result"><strong>iPhone and iPad</strong><span>Open Crestmap in Safari. With Compact tabs, tap More (&hellip;) then Share; with Bottom or Top tabs, tap Share directly. Scroll down and choose Add to Home Screen. If it is missing, scroll to the bottom, open Edit Actions, and add it. Turn on Open as Web App, tap Add, then launch Crestmap from the new icon. Open Alerts from the menu and approve the notification prompt.</span></div>
        <div class="result"><strong>Privacy</strong><span>A browser-generated push endpoint and your choices are stored. No email address, phone number, or account is required. Turning alerts off deactivates that device subscription.</span></div>
        <div class="result"><strong>Delivery</strong><span>Notifications are sent only for newly discovered incidents after you subscribe. Delivery can be delayed or suppressed by Focus, Low Power settings, connectivity, or browser notification settings.</span></div>
        <div class="filter-actions"><button type="button" data-open-push-settings>Manage alert choices</button></div>
      </section>
      <section class="section">
        <h2>Project Links</h2>
        <div class="result"><strong>CHP CAD source</strong><span><a href="https://cad.chp.ca.gov/Traffic.aspx" rel="noopener">cad.chp.ca.gov/Traffic.aspx</a></span></div>
        <div class="result"><strong>WildWeb source</strong><span><a href="https://www.wildwebe.net/incidents?dc_Name=CAANCC" rel="noopener">wildwebe.net · CAANCC</a></span></div>
        <div class="result"><strong>Camera source</strong><span><a href="https://cameras.alertcalifornia.org/" rel="noopener">ALERTCalifornia</a> | UC San Diego</span></div>
        <div class="result"><strong>Air-temperature sources</strong><span><a href="https://api.weather.gov/" rel="noopener">National Weather Service</a> station observations and <a href="https://open-meteo.com/" rel="noopener">Open-Meteo</a> elevation-adjusted model estimates.</span></div>
        <div class="result"><strong>Road-weather sources</strong><span><a href="https://api.weather.gov/alerts" rel="noopener">National Weather Service</a> active alerts and <a href="https://open-meteo.com/" rel="noopener">Open-Meteo</a> six-hour elevation-aware precipitation forecasts. Road-weather markers are forecasts, not measured pavement conditions.</span></div>
        <div class="result"><strong>Mile-marker sources</strong><span><a href="https://postmile.dot.ca.gov/" rel="noopener">Caltrans postmiles</a> and <a href="https://dpw.gis.lacounty.gov/dpw/rest/services/road/MapServer/0" rel="noopener">LA County Public Works surveyed markers</a>.</span></div>
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
        admin_mode=admin_mode,
        google_analytics_id=google_analytics_id,
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
