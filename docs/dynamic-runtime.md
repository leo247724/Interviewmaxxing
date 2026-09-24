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
