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
| `captcha-gate` | Security Operations Analyst (BWA-CG-141) | A "Verify you are human" page with a solvable CAPTCHA widget (`?kind=`) in front of the core form; the widget's callback posts its token and the right one opens the form. See [Solvable CAPTCHA widgets](#solvable-captcha-widgets-round-14). |
| `captcha-form` | Platform Security Engineer (BWA-CF-142) | The core form with a solvable CAPTCHA widget (`?kind=`); the POST is accepted only with that widget's expected token. See [Solvable CAPTCHA widgets](#solvable-captcha-widgets-round-14). |
| `captcha-steps` | Detection Engineer (BWA-CS-143) | Contact information with a reCAPTCHA v2 checkbox, then the resume, then a review page; the first step's POST requires the checkbox's expected token. See [Solvable CAPTCHA widgets](#solvable-captcha-widgets-round-14). |
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
| `bamboohr-conditional` | Paid Media Manager (BWA-BH-182) | BambooHR-style conditional fields. First name, last name, email, then the required radio "Will you now or will you in the future require employment visa sponsorship?" (`sponsorship`, Yes/No) and the required radio "This position is located in Austin, Texas. Are you currently located in the Austin area?" (`located_austin`, Yes/No). Right after the sponsorship fieldset sits an inert `template#f-sponsorship-reveals[data-reveals-for=sponsorship]` holding two follow-up questions: the required select "What is the basis of your current authorization to work in the United States?" (`authorization_basis`: `citizen`, `permanent_resident`, `visa`) and the required radio "Can you provide documentation of your work authorization at hire?" (`authorization_proof`, Yes/No). `REVEAL_JS` mounts the template's content after it the moment either sponsorship option is chosen (its `change` event; synchronously, or `?reveal_ms=<n>` later), once (`window.__mock.reveals` counts the mounts; a `reveal` entry goes into `window.__mock.log`), and on load when the trigger is already checked and the server did not render them. The server validates and records the follow-ups only when a sponsorship answer was posted, and a re-render with errors shows them inline (`revealed_fields`). `Field.reveals_on` restricts a reveal to one option value (choosing another removes the follow-ups); this job reveals on either. |
| `bamboohr-required` | Paid Media Manager (BWA-BH-183) | BambooHR-style conditional requiredness, as on a live BambooHR form: first name, last name, email, then four Yes/No radios (values `Yes`/`No`) named like BambooHR's custom questions: "Are you legally authorized to work in the United States for any employer?" (`customQuestionAnswers.yes_no_2101`, required), "Will you now or will you in the future require employment visa sponsorship?" (`…_2102`), "This position is in Austin, TX. Are you currently located in Austin?" (`…_2103`) and "Are you able to work onsite in our Austin office?" (`…_2104`). The optional ones show no marker at all. The sponsorship fieldset carries `data-required-after="customQuestionAnswers.yes_no_2101"`: the moment an authorization option is chosen, `REVEAL_JS` marks it required (`aria-required="true"`, `required` on its radios and a `<span aria-hidden="true">*</span>` after its question; `window.__mock.required` counts it, a `required` entry goes into `window.__mock.log`). No question appears or disappears. The server requires the sponsorship answer only when an authorization answer was posted (`Field.required_after`). |
| `bamboohr-churn` | Paid Media Manager (BWA-BH-184) | BambooHR-style generated ids that churn, as on live BambooHR forms. First name, last name and email (stable ids), then Address (`streetAddress.value`), City (`city.value`) and ZIP (`zip.value`), each a Fabric text field `div.fab-TextField` whose input and label `for` carry a generated id `FabricTextField-<n>` (`Field.fabric_id`, 48–50), then two required Yes/No radios (`customQuestionAnswers.yes_no_2101` work authorization, `…_2102` visa sponsorship, values `Yes`/`No`), then "Date Available" (`FabricTextField-51`, placeholder `mm/dd/yyyy`, optional), a Fabric text field without a `name` (`Field.unnamed`: never posted, never required, so a runtime knows the question only by its generated id). `FABRIC_JS`: answering either Yes/No mounts every Fabric text field again under the next free numbers (one page-wide counter), value kept, its label's `for` following it (`window.__mock.rerenders` counts the re-renders; a `rerender` entry goes into `window.__mock.log`). |
| `greenhouse-eeo` | Marketing Operations Manager (BWA-GH-130) | Greenhouse-style voluntary self-identification after the custom question. First name, last name, email, the required select "Are you legally authorized to work in the United States?" (`question_7001`: `1` Yes, `0` No), then optional native selects Gender (`gender`), "Are you Hispanic/Latino?" (`hispanic_ethnicity`: `Yes`, `No`, `Decline To Self Identify`) and Veteran Status (`veteran_status`), each with an empty first option. Choosing `No` for Hispanic/Latino makes `REVEAL_JS` (selects trigger reveals too) mount "Please identify your race" (`race`, seven options) right after that question's block, and add `p.hint[data-revealed-hint]` "Race and ethnicity categories are those the U.S. Equal Employment Opportunity Commission defines." to the block itself (`Field.reveal_hint`: the question's help text changes). Another answer removes both. The server records `race` only when `No` was posted. |
| `teamtailor-late` | Demand Generation Director (BWA-TT-172) | Teamtailor-style late question. The apply page shows a job description 2400 px tall (`Job.description_px`) above the form, so the form starts below the fold. The form's first question, the required text "Linkedin profile" (`candidate[answers_attributes][0][text]`, `Field.lazy`), is an empty `div[data-lazy-block]` placeholder (min-height 3rem) holding a `template[data-lazy-question]` until the placeholder scrolls into view; then `LAZY_JS` (an `IntersectionObserver`) mounts the question in place (`window.__mock.lazyMounts`, a `lazy-mount` log entry). Then first name, last name, email and phone. A runtime that only reads the page never sees the question; filling the first fields brings it into view. |
| `paylocity-address` | Paid Media Manager (BWA-PL-212) | Paylocity-style: one step followed by the review page. The step form is `novalidate`, as Paylocity's must be (an input-select's required input stays empty after a choice), and its next control is `button#btn-submit[type=submit]` "Continue". After first name, last name, email and phone come two react-widgets DropdownLists: "We may use SMS during the hiring process. Do you give us permission to text you?" (`info.smsOptedIn`, required, the fictional SMS policy as help text) and "Have you worked with us before?" (`info.haveYouWorkedWithUsBefore`, required). Each is a `<label for>` pointing at `div#<name>.rw-dropdownlist.rw-widget[role=combobox][aria-owns=<id>__listbox][aria-expanded][aria-haspopup=true][aria-autocomplete=list][data-for=<question>][tabindex=0]` with a picker `span` and `div.rw-input` showing `--`. A click, Space or ArrowDown mounts `div.rw-popup-container > ul#<id>__listbox[role=listbox] > li[role=option]` (Yes / No, posted as `true`/`false`) inside it; a click chooses; Escape or an outside press closes it. Country (`address.country`, showing "United States") and State (`address.state`, showing "Select a state", the 51 state names, posted as the abbreviation) are input-selects: a `<label for>` wraps `div.pcty-input-select`, whose `div.input-select-input-single-value` (with `input-select-input-placeholder` while empty) covers the role-less `input[aria-autocomplete=list][maxlength=250][required]` (`#public-site-address-country`, `#public-site-address-us-state`) and takes the pointer (a press on it focuses the input). Typing lists the options containing the text as `div.pcty-input-select__option`s in a `div.pcty-input-select__menu` under the same container; Enter takes the first, a click the one clicked; the value element then shows it. "Address Line 1" (`address.address1`, `#public-site-address-address-1`) is `input[role=combobox][aria-autocomplete=list][aria-controls=public-site-address-address-1-autocomplete-list][maxlength=50]` beside two `role=status` regions; from three characters it lists the fictional addresses containing the text in that `ul[role=listbox]`, and the typed text is kept (a click on a suggestion writes it and sets `window.__addressPicked`; Escape or an outside press closes the list). Address Line 2, City and Zip follow. A OneTrust banner (`#onetrust-consent-sdk > #onetrust-banner-sdk[role=region][aria-label="Cookie banner"]`, fixed over the bottom half of the page) slides in `?cookie_ms=<n>` (600) after load unless the `OptanonAlertBoxClosed` cookie is set, with `#onetrust-policy` text and the buttons "Cookies Settings" (`#onetrust-pc-btn-handler`), "Reject All" (`#onetrust-reject-all-handler`) and "Accept All Cookies" (`#onetrust-accept-btn-handler`); a choice sets that cookie and `window.__cookieChoice` (`reject`/`accept`) and hides it. The review page shows the banner too. |
| `workable-like` | Demand Generation Manager (BWA-WK-127) | Workable-style. An intl-tel-input 18 phone with `separateDialCode`: `div.iti__selected-flag[role=combobox][aria-haspopup=listbox][aria-label="Telephone country code"][title=<country>]` shows the code only in `div.iti__selected-dial-code` ("+1"). Typing "+1…" takes the code out of the input, leaving the national digits; "+44…" selects the United Kingdom. The résumé is a dropzone whose `input#input_files_input_resume[type=file]` (visually hidden, no `name`) is emptied once the widget takes the file. The widget then shows the name in `[data-id=filename]` (two spans; the first space a no-break space) and a "Delete" button. |
| `teamtailor-like` | Growth Marketing Lead (BWA-TT-171) | Teamtailor-style Dropzone uploaders: "Upload resume" (required) and "Additional files". In `div#upload_resume_field`, page script mounts the hidden file input in the trigger `div` and hands it the label's id, `candidate_resume_remote_url` (opacity 0 over the drop zone, `aria-label="Drop your file or upload, Upload resume"`). A chosen file disables that input, hides the trigger and replaces the input with a fresh one that has no id or `required`. It also adds a preview from a `<template>`: the file's name in a hidden `a[data-dz-name]` with a "Clear file selection" button, "Uploading…" with a remove link, and a hidden, disabled `input[type=text][name="candidate[resume_remote_url]"]` that reuses the id. After 1.2 s (`uploadDelayMs`) the upload ends: the name shows, the URL input gets a fictional stored-file address, and the fresh input gets the id back. From then on `#candidate_resume_remote_url` names two elements. Each chosen file counts in `window.__filesTaken` (each is an upload). |
| `jobvite-like` | Paid Media Manager (BWA-JV-190) | Jobvite-style. Until the consent is accepted, `GET /jobs/jobvite-like/apply` (and a `POST` without it) is a "Data Consent" page: `<h3>Data Consent</h3>` and `form[name=consentForm][method=POST]` (to the apply URL) with `label[for=jv-country-select]` "Location of Residence and Language:", `select#jv-country-select[required]` (no `name`; "Select your location of residence and language" and one policy, `policy-7d1f`) and a "Back" link to the posting. Choosing the policy shows its text, a submit `button` "I Accept", an "I Decline" link to the posting and a hidden `policyIds` input. The accept `POST` (it carries `policyIds`) records the consent (`consents` in `/__test__/submissions`) and returns the form (200); the accept click also sets the `bwa_jv_consent=accepted` cookie, with which the apply URL shows the form directly. The form: an `<h3 id="jv-resume-header">Add Resume*</h3>` naming a `button[aria-haspopup=true][aria-labelledby=jv-resume-header][aria-required=true]` "Select" inside `div#attach-resume`, then first name, last name, email, phone and two selects shaped like Jobvite's screening questions (referral, sponsorship). Page script appends a hidden `div.jv-add-attachment[role=dialog][aria-label="Attachment Options"]` to `body`, outside the form: "Dropbox", a `label[for=file-input-0]` "File" with the visually hidden `input#file-input-0[type=file]` (no `name`), "Type or Paste Resume" and "Close". "Select" toggles the popup. A chosen file goes into page state (the input keeps it; each counts in `window.__filesTaken`), the popup closes, the button's `div.jv-select` is hidden and `ul.jv-file-list` lists the name with a "Remove" link. The form's `formdata` event posts the stored file as `resume`. |
| `div-combobox-orphan` | Brand Marketing Manager (BWA-RP-126) | Rippling-style, rendered **without a `<form>`**; a page-script "Submit application" (`type=button`) posts the named controls plus widget state as multipart. Its popover div comboboxes (`field-55` Gender, labelled by `aria-labelledby`; `field-63`, a custom question named only by its paragraph) keep their placeholder as `aria-label` ("Select..."/"Select"). Focus or a click opens them; a click on an open one, Escape and blur do nothing. Only a choice or a document `mousedown` outside the control and popover closes them. The list is `ul[role=listbox]` in a `div[role=dialog][data-testid=popper]` (with a status line) inside the question block. Options carry `aria-setsize`/`aria-selected` and wrap `div > div[data-testid=menuListLabel] > p`; a chosen label replaces the placeholder `<p>` as a bare text node. The role-less location lookup `input#field-42` (`aria-label="textbox"`, `aria-labelledby`) never gets `aria-expanded` and shows its popper only with results. Its first query answers after about 1.8 s. |
| `multiselect-react` | Content Marketing Manager (BWA-GH-124) | A React-select-style multi-select "Which marketing channels have you managed?" (`question_8001`, required) whose listbox has `aria-multiselectable=true` and whose choices appear as chips (`div.select__multi-value` with a `role=button` "Remove …"), beside a single-choice React select (`question_8002`). The runtime must leave the multi-select to the user. |
| `autofill-upload` | Revenue Operations Analyst (BWA-AS-130) | Ashby-style resume parsing. Fields in DOM order: first name, last name, email, phone, LinkedIn (optional) and the standard native `#f-resume` **last**, so filling in DOM order types the names before the upload. On each `change` of `#f-resume` with a file, page script inserts (once) `div#resume-parse-status[role=status][aria-live=polite][aria-busy=true]` right after the input ("Parsing your resume…" after a `span.spinner`), and `autofill_ms` (600) later overwrites `#f-first_name` = "A.", `#f-last_name` = "Quill (resume)" and `#f-email` = "a.quill@resume-parser.example.test" (value assignment plus bubbling `input` and `change`, all untrusted), then sets the status to "We filled in some fields from your resume." with `aria-busy=false` (it stays visible). The input keeps its file; another change repeats everything. `fixture_identity`. |
| `custom-uploader` | Customer Marketing Manager (BWA-GH-131) | Greenhouse-style uploaders that page script mounts (the static HTML holds one `div[data-mount-html]` placeholder each). Resume: `div#resume-field.field.uploader[role=group][aria-labelledby=resume-label][aria-required=true]` with "Resume/CV *", a drop zone `#resume-dropzone` ("Drop or select a file", `button#resume-button` "Upload resume", which opens the file chooser), a hint, `input#resume-input[type=file][name=resume]` with `display:none` and **no label, no `aria-label`, no `required`**, `div#resume-chip.file-chip[hidden]` and `div#resume-notice.upload-notice[role=status][aria-live=polite]`. On `change` the file moves into page state and the input is **cleared at once**; the chip shows `span.file-chip__name`, `span.file-chip__size` "(N bytes)" and `button.file-chip__remove[aria-label="Remove file"]`; the notice says "Uploading…" (`aria-busy=true`), then after `upload_ms` (800) "<name> uploaded" (`aria-busy=false`). The remove button clears the page state, the chip and the notice. A `drop` on the zone goes through the input. The form's `formdata` event posts the stored file as `resume`. Cover letter (optional): `input#cover-letter-input[name=cover_letter].visually-hidden` inside `label.upload-label[data-testid=cover_letter]` ("Attach cover letter", no `for`); it keeps its file and `#cover-letter-chip` shows its name. Both are validated like `file` (kinds `custom_file` and `label_file`). A 422 re-render shows them empty with an inline error; nothing is retained. `fixture_identity`. |
| `linkedin-autofill` | Marketing Operations Specialist (BWA-LV-132) | Lever-style: the native `#f-resume` first, then `name` ("Full name", `autocomplete=name`), `email`, `phone`, `location` ("Current location", optional) and `urls[LinkedIn]` ("LinkedIn URL", optional, id `f-urls[LinkedIn]`). Before the first field block, `div#awli` holds `button#linkedin-apply.awli-button[aria-busy=true]` "Loading…" (after `loading_ms`, 1500, "Apply with LinkedIn" without `aria-busy`) under `div#linkedin-overlay.awli-overlay[title="Apply with LinkedIn"]`, a transparent overlay covering the button exactly, so a pointer click lands on the overlay. The button's own click sets `name` = "LinkedIn Member" and `email` = "member@linkedin.example.test". An autofill prompt `div#autofill-prompt[role=dialog][aria-modal=true]` ("Autofill your application?", buttons `#autofill-prompt-accept` "Autofill with LinkedIn" and `#autofill-prompt-dismiss` "No thanks") is appended to `body`, outside `main`, after load or after the resume changes (see below); while it is shown, `main` is `inert` and `aria-hidden`. "No thanks" removes it and restores `main`; "Autofill with LinkedIn" does too and sets the LinkedIn values. A closed prompt never returns. `fixture_identity`. |
| `react-controlled` | Retention Marketing Manager (BWA-RC-133) | React-like controlled inputs: first name, last name, email and phone (standard markup) inside `div#react-root`, the native resume outside it. Only trusted `input` events (typing: `page.fill`, `press_sequentially`) update the page state (`__mock.state`, which starts from the rendered values); every 150 ms and on `focusout` any other value, such as a value assignment followed by a synthetic `input` event, is reset to the state. The first typed change re-renders the fields once: after `rerender_ms` (0) the field blocks give way to `p#react-saving[aria-busy=true]` "Saving draft…", and `unmount_ms` (300) later **new** elements with the same ids, names, labels and attributes are mounted with the state's values (element handles taken before are disconnected). With `?lose_first=1` the first typed value never reaches the state (typing before hydration), so the re-render drops it. The form's `formdata` event posts the state. `fixture_identity`. |
| `changed-after-prepare` | Decision Scientist (BWA-DS-128) | The core questions (contact, résumé, work authorization, sponsorship) on the **first** load of `/jobs/changed-after-prepare/apply`. From the second load on the form also asks a new required radio question, "Are you willing to travel to client sites up to 25% of the time?" (`travel_willingness`: `travel_yes`/`travel_no`). The server counts the GETs of this application page (in `state.json`; `/__test__/reset` clears the count) and renders and validates whichever form it served last, so answers prepared on the first load no longer match the form when it is opened again, and a POST without the new answer is a 422 rejection. The catalog names the question as `added_on_reload`. Used to prove that an approved application whose form changed is not submitted. |
| `modal-wizard` | Growth Marketing Lead (BWA-LI-130) | LinkedIn-style Easy Apply. The job view's Easy Apply control (a link by default, a type-less button with `?trigger=button`) opens a four-step modal `role="dialog"` built by page script: Contact info (pre-filled), Resume (saved resume cards and an upload), Additional Questions, Review. Page-behind decoys stay in the document: a search form, an "Easy Apply" search-filter toggle and two form-less fillable controls. Only "Submit application" contacts the server, with one `POST /jobs/modal-wizard/easy-apply`. See [Replicated application flows](#replicated-application-flows). |
| `iframe-embed` | Partnerships Manager (BWA-GH-141) | An employer careers page with "Role overview" and "Application" tabs. Page script injects the Greenhouse-style `iframe#grnhse_iframe` into the hidden Application panel 300 ms after load; the iframe shows the `standard` form from `/embed/job_app`. "Apply Now" and the Application tab only switch panels. |
| `stepper-ambiguous` | Head of Paid Media (BWA-JZ-132) | JazzHR-style: the form has no `<button>` at all. "Attach resume", "Paste resume" and "Submit Application" are `href="#"` anchors; "Submit Application" validates on the client, then submits the form by script. Cookie-consent buttons and a Share anchor sit outside the form. `?sections=2` splits the form into two client-side sections with Next, Save and Back anchors. |
| `apply-in-alert-form` | Marketing Project Manager (BWA-DF-133) | Dayforce-style: the whole posting is one ASP.NET-style form whose Apply button posts to `/start`, beside a "Get job alerts" email field and a Subscribe button (`formaction` `/alerts`). Apply leads to "How would you like to apply?", whose "Apply without an Account" routes on the client (2.5 s, `history.pushState`) to the `CORE` form. `?nav=spa` has no forms and also routes on the client from the posting. A typed alert email is recorded as a job-alert subscription, never as an application. |
| `workday-wizard` | Growth Marketing Manager (JR-BWA-201) | A Workday tenant, from `scripts/mock_workday.py` (see **Workday wizard** below). The posting's Apply opens a "Start Your Application" popup; "Apply Manually" leads to the Create Account / Sign In step until the user signs in; then one document runs My Information, My Experience, Application Questions, Voluntary Disclosures, Self Identify and Review. Only "Submit" records an application. |

