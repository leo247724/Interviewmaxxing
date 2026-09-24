# Application preparation

Current operating mode is **prepare only**. The CLI `apply`/`resume` commands and
dashboard runner default to this mode. A complete form stops at the final review
step as `NEEDS_INPUT`, with a `preparation.ready` event stating that nothing was
submitted. Missing answers, an uncertain action or validation errors stop earlier.
Saved cards remain Saved; preparation creates no submission attempt or receipt.

The no-submit restriction is stored before opening the browser. It survives
process restarts, repeated application requests and resume, even if a later caller
uses a submission-capable runner. The store rejects `begin_submission`; an SQLite
trigger also blocks insertion of a submission attempt. There is no CLI flag that
clears this restriction. Future submission authorization needs an explicit change.

The browser receives `allow_submission=False`, which also overrides a permissive
factory action policy. It can fill and advance unambiguous intermediate steps but
cannot dispatch the final submission. Before recording readiness it reads the
final DOM again, checks native validity and saves review evidence. An OpenCLI run
leaves its owned final page and session open; their location is recorded with the
preparation event. The retained tab can be closed after review. Playwright sessions
close after saving evidence. Preparation currently runs one application at a time;
the future bulk harness must bound retained review tabs separately from workers.

Some application forms embed a CAPTCHA widget (an invisible reCAPTCHA or hCaptcha
badge with its token field) that the site checks only when the form is submitted.
The browser runtime fills and prepares such a form as usual and reports it as
`captcha_pending`; the `preparation.ready` event records `captcha_pending` and the
stop message says the CAPTCHA must be solved in the browser before submission. A
text CAPTCHA challenge or a full-page interstitial still stops the run as `CAPTCHA`
before anything is filled. A submission-capable run refuses to dispatch while the
widget is unsolved and records a "Solve the CAPTCHA" user action instead. Before
classifying a freshly opened page the runtime also waits, bounded by its settle
timeout, for a page that is still rendering (an SPA showing "Fetching application
form", an `aria-busy` region) to classify or stop changing, and dismisses a cookie
consent banner once (decline preferred, accept otherwise) when its buttons sit
outside the application form. Pages that say the job does not exist, is not
currently active, has expired or has been filled are `JOB_CLOSED` (permanent), not
retryable unknown pages. A posting whose "Apply" / "Apply now" control has no real
form behind it (fewer than two fillable fields; a language switch, share or search
box does not count) is a job description: the control is followed to the form and is
never treated as a submit, so an unfilled posting is never reported as prepared.
Section headings that precede a control are recorded as `section_context`, not as
part of its question, so saved answers keep matching the bare question wording.

Verification uses fictional identities and localhost forms. No real employer
applications are submitted. Actual candidate details still need to be supplied
and verified before filling employer forms. Some sites save drafts during earlier
steps; prepare only prevents final submission, not every intermediate server write.

`interviewmaxxing status APP` labels a prepared application ("prepared: final review
step reached; nothing was submitted", plus a CAPTCHA line when one must be solved at
submission time) and points to the evidence directory instead of suggesting answers.
Repeating `apply` for a prepared URL re-runs the browser and records a second
`preparation.ready` rather than returning the stored state; `receipt APP` reports that
no confirmed submission exists (exit 4) and `reconcile APP` refuses because the
application was never submitted (exit 4). The end-to-end suite in `e2e/test_cli_e2e.py`
asserts these boundaries against the localhost mock ATS.

The runner can explicitly enable Jev routing and the grounded Opus writer through
the CLI or service settings in [dynamic-runtime.md](dynamic-runtime.md). Backend
maps supply versioned context from a local cache of the Supabase registry. Unknown
controls, unavailable facts and gated pages still require input; a map does not
establish that every employer variation can be completed automatically.
