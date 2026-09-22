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
| `GenericApplicationBrowser(driver, options, policy=)` | The same runtime over any `PageDriver`. This is the seam for a user-present OpenCLI driver. |
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
- **Custom widgets.** ARIA comboboxes, contenteditable elements and typeahead inputs become `UNSUPPORTED` fields. After the user operates one, it is reported as no longer required, with the same fingerprint.
- **Page kind.** A visible password box means `SIGN_IN_REQUIRED`; an unsolved CAPTCHA means `CAPTCHA`. Other kinds are `APPLICATION_FORM`, `ALREADY_APPLIED`, `CONFIRMATION`, `JOB_CLOSED`, `JOB_DESCRIPTION` (an apply link), `ERROR` (HTTP ≥ 400 or an error heading) and `UNKNOWN`. A GET form with at most two fields and no submit/next control, such as a status lookup, is not an application form.
- **Steps.** The step index comes from the page ("Step N of M" or `aria-current="step"`) when shown, else from the session's own count of advances.
- **Job identity.** A schema.org `JobPosting` identifier (`STRUCTURED_DATA`), or a single "Job ID / Req # …" token in the page text (`ATS_JOB_ID_ON_PAGE`). The tenant is the site's host. An identity is never derived from a URL or redirect. The identity seen on a posting page is carried to the form reached from it.
- **Semantic types.** Types come only from the field's own label, help text, name/id, `autocomplete` and input type. Checkboxes that read like consent or a personal statement become `CONSENT`/`ATTESTATION`, which core only lets a saved answer or user input fill.

## Acting safely

- `fill` refuses (raises `ValueError`) when `packet.problems_against(form)` is non-empty, or when the live page no longer shows that exact form (fingerprint). It operates each answered control and reads the value back, reporting `FILLED`, `VERIFICATION_MISMATCH` or `FAILED`. It verifies the resume digest before uploading. It never clicks next or submit, and it unchecks a pre-checked consent/attestation box the packet does not answer. `FillResult.page_errors` lists only errors that appeared while filling.
- `advance` raises `SubmissionRefused` on a step whose primary action submits, and `AmbiguousAction` when the forward control is ambiguous (for example "Review and submit", unlabelled submit buttons, or both next and submit). If the browser's own constraint validation would block the step, it returns `advanced=False` with those messages without clicking. A step the site shows again with errors returns `advanced=False` with them.
- `submit` clicks the unambiguous final submit control exactly once. It must only be called after `ApplicationStore.begin_submission`. It reports `dispatched=False` (nothing sent) when there is no such control, when browser validation would block the form, when a required custom control still needs the user, or when the questions changed after filling. After a dispatched submit whose outcome is not established, or after an accepted one, the session refuses to submit again.
- `confirm` requires acceptance wording tied to this application: the job id or title shown on the page, or a confirmation reference that appeared after the click. A page naming a different job id is not acceptance. The same form shown again with errors is `NOT_SUBMITTED`: `NEEDS_INPUT` for field errors, `FILLING` otherwise. Everything else is `UNKNOWN`, including a 502, a generic "Thank you!", a vanished form or a timeout. Evidence (screenshots, HTML, visible text, confirmation URL) is written under `artifacts_dir` and referenced relative to `artifacts_root`.
- `wait_for_user(reason, timeout_s)` does nothing to the page. It polls until there is no sign-in, CAPTCHA or unoperated required custom control, or the timeout passes, then returns a fresh inspection.
- `reconcile(url, tie=, lookup_email=)` re-reads the site for a `SUBMISSION_UNKNOWN` application. It follows application-status links and submits only GET lookup forms with one email field. It returns ACCEPTED with signals and a reference, or UNKNOWN. It never touches an application form.

## Runner wiring (I1)

Follow CONTRACTS.md section 7. Browser-specific points:

- For `USER_ACTION_PAGES`: call `user.request_action(...)`, then `await browser.wait_for_user(reason, timeout_s)`, renewing the claim during long waits.
- For required `UNSUPPORTED` fields (the resolver's `UNSUPPORTED_CONTROL` items): ask the user to operate them in the visible window, call `wait_for_user`, then re-inspect and resolve again.
- `SubmitActionResult.dispatched=False` still needs `confirm()`, which returns the proof-bearing `NOT_SUBMITTED` observation to record.
- To settle `SUBMISSION_UNKNOWN`: start a session, call `obs = await browser.reconcile(job.application_url, tie=ConfirmationTie.from_job(job), lookup_email=candidate.identity.email)`, and if `reconciliation_from(obs, method=...)` returns one, pass it to `store.reconcile_submission`. Never call the mock's `/__test__/` API from product code.

## OpenCLI user-present path (next package, not implemented here)

ARCHITECTURE.md section 9 adds a user-present OpenCLI session. That path should plug in as a **driver**, not as a second runtime or state machine:

1. `OpenCliDriver` implements `PageDriver` (`url`, `last_status`, `goto`, `evaluate`, `fill`, `select_values`, `set_checked`, `set_files`, `click`, `settle`, `screenshot`, `html`, `bring_to_front`) over a **named, stable** OpenCLI session and tab. It passes arguments as structured process arguments, never shell-interpolated text. `evaluate(inspector_script())` must return the same `DomSnapshot` JSON, so field ids, fingerprints and page kinds are identical to the Playwright path, along with provenance and store records.
2. Run `GenericApplicationBrowser(OpenCliDriver(...), options, policy=ActionPolicy(automation_may_navigate=..., automation_may_submit=...))`. The policy records who owns navigation and submission in that session. When the user keeps them, `advance` raises and `submit` reports `dispatched=False`, and the user acts. Asking for advice does not authorize clicking.
3. After each page change the runtime re-inspects, as it already does. Screenshots only for visual questions.
4. Use the same `ApplicationStore`, packets, `UserInput`s and receipts. Assessment or follow-up completion stays separate from application receipts. Commit no invitation tokens, real questions or answers, or candidate data.
5. Tests: the whole `tests/browser` suite can be parameterized over a second driver against the same mock ATS. Do not use it against the user's live session during development.

## Tests

```bash
uv venv .venv-task --python 3.12
uv pip install --python .venv-task/bin/python -e packages/core -e apps/cli -e packages/browser pytest ruff mypy
.venv-task/bin/playwright install chromium        # only if not already cached
.venv-task/bin/python -m pytest tests/browser     # real headless Chromium vs the localhost mock ATS
```
