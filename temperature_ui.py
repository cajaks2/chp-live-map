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
    .temperature-popup { color: #414940; font: 13px/1.65 -apple-system, BlinkMacSystemFont, sans-serif; }
    .temperature-popup strong { font-size: 17px; color: #263122; }
    .temperature-popup small { display: block; margin-top: 6px; max-width: 220px; }
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
          : "Estimated °F · more detail as you zoom";
        button.title = `${enabled ? "Hide" : "Show"} estimated air temperatures`;
      }
      function fresh(point) {
        const age = Date.now() - Date.parse(point.valid_at);
        return Number.isFinite(age) && age >= -900000 && age <= 3600000;
      }
      function renderTemperatures() {
        layer.clearLayers();
        if (!enabled) return;
        const occupied = [];
        markers.forEach(marker => {
          if (map.hasLayer(marker)) occupied.push(map.latLngToContainerPoint(marker.getLatLng()));
        });
        const placed = [];
        for (const point of points) {
          if (!fresh(point)) continue;
          const latlng = [point.latitude, point.longitude];
          if (!map.getBounds().contains(latlng)) continue;
          const pixel = map.latLngToContainerPoint(latlng);
          const size = map.getSize();
          if (pixel.x < 32 || pixel.y < 38 || pixel.x > size.x - 32 || pixel.y > size.y - 38) continue;
          // Keep temperature badges clear of incidents and one another.
          if (occupied.some(p => Math.abs(p.x - pixel.x) < 54 && Math.abs(p.y - pixel.y) < 55)) continue;
          if (placed.some(p => Math.abs(p.x - pixel.x) < 52 && Math.abs(p.y - pixel.y) < 26)) continue;
          placed.push(pixel);
          const degrees = Math.round(point.temperature_f);
          const elevation = Math.round(point.elevation_m * 3.28084).toLocaleString();
          const valid = new Date(point.valid_at).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
          const marker = L.marker(latlng, {
            pane: "temperatures", keyboard: true, riseOnHover: false,
            title: `${point.name}: approximately ${degrees}°F, estimated air temperature`,
            icon: L.divIcon({className: "temperature-label", html: `<span>${degrees}°</span>`, iconSize: [34, 24], iconAnchor: [4, 12]})
          });
          marker.bindPopup(`<div class="temperature-popup"><strong>${degrees}°F · Air temperature</strong><br>
            Estimated · ${escapeHtml(point.name)}<br>Terrain elevation ${elevation} ft<br>Model valid ${escapeHtml(valid)}<br>
            <a href="https://open-meteo.com/" target="_blank" rel="noopener">Open-Meteo</a>
            <small>Elevation-adjusted air temperature. Local conditions may differ; not a station or road-surface reading.</small></div>`, {className: "temperature-map-popup", maxWidth: 270, autoPanPadding: [32, 32]});
          marker.addTo(layer);
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
          points = data.points.filter(p => p.kind === "estimate" && Number.isFinite(p.temperature_f) && Number.isFinite(p.elevation_m) && Number.isFinite(p.latitude) && Number.isFinite(p.longitude) && fresh(p));
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
