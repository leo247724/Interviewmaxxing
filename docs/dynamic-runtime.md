# Dynamic runtime integration

`apply` and `resume` support explicit AI routing and the connected OpenCLI browser:

```sh
interviewmaxxing apply URL --browser opencli --opencli-profile PROFILE \
  --ai-routing --env-file /absolute/path/to/env.local \
  --writer-model anthropic/claude-opus-5.5
```

Both commands remain preparation-only. They use the canonical runner, claim and
heartbeat lifecycle, verified candidate facts, scoped answers, pinned resume,
field executor, and durable no-submit restriction. Complete forms stop at final
review with `NEEDS_INPUT` and `preparation.ready`; no receipt is minted. OpenCLI
keeps the owned review tab available to the user. Missing facts, consent,
attestations, ambiguous controls and ungrounded writing stop earlier.

To inspect a URL without entering any candidate information:

```sh
interviewmaxxing classify URL --browser opencli --opencli-profile PROFILE \
  --ai-routing --env-file /absolute/path/to/env.local \
  --writer-model anthropic/claude-opus-5.5
```

`classify` opens exactly the supplied URL. It does not follow Apply links or
buttons, fill fields, expand dropdowns, advance steps, resolve answers or write
application state. Its JSON includes canonical inspection plus per-field route,
confidence/probabilities, source requirements, and provider call/latency/cost
metadata. A job-description URL therefore reports a job description; inspect the
actual form URL to classify its fields. Its temporary owned tab closes afterward.
Only page observations are sent for classification; no candidate store is loaded.

Without `--ai-routing`, existing deterministic semantics and factual resolution
remain in effect. Without `--browser opencli`, the existing Playwright factory is
used. The writer model must be explicitly configured as
`anthropic/claude-opus-5.5`; credentials are loaded from the explicit env file and
are never printed in receipts. Schema hints use the local schema catalog and
remain untrusted priors, never executable instructions.

After [indexing the candidate evidence and job description](rag-writing.md), add
`--rag-connection-file /absolute/private/connection.json` to `apply` or `resume`.
This enables scoped pgvector retrieval for complex answers and cover-letter text.
The file is local configuration, not model context. Retrieval failure or missing
evidence holds the field; it cannot silently substitute an unrelated job or fact.

Every annotated inspection binds the result to document identity plus the full
control, option, selector, requiredness, constraint, form and action observation.
Provider work runs off the event loop. The browser takes a fresh snapshot after
it finishes, retries changed observations up to three times, and rejects a
persistently changing form. Independently of model validation, annotations may
change only semantic types. The runtime checks the exact issued observation again
before filling; a packet cannot authorize a replaced document, selector or
changed constraint. Filled values do not invalidate the structural provider cache.

Verification lives in `tests/browser/test_dynamic_runtime.py` and
`tests/core/test_dynamic_cli.py`. Normal runs exercise only isolated headless
localhost pages and synthetic providers. The OpenCLI canonical-runner fixture is
explicitly opt-in with `IMX_DYNAMIC_OPENCLI_LIVE=1`; it uses a separate
`imx-dynamic-fixture` session and verifies zero server POSTs, no submission receipt,
readback of fictional identity values, ignored injected page instructions and a
single cached full-form classification. It closes only its owned fixture tab after
verification. It never targets an employer URL.

The HTTP service supports the same runtime through explicit environment settings:

```sh
IMX_SERVICE_BROWSER=opencli
IMX_SERVICE_OPENCLI_PROFILE=PROFILE
IMX_SERVICE_AI_ROUTING=1
IMX_SERVICE_AI_ENV_FILE=/absolute/path/to/env.local
IMX_SERVICE_WRITER_MODEL=anthropic/claude-opus-5.5
IMX_SERVICE_RAG_CONNECTION_FILE=/absolute/private/connection.json
```

Service defaults remain deterministic Playwright, AI routing off, `TEST_ONLY`, and
preparation-only. Enabling AI or OpenCLI does not change the application mode or
enable submission. Incomplete or invalid settings fail configuration parsing.
Health and application preflight check local runtime availability and the presence
of a readable configured API key without constructing providers, starting browsers,
loading candidate data or making network calls. Missing credentials mark the runner
unavailable and block application requests before they are recorded. Credentials
and their contents are never included in health responses. The configured runner
constructs its AI components only when executing an authorized application run.

## Custom widgets (menus, lookups, phone pickers)

Hosted ATS forms (Greenhouse, Rippling) render most choices as script widgets: React
selects whose options exist only while the menu is open, Rippling `div` comboboxes,
search comboboxes, location lookups and intl-tel-input phone fields. The generic
runtime operates them without site adapters, coordinates or provider-written code; every
page script involved is a fixed read-only script (also allowlisted for OpenCLI).

- **Probing at inspection.** For each visible, enabled, closed menu control of the
  selected application form (`role=combobox` or `aria-haspopup=listbox`, not inside a
  dialog, not multi-select, options not observable yet) the runtime focuses it, clicks
  it (never an already open one), falls back to ArrowDown, reads the options of the one
  listbox its `aria-controls`/`aria-owns` names (label, `data-value`, `aria-selected`,
  disabled, index; never a page-wide option scan), closes it again and verifies that the
  document, the displayed value and the form's field set are unchanged. It never types
  or chooses. Closing takes the steps a person would, until `aria-expanded` is false:
  Escape, one toggle click on a click-opened control, then a press outside every control
  (pointer and mouse press events on the page body, never a click on anything; Rippling's
  popovers close only that way). The step that closed a document's previous menu goes
  first. Inside a dialog (a wizard step) the control's own toggle goes first and nothing
  is pressed outside, since Escape or an outside press may close the dialog. A menu the
  probe cannot observe or close stays `UNSUPPORTED`, even while it shows its list;
  probing stops for that document. A complete static option set becomes a canonical `SELECT` (values and
  labels exactly like a native select); an input whose menu offers nothing until
  something is typed becomes a `TYPEAHEAD`; multi-select menus and anything ambiguous,
  virtualized past 5 scrolls or over 500 options stay `UNSUPPORTED` for the user.
  Observations are cached per document (reused by later inspections without reopening,
  dropped on navigation or context loss); at most 24 probes and 20 s per document, 3 s
  per control. Probing runs before semantic annotation with menus closed again, so it
  never counts as a form change. `observe`/`classify` and waits for the user never probe.
  A menu's displayed value or placeholder ("Select...", "+1") is state, not question
  wording, so fingerprints and saved-answer matching stay stable after filling. That
  display is read as the browser renders it: adjacent text nodes run together, as with
  React's "+" and "1" for Greenhouse's Country value, which shows "+1", not "+ 1". A div
  menu's `aria-label` that is its placeholder or its displayed value ("Select") names
  no question; the text around it does.