### Workday wizard

`workday-wizard` reproduces the Workday candidate site as observed read-only on four live
tenants on 2026-09-24 up to the account step, and Workday's conventions behind it (not
observable without an account). Every route lives under `/jobs/workday-wizard`.

| Path | Result |
| --- | --- |
| `GET /jobs/workday-wizard` | The posting: `h2[data-automation-id=jobPostingHeader]`, a `role=alert` "… page is loaded" announcement, the job details list ("job requisition id JR-BWA-201"), JSON-LD `JobPosting` and Apply = `a[role=button][data-automation-id=adventureButton]` to `…/apply`. Header buttons "English", "Sign In" and "Search for Jobs". |
| posting Apply, clicked | A `wd-popup-glass` with a `role=dialog` "Start Your Application" popup in the page (the URL stays, `#root` turns `aria-hidden`): a Close button and three `a[role=button]` routes, `autofillWithResume`, `applyManually`, `useMyLastApplication`. |
| `GET …/apply` | The same three routes on a page of their own, `div[data-automation-id=applyAdventurePage]` with h2 "Start Your Application" (the live site's response to that URL). |
| `GET …/apply/<route>` | Records a route visit. Signed out: the account step. Signed in: the wizard (`applyManually`) or a placeholder (other routes). |
| account step | The progress list `ol[data-automation-id=progressBar]` (each `li` labelled "current step 1 of 7", "step 2 of 7" …), `h2[data-automation-id=jobTitleHeading]`, `h3#authViewTitle` "Create Account", the password rules, a `form[data-automation-id=signInFormo]` (Email Address, Password, Verify New Password, the privacy-notice checkbox `createAccountCheckbox`, a `click_filter` `div[role=button]` over a hidden `aria-hidden` submit), "Already have an account? Sign In" (`signInLink`, to `?view=signin`), "Forgot your password?" and the zero-height honeypot `input[data-automation-id=beecatcher][name=website]` labelled "Enter website. This input is for robots only, do not enter if you're human." The sign-in view has Email Address and Password. |
| `POST …/account` | `mode=create` (email, password, `verifyPassword`, `createAccountCheckbox`) or `mode=signin`; the fixture's credentials (`/__test__/jobs` `signin`) or an account created here. Errors re-render the step (422). Success sets the `bwa_wd_session` cookie (HttpOnly, one day, so a persistent browser profile keeps it) and redirects to `…/apply/applyManually`. |
| wizard | One document (the URL never changes; JSON-LD kept) whose script renders a page per step: My Information (a search-on-Enter multi-select prompt "How Did You Hear About Us?", a Yes/No radio, `button[aria-haspopup=listbox]` dropdowns Country, State and Phone Device Type, text fields, a single-select Country Phone Code prompt prefilled with "United States of America (+1)", Phone Number and Phone Extension), My Experience (a Work Experience "Add" button, the résumé drop zone and LinkedIn), Application Questions (three dropdowns), Voluntary Disclosures (three dropdowns and a consent checkbox), Self Identify (Name, a Month / Day / Year spinbutton date, a one-box-only disability checkbox group) and Review (answers by section, "Submit"). Footer buttons "Back" and "Save and Continue" (`pageFooterNextButton`; `pageFooterSubmitButton` "Submit" on Review). A `role=alert` announces "<page> page is loaded". |
| `POST …/wizard/save` | JSON `{page, values}` from the page script. Server checks: required answers, listed option values, a phone number without its country code ("+1 …" is refused) of 10 digits, a real `MM/DD/YYYY` date, one disability box. 422 `{errors}` (recorded as a rejection, never counted): the page shows an alert banner "Errors Found (n)" and, per field, an "Error: …" message the control is described by with `aria-invalid=true`. 200: the page is stored in the account's draft and the next page shows. |
| `POST …/wizard/upload` | Multipart `file` (.pdf, .doc, .docx), sent on attach; the uploader then empties its input and shows the file's name and "Delete". |
| `POST …/wizard/submit` | The only request that records (and counts) an application, and only when every page is saved and valid; otherwise 422. `GET …/wizard/submitted?ref=` shows the confirmation. |

Widgets keep their answers in page state (`window.__wdState`) and mirror the live site.
Dropdowns: `button[type=button][aria-haspopup=listbox][aria-label="<label> <shown> Required"]`
(no `aria-expanded`/`aria-required` while closed) with a zero-size text input beside it;
opened, `aria-expanded=true` and `aria-controls` name a body-portal
`ul[role=listbox][tabindex=-1]` whose id is a new short token each time, focus moves into
it, the first option is `li#select-one[role=option][aria-disabled=true][data-value=""]`
"Select One", the others `li[role=option][id=<value>][data-value=<value>]` (opaque values
such as `wd_country_1`); a click, Escape or an outside press closes it. Prompts ("How Did
You Hear About Us?" multi-select, "Country Phone Code" single-select, prefilled "United
States of America (+1)"): the search box has no role, `aria-haspopup`, `aria-controls` or
`aria-expanded`, has `enterkeyhint=search` and is described by a hidden "N items selected[,
labels]" ("Expanded" while open; the single-select one says "Minimized" once closed); a
click opens a body-portal `div[role=listbox][aria-label="Options Expanded"]` of
`div[role=option][aria-label="<label> not checked"|"<label> checked"]` rows (categories
with a chevron, or a flat list with a radio per row for the single-select prompt); typed
words are searched on Enter (leaves whose words all begin with the typed words, after
300 ms); a click on a multi-select row toggles it and keeps the list open, on a
single-select row replaces the item and closes the list; Escape does nothing, an outside
press closes it. Chosen items are `ul[role=listbox][aria-label="items selected"] >
li[role=presentation] > div[role=option][aria-label="<label>, press delete to clear
value."]` with an aria-hidden delete charm (a click removes the item) and
`p[data-automation-label]`. Date segments accept digits only and move to the next segment
after two (month, day) digits; "/" moves on too.

### Static pages

| Path | Result |
| --- | --- |
| `GET /closed` | HTTP 200 page for a job that is gone, worded like some ATS vendors: "We're sorry, that job does not exist or is not currently active." No form, no apply link. |
| `GET /closed/not-found` | HTTP 200 page for a removed job, worded like Ashby: "Job not found" / "The job you requested was not found." |
| `GET /captcha/widget.html` | The tiny document the `captcha-widget` badge iframe shows. |
| `GET /forms/unlabeled-custom-questions` | A Lever-style application form (GET only; the POST is not served): two labelled contact fields plus three custom questions with **no** `<label>`, each a `<div class="application-label">Question ✱</div>` before an input named `cards[<uuid>][field1..3]` (the third is a `<select>`), and no `required` attribute on the custom inputs. The visible question must become the field's wording, the marker its required flag. Also three yes/no radio groups (`CA_9001`–`CA_9003`) whose radios are labelled only "YES"/"NO", with the question in a preceding block starting with `*` and no `required` attribute: one inside its own question block, two in a flat layout where the question `<p>` is a preceding sibling. |
| `GET /forms/choices-without-values` | Two radio groups whose members share an opaque `<uuid>_<uuid>` name and have `value=""` with distinct labels (years of experience; marketing automation platform), the question in a block before each group. The first group's inputs have no ids, the second's do (`opt-hubspot` …). Inspection must synthesize stable option values and keep every member selectable. GET only. |
| `GET /forms/breezy-like` | A Breezy HR application as it renders live (BWA-BZ-172), with no `<label>` anywhere. Each question is an `<h3>` (with a `*` span) before its control. Contact inputs carry placeholders equal to their headings ("Full Name"), and an SMS consent checkbox after the phone input states its own text. A "Desired Salary" block holds a currency select, the amount input (placeholder "Desired Salary") and an **unnamed** pay-period select (Hourly … Yearly). Custom questions are named `section_1787064635874_question_0..4`: two text inputs, a textarea, a Yes/No radio group in `ul.options` with wrapping labels, and a checkbox group whose options have **neither labels nor values**, only a `<span>` each (Google Ads, Meta, LinkedIn, TikTok). There is also an EEO race radio group headed "Race or Ethnicity" whose first option is "White (not Hispanic or Latino)". Labels must be the headings, options their own texts. GET only. |
| `GET /forms/ashby-like` | An Ashby application as it renders live (BWA-AS-173), with **no `<form>`** (page script posts it). Each question is a field entry titled by `<label for="<field path>">`. Name and Email are system fields. "What is your expected salary?" has a `Type here...` placeholder. A react-datepicker date input has **no id or name**, so its title labels nothing, and a `Pick date...` placeholder. On focus it opens, inside its own entry, an absolutely placed calendar above itself: a `role=listbox` month of `role=option` days and two unnamed month buttons. A click on a day writes MM/DD/YYYY; a typed M/D/YYYY is kept on blur, Enter, Tab or Escape, anything else is cleared. It also has: a referral radio group sharing one name; a radio group ("How would you like to work?") and a checkbox group ("Will you now or in the future require sponsorship?") whose options are **named after their own text** (`name="Yes"`/`name="No"`, like Compyl's), which page script keeps exclusive; and a yes/no question drawn as two `type="submit"` buttons with `aria-pressed` over a `display:none` checkbox that mirrors "yes" (its title's `::after` draws the required "*"). A Location lookup (`role=combobox`, `Start typing...`, its title `for="_systemfield_location"` labelling nothing) mounts its suggestion portal on focus: a wrapper holding a `role=listbox` and a "Powered by Google" link, appended to `<body>` (`?portal=inline`: right after its field entry, before the later questions). The input's `aria-controls` names the wrapper (`?owns=listbox`: the listbox). Suggestions come from `/__fixture__/cities?style=long` for 2+ typed characters; a click writes the suggestion into the input and unmounts the portal, as do Escape and blur. `?ui=floating` is Ashby as it renders live (Sanity): a Floating UI portal (`data-floating-ui-portal`, `id=":r2:"`) holding the listbox that `aria-controls` names (`:r0:`) and a focus-guard button. While suggestions show, everything else is marked `aria-hidden="true"` with `data-aria-hidden="true"`, and a chosen suggestion leaves the input empty until the save returns (`?save_ms`, 400 ms). A "LinkedIn Profile" text question follows the yes/no. GET only. |
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
the Workable dropzone show an error alert instead of the file (a string is the alert's
own text, `{file}` standing for the file's name), and
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

### Replicated application flows

Four scenarios reproduce the structure of real vendor pages, with fictional text. They
follow the vendors' markup, not the conventions under "Shared behavior": some controls
have only an `aria-label`, and some actions are anchors or type-less buttons. Their
timers are fixed: 300 ms and 400 ms (Easy Apply), 300 ms (iframe injection) and 2.5 s
(Dayforce routes). Page script keeps its configuration in an `application/json` script
element whose `<`, `>` and `&` are escaped, so no route content reads as markup in the raw HTML.

#### `modal-wizard`: LinkedIn-style Easy Apply

| Method and path | Result |
| --- | --- |
| `GET /jobs/modal-wizard` | The job view: `_job_heading` identity, canonical link and JSON-LD as on every posting, the apply control, and the page-behind decoys. |
| `GET /jobs/modal-wizard/apply` | The SDUI apply URL: the same job view, with the dialog opened by script 300 ms after load. |
| `POST /jobs/modal-wizard/easy-apply?resumes=<variant>` | The dialog's only request (multipart, sent by `fetch`). |
| `POST /jobs/modal-wizard/apply` | 405. Nothing is recorded. |

Variants are query parameters. The link's `href` keeps them.

| Parameter | Values |
| --- | --- |
| `trigger` | `link` (default): `<a aria-label="Easy Apply to this job" href="/jobs/modal-wizard/apply?openSDUIApplyFlow=true&…">` wrapping an `svg[aria-hidden]` and the text "Easy Apply". `button`: `button#jobs-apply-button-id.jobs-apply-button[data-live-test-job-apply-button]` with `aria-label="Easy Apply to Growth Marketing Lead at Brambleway Analytics"`, no `type` and outside any form. A click opens the dialog 400 ms later (a simulated request, no navigation). |
| `resumes` | Saved resume cards on the Resume step; the first is preselected. `match` (default): "Avery_Quill_Resume_2025.pdf", "resume_avery_quill.pdf". `one`: "Avery_Quill_Resume_2025.pdf". `nomatch`: "Avery_Quill_Resume_2025.pdf", "AQ_CV_marketing.docx". `none`: no card, so a file must be uploaded. Unknown values mean `match`. |
| `shadow` | `1`: `div#interop-outlet[data-testid=interop-shadowdom]` is appended to `body` at load, and the whole `#artdeco-modal-outlet` subtree, with a copy of the dialog styles, is rendered inside its **open** shadow root. While the dialog is open the host has `style="position:fixed;inset:0;z-index:1000"`. `document.querySelector` finds none of the dialog; `label[for]` ids resolve inside the shadow root; Playwright CSS and label locators pierce it. |
| `import` | `1`: the Contact info step opens with the paragraph "Import from LinkedIn or fill out this form." and a `type=button` "Import from LinkedIn" (counted in `window.__easyApply.imported`). Its footer has two `type=submit` buttons in the step's form. "Skip" moves on without validation and is counted in `skipped`. "Continue" (`aria-label="Continue to next step"`) replaces Next. The whole dialog's text reads like an autofill offer. A runtime must not decline it with Skip or Dismiss; only `advance()` moves the step on. |

Page-behind decoys, which a runtime must ignore once the dialog is open:

- `form[role=search][method=get][action="/jobs/modal-wizard"]` with `input[type=search][name=keywords][aria-label="Search jobs"]` (also `id="jobs-search-keywords"` with a visually hidden `<label for>`) and a submit button "Search".
- `button.filter-pill[type=button][aria-pressed]` "Easy Apply", plus a "Remote" pill. These are search-filter toggles that flip `aria-pressed`.
- `textarea[aria-label="Write a message to the hiring team"]` and `input[type=text][aria-label="Add a note about this job"]`, outside any form.

The dialog, appended to `body` when it opens and removed when it closes:
`div#artdeco-modal-outlet` > `div.artdeco-modal-overlay.artdeco-modal-overlay--is-top-layer[data-test-modal-container][aria-hidden=false]` >
`div[data-test-modal][role=dialog][tabindex=-1][aria-labelledby=jobs-apply-header].artdeco-modal.jobs-easy-apply-modal`. It has **no** `aria-modal`.
Inside it are `button[aria-label=Dismiss][data-test-modal-close-btn]` (no `type`), `h2#jobs-apply-header` "Apply to Brambleway Analytics" and
`div.artdeco-modal__content` > `div[role=region][aria-label="Your job application progress is at N percent."]`. The region holds
`progress.artdeco-completeness-meter-linear__progress-element` and `span[role=note]` "N%" (N is 0, 33, 67 or 100), then one `<form>` with no attributes,
whose `submit` is prevented so Enter never navigates. The form holds `div.ph5` (the step, headed by `h3.t-16.t-bold`) and `footer[role=presentation]`.
The footer shows "Submitting this application won’t change your LinkedIn profile." and `type="button"` buttons with `span.artdeco-button__text`:

- Next: `aria-label="Continue to next step"`, `data-easy-apply-next-button`.
- Back: `aria-label="Back to previous step"`.
- Review: `aria-label="Review your application"`.
- Submit application: `aria-label="Submit application"`, `data-live-test-easy-apply-submit-button`.

Each question sits in `div.fb-dash-form-element[data-test-form-element]` with a `<label for>` (the radio group has a `<legend>`). Every select starts with
the option "Select an option", and every option's value is its text. In the ids below, `S` is
`text-entity-list-form-component-formElement-urn-li-jobs-applyformcommon-easyApplyFormElement-4007130` and `T` is the same with
`single-line-text-form-component`.

| Step (progress) | Question | Control and id | Initial value |
| --- | --- | --- | --- |
| Contact info (0) | Email address | `select#S-9001-multipleChoice` ("avery.quill@example.test", "a.quill@example.test") | `avery.quill@example.test` |
| | Phone country code | `select#S-9002-phoneNumber-country` ("United States (+1)", "Canada (+1)", "United Kingdom (+44)", "Afghanistan (+93)") | `United States (+1)` |
| | Mobile phone number | `input#T-9002-phoneNumber-nationalNumber[type=text][inputmode=text]` | `3035550142` |
| | City | `input#T-9003-text[type=text]` | `Boulder` |
| Resume (33) | resume cards, upload | see below | first card selected |
| Additional Questions (67) | How many years of work experience do you have with SQL? | `input#T-9004-numeric[type=text]`: a whole number 0–99 | empty |
| | Are you legally authorized to work in the United States? | `fieldset#radio-button-form-component-formElement-urn-li-jobs-applyformcommon-easyApplyFormElement-4007130-9005-multipleChoice` whose `<legend>` also contains a visually hidden "Required". The radios Yes/No have the `name` `urn:li:fsd_formElement:urn:li:jobs_applyformcommon_easyApplyFormElement:(4007130,9005,multipleChoice)` and the ids `<name>-0`, `<name>-1` | none |
| | Will you now or in the future require sponsorship for employment visa status? | `select#S-9006-multipleChoice` (Yes, No) | "Select an option" |
| Review your application (100) | Follow Brambleway Analytics to stay up to date with their page. | `input#follow-company-checkbox.visually-hidden-checkbox[type=checkbox]` | checked |

The contact profile card (name, headline, location) is display-only. Next and Review validate the current step. Like
LinkedIn, every question block ends with an empty `div#<control id>-error` that stays in place. A bad field gets
`aria-invalid="true"`, `aria-describedby="<control id>-error"`, the class `fb-dash-form-element__error-field` and, inside
that container, `div.artdeco-inline-feedback.artdeco-inline-feedback--error[role=alert]` > `span.artdeco-inline-feedback__message` with
"Please enter a valid answer" (for the SQL question when it is not a whole number: "Enter a whole number between 0 and 99").
Typing into the field empties the container again.
The Resume step needs a selected card or an uploaded file ("Please select or upload a resume"). The Review step lists
the answers in one `section` per step (`dl`), each with a `type="button"` "Edit" button (`aria-label="Edit Contact info"`,
`"Edit Resume"`, `"Edit Additional Questions"`) that returns to that step.

Resume step: `span.jobs-document-upload__title--is-required` "Be sure to include an updated resume". Each card is a
`div.ui-attachment.jobs-document-upload-redesign-card__container.ui-attachment--pdf` (or `--docx`) with `tabindex="0"`.
It holds `h3.jobs-document-upload-redesign-card__file-name`, a details line such as "290 KB · Last used on 8/12/2026",
a no-op download button (`aria-label="Download resume <name>"`) and `input#jobsDocumentCardToggle-N.visually-hidden-radio[type=radio]`
with **no** `name`. The radio's `label[for]` wraps `span.a11y-text`. The selected card has the class
`jobs-document-upload-redesign-card__container--selected`, `aria-label="Selected"`, a checked radio and the label text "Deselect resume <name>".
The others have `aria-label="Select this resume"`, an unchecked radio and "Select resume <name>". Card ids are stable per card (uploads
get the next number). Selecting a card deselects the others. Clicking the selected card's toggle deselects it, which leaves no resume.
The radios are clipped to 1 px (`clip: rect(1px,1px,1px,1px)`), so they keep a box but sit under their visible 24 px label.
A pointer `check()` aimed at the radio is intercepted by that label; `check()` on `label[for=jobsDocumentCardToggle-N]`
(or a forced check) selects the card. With two or more cards, a no-op
`button.jobs-document-upload__show-more-less-button` shows "Show N more resumes". The upload is
`label.jobs-document-upload__upload-button[for=jobs-document-upload-file-input-upload-resume]` holding a
`span[role=button]` "Upload resume", with `input#jobs-document-upload-file-input-upload-resume.hidden[type=file][name=file]` (`display:none`,
`accept` DOC, DOCX and PDF) and the hint "DOC, DOCX, PDF (2 MB)". A chosen .pdf, .doc or .docx file of at most 2 MB becomes a new selected card at the top
("1 KB · Uploaded just now"). The file is kept in page state until the submit.

"Submit application" posts `FormData` with the answers under their keys (`email`, `phone_country`, `phone`, `city`,
`sql_years`, `work_authorization`, `sponsorship`), `follow_company=yes` when the checkbox is checked, and either the uploaded file as
`resume` or the selected card's file name as `resume_choice`. The server validates like the other scenarios. A
`resume_choice` must name a card of the request's `?resumes=` variant, and an upload must be a DOC, DOCX or PDF file of at most 2 MB. It then
answers JSON:

- **Accepted**: 200 `{"accepted": true, "submission_id", "confirmation_reference", "job_id", "job_code"}`. The dialog content becomes
  `h3` "Application submitted" and "Your application for **Growth Marketing Lead** (Job ID BWA-LI-130) was sent to Brambleway Analytics."
  (inside `role="status"`), with a "Done" button that closes the dialog. A saved card is recorded as
  `files.resume = {"source": "saved_resume", "filename", "details"}`; an upload as ordinary upload metadata. `resume_choice` never
  appears in `extra_fields`.
- **Rejected**: 422 `{"accepted": false, "errors": {…}}`, shown as `#jobs-easy-apply-submit-error[role=alert]` in the Review step. The
  rejection is recorded, not counted.

`window.__easyApply` is test instrumentation, like `window.__widgetState`:
`{open, opens, step, submitted, answers, writes}` (plus `result` after acceptance). `step` is 1–4. `answers` holds each key's
current value: selects keep their raw value (the placeholder is "Select an option"), the radio is `null` until chosen, `resume` is the
selected card's file name or `null`, `resume_uploaded` is a boolean and `follow` is a boolean. `writes` counts the `input` and
`change` events per key (`email`, `phone_country`, `phone`, `city`, `sql_years`, `work_authorization`, `sponsorship`, `resume`, `follow`),
so a zero proves that a pre-filled value was never touched. Selecting a card counts under `resume`, and so does the deselection, which
fires `input` and `change` from script. Each opening starts from fresh answers and zero counts. Dismiss or Done closes the dialog and
sets `open` to false.

#### `iframe-embed`: a careers page embedding a Greenhouse-style iframe

- `GET /jobs/iframe-embed`: the careers page. It has the job identity, canonical link and JSON-LD, and a hidden GET search form with one field. `div#main-content[role=tablist]` holds `button#tab-overview[role=tab][aria-controls=job-detail-panel]` "Role overview" (`aria-selected=true`) and `button#tab-application[role=tab][aria-controls=job-application-panel]` "Application". `div#job-detail-panel[role=tabpanel]` holds the description and `button.apply-btn[aria-label="Switch to application form"]` "Apply Now" (no `type`, no form). `div#job-application-panel[role=tabpanel]` has `display:none` and holds `div#grnhse_app`. "Apply Now" or the Application tab switches panels without navigating. About 300 ms after load, page script (standing in for `boards.greenhouse.io/embed/job_board/js?for=brambleway`) injects `<iframe id="grnhse_iframe" title="Greenhouse Job Board" width="100%" height="1200" frameborder="0" scrolling="no" src="/embed/job_app?for=brambleway&token=4007131">` into `#grnhse_app`. The form in it is taller than 1200 px; its document scrolls only by script.
- `?panel=visible` selects the Application tab from the start. `?embedded_only=1` appends `&embedded_only=1` to the iframe `src`.
- `GET /embed/job_app?for=brambleway&token=4007131` serves exactly the page of `GET /jobs/iframe-embed/apply` (the `standard` questions, posting to `/jobs/iframe-embed/apply`, which answers like any single-page form). Any other `for` or `token` returns 404. With `embedded_only=1` the form is served only when the request header `Sec-Fetch-Dest` is `iframe`. For `document`, any other value or no header (a non-browser client), it returns 403 with the page "This application form can only be shown on the Brambleway Analytics careers page." Chromium sends `iframe` for the injected frame.

#### `stepper-ambiguous`: JazzHR-style anchor actions

- `GET /jobs/stepper-ambiguous` is an ordinary posting. `GET /jobs/stepper-ambiguous/apply` has a cookie-consent bar outside the form (`#resumator-cookie-consent`, "This website uses cookies…", with type-less `<button>`s "Dismiss", "ALLOW" and "REJECT ALL"; any of them hides the bar), `a.share[role=button][href="#"]` "Share", and a hidden `button#resumator-mobile-apply-button[type=button]` "Apply".
- `form#form_submit_new_resume[method=post][action="/jobs/stepper-ambiguous/apply"][enctype=multipart/form-data]` contains six hidden inputs (`resumator-job-id`, `resumator-board-code`, `resumator-source`, `resumator-referrer`, `resumator-applicant-token`, `resumator-form-version`), which are recorded in `extra_fields`. `div.job-form-fields` holds `div.form-group`s, each a `<label for>` and a control, with ids `resumator-<x>-value` and machine names: First Name (`first_name`), Last Name (`last_name`), Email (`email`), Phone (`phone`), the optional "Desired salary" (`desired_salary`), and the select "How did you hear about this job?" (`heard_about`: `hear_linkedin`, `hear_indeed`, `hear_site` and `hear_other` for LinkedIn, Indeed, Company website and Other). Required controls carry `aria-required="true"`; there is no `required` attribute. `div#resumator-resume` holds `a#resumator-choose-upload` "Attach resume", which clicks the hidden `input#resumator-resume-file[type=file][name=resume][accept=".pdf,.doc,.docx"][aria-label=Resume]` (`display:none`, also labelled "Resume"), and `a#resumator-choose-paste` "Paste resume", which reveals the optional `textarea#resumator-resume-value[name=resume_text]`. The resume is required, but pasted text stands in for the file. `div#resumator-submit.form-group` holds `a#resumator-submit-resume.btn.btn-primary[href="#"]` "Submit Application". There is **no `<button>` inside the form**.
- "Submit Application" validates on the client. Each bad control gets `aria-invalid`, the group class `has-error` and `span#<id>-error.help-block[role=alert]` ("This field is required.", "Enter a valid email address." or "Attach or paste your resume."). If the form is valid, the anchor calls `form.requestSubmit()`. The POST is handled like any single-page form: a 422 re-render of this page (error summary, inline errors, preserved values, a retained resume), or a 303 to the confirmation.
- `?sections=2` (the action keeps `?sections=2`): section 1 (`#resumator-section-1`: the contact fields) ends with the anchors `a.btn` "Next" and "Save". Save stores the section's values in `sessionStorage["resumator-saved-application"]` and shows "Saved" in `#resumator-save-status[role=status]`; it never posts. Next validates section 1, hides it and shows `#resumator-section-2` (the resume, "Desired salary" and "How did you hear about this job?"), which has a "Back" anchor and the "Submit Application" anchor. Nothing is posted before Submit Application. A 422 re-render opens the section that holds the first error.

#### `apply-in-alert-form`: a Dayforce-style posting inside a job-alert form

| Method and path | Result |
| --- | --- |
| `GET /jobs/apply-in-alert-form` | The legacy portal. The whole content is `form#aspnetForm[method=post][action="/jobs/apply-in-alert-form/start"]`, with `__VIEWSTATE` and `__EVENTVALIDATION` hidden inputs, the identity, `button[type=submit][name=apply][value=1][aria-label="Apply for Marketing Project Manager"]` "Apply" (the form's default button), the description, and a "Get job alerts" block. The block has `label[for=alert-email]` "Email address for job alerts", `input#alert-email[type=email][name=alert_email]` and `button[type=submit][formaction="/jobs/apply-in-alert-form/alerts"][name=subscribe][value=1]` "Subscribe". |
| `GET /jobs/apply-in-alert-form?nav=spa` | The current portal. There are no forms. `button[type=button].ant-btn.ant-btn-primary[test-id=apply-button]` (same `aria-label`) "Apply" and a form-less "Share" button. A click on Apply waits 2.5 s, then calls `history.pushState` to `/jobs/apply-in-alert-form/apply?flowSelection=true` and renders that route in `main`, without a document load. |
| `POST /jobs/apply-in-alert-form/start` | Records a job-alert subscription if `alert_email` is not empty, then always returns 303 to `/jobs/apply-in-alert-form/apply?flowSelection=true`. |
| `POST /jobs/apply-in-alert-form/alerts` | With a non-empty `alert_email`: records a subscription and re-renders the posting with "You are subscribed to job alerts at …". With an empty one: 422 and "Enter an email address for job alerts." Nothing is recorded. |
| `GET /jobs/apply-in-alert-form/apply?flowSelection=true` | No forms: `h1` "How would you like to apply?" and the `type="button"` buttons "Apply without an Account" and "Sign In". "Apply without an Account" waits 2.5 s, then calls `history.pushState` to `/jobs/apply-in-alert-form/apply/manual` and renders the application form in place. It is exactly the `_render_single` page body. "Sign In" navigates to `/login?next=/jobs/apply-in-alert-form/apply%3FflowSelection%3Dtrue`. |
| `GET /jobs/apply-in-alert-form/apply/manual`, `GET /jobs/apply-in-alert-form/apply` | The application form page (`CORE` questions). |
| `POST /jobs/apply-in-alert-form/apply` | The standard single-page POST. |

Client-side routes answer the browser's Back button with a reload of the URL. Tests assert that nothing was
subscribed (`alert_count` stays 0) when a runtime applies without typing into the alert email.

### Solvable CAPTCHA widgets (round 14)

`captcha-gate`, `captcha-form` and `captcha-steps` carry the widgets a runtime solves
through 2Captcha (`tests/browser/test_captcha_round14.py`). The site keys are fictional
and nothing reaches Google, hCaptcha or Cloudflare: the vendors' scripts and badge frames
are local stubs under `GET /fixture/<path>`, in the vendors' URL shapes so a detector reads
the site key from them (`/fixture/recaptcha/api.js?render=<key>`,
`/fixture/recaptcha/api2/anchor?k=<key>&size=normal|invisible`,
`/fixture/hcaptcha.com/checkbox.html#frame=checkbox&sitekey=<key>`,
`/fixture/challenges.cloudflare.com/turnstile/<key>/normal`). The stub `api.js` defines a
`grecaptcha` whose `execute` resolves to a token no server accepts. The only accepted
token is `fixture-solved:<site key>` (`captcha_expected_token(kind)`), which is what the
tests' fake 2Captcha transport returns for a task's `websiteKey`.

`?kind=` picks the widget (`SOLVABLE_CAPTCHAS`; default `recaptcha-v2`):

| `kind` | Site key | On the page |
| --- | --- | --- |
| `recaptcha-v2` | `6LfixtureV2CheckboxKeyAAAAAAAAAAAAAAAAAAAAA` | `.g-recaptcha[data-sitekey]`, a hidden `g-recaptcha-response` textarea, the badge iframe. |
| `recaptcha-v2-invisible` | `6LfixtureV2InvisibleKeyAAAAAAAAAAAAAAAAAAAA` | The same with `data-size="invisible"`. |
| `recaptcha-v3` | `6LfixtureV3ScoreKeyAAAAAAAAAAAAAAAAAAAAAAAAA` | No widget: `api.js?render=<key>`, a hidden `g-recaptcha-response` input and the badge; on submit with the field empty, the form's script asks `grecaptcha.execute(key, {action: 'apply'})` for a token. `captcha-form` only. |
| `recaptcha-v2-submit` | `6LfixtureV2SubmitKeyAAAAAAAAAAAAAAAAAAAAAAA` | An invisible reCAPTCHA bound to the submit button (`button.g-recaptcha[data-sitekey][data-callback="bwaSubmitWithToken"]`): its callback writes the token and sends the form. `captcha-form` only. |
| `hcaptcha` | `10000000-ffff-ffff-ffff-00000000f1x7` | `.h-captcha[data-sitekey]`, a hidden `h-captcha-response` textarea, the badge iframe. |
| `turnstile` | `0x4AAAAAAAFixtureTurnstile01` | `.cf-turnstile[data-sitekey]`, a hidden `cf-turnstile-response` input, the badge iframe. |

- **`captcha-gate`** (the four widget kinds with a container): `/jobs/captcha-gate/apply`
  shows "Verify you are human" with the widget, whose `data-callback="bwaCaptchaPassed"`
  posts `{kind, token}` to `POST /jobs/captcha-gate/captcha-verify`. The expected token
  answers 200 and sets the cookie `bwa_captcha_gate=<kind>` (HttpOnly, an hour), anything
  else 400; after a 200 the page reloads into the core form (`?delay_ms=<ms>`, at most
  9000, delays the reload). A browser profile that passed one kind's gate goes straight to
  the form for that kind; the other kinds still show their gate. The callback sets
  `window.__bwaCallbackCalled = true` first. `?form=email` adds a small form (an "Email"
  input and a "Continue" button) to the gate page: a CAPTCHA page whose callback could
  send a form.
- **`captcha-form`**: the core form with the widget and a hidden `captcha_kind`. The POST
  is accepted only when the widget's response field holds the expected token; otherwise
  it is a 422 re-render with "Please complete the CAPTCHA." on that field.
- **`captcha-steps`**: step 1 carries a `recaptcha-v2` checkbox; its POST needs the
  expected token in `g-recaptcha-response`, else a 422 on that field.
- The response fields and `captcha_kind` are never recorded in `fields` or
  `extra_fields`.

### Shared behavior

- **Disabled options.** `missing-required` offers "3 months or more (no longer offered)" as a disabled option; posting its value is rejected.
- **Machine values differ from labels.** Selects, radios and checkboxes post values such as `wa_authorized` and `sk_python`, while users see "Yes, I am authorized to work in the US" and "Python". Posting a label or an unknown value is rejected with "Select one of the listed options."
- **Accessible markup.** Every control has a `<label for>`, except where a scenario says otherwise (script widgets, and the `custom-uploader` inputs that page script mounts). Radio and checkbox groups are `<fieldset>`s with a `<legend>`. Required controls use `required`; the `*` marker is `aria-hidden`, and optional controls say "(optional)". Controls can be found by accessible name, for example Playwright `get_by_label("First name")`. No test IDs or product-specific hooks are needed.
- **Rejection.** An invalid POST returns 422 and re-renders the form. The page gets an error summary (`role="alert"`, "There is a problem with your application", with links to the fields). Each invalid field gets an inline error, `aria-invalid="true"` and `aria-describedby`. Values are preserved. A valid resume from the rejected POST is retained as "Currently attached: …" with a hidden `resume_upload_id`, so the file input stops being required. Rejections are recorded but **never counted as submissions**.
- **Every accepted POST counts.** Resubmitting an identical form creates another record with a new reference, so a mistaken retry is detectable.
- **Status page.** `/jobs/<job_id>/application-status?email=…` is a public page linked from each posting ("Already applied? Check your application status"). For each matching record it shows the reference, or "still processing" while the confirmation is withheld. This page is how a runtime reconciles an uncertain outcome.
- **Job identity.** Postings include the title, company, location and Job ID. They also carry a canonical link and a schema.org `JobPosting` JSON-LD block with `identifier.value` set to the Job ID. The apply pages repeat the same identity line.
- **Deterministic ids.** Ids come from counters in a fresh state dir: `sub_000001`/`BWA-000001`, `dft_000001`, `upl_000001`, `cap_000001`. Captcha answers are derived from the token. Only timestamps vary. The server adds no artificial delays except the fixed 250 ms of `/__fixture__/cities`. Page scripts have their own timers (`spa-loading`, and the upload and autofill scenarios).
- **Job alerts are not applications.** `apply-in-alert-form` records job-alert subscriptions (`{"job_id", "email", "received_at"}`) separately. They never count as submissions or rejections.

## Test-only API — never call from product code

These endpoints exist for test assertions and fixture control only. The runtime
must reconcile through the public pages. The pages never link to these endpoints.

| Method and path | Result |
| --- | --- |
| `GET /__test__/health` | `{"ok": true, "origin", "state_dir"}` |
| `GET /__test__/jobs` | Scenario catalog and sign-in credentials |
| `GET /__test__/workday` | `workday-wizard`: route visits, saves (page, ok, errors, values), submit calls, uploads, accounts and drafts by account email |
| `GET /__test__/submissions[?job_id=<job>]` | `{"accepted_count", "rejected_count", "submissions": [...], "rejections": [...], "alert_count", "alerts": [{"job_id", "email", "received_at"}]}` |
| `GET /__test__/submissions/<submission_id>` | One submission record |
| `POST /__test__/submissions/<submission_id>/reveal` | Makes a withheld confirmation visible on later page visits |
| `GET /__test__/captcha/<token>` | `{"token", "answer", "used"}`, which stands in for the person solving it |
| `POST /__test__/reset` | Clears all state, including counters, uploads, drafts, sessions, page-load counts and job-alert subscriptions |
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

The replicated flows need page script, so they are tested with headless Chromium
through Playwright and the `tests/browser/conftest.py` fixtures (about 20 seconds):

```bash
uv run --no-sync pytest tests/browser/test_wizard_mock.py -q
```
