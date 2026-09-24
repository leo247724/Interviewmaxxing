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
| `captcha-widget` | Frontend Platform Engineer (BWA-FE-112) | The `standard` form plus an invisible reCAPTCHA-style badge: a `.g-recaptcha[data-sitekey]` container, a small badge iframe (`/captcha/widget.html`, `title="reCAPTCHA"`) and a hidden, required `g-recaptcha-response` textarea with a hidden label. Nothing is solved on the page. The POST is accepted only when `g-recaptcha-response` is non-empty; otherwise it is a 422 re-render with "Please complete the CAPTCHA." A test stands in for the widget by setting the textarea's value (for example `page.evaluate`). The token is never recorded in `fields` or `extra_fields`. |
| `spa-loading` | Data Platform Engineer (BWA-DE-113) | The GET shows only `<p aria-busy="true">Fetching application form</p>`; page script injects the `standard` form from a `<template>` 1.5 s after load (no network). A 422 re-render after a POST shows the form immediately. |
| `cookie-banner` | Analytics Platform Engineer (BWA-AE-114) | The `standard` form under a full-viewport modal `role="dialog"` ("This website uses cookies") with `type="button"` buttons "Cookies settings", "Accept all" and "Decline all". While it is shown, `main` is `inert` and `aria-hidden`, so the form's controls are neither visible to an inspector nor clickable. Accept or Decline removes the dialog, restores `main` and sets the `bwa_consent` cookie (`accepted`/`declined`, one day); later visits with that cookie show no banner. "Cookies settings" does nothing. |
| `react-select` | Growth Marketing Manager (BWA-GH-120) | Greenhouse-style: the `standard` questions, with work authorization (`question_6001`, Yes/No), visa sponsorship (`question_6002`, Yes/No), "How did you hear about us?" (`question_6003`: LinkedIn, Indeed, Company website, Referral, Other) and a phone "Country" (`question_6004`, 31 options such as "United States +1"; "Canada +1" shares its dial code) as React-select replicas instead of native controls. Each is an editable `input.select__input[role=combobox][aria-autocomplete=list][aria-haspopup=true][aria-expanded=false]` with `aria-labelledby="<id>-label"`, a `<label for>`, and **no `aria-controls` while closed**. A click toggles the menu (a click on an open one closes it); `question_6003` also opens on focus. The open menu is a portal `div.select__menu` appended to `document.body` holding `div#react-select-<id>-listbox[role=listbox]` with `div#react-select-<id>-option-<i>[role=option]` (ids per open; the chosen one has `aria-selected=true` and the class `select__option--is-selected`); the input then has `aria-expanded=true`, `aria-controls` and `aria-activedescendant`. Typing filters, ArrowDown/Up move, Enter or a click selects (Tab selects too), Escape closes; a blur closes. Like react-select, when `navigator.userAgent` matches `/Mac|iPhone|iPad/` the options carry no `aria-selected` and `aria-activedescendant` stays empty; the selected class remains. The chosen value shows in `div.select__single-value` (the dial-code select shows only the dial code: "+1" for both "United States +1" and "Canada +1"); with no value a `div.select__placeholder` "Select..." is the input's `aria-describedby`. |
| `div-combobox` | Lifecycle Marketing Specialist (BWA-RP-121) | Rippling-style: `div#field-3` and `div#field-4` `[role=combobox][aria-haspopup=listbox][aria-autocomplete=list]` with a child `<p>Select</p>` and the question in a preceding sibling `<p>` (no label association, `*` marker, `aria-required`). `field-3` opens on click; `field-4` **only** on focus + ArrowDown. `aria-controls="field-N-list"` exists only while open; the list is `ul#field-N-list[role=listbox]` with `li#field-N-list-option-i[role=option]` ("No", "Yes"). Escape sets `aria-expanded=false`; a keyboard-opened list is only hidden and stays in the document, so two lists can coexist. Also a phone block whose country-code search `input#field-7-country[role=combobox][data-testid=input-select-search-input]` is pre-filled "+1 US" and lists codes only after typing, beside `input#field-7[type=tel][name=phone]`, and a "Pronouns" search combobox (`field-9`) that lists four options when opened. The search inputs of questions are rendered by page script without a label (the preceding `<p>` is the question). |
| `typeahead` | Field Marketing Manager (BWA-GH-122) | Lookups that list suggestions only after typing: a React-select-style async "Location (City)" (`input#candidate-location`; "Type to search" when opened, "Type at least 3 characters", "Loading..." while it fetches `/__fixture__/cities?style=long&q=`, "No options"), a Rippling-style role-less "Location" (`input#field-42[aria-haspopup=listbox][aria-autocomplete=list]`, no `role`; suggestions such as "Austin, TX, USA" from `/__fixture__/cities?style=short&q=`) and a "What state do you live in?" search (`field-43`, "Start typing to search" until typed, state names, abbreviations understood). A value is committed only by choosing a suggestion; typing again un-chooses it. The server rejects a typed but unchosen string. |
| `phone-widget` | Partner Marketing Manager (BWA-GH-123) | An intl-tel-input replica: `div.iti` holding `button.iti__selected-country[aria-haspopup=dialog][aria-controls=iti-0__dropdown-content]` (`aria-label="Change country, selected United States (+1)"`), a hidden `div#iti-0__dropdown-content[role=dialog]` with a search `input[role=combobox][aria-controls=iti-0__country-listbox]` and an **always present** `ul#iti-0__country-listbox[role=listbox]` of 32 `li[role=option]` ("Afghanistan+93", …), and `input#phone[type=tel]`. Typing a value that starts with `+` selects the country by dial code (+1 keeps United States, +44 selects United Kingdom), updates the button label and flag class and reformats the display ("+1 561-555-0100"). Also a React-select "How did you hear about us?" (`question_7003`, optional). The server accepts only a consistent country and a 10-digit US national number. |
| `react-select-inline` | Performance Marketing Manager (BWA-GH-125) | Greenhouse-style. The React selects (`question_9001` work authorization, `question_9002` sponsorship, `question_9003` "How did you hear about us?", `question_9004` Country) render their menu **inside the form**, next to the control's unnamed wrapper `div` (no `menuPortalTarget`). Each has a real icon-only `button[aria-label="Toggle flyout"]`. While a required select is empty it also renders a hidden required proxy `input.select__required[aria-hidden=true][tabindex=-1][name=<id>]`, removed once a value is chosen. The Country options show a flag `div` before "United States +1" …; its display is the dial code, and typing filters on the country's name only ("United States +1" leaves no option). The résumé is Greenhouse's uploader: `div.file-upload[role=group][aria-labelledby=upload-label-resume]` ("Resume/CV*") with an "Attach" button, a "Dropbox" button and a visually hidden `input#resume[type=file]` (no `name`), labelled "Attach". Once it takes a file it replaces its button container, input included, with `div.file-upload__filename` (the name and a "Remove file" button); the file lives in page state. A page-level `button#autofill-application` "Autofill my application" outside the form is disabled for 120 ms on every `input` event. The work-authorization and sponsorship selects are clearable: once answered they show an icon-only `button[aria-label="Clear selection"]` in their indicators. The submit button stays disabled until every required question is answered (checked every 100 ms), and a text input that loses focus gets `aria-invalid="false"`. |
| `react-select-inline-async` | Lifecycle Marketing Manager (BWA-GH-128) | `react-select-inline` with Greenhouse's uploader as it behaves live. The input keeps the file while the upload runs. 2.5 s after the attach, the résumé's `div.field-wrapper` is replaced by a new element showing the file's name ("Remove file" instead of the input and its buttons). The page's action area is re-rendered too: the submit button moves from `div.form-actions > div.actions-row` up into `div.form-actions`, so its path selector shifts. |
| `workable-like` | Demand Generation Manager (BWA-WK-127) | Workable-style. An intl-tel-input 18 phone with `separateDialCode`: `div.iti__selected-flag[role=combobox][aria-haspopup=listbox][aria-label="Telephone country code"][title=<country>]` shows the code only in `div.iti__selected-dial-code` ("+1"). Typing "+1…" takes the code out of the input, leaving the national digits; "+44…" selects the United Kingdom. The résumé is a dropzone whose `input#input_files_input_resume[type=file]` (visually hidden, no `name`) is emptied once the widget takes the file. The widget then shows the name in `[data-id=filename]` (two spans; the first space a no-break space) and a "Delete" button. |
| `div-combobox-orphan` | Brand Marketing Manager (BWA-RP-126) | Rippling-style, rendered **without a `<form>`**; a page-script "Submit application" (`type=button`) posts the named controls plus widget state as multipart. Its popover div comboboxes (`field-55` Gender, labelled by `aria-labelledby`; `field-63`, a custom question named only by its paragraph) keep their placeholder as `aria-label` ("Select..."/"Select"). Focus or a click opens them; a click on an open one, Escape and blur do nothing. Only a choice or a document `mousedown` outside the control and popover closes them. The list is `ul[role=listbox]` in a `div[role=dialog][data-testid=popper]` (with a status line) inside the question block. Options carry `aria-setsize`/`aria-selected` and wrap `div > div[data-testid=menuListLabel] > p`; a chosen label replaces the placeholder `<p>` as a bare text node. The role-less location lookup `input#field-42` (`aria-label="textbox"`, `aria-labelledby`) never gets `aria-expanded` and shows its popper only with results. Its first query answers after about 1.8 s. |
| `multiselect-react` | Content Marketing Manager (BWA-GH-124) | A React-select-style multi-select "Which marketing channels have you managed?" (`question_8001`, required) whose listbox has `aria-multiselectable=true` and whose choices appear as chips (`div.select__multi-value` with a `role=button` "Remove …"), beside a single-choice React select (`question_8002`). The runtime must leave the multi-select to the user. |