- **Open menus are not page changes.** A menu, list or dialog that a combobox or picker
  owns belongs to that widget wherever it renders, in a body portal or inside the form
  (Greenhouse). It never adds text to another question, never shifts another element's
  position in a selector, and its buttons, links, headings and live regions are not the
  page's. react-select's hidden required proxy input (`aria-hidden`, `tabindex=-1`,
  rendered only while a required select is empty) is part of its select, not a
  question. A list inside such a popup is part of it too, even when `aria-controls`
  names a wrapper around it (Ashby's location lookup mounts its suggestions in a portal
  of its own). While a widget is being operated (between typing into a lookup and
  choosing its suggestion, or opening a menu and choosing), a difference confined to
  the popup the widget owns at that moment is not a page change. That covers what its
  `aria-controls`/`aria-owns` names, up to that popup's own container, and the element
  paths the mounted popup shifts in later questions. The rest of the guard is unchanged.
  While a popup is open, an overlay manager (Floating UI, the `aria-hidden` package behind
  Radix) marks everything else `aria-hidden`, with `data-aria-hidden`: Ashby's location
  lookup (Sanity) hides the whole form that way while its suggestions show. Such marks
  hide nothing on screen. They count only while a modal dialog is open; otherwise the
  questions, their titles and the page's actions read as they are shown.
