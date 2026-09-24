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
  first. A menu the probe cannot observe or close stays `UNSUPPORTED`, even while it
  shows its list; probing stops for that document. A complete static option set becomes a canonical `SELECT` (values and
  labels exactly like a native select); an input whose menu offers nothing until
  something is typed becomes a `TYPEAHEAD`; multi-select menus and anything ambiguous,
  virtualized past 5 scrolls or over 500 options stay `UNSUPPORTED` for the user.
  Observations are cached per document (reused by later inspections without reopening,
  dropped on navigation or context loss); at most 24 probes and 20 s per document, 3 s
  per control. Probing runs before semantic annotation with menus closed again, so it
  never counts as a form change. `observe`/`classify` and waits for the user never probe.
  A menu's displayed value or placeholder ("Select...", "+1") is state, not question
  wording, so fingerprints and saved-answer matching stay stable after filling. A div
  menu's `aria-label` that is its placeholder or its displayed value ("Select") names
  no question; the text around it does.
- **Open menus are not page changes.** A menu, list or dialog that a combobox or picker
  owns belongs to that widget wherever it renders, in a body portal or inside the form
  (Greenhouse). It never adds text to another question, never shifts another element's
  position in a selector, and its buttons, links, headings and live regions are not the
  page's. react-select's hidden required proxy input (`aria-hidden`, `tabindex=-1`,
  rendered only while a required select is empty) is part of its select, not a
  question.
- **Selecting.** A probed menu is opened the recorded way, its listbox re-resolved after
  opening, and the one matching option (freshly derived from the owned listbox) clicked.
  Only when an input menu of more than 20 options does not render that option is it
  filtered by typing. The label is tried first, then its name without a trailing code or
  parenthetical ("United States" of "United States +1": Greenhouse filters on the
  country's name), then its first word. A menu still open after the choice is closed the
  way the probe closed it. Readback:
  the menu closed and the control displays the chosen label. When it displays only a
  suffix of it (a dial-code select shows "+1", which "United States +1" and "Canada +1"
  share), the menu is reopened once and its own selection must name the chosen option;
  the first signal the widget exposes decides: `aria-selected` (exactly one option),
  `aria-activedescendant`, or exactly one option whose class names the selection
  (react-select's `select__option--is-selected`). A confirmed choice keeps naming the
  "+1" display in that document. Anything else is `VERIFICATION_MISMATCH`.
- **Lookups.** A `TYPEAHEAD` answer is typed (about 30 ms per character). Suggestions are
  read from the owned listbox once stable for 400 ms. The wait is at most 6 s, or 3 s
  while nothing appears: Rippling loads its place search on the first query. A shown
  list counts even when the input never exposes `aria-expanded`, as Rippling's location
  input doesn't. Suggestions are matched with
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
  back by digits and by the picker's dial code. The picker's text is its name, its
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
  its uploader group ("Resume/CV").
- **Passing states and own changes.** Before every write the page must still show the
  approved questions, bindings and actions. A difference is waited out for up to 1 s,
  because Greenhouse disables its "Autofill my application" button while it handles a
  keystroke; only a difference that persists stops the fill. After a verified attach,
  the fill continues against the page as it now is, if every other question and binding
  is as approved.
- **Closed postings.** "Job not found", "The job you requested was not found", "posting
  not found", "job does not exist" and "no longer open" (Greenhouse's redirect for a
  closed job) classify `JOB_CLOSED`, as "no longer available" already did.
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
`tests/browser/test_custom_widgets*.py`.