### Static pages

| Path | Result |
| --- | --- |
| `GET /closed` | HTTP 200 page for a job that is gone, worded like some ATS vendors: "We're sorry, that job does not exist or is not currently active." No form, no apply link. |
| `GET /closed/not-found` | HTTP 200 page for a removed job, worded like Ashby: "Job not found" / "The job you requested was not found." |
| `GET /captcha/widget.html` | The tiny document the `captcha-widget` badge iframe shows. |
| `GET /forms/unlabeled-custom-questions` | A Lever-style application form (GET only; the POST is not served): two labelled contact fields plus three custom questions with **no** `<label>`, each a `<div class="application-label">Question ✱</div>` before an input named `cards[<uuid>][field1..3]` (the third is a `<select>`), and no `required` attribute on the custom inputs. The visible question must become the field's wording, the marker its required flag. Also three yes/no radio groups (`CA_9001`–`CA_9003`) whose radios are labelled only "YES"/"NO", with the question in a preceding block starting with `*` and no `required` attribute: one inside its own question block, two in a flat layout where the question `<p>` is a preceding sibling. |
| `GET /forms/choices-without-values` | Two radio groups whose members share an opaque `<uuid>_<uuid>` name and have `value=""` with distinct labels (years of experience; marketing automation platform), the question in a block before each group. The first group's inputs have no ids, the second's do (`opt-hubspot` …). Inspection must synthesize stable option values and keep every member selectable. GET only. |
| `GET /postings/apply-wording?text=<wording>&kind=link\|button\|form` | The `standard` posting with one apply control of the given wording: an `<a>` to `/jobs/standard/apply` (`link`, default), a script-navigating `type="button"` (`button`), or a submit button in a form with only a hidden input posting to `/postings/go-apply` (`form`). `POST /postings/go-apply` answers 303 to `/jobs/<job>/apply`. |
| `GET /postings/with-select` | The `standard` posting rendered like some ATS vendors do: two `type="button"` "Apply now" buttons that navigate by script to `/jobs/standard/apply`, a share widget ("Share", "Copy link") and a `<select name="locale">` with two options, none of them inside a form. A job description, never an application form. |
| `GET /__fixture__/cities?style=long\|short&q=<text>` | The site's own city suggestions that the lookup widgets fetch (JSON list, at most 10, after a fixed 250 ms): every query word must begin a word of the city, with US state names and abbreviations and "USA"/"United States" equivalent; fewer than 2 characters returns `[]`. `long`: "Austin, Texas, United States", "Austin, Minnesota, United States", "Austintown, Ohio, United States", "Austin, Indiana, United States", "Austin, Arkansas, United States", "Denver, Colorado, United States", "Boulder, Colorado, United States", "Round Rock, Texas, United States", "Aurora, Colorado, United States", "Dallas, Texas, United States". `short`: "Austin, TX, USA", "Austin, MN, USA", "Austintown, OH, USA", "Denver, CO, USA", "Boulder, CO, USA", "Round Rock, TX, USA". Part of the fictional site (not a test-only route). |

