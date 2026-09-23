# interviewmaxxing-browser

The browser runtime for the supplied-URL flow (ARCHITECTURE.md sections 9 and 10,
CONTRACTS.md sections 5 and 6). It inspects live application pages, fills and uploads
from a canonical `ApplicationPacket`, navigates steps, submits once and reports what
the site showed. It never writes application state; the runner records its
observations through `ApplicationStore`.

Dependencies: `interviewmaxxing-core`, `playwright>=1.62,<2`, and a Chromium build
(`playwright install chromium`).

## Public API

```python
from interviewmaxxing_browser import PlaywrightSessionFactory, ConfirmationTie, reconciliation_from

browser = await PlaywrightSessionFactory().start(BrowserOptions(
    artifacts_dir=paths.application_artifacts(app.id),
    artifacts_root=paths.artifacts_dir,
    profile_dir=paths.browser_dir,      # persistent sign-in
    headless=False,                     # visible, so the user can sign in / solve a CAPTCHA
))
page = await browser.open(url)          # PageInspection; follows an apply *link*
...
await browser.close()
```

| Object | Contract |
| --- | --- |
| `PlaywrightSessionFactory` | `BrowserSessionFactory`. Launches Chromium, with a persistent profile when `profile_dir` is set (one process per profile). |
| `PlaywrightApplicationBrowser` | `ApplicationBrowser` (`open`, `inspect`, `fill`, `advance`, `submit`, `confirm`, `wait_for_user`, `close`), plus `reconcile` and `.page`. |
| `GenericApplicationBrowser(driver, options, policy=)` | The same runtime over any `PageDriver`. |
| `OpenCliSessionFactory(config)` / `OpenCliDriver` / `OpenCliApplicationBrowser` | The runtime in the user's own Chrome through OpenCLI (see below). |
| `GenericAdapter` | `ATSAdapter` for native, accessible forms. |
| `ConfirmationTie` | What ties a confirmation to this application: the job id or title, and references already visible before the submit. `ConfirmationTie.from_job(job_record)`. |
| `reconciliation_from(observation, method=)` | The `SubmissionReconciliation` an ACCEPTED re-read establishes, or `None`. |
| `user_action_needs`, `unsupported_control_needs`, `attestation_fields` | `MissingInput` items for sign-in and CAPTCHA pages and for custom controls, and the consent/attestation questions on a step. |
| `inspector_script()`, `DomSnapshot`, `build_page(snapshot, ...)` | The raw DOM snapshot and pure normalization, shared with any driver. |
| `SubmissionRefused`, `AmbiguousAction`, `DriverError`, `NotActionable` | Refusals and driver failures. |

## Inspection

- **Field identity.** `id` is the control's `name` (one per radio/checkbox group), else its DOM `id`, else `field-<label slug>`. Ids are stable across reloads and restarts because they come from the page, not from DOM order.
- **Question text.** The label (the legend for a group) plus everything the user sees for that field: `aria-describedby` text, text adjacent to the control inside its own container (for example the terms next to an "I agree" checkbox), and for a single control in a fieldset, the legend. Validation messages go to `validation_error`, without the screen-reader "Error:" prefix. Live counters are excluded, so typing does not change a fingerprint.
- **Options.** Select and radio/checkbox options carry the machine `value`, the visible `label` and `disabled`. Placeholders (`value=""`) are reported as the page offers them; `answer_problems` never accepts them.
- **Excluded.** Disabled controls, hidden inputs, honeypots (aria-hidden or off-screen) and password boxes are excluded. CAPTCHA answer boxes are also excluded and reported as a `CAPTCHA` page, and once the user has filled one it is left untouched.
- **Custom widgets.** ARIA comboboxes (including `<button role="combobox">` and other widget-role buttons), contenteditable elements and typeahead inputs become `UNSUPPORTED` fields; ordinary buttons stay actions. After the user operates one, it is reported as no longer required, with the same fingerprint.
- **Page kind.** A visible password box means `SIGN_IN_REQUIRED`; an unsolved CAPTCHA means `CAPTCHA`. Other kinds are `APPLICATION_FORM`, `ALREADY_APPLIED`, `CONFIRMATION`, `JOB_CLOSED`, `JOB_DESCRIPTION` (an apply link), `ERROR` (HTTP ≥ 400 or an error heading) and `UNKNOWN`. A GET form with at most two fields and no submit/next control, such as a status lookup, is not an application form.
- **Steps.** The step index comes from the page ("Step N of M" or `aria-current="step"`) when shown, else from the session's own count of advances.
- **Job identity.** A schema.org `JobPosting` identifier (`STRUCTURED_DATA`), or a single "Job ID / Req # …" token in the page text (`ATS_JOB_ID_ON_PAGE`). The tenant is the site's host. An identity is never derived from a URL or redirect. The identity seen on a posting page is carried to the form reached from it.
- **Semantic types.** Types come only from the field's own label, help text, name/id, `autocomplete` and input type. Consent and attestation wording anywhere in the captured question (label, legend or help text) makes it `CONSENT`/`ATTESTATION` on checkboxes, checkbox groups, multiselects, radios and selects ("I consent to the following uses of my application data", "I certify that I have never been dismissed…" with Yes/No), and on text fields asking for a signature or certification, including a "Full name" box whose help says "By typing your name, you certify…". This is decided before identity or factual rules. Core only lets a saved answer or user input fill those types.

