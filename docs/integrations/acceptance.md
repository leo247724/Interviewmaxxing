# X1 acceptance and delivery sequence

These are future implementation acceptance requirements, not tests claimed to have run. All fixtures use fictional candidates, `example.com` addresses, synthetic job IDs and a temporary test data root. Real accounts, inboxes, calendar APIs, booking APIs, OAuth consent and credentials are not exercised by X1.

## Required scenarios

| Scenario | Observable assertion |
| --- | --- |
| Duplicate page or repeated webhook | One source version/observation per identity; unchanged manual card revision; delivery receipt records retry |
| Same provider ID in two accounts/calendars | Separate objects; no cross-account cursor, link, credential or evidence access |
| Crash after page commit / before final cursor | Restart replays safely; no lost page, duplicate observation or false last-success timestamp |
| Concurrent refresh and expiring lease | Only the fenced generation/checkpoint winner advances state; one credential refresh at a time |
| Disconnect during fetch, then another account connects | Late response cannot commit into new connection; old source history remains separately attributed |
| Gmail history gap / Calendar 410 | New mirror generation, complete reconciliation, preserved manual fields, links and audit history |
| Partial pagination / rate limit / permission loss | Last complete success unchanged; coverage partial; unseen events are not cancelled or jobs closed |
| Cal update exactly at watermark; booking moves to past/cancelled | Overlap and all-status pagination retain updates; page cursor never becomes a change cursor |
| Out-of-order Cal cancellation/create notification | Refetch latest state; no resurrection by stale payload; conflicting version ties held |
| Two Fictional Northstar jobs and one recruiter | Ambiguous match stays unlinked; no company-only automatic choice |
| Approved thread later mentions another role | Thread inheritance stops; explicit conflicting job ID reopens review |
| Email receipt plus browser-unknown application | Email observation shown; canonical application stays unknown; no fabricated browser receipt |
| Booking rejected or event cancelled | Meeting status changes; manual lane and job status remain unchanged |
| Google recurring exception moves then cancels | Stable occurrence identity; old/new schedules preserved; only that occurrence suppressed |
| All-day event and DST transition | Dates remain dates; exclusive end preserved; local display follows source zone without shifting series identity |
| Gmail invite + Google event + Cal booking | Three provenance records, one resolved occurrence group only when linkage supports it |
| User edits notes/stage while sync runs | Exact edits survive; sync produces suggestions only; later stale user acceptance returns conflict |
| Provider text contains HTML/instructions/remote images | Render escaped text, no remote loads or tool actions, no body/secret leakage in logs |
| Wrong JWT audience/account, invalid HMAC, unknown channel | Rejected before sync mutation; no arbitrary account routing |
| Valid replay, renewal overlap, dropped push, initial sync race | Deduped wake-up and periodic reconciliation; no dependence on consecutive message numbers |
| Service sleeps/offline, empty successful poll | Stale badge after elapsed threshold; successful empty poll updates freshness; fixtures never display as live |

## Implementation order

1. Land this design and confirm provider meaning. Build provider-neutral models, SQLite migrations, query fingerprints, run checkpoints and fixture adapters. Validate crash/replay/account-isolation scenarios before any provider connection.
2. Expose source health and a separate observation/review service behind the existing gateway. Add dashboard fixture views and unchanged-manual-field checks. Preserve existing application confirmation DTOs.
3. Implement Google read adapters and explicit account-connection UI using read scopes and protected token storage. Test authorization/cursor/error behavior against mocks first. Actual account connection requires the selected accounts/calendars and user authorization; this document supplies neither.
4. Implement Cal.com only if that is the intended provider, with pinned API version and fixtures for pagination, recurrence and reschedule variants. Establish actual account booking visibility and OAuth client setup before claiming live coverage.
5. Add conservative linking, correlation and versioned suggestions. Keep ambiguous links in review. If later implementing “apply suggestion,” first make its command idempotent across pipeline and integration stores and verify optimistic-revision behavior.
6. Consider Pub/Sub pull or authenticated webhooks only if local polling latency is insufficient. Keep polling recovery, renewal state, replay protection and source health. No hosting platform is selected or required by this design.

## Account/product questions before live connection

- Does email mean Gmail, and does Cal mean Cal.com or Apple Calendar? Which account is personal versus Workspace, and which calendars should be included?
- Is the user a Cal.com host with API-visible bookings, or primarily an invitee booking other people's links? Do not promise the latter is fully visible through the user's booking API.
- What initial email history window and calendar coverage are wanted? May relevant email excerpts be retained locally, and for how long? A restricted scope does not itself enforce that product-level filter.
- Is five-minute freshness while the machine is awake sufficient? If continuous updates are required, choose and authorize deployment separately.
- Which local OAuth clients can be registered/approved, and are organization restrictions present? Which identity verification mechanism will bind each grant to its account? No credentials should be pasted into docs or chat.

These questions do not block provider-neutral fixture implementation. They do block an accurate claim of connected-account coverage. No additional questions are needed for X1 design completion.