### Script widgets

The widget scenarios keep their answers only in page state: there are **no hidden
inputs**. The Greenhouse and Workable uploaders keep their file there too. `window.__widgetState` maps each field name to `{"value": …}` (a list for the
multi-select; `{"country": iso2}` for the phone widget) and the form's `formdata` event
appends those entries when the form is submitted (constructing `new FormData(form)` runs
it too, without sending anything). React-select values are option machine values
(`rs_wa_yes`, `us`, `src_linkedin`); Rippling menus, lookups and search comboboxes post
their visible labels. Server validation is the shared one: required, a listed value, and
for the phone widget the country/number rule. A test can make a widget choose the option
after the clicked one with `window.__widgetHooks.selectNext["<element id>"] = true`, make a
React select commit another option than the one clicked with
`window.__widgetHooks.selectValue["<element id>"] = "<option value>"` ("ca" for "Canada +1"),
or make it expose none of the three selection signals (`aria-selected`,
`aria-activedescendant`, the `select__option--is-selected` class) with
`window.__widgetHooks.hideSelection["<element id>"] = true`; each provokes a readback
mismatch. `window.__widgetHooks.stuck["<element id>"] = true` makes a Rippling popover
that nothing outside it closes. `window.__widgetHooks.uploadError["resume"] = true` makes
the Workable dropzone show an error alert instead of the file, and
`window.__widgetHooks.keepFile["resume"] = true` makes it keep the file in its input.
`window.__widgetHooks.uploadDelayMs = <ms>` changes the async Greenhouse uploader's delay.
`window.__widgetHooks.uploadRenderOn = "focusin"` makes it re-render as the next question
takes focus, i.e. while a later answer is being written. These hooks must be set before
the page script runs (an init script). Headless sessions of the runtime present a Linux user agent; a test can start a
session with a Mac one (`PlaywrightSessionFactory(user_agent=...)`) to get the Apple
behaviour.

