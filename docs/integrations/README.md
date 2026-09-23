# X1 — Email and calendar outcome tracking

Status: proposed design, 2026-09-22. No connectors, OAuth configuration, credentials, provider calls, or dashboard changes are implemented by this document. Gmail and Cal.com are provisional interpretations of “email” and “Cal”; Google Calendar is explicit. If Cal means Apple Calendar, retain these provider-neutral contracts and replace the Cal.com adapter plan after clarification.

The result is a source-attributed activity timeline beside the user's pipeline. Start with read-only local polling; introduce authenticated push only after polling and recovery work. Sending email, accepting invitations, modifying calendars, creating bookings, and writing application submission receipts are outside this capability.

- [Provider findings and exact official sources](provider-research.md)
- [Acceptance scenarios and implementation order](acceptance.md)

## Existing seams and decision

Read-only code inspection used root `caramel-ketch` at `c657108`, cross-checked pipeline checkpoint `e5c14c7`, and service worktree `queue-runtime` at `5115dae`. This docs branch starts at `9eb9f72`; the pipeline/service packages are not present here yet. Paths below describe those inspected checkpoints, not files introduced by X1:

- `packages/core/src/interviewmaxxing_core/discovery.py`: `PipelineEntry` is a user-managed tracker, separate from `ApplicationState`.
- `packages/pipeline/src/interviewmaxxing_pipeline/models.py`: `PipelineItem` owns editable tracking, lane, notes, optional application link and optimistic `revision`. `ImportProvenance` and `SourceVersion` retain original/latest/import history.
- `packages/pipeline/src/interviewmaxxing_pipeline/store.py`: `update_item` and `move_item` require `expected_revision`; `StageChange.actor` currently admits only user/import.
- `apps/service/src/interviewmaxxing_service/pipeline_api.py` and `discovery_models.py`: `PipelineApi` maps candidate-scoped cards; linked application confirmation comes from `ApplicationStore`.
- `apps/service/README.md`: local service is loopback-only, behind the Next same-origin `/api/imx` gateway, with strict body validation and revision conflicts.

**Proposed decision:** a separate integrations store and read service own observations, source health, links and suggestions. They do not reuse spreadsheet import provenance or insert provider actors into the current `StageChange` contract. Existing pipeline fields and application receipts retain their authority. This prevents a background sync from overwriting a manual move or interpreting an invitation as a submitted application.

The first release auto-attaches only unambiguous evidence; it never auto-moves a card. A later user action may apply a suggestion through the existing revision-checked pipeline operation. Cross-store acceptance needs a durable command/idempotency receipt before it is enabled; it is deliberately absent from the initial API.

## Proposed storage contract

SQLite under the service's configured local data root is the proposed persistence choice, with migrations owned by the later integrations package. No path is created by this design. IDs are opaque strings; composite keys include candidate and account. An adapter must never infer account ownership from an event sender address.

| Record | Minimum fields and uniqueness |
| --- | --- |
| `SourceAccount` | `id`, `candidate_id`, `provider`, verified provider subject/account key, display label, granted scopes, selected collections, `credential_ref`, connection generation, status; unique candidate/provider/subject |
| `SyncCollection` | account + collection ID, query fingerprint, committed cursor, generation, checkpoint revision, lease owner/expiry, last attempt/success, retry time, coverage bounds and completeness |
| `SyncRun` | run ID, collection, mode, starting cursor, next-page checkpoint, proposed ending cursor, counts, started/completed times, sanitized error; incomplete runs cannot claim success |
| `SourceObject` | unique candidate/account/collection/provider-object-ID; current version reference, availability and tombstone state |
| `SourceVersion` | unique object key + provider version key + payload digest; immutable normalized snapshot, native version/updated time, observed time, sync run, adapter/schema version, restricted evidence reference |
| `Observation` | unique source-version + normalized event kind + occurrence key; fact, evidence pointer, provider occurrence time if supplied, ingestion time, supersedes/retracts links |
| `PipelineLink` | observation/object, candidate, pipeline item, state (`linked`, `unmatched`, `ambiguous`, `ignored`), match evidence, method, rule version, revision; append-only decisions |
| `StageSuggestion` | linked observation IDs, proposed configured lane, rationale, rule/model version, confidence, `pending`/`dismissed`/`superseded`; separate from facts and manual stage |
| `WebhookDelivery` | account/channel, delivery ID if supplied, body digest, authentication result, received time, processing state; deduplication and durable wake-up only |

