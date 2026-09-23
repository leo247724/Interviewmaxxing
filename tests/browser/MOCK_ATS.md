# Mock ATS fixture

`scripts/mock_ats.py` is a deterministic, standard-library-only HTTP server that
imitates the careers site of a fictional employer, **Brambleway Analytics**. It
serves ordinary accessible HTML application forms for the browser runtime (C4)
and integration (I1) tests, and it records every submission server-side so tests
can assert what was actually received.

Everything is local and fictional: the server binds only to loopback addresses,
the candidate is "Avery Quill" at `avery.quill@example.test`, and nothing is sent
anywhere.

## Start and stop

Run with Python 3.12+ through `uv`. No project dependencies are needed:

```bash
uv run --no-project --python 3.12 scripts/mock_ats.py \
  --state-dir /tmp/mock-ats-state --ready-file /tmp/mock-ats-ready.json
```

On startup it prints, then flushes:

```text
MOCK_ATS_ORIGIN=http://127.0.0.1:54321
MOCK_ATS_STATE_DIR=/private/tmp/mock-ats-state
MOCK_ATS_PID=12345
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Must be `localhost` or an IPv4 loopback address; anything else exits with status 2. |
| `--port` | `0` | `0` picks a free ephemeral port. Always use the printed origin. |
| `--state-dir` | new `mock-ats-*` temp directory | Holds `state.json` and `uploads/`. Reusing it keeps submissions across restarts. |
| `--ready-file` | none | Written atomically as `{"origin", "state_dir", "pid"}` once the server is listening. Removed on a clean stop. |
| `--verbose` | off | Logs requests to stderr. |

Stop **only this process** with any of the following. Each exits with status 0 and prints `MOCK_ATS_STOPPED`:

- Press Ctrl-C in its terminal. SIGINT triggers a graceful shutdown.
- Run `kill -TERM "$(python3 -c 'import json;print(json.load(open("/tmp/mock-ats-ready.json"))["pid"])')"`.
- Run `curl -X POST "$ORIGIN/__test__/shutdown"`.

Do not use `pkill`/`killall` by name. Other workers may be running their own mock servers.

In Python tests, run it in-process:

```python
ats = mock_ats.MockATS(port=0, state_dir=tmp_path / "state").start()
try:
    ...  # use ats.origin
finally:
    ats.stop()
