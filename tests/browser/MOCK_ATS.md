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
required flag and option `value`/`label` pairs, and each job's flags (such as
`fixture_identity`).

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
| `phone-dialcode-collision` | Growth Operations Manager (BWA-GH-129) | Greenhouse's phone fieldset. The "Country" React select (`country`, 31 dial-code options, inline menu) shows its value as Greenhouse does: a flag `div` and `<span>` holding "+" and the code as two text nodes. Choosing it sets the phone widget's country and focuses the number. The phone is an intl-tel-input with a separate dial code whose flag `div[role=combobox]` is also named "Country". It shows the code as two text nodes, and typing "+1…" leaves the national digits. Also a React-select work-authorization question. The runtime must report one Country question, bound to the select. |
| `bamboohr-like` | Paid Media Manager (BWA-BH-181) | BambooHR-style address block of Fabric selects: State (`state.value`, required, the 51 state names, showing the placeholder "–Select–" with en dashes), Country (`countryId.value`, required, ten countries, **already showing "United States"**) and "Highest Education Obtained" (`educationLevelId`, optional), after first name, last name and email. Each is a `<label for>` naming a hidden proxy `select#<id>[aria-hidden=true][tabindex=-1][readonly]` that holds a single option, the chosen option's id ("1" for United States; `required` when the question is), beside a role-less `button.fab-SelectToggle[type=button][aria-haspopup=true][aria-expanded][data-menu-id=fab-menu…]` whose `aria-label` is the label plus what it shows ("Country United States") and whose text is a `div.fab-SelectToggle__content` value or a `div.fab-SelectToggle__placeholder`. While a value is held a "Clear Selection" button follows it. The first opening appends a body portal `div[data-helium-id=<menu id>]` holding a search box (`input.fab-MenuSearch__input`, which takes focus) and `div#<menu id>[role=menu]` of `div.fab-MenuOption[role=menuitem]` (no `aria-selected`; the current value's item is `fab-MenuOption--active` and the menu's and search box's `aria-activedescendant`). State's items take the state's id as element id ("1", "2", …); the others are numbered `menu-item-<n>` from one page-wide counter. A click, Enter, Space or ArrowDown opens it; Escape (on the button or in the menu) or a click on the button closes it; an outside press or a blur does not. After closing, the portal stays in the document, hidden. Choosing an item closes the menu and puts the item's id into the proxy, which the form posts. |
| `workable-like` | Demand Generation Manager (BWA-WK-127) | Workable-style. An intl-tel-input 18 phone with `separateDialCode`: `div.iti__selected-flag[role=combobox][aria-haspopup=listbox][aria-label="Telephone country code"][title=<country>]` shows the code only in `div.iti__selected-dial-code` ("+1"). Typing "+1…" takes the code out of the input, leaving the national digits; "+44…" selects the United Kingdom. The résumé is a dropzone whose `input#input_files_input_resume[type=file]` (visually hidden, no `name`) is emptied once the widget takes the file. The widget then shows the name in `[data-id=filename]` (two spans; the first space a no-break space) and a "Delete" button. |
| `teamtailor-like` | Growth Marketing Lead (BWA-TT-171) | Teamtailor-style Dropzone uploaders: "Upload resume" (required) and "Additional files". In `div#upload_resume_field`, page script mounts the hidden file input in the trigger `div` and hands it the label's id, `candidate_resume_remote_url` (opacity 0 over the drop zone, `aria-label="Drop your file or upload, Upload resume"`). A chosen file disables that input, hides the trigger and replaces the input with a fresh one that has no id or `required`. It also adds a preview from a `<template>`: the file's name in a hidden `a[data-dz-name]` with a "Clear file selection" button, "Uploading…" with a remove link, and a hidden, disabled `input[type=text][name="candidate[resume_remote_url]"]` that reuses the id. After 1.2 s (`uploadDelayMs`) the upload ends: the name shows, the URL input gets a fictional stored-file address, and the fresh input gets the id back. From then on `#candidate_resume_remote_url` names two elements. Each chosen file counts in `window.__filesTaken` (each is an upload). |
| `jobvite-like` | Paid Media Manager (BWA-JV-190) | Jobvite-style. Until the consent is accepted, `GET /jobs/jobvite-like/apply` (and a `POST` without it) is a "Data Consent" page: `<h3>Data Consent</h3>` and `form[name=consentForm][method=POST]` (to the apply URL) with `label[for=jv-country-select]` "Location of Residence and Language:", `select#jv-country-select[required]` (no `name`; "Select your location of residence and language" and one policy, `policy-7d1f`) and a "Back" link to the posting. Choosing the policy shows its text, a submit `button` "I Accept", an "I Decline" link to the posting and a hidden `policyIds` input. The accept `POST` (it carries `policyIds`) records the consent (`consents` in `/__test__/submissions`) and returns the form (200); the accept click also sets the `bwa_jv_consent=accepted` cookie, with which the apply URL shows the form directly. The form: an `<h3 id="jv-resume-header">Add Resume*</h3>` naming a `button[aria-haspopup=true][aria-labelledby=jv-resume-header][aria-required=true]` "Select" inside `div#attach-resume`, then first name, last name, email, phone and two selects shaped like Jobvite's screening questions (referral, sponsorship). Page script appends a hidden `div.jv-add-attachment[role=dialog][aria-label="Attachment Options"]` to `body`, outside the form: "Dropbox", a `label[for=file-input-0]` "File" with the visually hidden `input#file-input-0[type=file]` (no `name`), "Type or Paste Resume" and "Close". "Select" toggles the popup. A chosen file goes into page state (the input keeps it; each counts in `window.__filesTaken`), the popup closes, the button's `div.jv-select` is hidden and `ul.jv-file-list` lists the name with a "Remove" link. The form's `formdata` event posts the stored file as `resume`. |
| `div-combobox-orphan` | Brand Marketing Manager (BWA-RP-126) | Rippling-style, rendered **without a `<form>`**; a page-script "Submit application" (`type=button`) posts the named controls plus widget state as multipart. Its popover div comboboxes (`field-55` Gender, labelled by `aria-labelledby`; `field-63`, a custom question named only by its paragraph) keep their placeholder as `aria-label` ("Select..."/"Select"). Focus or a click opens them; a click on an open one, Escape and blur do nothing. Only a choice or a document `mousedown` outside the control and popover closes them. The list is `ul[role=listbox]` in a `div[role=dialog][data-testid=popper]` (with a status line) inside the question block. Options carry `aria-setsize`/`aria-selected` and wrap `div > div[data-testid=menuListLabel] > p`; a chosen label replaces the placeholder `<p>` as a bare text node. The role-less location lookup `input#field-42` (`aria-label="textbox"`, `aria-labelledby`) never gets `aria-expanded` and shows its popper only with results. Its first query answers after about 1.8 s. |
| `multiselect-react` | Content Marketing Manager (BWA-GH-124) | A React-select-style multi-select "Which marketing channels have you managed?" (`question_8001`, required) whose listbox has `aria-multiselectable=true` and whose choices appear as chips (`div.select__multi-value` with a `role=button` "Remove …"), beside a single-choice React select (`question_8002`). The runtime must leave the multi-select to the user. |
| `autofill-upload` | Revenue Operations Analyst (BWA-AS-130) | Ashby-style resume parsing. Fields in DOM order: first name, last name, email, phone, LinkedIn (optional) and the standard native `#f-resume` **last**, so filling in DOM order types the names before the upload. On each `change` of `#f-resume` with a file, page script inserts (once) `div#resume-parse-status[role=status][aria-live=polite][aria-busy=true]` right after the input ("Parsing your resume…" after a `span.spinner`), and `autofill_ms` (600) later overwrites `#f-first_name` = "A.", `#f-last_name` = "Quill (resume)" and `#f-email` = "a.quill@resume-parser.example.test" (value assignment plus bubbling `input` and `change`, all untrusted), then sets the status to "We filled in some fields from your resume." with `aria-busy=false` (it stays visible). The input keeps its file; another change repeats everything. `fixture_identity`. |
| `custom-uploader` | Customer Marketing Manager (BWA-GH-131) | Greenhouse-style uploaders that page script mounts (the static HTML holds one `div[data-mount-html]` placeholder each). Resume: `div#resume-field.field.uploader[role=group][aria-labelledby=resume-label][aria-required=true]` with "Resume/CV *", a drop zone `#resume-dropzone` ("Drop or select a file", `button#resume-button` "Upload resume", which opens the file chooser), a hint, `input#resume-input[type=file][name=resume]` with `display:none` and **no label, no `aria-label`, no `required`**, `div#resume-chip.file-chip[hidden]` and `div#resume-notice.upload-notice[role=status][aria-live=polite]`. On `change` the file moves into page state and the input is **cleared at once**; the chip shows `span.file-chip__name`, `span.file-chip__size` "(N bytes)" and `button.file-chip__remove[aria-label="Remove file"]`; the notice says "Uploading…" (`aria-busy=true`), then after `upload_ms` (800) "<name> uploaded" (`aria-busy=false`). The remove button clears the page state, the chip and the notice. A `drop` on the zone goes through the input. The form's `formdata` event posts the stored file as `resume`. Cover letter (optional): `input#cover-letter-input[name=cover_letter].visually-hidden` inside `label.upload-label[data-testid=cover_letter]` ("Attach cover letter", no `for`); it keeps its file and `#cover-letter-chip` shows its name. Both are validated like `file` (kinds `custom_file` and `label_file`). A 422 re-render shows them empty with an inline error; nothing is retained. `fixture_identity`. |
| `linkedin-autofill` | Marketing Operations Specialist (BWA-LV-132) | Lever-style: the native `#f-resume` first, then `name` ("Full name", `autocomplete=name`), `email`, `phone`, `location` ("Current location", optional) and `urls[LinkedIn]` ("LinkedIn URL", optional, id `f-urls[LinkedIn]`). Before the first field block, `div#awli` holds `button#linkedin-apply.awli-button[aria-busy=true]` "Loading…" (after `loading_ms`, 1500, "Apply with LinkedIn" without `aria-busy`) under `div#linkedin-overlay.awli-overlay[title="Apply with LinkedIn"]`, a transparent overlay covering the button exactly, so a pointer click lands on the overlay. The button's own click sets `name` = "LinkedIn Member" and `email` = "member@linkedin.example.test". An autofill prompt `div#autofill-prompt[role=dialog][aria-modal=true]` ("Autofill your application?", buttons `#autofill-prompt-accept` "Autofill with LinkedIn" and `#autofill-prompt-dismiss` "No thanks") is appended to `body`, outside `main`, after load or after the resume changes (see below); while it is shown, `main` is `inert` and `aria-hidden`. "No thanks" removes it and restores `main`; "Autofill with LinkedIn" does too and sets the LinkedIn values. A closed prompt never returns. `fixture_identity`. |
| `react-controlled` | Retention Marketing Manager (BWA-RC-133) | React-like controlled inputs: first name, last name, email and phone (standard markup) inside `div#react-root`, the native resume outside it. Only trusted `input` events (typing: `page.fill`, `press_sequentially`) update the page state (`__mock.state`, which starts from the rendered values); every 150 ms and on `focusout` any other value, such as a value assignment followed by a synthetic `input` event, is reset to the state. The first typed change re-renders the fields once: after `rerender_ms` (0) the field blocks give way to `p#react-saving[aria-busy=true]` "Saving draft…", and `unmount_ms` (300) later **new** elements with the same ids, names, labels and attributes are mounted with the state's values (element handles taken before are disconnected). With `?lose_first=1` the first typed value never reaches the state (typing before hydration), so the re-render drops it. The form's `formdata` event posts the state. `fixture_identity`. |

