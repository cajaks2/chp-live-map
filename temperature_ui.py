"""Small optional temperature layer for the existing Leaflet map."""

import json

TEMPERATURE_CSS = """
    .temperature-label { background: transparent; border: 0; pointer-events: none; }
    .temperature-label::before {
      content: ""; position: absolute; left: -2px; top: -2px; width: 4px; height: 4px;
      border-radius: 50%; background: #687162; box-shadow: 0 0 0 1px #ffffffb3;
    }
    .temperature-label span {
      position: absolute; left: var(--temperature-x); top: var(--temperature-y); display: block;
      box-sizing: border-box; width: var(--temperature-width); height: var(--temperature-height);
      pointer-events: auto; display: flex; align-items: center; justify-content: center;
      color: #354333; text-align: center; font: 600 12px/16px -apple-system, BlinkMacSystemFont, sans-serif;
      text-shadow: -1px -1px 0 #fbfcf8, 1px -1px 0 #fbfcf8, -1px 1px 0 #fbfcf8, 1px 1px 0 #fbfcf8, 0 0 3px #fff;
    }
    .temperature-label.is-observation::before {
      left: -4px; top: -4px; width: 8px; height: 8px; background: #27764e;
      box-shadow: 0 0 0 2px #f8fbf7, 0 0 0 3px #27764e;
    }
    .temperature-label.is-observation span {
      padding: 2px 5px; color: #204c34;
      background: rgba(248,251,247,.96); border: 1px solid #73a989;
      border-radius: 9px; box-shadow: 0 1px 4px rgba(27,67,45,.20);
      font-weight: 700; text-shadow: none;
    }
    .temperature-label:hover span { color: #263122; }
    .temperature-label:focus-visible span { outline: 2px solid #465a3d; border-radius: 3px; }
    .temperature-map-popup { position: absolute; padding-bottom: 10px; text-align: left; }
    .temperature-map-popup .leaflet-popup-content-wrapper {
      background: #fbfcf8; border: 1px solid #c8cec3; border-radius: 10px;
      box-shadow: 0 4px 18px rgba(24,32,38,.22); padding: 1px;
    }
    .temperature-map-popup .leaflet-popup-content { margin: 16px 22px 16px 16px; }
    .temperature-map-popup .leaflet-popup-close-button {
      position: absolute; top: 5px; right: 6px; width: 24px; height: 24px;
      color: #596253; text-align: center; text-decoration: none; font: 20px/24px sans-serif;
    }
    .temperature-map-popup .leaflet-popup-tip-container {
      position: absolute; bottom: 0; left: 50%; margin-left: -10px;
      width: 20px; height: 11px; overflow: hidden; pointer-events: none;
    }
    .temperature-map-popup .leaflet-popup-tip {
      width: 12px; height: 12px; margin: -6px auto 0; transform: rotate(45deg);
      background: #fbfcf8; border: 1px solid #c8cec3;
    }
    .temperature-popup { color: #414940; font: 13px/1.4 -apple-system, BlinkMacSystemFont, sans-serif; }
    .temperature-popup__reading { color: #263122; font-size: 19px; font-weight: 750; line-height: 1.2; }
    .temperature-popup__kind { margin-top: 2px; color: #687168; font-size: 12px; font-weight: 650; }
    .temperature-popup__location { margin-top: 10px; color: #303a30; font-size: 14px; font-weight: 650; line-height: 1.35; }
    .temperature-popup__meta { margin-top: 5px; color: #687168; line-height: 1.5; }
    .temperature-popup__forecast { margin-top: 11px; padding-top: 9px; border-top: 1px solid #dfe4dc; }
    .temperature-popup__forecast-title { color: #34483b; font-size: 11px; font-weight: 750; letter-spacing: .04em; text-transform: uppercase; }
    .temperature-popup__forecast-values { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 2px; margin-top: 5px; }
    .temperature-popup__forecast-item { min-width: 0; padding: 3px 1px; border-radius: 4px; background: #f0f4ee; color: #34483b; text-align: center; line-height: 1.2; }
    .temperature-popup__forecast-time { display: block; overflow: hidden; color: #687168; font-size: 9px; white-space: nowrap; }
    .temperature-popup__forecast-temp { display: block; font-size: 12px; font-weight: 750; }
    .temperature-popup__source { display: inline-block; margin-top: 9px; font-size: 12px; }
    .temperature-popup__note { display: block; margin-top: 7px; color: #687168; font-size: 11px; line-height: 1.4; }
    @media (max-width: 520px) {
      .temperature-map-popup .leaflet-popup-content { margin: 10px 20px 10px 12px; }
      .temperature-popup__heading { display: flex; align-items: baseline; gap: 7px; padding-right: 12px; }
      .temperature-popup__kind { margin-top: 0; }
      .temperature-popup__location { margin-top: 6px; }
      .temperature-popup__meta { margin-top: 2px; line-height: 1.35; }
      .temperature-popup__meta br { display: inline; content: ""; }
      .temperature-popup__meta br::after { content: " · "; }
      .temperature-popup__forecast { margin-top: 7px; padding-top: 6px; }
      .temperature-popup__source-note { margin-top: 7px; color: #687168; font-size: 10px; line-height: 1.3; }
      .temperature-popup__source { display: inline; margin-top: 0; font-size: inherit; }
      .temperature-popup__note { display: inline; margin-top: 0; font-size: inherit; line-height: inherit; }
    }
    .temperature-load-status {
      position: absolute; left: 50%; top: 54px; z-index: 1000; display: none;
      align-items: center; gap: 7px; max-width: calc(100% - 110px); padding: 6px 9px;
      border: 1px solid #c8cec3; border-radius: 9px; background: rgba(251,252,248,.96);
      color: #4b554a; box-shadow: 0 1px 5px rgba(24,32,38,.16);
      font: 600 11px/15px -apple-system, BlinkMacSystemFont, sans-serif;
      transform: translateX(-50%);
    }
    .temperature-load-status.is-visible { display: flex; }
    .temperature-load-status.is-error { cursor: pointer; color: #34483b; }
    .temperature-load-spinner {
      width: 10px; height: 10px; flex: 0 0 auto; border: 2px solid #b9c3b8;
      border-top-color: #397654; border-radius: 50%; animation: temperature-spin .8s linear infinite;
    }
    .temperature-load-status.is-error .temperature-load-spinner { display: none; }
    @keyframes temperature-spin { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .temperature-load-spinner { animation: none; } }
"""


