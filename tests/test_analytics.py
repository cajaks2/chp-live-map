import json
import re
import shutil
import subprocess

import pytest

from generate_live_map import analytics_script, build_html


def run_node(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Analytics runtime tests")
    subprocess.run([node, "-e", source], check=True, capture_output=True, text=True)


def test_analytics_sanitizes_events_and_labels_internal_and_developer_visits():
    script = re.search(r"<script>(.*?)</script>", analytics_script("G-TEST123"), re.S)[1]
    admin_script = re.search(
        r"<script>(.*?)</script>", analytics_script("G-TEST123", admin_mode=True), re.S
    )[1]
    run_node(r'''
const assert = require("node:assert/strict");
const vm = require("node:vm");
function load(script, {query = "", savedMode = null, storageFails = false} = {}) {
  const handlers = {};
  const saved = new Map(savedMode ? [["crestmap-analytics-mode", savedMode]] : []);
  const window = {location: {href: "https://crestmap.us/?incident=private&q=private" + query},
    history: {state: {incident: "keep"}, replaceState(state, title, url) {
      assert.equal(state.incident, "keep"); this.url = url;
    }}};
  const sandbox = {window, URL, Date, Set,
    document: {referrer: "https://example.test/path?contact=private", addEventListener: (name, fn) => handlers[name] = fn},
    localStorage: {getItem: k => {if(storageFails) throw Error("disabled"); return saved.get(k);},
      setItem: (k,v) => {if(storageFails) throw Error("disabled"); saved.set(k,v);}}};
  Object.defineProperty(sandbox, "dataLayer", {get: () => window.dataLayer});
  vm.runInNewContext(script, sandbox);
  return {window, saved, handlers, calls: window.dataLayer.map(a => Array.from(a))};
}
''' + f"const script = {json.dumps(script)}; const adminScript = {json.dumps(admin_script)};\n" + r'''
const visit = load(script, {query: "&utm_source=newsletter&utm_campaign=forest-update&utm_content=private%40example.test"});
assert.equal(visit.calls.filter(c => c[0] === "config").length, 1);
const common = visit.calls.find(c => c[0] === "set")[1];
assert.equal(common.page_location, "https://crestmap.us/");
assert.equal(common.page_referrer, "https://example.test/path");
assert.equal(common.campaign_source, "newsletter");
assert.equal(common.campaign_name, "forest-update");
assert.equal(common.campaign_content, undefined);
assert.equal(common.traffic_type, undefined);
assert.equal(common.debug_mode, undefined);
visit.window.crestmapTrack("incident_select", {incident_source: "chp", name: "PRIVATE", contact: "PRIVATE", latitude: 34, page_location: "PRIVATE"});
visit.window.crestmapTrack("camera_open", {camera_status: "PRIVATE"});
visit.window.crestmapTrack("unapproved_event", {text: "PRIVATE"});
const events = visit.window.dataLayer.map(a => Array.from(a)).filter(c => c[0] === "event");
assert.equal(events.length, 2);
assert.equal(events[0][2].incident_source, "chp");
assert.equal(events[1][2].camera_status, undefined);
assert.ok(!JSON.stringify(events).includes("PRIVATE"));
assert.ok(!JSON.stringify(events).includes("private"));
visit.handlers.click({target: {closest: () => ({href: "https://crestmap.us/?region=malibu"})}});
assert.equal(visit.window.dataLayer.at(-1)[1], "region_change");
assert.equal(visit.window.dataLayer.at(-1)[2].target_region, "malibu");
const count = visit.window.dataLayer.length;
visit.handlers.click({target: {closest: () => ({href: "https://crestmap.us/?region=forest"})}});
assert.equal(visit.window.dataLayer.length, count);
const internal = load(script, {query: "&analytics_mode=internal"});
assert.equal(internal.calls.find(c => c[0] === "set")[1].traffic_type, "internal");
assert.equal(internal.saved.get("crestmap-analytics-mode"), "internal");
assert.ok(!internal.window.history.url.includes("analytics_mode"));
assert.ok(internal.window.history.url.includes("incident=private"));
assert.equal(load(script, {savedMode: "internal"}).calls.find(c => c[0] === "set")[1].traffic_type, "internal");
assert.equal(load(script, {query: "&analytics_mode=visitor", savedMode: "internal"}).calls.find(c => c[0] === "set")[1].traffic_type, undefined);
assert.equal(load(script, {query: "&analytics_mode=developer", storageFails: true}).calls.find(c => c[0] === "set")[1].debug_mode, true);
assert.equal(load(adminScript).calls.find(c => c[0] === "set")[1].traffic_type, "internal");
''')


def test_rendered_scripts_parse_and_copy_event_requires_success():
    rendered = build_html([], "2026-09-04T12:00:00Z", 72, google_analytics_id="G-TEST123")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", rendered, re.S)
    # JSON-LD is data, while all other inline scripts must parse as JavaScript.
    scripts = [s for s in scripts if s.strip() and not s.lstrip().startswith('{"@context"')]
    copy_function = re.search(r"    async function copyIncidentLink\(.*?\n    }", rendered, re.S)[0]
    run_node(f"const scripts = {json.dumps(scripts)}; for (const script of scripts) new Function(script);\n" + r'''
const assert = require("node:assert/strict");
const events = [];
const window = {crestmapTrack: (...args) => events.push(args), setTimeout: () => {}, prompt: () => {}};
Object.defineProperty(globalThis, "navigator", {value: {clipboard: {writeText: async () => {}}}, configurable: true});
const incidentUrl = () => new URL("https://crestmap.us/?incident=synthetic");
''' + copy_function + r'''
(async () => {
  await copyIncidentLink({}, {});
  assert.equal(events.length, 1);
  assert.equal(events[0][0], "share");
  navigator.clipboard.writeText = async () => {throw Error("denied")};
  await copyIncidentLink({}, {});
  assert.equal(events.length, 1);
})().catch(e => {console.error(e); process.exit(1)});
''')


def test_subscription_event_only_fires_after_first_successful_registration():
    rendered = build_html([], "2026-09-04T12:00:00Z", 72)
    handler = re.search(
        r'    form\?\.addEventListener\("submit", async event => \{.*?\n    \}\);', rendered, re.S
    )[0]
    run_node(r'''
const assert = require("node:assert/strict");
const events = [];
let submit;
const form = {addEventListener: (event, handler) => submit = handler};
const selected = () => ["synthetic"];
const setBusy = () => {};
const Notification = {requestPermission: async () => "granted"};
const registration = {pushManager: {getSubscription: async () => ({toJSON: () => ({endpoint: "private"})})}};
let currentSubscription;
let serverSubscribed = false;
let shouldFail = false;
const postSubscription = async () => {if (shouldFail) throw Error("server failure")};
const window = {crestmapTrack: (...args) => events.push(args)};
const status = {}, saveButton = {}, testButton = {}, disableButton = {};
const renderHeaderAlertState = () => {};
''' + handler + r'''
(async () => {
  shouldFail = true;
  await submit({preventDefault: () => {}});
  assert.equal(events.length, 0);
  assert.equal(serverSubscribed, false);
  shouldFail = false;
  await submit({preventDefault: () => {}});
  assert.deepEqual(events, [["alert_subscribe"]]);
  await submit({preventDefault: () => {}});
  assert.equal(events.length, 1, "saving existing preferences must not count as a new subscription");
})().catch(e => {console.error(e); process.exit(1)});
''')


def test_automatic_selections_do_not_count_as_user_interactions():
    rendered = build_html([], "2026-09-04T12:00:00Z", 72)
    functions = "\n".join(
        re.search(r"    function " + name + r"\(.*?\n    }", rendered, re.S)[0]
        for name in ["selectIncident", "selectCamera"]
    )
    run_node(r'''
const assert = require("node:assert/strict");
const events = [];
let pauses = 0, selectedIncidentKey, selectedCamera, selectedCameraId, cameraImageRefreshTimer;
const window = {crestmapTrack: (...args) => events.push(args), setInterval: () => 1};
const document = {querySelectorAll: () => []};
const detailsPanel = {dataset: {}};
const detailsCue = null, adminMode = false;
const markers = new Map(), cameraMarkers = new Map();
const clearCameraSelection = () => {};
const pauseUserLocationFollowing = () => pauses++;
const detailHtml = () => "";
const loadComments = () => {};
const cameraDetailHtml = () => "";
const bindCameraImageLightbox = () => {};
const renderSelectedCameraFov = () => {};
const refreshSelectedCameraImage = () => {};
const cameraIsOnline = c => c.online;
''' + functions + r'''
const automatic = {pan: false, updateUrl: false};
const user = {...automatic, userInitiated: true};
selectIncident({event_key: "test", source: "chp"}, automatic);
selectCamera({id: "test", online: true}, automatic);
assert.equal(events.length, 0);
selectIncident({event_key: "test", source: "wildweb"}, user);
selectCamera({id: "test", online: false}, user);
assert.equal(events.length, 2);
assert.deepEqual(events[0], ["incident_select", {incident_source: "wildweb"}]);
assert.deepEqual(events[1], ["camera_open", {camera_status: "offline"}]);
assert.equal(pauses, 2);
selectIncident({event_key: "test", source: "chp"}, automatic);
selectCamera({id: "test", online: true}, automatic);
assert.equal(events.length, 2, "background refreshes must not add interaction events");
''')