### Static pages

| Path | Result |
| --- | --- |
| `GET /closed` | HTTP 200 page for a job that is gone, worded like some ATS vendors: "We're sorry, that job does not exist or is not currently active." No form, no apply link. |
| `GET /closed/not-found` | HTTP 200 page for a removed job, worded like Ashby: "Job not found" / "The job you requested was not found." |
| `GET /captcha/widget.html` | The tiny document the `captcha-widget` badge iframe shows. |
| `GET /forms/unlabeled-custom-questions` | A Lever-style application form (GET only; the POST is not served): two labelled contact fields plus three custom questions with **no** `<label>`, each a `<div class="application-label">Question ✱</div>` before an input named `cards[<uuid>][field1..3]` (the third is a `<select>`), and no `required` attribute on the custom inputs. The visible question must become the field's wording, the marker its required flag. Also three yes/no radio groups (`CA_9001`–`CA_9003`) whose radios are labelled only "YES"/"NO", with the question in a preceding block starting with `*` and no `required` attribute: one inside its own question block, two in a flat layout where the question `<p>` is a preceding sibling. |
| `GET /forms/choices-without-values` | Two radio groups whose members share an opaque `<uuid>_<uuid>` name and have `value=""` with distinct labels (years of experience; marketing automation platform), the question in a block before each group. The first group's inputs have no ids, the second's do (`opt-hubspot` …). Inspection must synthesize stable option values and keep every member selectable. GET only. |
| `GET /forms/breezy-like` | A Breezy HR application as it renders live (BWA-BZ-172), with no `<label>` anywhere. Each question is an `<h3>` (with a `*` span) before its control. Contact inputs carry placeholders equal to their headings ("Full Name"), and an SMS consent checkbox after the phone input states its own text. A "Desired Salary" block holds a currency select, the amount input (placeholder "Desired Salary") and an **unnamed** pay-period select (Hourly … Yearly). Custom questions are named `section_1787064635874_question_0..4`: two text inputs, a textarea, a Yes/No radio group in `ul.options` with wrapping labels, and a checkbox group whose options have **neither labels nor values**, only a `<span>` each (Google Ads, Meta, LinkedIn, TikTok). There is also an EEO race radio group headed "Race or Ethnicity" whose first option is "White (not Hispanic or Latino)". Labels must be the headings, options their own texts. GET only. |
| `GET /forms/ashby-like` | An Ashby application as it renders live (BWA-AS-173), with **no `<form>`** (page script posts it). Each question is a field entry titled by `<label for="<field path>">`. Name and Email are system fields. "What is your expected salary?" has a `Type here...` placeholder. A react-datepicker date input has **no id or name**, so its title labels nothing, and a `Pick date...` placeholder. On focus it opens, inside its own entry, an absolutely placed calendar above itself: a `role=listbox` month of `role=option` days and two unnamed month buttons. A click on a day writes MM/DD/YYYY; a typed M/D/YYYY is kept on blur, Enter, Tab or Escape, anything else is cleared. It also has: a referral radio group sharing one name; a radio group ("How would you like to work?") and a checkbox group ("Will you now or in the future require sponsorship?") whose options are **named after their own text** (`name="Yes"`/`name="No"`, like Compyl's), which page script keeps exclusive; and a yes/no question drawn as two `type="submit"` buttons with `aria-pressed` over a `display:none` checkbox that mirrors "yes" (its title's `::after` draws the required "*"). A Location lookup (`role=combobox`, `Start typing...`, its title `for="_systemfield_location"` labelling nothing) mounts its suggestion portal on focus: a wrapper holding a `role=listbox` and a "Powered by Google" link, appended to `<body>` (`?portal=inline`: right after its field entry, before the later questions). The input's `aria-controls` names the wrapper (`?owns=listbox`: the listbox). Suggestions come from `/__fixture__/cities?style=long` for 2+ typed characters; a click writes the suggestion into the input and unmounts the portal, as do Escape and blur. GET only. |
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
`window.__widgetHooks.uploadDelayMs = <ms>` changes the async Greenhouse uploader's delay
and how long the Teamtailor-style uploader shows "Uploading…".
`window.__widgetHooks.valueText["<element id>"] = "<text>"` makes a React select show that
text as its value, for a display that names no option.
`window.__widgetHooks.uploadRenderOn = "focusin"` makes it re-render as the next question
takes focus, i.e. while a later answer is being written. The `bamboohr-like` Fabric selects
are the exception to "no hidden inputs": they post their proxy `<select>`. For them
`selectNext` is keyed by the field name (`"state.value"`), and
`window.__widgetHooks.fabIgnore["<field name>"] = true` makes a click on a menu item change
nothing (the menu still closes). These hooks must be set before
the page script runs (an init script). Headless sessions of the runtime present a Linux user agent; a test can start a
session with a Mac one (`PlaywrightSessionFactory(user_agent=...)`) to get the Apple
behaviour.