Account reauthorization to the same verified subject resumes the same source identity with a new connection generation. A different subject creates another account and cannot inherit cursors. Credential references point to local protected storage; secrets never enter DTOs, fixtures, logs, or source snapshots. Access/refresh token rotation is serialized per account and saved atomically. Validate OAuth state, PKCE where applicable, redirect binding and granted scopes before activating sync. Disconnect stops new work and invalidates in-flight generation claims; retention/deletion of already imported evidence is an explicit separate choice.

Provider version mapping:

- Gmail: object = message ID within account/mailbox; retain thread ID, RFC Message-ID and references separately. Native `historyId` plus normalized digest identifies observed versions; history changes also describe deletion. Do not use thread ID as a message ID or convert history IDs to floating point.
- Google Calendar: object = calendar ID + event ID. Retain opaque `etag`, `updated`, sequence, `iCalUID`, recurring parent and original occurrence time. ETags are equality tokens, not sortable versions.
- Cal.com: object = booking UID within verified account; retain numeric booking ID, API version, `updatedAt`, payload digest, recurrence/ICS identifiers if supplied, and explicit predecessor/successor UIDs. Missing native version uses a canonical normalized digest, not ingestion time.

Native IDs and canonical source JSON provide provenance; a derived digest is not provider attestation. Store minimal matching fields and only relevant text excerpts by default. Full email bodies/attachments are not retained automatically. Preserve source history within the chosen retention policy; a deliberate user deletion may remove content while leaving a minimal local deletion receipt. Do not log bodies, participant addresses, OAuth tokens or raw provider errors. Treat source text as untrusted data: no instructions, scripts, remote-image loads, or automatic URL visits.

## Sync and recovery algorithm

The following is proposed local behavior, not a claim about provider guarantees:

1. While the local service runs, schedule enabled sources every five minutes with jitter, plus a manual refresh. One fenced worker per collection, with account-level credential refresh serialization. Suspend/backoff when offline or rate limited; honor retry hints. The UI shows staleness when the computer sleeps.
2. Capture a fixed query fingerprint and starting cursor. Fetch a page, normalize it, and transactionally upsert immutable versions, observations and the page checkpoint. Duplicate pages produce no duplicate observations. Advance the **committed** sync cursor and `last_success_at` only after every page and required object fetch succeeds.
3. A restart resumes a valid page checkpoint or replays from the old committed cursor. Partial results can be displayed with a partial-run badge, but cannot delete unseen records or mark coverage complete. Authentication failures stop retries pending reconnection; permission loss is availability loss, not cancellation.
4. Cursor invalidation creates a new mirror generation and full reconciliation. Replace the current source mirror only after a complete replacement scan. Keep historical observations, link decisions, imported spreadsheet history and manual edits. An object absent from a scan is `unavailable`, not evidence of rejection; use explicit provider deletion/cancellation evidence for tombstones.
5. Notification wake-ups coalesce into the same sync worker. Never advance a cursor merely to the value in a notification. Reject obsolete worker commits by generation and checkpoint revision. Out-of-order notification payloads never replace a newer fetched snapshot.

Gmail bootstrap obtains the current mailbox history watermark from `users.getProfile`, enumerates the configured recent-message scope, then replays history from that pre-scan watermark; this also handles an initially empty mailbox. Persist a history cursor only after all change pages and referenced message reads. A history gap resets the source mirror, not the pipeline. Mailbox/query membership changes are separate from deletions. The profile method and returned watermark are documented in the [Gmail profile reference](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile).

Google Calendar uses one collection cursor per selected calendar. Initial sync uses a stable compatible query (`singleEvents=false`, deletion visibility on, no moving time window); retain masters and exceptions, then derive a bounded UI occurrence window locally. Incremental queries preserve compatible options and carry the sync token. Never expand a recurrence infinitely. Changing selection/query shape starts another generation.

Cal.com uses a completed fixed update-time window with overlap on the next poll, all booking statuses, and full pagination. Record the window upper bound only on completion; page cursors are not durable change-feed cursors. Use UTC clock skew allowance and periodic full reconciliation because the published list contract does not establish a snapshot-consistent or lossless change feed. Do not rely on `upcoming` alone, which would hide cancellations/past bookings. If a version tie has differing digests, refetch/reconcile and hold ordering rather than guessing. Coverage remains qualified until adapter contract tests establish actual endpoint behavior.

## Linking and interpretation

Match within one candidate. Candidate's own email is routing context, not a job identifier. Link automatically only for an exact known application/job reference or a previously user-approved thread/booking link that still has no conflicting role evidence. Normalize URLs only through existing job identity rules; do not strip identifiers speculatively. RFC threading headers and provider threads can support continuity, but are not proof of the same job.