```

`MockATS` is also a context manager. Load the module from `scripts/mock_ats.py` with
`importlib.util.spec_from_file_location`, as `tests/browser/test_mock_ats.py` does.

## Scenarios

Each scenario is a job at `/jobs/<job_id>`, with the form at `/jobs/<job_id>/apply`.
`GET /__test__/jobs` returns this catalog with every field's name, label, kind,
required flag and option `value`/`label` pairs.

| Job id | Title (Job ID) | Behavior |
| --- | --- | --- |
| `standard` | Senior Data Platform Engineer (BWA-ENG-101) | A single page using every native control: text, email, tel, url, a textarea, a select, a radio group, a checkbox group, a single checkbox, a multiselect and a required multipart resume upload. Acceptance returns a 303 redirect to `/applications/sub_NNNNNN`, which shows "Application submitted" and "Confirmation reference: BWA-NNNNNN". |
| `multistep` | Machine Learning Engineer (BWA-ML-102) | Step 1 is contact information. Step 2 covers the resume, experience, work authorization and sponsorship. Step 3 has additional questions. Step 4 is a review page with Edit links and a "Submit application" button. The step 1 POST creates draft `dft_NNNNNN`. Later steps cannot be skipped, "Back" links keep saved values, and the uploaded resume is retained. Nothing is counted until the review form is posted. |
| `missing-required` | Analytics Engineer (BWA-AE-103) | Adds required questions the fixture candidate cannot answer: notice period (select), desired salary (text) and an FAA Part 107 certificate (radio). These must surface as missing input. They must never be inferred. |
| `attestation` | Staff Security Engineer (BWA-SEC-104) | Adds two required, unchecked personal-attestation checkboxes: accuracy and the privacy notice. Only the user may check them. |
| `validation` | Backend Engineer (BWA-BE-105) | A server-side rule requires the phone number as exactly 10 digits. The fixture phone `+1 (303) 555-0142` is rejected with a visible error. The HTML has no `pattern`, so the browser does not catch it first. |
| `signin` | Product Engineer (BWA-PE-106) | The form, including its POST, returns a 303 redirect to `/login?next=/jobs/signin/apply` until the user signs in. Fictional credentials are `avery.quill@example.test` / `fixture-password-123`. Signing in sets the `bwa_session` HttpOnly cookie (`Max-Age` one day, so a persistent browser profile keeps it). |
| `captcha` | Frontend Engineer (BWA-FE-107) | The page includes "Verify you are human (CAPTCHA)": an SVG image at `/captcha/cap_NNNNNN.svg` and a required "Characters shown in the image" field. Each challenge works for one attempt only, and each render issues a new one. |
| `uncertain` | Site Reliability Engineer (BWA-SRE-108) | The POST is validated, then **recorded and counted**. The response is a 502 "Something went wrong" page with no reference. The confirmation URL returns 404 and the status page says the application is still being processed. After the test-only reveal, a later visit to the status page shows the real reference and a "View confirmation" link. |
| `agreement` | Data Engineer (BWA-DE-109) | Two required checkboxes both labelled only "I agree". The first sits in a fieldset with the legend "Candidate declaration", and its terms are an adjacent paragraph that is **not** linked by `aria-describedby`. The second's terms are `aria-describedby` hint text. A runtime must capture both as part of each question. |
| `custom-control` | Operations Analyst (BWA-OPS-110) | Adds a required "Preferred office" ARIA combobox: a `div role="combobox"` with a listbox, backed by a hidden input, and deliberately not a native control. It also has a disabled "Employee referral code" field and a visually hidden honeypot (`website_hp`, inside `aria-hidden` and off-screen). Any honeypot value is rejected. |
| `vague-confirmation` | QA Engineer (BWA-QA-111) | The POST is recorded and counted, but the response is a bare "Thank you!" page that names no job and no reference. The status page shows the real reference right away. |

### Shared behavior

- **Disabled options.** `missing-required` offers "3 months or more (no longer offered)" as a disabled option; posting its value is rejected.
- **Machine values differ from labels.** Selects, radios and checkboxes post values such as `wa_authorized` and `sk_python`, while users see "Yes, I am authorized to work in the US" and "Python". Posting a label or an unknown value is rejected with "Select one of the listed options."
- **Accessible markup.** Every control has a `<label for>`. Radio and checkbox groups are `<fieldset>`s with a `<legend>`. Required controls use `required`; the `*` marker is `aria-hidden`, and optional controls say "(optional)". Controls can be found by accessible name, for example Playwright `get_by_label("First name")`. No test IDs or product-specific hooks are needed.
- **Rejection.** An invalid POST returns 422 and re-renders the form. The page gets an error summary (`role="alert"`, "There is a problem with your application", with links to the fields). Each invalid field gets an inline error, `aria-invalid="true"` and `aria-describedby`. Values are preserved. A valid resume from the rejected POST is retained as "Currently attached: …" with a hidden `resume_upload_id`, so the file input stops being required. Rejections are recorded but **never counted as submissions**.
- **Every accepted POST counts.** Resubmitting an identical form creates another record with a new reference, so a mistaken retry is detectable.
- **Status page.** `/jobs/<job_id>/application-status?email=…` is a public page linked from each posting ("Already applied? Check your application status"). For each matching record it shows the reference, or "still processing" while the confirmation is withheld. This page is how a runtime reconciles an uncertain outcome.
- **Job identity.** Postings include the title, company, location and Job ID. They also carry a canonical link and a schema.org `JobPosting` JSON-LD block with `identifier.value` set to the Job ID. The apply pages repeat the same identity line.
- **Deterministic ids.** Ids come from counters in a fresh state dir: `sub_000001`/`BWA-000001`, `dft_000001`, `upl_000001`, `cap_000001`. Captcha answers are derived from the token. Only timestamps vary. There are no artificial delays.

## Test-only API — never call from product code

These endpoints exist for test assertions and fixture control only. The runtime
must reconcile through the public pages. The pages never link to these endpoints.

| Method and path | Result |
| --- | --- |
| `GET /__test__/health` | `{"ok": true, "origin", "state_dir"}` |
| `GET /__test__/jobs` | Scenario catalog and sign-in credentials |
| `GET /__test__/submissions[?job_id=<job>]` | `{"accepted_count", "rejected_count", "submissions": [...], "rejections": [...]}` |
| `GET /__test__/submissions/<submission_id>` | One submission record |
| `POST /__test__/submissions/<submission_id>/reveal` | Makes a withheld confirmation visible on later page visits |
| `GET /__test__/captcha/<token>` | `{"token", "answer", "used"}`, which stands in for the person solving it |
| `POST /__test__/reset` | Clears all state, including counters, uploads, drafts and sessions |
| `POST /__test__/shutdown` | Stops this server |

A submission record looks like this:

```json
{
  "submission_id": "sub_000001",
  "sequence": 1,
  "confirmation_reference": "BWA-000001",
  "job_id": "standard", "job_code": "BWA-ENG-101",
  "job_title": "Senior Data Platform Engineer", "company": "Brambleway Analytics",
  "received_at": "2026-09-22T21:55:07Z",
  "confirmation_visible": true, "revealed_at": null, "draft_id": null,
  "fields": {"first_name": "Avery", "skills": ["sk_python", "sk_sql"], "sponsorship": "no_sponsorship"},
  "extra_fields": {},
  "files": {"resume": {"upload_id": "upl_000001", "filename": "resume_avery_quill.pdf",
            "content_type": "application/pdf", "size": 802, "sha256": "…", "stored_path": "…"}}
}
```

`fields` holds the declared, non-empty received values. Single-choice fields are strings, a checked
single checkbox is `"yes"`, and checkbox groups and multiselects are lists in document order.
`extra_fields` holds any undeclared names that were posted. CAPTCHA and retained-upload hidden fields are excluded.

## Fixtures

`tests/fixtures/browser/` contains:

- `candidate.json`: the fictional candidate's contact details, facts and a saved answer. It is raw fixture data, **not** a canonical contract. It deliberately has no notice period, salary, certification or attestation.
- `resume_avery_quill.pdf`: a valid one-page PDF, 802 bytes.
- `user_inputs.json`: the answers the fictional user gives when prompted, by job id and question label. It includes the corrected 10-digit phone and the sign-in credentials.

## Tests

```bash
uv run --no-project --python 3.12 python -m unittest discover -s tests/browser -p 'test_mock_ats.py' -v
```

The tests use only the standard library. They fill forms by visible label and encode
them the way a browser submits native forms (multipart or urlencoded). They also
start the CLI as a subprocess to check the printed origin, the ready file and signal
or endpoint shutdown. All state is written to temporary directories.
