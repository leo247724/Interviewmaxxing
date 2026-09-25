# The dashboard

The dashboard (`apps/web`) runs on this computer against the local service
(`apps/service`). It has a desk (one application at a time), a pipeline, a jobs view and
the review lane described here. Setup and routes: `apps/web/README.md` and
`apps/service/README.md`.

## Applying from the dashboard

`prepare-batch` (or the desk) prepares applications and stops each one at the site's final
review step without submitting. The review lane turns each prepared application into a
short loop: check it, fix what's wrong, approve it, submit it.

### Start it

```bash
# The service, over your IMX_HOME. LIVE lets it open real employer sites; without
# IMX_ALLOW_SUBMISSION=1 it can prepare and approve but never submit.
IMX_SERVICE_ORIGIN=http://127.0.0.1:4317 IMX_SERVICE_APPLICATION_MODE=LIVE \
IMX_ALLOW_SUBMISSION=1 uv run --no-sync interviewmaxxing-service

# The dashboard.
cd apps/web && npm run build && IMX_BACKEND_URL=http://127.0.0.1:8765 \
  IMX_WEB_ORIGIN=http://127.0.0.1:4317 npm start -- --hostname 127.0.0.1 --port 4317
```

Preparing again from the dashboard uses the service's runtime. To prepare with the same AI
routing and writer as `prepare-batch`, start the service with the matching
`IMX_SERVICE_AI_ROUTING`, `IMX_SERVICE_AI_ENV_FILE`, `IMX_SERVICE_WRITER_MODEL` (and
`IMX_SERVICE_BROWSER`) settings ([dynamic-runtime.md](dynamic-runtime.md)). An application
is submitted with the runtime the service runs.

### The loop

1. **Open Review** (`/review`). The Prepared queue lists every application stopped at its
   final review step, and those held only by a sign-in, a CAPTCHA or another step you do in
   the browser, newest first: employer, title, backend, when it was prepared, what the AI
   provider calls cost, and one line saying what it waits for ("Ready to review and
   approve", "Approved: waiting to be submitted", "Answers changed since this preparation:
   prepare it again", "CAPTCHA to solve in the browser", ...).
2. **Read the review page.** Every question of the form in its order, page by page, with
   the answer that was filled in, or "Left blank" for an optional question nothing
   answered. Compare it with the screenshot of the filled review page. Each answer says
   where it came from:

   | Badge | Where the answer came from |
   | --- | --- |
   | Your details | Your verified contact details |
   | Your resume | The resume file pinned to this application |
   | Saved answer | Your saved answers: the simple-answer keys and answers kept for one job |
   | Saved policy | A standing rule: an answer you saved for every application (with `answer --reuse global` or an answer sheet), the standing referral answer, or an answer policy |
   | Derived | Worked out from a saved answer: salary and pay period, work authorization and sponsorship from your stated status, start date, work arrangement |
   | Fact-grounded screener | A screener or a fact answered from your verified facts; its cited fact ids are listed under it |
   | RAG narrative | A written answer drafted from your facts and stories, shown in full with **Copy**; its cited fact, story passage and job-description ids are listed under it |
   | Your answer | What you answered for this application |

   Answers the resolver was less sure of are flagged "Check this".
3. **Change what's wrong.** **Edit** opens the answer in place. Choose how far it goes:
   this application, this job, or every application (saved for reuse, like
   `answer --reuse global`), then **Save and prepare again**. The service saves it through
   the same answer path as any question and prepares the form again, which fills in your
   answer; the application comes back to the queue when the new preparation stops at the
   review step. A choice question can be changed only when its options were recorded (the
   form asked it at some point); a question the runner filled without asking shows why it
   can't be changed. Reuse beyond this application needs the question's full wording;
   contact details (name, email, phone, address, profile links) change for this application
   only (change them in your profile to change them everywhere); and a RAG narrative, drafted
   for this job, can be kept for this application or this job but not for every application.
4. **Approve.** **Approve** approves exactly the preparation you reviewed, every page of
   it. It submits nothing. Changing an answer or preparing again withdraws it: approve the
   new preparation.
5. **Submit.** **Submit** is available only when the service runs with
   `IMX_ALLOW_SUBMISSION=1`, you approved this preparation, and (in `TEST_ONLY`) the site
   is a local test site; otherwise the button says what is missing. It asks you to confirm
   in a dialog. The service then submits exactly the approved answers, once, and shows the
   outcome: submitted with the site's confirmation as a receipt, not confirmed (settle it in
   the desk; it is never submitted again), or stopped before submitting because the form
   changed (the approval is withdrawn: prepare it again). When the form has a CAPTCHA to
   solve at submission, the browser window the submission opens is where you solve it; a
   service running its browser headless can't show it, so the page gives the terminal
   command instead.
6. **A sign-in or CAPTCHA first.** For an application held by a browser step,
   **Resume in browser** opens the application in a visible window (the
   `interviewmaxxing resume APP --act` equivalent); do the step there and the preparation
   carries on. When the service can't open a window (headless), the page shows the exact
   command to run in a terminal.

The desk (`/`) stays the place for one application's history, a receipt and an unconfirmed
submission.

### What never happens

- Nothing is submitted unless all three hold: the service was started with
  `IMX_ALLOW_SUBMISSION=1`, you approved this exact preparation, and you confirmed the
  submission in the dialog. Approving, editing, preparing again and resuming never submit.
- A submitted, submitting or unconfirmed application is never submitted again.
- In `TEST_ONLY` the service opens and submits only local test sites.
- Your answers are shown on this computer only; the service logs no values.

### Limits

- Choice questions the runner filled without ever asking them can't be changed here yet:
  their options aren't recorded with the preparation. Answer them with `answer` after a
  question stop, or ask for the runner to record the options with `preparation.ready`.
- A multi-step site that reopens a kept draft at a later page may not fill an earlier page
  again when preparing again; check the new preparation's pages before approving it.
- Submitting from the dashboard is verified against the localhost mock ATS only
  (`tests/service/test_review_acceptance.py`), like the CLI's `submit`
  ([submission.md](submission.md)).
