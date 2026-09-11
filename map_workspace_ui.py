"""Map workspace controls and touch sheet; desktop retains its three panes."""

MAP_WORKSPACE_CSS = """
    .mobile-map-toolbar, .map-sheet-controls, #map-sheet-preview { display: none; }
    .mobile-polled { display: none; }
    .crestmap-wordmark { display: inline-flex; align-items: center; gap: 6px; color: #18392b;
      font-weight: 900; letter-spacing: -.045em; white-space: nowrap; }
    .crestmap-mark { width: 24px; height: 24px; flex: 0 0 auto; overflow: visible; }
    .crestmap-mark rect { fill: #18392b; }
    .crestmap-mark-back { fill: #f4f7ee; }
    .crestmap-mark-front { fill: #6fbf73; }
    .crestmap-mark circle { fill: #2f8a4e; }
    .crestmap-mark.has-active circle { fill: #d83b3b; }
    .incident-marker .incident-marker-dot { inset: 11px; }
    .incident-marker:focus-visible { outline: 2px solid #245939; outline-offset: 2px; border-radius: 50%; }
    .map-overlap-choice { display: block; width: 100%; padding: 12px; text-align: left; cursor: pointer;
      background: #fff; border: 0; border-bottom: 1px solid #d8ddd2; color: #263f2e; font: inherit; }
    .map-overlap-choice span { display: block; font-size: 12px; }
    #incident-search-shell { flex: 0 0 auto; padding: 10px 12px 8px; border-bottom: 1px solid #dfe5dc; background: #f7f9f5; }
    #incident-search-shell label { display: block; margin: 0 0 5px; color: #405047; font-size: 11px; font-weight: 800; letter-spacing: .02em; }
    .incident-search-field { position: relative; }
    #incident-search { box-sizing: border-box; width: 100%; min-height: 40px; padding: 8px 38px 8px 12px;
      border: 1px solid #c9d3c9; border-radius: 9px; background: #fff; color: #24362a; font: 14px/1.3 -apple-system, BlinkMacSystemFont, sans-serif; }
    #incident-search:focus { border-color: #397a50; outline: 3px solid rgba(57,122,80,.15); }
    #incident-search-clear { position: absolute; top: 3px; right: 3px; width: 34px; height: 34px; padding: 0;
      border: 0; background: transparent; color: #496052; font-size: 20px; cursor: pointer; }
    #incident-search-status { display: block; min-height: 15px; margin-top: 4px; color: #647067; font-size: 11px; }
    #map .map-layer-menu summary { display: flex; width: auto; min-width: 76px; min-height: 44px; gap: 6px; padding: 0 10px; white-space: nowrap; }
    .map-layer-label { font: 600 12px/1.3 -apple-system, BlinkMacSystemFont, sans-serif; }
    @media (max-width: 1000px) {
      html, body { overflow: hidden; }
      #app { position: fixed; inset: 0; display: grid; grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr);
        width: 100%; height: auto; min-height: 0; overflow: hidden; }
      #sidebar { display: contents; }
      #sidebar header { grid-row: 1; padding: max(6px, env(safe-area-inset-top)) 10px 6px; z-index: 800; }
      #sidebar .title-row h1 { font-size: 16px; }
      #sidebar .title-row h1 { flex: 0 0 auto; order: 1; }
      #sidebar .title-row #incident-summary { order: 2; flex: 1 1 auto; min-width: 0; margin: 0;
        overflow: hidden; color: #526158; font-size: 10px; line-height: 1.2; text-align: right; white-space: nowrap; }
      #sidebar .title-row .view-header-actions { flex: 0 0 auto; order: 3; }
      #sidebar .mobile-polled { display: inline; }
      #sidebar .map-title-context { display: none; }
      #sidebar .checked-meta { font-size: 11px; }
      #sidebar #connection-status { margin-top: 3px; font-size: 11px; }
      #sidebar #connection-status[data-state="online"] { display: none; }
      #sidebar .range-tab, #sidebar .region-tab { min-height: 32px; }
      #sidebar .view-menu summary { min-width: 44px; min-height: 44px; }
      #sidebar .view-menu-popover .checked-meta { display: flex; padding: 12px; flex-wrap: wrap; }
      #sidebar .view-menu-popover .auto-refresh-control { min-height: 44px; font-size: 13px; }
      #sidebar .view-menu-popover .auto-refresh-control input { width: 18px; height: 18px; }
      #map { grid-row: 2; height: 100%; min-height: 0; transform: translateZ(0); backface-visibility: hidden; }
      #details-cue { display: none; }
      .map-layer-popover { max-height: min(60dvh, 430px); }
      #incident-list-shell { box-sizing: border-box; display: flex; flex-direction: column; position: absolute; z-index: 700; left: 0; right: 0;
        bottom: 0; height: min(60%, 520px); min-height: 0; padding-bottom: calc(var(--map-toolbar-height, 52px) + 10px);
        background: #fbfcf8; border-top: 1px solid #cbd6cc; border-radius: 14px 14px 0 0;
        box-shadow: 0 -4px 18px #18202620; visibility: hidden; pointer-events: none;
        will-change: transform; backface-visibility: hidden;
        transform: translate3d(0, calc(100% + var(--list-drag-y, 0px)), 0);
        transition: transform 220ms cubic-bezier(.2,.8,.2,1), height 220ms cubic-bezier(.2,.8,.2,1), visibility 0s linear 220ms; }
      #app[data-map-list="open"] #incident-list-shell { visibility: visible; pointer-events: auto; transform: translate3d(0, var(--list-drag-y, 0px), 0);
        transition-delay: 0s; }
      #app[data-map-list="expanded"] #incident-list-shell { visibility: visible; pointer-events: auto;
        height: calc(100% - var(--map-header-height, 170px) + 8px);
        max-height: 760px; transform: translate3d(0, var(--list-drag-y, 0px), 0); transition-delay: 0s; }
      #incident-list-shell.is-dragging { transition: none; }
      #incident-list-handle { flex: 0 0 24px; width: 100%; padding: 0; border: 0; border-radius: 14px 14px 0 0;
        background: #f7f9f5; touch-action: none; cursor: grab; }
      #incident-list-handle span { display: block; width: 38px; height: 4px; margin: 8px auto 6px;
        border-radius: 999px; background: #aebcaf; }
      #incident-list-close { position: absolute; top: 3px; right: 6px; z-index: 2; width: 44px; height: 44px;
        padding: 0; border: 0; background: transparent; color: #31523e; font: 20px/1 sans-serif; cursor: pointer; }
      #incident-list { flex: 1; min-height: 0; height: auto; }
      #incident-search-shell { padding-top: 3px; }
      .mobile-map-toolbar { display: flex; position: absolute; left: 50%; bottom: max(10px, env(safe-area-inset-bottom));
        align-items: center; width: max-content; max-width: calc(100% - 24px); transform: translateX(-50%);
        z-index: 750; font-size: 12px; transition: opacity 160ms ease, transform 180ms ease; }
      #app[data-map-sheet="expanded"] .mobile-map-toolbar,
      #app[data-map-sheet="full"] .mobile-map-toolbar {
        opacity: 0; pointer-events: none; transform: translate(-50%, 12px); }
      #app[data-map-list="open"] .mobile-map-toolbar,
      #app[data-map-list="expanded"] .mobile-map-toolbar {
        opacity: 0; pointer-events: none; transform: translate(-50%, 12px); }
      .mobile-map-toolbar button, .map-sheet-controls button { min-width: 44px; min-height: 44px;
        border: 0; border-radius: 7px; background: transparent; color: #245939; font: inherit; cursor: pointer; }
      #map-list-toggle { display: inline-flex; align-items: center; gap: 7px; min-height: 44px; padding: 8px 14px;
        border: 1px solid rgba(42,57,47,.25); border-radius: 10px; color: #294d37;
        background: rgba(255,255,255,.96); box-shadow: 0 3px 12px rgba(24,32,38,.2); font-weight: 800; }
      #map-list-toggle svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; }
      #details { display: flex; flex-direction: column; position: absolute; z-index: 710; left: 0; right: 0;
        bottom: 0; max-height: calc(100% - var(--map-header-height, 170px));
        overflow: hidden; border: 1px solid #cbd6cc; border-bottom: 0; border-radius: 16px 16px 0 0;
        box-shadow: 0 -4px 18px #18202620; background: #fff; visibility: hidden; pointer-events: none;
        will-change: transform; backface-visibility: hidden; transform: translate3d(0, 100%, 0);
        transition: transform 220ms cubic-bezier(.2,.8,.2,1), height 220ms cubic-bezier(.2,.8,.2,1), visibility 0s linear 220ms; }
      #app[data-map-sheet="expanded"] #details,
      #app[data-map-sheet="full"] #details {
        visibility: visible; pointer-events: auto; transform: translate3d(0, var(--sheet-drag-y, 0px), 0); transition-delay: 0s; }
      #details.is-dragging { transition: none; }
      .map-sheet-controls { position: relative; display: flex; align-items: center; justify-content: space-between;
        flex: 0 0 44px; padding: 0 8px; touch-action: none; background: #fff; cursor: grab; }
      .map-sheet-controls::before { content: ""; position: absolute; top: 6px; left: calc(50% - 17px);
        width: 34px; height: 4px; background: #c2cec2; border-radius: 3px; }
      #map-sheet-back { min-height: 38px; margin-top: 5px; padding: 6px 11px;
        border: 1px solid #cbd6cc; border-radius: 7px; background: #f8faf6;
        color: #1f6840; font-size: 12px; font-weight: 800; }
      #map-sheet-back:hover, #map-sheet-back:focus-visible { border-color: #94b69a; background: #edf5ed; outline: none; }
      #map-sheet-close { margin-left: auto; }
      #map-sheet-preview { display: block; padding: 0 18px 14px; touch-action: none; }
      #map-sheet-preview strong { display: block; font-size: 16px; line-height: 1.25; margin: 3px 0; }
      #map-sheet-preview .sheet-location { font-size: 13px; }
      #map-sheet-preview .sheet-facts { margin-top: 4px; color: #607067; font-size: 11px; line-height: 1.3; }
      #map-sheet-preview .sheet-update { margin-top: 6px; font-size: 12px; color: #47564c;
        display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
      #app[data-map-sheet="expanded"] #details { height: min(56%, 560px); }
      #app[data-map-sheet="full"] #details { height: 100%; max-height: 100%; border-radius: 0; }
      #app[data-map-sheet="full"] .map-sheet-controls { flex-basis: calc(44px + env(safe-area-inset-top));
        padding-top: env(safe-area-inset-top); }
      #app[data-map-sheet="expanded"] #map-sheet-preview,
      #app[data-map-sheet="full"] #map-sheet-preview { display: none; }
      #app[data-map-sheet="expanded"] #detail-content,
      #app[data-map-sheet="full"] #detail-content { overflow: auto; overscroll-behavior: contain;
        min-height: 0; -webkit-overflow-scrolling: touch; }
      #details .detail-panel { padding: 10px 14px 18px; }
      #details .detail-header { margin-bottom: 2px; }
      #details .detail-panel h2 { margin-bottom: 3px; font-size: 17px; }
      #details .detail-actions { margin-top: 7px; }
      #details .detail-section { margin-top: 10px; padding-top: 10px; }
      #details .detail-log { margin-top: 4px; }
      #details .detail-log li { padding: 7px 0; }
      #details .detail-grid dd { min-width: 0; overflow-wrap: anywhere; }
    }
    @media (max-width: 1000px) and (max-height: 500px) and (min-width: 560px) {
      #sidebar header { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 14px; align-items: center; }
      #sidebar .title-row { grid-column: 1; grid-row: 1; }
      #sidebar header > .meta { grid-column: 1; grid-row: 2; }
      #sidebar #connection-status { grid-column: 1; grid-row: 3; }
      #sidebar .range-tabs { grid-column: 2; grid-row: 1; margin: 0; }
      #sidebar .region-tabs { grid-column: 2; grid-row: 2 / 4; margin: 0; }
      #sidebar #stale-notice { grid-column: 1 / -1; }
      #map-sheet-preview .sheet-update { -webkit-line-clamp: 1; }
      #app[data-map-sheet="expanded"] #details { height: 50%; max-height: 100%; z-index: 850; }
    }
"""