- **Selecting.** A probed menu is opened the recorded way, its listbox re-resolved after
  opening, and the one matching option (freshly derived from the owned listbox) clicked.
  A menu that already shows exactly the chosen option (pre-filled, like BambooHR's
  "United States") is verified by its display and not operated. Only when an input
  menu of more than 20 options does not render that option is it filtered by typing;
  typed text is cleared again if nothing matches. The label is tried first, then its name without a trailing code or
  parenthetical ("United States" of "United States +1": Greenhouse filters on the
  country's name), then its first word. A menu still open after the choice is closed the
  way the probe closed it. A site that saves a choice before showing it (Ashby empties the
  input until its save returns, then shows the choice) is read back once the display
  returns, at most 3 s; lookups wait the same way. Readback:
  the menu closed and the control displays the chosen label. When it displays only a
  suffix of it (a dial-code select shows "+1", which "United States +1" and "Canada +1"
  share), the menu is reopened once and its own selection must name the chosen option;
  the first signal the widget exposes decides: `aria-selected` (exactly one option),
  `aria-activedescendant`, or exactly one option whose class names the selection
  (react-select's `select__option--is-selected`). A confirmed choice keeps naming the
  "+1" display in that document. A display that names no option at all is re-resolved
  the same way; the reopened menu's own selection decides, but only when the display is
  consistent with the chosen label (its letters and digits, in order, within the
  label's: "+ 1" or "United States" for "United States +1") and, where the menu exposes
  both, `aria-selected` and `aria-activedescendant` name the same option. APG and
  Downshift menus mark the option they highlight on opening (the first) as selected, so a
  click that did not take behind a placeholder ("Country *") is never confirmed. A
  display naming another option, or nothing, is `VERIFICATION_MISMATCH`.
- **Menu buttons over a hidden select (BambooHR).** A role-less
  `button[aria-haspopup][data-menu-id]` beside an `aria-hidden`, `tabindex=-1` select
  that holds only the current value is one field. Its label, name and required flag come
  from the select and its `<label for>`, and it is operated through the button. The
  select, the button and its "Clear Selection" are never reported separately or as page
  actions. The button is probed like other menus. Its menu is the element whose id is the
  button's `data-menu-id`, a `role=menu` whose menu items are the options, and a menu
  not rendered yet is no menu. The menu's search box and the hidden portal it leaves
  behind are part of the widget. A display inside an element whose class names a
  placeholder, or "Select" framed by dashes ("–Select–"), is a placeholder.
- **Lookups.** A `TYPEAHEAD` answer is typed (about 30 ms per character). Suggestions are
  read from the owned listbox once stable for 400 ms. The wait is at most 6 s, or 3 s
  while nothing appears: Rippling loads its place search on the first query. A shown
  list counts even when the input never exposes `aria-expanded`, as Rippling's location
  input doesn't. A link beside the suggestions (a "powered by …" attribution) does not
  make the list unusable. Suggestions are matched with
  US state abbreviations and United States synonyms spelled out: the typed place must
  equal a whole comma segment ("Austin" never matches "Austintown") and every other typed
  word must begin a word of the suggestion. A suggestion typed verbatim always wins.
  Exactly one match is clicked and read back (`FILLED`); otherwise the input is emptied
  and `NEEDS_CHOICE` carries up to 20 suggestions in the order shown.
  `fill_fields(form, packet, field_ids)` (`SelectiveFill`) then types the chosen label
  into just those fields with the same guards as `fill`, leaving every other control as
  it is.
- **Phones.** A `tel` input whose own widget has a country picker (an `.iti` container, a
  preceding `aria-haspopup=dialog` button, or a sibling combobox showing `+<code>`) sets
  `ApplicationField.expects_international_phone`; its value is typed as given and read
  back by digits and by the picker's dial code. The picker belongs to that field: an
  intl-tel-input flag or dialog button is never probed or reported as a question of its
  own, even when it is named "Country" beside a form's own Country select. The picker's text is its name, its
  title and its own text, because intl-tel-input with a separate dial code (Workable)
  shows "+1" only in a child element, and "+1" is taken out of the input. A plain tel
  input keeps the exact fill-and-readback.
- **File uploaders.** A file answer is verified by the attached bytes. When the page's
  uploader takes the file and empties its input (Workable), or replaces the input with
  the file's name (Greenhouse unmounts the input together with its Attach and cloud
  buttons), the answer is verified instead by the uploader's own container. That
  container, recorded before attaching, must show the file's name, with no error alert
  and no progress bar. The file question then keeps its approved wording while the
  uploader shows that file, and the file is not attached again on a second fill.
  A hidden file input labelled with its button's verb ("Attach") takes its question from
  its uploader group ("Resume/CV"). One named only by a developer's token (BambooHR's
  `aria-label="file-input"`) takes the short question its uploader box states ("Resume
  *", "Cover Letter"); "No file selected" is state, not question text. A file input
  outside every form in an upload popup (`[role=dialog]`; Jobvite's "Attachment Options"
  appended to `<body>`) belongs to the one popup button in a form that names the same
  document (résumé or cover letter). It takes that button's name ("Add Resume"), form
  and requiredness, and the button's box is the uploader's container; the input is
  attached directly and nothing in the popup is clicked.

  The file is verified against the element it was set on. When the page removed that
  element, or the selector now names several elements or one that is not a file input
  (Teamtailor's Dropzone hands the id to a fresh input and to a hidden URL field in its
  preview), the uploader container recorded before attaching is read instead. While the
  uploader shows the upload in progress ("Uploading…", a progress bar) the readback
  waits, up to 15 s. An uploaded question that stays bound to a fresh input keeps its
  approved binding while the uploader shows the file, and hidden inputs inside our own
  uploader's container are not page changes (visible new controls still are). An attach
  accepted on the uploader's display alone, because the page kept no readable copy of the
  bytes, says so in its detail ("the attached bytes were not verified"). An uploader
  that shortens the name ("resume_av…quill.pdf") or shows a count ("1 file selected")
  still counts as showing the file, so a second fill never uploads again. Teamtailor and
  Workable upload the file to their storage the moment it is attached, even in a
  preparation-only run.
- **Passing states and own changes.** Before every write the page must still show the
  approved questions, bindings and actions. A difference is waited out for up to 1 s,
  because Greenhouse disables its "Autofill my application" button while it handles a
  keystroke; only a difference that persists stops the fill.

  Buttons and the submit/next actions count by what they are (text, kind, form, request),
  not by their position or whether they are enabled: a submit enabled once required
  fields are valid, or moved to another container, is the same action. A menu's own
  buttons are part of its widget, not page buttons ("Toggle flyout", a "Clear selection"
  that appears with a value). Validity (`aria-invalid`) and error messages, including a
  description the control names in `aria-errormessage`, were never part of the guard.

  After this runtime's own upload, a change confined to that uploader's container is
  our answer arriving, at any later write and in the after-fill and review checks. The
  change may be the Attach and cloud buttons giving way to the file's name and "Remove
  file", the file input going, or the page's actions moving. Greenhouse re-renders the
  uploader seconds after the attach. Every question, option set and binding must still
  be as approved, and the approved observation then moves to the page as it now is.
- **Closed postings.** "Job not found", "The job you requested was not found", "posting
  not found", "job does not exist" and "no longer open" (Greenhouse's redirect for a
  closed job) classify `JOB_CLOSED`, as "no longer available" already did. Without an
  HTTP 404/410 the wording must persist: the page settles and is read again for 2 s,
  and a later reading that is no longer closed wins. An SPA may render "Job not found"
  before its data arrives.
- **Consent pages (Jobvite).** A data-processing consent page in front of the form
  (Jobvite's "Data Consent": such a heading, one or two choice questions, no way to
  apply) is `SIGN_IN_REQUIRED` with a message to accept the consent in the browser. The
  runtime never chooses a policy or clicks "I Accept"; choosing Jobvite's default policy
  already posts the consent. The runner asks, `wait_for_user` returns once the form
  shows, and the item reads "Accept the data-processing consent". A form reached after
  the person acted, with no job identity of its own, takes the identity of the posting
  `open()` loaded when it is on the same origin at the posting's path or one segment
  below it, as `open()` does after following an apply link.
- **Uploads and annotation.** A file question kept after its uploader replaced the input
  is restored before the form is annotated, so the provider annotates, and later
  resolves reuse, exactly the form the runtime returns.
- **Waiting for the user** never opens a menu, including the final read after the wait.
  The exception is a wait that began on a page asking the person to act (a sign-in, a
  CAPTCHA, a consent) and ended because that page is gone. The page it led to is new, so
  it is read as `open` reads a page: once ready, with its menus probed (see "Workday
  application wizards").
- **Questions, not placeholders or ids.** A field's label is the question the page shows:
  its label, legend or accessible name. Without one, the question its own box states
  comes next: a `<label>` that labels nothing (Ashby's title `for` a field path no element
  has, as on its date picker; a required "*" its CSS draws with `::after` counts as
  shown), or the heading its block opens with (Breezy's `<h3>`
  before the input, the options or a salary block's currency, amount and period), unless
  another field of that block has its own label. A checkbox stating its own text (Breezy's
  SMS consent after the phone input) keeps it over a heading that opens another field.
  After that comes the text before the control, climbing out of boxes that open their
  parent (Lever's "Pronouns" before its options), for machine-named controls only. A
  placeholder that only says what to do ("Type here...", "Pick date...", "Type your
  response") is never a label; it stays the placeholder. Neither is an identifier
  (`section_…_question_3`, `field-12`, `startDate`), an accessible name that is a
  developer's token ("file-input"), or an option's text: a group whose only name is an
  option ("White (not Hispanic or Latino)", "Yes") takes the question shown for it, else
  a readable name, else none. An unlabelled option (Breezy's `<li><input
  type=checkbox><span>Google Ads</span>`) is named by its own text.
- **Options without a shared name.** Radios or checkboxes of one fieldset (or
  `role=radiogroup`/`group`) whose names are all different are one question when each
  name is empty or the option's own text (Ashby's `name="Yes"`/`name="No"` checkboxes:
  Compyl's sponsorship question was two fields labelled "Yes" and "No"); radios need
  only different names. The field id is the question's field path. Separately named
  checkboxes of one fieldset ("terms_consent", "privacy_consent") stay separate
  questions. Such a group is typed by its question and field path, never by an option's
  name.
- **Semantic types of choices.** A profile URL type (LinkedIn, GitHub, website) belongs
  only to a single text input, never to a choice. "How did you hear about …" wording is
  the referral question on any control, whatever its options say ("Company website",
  "LinkedIn"). Before this, Base Power Company's checkbox group came out typed WEBSITE
  and the resolver held it.
- **Yes/no toggle buttons.** A question drawn as buttons with `aria-pressed` over a
  checkbox that only mirrors "yes" (Ashby's yes/no, the checkbox `display:none`) is a
  `RADIO` whose options are the buttons, clicked (unless already pressed) and read back
  by `aria-pressed`. The pressed state is the answer, like a checkbox's checked state,
  never page structure. The buttons are not page buttons. Only buttons that cannot submit a
  form by themselves count (no form owner, or `type=button`): Ashby's are
  `type=submit` outside any `<form>`. Before this, such a required question was not in
  the model at all.
- **Calendar popups.** A date input's calendar (react-datepicker's popper, opened inside
  the input's own field box while it has focus) is a popup: its month list, day options,
  month buttons and text never become questions, question text, page buttons or selector
  positions, so an open calendar changes nothing the fill guard compares.
- **User agent.** Headless Playwright sessions present the browser's own user agent with
  a Linux desktop platform segment (`(X11; Linux x86_64)`; product and version tokens
  unchanged): react-select leaves out `aria-selected` and `aria-activedescendant` when
  the user agent names an Apple platform (a VoiceOver workaround), which was confirmed on
  a live Greenhouse board on a Mac. Visible sessions and OpenCLI keep the platform user
  agent, so the person's own browsing is unchanged.
- **OpenCLI.** The same flow runs over `focus`, `keys` (sent only while the target holds
  focus) and `type`. When the session cannot press keys, a probed menu cannot be closed
  or re-verified by Escape and is held for the user; menu lists are never scrolled there.

Mock scenarios `react-select`, `div-combobox`, `typeahead`, `phone-widget` and
`multiselect-react` (`tests/browser/MOCK_ATS.md`) reproduce these widgets; the tests are
`tests/browser/test_custom_widgets*.py`. Round 7 added `/forms/breezy-like`,
`/forms/ashby-like` (placeholders, orphan titles, a date picker, name-per-option groups,
yes/no buttons, a lookup whose suggestions mount a portal), `bamboohr-like`,
`teamtailor-like`, `jobvite-like`, `flash-closed` and `react-controlled-narrative`.

## Round 12: Paylocity controls, cookie banners and four live fixes

Paylocity's apply form (one page, `#btn-submit` as its next control) drew four shapes no
earlier widget covered, and retry five of the batch pilot failed four fills on Lever,
BambooHR and an embedded Greenhouse form. Mocks `paylocity-address` and
`bamboohr-required` (`tests/browser/MOCK_ATS.md`); tests
`tests/browser/test_paylocity_round12.py` and `tests/browser/test_round12_live_fixes.py`.

- **react-widgets DropdownList.** A `div[role=combobox][aria-haspopup=true]` owning
  `<id>__listbox`, showing `--` until chosen, is probed like any div menu (its
  `li[role=option]` list mounts inside the widget while open) and becomes a `SELECT`
  whose options are the shown texts; it is chosen by clicking the option and read back
  from its display. Dashes alone are a placeholder. A `div` is not labelable, so its
  question is the visible `<label for>` that points at it, else the `data-for` text when
  the page shows that text; the widget is required when that label ends with an asterisk
  (hidden from assistive technology or not). The SMS question with its SMS policy is
  `CONSENT`; "Have you worked with us before?" stays `CUSTOM_SELECT`.
- **Input-select.** A react-select without ARIA roles: a role-less text input
  (`aria-autocomplete=list`, no `aria-haspopup`) under a value element classed
  `*single-value`/`*placeholder` that covers it and takes the pointer, with options as
  plain `div`s classed `*option` shown only for what is typed. The value element is
  looked for only in the input's own widget (boxes around it holding no other field). The
  label leaves out what the widget shows ("Country", not "Country United States"); the
  field is a `TYPEAHEAD` whose value is that display. `fill` leaves a display already equal
  to the answer as it is ("already shows this value": Country "United States"); otherwise
  it focuses the input (never a click, the value element covers it), types the answer,
  reads the options until they are stable, clicks the one option whose normalized text
  equals the answer and reads the display back (3 s for the site's round trip). No equal
  option: Escape, nothing chosen, `NEEDS_CHOICE` with the listed options as suggestions.
  What the widget shows is its answer, never page structure, for the fill guard. The
  browser-validity read before `advance`/`submit` skips an input-select that shows a
  choice (its required input stays empty; the site validates it by script) and reports
  one that shows its placeholder under its question ("State").
- **Street address with suggestions.** An `ADDRESS` input with `role=combobox` and
  `aria-autocomplete=list` (Address Line 1) is a `TEXT` field: the street is typed,
  Escape closes the suggestions (a no-op for a session without key presses) and the input
  is read back. A suggestion is never chosen.
- **Cookie banners are declined, never accepted.** On a page that mentions cookies, a
  button outside the application form (or a control of a visible cookie dialog) that
  neither submits nor navigates and declines non-essential cookies is clicked: "Reject
  All", "Decline", "Necessary cookies only", "Accept only necessary cookies", "I do not
  accept", "Deny", "Continue without accepting". That happens when the page opens, before
  `inspect` and `fill`, and during a fill when a banner slides in later (OneTrust, over the
  bottom half of the page); the page is then read again and compared with the approved
  one. Nothing else on a banner is clicked: never "Accept All Cookies", "I accept", "Allow
  all" or "Agree", and not a notice's "Got it"/"OK", which is often the accept button
  relabelled (OneTrust's `#onetrust-accept-btn-handler`). A banner without a decline stays
  for the person. Each button is clicked at most once per document.
- **A. The upload's own progress (Lever).** After attaching, busy markers count only in
  the upload's own field (the largest box around the input that holds no other visible
  field), and never a marker that already outlasted a whole bounded wait in the document
  (Lever's "Apply with LinkedIn" helper that stays "Loading..." above the resume had
  kept every Lever resume "still in progress when the wait ended"). While the uploader
  shows its own progress, the wait lasts up to 60 s; an upload showing neither progress
  nor the file is read for at most 20 s (and the settle timeout), and 2 s more after its
  progress ended. Only a state still in progress at the bound is reported as such.
- **B. A choice that makes a question required (BambooHR).** Answering work authorization
  marks the sponsorship question required (its asterisk); nothing appears. Requiredness is
  not part of a question's fingerprint but is part of the fill guard's structure, so the
  round-11 path (which needed an added question) did not apply and the fill failed as a
  changed page. Now, after a choice this fill made, questions that became required or
  optional are treated like revealed ones: the fill stops with "… question(s) (…) became
  required after the answer to '…'; inspect this step and resolve it again before
  continuing", nothing is reported failed, and the runner resolves the step again. A
  reworded or removed question, a changed action or context, or a change after a text
  write still stop the fill; that failure now says what changed, by position and field id
  with the kind of change ("changed: #5 customQuestionAnswers.yes_no_2102 (required); #6 …
  (wording)", then "actions" or "employer context"), never the wording or a value.
- **C. A fixed dialog over the checkboxes (embedded Greenhouse).** A checkbox or radio is
  set by clicking the input, then its label; when something else takes the pointer where
  they are (the error says an element "intercepts pointer events", or both clicks fail),
  the click is dispatched to the enabled input itself, never to whatever covers it, and
  `checked` is read back. A forced pointer click is never used: it would land on the
  covering element, possibly a banner's "I accept".
- **D. Phone numbers the site formats.** A `PHONE` field's readback compares digits only
  (spaces, dashes, parentheses and "+" ignored, and a leading country code 1 on an
  11-digit number): "+1 303 555 0142" read back as "(303) 555-0142" is `FILLED` with the
  detail "the site formats it as '(303) 555-0142'". Other digits are still a mismatch.
  The end-of-fill sweep compares the same way, so a formatted number is not typed again.

## Round 13: forms that re-render while being filled

Retry six failed four fills on pages that re-render after a value is set; the fill guard
read each as a changed form. Round 12's failure details named what changed, and each shape
is now a mock (`bamboohr-churn`, `greenhouse-eeo`, `teamtailor-late`; tests
`tests/browser/test_round13_rerenders.py`, including a preparation-only run of the runner
on each that ends at the final review step).

- **Generated ids that churn (BambooHR, Zinda 881 and 891).** Once a Yes/No is answered,
  BambooHR mounts its Fabric text fields again under new generated ids: "Date Available"
  (no name, so its field id is its DOM id) went from `FabricTextField-68` to `-355`, and
  Address, City and ZIP kept their names with new selectors. A question matched by id on
  neither side is the same question when it is the same occurrence of the same wording,
  placeholder, control and options after the same question matched by id
  (`_renamed_questions`). The fill guard always reads the page with such questions under
  the ids the fill knows them by, so the re-render is a re-render: the next write goes
  through the re-resolved control ("Date Available" is typed into its new element), and a
  step whose ids churned after the fill is not "changed since the fill" for `advance`,
  `submit` or `prepare_review` (only selectors or generated ids differ). The runner still
  resolves the step once more when the packet's form fingerprint no longer matches the ids
  on the page.
- **Follow-up changes do not stop the fill.** When the only differences from the structure
  the fill writes against are follow-up changes, that structure takes them in and the fill
  goes on with the approved answers (round 11 stopped at once):
  - unanswered questions that appeared anywhere, after a choice (Greenhouse shows "Please
    identify your race" right after "Are you Hispanic/Latino?" once "No" is chosen) or
    whatever was written last (Teamtailor renders its "Linkedin profile" question only once
    it scrolls into view, while the first answers are typed);
  - right after a choice this fill made, and only then: questions that became required or
    optional, and a question whose help text changed (Greenhouse's Hispanic/Latino
    question gains a definitions hint with the race question).
  The readback of the step then reports the new questions `SKIPPED` ("appeared while
  filling (Please identify your race); answered once this step is resolved again") and
  asks for a fresh inspection ("1 question(s) appeared (Please identify your race) and 1
  question(s) (Are you Hispanic/Latino?) changed their help text after the answer to 'Are
  you Hispanic/Latino?'; inspect this step and resolve it again before continuing"); the
  runner resolves the step again, and the second fill answers them. When a choice changes
  the requiredness or help text of a question not written yet (round 12's sponsorship
  question), the fill stops there for that inspection instead: that answer is not written.
- **Still a changed page, never written through:** a question that appears already
  answered (a pre-checked attestation), a reworded, removed or moved question, a changed
  option, length limit or type, requiredness or help text that changes after a typed
  answer (the page must not re-word a later question between our writes), and changed
  actions or employer context. The comparison leaves out the text before each control
  (`DomControl.preceding`: a question inserted before it replaces it; a label drawn from it
  is compared as the question's wording), requiredness and the changed questions' own
  controls. The failure names what changed, and a question that appeared by its wording:
  "changed while filling (appeared: #1 candidate[answers_attributes][0][text] (Linkedin
  profile); changed: #4 email (wording))"; the question itself is reported "appeared or
  changed while filling (Linkedin profile); not answered by this packet".
- **Uploads.** An uploader's later re-render (Greenhouse) is compared with the structure the
  fill writes against now, follow-up questions it took in included.

## Round 14: appeared questions in the same run, Paylocity's work history, ARIA choices and the consent page

Retry seven's holds and failures, each reproduced on a fictional mock (tests
`tests/browser/test_round14_live_fixes.py` and `tests/browser/test_round14_data_consent.py`,
including preparation-only runs of the runner that end at the final review step). CAPTCHAs
are solved through 2Captcha behind a flag (see "CAPTCHAs").

- **Questions that appear are answered in the same run.** Round 13 named them ("appeared or
  changed while filling (Are you Hispanic/Latino?); not answered by this packet") but the
  run still failed: when the page also changed a control outside the questions (live, the
  change summaries named only the questions), the strict comparison of every other control
  failed and the step's readback took the change for a changed page. The question-level
  change is now enough when the page is still the same application step
  (`_same_application`: address, title, meta, structured data, job identity, kind and step,
  every form's request, every button that submits a form, and the next and submit controls
  by what they are; a button that submits nothing, such as "Show definitions" or "Clear",
  does not count). Then:
  - at the step's readback, the new questions are `SKIPPED` and named and the page error asks
    for a fresh inspection; the runner inspects the step again, resolves it (the saved
    `hispanic_latino`, `race_ethnicity` and `linkedin_url` answers answer them) and fills it
    before going on to the final review step;
  - during the fill, nothing more is written and the step goes back to inspection the same
    way (the round-11 reveal path).
  The comparison still rejects a reworded or moved question, a question that appears already
  answered, and changes of the page or its submitting actions; those fail as before, named.
  Mocks: `greenhouse-eeo?reveal_extra=1`, `teamtailor-late?lazy_extra=1`.
- **A question a choice takes away.** Right after a choice this fill made, questions that
  disappear without having been written are a follow-up change too (Paylocity hides the
  entry's end date once "I currently work here" is checked); the readback names them ("1
  question(s) (End Date) disappeared").
- **Paylocity's "Address Line 1" when its list cannot be probed.** A live form names its
  suggestion list in `aria-controls` but mounts it only once there are suggestions, so the
  menu probe finds no list and the text combobox was an unsupported control. An unprobed
  text combobox whose wording is an address is now the typed address answer (round 12's
  path: typed, Escape, read back; a suggestion is never chosen). Mock
  `paylocity-address?address_list=late`.
- **Paylocity's work-history entry.** "Start Date", "End Date" (both stating `MM/YYYY`) and
  "I currently work here" in an entry whose field paths name a work history
  (`txt-workHistory-startDate-0`, `workHistory.currentlyWorkingHere.0`) are answered from the
  profile's roles (`CandidateProfile.experience`, the resume's roles): entry 0 is the most
  recent role (a current one first, then by start), a date is written only in a format the
  question states (`MM/YYYY`, `YYYY-MM`, a month input), and every answer cites the role's
  verified facts (`interviewmaxxing_generation.work_history`). For a role the person still
  holds, the box is checked and the end date is left blank, which is not asked of the
  person (a non-blocking missing input that routing leaves alone). "I currently work here"
  is a yes/no question (`CUSTOM_BOOLEAN`), not an attestation. In routed runs Jev reads
  these questions as the applicant's past (`HISTORICAL_OR_CONTEXTUAL`, live: 1.0 for the
  dates, 0.84 with 0.14 current for the box), for which the profile copy is never allowed,
  so the route gate held every such answer. It now admits exactly this derivation when the
  route is a sure `COPY_KNOWN` and the source is the applicant's own, current or historical
  together at 0.95 or more (`_own_work_history`, traced as `work_history`); another
  person's dates never pass. Mock `paylocity-work-history`.
- **Checkbox and radio groups drawn as ARIA widgets** (Greenhouse's job board, Radix-style):
  every option is a `button[role=checkbox|radio][aria-checked]` named by a `<label for>`,
  beside an `aria-hidden`, invisible native "bubble" input that carries the name and value.
  The inspector reads each group as one question with its options (a `fieldset` or
  `role=group` of checkboxes, a `role=radiogroup` of radios, or one checkbox), the bubble
  inputs are never questions of their own, and the runtime clicks the buttons and reads
  them back by `aria-checked`. Routing can then answer "How many clients do you currently
  support?" and "What range of monthly budgets are you used to working with?". Mock
  `greenhouse-aria`.
- **The "double-check" attestation.** "Please double-check all the information provided
  above. Ensuring accuracy is crucial…" is one of those ARIA checkboxes and is typed
  `ATTESTATION` (the wording asks the person to vouch for the information). Like any
  attestation, only the person's own answer checks it: an exact saved answer, or the
  statement-coverage decision over their saved statements (`certify_information_true`).
- **Jobvite's data-processing consent page** ("Data Consent": choose a location of residence
  and language, then "I Accept") is still reported as a page the person acts on
  (`SIGN_IN_REQUIRED`), and `open`, `inspect` and `wait_for_user` still never touch it. The
  runner now asks first whether the person's own statement covers it:
  - `data_consent(residence)` reads the page's question without touching the page, as a
    one-field form: a required `CONSENT` checkbox "I accept the <policy>" under the page's
    heading. The policy is the page's only one, else the one naming the person's country of
    residence (its name, or "US", "U.S.A.", "UK"); none, or several (English and French for
    Canada), leaves the page to the person;
  - the runner resolves that question like any consent on a form: an exact saved answer or
    input, else the routing resolver's statement coverage over the person's saved
    statements (`acknowledge_privacy_notice`);
  - only a checked answer from the person's own answers lets `accept_data_consent` choose
    the policy, wait for the page's one "I Accept", keep evidence of the policy shown
    (`data-consent`), click it and read the form it leads to (with the posting's identity).
    The runner records `consent.accepted` (the question, the page and the ids of the
    answers that cover it), once per preparation run; a submission run of an approval
    resolves nothing, so there the page stays the person's.
  Accepting sends the consent to the site; it never submits an application. A consent no
  statement covers stops the run as before ("Accept the data-processing consent"). Mock
  `jobvite-like` (`?policies=regional` for one policy per location).

## Uploads, autofill overlays and readback

Hosted forms upload through styled controls and react to the upload: Ashby and Lever parse
the resume and autofill name, email, phone, location and LinkedIn; Greenhouse, Rippling,
Workable, Teamtailor and BambooHR put "Attach"/"Upload" buttons or drop zones over a hidden
`input[type=file]`; Lever shows an "Apply with LinkedIn" helper that reads "Loading..."
first; React forms re-render after the first input. The generic runtime handles these
without site adapters:

- **Uploads first.** `fill` attaches every answered file before anything else, directly
  to the file input (hidden or not; a hidden input counts as an upload field when a
  visible label, "Attach"/"Upload" button, link or focusable drop zone in its own box is
  there). The driver verifies the bytes against the pinned resume: those the input holds,
  or, when the uploader emptied or replaced its input, the bytes it was handed with its
  input/change event (when the page can hash them) together with the uploader's own
  container showing the file's name without an error (see "File uploaders" above). A file
  is never attached twice: not when the input already holds the pinned file (name, size
  and SHA-256; "already attached"), and not when this session attached it in the same
  document and the uploader, having emptied or replaced its input, still shows it ("the
  uploader already shows this file"). After attaching, a spinner or "Uploading..."/
  "Analyzing resume..." text is waited out (at most 20 s) and the upload is read back from
  `input.files` (name and size) or, once the input is emptied or replaced, a visible chip
  naming the file or an `aria-live`/status notice; a visible upload error is a mismatch.
- **Then settle and re-read.** The page is read until nothing is busy and two reads 0.3 s
  apart agree, values included (an autofill the upload triggered has landed; at most 20 s).
  Only values, validation messages, the attached control's own description and controls
  (its chip or status line, a replaced input kept as approved), the buttons inside its
  uploader's container and where the page's actions sit may have changed: actions count by
  what they are, so a re-rendered action area is accepted while the submit control keeps
  its text and its form. Every other question and binding must be as approved. Then every
  other answered field is filled and read back, so our verified values overwrite the site's
  autofill. If other questions, constraints, bindings, actions (a submit with other wording
  or another form) or employer context changed, the fill stops with a page error (the
  attached file stays; the runner re-inspects and resolves the step again, and the next
  fill does not attach again). An uploader that re-renders later (Greenhouse, seconds after
  the attach) is accepted at any later write by the same rule (see "Passing states and own
  changes" above). Pre-checked consent boxes the packet does not answer are cleared as
  before.
- **Follow-up questions a choice reveals (round 11).** BambooHR shows conditional fields
  the moment a Yes/No is chosen ("Will you now or in the future require employment visa
  sponsorship?" reveals two follow-ups). When the freshness check before the next write,
  or the readback of the step after the last write, finds that only *additional*
  questions appeared after a choice this fill made (radio, select, multiselect,
  checkbox, checkbox group or lookup): every approved question still shown, in its order,
  with the same wording, options and binding shape, the actions and the employer context
  as approved once the revealed questions' own controls are left out, then that is a
  conditional reveal, not a lost context. The fill stops with the same kind of page error
  as after an upload that changed the step ("2 follow-up question(s) (…) appeared after the
  answer to '…'; inspect this step and resolve it again before continuing"): the question
  about to be written is not attempted and nothing is reported failed (follow-ups found by
  the readback are reported `SKIPPED`), the answered choice stays answered, and the runner
  re-inspects and resolves the step again with every answer, the choice included, so the
  run reaches its final review in two resolutions. A reworded or removed existing question,
  a changed action or context, or questions appearing after a *text* write still stop the
  fill as before ("questions, bindings, actions, or employer context changed while filling";
  the remaining answers are not attempted). Mock: `bamboohr-conditional`. Round 12 sends
  questions a choice makes required or optional down the same path (`bamboohr-required`).
  Round 13 lets the fill go on past a reveal with the approved answers and treats
  unanswered questions that appear after a typed answer alike (see "Round 13").
- **Question text of upload controls** leaves out the trigger ("ATTACH RESUME/CV"), file
  chips, sizes and upload/parse status, and a label that only says "Attach" yields to the
  group's question ("Resume/CV"), so an upload does not change the field's fingerprint and a
  "✱" after the question still marks it required.
- **Overlays and autofill offers.** `open`, `inspect` and `fill` wait (at most 8 s) while
  loading overlays are shown: `aria-busy` regions, progress bars and short status texts
  ("Loading...", "Parsing your resume…", a button reading "Loading..."). Third-party
  helpers ("Apply with LinkedIn", "Autofill my application", "Import from Indeed") are
  never clicked, never a submit or next action, and not part of the observed structure, so
  a helper that finishes loading mid-fill no longer aborts the fill. A dialog offering to
  autofill the application is declined once per document with its own decline control ("No
  thanks", "Not now", "Close"), never its accept control, and the page is read again.
  `observe`/`classify` does neither.
- **Readback robustness.** Text is typed with real input events (Playwright's trusted
  insertText), never by assigning the value, so React-controlled inputs update their
  state. A mismatching readback is read once more after a settle, with the control
  re-resolved by its field id (same question fingerprint); if the re-rendered control lost
  the value it is typed once more key by key and read back; only then is it
  `VERIFICATION_MISMATCH`. A textarea, text containing a line break or tab, and text over
  200 characters are written again as one input event instead (typing a newline would
  press Enter; the contract allows `\n`, `\r` and `\t` in a textarea). A Playwright
  error counts as a lost page only by its own message, not by its call log, which always
  mentions waiting for navigations. Before each write, after the
  1 s passing-state settle above, a transient re-render (the form's controls briefly
  missing, a busy marker) is waited out (at most 3 s more) instead of aborting; a
  re-render that regenerated only selectors re-reads the form and the field is operated
  through its re-resolved control, never a stale selector. At the
  end of the fill, text that the page changed after its readback (a late autofill, a
  reverting controlled input) is written once more and verified.
- **Details quote values.** A fill result's detail quotes every value typed or read back
  (`'…'`). That covers a phone's number and its picker's text, the pinned file's name,
  and an uploader's own error text. The runner's redaction (`redact_detail`) therefore
  removes them before a failure's metadata is stored.
  `tests/browser/test_custom_widgets_round10.py` also checks the source: in the browser
  modules, no f-string interpolates a typed or read-back value unquoted.

Mock scenarios `autofill-upload`, `custom-uploader`, `linkedin-autofill` and
`react-controlled` (`tests/browser/MOCK_ATS.md`) reproduce these pages; the tests are
`tests/browser/test_uploads*.py`, including a preparation-only run of the real runner on
`autofill-upload` that ends at `preparation.ready` with our values and the resume attached.

## Dialog wizards, embedded forms and step navigation

Sign-in-gated boards (LinkedIn Easy Apply, Wellfound, Indeed, iCIMS) run in the person's
Chrome through OpenCLI and put the application in a modal wizard. The same generic
runtime handles them, with no site adapter:

- **Reaching the form.** `open` takes the posting's own way onwards, at most once per
  entry: an application page embedded in an iframe (below), an apply link, or an apply
  control. "Easy Apply", "Quick apply", "I'm interested", "Start your application",
  "Apply without an account" and "Continue as guest" always lead to an application and are
  never submit controls; a toggle (a search filter pill that also reads "Easy Apply") is
  never an apply control. A control that submits a form is clicked only when that form
  sends nothing typed (at most one question, every typed field empty and optional, such as
  a job-alert email box, no file or password input). A posting whose only ways onwards
  are clicks settles once first, so an application frame injected after load is followed
  instead. After a click the runtime waits, within the settle timeout, until the page is
  no longer that posting: a dialog opening, a new document, or a client-side route that
  lands 6 to 10 s later (Dayforce). Links are taken once, compared without query strings
  (LinkedIn adds a tracking id per load). An anchor in a form whose `href` is `#`, empty or
  `javascript:` is that form's button, classified by its wording (JazzHR's "Submit
  Application"). Fields with no submit or next control of their own, on a page that
  offers an apply link or control, are not an application (a job page's message box).
  How the form was reached is added to the inspection message.
- **The dialog is the form.** A visible `role=dialog`/`alertdialog`/`dialog` whose own
  controls make an application step with a Next/Continue/Review/Submit control, and that
  looks like an application (two or more questions, a step or progress indicator, a file
  field, or application wording in its name), is the application form. Only its controls,
  buttons and alerts count; the page behind is ignored. Sign-in and account dialogs never
  qualify. Inside it a plain "Apply" is the dialog's own submit (a one-note slide-in).
  Without an application dialog, a visible dialog (a banner, a sign-up or chat prompt)
  never contributes fields or step actions to the page's form. A step worded like an
  autofill offer ("Import from LinkedIn or fill out this form") is still the application.
  The autofill decline never acts in the application dialog, in a dialog around it, or in
  one holding a question the form binds. It never uses a decline control that submits a
  form (a `<form method="dialog">` only closes its dialog) or that loads another document.
  "Skip" is never a step action, so only `advance` moves such a step on, through its
  Continue. Menus inside the
  application dialog are probed like any form's; menus of a dialog nested in it (a phone
  picker's popup) are not. Native validity is read from the dialog's own controls.
- **Steps.** Next, Continue and Review advance; the step whose primary action submits
  ("Submit application") is final, so prepare-only stops there with
  `preparation.ready`. "Save", "Dismiss", "Download", "Show more" and "Paste resume" are
  never step actions. `ApplicationForm.step` comes from "Step N of M" (N-1) or an
  `aria-current` list; a wizard that shows only a percentage bar (LinkedIn: 0, 33, 67,
  100) is identified by that percentage, so a step keeps one identity on every run and a
  Next that did not move the bar did not advance. A step rendered in place gets up to
  3 s to replace the one that was left before it counts as shown again.
- **Pre-filled values.** A text field the site filled from the person's profile is left
  as it is when it already says the answer: exactly, an email in another case, or a phone
  with the same digits or only the national part of ours. A differing value, or any value
  the site marks invalid, is overwritten. A select already on the answer is not touched.
- **Resume step without upload.** Resumes the site keeps are offered as cards (radios
  labelled "Select resume Avery_Quill_Resume.pdf"). They are folded into the resume field,
  never asked as a question. The card named like the pinned resume file is chosen, or the
  only card; the selection is read back (exactly that card checked). OpenCLI's Browser
  Bridge refuses `upload` (`OpenCliConfig.attach_files` is off), so in a dialog wizard with
  no usable card the resume field becomes a required control for the person, with "Attach
  your resume in the browser window" (and the pinned file name once known) in its wording.
  The runner's existing user-action path asks for it (`UNSUPPORTED_CONTROL`); OpenCLI keeps
  the tab open for the person while that attachment is pending. The field is required
  even when the site's input is not marked required, since a preselected other resume
  would otherwise go with the application. A page form's file field over OpenCLI is still
  tried once and reports what the person can do, as before.
- **Embedded application pages.** A page with no application form but one iframe showing
  an application page on a known ATS host (Greenhouse `embed/job_app`, Lever, Workday,
  iCIMS, Jobvite; or the ATS's own `grnhse_iframe`/`icims_content_iframe` id on the page's
  origin) is a job description whose form is that page, visible or not (careers pages keep
  it in a hidden "Application" tab). `open` goes to the frame's `src` like an apply link,
  before clicking anything. When that page does not show a form on its own, a Playwright
  session reopens the careers page, reveals the frame and operates the page inside it.
  `classify` reports the frame's `src` in the message and follows nothing.
- **Shadow roots.** The inspector reads open shadow roots (LinkedIn has rendered Easy Apply
  inside `#interop-outlet`'s shadow root since 2026). Selectors of elements there read
  `host >> inner`, which Playwright resolves, and the fixed read scripts look inside open
  shadow roots only when the document itself has no match. OpenCLI 1.8.6 cannot act there
  (its CSS and role/name locators do not enter shadow roots, and its `state` refs for
  shadow elements are not found by its actions), so an action there is refused with a
  message for the person instead of being attempted.

Mock scenarios `modal-wizard`, `iframe-embed`, `stepper-ambiguous` and
`apply-in-alert-form` (`tests/browser/MOCK_ATS.md`) reproduce these structures; the tests
are `tests/browser/test_wizard*.py`.

## Workday application wizards

Workday runs one careers site per employer tenant (`<tenant>.wdN.myworkdayjobs.com`).
The shapes below were read on four public postings (Salesforce, Material, General Motors,
Zendesk) on 2026-09-24, without typing anything and with LinkedIn hosts blocked.
Salesforce, GM and Zendesk put an account step first; Material goes straight into the
wizard, so its first page (My Information) was read live, menus opened and closed. The
`workday-wizard` mock scenario (`scripts/mock_workday.py`, `tests/browser/MOCK_ATS.md`)
reproduces them.

- **Apply chooser.** The posting's Apply is `a[role=button][data-automation-id=
  adventureButton]` to `<posting>/apply`. Clicked, it opens a `role=dialog` "Start Your
  Application" popup in the page (the URL stays, the posting turns aria-hidden); opened as a
  URL, as `open()` follows it, `<posting>/apply` shows the same three `a[role=button]` routes
  on a page of its own (`applyAdventurePage`): "Autofill with Resume", "Apply Manually" and
  "Use My Last Application". `open()` prefers a manual route ("Apply Manually") over every
  other apply control; the autofill route, which uploads and parses the resume before any
  question is shown, and "Use My Last Application", which copies another application, are
  never followed. The resume is attached later on its own question (My Experience).
  The popup appears only when Apply is clicked, for example by the person. If it is showing
  when the page is inspected, it counts as an offer to autofill like any other (see "Overlays
  and autofill offers" above): it is declined once with its own Close, and none of its
  routes is followed.
- **No LinkedIn traffic.** The same popup embeds an "Apply with LinkedIn" gadget
  (`applywithlinkedin.myworkdaygadgets.com`) that posts to `www.linkedin.com` as soon as the
  popup shows. `PlaywrightSessionFactory` aborts every request to `BLOCKED_HOSTS`
  (`linkedin.com`, `licdn.com`, that gadget host) in every session (`blocked_hosts=` to
  change it). OpenCLI drives the person's own Chrome and cannot block requests; use the
  Playwright path for Workday.
- **Account step.** `<posting>/apply/applyManually` shows, signed out, a progress list
  ("current step 1 of N: Create Account/Sign In") and a Create Account form (Email
  Address, Password, Verify New Password, a privacy-notice checkbox, a `click_filter`
  overlay over a hidden submit), with "Already have an account? Sign In" and "Forgot your
  password?" outside the form. A visible password box makes the page `SIGN_IN_REQUIRED`;
  its message names the tenant and says an account is needed ("A Workday account for
  salesforce (salesforce.wd12.myworkdayjobs.com) is needed to apply ..."; other sites
  that offer "Create Account" get "An account on <host> is needed to apply ..."). The
  runtime never types credentials. A prepare-only run stops as `NEEDS_INPUT` with that
  `USER_ACTION`; `resume APP --act` opens the visible browser, where the person creates
  an account or signs in once per tenant; the persistent profile (`IMX_HOME/browser`)
  keeps the session for later runs as long as the tenant's cookies last. A wait that
  began on a sign-in or CAPTCHA page ends once that page has been gone for two reads, and
  menu controls no probe has seen yet never count as the person's work (waits never
  probe); the page the sign-in leads to is then inspected afresh, its menus probed. A read
  that the person's navigation interrupts ("Execution context was destroyed") is read again
  once the new document has settled; writers still notice such a change through the
  document identity.
- **Honeypot.** Workday's account and apply pages carry `input[name=website]` labelled
  "Enter website. This input is for robots only, do not enter if you're human." in a
  1 x 0.01 px box whose 1 px label counted as visible, so it used to be a WEBSITE field.
  A text box smaller than 2 px is not visible, a text box is never operated through its
  label alone, and a control whose label, placeholder or description says it is for
  robots (or asks humans to leave it blank) is never a field.
- **One document, many steps.** After the account step the wizard (My Information, My
  Experience, Application Questions (GM splits it in two), Voluntary Disclosures, Self
  Identify on some tenants, Review) runs in one document whose URL never changes; the
  document keeps the posting's JSON-LD, so every step carries the job identity. The step
  comes from the progress list's screen-reader labels: "current step N of M" wins over
  any other "step N of M" text ("completed step 1 of 7" comes first), and the number may
  run straight into the step name ("current step 1 of 6My Information"). A changed step
  number means the wizard advanced even when the next page reuses field ids. Probed menus
  and restored uploads belong to the page they were observed on (the document and the
  step it shows), so each step gets its own probing budget. Workday draws the progress
  list and "Next" / "Save and Continue" before the step's questions: a step with no
  question whose primary action is not the final one is not ready yet (only Review is
  empty), in `open()`, `advance()` and after a wait for the person. "Submit" on Review is
  the final action; prepare-only reads Review and stops with `preparation.ready`, Submit
  is never clicked. An alert that only announces a page ("My Information page is
  loaded") is not a validation error.
- **Required errors.** A step shown again after "Save and Continue" (the mock: an alert
  banner "Errors Found (n)" with one line per field, and per field an `aria-invalid`
  control described by its "Error: ..." message) is `advanced=False` with those messages;
  the field's `validation_error` carries its own message and the runner asks again.
- **Dropdowns.** `button[type=button][aria-haspopup=listbox]` without a role is a menu
  control. Opened, it sets `aria-expanded` and names a body-portal `ul[role=listbox]` in
  `aria-controls` (a new short id such as "cq4q3" each time: such ids constrain nothing,
  the reference and the option set do), moves focus into it, and closes on Escape or an
  outside press. Options are `li[role=option]` with an opaque `data-value`; the first is
  a disabled "Select One" with an empty value, which is not an option. Country lists 249
  countries and is read in full. Its question is its `<label for>`; it has no
  `aria-required`, so a menu button whose accessible name ends in "Required" (Workday:
  "Country United States of America Required") or whose label shows an asterisk, even an
  aria-hidden one, is required. A zero-size text input beside each button holds the value
  and is never a field.
- **Prompts (pickers).** "How Did You Hear About Us?" and "Country Phone Code" are search
  boxes with no role, `aria-haspopup`, `aria-controls` or `aria-expanded`; each is
  described by a hidden count ("0 items selected", "1 item selected, United States of
  America (+1)"; "Expanded" while open, "Minimized" after) and has `enterkeyhint=search`;
  chosen items are `role=option` pills in a `ul[role=listbox][aria-label="items
  selected"]` beside it ("United States of America (+1), press delete to clear value.").
  Such a picker's list is the one visible listbox no control names and no picker shows
  its chosen items in, while its search box (or the list) has focus: a body-portal
  `div[role=listbox][aria-label="Options Expanded"]` of categories (a chevron each; the
  Country Phone Code list is flat, a radio per row). Option names drop the state their
  `aria-label` adds ("Career Websites not checked"). The list closes on an outside press,
  not on Escape. The chosen-items list and an open list are part of the picker, never
  questions, and radios or checkboxes inside an open list are never fields. Unprobed, a
  picker is `UNSUPPORTED` (typing alone commits nothing); probed, it is a `TYPEAHEAD` for
  one value: a picker whose one chosen item already matches the answer is left alone
  ("United States" matches "United States of America (+1)": a trailing dial code is not
  part of a place); otherwise the answer is typed, Enter is pressed once when the list
  shows no results (only where Enter cannot submit a form), the one matching result is
  clicked unless it is already chosen, the list is closed the way the probe closed it,
  and the picker must then show exactly that item; a result it already marks chosen is not
  clicked, and the search typed to find it is cleared again.
- **Dates.** Month / Day / Year spinbutton inputs in one wrapper (Self Identify) are one
  `TEXT` field (`input_type` `date`, placeholder "MM/DD/YYYY") named by its label. The
  answer (ISO "2026-09-24", "09/24/2026", "9/24/2026" or "September 24, 2026") is typed
  into the first segment in the widget's order, "09/24/2026", as a person does; a widget
  that does not move on by itself is typed segment by segment. Each segment is read back
  as a number. (Mock only: no live date could be reached.)
- **Phones and names.** "Country Phone Code" is a `COUNTRY` question, "Phone Device Type"
  a choice of its own and "Phone Extension" a short text, never the number. A phone number
  box right after a country-code picker gets the national number: "+1 (303) 555-0142" is
  typed as "(303) 555-0142" when the picker holds "+1". "Middle Name"
  (`legalName--middleName`) and "Address Line 2" are short texts, never the full name or
  the street address.
- **Not yet observed live.** The pages after My Information (My Experience, Application
  Questions, Voluntary Disclosures, Self Identify, Review), the error banner, search
  results, the uploader and the date widget are reproduced from Workday's published
  conventions, not from a live page (reaching them needs typing or an account). The first
  `resume --act` run on a real tenant is the check; `docs/application-schemas/workday.json`
  is not updated.
