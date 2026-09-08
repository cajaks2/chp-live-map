"""Small optional temperature layer for the existing Leaflet map."""

import json

TEMPERATURE_CSS = """
    .temperature-label { background: transparent; border: 0; }
    .temperature-label::before {
      content: ""; position: absolute; left: 2px; top: 10px; width: 4px; height: 4px;
      border-radius: 50%; background: #687162; box-shadow: 0 0 0 1px #ffffffb3;
    }
    .temperature-label span {
      position: absolute; left: 10px; top: -13px; display: block; box-sizing: border-box; width: 34px; padding: 4px 0;
      color: #454e43; text-align: center; font: 500 11px/16px -apple-system, BlinkMacSystemFont, sans-serif;
      text-shadow: 0 0 3px #fff, 0 1px 2px #fff, 0 -1px 2px #fff;
    }
    .temperature-label.is-left span { left: -40px; }
    .temperature-label.is-above span { left: -15px; top: -29px; }
    .temperature-label.is-below span { left: -15px; top: 14px; }
    .temperature-label.is-observation::before {
      left: 0; top: 8px; width: 8px; height: 8px; background: #27764e;
      box-shadow: 0 0 0 2px #f8fbf7, 0 0 0 3px #27764e;
    }
    .temperature-label.is-observation span {
      width: auto; min-width: 39px; padding: 2px 5px; color: #204c34;
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
      function renderTemperatures() {
        layer.clearLayers();
        if (!enabled) return;
        const occupied = [];
        markers.forEach(marker => {
          if (map.hasLayer(marker)) occupied.push(map.latLngToContainerPoint(marker.getLatLng()));
        });
        const placed = [];
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
          const edgeMargin = point.priority ? 12 : 32;
          if (pixel.x < edgeMargin || pixel.y < 38 || pixel.x > size.x - edgeMargin || pixel.y > size.y - 38) continue;
          // Road labels may sit near incidents, but never directly under one.
          const nearbyIncident = occupied.find(p => Math.abs(p.x - pixel.x) < 54 && Math.abs(p.y - pixel.y) < 55);
          if (nearbyIncident && !point.road && !measured) continue;
          const directClearanceX = point.priority ? 6 : 8;
          const directClearanceY = point.priority ? 8 : 10;
          if (point.road && occupied.some(p => Math.abs(p.x - pixel.x) < directClearanceX
            && Math.abs(p.y - pixel.y) < directClearanceY)) continue;
          if (placed.some(p => Math.abs(p.pixel.x - pixel.x) < (point.priority && p.priority ? 34 : 52)
            && Math.abs(p.pixel.y - pixel.y) < 26)) continue;
          placed.push({pixel, priority: Boolean(point.priority)});
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
          let labelDirection = pixel.x > size.x - 54 ? " is-left" : "";
          if (!labelDirection && nearbyIncident && (point.road || measured)) {
            const dx = nearbyIncident.x - pixel.x;
            const dy = nearbyIncident.y - pixel.y;
            if (Math.abs(dx) >= Math.abs(dy)) labelDirection = dx > 0 ? " is-left" : "";
            else labelDirection = dy > 0 ? " is-above" : " is-below";
          }
          const marker = L.marker(latlng, {
            pane: "temperatures", keyboard: true, riseOnHover: false,
            title: `${point.name}: ${degrees}°F, ${measured ? "measured" : "estimated"} air temperature`,
            icon: L.divIcon({className: `temperature-label${measured ? " is-observation" : ""}${labelDirection}`, html: `<span>${degrees}°</span>`, iconSize: [34, 24], iconAnchor: [4, 12]})
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
      }
      function scheduleRender() {
        if (frame !== null) return;
        frame = requestAnimationFrame(() => {
          frame = null;
          // Popup auto-pan must not remove the marker that owns the open popup.
          if (!layer.getLayers().some(marker => marker.isPopupOpen())) renderTemperatures();
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
      map.on("moveend zoomend resize", scheduleRender);
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
