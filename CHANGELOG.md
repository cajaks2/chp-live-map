# Changelog

This file records notable user-facing and operational changes to Crestmap.

The project did not previously maintain Git tags or a changelog. Entries through
0.1.192 were reconstructed from commit dates, commit messages, and version values
in the Makefile and deployment manifests. Dates below are supporting commit dates,
not independently recorded production deployment times. Merge-only commits are
omitted in favor of their underlying change commits. Range headings group related
development eras and do not imply that every intermediate version number shipped.

## Unreleased

- Keep only the primary Analytics destination on the installed Google tag; move
  the duplicate secondary property and empty Analytics account to Trash.

## 0.1.204 - 2026-09-04

- Pass Analytics dimensions and internal/developer flags directly to the tag
  configuration so initial pageviews receive the same context as interactions.

## 0.1.203 - 2026-09-04

- Measure deliberate incident/camera selections, region changes, copied links and
  successful new alert subscriptions with restricted event parameters.
- Stabilize Analytics page titles and omit query strings from reported page URLs;
  disable history-based pageviews, automatic search and form measurement in both
  production streams to keep interactions distinct from document loads.
- Add persistent browser modes for internal/developer traffic and automatically
  label admin views. Keep exclusion filters in Testing in both GA4 properties.
- Label the two Analytics properties Primary and Secondary and register usage
  dimensions in the primary property; document configuration and rollback.

## 0.1.202 - 2026-09-04

- Include the configured Google Analytics tag on Summary, History, and About
  pages as well as the map, restoring consistent pageview coverage.
- Use the Google-provided installation tag in production to restore loading
  and route events to the connected Crestmap Analytics destination.

## 0.1.201 - 2026-09-03

- Fix comment form fields overlapping or extending past the panel edge, and
  keep name and contact inputs aligned when their labels wrap.

## 0.1.200 - 2026-09-02

- Refresh ALERTCalifornia camera metadata while the map is active and when a
  backgrounded tab becomes visible again, preventing live cameras from being
  marked stale based on an old page-load snapshot.
- Open current camera images in a responsive in-app full-screen viewer that
  stays synchronized with automatic image refreshes, with the direct image link
  retained as a fallback.

## 0.1.199 - 2026-09-02

- Added an optional ALERTCalifornia camera layer to the live map. Camera markers
  show their current viewing direction, selection reveals a bounded field-of-view
  fan, and the existing detail panel displays the uncropped current image with
  source attribution and a link to the ALERTCalifornia viewer.
- Added the camera layer toggle to the map menu, separated collocated cameras,
  aligned field-of-view fans with their visible marker dots, and credited
  ALERTCalifornia and UC San Diego on the About page.

## 0.1.198 - 2026-09-02

- Fixed the service worker's Content Security Policy so it can cache the pinned
  unpkg Leaflet assets and successfully activate for cold offline launches.
- Clean up incomplete application-shell caches when installation fails and test
  that the worker policy permits its required external downloads.

## 0.1.197 - 2026-09-01

- Added versioned application-shell caching so the installed app can launch
  after being force-closed while the device is offline.
- Cached the existing pinned unpkg Leaflet JavaScript and CSS without moving
  those files onto Crestmap hosting.
- Registered offline support independently of push-notification availability
  and added a simulated unreachable-origin cold-navigation test.

## 0.1.196 - 2026-09-01

- Added durable last-known incident snapshots for each map region and history
  window, with automatic refresh and recovery when connectivity returns.
- Added explicit online, reconnecting, and offline status with the saved-data
  timestamp instead of relying only on a generic stale-data warning.
- Added a lightweight bundled road-and-boundary basemap for Forest and Malibu so
  incidents, mile markers, and user location remain geographically useful when
  OpenStreetMap raster tiles are unavailable.
- Added offline snapshot, connection-state recovery, and bundled-basemap tests.

## 0.1.195 - 2026-08-31