### Shared behavior

- **Disabled options.** `missing-required` offers "3 months or more (no longer offered)" as a disabled option; posting its value is rejected.
- **Machine values differ from labels.** Selects, radios and checkboxes post values such as `wa_authorized` and `sk_python`, while users see "Yes, I am authorized to work in the US" and "Python". Posting a label or an unknown value is rejected with "Select one of the listed options."
- **Accessible markup.** Every control has a `<label for>`. Radio and checkbox groups are `<fieldset>`s with a `<legend>`. Required controls use `required`; the `*` marker is `aria-hidden`, and optional controls say "(optional)". Controls can be found by accessible name, for example Playwright `get_by_label("First name")`. No test IDs or product-specific hooks are needed.
- **Rejection.** An invalid POST returns 422 and re-renders the form. The page gets an error summary (`role="alert"`, "There is a problem with your application", with links to the fields). Each invalid field gets an inline error, `aria-invalid="true"` and `aria-describedby`. Values are preserved. A valid resume from the rejected POST is retained as "Currently attached: …" with a hidden `resume_upload_id`, so the file input stops being required. Rejections are recorded but **never counted as submissions**.
- **Every accepted POST counts.** Resubmitting an identical form creates another record with a new reference, so a mistaken retry is detectable.
- **Status page.** `/jobs/<job_id>/application-status?email=…` is a public page linked from each posting ("Already applied? Check your application status"). For each matching record it shows the reference, or "still processing" while the confirmation is withheld. This page is how a runtime reconciles an uncertain outcome.
- **Job identity.** Postings include the title, company, location and Job ID. They also carry a canonical link and a schema.org `JobPosting` JSON-LD block with `identifier.value` set to the Job ID. The apply pages repeat the same identity line.
- **Deterministic ids.** Ids come from counters in a fresh state dir: `sub_000001`/`BWA-000001`, `dft_000001`, `upl_000001`, `cap_000001`. Captcha answers are derived from the token. Only timestamps vary. There are no artificial delays except the fixed 250 ms of `/__fixture__/cities`.

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