### Upload and autofill scenarios

`autofill-upload`, `custom-uploader`, `linkedin-autofill` and `react-controlled` are
single-page forms whose page scripts change or hide what a runtime fills in.

**Only the candidate's own values are accepted.** These jobs have `fixture_identity: true`.
After the shared validation, and only for a field without another error, the server
rejects:

- A resume whose bytes are not `tests/fixtures/browser/resume_avery_quill.pdf` (compared by
  sha256; a retained `resume_upload_id` is compared by its stored sha256; if the fixture file
  is missing, every resume is rejected): "The attached resume is not the file the candidate
  chose." A rejected resume is not retained for the next attempt.
- Any identity field the job has whose value is not exactly the candidate's (`first_name`
  "Avery", `last_name` "Quill", `email` "avery.quill@example.test", `name` "Avery Quill"):
  "This value was not entered by the candidate."

Rejections are ordinary 422 re-renders. Nothing changes for other jobs.

**`window.__mock`.** Each page script defines `window.__mock`: a `log` of
`{t, event, detail?}` entries (`t` is `Math.round(performance.now())`; `detail` is an
optional string) plus the counters below. Every trusted `input` event on a form control is
logged as `input:<control name>`. Typing (`page.fill`, `press_sequentially`) and
Playwright's `set_input_files` with a file **path** (the browser sets the files) are
trusted. Script-made events are not logged: the scenarios' own writes, and
`set_input_files` with an in-memory buffer payload, which Playwright applies by script.
Read the object with `page.evaluate("() => JSON.parse(JSON.stringify({...window.__mock, files: undefined}))")`.

