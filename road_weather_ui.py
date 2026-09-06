"""Subtle road weather forecast overlay for the Leaflet map."""

import json


ROAD_WEATHER_CSS = """
    .road-weather-label { background: transparent; border: 0; }
    .road-weather-label span {
      display: block; box-sizing: border-box; min-width: 31px; padding: 2px 4px;
      border: 1px solid rgba(255,255,255,.96); border-radius: 5px; color: #fff;
      box-shadow: 0 0 0 1px currentColor, 0 1px 4px rgba(20,31,24,.28);
      font: 800 8px/11px -apple-system,BlinkMacSystemFont,sans-serif; letter-spacing: .04em;
      text-align: center; text-shadow: none;
    }
    .road-weather-label.is-rain { color: #337a96; }
    .road-weather-label.is-rain span { background: rgba(51,122,150,.92); }
    .road-weather-label.is-snow { color: #6a67a0; }
    .road-weather-label.is-snow span { background: rgba(106,103,160,.94); }
    .road-weather-label.is-ice { color: #596b7d; }
    .road-weather-label.is-ice span { background: rgba(89,107,125,.94); }
    .road-weather-popup { color: #414940; font: 13px/1.55 -apple-system,BlinkMacSystemFont,sans-serif; }
    .road-weather-popup strong { color: #263122; font-size: 16px; }
    .road-weather-popup small { display: block; margin-top: 7px; max-width: 240px; }
    .road-weather-map-popup { position: absolute; padding-bottom: 10px; text-align: left; }
    .road-weather-map-popup .leaflet-popup-content-wrapper {
      background: #fbfcf8; border: 1px solid #c8cec3; border-radius: 10px;
      box-shadow: 0 4px 18px rgba(24,32,38,.22); padding: 1px;
    }
    .road-weather-map-popup .leaflet-popup-content { margin: 16px 24px 16px 16px; }
    .road-weather-map-popup .leaflet-popup-close-button {
      position: absolute; top: 5px; right: 6px; width: 24px; height: 24px;
      color: #596253; text-align: center; text-decoration: none; font: 20px/24px sans-serif;
    }
    .road-weather-map-popup .leaflet-popup-tip-container {
      position: absolute; bottom: 0; left: 50%; margin-left: -10px; width: 20px; height: 11px;
      overflow: hidden; pointer-events: none;
    }
    .road-weather-map-popup .leaflet-popup-tip {
      width: 12px; height: 12px; margin: -6px auto 0; transform: rotate(45deg);
      background: #fbfcf8; border: 1px solid #c8cec3;
    }
    .road-weather-alert {
      position: absolute; left: 50%; top: 48px; z-index: 431; max-width: calc(100% - 120px);
      padding: 5px 9px; border: 1px solid #8796a2; border-radius: 999px; background: rgba(251,252,248,.96);
      color: #465767; box-shadow: 0 1px 5px rgba(24,32,38,.16); transform: translateX(-50%);
      font: 700 10px/14px -apple-system,BlinkMacSystemFont,sans-serif; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
"""


def road_weather_script(endpoint):
    return ROAD_WEATHER_JS.replace("__ROAD_WEATHER_ENDPOINT__", json.dumps(endpoint))