def temperature_script(endpoint):
    return TEMPERATURE_JS.replace("__TEMPERATURE_ENDPOINT__", json.dumps(endpoint))


TEMPERATURE_JS = r"""
    (() => {
      const temperatureEndpoint = __TEMPERATURE_ENDPOINT__;
      const pane = map.createPane("temperatures");
      pane.style.zIndex = "430"; // Below cameras and incidents.
      const layer = L.layerGroup().addTo(map);
      let enabled = true;
      try { enabled = localStorage.getItem("crestmap-temperature") !== "hidden"; } catch (_) {}
      let points = [];
      let inFlight = false;
      let state = "idle";
      let frame = null;
      const previousPlacements = new Map();
      const button = document.querySelector("[data-temperature-layer-toggle]");
      if (!button) return;
      const loadStatus = document.createElement("button");
      loadStatus.type = "button";
      loadStatus.className = "temperature-load-status";
      loadStatus.setAttribute("aria-live", "polite");
      loadStatus.innerHTML = '<span class="temperature-load-spinner" aria-hidden="true"></span><span></span>';
      map.getContainer().appendChild(loadStatus);
      L.DomEvent.disableClickPropagation(loadStatus);
      loadStatus.addEventListener("click", () => { if (state === "error") refresh(); });
      button.addEventListener("click", () => {
        enabled = !enabled;
        try { localStorage.setItem("crestmap-temperature", enabled ? "shown" : "hidden"); } catch (_) {}
        updateButton();
        renderTemperatures();
        if (enabled) refresh();
      });
      function updateButton() {
        button.setAttribute("aria-pressed", String(enabled));
        button.classList.toggle("is-active", enabled);
        button.querySelector(".view-menu-description").textContent = !enabled ? "Hidden from map"
          : state === "loading" ? "Loading estimates…" : state === "error" ? "Estimates unavailable · retry by toggling"
          : "Measured + estimated °F · more detail as you zoom";
        button.title = `${enabled ? "Hide" : "Show"} estimated air temperatures`;
        updateLoadStatus();
      }
      function updateLoadStatus() {
        const initialLoading = enabled && state === "loading" && !points.length;
        const initialError = enabled && state === "error" && !points.length;
        loadStatus.classList.toggle("is-visible", initialLoading || initialError);
        loadStatus.classList.toggle("is-error", initialError);
        loadStatus.disabled = !initialError;
        loadStatus.querySelector("span:last-child").textContent = initialError
          ? "Temperatures unavailable · Tap to retry" : "Loading temperatures…";
      }
      function fresh(point) {
        const age = Date.now() - Date.parse(point.valid_at);
        const maxAge = point.kind === "observation" ? 10800000 : 3600000;
        return Number.isFinite(age) && age >= -900000 && age <= maxAge;
      }
      // TEMPERATURE_PLACEMENT_START: pure geometry, also exercised with dense road fixtures.
      function boxesOverlap(a, b, gap = 3) {
        return a.left < b.right + gap && a.right + gap > b.left
          && a.top < b.bottom + gap && a.bottom + gap > b.top;
      }
      function placeTemperatureLabel(pixel, size, occupied, width, height, previous) {
        const candidates = [
          [8, -height - 4], [-width - 8, -height - 4],
          [8, 5], [-width - 8, 5],
          [-width / 2, -height - 12], [-width / 2, 12],
          [24, -height / 2], [-width - 24, -height / 2],
          [18, -height - 22], [-width - 18, -height - 22],
          [18, 22], [-width - 18, 22]
        ];
        const order = [...candidates.keys()];
        if (Number.isInteger(previous) && previous >= 0 && previous < candidates.length) {
          order.splice(previous, 1); order.unshift(previous);
        }
        for (const index of order) {
          const [dx, dy] = candidates[index];
          const box = {left: pixel.x + dx, top: pixel.y + dy,
            right: pixel.x + dx + width, bottom: pixel.y + dy + height};
          if (box.left < 6 || box.top < 6 || box.right > size.x - 6 || box.bottom > size.y - 6) continue;
          if (occupied.some(other => boxesOverlap(box, other))) continue;
          return {index, dx, dy, box};
        }
        return null;
      }
      // TEMPERATURE_PLACEMENT_END
      function renderTemperatures() {
        layer.clearLayers();
        if (!enabled) return;
        const occupied = [];
        const protectPoint = (marker, radius) => {
          if (!map.hasLayer(marker)) return;
          const p = map.latLngToContainerPoint(marker.getLatLng());
          occupied.push({left: p.x - radius, right: p.x + radius, top: p.y - radius, bottom: p.y + radius});
        };
        markers.forEach(marker => protectPoint(marker, 23));
        cameraMarkers.forEach(marker => protectPoint(marker, 14));
        aircraftMarkers.forEach(marker => protectPoint(marker, 18));
        const mapRect = map.getContainer().getBoundingClientRect();
        // Protect app-owned road labels, weather notices and controls. Raster basemap text
        // has no measurable DOM boxes; halos keep it readable without guessing its location.
        map.getContainer().querySelectorAll('.leaflet-tooltip, .road-weather-label, .map-layer-menu, #locate-user, #reset-map-view').forEach(element => {
          if (element.classList.contains('offline-basemap-label') && !mapEl.classList.contains('using-offline-basemap')) return;
          const r = element.getBoundingClientRect();
          if (r.width && r.height) occupied.push({left: r.left - mapRect.left, right: r.right - mapRect.left,
            top: r.top - mapRect.top, bottom: r.bottom - mapRect.top});
        });
        const width = 42;
        const height = window.matchMedia('(pointer: coarse)').matches ? 36 : 26;
        const activeKeys = new Set();
        const displayRank = point => point.kind === "observation" ? 3 : point.priority ? 2 : point.road ? 1 : 0;
        const orderedPoints = [...points].sort((a, b) => displayRank(b) - displayRank(a));
        for (const point of orderedPoints) {
          const measured = point.kind === "observation";
          if (!fresh(point)) continue;
          // Keep the overview road-focused; reveal surrounding terrain after zooming in.
          if (point.kind !== "observation" && !point.priority && !point.road && map.getZoom() < 11) continue;
          const latlng = [point.latitude, point.longitude];
          if (!map.getBounds().contains(latlng)) continue;
          const pixel = map.latLngToContainerPoint(latlng);
          const size = map.getSize();
          const key = `${point.kind}:${point.latitude}:${point.longitude}:${point.name}`;
          activeKeys.add(key);
          const placement = placeTemperatureLabel(pixel, size, occupied, width, height, previousPlacements.get(key));
          if (!placement) continue; // All nearby positions are occupied; never cover an incident.
          previousPlacements.set(key, placement.index);
          occupied.push(placement.box);
          const degrees = Math.round(point.temperature_f);
          const elevation = Math.round(point.elevation_m * 3.28084).toLocaleString();
          const validDate = new Date(point.valid_at);
          const valid = validDate.toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
          const observationAge = measured ? Math.max(0, Date.now() - validDate.getTime()) : 0;
          const humidity = Number.isFinite(point.relative_humidity_percent) ? ` · ${Math.round(point.relative_humidity_percent)}% humidity` : "";
          const ageProgress = measured ? Math.min(1, Math.max(0, (observationAge - 1800000) / 5400000)) : 0;
          const forecast = (Array.isArray(point.forecast) ? point.forecast : []).slice(0, 6).map(item => {
            const when = new Date(item.valid_at);
            if (Number.isNaN(when.getTime()) || !Number.isFinite(item.temperature_f)) return null;
            const hour = when.toLocaleTimeString([], {hour: "numeric"});
            return `<span class="temperature-popup__forecast-item"><span class="temperature-popup__forecast-time">${escapeHtml(hour)}</span><span class="temperature-popup__forecast-temp">${Math.round(item.temperature_f)}°</span></span>`;
          }).filter(Boolean).join("");
          const forecastCopy = forecast ? `<div class="temperature-popup__forecast"><div class="temperature-popup__forecast-title">${measured ? "Nearby modeled forecast" : "Hourly forecast"}</div><div class="temperature-popup__forecast-values">${forecast}</div></div>` : "";
          const {dx, dy} = placement;
          const marker = L.marker(latlng, {
            pane: "temperatures", keyboard: true, riseOnHover: false,
            title: `${point.name}: ${degrees}°F, ${measured ? "measured" : "estimated"} air temperature`,
            icon: L.divIcon({className: `temperature-label${measured ? " is-observation" : ""}`, html: `<span style="--temperature-x:${dx}px;--temperature-y:${dy}px;--temperature-width:${width}px;--temperature-height:${height}px">${degrees}°</span>`, iconSize: [0, 0], iconAnchor: [0, 0]})
          });
          const detail = measured
            ? `<div class="temperature-popup__heading"><div class="temperature-popup__reading">${degrees}°F</div><div class="temperature-popup__kind">Measured air temperature</div></div><div class="temperature-popup__location">${escapeHtml(point.name)}</div><div class="temperature-popup__meta">Station elevation ${elevation} ft${humidity}<br>Observed ${escapeHtml(valid)}</div>${forecastCopy}<div class="temperature-popup__source-note"><a class="temperature-popup__source" href="https://api.weather.gov/stations/${encodeURIComponent(point.station_id)}/observations/latest" target="_blank" rel="noopener">National Weather Service station</a><span class="temperature-popup__note"> · Forecast by Open-Meteo · Local conditions may differ.</span></div>`
            : `<div class="temperature-popup__heading"><div class="temperature-popup__reading">${degrees}°F</div><div class="temperature-popup__kind">Estimated air temperature</div></div><div class="temperature-popup__location">${escapeHtml(point.name)}</div><div class="temperature-popup__meta">${elevation} ft elevation${humidity}<br>Valid ${escapeHtml(valid)}</div>${forecastCopy}<div class="temperature-popup__source-note"><a class="temperature-popup__source" href="https://open-meteo.com/" target="_blank" rel="noopener">Open-Meteo model</a><span class="temperature-popup__note"> · Air and road-surface temperatures may differ.</span></div>`;
          marker.bindPopup(`<div class="temperature-popup">${detail}</div>`, {className: "temperature-map-popup", maxWidth: 280, offset: [0, -14], autoPanPaddingTopLeft: [24, 32], autoPanPaddingBottomRight: [24, 74]});
          marker.addTo(layer);
          if (measured && ageProgress > 0) {
            marker.setOpacity(1 - (0.40 * ageProgress));
            const element = marker.getElement();
            if (element) element.style.filter = `grayscale(${Math.round(ageProgress * 100)}%)`;
          }
        }
        for (const key of previousPlacements.keys()) if (!activeKeys.has(key)) previousPlacements.delete(key);
      }
      function scheduleRender() {
        if (frame !== null) return;
        frame = requestAnimationFrame(() => {
          frame = null;
          // Popup auto-pan must not remove the marker that owns the open popup.
          if (!layer.getLayers().some(marker => marker.isPopupOpen?.())) renderTemperatures();
        });
      }
      async function refresh() {
        if (!enabled || inFlight || document.hidden) return;
        inFlight = true;
        state = "loading";
        updateButton();
        try {
          const response = await fetch(`${temperatureEndpoint}?region=${encodeURIComponent(currentRegion)}`, {signal: AbortSignal.timeout(12000)});
          if (!response.ok) throw new Error("unavailable");
          const data = await response.json();
          if (data.region !== currentRegion || !Array.isArray(data.points)) throw new Error("invalid data");
          points = data.points.filter(p => ["estimate", "observation"].includes(p.kind) && Number.isFinite(p.temperature_f) && Number.isFinite(p.elevation_m) && Number.isFinite(p.latitude) && Number.isFinite(p.longitude) && fresh(p));
          state = points.length ? "ready" : "error";
        } catch (_) {
          points = []; // Never quietly present a failed refresh as current data.
          state = "error";
        } finally {
          inFlight = false;
          updateButton();
          renderTemperatures();
        }
      }
      map.on("moveend zoomend resize workspacechange", scheduleRender);
      map.on("layeradd layerremove", event => {
        if (event.layer instanceof L.Marker && event.layer.options.pane !== "temperatures") scheduleRender();
      });
      document.addEventListener("visibilitychange", () => { if (!document.hidden) { renderTemperatures(); refresh(); } });
      window.addEventListener("online", refresh);
      window.setInterval(refresh, 15 * 60 * 1000);
      window.setInterval(() => {
        if (points.some(point => !fresh(point))) {
          points = points.filter(fresh);
          if (!points.length) { state = "error"; updateButton(); }
          renderTemperatures();
        }
      }, 60 * 1000);
      updateButton();
      refresh();
      window.chpLiveMap.temperatureLayer = layer;
    })();
"""