| Job id | Query parameters (defaults) | Log events | Counters and state |
| --- | --- | --- | --- |
| `autofill-upload` | `autofill_ms` (600, after each resume change) | `upload` (detail: file name), `autofill` | `uploads`, `autofills` |
| `custom-uploader` | `upload_ms` (800, after each resume change) | `upload` (name), `uploaded` (name), `removed`, `cover-upload` (name) | `uploads`, `coverUploads`; `files.resume` is the stored `File` (not serialisable) |
| `linkedin-autofill` | `loading_ms` (1500, after the load event); `prompt` = `load` (default), `upload` or `none`; `prompt_ms` (400 after the load event, or 300 after the first resume upload) | `linkedin-ready`, `overlay-click`, `linkedin-click`, `upload` (name), `prompt-shown`, `prompt-dismissed`, `prompt-accepted` | `overlayClicks`, `linkedinClicks`, `promptShown`, `promptDismissed`, `promptAccepted`, `uploads` |
| `react-controlled` | `rerender_ms` (0, after the first typed change); `unmount_ms` (300); `lose_first=1` | `revert:<name>`, `rerender` | `renders`; `state` (name to value) |

The parameters belong to the apply page's own URL, for example
`/jobs/linkedin-autofill/apply?loading_ms=300&prompt=none`. A 422 re-render is posted to
the plain apply path, so it uses the defaults.

