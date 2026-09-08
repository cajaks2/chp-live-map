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
    .road-weather-label.is-rain_possible { color: #628c9c; }
    .road-weather-label.is-rain_possible span { background: rgba(98,140,156,.82); }
    .road-weather-label.is-rain_recent { color: #788b91; }
    .road-weather-label.is-rain_recent span { background: rgba(120,139,145,.76); }
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
      position: absolute; left: 50%; top: 12px; z-index: 1000; height: 36px; max-width: calc(100% - 120px);
      padding: 0 10px; border: 1px solid #8796a2; border-radius: 999px; background: rgba(251,252,248,.96);
      color: #465767; box-shadow: 0 2px 8px rgba(24,32,38,.16); cursor: pointer;
      font: 700 10px/14px -apple-system,BlinkMacSystemFont,sans-serif; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      transform: translateX(-50%);
    }
    .road-weather-alert-details {
      position: absolute; left: 56px; right: 56px; top: 54px; z-index: 1002;
      padding: 12px 14px; border: 1px solid rgba(56,74,62,.22); border-radius: 11px;
      background: rgba(251,252,248,.98); color: #414940; box-shadow: 0 6px 20px rgba(24,32,38,.2);
      font: 12px/1.45 -apple-system,BlinkMacSystemFont,sans-serif;
    }
    .road-weather-alert-details strong { display: block; padding-right: 22px; color: #263122; font-size: 14px; }
    .road-weather-alert-details span { display: block; margin-top: 4px; }
    .road-weather-alert-details button {
      position: absolute; top: 5px; right: 6px; width: 28px; height: 28px; padding: 0; border: 0;
      color: #596253; background: transparent; cursor: pointer; font: 20px/28px sans-serif;
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
      const alertBadge = document.createElement("button");
      alertBadge.type = "button";
      alertBadge.className = "road-weather-alert";
      alertBadge.setAttribute("aria-expanded", "false");
      alertBadge.hidden = true;
      map.getContainer().appendChild(alertBadge);
      const alertDetails = document.createElement("div");
      alertDetails.className = "road-weather-alert-details";
      alertDetails.hidden = true;
      map.getContainer().appendChild(alertDetails);

      function closeAlertDetails() {
        alertDetails.hidden = true;
        alertBadge.setAttribute("aria-expanded", "false");
      }
      alertBadge.addEventListener("click", event => {
        event.stopPropagation();
        alertDetails.hidden = !alertDetails.hidden;
        alertBadge.setAttribute("aria-expanded", String(!alertDetails.hidden));
      });
      alertDetails.addEventListener("click", event => {
        event.stopPropagation();
        if (event.target.closest("button")) closeAlertDetails();
      });
      document.addEventListener("click", closeAlertDetails);

      function description() {
        if (!enabled) return "Hidden from map";
        if (state === "loading") return "Checking recent + next 6 hours…";
        if (state === "error") return "Forecast unavailable · tap to retry";
        const counts = points.reduce((all, point) => { all[point.hazard] = (all[point.hazard] || 0) + 1; return all; }, {});
        const rainCount = (counts.rain || 0) + (counts.rain_possible || 0) + (counts.rain_recent || 0);
        const labels = [rainCount ? "Rain" : "", counts.snow ? "Snow" : "", counts.ice ? "Ice" : ""].filter(Boolean);
        return labels.length ? `${labels.join(" · ")} indicated along roads` : "No rain, snow, or ice indicated";
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
        closeAlertDetails();
        if (!enabled) return;
        if (alerts.length) {
          const alert = alerts[0];
          const expiry = new Date(alert.expires);
          const expiryCopy = Number.isNaN(expiry.getTime()) ? "" : `Expires ${new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit" }).format(expiry)}`;
          alertBadge.textContent = `NWS · ${alert.event}`;
          alertBadge.title = `${alert.event} — tap for details`;
          alertBadge.setAttribute("aria-label", `${alert.event}. Tap for details.`);
          alertDetails.innerHTML = `<button type="button" aria-label="Close advisory details">×</button><strong>${escapeHtml(alert.event)}</strong><span>${escapeHtml(alert.headline)}</span><span>${escapeHtml(alert.area)}${expiryCopy ? ` · ${escapeHtml(expiryCopy)}` : ""}</span>`;
          alertBadge.hidden = false;
        }
        const placed = [];
        const rank = { ice: 5, snow: 4, rain: 3, rain_possible: 2, rain_recent: 1 };
        [...points].sort((a, b) => rank[b.hazard] - rank[a.hazard]).forEach(point => {
          const latlng = [point.latitude, point.longitude];
          if (!map.getBounds().contains(latlng)) return;
          const pixel = map.latLngToContainerPoint(latlng);
          if (placed.some(existing => Math.abs(existing.x - pixel.x) < 58 && Math.abs(existing.y - pixel.y) < 42)) return;
          placed.push(pixel);
          const elevation = Math.round(point.elevation_m * 3.28084).toLocaleString();
          const label = point.hazard === "ice" ? "Ice possible" : point.hazard === "snow" ? "Snow possible" : point.hazard === "rain" ? "Rain likely" : point.hazard === "rain_possible" ? "Rain possible" : "Recent rain";
          const amount = point.hazard === "snow" ? `${point.snow_inches} in modeled snow` : point.hazard.startsWith("rain") ? `${point.rain_inches} in modeled rain` : `Low near ${Math.round(point.minimum_temperature_f)}°F`;
          const periods = Array.isArray(point.periods) && point.periods.length
            ? point.periods : [{ starts_at: point.starts_at, ends_at: point.ends_at }];
          const time = new Intl.DateTimeFormat([], { hour: "numeric" });
          const now = Date.now();
          const hazardWindow = periods.map(period => {
            const start = new Date(period.starts_at);
            const end = new Date(period.ends_at);
            if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return null;
            const startLabel = start.getTime() <= now && now < end.getTime() ? "Now" : time.format(start);
            return `${startLabel}–${time.format(end)}`;
          }).filter(Boolean).join(", ") || "Within the next six hours";
          const hazardWindowLabel = point.hazard === "ice" ? "Possible ice" : point.hazard === "snow" ? "Expected snow" : point.hazard === "rain_recent" ? "Modeled rain" : "Expected rain";
          const validUntil = new Date(point.valid_until);
          const checkedThrough = Number.isNaN(validUntil.getTime()) ? "" : time.format(validUntil);
          const marker = L.marker(latlng, {
            pane: "roadWeather", keyboard: true,
            title: `${point.name}: ${label}`,
            icon: L.divIcon({
              className: `road-weather-label is-${point.hazard}`,
              html: `<span>${point.hazard === "rain_recent" ? "WET" : point.hazard === "rain_possible" ? "RAIN?" : point.hazard.toUpperCase()}</span>`, iconSize: [34,17], iconAnchor: [17,8]
            })
          });
          const recentDetail = `<b>${escapeHtml(hazardWindow)}</b><br>${elevation} ft<br>${escapeHtml(amount)}<small>Modeled recent rainfall, not a rain-gauge or road-surface measurement.</small>`;
          const forecastDetail = `<b>${hazardWindowLabel}: ${escapeHtml(hazardWindow)}</b>${checkedThrough ? `<br>Forecast checked through ${escapeHtml(checkedThrough)}` : ""}<br>${elevation} ft · ${point.precipitation_probability}% chance<br>${escapeHtml(amount)}<small>Hourly forecast guidance may shift. Check posted closures and chain controls before travel.</small>`;
          marker.bindPopup(`<div class="road-weather-popup"><strong>${label}</strong><br>${escapeHtml(point.name)}<br>${point.hazard === "rain_recent" ? recentDetail : forecastDetail}</div>`, { className: "road-weather-map-popup", maxWidth: 280, offset: [0,-14], autoPanPadding: [32,32] });
          marker.on("popupopen", () => { popupOpen = true; });
          marker.on("popupclose", () => {
            popupOpen = false;
            window.requestAnimationFrame(render);
          });
          marker.addTo(layer);
          if (point.hazard === "rain_recent") {
            const recentAgeHours = Math.max(0, (Date.now() - Date.parse(point.ends_at)) / 3600000);
            const fadeProgress = Math.min(1, Math.max(0, (recentAgeHours - 2) / 2));
            marker.setOpacity(0.9 - 0.45 * fadeProgress);
            const element = marker.getElement();
            if (element) element.style.filter = `grayscale(${Math.round(fadeProgress * 60)}%)`;
          }
        });
      }
      async function refresh() {
        if (!enabled || inFlight || document.hidden) return;
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
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) refresh();
      });
      window.addEventListener("online", refresh);
      window.setInterval(refresh, 15 * 60 * 1000);
      updateButton(); if (enabled) refresh();
      window.chpLiveMap.roadWeatherLayer = layer;
    })();
"""
