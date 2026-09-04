# Crestmap Analytics

## Reporting property

Use **Crestmap (Primary)**, property `539897947`, in account `396518325`
for routine reporting. **Crestmap (Secondary)**, property `539916329`, in
account `396538019`, is a separate property retained for continuity. Both
properties were connected to the same Google tag on June 1, 2026. Their event
counts must not be added together when reporting site totals.

Use the installation ID from Google's **View tag instructions**, configured
through production `GOOGLE_ANALYTICS_ID`. A destination Measurement ID is not
necessarily an independently loadable installation tag. The working tag routes
to both properties; neither property nor destination has been deleted.

## Pageviews and events

Keep Enhanced measurement enabled in both streams, with **Page changes based on
browser history events** disabled. The normal Google config call produces one
pageview per document load. Changing the selected incident or camera changes the
shareable URL but does not create another pageview. Automatic map refreshes do
not count as user selections.

Automatic site-search and form-interaction measurement are disabled to keep
free-text searches and form-derived metadata out of Analytics. Explicit events
use a fixed list of accepted parameter names and values:

| Event | Trigger | Additional parameters |
| --- | --- | --- |
| `incident_select` | User selects an incident | `incident_source`: chp/wildweb |
| `camera_open` | User selects a camera | `camera_status`: online/offline |
| `camera_image_open` | User opens the camera image viewer | `camera_status` |
| `region_change` | User follows a different region tab | `target_region`: forest/malibu |
| `share` | Incident link successfully copied to clipboard | `method`: copy_link; `content_type`: incident |
| `alert_subscribe` | First successful alert subscription save | None |

All explicit events include `map_region` and `page_type`. These and the three
selection dimensions are registered as event-scoped custom definitions in the
primary property. Page titles are stable by view and region; reported page and
referrer URLs omit query strings and fragments. Conventional UTM campaign labels (up to 100 letters, digits, spaces, periods,
underscores, tildes or hyphens) are preserved as explicit campaign fields.
Names, contacts, comment
text, push endpoints, incident IDs and device coordinates are not passed to the
explicit event helper.

## Internal and developer visits

Open a mode link once in each browser/profile used for testing:

- `https://crestmap.us/?analytics_mode=internal` labels events with
  `traffic_type=internal` for the owner's visits.
- `https://crestmap.us/?analytics_mode=developer` enables GA DebugView for testing.
- `https://crestmap.us/?analytics_mode=visitor` restores ordinary visitor mode.

The choice persists in local storage. The control parameter is removed from the
address bar before Analytics is configured. If storage is blocked, a mode link
still applies to that page load. Authenticated admin views are automatically
marked internal, independent of the browser marker. Existing open tabs need a
reload to adopt a newly selected mode.

Both properties have **Internal Traffic** and **Developer Traffic** exclusion
filters in **Testing** state. No events are discarded: matching visits are
identified by **Test data filter name**. Validate matching events in reports
before activating exclusion; activation permanently excludes matching incoming
data. DebugView and collection requests can verify the developer flag earlier.
The filter-testing dimensions and new custom dimensions may take time to become
available in processed reports.

## Verification and rollback

Run `make test` before release. After deployment, verify one `page_view` per
connected destination per load, then click several incidents and confirm only
`incident_select` events are added. Verify camera and region interactions,
clipboard success, and developer/internal flags. Subscription runtime tests
exercise failed saves, first registration, and preference updates without
creating a production push subscription.

For rollback, deploy the prior application version. The previous stream settings
had browser-history pageviews, site-search events and form-interaction events
enabled; restoring them is optional and should be deliberate. The prior property
names were both `crestmap`. Testing filters do not discard data and can remain
in Testing or be made Inactive if no longer needed.