- Fixed navigation menus being clipped on iPhone; menus now stay within the
  visible viewport and scroll independently when space is limited.

## 0.1.194 - 2026-08-30

- Added a Corners link to the navigation menu for the corner crash-count map.

## 0.1.193 - 2026-08-30

- Added this repository changelog and backfilled its release history.
- Required future agents/contributors to maintain Unreleased entries and move them
  into dated version sections when preparing releases.
- Added configurable rolling admin sessions, an optional 30-day remembered-device
  login, and a sessions page for per-device and all-device revocation.
- Prevented background map polling from renewing admin sessions; only active
  interaction renews normal sessions, with a fixed maximum lifetime.
- Wired session lifetime settings into DigitalOcean Compose and Kubernetes.
- Fixed login-card overflow on narrow phone screens.
- Added persisted, hashed session records. Existing browser logins require one
  fresh sign-in after this database migration; logout now revokes server-side
  access as well as clearing the cookie.

## 0.1.192 - 2026-08-30

- Made location following opt-in on every page load. When geolocation permission
  is already granted, the blue dot updates quietly without moving the map; the
  location button enables centering and follow mode
  ([ce716fa](https://github.com/cajaks2/chp-live-map/commit/ce716fa5c2291beeab9a9aa8d481229fc4a04864)).

## 0.1.191 - 2026-08-29

- Expanded official mile-marker coverage for GMR, GRR, Highway 39/San Gabriel
  Canyon, and Mount Baldy Road, and displayed every trusted marker at zoom 16+.
- Auto-published new incident comments and approved media by default, with an
  environment switch available to restore pre-publication moderation
  ([426259c](https://github.com/cajaks2/chp-live-map/commit/426259c6016d8bf5dc8b064a8a58f99542e5549c)).

## 0.1.190 - 2026-08-29

- Added continuously updating browser geolocation, a blue location marker,
  accuracy circle, follow mode, and map-interaction pause behavior
  ([ffd8bb0](https://github.com/cajaks2/chp-live-map/commit/ffd8bb0)).

## 0.1.189 - 2026-08-28

- Expanded official marker coverage across Angeles Crest, Angeles Forest, Big
  Tujunga, and Upper Big Tujunga
  ([e7419ef](https://github.com/cajaks2/chp-live-map/commit/e7419ef)).

## 0.1.188 - 2026-08-28

- Added subtle roadway mile-marker overlays with zoom-based sampling
  ([c0b2f46](https://github.com/cajaks2/chp-live-map/commit/c0b2f46)).
- Included the marker dataset in the production image
  ([908d188](https://github.com/cajaks2/chp-live-map/commit/908d188)).

## 0.1.187 - 2026-08-25

- Excluded US-101 freeway incidents from Malibu results
  ([f81a209](https://github.com/cajaks2/chp-live-map/commit/f81a209)).

## 0.1.175-0.1.186 - 2026-08-11 to 2026-08-17

- Added WildWeb/CAANCC as a second incident source with independent collection,
  storage, map display, and conservative source-aware statuses
  ([0cbee0b](https://github.com/cajaks2/chp-live-map/commit/0cbee0b)).
- Improved descriptions and sorting while keeping WildWeb reports out of CHP
  active counts ([cc5c9af](https://github.com/cajaks2/chp-live-map/commit/cc5c9af),
  [83dd658](https://github.com/cajaks2/chp-live-map/commit/83dd658)).
- Distinguished archived and aging WildWeb reports
  ([f4decc2](https://github.com/cajaks2/chp-live-map/commit/f4decc2),
  [8e84a00](https://github.com/cajaks2/chp-live-map/commit/8e84a00),
  [4086145](https://github.com/cajaks2/chp-live-map/commit/4086145)).
- Preserved cleared details and kept the mobile detail cue near the map bottom
  ([1f5cb05](https://github.com/cajaks2/chp-live-map/commit/1f5cb05),
  [cf28a79](https://github.com/cajaks2/chp-live-map/commit/cf28a79)).
- Unified CHP/WildWeb metrics and graphed WildWeb response codes
  ([953670f](https://github.com/cajaks2/chp-live-map/commit/953670f),
  [1f5d8db](https://github.com/cajaks2/chp-live-map/commit/1f5d8db)).
- Prioritized CHP road locations, scoped Cloudflare caching, and synchronized the
  0.1.186 release ([30647f5](https://github.com/cajaks2/chp-live-map/commit/30647f5),
  [c97cc07](https://github.com/cajaks2/chp-live-map/commit/c97cc07),
  [99218e6](https://github.com/cajaks2/chp-live-map/commit/99218e6)).

## 0.1.164-0.1.174 - 2026-08-07 to 2026-08-09

- Added LASD and LA County Fire rescue-helicopter tracking
  ([38bf844](https://github.com/cajaks2/chp-live-map/commit/38bf844),
  [66f3c2c](https://github.com/cajaks2/chp-live-map/commit/66f3c2c)).
- Added recent/current flight trails, then rendered them as one smooth path
  ([3cf994a](https://github.com/cajaks2/chp-live-map/commit/3cf994a),
  [4cff646](https://github.com/cajaks2/chp-live-map/commit/4cff646),
  [4de8fba](https://github.com/cajaks2/chp-live-map/commit/4de8fba)).
- Refined aircraft markers and retained stale positions longer
  ([973049a](https://github.com/cajaks2/chp-live-map/commit/973049a),
  [b29154c](https://github.com/cajaks2/chp-live-map/commit/b29154c),
  [06edd1f](https://github.com/cajaks2/chp-live-map/commit/06edd1f)).
- Refreshed data when the app resumes and reloaded on deployed-version changes
  ([1dc2569](https://github.com/cajaks2/chp-live-map/commit/1dc2569),
  [a7ae9de](https://github.com/cajaks2/chp-live-map/commit/a7ae9de)).

## 0.1.148-0.1.163 - 2026-08-06 to 2026-08-07

- Added configurable browser push alerts, device testing, VAPID handling, alert
  controls, installation guidance, header status, and unread badges
  ([f5ad653](https://github.com/cajaks2/chp-live-map/commit/f5ad653),
  [16c169d](https://github.com/cajaks2/chp-live-map/commit/16c169d),
  [a951f99](https://github.com/cajaks2/chp-live-map/commit/a951f99),
  [9263dbb](https://github.com/cajaks2/chp-live-map/commit/9263dbb),
  [3d93af8](https://github.com/cajaks2/chp-live-map/commit/3d93af8),
  [a1af5cd](https://github.com/cajaks2/chp-live-map/commit/a1af5cd),
  [bba04d0](https://github.com/cajaks2/chp-live-map/commit/bba04d0)).
- Improved iPhone onboarding and mobile navigation
  ([ac3995e](https://github.com/cajaks2/chp-live-map/commit/ac3995e),
  [c3574eb](https://github.com/cajaks2/chp-live-map/commit/c3574eb)).
- Added west-Crest filtering, time-sensitive priority, and push metrics
  ([b67d8c4](https://github.com/cajaks2/chp-live-map/commit/b67d8c4),
  [ced2577](https://github.com/cajaks2/chp-live-map/commit/ced2577),
  [f8e47d9](https://github.com/cajaks2/chp-live-map/commit/f8e47d9)).
- Excluded the CA-14 corridor and tightened its cutoff
  ([1185418](https://github.com/cajaks2/chp-live-map/commit/1185418),
  [3651b4e](https://github.com/cajaks2/chp-live-map/commit/3651b4e)).

## 0.1.134-0.1.147 - 2026-07-18 to 2026-08-05

- Added moderated public comments and the moderation admin UI
  ([e7c4501](https://github.com/cajaks2/chp-live-map/commit/e7c4501),
  [7c27df5](https://github.com/cajaks2/chp-live-map/commit/7c27df5)).
- Added moderator IP visibility, admin sessions, and hidden incident history
  ([e818a23](https://github.com/cajaks2/chp-live-map/commit/e818a23),
  [26f3e42](https://github.com/cajaks2/chp-live-map/commit/26f3e42)).
- Added moderated R2 photo/video uploads and new-incident logging
  ([39db8c6](https://github.com/cajaks2/chp-live-map/commit/39db8c6),
  [e88e42a](https://github.com/cajaks2/chp-live-map/commit/e88e42a)).
- Preserved comment drafts and refined their incident-detail placement
  ([1ad74be](https://github.com/cajaks2/chp-live-map/commit/1ad74be),
  [75ccf8e](https://github.com/cajaks2/chp-live-map/commit/75ccf8e),
  [336bd1f](https://github.com/cajaks2/chp-live-map/commit/336bd1f)).
- Fixed proxy moderation, contact/header clipping, authenticated routes, and admin
  tab continuity ([62f0f48](https://github.com/cajaks2/chp-live-map/commit/62f0f48),
  [608ef98](https://github.com/cajaks2/chp-live-map/commit/608ef98),
  [eef12ec](https://github.com/cajaks2/chp-live-map/commit/eef12ec),
  [9139b8f](https://github.com/cajaks2/chp-live-map/commit/9139b8f),
  [dce7437](https://github.com/cajaks2/chp-live-map/commit/dce7437)).

## 0.1.115-0.1.133 - 2026-06-28 to 2026-07-12

- Migrated the production web app to FastAPI and gunicorn
  ([1650101](https://github.com/cajaks2/chp-live-map/commit/1650101)).
- Added foothill boundary filtering and a boundary preview
  ([228e1b0](https://github.com/cajaks2/chp-live-map/commit/228e1b0),
  [0171475](https://github.com/cajaks2/chp-live-map/commit/0171475)).
- Added summary filters, Malibu road buckets, and daily-chart refinements
  ([bab743e](https://github.com/cajaks2/chp-live-map/commit/bab743e),
  [926e1d0](https://github.com/cajaks2/chp-live-map/commit/926e1d0),
  [68736cf](https://github.com/cajaks2/chp-live-map/commit/68736cf),
  [29484b6](https://github.com/cajaks2/chp-live-map/commit/29484b6)).
- Added XML freshness tracking and stale-feed fallback
  ([2a7fee8](https://github.com/cajaks2/chp-live-map/commit/2a7fee8),
  [498c806](https://github.com/cajaks2/chp-live-map/commit/498c806),
  [9ee9d5b](https://github.com/cajaks2/chp-live-map/commit/9ee9d5b)).
- Excluded Valley Topanga and north-of-101 Malibu false positives
  ([065424b](https://github.com/cajaks2/chp-live-map/commit/065424b),
  [107cc07](https://github.com/cajaks2/chp-live-map/commit/107cc07)).
- Added source-attempt metrics, serialized schema setup, clarified timestamps, and
  reorganized Grafana ([0251450](https://github.com/cajaks2/chp-live-map/commit/0251450),
  [f381a7b](https://github.com/cajaks2/chp-live-map/commit/f381a7b),
  [de224dc](https://github.com/cajaks2/chp-live-map/commit/de224dc),
  [741dc2c](https://github.com/cajaks2/chp-live-map/commit/741dc2c)).

## 0.1.90-0.1.114 - 2026-06-11 to 2026-06-28

- Promoted Malibu from preview to a public region with viewport, URL, counts, and
  badge support ([363a2c8](https://github.com/cajaks2/chp-live-map/commit/363a2c8),
  [c70218c](https://github.com/cajaks2/chp-live-map/commit/c70218c),
  [48cfbe1](https://github.com/cajaks2/chp-live-map/commit/48cfbe1),
  [07fa1bd](https://github.com/cajaks2/chp-live-map/commit/07fa1bd)).
- Added XML shadow comparisons and source-aware timings
  ([b0cf5d2](https://github.com/cajaks2/chp-live-map/commit/b0cf5d2),
  [401a248](https://github.com/cajaks2/chp-live-map/commit/401a248),
  [7e2bbba](https://github.com/cajaks2/chp-live-map/commit/7e2bbba),
  [663b593](https://github.com/cajaks2/chp-live-map/commit/663b593)).
- Added support for bookmarked incidents outside the selected history window
  ([13e04fa](https://github.com/cajaks2/chp-live-map/commit/13e04fa)).
- Made XML the primary scraper with CAD fallback and reduced redundant detail work
  ([b4e17a0](https://github.com/cajaks2/chp-live-map/commit/b4e17a0),
  [851acdf](https://github.com/cajaks2/chp-live-map/commit/851acdf),
  [eb859e8](https://github.com/cajaks2/chp-live-map/commit/eb859e8),
  [87df166](https://github.com/cajaks2/chp-live-map/commit/87df166)).
- Added pooled Postgres connections and pool metrics while reducing web-metrics
  overhead ([5c31496](https://github.com/cajaks2/chp-live-map/commit/5c31496),
  [63232f8](https://github.com/cajaks2/chp-live-map/commit/63232f8),
  [85d6b84](https://github.com/cajaks2/chp-live-map/commit/85d6b84)).

## 0.1.64-0.1.89 - 2026-06-07 to 2026-06-11

- Added JSON incident loading plus Summary, History, and About views
  ([e024d38](https://github.com/cajaks2/chp-live-map/commit/e024d38),
  [f3d76b4](https://github.com/cajaks2/chp-live-map/commit/f3d76b4)).
- Added Malibu collection, bounds, Ventura coverage, and per-region metrics
  ([4d83110](https://github.com/cajaks2/chp-live-map/commit/4d83110),
  [495e22b](https://github.com/cajaks2/chp-live-map/commit/495e22b),
  [6d185a3](https://github.com/cajaks2/chp-live-map/commit/6d185a3),
  [8679204](https://github.com/cajaks2/chp-live-map/commit/8679204)).
- Added summary time buckets and weekday labels
  ([8ed173c](https://github.com/cajaks2/chp-live-map/commit/8ed173c),
  [990f362](https://github.com/cajaks2/chp-live-map/commit/990f362)).
- Refined selected markers and repaired Leaflet positioning
  ([2329e99](https://github.com/cajaks2/chp-live-map/commit/2329e99),
  [98c7c5b](https://github.com/cajaks2/chp-live-map/commit/98c7c5b),
  [3cef35b](https://github.com/cajaks2/chp-live-map/commit/3cef35b),
  [277cbd1](https://github.com/cajaks2/chp-live-map/commit/277cbd1)).
- Expanded forest matching while constraining Highway 39, Mount Wilson, coordinate
  bounds, La Tuna, and other false positives
  ([c428a45](https://github.com/cajaks2/chp-live-map/commit/c428a45),
  [2bfd757](https://github.com/cajaks2/chp-live-map/commit/2bfd757),
  [bdeb922](https://github.com/cajaks2/chp-live-map/commit/bdeb922),
  [22a2b3c](https://github.com/cajaks2/chp-live-map/commit/22a2b3c),
  [8232aa5](https://github.com/cajaks2/chp-live-map/commit/8232aa5),
  [be2f091](https://github.com/cajaks2/chp-live-map/commit/be2f091)).

## 0.1.32-0.1.63 - 2026-05-31 to 2026-06-07

- Added DigitalOcean Compose deployment, health checks, backups, and a long-running
  metrics-enabled scraper ([3549ce5](https://github.com/cajaks2/chp-live-map/commit/3549ce5),
  [61a5f48](https://github.com/cajaks2/chp-live-map/commit/61a5f48),
  [58d17f0](https://github.com/cajaks2/chp-live-map/commit/58d17f0),
  [da15f61](https://github.com/cajaks2/chp-live-map/commit/da15f61)).
- Added search/JSON-LD/social metadata, analytics hooks, and the `crestmap.us`
  canonical domain ([b27fe40](https://github.com/cajaks2/chp-live-map/commit/b27fe40),
  [deab2d2](https://github.com/cajaks2/chp-live-map/commit/deab2d2),
  [d08448d](https://github.com/cajaks2/chp-live-map/commit/d08448d),
  [56dd674](https://github.com/cajaks2/chp-live-map/commit/56dd674),
  [f0d3778](https://github.com/cajaks2/chp-live-map/commit/f0d3778)).
- Added remembered refresh controls, scraper metrics, and richer sharing metadata
  ([05b7e18](https://github.com/cajaks2/chp-live-map/commit/05b7e18),
  [4144610](https://github.com/cajaks2/chp-live-map/commit/4144610)).
- Improved mobile layout, details cues, sharing, refresh cadence, security headers,
  and iOS map behavior ([377a475](https://github.com/cajaks2/chp-live-map/commit/377a475),
  [424ced3](https://github.com/cajaks2/chp-live-map/commit/424ced3),
  [0d037ef](https://github.com/cajaks2/chp-live-map/commit/0d037ef),
  [1c290db](https://github.com/cajaks2/chp-live-map/commit/1c290db)).

## 0.1.0-0.1.31 - 2026-05-31

- Created the CHP scraper, live map, detail view, coordinate parsing, and Leaflet
  presentation ([db249de](https://github.com/cajaks2/chp-live-map/commit/db249de),
  [decd923](https://github.com/cajaks2/chp-live-map/commit/decd923),
  [539d808](https://github.com/cajaks2/chp-live-map/commit/539d808),
  [e310a35](https://github.com/cajaks2/chp-live-map/commit/e310a35)).
- Added Kubernetes scraper/web separation, pushed images, ECS logging, and ingress
  ([12c4e7d](https://github.com/cajaks2/chp-live-map/commit/12c4e7d),
  [d9fbaab](https://github.com/cajaks2/chp-live-map/commit/d9fbaab),
  [9c31695](https://github.com/cajaks2/chp-live-map/commit/9c31695),
  [1a74014](https://github.com/cajaks2/chp-live-map/commit/1a74014)).
- Added tests, deployment automation, history presets, deep links, stale-data
  checks, full detail sections, and database resource limits
  ([ef04517](https://github.com/cajaks2/chp-live-map/commit/ef04517),
  [b3bb4cc](https://github.com/cajaks2/chp-live-map/commit/b3bb4cc),
  [37c6d97](https://github.com/cajaks2/chp-live-map/commit/37c6d97),
  [763e368](https://github.com/cajaks2/chp-live-map/commit/763e368),
  [1304c05](https://github.com/cajaks2/chp-live-map/commit/1304c05),
  [e5e592b](https://github.com/cajaks2/chp-live-map/commit/e5e592b),
  [b7896ae](https://github.com/cajaks2/chp-live-map/commit/b7896ae)).
- Extended history to 72 hours and refined filtering, mobile interactions, cards,
  cache behavior, logging, and scraper politeness
  ([4c1a5d3](https://github.com/cajaks2/chp-live-map/commit/4c1a5d3),
  [440f2a0](https://github.com/cajaks2/chp-live-map/commit/440f2a0),
  [004dc0c](https://github.com/cajaks2/chp-live-map/commit/004dc0c),
  [cc18d27](https://github.com/cajaks2/chp-live-map/commit/cc18d27),
  [2f096cc](https://github.com/cajaks2/chp-live-map/commit/2f096cc)).

The complete ungrouped history remains available in
[GitHub's commit log](https://github.com/cajaks2/chp-live-map/commits/main/).