## Acting safely

- `fill` refuses (raises `ValueError`) when `packet.problems_against(form)` is non-empty, or when the live page no longer shows that exact form (fingerprint). It operates each answered control and reads the value back, reporting `FILLED`, `VERIFICATION_MISMATCH` or `FAILED`. It verifies the pinned local resume before and after attaching it and the actual browser File SHA-256 after attachment. It never clicks next or submit, and it unchecks a pre-checked consent/attestation box the packet does not answer. The inspected form's fingerprint (the one the packet was resolved against) stays the authority. If questions appear or change while filling, `FillResult` fails with "changed while filling", any pre-checked consent/attestation among them is cleared, and `advance`/`submit` refuse until the step is inspected and resolved again. `FillResult.page_errors` lists only errors that appeared while filling.
- If a document or origin changes during filling, or its identity can no longer be verified, the runtime aborts all remaining writes (including consent cleanup). Recoverable field failures may continue only in the same document. Fresh inspection and refill are required before navigation or submission. The filled document identity is rechecked immediately before next/submit.
- `advance` raises `SubmissionRefused` on a step whose primary action submits, and `AmbiguousAction` when the forward control is ambiguous (for example "Review and submit", unlabelled submit buttons, or both next and submit). If the browser's own constraint validation would block the step, it returns `advanced=False` with those messages without clicking. A step the site shows again with errors returns `advanced=False` with them.
- `submit` clicks the unambiguous final submit control exactly once. It must only be called after `ApplicationStore.begin_submission`. It reports `dispatched=False` (nothing sent) when there is no such control, when browser validation would block the form, when a required custom control still needs the user, or when the questions changed after filling. After a dispatched submit whose outcome is not established, or after an accepted one, the session refuses to submit again.
- `confirm` requires acceptance wording tied to this application: the job id or title shown on the page, or a confirmation reference that appeared after the click. A page naming a different job id is not acceptance. Acceptance wording must be affirmative: negated, conditional, future, instructional or questioning uses ("No application received", "If your application was submitted…", "Application submitted?") never count. `NOT_SUBMITTED` (`NEEDS_INPUT`) requires the same form re-rendered with field-level errors, without a 5xx status or wording that the outcome is unknown. A form shown again with only page-level alerts, a transport error, a 502, a generic "Thank you!", a vanished form or a timeout is `UNKNOWN`, and the session will not submit again. Evidence (screenshots, HTML, visible text, confirmation URL) is written under `artifacts_dir` and referenced relative to `artifacts_root`.
- `wait_for_user(reason, timeout_s)` does nothing to the page. It polls until there is no sign-in, CAPTCHA or unoperated required custom control, or the timeout passes, then returns a fresh inspection.
- `reconcile(url, tie=, lookup_email=)` re-reads the site for a `SUBMISSION_UNKNOWN` application. It follows application-status links and explicit same-origin "View confirmation/receipt" links, and submits only lookup forms with one email field whose *effective* request (including the button's `formmethod`, `formaction` and `formtarget`, re-read from the live page just before the click) is a same-origin GET in the same tab. A page listing several applications (sibling cards, articles, list items or rows of any mix of tags, carrying a status or job identity) is read one record at a time: a status counts only with identity inside the same outermost record, page-level text never ties it, a record also showing a draft/incomplete status is ambiguous, and a new reference ties only a single-record page. When repeated-record boundaries are absent, acceptance, identity and references must share an explicit heading-delimited receipt/job section or one affirmative clause in an ungrouped leaf statement. Sections stop at the next heading; a generic collection heading such as "My applications" is not a job record. Separate paragraphs without that scope, missing scope evidence, conflicting statuses and multiple job ids inside a scope remain UNKNOWN. A newly seen reference alone ties only a single dedicated receipt section immediately after submission. The job id or title must identify this application; a reference seen for the first time ties only the immediate post-submit result, never a later portal. It returns ACCEPTED with signals and a reference, or UNKNOWN, and never touches an application form.

## Runner wiring (I1)

Follow CONTRACTS.md section 7. Browser-specific points:

- For `USER_ACTION_PAGES`: call `user.request_action(...)`, then `await browser.wait_for_user(reason, timeout_s)`, renewing the claim during long waits.
- For required `UNSUPPORTED` fields (the resolver's `UNSUPPORTED_CONTROL` items): ask the user to operate them in the visible window, call `wait_for_user`, then re-inspect and resolve again.
- `SubmitActionResult.dispatched=False` still needs `confirm()`, which returns the proof-bearing `NOT_SUBMITTED` observation to record.
- To settle `SUBMISSION_UNKNOWN`: start a session, call `obs = await browser.reconcile(job.application_url, tie=ConfirmationTie.from_job(job), lookup_email=candidate.identity.email)`, and if `reconciliation_from(obs, method=...)` returns one, pass it to `store.reconcile_submission`. Never call the mock's `/__test__/` API from product code.

## Choosing a browser: Playwright or OpenCLI

Both factories return an `ApplicationBrowser` built on the same `GenericApplicationBrowser`, so field ids, fingerprints, consent/attestation handling, confirmation, uncertainty and reconciliation behave identically. Only the page driver differs.

```python
from interviewmaxxing_browser import OpenCliConfig, OpenCliSessionFactory, PlaywrightSessionFactory

factory = PlaywrightSessionFactory()                                   # own Chromium
factory = OpenCliSessionFactory(OpenCliConfig(profile="jgd7jms9"))    # the user's Chrome
browser = await factory.start(options)   # same BrowserOptions; same ApplicationBrowser calls
```

| | `PlaywrightSessionFactory` | `OpenCliSessionFactory` |
| --- | --- | --- |
| Browser | Chromium launched by Playwright | The user's own Chrome via OpenCLI Browser Bridge |
| Sign-in / cookies | `BrowserOptions.profile_dir` (persistent profile) | The connected Chrome profile (`OpenCliConfig.profile`, see `opencli profile list`); `profile_dir` is ignored |
| Visibility | `headless=False` shows the window; `wait_for_user` brings it to front | Owned tab in a named session (default `imx-application-<random suffix>`), `window="background"` by default; OpenCLI cannot focus windows, so tell the user where the tab is (`browser.location`) |
| Mutations | Playwright locators | Structured `opencli browser` commands only (`open`, `fill`, `select`, `check`, `uncheck`, `upload`, `click`), each verified afterwards (envelope, read-back, same document) |
| Evaluation | Read-only page scripts | Exact allowlist of fixed package read scripts; arbitrary JavaScript is rejected. The internal regex lint is not a JavaScript security boundary |
| File upload | Supported | Only where Browser Bridge may set files. Otherwise the field fails with `CapabilityUnsupported` asking the user to attach the file in the visible tab; a file the user attached is accepted only when its name, size and actual browser File SHA-256 match the pinned artifact. Unavailable digest support requires manual attachment with verification; it never means success |
| Multi-select with several options | Supported | `CapabilityUnsupported` (OpenCLI `select` replaces the choice); checkbox groups work |
| Errors | `DriverError` / `NotActionable` | Also `OpenCliUnavailable` (daemon/extension/profile; run `opencli doctor`), `OpenCliTargetError` (nothing done), `OpenCliTimeout` and `UnverifiedAction` (effect unknown; never counted as success) |

OpenCLI session rules:
- Each driver appends a random suffix to `OpenCliConfig.session` (a prefix), creates a blank **owned** tab with `tab new`, and checks its returned id against the session list and protected ids before any navigation. All page commands, including the first `open`, use `--tab`. A restored default tab is never navigated. Concurrent drivers never share a session.
- It refuses the user's assessment session and job-search sessions (`imx-assessment*`, `imx-jobs*`), plus any ids in `OpenCliConfig.protected_tabs`.
- Arguments go to `opencli` as structured argv lists after `--`, never through a shell.
- `close()` closes only the driver-created tab with `tab close <id>`, then releases its unique session. A failed tab close retains ownership for retry.
- `ActionPolicy(automation_may_navigate=False, automation_may_submit=False)` leaves navigation or submission to the user. `advance` then raises, and `submit` reports `dispatched=False`.
- A dispatched submit whose outcome is unknown still blocks any retry in the session.
- Use the same `ApplicationStore`, packets, `UserInput`s and receipts as the Playwright path; there is no second state machine.

## Tests

```bash
uv venv .venv-task --python 3.12
uv pip install --python .venv-task/bin/python -e packages/core -e apps/cli -e packages/browser pytest ruff mypy
.venv-task/bin/playwright install chromium        # only if not already cached
.venv-task/bin/python -m pytest tests/browser     # real headless Chromium vs the localhost mock ATS
IMX_OPENCLI_LIVE=1 IMX_OPENCLI_PROFILE=jgd7jms9 \
  .venv-task/bin/python -m pytest -s tests/browser/test_opencli_live.py   # opt-in, owned background tab, localhost only
```