MAP_WORKSPACE_HTML = """
    <div class="mobile-map-toolbar">
      <button type="button" id="map-list-toggle" aria-expanded="false" aria-controls="incident-list-shell">
        <svg viewBox="0 0 18 18" aria-hidden="true"><path d="M6 4h9M6 9h9M6 14h9M2.5 4h.1M2.5 9h.1M2.5 14h.1"></path></svg>
        <span id="map-activity-count" aria-live="polite">View incidents</span>
      </button>
    </div>
"""

MAP_SHEET_HTML = """
    <aside id="details" aria-label="Map details">
      <div class="map-sheet-controls">
        <button type="button" id="map-sheet-back" hidden>← Incidents</button>
        <button type="button" id="map-sheet-close" aria-label="Close details">×</button>
      </div>
      <div id="map-sheet-preview"></div>
      <div id="detail-content"></div>
    </aside>
"""

MAP_WORKSPACE_JS = r"""
    (() => {
      const shell = document.getElementById("app");
      const sheet = document.getElementById("details");
      const preview = document.getElementById("map-sheet-preview");
      const close = document.getElementById("map-sheet-close");
      const back = document.getElementById("map-sheet-back");
      const listToggle = document.getElementById("map-list-toggle");
      const listLabel = document.getElementById("map-activity-count");
      const listHandle = document.getElementById("incident-list-handle");
      const listClose = document.getElementById("incident-list-close");
      const toolbar = document.querySelector(".mobile-map-toolbar");
      const checkedMeta = document.querySelector(".checked-meta");
      const checkedHome = document.createComment("refresh control home");
      checkedMeta.before(checkedHome);
      const incidentSummary = document.getElementById("incident-summary");
      const summaryHome = document.createComment("incident summary home");
      incidentSummary.before(summaryHome);
      function placeRefreshControls() {
        if (mobileViewport.matches) {
          document.querySelector(".view-menu-popover").append(checkedMeta);
          const titleRow = document.querySelector("#sidebar .title-row");
          titleRow.insertBefore(incidentSummary, titleRow.querySelector(":scope > .view-header-actions"));
        } else {
          checkedHome.after(checkedMeta);
          summaryHome.after(incidentSummary);
        }
      }
      placeRefreshControls();
      mobileViewport.addEventListener("change", placeRefreshControls);
      let selection = null;
      let selectionKey = null;
      let returnFocus = null;
      let detailOrigin = "map";
      let incomingLink = new URLSearchParams(location.search).get("incident") || new URLSearchParams(location.search).get("camera");
      shell.dataset.mapSheet = "closed";
      shell.dataset.mapList = "closed";
      function setSheet(state) {
        shell.dataset.mapSheet = state;
      }
      function setList(next) {
        const state = next === true ? "open" : next === false ? "closed" : next;
        shell.dataset.mapList = state;
        const visible = state !== "closed";
        listToggle.setAttribute("aria-expanded", String(visible));
        listLabel.textContent = visible ? "Back to map" : (listToggle.dataset.closedLabel || "View incidents");
        if (visible) setSheet("closed");
        requestAnimationFrame(updateListScrollCue);
      }
      // Pan minimally at the existing zoom; never recenter just because a feed refreshed.
      function revealPoint(latlng) {
        if (!mobileViewport.matches || !latlng) return;
        requestAnimationFrame(() => {
          const p = map.latLngToContainerPoint(latlng);
          const bounds = mapEl.getBoundingClientRect();
          const top = 65;
          const bottom = Math.max(top + 44, Math.min(bounds.height - 48, sheet.getBoundingClientRect().top - bounds.top - 52));
          const x = Math.max(30, Math.min(bounds.width - 30, p.x));
          const y = Math.max(top, Math.min(bottom, p.y));
          if (Math.abs(p.x - x) > 1 || Math.abs(p.y - y) > 1) {
            map.panBy([p.x - x, p.y - y], {animate: true, duration: .28, easeLinearity: .35});
          }
        });
      }
      function showSelection(item, options = {}, camera = false) {
        const key = camera ? `camera:${item.id}` : item.event_key;
        selectionKey = key;
        selection = item.latitude != null && item.longitude != null ? [item.latitude, item.longitude] : null;
        if (camera) {
          const online = cameraIsOnline(item);
          const elevation = Number(item.elevation);
          preview.innerHTML = `<span class="status-pill ${online ? "status-reported" : "status-cleared"}">${online ? "Live camera" : "Offline"}</span>
            <span class="source-pill">ALERTCalifornia</span><strong>${escapeHtml(item.name || "Fire camera")}</strong>
            <span class="sheet-location">Facing ${escapeHtml(cameraDirectionLabel(item.az_current))}${Number.isFinite(elevation) ? ` · ${Math.round(elevation)} m` : ""}</span>
            <div class="sheet-update">Image updated ${escapeHtml(formatCameraUpdatedAt(item))}</div>`;
        } else {
          const entries = (item.detail_entries || []).filter(e => e.section !== "Unit Information");
          const latest = entries[entries.length - 1];
          const location = incidentLocationLines(item).primary || "Location unavailable";
          preview.innerHTML = `<span class="status-pill ${incidentStatusClass(item)}">${escapeHtml(incidentStatusLabel(item))}</span>
            <span class="source-pill">${escapeHtml(incidentSourceLabel(item))}</span><strong>${escapeHtml(item.type || "Incident")}</strong>
            <span class="sheet-location">${escapeHtml(location)}${selection ? "" : " · no map pin"}</span>
            <div class="sheet-facts">${escapeHtml(formatIncidentWhen(item))} · ${escapeHtml(item.area || "Unknown area")} · #${escapeHtml(item.incident_no || "—")}</div>
            <div class="sheet-update">${latest ? `${escapeHtml(latest.time)} · ${escapeHtml(latest.text)}` : "No additional updates captured."}</div>`;
        }
        if (!mobileViewport.matches) return;
        if (options.fromList) detailOrigin = "list";
        else if (options.userInitiated || options.openSheet) detailOrigin = "map";
        back.hidden = detailOrigin !== "list";
        const linked = incomingLink && incomingLink === (camera ? item.id : item.event_key);
        const requested = options.userInitiated || options.revealDetails || options.openSheet || linked;
        if (linked) incomingLink = null;
        if (requested) {
          returnFocus = document.activeElement;
          setList(false);
          setSheet("expanded");
          // Measure after the 220ms sheet transition so the marker clears the sheet's final edge.
          setTimeout(() => revealPoint(selection), 240);
        }
      }
      function closeSheet() {
        setSheet("closed");
        if (sheet.contains(document.activeElement)) {
          const returnStyle = returnFocus?.isConnected ? getComputedStyle(returnFocus) : null;
          if (returnStyle && returnStyle.display !== "none" && returnStyle.visibility !== "hidden" && returnFocus !== document.body) returnFocus.focus({preventScroll: true});
          else listToggle.focus({preventScroll: true});
        }
      }
      function backToResults() {
        const selected = selectionKey && document.querySelector(`.incident[data-event-key="${CSS.escape(selectionKey)}"]`);
        setSheet("closed");
        setList(true);
        requestAnimationFrame(() => selected?.focus({preventScroll: true}));
      }
      function showNearby(incident) {
        const anchor = map.latLngToContainerPoint([incident.latitude, incident.longitude]);
        const nearby = incidents.filter(item => {
          const marker = markers.get(item.event_key);
          if (!marker || !map.hasLayer(marker)) return false;
          const point = map.latLngToContainerPoint(marker.getLatLng());
          return Math.hypot(point.x - anchor.x, point.y - anchor.y) < 22;
        });
        if (nearby.length < 2) return false;
        clearCameraSelection();
        returnFocus = document.activeElement;
        selectionKey = null;
        selection = [incident.latitude, incident.longitude];
        setList(false);
        preview.innerHTML = '<strong>Several reports at this location</strong><span class="sheet-location">Open full details to choose an incident.</span>';
        detailsPanel.innerHTML = '<div class="detail-panel"><h2>Choose an incident</h2><p class="meta">Individual reports at this location</p><div data-overlap-choices></div></div>';
        const choices = detailsPanel.querySelector('[data-overlap-choices]');
        nearby.forEach(item => {
          const button = document.createElement('button');
          button.type = 'button'; button.className = 'map-overlap-choice';
          button.innerHTML = `<strong>${escapeHtml(item.type || 'Incident')}</strong><span>${escapeHtml(incidentStatusLabel(item))} · ${escapeHtml(incidentSourceLabel(item))} · ${escapeHtml(formatIncidentWhen(item))}</span><span>${escapeHtml(incidentLocationLines(item).primary)}</span>`;
          button.addEventListener('click', () => selectIncident(item, {pan: false, userInitiated: true, pulse: true}));
          choices.append(button);
        });
        setSheet('expanded');
        return true;
      }
      close.addEventListener("click", closeSheet);
      back.addEventListener("click", backToResults);
      listClose.addEventListener("click", () => setList(false));
      listToggle.addEventListener("pointerdown", event => event.preventDefault());
      listToggle.addEventListener("click", () => setList(shell.dataset.mapList === "closed" ? "open" : "closed"));
      function bindListDrag(dragSurface) {
        let start = null;
        dragSurface.addEventListener("pointerdown", event => {
          if (!mobileViewport.matches || !event.isPrimary || event.button !== 0) return;
          start = {y: event.clientY, id: event.pointerId, state: shell.dataset.mapList};
          dragSurface.setPointerCapture(event.pointerId);
          listShell.classList.add("is-dragging");
        });
        dragSurface.addEventListener("pointermove", event => {
          if (!start || event.pointerId !== start.id) return;
          const dy = event.clientY - start.y;
          listShell.style.setProperty("--list-drag-y", `${Math.max(-90, dy)}px`);
        });
        const finish = event => {
          if (!start || event.pointerId !== start.id) return;
          const dy = event.clientY - start.y;
          const state = start.state;
          start = null;
          listShell.classList.remove("is-dragging");
          listShell.style.removeProperty("--list-drag-y");
          if (dy < -45) setList("expanded");
          else if (dy > 150 && state === "expanded") setList("closed");
          else if (dy > 45) setList(state === "expanded" ? "open" : "closed");
          else setList(state);
        };
        dragSurface.addEventListener("pointerup", finish);
        dragSurface.addEventListener("pointercancel", event => {
          if (!start) return;
          listShell.classList.remove("is-dragging");
          listShell.style.removeProperty("--list-drag-y");
          start = null;
        });
      }
      bindListDrag(listHandle);
      // Capture gestures only on the handle and compact preview, never on the scrollable record/form.
      for (const surface of [sheet.querySelector(".map-sheet-controls"), preview]) {
        let start = null;
        let suppressClickUntil = 0;
        surface.addEventListener("click", event => {
          if (Date.now() < suppressClickUntil) { event.preventDefault(); event.stopImmediatePropagation(); }
        }, {capture: true});
        surface.addEventListener("pointerdown", event => {
          if (!mobileViewport.matches || !event.isPrimary || event.button !== 0) return;
          start = {x: event.clientX, y: event.clientY, id: event.pointerId,
            height: sheet.getBoundingClientRect().height};
          sheet.classList.add("is-dragging");
          // Capturing on the original button preserves its normal click target.
          (event.target.closest("button") || surface).setPointerCapture(event.pointerId);
        });
        surface.addEventListener("pointermove", event => {
          if (!start || event.pointerId !== start.id) return;
          const dy = event.clientY - start.y;
          const maximum = Math.max(180, shell.getBoundingClientRect().height);
          sheet.style.height = `${Math.max(92, Math.min(maximum, start.height - dy))}px`;
        });
        surface.addEventListener("pointercancel", () => {
          start = null;
          sheet.classList.remove("is-dragging");
          sheet.style.removeProperty("height");
        });
        surface.addEventListener("pointerup", event => {
          if (!start || event.pointerId !== start.id) return;
          const dy = event.clientY - start.y, dx = event.clientX - start.x;
          start = null;
          sheet.classList.remove("is-dragging");
          if (Math.abs(dy) < 24 || Math.abs(dy) < Math.abs(dx) * 1.3) {
            sheet.style.removeProperty("height");
            return;
          }
          event.preventDefault();
          const current = shell.dataset.mapSheet;
          const target = dy < 0 ? "full" : current === "full" ? "expanded" : "closed";
          setSheet(target);
          requestAnimationFrame(() => sheet.style.removeProperty("height"));
          // A swipe ending on a button must not also activate its click.
          suppressClickUntil = Date.now() + 350;
        });
      }
      document.addEventListener("keydown", event => {
        if (event.key !== "Escape" || !mobileViewport.matches) return;
        if (shell.dataset.mapSheet === "expanded") closeSheet();
        else if (shell.dataset.mapList === "open") { setList(false); listToggle.focus(); }
      });
      function measureToolbar() {
        shell.style.setProperty("--map-toolbar-height", `${toolbar.getBoundingClientRect().height}px`);
        shell.style.setProperty("--map-header-height", `${document.querySelector('#sidebar header').getBoundingClientRect().height}px`);
        map.invalidateSize({pan: false});
      }
      const workspaceObserver = new ResizeObserver(measureToolbar);
      workspaceObserver.observe(toolbar);
      workspaceObserver.observe(document.querySelector('#sidebar header'));
      mobileViewport.addEventListener("change", measureToolbar);
      function resetView() {
        pauseUserLocationFollowing();
        const roads = currentRegion === "forest"
          ? Object.values(roadwayMileMarkers).flat().map(p => [p[1], p[2]])
          : Object.values(offlineRoadData).flat(2);
        if (roads.length) map.fitBounds(L.latLngBounds(roads), {paddingTopLeft: [24, 60], paddingBottomRight: [24, 45], animate: false});
      }
      // Only a genuinely new map view is fitted. Saved views and incoming incident links retain their context.
      if (!restoredMapView && !new URLSearchParams(location.search).has("incident") && !new URLSearchParams(location.search).has("camera")) {
        requestAnimationFrame(resetView);
      }
      window.chpLiveMap.workspace = {showSelection, showNearby, closeSheet, setSheet, revealPoint, resetView,
        updateCount() {
          const total = currentDataStatus.total_count ?? incidents.length;
          const label = `${total} incident${total === 1 ? "" : "s"}`;
          listToggle.dataset.closedLabel = label;
          if (shell.dataset.mapList === "closed") listLabel.textContent = label;
        }};
      window.chpLiveMap.workspace.updateCount();
    })();
"""