### Shared behavior

- **Disabled options.** `missing-required` offers "3 months or more (no longer offered)" as a disabled option; posting its value is rejected.
- **Machine values differ from labels.** Selects, radios and checkboxes post values such as `wa_authorized` and `sk_python`, while users see "Yes, I am authorized to work in the US" and "Python". Posting a label or an unknown value is rejected with "Select one of the listed options."
- **Accessible markup.** Every control has a `<label for>`, except where a scenario says otherwise (script widgets, and the `custom-uploader` inputs that page script mounts). Radio and checkbox groups are `<fieldset>`s with a `<legend>`. Required controls use `required`; the `*` marker is `aria-hidden`, and optional controls say "(optional)". Controls can be found by accessible name, for example Playwright `get_by_label("First name")`. No test IDs or product-specific hooks are needed.
- **Rejection.** An invalid POST returns 422 and re-renders the form. The page gets an error summary (`role="alert"`, "There is a problem with your application", with links to the fields). Each invalid field gets an inline error, `aria-invalid="true"` and `aria-describedby`. Values are preserved. A valid resume from the rejected POST is retained as "Currently attached: …" with a hidden `resume_upload_id`, so the file input stops being required. Rejections are recorded but **never counted as submissions**.
- **Every accepted POST counts.** Resubmitting an identical form creates another record with a new reference, so a mistaken retry is detectable.
- **Status page.** `/jobs/<job_id>/application-status?email=…` is a public page linked from each posting ("Already applied? Check your application status"). For each matching record it shows the reference, or "still processing" while the confirmation is withheld. This page is how a runtime reconciles an uncertain outcome.
- **Job identity.** Postings include the title, company, location and Job ID. They also carry a canonical link and a schema.org `JobPosting` JSON-LD block with `identifier.value` set to the Job ID. The apply pages repeat the same identity line.
- **Deterministic ids.** Ids come from counters in a fresh state dir: `sub_000001`/`BWA-000001`, `dft_000001`, `upl_000001`, `cap_000001`. Captcha answers are derived from the token. Only timestamps vary. The server adds no artificial delays except the fixed 250 ms of `/__fixture__/cities`. Page scripts have their own timers (`spa-loading`, and the upload and autofill scenarios).

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
