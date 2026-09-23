# X1 provider evidence

Official primary documentation inspected 2026-09-22. This establishes documented contracts, not successful account access, API experiments, customer entitlements, or configured infrastructure. Provider details require another check when adapters are implemented. The behavioral choices in [the design](README.md) are our proposals.

## Read scopes and local authorization

| Provider | Minimum proposed grant and constraints |
| --- | --- |
| Gmail, provisional | `https://www.googleapis.com/auth/gmail.readonly` permits message reads for body-based outcome classification. `gmail.metadata` omits bodies and is insufficient for that behavior. Both are restricted scopes; do not request send/modify/full-mail access. Public deployment may require verification. [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) |
| Google Calendar | `https://www.googleapis.com/auth/calendar.events.readonly`; add `calendar.calendarlist.readonly` only for selecting subscribed calendars, and `calendar.calendars.readonly` only if reading calendar metadata/default zones is required. Scope grants are broader than our selected-calendar filter. No event write scope. [Calendar scopes](https://developers.google.com/workspace/calendar/api/auth) |
| Cal.com, provisional | `BOOKING_READ` for booking reads; add only a verified identity-read grant if needed. OAuth scopes must be enabled on the client; redirect URI must exactly match registration. Public clients use S256 PKCE. `WEBHOOK_WRITE` is separate from booking read and is not part of baseline authorization. [Cal OAuth](https://cal.com/docs/api-reference/v2/oauth) |

Google's installed desktop OAuth flow supports loopback redirects on macOS/Linux/Windows and an external authorization browser. A desktop client with PKCE and a temporary loopback callback is the proposed local option, subject to the actual chosen client type. No callback is registered or started here. [Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)

Cal.com client approval and redirect configuration remain account-dependent. The docs describe configurable local app hosts, but this research did not establish acceptance of a particular hosted-Cal localhost redirect. Do not invent one or route personal data through a temporary tunnel. Until confirmed, Cal development uses fixtures. A separately chosen self-hosted Cal test instance is an optional later environment, not required infrastructure.

## Polling contracts

**Gmail:** initial/full synchronization lists/fetches relevant messages. Partial synchronization uses `users.history.list(startHistoryId=...)`; unavailable history returns HTTP 404 and requires full synchronization. History retention is not a guaranteed seven-day window. [Gmail sync](https://developers.google.com/workspace/gmail/api/guides/sync)

History records are increasing but not contiguous. Persist IDs as strings, exhaust `nextPageToken`, handle message additions/deletions and label changes separately, and retain the ending mailbox history ID only after successful processing. [History list reference](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list)

**Google Calendar:** `events.list` returns the next sync token only on the final page. Incremental sync returns deletions, forbids `showDeleted=false`, and disallows filters such as `q`, `timeMin`, `timeMax`, `updatedMin` and `orderBy` alongside the sync token. Keep remaining query options stable. [Events list](https://developers.google.com/workspace/calendar/api/v3/reference/events/list)

HTTP 410 means the token is invalid; rebuild the provider mirror. Our retained audit history and manual pipeline are separate from that mirror and must survive the rebuild. [Calendar sync](https://developers.google.com/workspace/calendar/api/guides/sync)

**Cal.com:** current `GET https://api.cal.com/v2/bookings` documentation requires `cal-api-version: 2026-05-01`, accepts OAuth `BOOKING_READ`, provides `pagination.nextCursor`/`hasMore`, and offers `afterUpdatedAt`/`beforeUpdatedAt` filters. Omitting status walks all statuses; `upcoming` alone is incomplete. These are pagination and filtering capabilities, not a documented durable change feed. Responses include booking UID, `updatedAt`, start/end, attendee zones, `rescheduledFromUid`/`rescheduledToUid`, and `icsUid`; preserve them when present. [Get all bookings](https://cal.com/docs/api-reference/v2/bookings/get-all-bookings)

## Optional notifications

**Gmail:** `users.watch` uses Cloud Pub/Sub. Renew at least every seven days (docs recommend daily) and honor returned expiration. Notifications carry a mailbox history ID, not message contents; they can be delayed/dropped. Polling remains required. Pub/Sub pull is a possible local enhancement without a public inbound endpoint, but still requires a configured cloud topic/subscription; none exists for this design. [Gmail push](https://developers.google.com/workspace/gmail/api/guides/push)

For later Pub/Sub push, validate Google's signed bearer ID token, issuer/expiry, expected audience, configured service-account email and `email_verified`; bind the subscription to the expected source. Do not trust the payload email to choose arbitrary credentials. Acknowledge only after durable delivery recording. [Authenticated Pub/Sub push](https://docs.cloud.google.com/pubsub/docs/authenticate-push-subscriptions)

**Google Calendar:** watches are per resource/calendar, require an HTTPS callback, and send headers without event bodies. Persist channel ID, resource ID, secret channel token and expiration; validate their binding before scheduling a read. No documented HMAC body signature is assumed. Renew by creating a new unique channel before expiration; overlapping channels are expected. Initial `sync` may arrive before watch response; message numbers are not contiguous and deliveries may be dropped. [Calendar push](https://developers.google.com/workspace/calendar/api/guides/push)

**Cal.com:** documented triggers include `BOOKING_CREATED`, `BOOKING_RESCHEDULED`, `BOOKING_CANCELLED` and `BOOKING_REJECTED`. Verify `x-cal-signature-256` against HMAC-SHA256 of the received raw body using the configured secret, with constant-time comparison. Reschedule payload examples connect new `uid` with previous `rescheduleUid`; preserve both. The webhook guide describes payload versions, including `2026-07-27`; pin a chosen version and fixture before enabling. This research establishes neither a renewable subscription TTL nor guaranteed replay/order or signed freshness timestamp. Do not manufacture those guarantees. [Cal webhook guide](https://cal.com/docs/developing/guides/automation/webhooks)

Our receiver design uses body-size limits, durable delivery deduplication, account binding and authenticated refetch. Valid signatures alone do not prevent replay. Keep periodic reconciliation and reject malformed/unknown-channel requests before source mutation. Public receivers, tunnels, cloud subscriptions and webhook creation are optional future setup, outside this local baseline.

## Calendar semantics

Google events distinguish ID from `iCalUID`. Recurring instances retain `recurringEventId` and immutable `originalStartTime` even after a move. Cancelled exceptions may contain only identity fields and must suppress their occurrence while the parent lives. `status=cancelled` concerns an event, not a job. Start/end may be all-day dates; end is exclusive. Preserve IANA zones for recurrence expansion; an `updated` timestamp alone is insufficient because reminder edits need not change it. [Event resource](https://developers.google.com/workspace/calendar/api/v3/reference/events)

Cal booking states describe scheduling, not recruiting outcomes. Reschedule links are stronger evidence than title/time resemblance. API booking representations can include recurrence variants; exact series/occurrence normalization needs fixtures for the selected version. Do not infer a job rejection from a rejected booking, or interview attendance from elapsed time.