ROAD_WEATHER_JS = r"""
    (() => {
      const endpoint = __ROAD_WEATHER_ENDPOINT__;
      const button = document.querySelector("[data-road-weather-layer-toggle]");
      if (!button) return;
      const pane = map.createPane("roadWeather");
      pane.style.zIndex = "440"; // Above temperatures and mile labels, below incidents and cameras.
      const layer = L.layerGroup().addTo(map);
      let enabled = true;
      try { enabled = localStorage.getItem("crestmap-road-weather") !== "hidden"; } catch (_) {}
      let state = "idle";
      let points = [];
      let alerts = [];
      let inFlight = false;
      let popupOpen = false;
      const alertBadge = document.createElement("div");
      alertBadge.className = "road-weather-alert";
      alertBadge.hidden = true;
      map.getContainer().appendChild(alertBadge);

      function description() {
        if (!enabled) return "Hidden from map";
        if (state === "loading") return "Checking next 6 hours…";
        if (state === "error") return "Forecast unavailable · tap to retry";
        const counts = points.reduce((all, point) => { all[point.hazard] = (all[point.hazard] || 0) + 1; return all; }, {});
        const labels = ["rain", "snow", "ice"].filter(key => counts[key]);
        return labels.length ? `${labels.map(key => key[0].toUpperCase() + key.slice(1)).join(" · ")} indicated along roads` : "No rain, snow, or ice indicated";
      }
      function updateButton() {
        button.classList.toggle("is-active", enabled);
        button.setAttribute("aria-pressed", String(enabled));
        const copy = button.querySelector(".view-menu-description");
        if (copy) copy.textContent = description();
      }
      function render() {
        if (popupOpen) return;
        layer.clearLayers();
        alertBadge.hidden = true;
        if (!enabled) return;
        if (alerts.length) {
          alertBadge.textContent = `NWS · ${alerts[0].event}`;
          alertBadge.title = `${alerts[0].event} — ${alerts[0].area}`;
          alertBadge.hidden = false;
        }
        const placed = [];
        const rank = { ice: 3, snow: 2, rain: 1 };
        [...points].sort((a, b) => rank[b.hazard] - rank[a.hazard]).forEach(point => {
          const latlng = [point.latitude, point.longitude];
          if (!map.getBounds().contains(latlng)) return;
          const pixel = map.latLngToContainerPoint(latlng);
          if (placed.some(existing => Math.abs(existing.x - pixel.x) < 58 && Math.abs(existing.y - pixel.y) < 42)) return;
          placed.push(pixel);
          const elevation = Math.round(point.elevation_m * 3.28084).toLocaleString();
          const label = point.hazard === "ice" ? "Ice possible" : point.hazard === "snow" ? "Snow possible" : "Rain likely";
          const amount = point.hazard === "snow" ? `${point.snow_inches} in modeled snow` : point.hazard === "rain" ? `${point.rain_inches} in modeled rain` : `Low near ${Math.round(point.minimum_temperature_f)}°F`;
          const periods = Array.isArray(point.periods) && point.periods.length
            ? point.periods : [{ starts_at: point.starts_at, ends_at: point.ends_at }];
          const time = new Intl.DateTimeFormat([], { hour: "numeric" });
          const forecastWindow = periods.map(period => {
            const start = new Date(period.starts_at);
            const end = new Date(period.ends_at);
            if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return null;
            const startLabel = start.getTime() <= Date.now() + 3600000 ? "Now" : time.format(start);
            return `${startLabel}–${time.format(end)}`;
          }).filter(Boolean).join(", ") || "Within the next six hours";
          const marker = L.marker(latlng, {
            pane: "roadWeather", keyboard: true,
            title: `${point.name}: ${label}`,
            icon: L.divIcon({
              className: `road-weather-label is-${point.hazard}`,
              html: `<span>${point.hazard.toUpperCase()}</span>`, iconSize: [34,17], iconAnchor: [17,8]
            })
          });
          marker.bindPopup(`<div class="road-weather-popup"><strong>${label}</strong><br>${escapeHtml(point.name)}<br><b>Model window: ${escapeHtml(forecastWindow)}</b><br>${elevation} ft · ${point.precipitation_probability}% chance<br>${escapeHtml(amount)}<small>Timing is hourly guidance and may shift. This is not a measured pavement condition. Check posted closures and chain controls before travel.</small></div>`, { className: "road-weather-map-popup", maxWidth: 280, autoPanPadding: [32,32] });
          marker.on("popupopen", () => { popupOpen = true; });
          marker.on("popupclose", () => {
            popupOpen = false;
            window.requestAnimationFrame(render);
          });
          marker.addTo(layer);
        });
      }
      async function refresh() {
        if (!enabled || inFlight) return;
        inFlight = true; state = "loading"; updateButton();
        try {
          const response = await fetch(`${endpoint}?region=${encodeURIComponent(currentRegion)}`, { signal: AbortSignal.timeout(12000) });
          if (!response.ok) throw new Error("road weather unavailable");
          const data = await response.json();
          points = Array.isArray(data.points) ? data.points : [];
          alerts = Array.isArray(data.alerts) ? data.alerts : [];
          state = "ready"; render();
        } catch (_) { state = "error"; }
        finally { inFlight = false; updateButton(); }
      }
      button.addEventListener("click", () => {
        if (enabled && state === "error") { refresh(); return; }
        enabled = !enabled;
        try { localStorage.setItem("crestmap-road-weather", enabled ? "shown" : "hidden"); } catch (_) {}
        updateButton(); render(); if (enabled && state === "idle") refresh();
      });
      map.on("moveend zoomend", render);
      updateButton(); if (enabled) refresh();
      window.chpLiveMap.roadWeatherLayer = layer;
    })();
"""
