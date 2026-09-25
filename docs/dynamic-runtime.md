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
  questions.
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