Company domain, recruiter identity, subject keywords or date proximity alone only generate candidates. Two jobs at Fictional Northstar with one recruiter stay `ambiguous` until an exact job reference or user choice resolves them. Forwarded messages, group invitations, changed subjects and agency recruiters default to review. A user match persists its scope (this message, this booking, or this thread), with an unlink history; a new conflicting job reference reopens ambiguity. Unmatched events remain visible in a review inbox.

Keep cross-provider source records separate. An exact ICS UID plus occurrence identity can establish a correlation group; booking-to-calendar provenance or a user decision strengthens it. ICS UID alone cannot collapse every instance of a recurring series. A Cal booking, Gmail invite and Google event may describe one meeting, but the UI should retain all evidence sources and count only a resolved occurrence group for meeting totals. Similar titles/times are suggestions, not deduplication keys.

| Observed evidence | Allowed interpretation |
| --- | --- |
| Email arrived in a message/thread | Observed email; “recruiter reply”, “receipt”, “rejection” and stage proposals are separately labeled content interpretations with excerpts |
| Calendar event or accepted booking exists | Observed scheduled event; “interview” requires explicit content/link evidence and remains a labeled classification |
| Booking/calendar cancellation or booking rejection | That meeting/booking was cancelled or rejected; never a job rejection or automatic closed lane |
| Start/end changed or explicit reschedule link | Retain old/new schedules, connect replacement UIDs; no additional interview count for the same resolved occurrence |
| Event elapsed, attendee accepted/declined, or organizer marked event confirmed | Time/response/status facts; not proof the interview happened, an offer exists, or candidate was rejected |
| Email says application received | Email provenance only; no `ApplicationStore` receipt, `SUBMITTED` mutation, or browser-confirmation label |

Persist instants as UTC plus original offset/IANA zone. Preserve all-day dates as dates with exclusive end date, never invented midnight instants. Recurring Google occurrences use parent + original start identity even when moved; cancelled exceptions suppress only that occurrence. Calendar cancellation tombstones may lack title or participants, so resolve them by stored identity. Retain series cancellation separately from exception cancellation. For Cal recurrence, use supplied grouping/occurrence identifiers; if missing or unclear, do not synthesize a recurrence rule. Daylight-saving expansion uses the source zone, not the machine zone.

## Proposed service and dashboard contract

These local paths and DTOs are **new sketches**, not existing routes or provider endpoints. Add them behind the current same-origin gateway with candidate identity supplied by the service, strict schemas, bounded pagination and existing origin checks. Never accept a caller-selected candidate/account owner. A source absent from configuration returns `not_configured`, not a successful empty feed.

| Proposed local endpoint | Request / response |
| --- | --- |
| `GET /integrations/sources` | `{sources: SourceHealthView[]}` without credentials or cursors |
| `POST /integrations/sources/{id}/sync` | `{}` → `202 {syncRunId}`; coalesce concurrent refreshes |
| `GET /integrations/sync-runs/{id}` | Sanitized run progress, completeness and error |
| `GET /integrations/observations?cursor=...` | Review inbox, source/version, link state, fact and optional suggestion |
| `GET /pipeline/entries/{id}/observations?cursor=...` | Separate timeline DTO; does not change existing pipeline history or linked application view |
| `POST /integrations/links/{id}/resolve` | `{pipelineItemId, expectedLinkRevision, scope}` → link decision; `409` on stale decision; target must belong to configured candidate |

```json
{
  "id": "src_fictional_01",
  "provider": "google_calendar",
  "label": "Fictional interview calendar",
  "state": "stale",
  "lastAttemptAt": "2026-09-22T16:10:00Z",
  "lastSuccessAt": "2026-09-22T15:50:00Z",
  "nextRetryAt": "2026-09-22T16:15:00Z",
  "coverage": {"complete": false, "reason": "offline"},
  "pendingMatches": 2,
  "error": {"code": "offline", "message": "Calendar sync is waiting for a connection."}
}
```

Health states: `not_configured`, `connecting`, `syncing`, `healthy`, `stale`, `partial`, `needs_reauth`, `error`, `disconnected`. Compute staleness from last complete success and configured cadence, independent of “no changes.” Do not show “live” when a source is stale or still using fixtures. Preserve the last success when an attempt fails.

The board gets source-health badges and an upcoming observed-meetings strip. Card detail gets an activity tab with event time, ingestion time, source/account label, link reason and evidence; suggestions read “Suggested stage” beside the unchanged manual stage. Cancellation/reschedule displays previous and current schedule. Review controls resolve/ignore/unlink evidence; a later stage-acceptance button must display the exact proposed change and use the current pipeline revision. Existing application evidence remains a separate panel.
