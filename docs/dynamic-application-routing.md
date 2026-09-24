# Dynamic application routing (prepare-only)

The AI package adds semantic interpretation to freshly inspected application fields and supplements the existing factual packet resolver. It has no browser actions, JavaScript execution, URL navigation, file selection or submission capability. The integration must stop at the final review step with submission disabled. A completed draft is **not submitted** and is not an application receipt.

```python
from pathlib import Path
from interviewmaxxing_browser.ai import build_ai_runtime

router, resolver = build_ai_runtime(
    env_file=Path("/absolute/worktree/env.local"),
    writer_model="anthropic/claude-opus-5.5",
)
# On every fresh canonical inspection, before packet resolution:
annotated = router.annotate(form, document_id=current_document_identity,
                            schema_hints=current_schema_prior)
# Build the canonical PacketContext using annotated, then:
packet = await resolver.resolve(context)
# Canonical executor/runner owns filling, reinspection and final-review stopping.
metadata = router.decisions.budget.metadata()
```

`annotate` is synchronous; an async browser integration should run it with `asyncio.to_thread`. `resolve` is async and moves its provider work to a thread. The factory reads only `OPENROUTER_API_KEY` through the existing selection credential loader with an explicit env-file path. Neither receipts nor exceptions expose the key or raw profile values.

## Full-form routes, binding and provenance

- `classify_form(form, document_id=..., schema_hints=...)` returns a typed `FormRouteReport`; `annotate` preserves the existing interface and `report_for(form)` is a non-provider lookup for the exact current inspection. Every field is evaluated, including native email and known identity controls. Decisions expose route, semantic type, prose need, source applicability, and their separate confidences/probabilities. FILE controls add attachment-versus-autofill purpose.
- The router receives the actual `ApplicationForm` from the generic DOM inspector. It only updates `semantic_type` for custom/unknown fields. It preserves IDs, selectors, option values, disabled state, constraints, scope and inspection time. Known sensitive semantic types are never downgraded.
- Page wording and schema priors appear only in decision state, not trusted instructions. A schema prior cannot add a field or select a locator. The entire current form binding, document identity, schema contents, prompt version and model contribute to the request/cache identity. A cache entry contains typed decisions; applying it still starts with the current form.
- Annotation does not establish document freshness. The caller must re-inspect after DOM changes and retain the canonical stale-packet, per-field binding, file verification and page-document checks. The existing canonical form fingerprint excludes semantic metadata (also some changing constraints); the AI hash includes all current binding metadata. Neither hash authorizes an action by itself.
- The factual resolver proposes answers first. Exact known identity/current values are released only when the whole-form gate confirms the requested subject is the applicant and the current profile source applies. An email input does not establish whose email is requested: supervisor, reference, employer and historical questions cannot use the applicant profile shortcut. A Jev choice may identify only an offered verified fact. Its value is then read from the candidate object and translated through existing typed value/option validation. Jev never generates the copied value. Conflicting same-key facts, unavailable facts and mismatched/disabled options hold. Unverified facts are excluded from provider requests.
- Existing scoped user inputs and matching saved answers retain precedence, including unusable saved answers. Explicit consent, attestation, eligibility, salary and protected attributes cannot enter AI supplementation. Unknown classifications, missing facts, unsupported/file controls and provider failures retain missing inputs. Approved document attachment fields remain the canonical resolver/executor's responsibility. Resume parser/autofill controls are held because their side effects can overwrite other answers; they are not equivalent to attachments.
- Every returned packet passes `PacketContext.problems`, preserving source IDs, candidate/job scope, required-field coverage and control compatibility. Model output cannot supply an arbitrary candidate ID, file path or browser command.

## Narrative escalation

Jev classifies and selects facts; it never authors prose. `COPY_KNOWN` describes responsibility, not ready-to-fill status. Readiness requires an actual verified source and canonical validation. `EXPLICIT_ANSWER`, unclear or low-confidence source applicability blocks both copy and writing unless an exact scoped user/saved answer already resolves the question. Demographic answers require an explicit verified saved answer; identity never implies them.

A narrow identity clarification handles near-threshold current-applicant source scores: it requires an existing canonical identity answer, high-confidence literal `COPY_KNOWN`, and a leading `APPLICANT_CURRENT` score and confidence of at least 0.90. One source-only Jev call receives the full fresh form context, not the candidate value. It must independently reach 0.95 before the local value is released. Historical, other-person and explicit-answer scopes remain held. For LinkedIn, GitHub and personal-website URL fields only, near-threshold probability mass that merely moved from current to historical is accepted without the extra call when the combined applicant mass is at least 0.95 and at most 0.01 remains on any other scope: a profile URL identifies the applicant in either timeframe, while another person's URL or an explicit answer still holds.

For prose, the optional [RAG runtime](rag-writing.md) retrieves up to eight current verified candidate facts using the question and the exact job description. It retrieves employer evidence only for the candidate and normalized application URL, with a separate namespace for optional style samples. Without RAG, Jev selects relevant verified facts. Work history does not establish motivation or personal intent. Before writing, consistency checks include canonical counterevidence outside the retrieved set, including differently named keys for the same subject. Generic experience collections may contain different compatible employers, budgets and team sizes.

The configured Opus writer receives those candidate facts, scoped job evidence, style samples, the question and length limit. It receives no browser bindings. Personal claims cite candidate facts; employer claims cite job evidence; style samples cannot establish facts. Opus returns a strict JSON object with `status`, paragraph-aware `sentences` and `missing_information`. `READY` requires supported sentences and no missing information. `NEEDS_INPUT` names the missing details and contains no draft sentences. For a broad summary, relevant experience is enough without requiring a complete life history. Unknown or cross-namespace citations, extra schema fields, overlong text, incomplete responses, tool calls and unexpected model IDs are rejected.

Jev checks each sentence against its cited sources and checks completeness against the whole question. Support probabilities at least 0.95 pass; probabilities at most 0.05 hold. Intermediate narrative consistency or grounding results receive an independent structured Opus review, which must return `SUPPORTED`. Conflicts, incomplete answers and unsupported claims remain held with specific issues. Consistency review sees all current verified candidate facts; draft review checks the actual cited sentences and scoped job evidence. Reviews use the same provider budget. Missing ABM platform history is never inferred from general B2B experience.

Structured draft-review `UNSUPPORTED` or `INCOMPLETE` results may receive one corrective rewrite with the same evidence and bounded review feedback. Both the failed and corrected attempts stay in the private trace. The corrected draft must pass fresh Jev and Opus checks. `NEEDS_INPUT`, conflicts, invalid citations, malformed/provider failures and exhausted budgets do not retry; a second rejected draft stays unresolved.

Semantic classification and routing require confidence >= 0.90 and selected probability >= 0.95. These checks remain probabilistic; they are not proof that arbitrary prose is true. An uncertain prose responsibility can escalate to WRITER, but source/sufficiency/grounding gates still apply. Packet confidence is the conservative minimum of applicable model gates and support scores; it is not calibrated probability of truth. An unsupported sentence becomes a user-input hold, and all real work remains prepare-only for review.

## Bounds and observations

Defaults share one per-runtime budget: 32 provider calls, USD 0.50 of conservative reservations and 60,000 request bytes. Jev uses a 15-second timeout; the writer uses a 90-second timeout and at most 3,000 output tokens. Strong review allows at most 1,200 output tokens. Runtime embedding requests reserve and record costs in the same budget before HTTP. Each provider call has one attempt. The router batches the whole form (default 16, configurable 8/16/32 in the historical benchmark), recursively splits oversized requests without dropping context, and handles at most 100 fields, fact routing at most 40 verified facts per bounded comparison, and the writer at most eight relevant facts. Exceeding a bound holds; it never silently truncates candidate evidence or retries indefinitely.

Reservations use UTF-8 request bytes plus framing as a conservative input-token allowance and the writer's output-token cap, with the verified model prices below. Failed/unknown-cost calls retain their reservation. Receipts separately record requested/resolved model, purpose, actual provider latency, reported cost, reservation and status. Aggregate cost stays null when any call has unknown cost; a known subtotal and unknown-cost call count are reported separately. Reservations are a client-side estimate, not a provider-enforced billing limit. Browser latency is not included. The bounded in-memory decision cache holds at most 128 entries, never persists raw private profile values, and is not shared across processes. Durable application/resume/duplicate state remains the canonical store's responsibility.

Official contracts checked September 23, 2026:

- [OpenRouter Jev 1.13](https://openrouter.ai/typesafe/jev-1.13/api) describes the typed decision model, priced at USD 0.042 per million input tokens. The existing selection client correctly uses `POST /api/alpha/decisions` with `{model,state,questions}`. The [official provider SDK changelog](https://github.com/OpenRouterTeam/ai-sdk-provider/blob/main/CHANGELOG.md) also identifies that endpoint.
- The [OpenRouter Jev guide](https://openrouter.ai/docs/guides/community/jev), [TypeSafe confidence](https://docs.typesafe.ai/confidence) and [Noul primitive](https://docs.typesafe.ai/primitives/noul) describe the decision contract and score semantics.
- [TypeSafe Choice](https://docs.typesafe.ai/primitives/choice) defines choices, criteria, confidence and probabilities. Jev is not sent to chat completions and need not appear in the ordinary chat-model catalog.
- The [live OpenRouter model registry](https://openrouter.ai/api/v1/models) included exact `anthropic/claude-opus-5.5`, structured-output support and USD 4 / 20 per million input/output tokens. The writer uses [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs), no alternate-model list and no provider fallback.

## Historical v6 verification receipt and limits

The following frozen results predate the RAG and strong-review additions described above. Current RAG evaluation receipts are kept separately under `.imx/rag-writing/`; they do not replace or change these historical measurements.

The frozen source is `full-form-routing-v6`; exact file hashes and configuration are in `.imx/dynamic-applications/astra/frozen-v6.json`. Thirty-five targeted unit tests pass, with Ruff and strict mypy clean. Coverage includes current versus supervisor/history sources, explicit writer scope, saved-answer scope mismatch, autofill versus attachment, conflicting writer evidence, unsupported claims despite valid citation IDs, conservative confidence, missing costs and stale binding/cache contexts.

The independent v6 synthetic source-confusion challenge produced zero unsafe actual copies across 17 cases and correctly identified both narrative questions. Only two of four intended known-copy positive controls were ready: conservative confidence held first-name/email controls. This is a safety observation on a small synthetic suite, not a completion guarantee.

Final real-form accuracy is scored independently from frozen v009 heldout inputs. The corpus contains 509 unique real questions, 79 real narratives and 28 observed backends, plus 52 separately identified synthetic cases. The mapper's broader backend inventory is a separate artifact. Full-form timing includes semantic routing and readiness checks; provider call latency and browser navigation/inspection latency are separate measurements. Development runs compare batches 8/16/32 with two cold repetitions; v4 results are superseded by the source/purpose gates. The v5 8/16/32 comparison selected batch 16; v6 keeps those classifier thresholds and changes only narrative sufficiency, with separate v6 confirmation and heldout timing.

Independent v6 scoring preserves responsibility and execution readiness separately:

| Frozen split | Unique questions | Actual known-copy readiness | Real narrative responsibility recall | Unsafe actual copies |
| --- | ---: | ---: | ---: | ---: |
| Development | 441 | 66/119 (55.5%) | 58/62 (93.5%) | 0 |
| Blind heldout | 120 (109 real, 11 synthetic) | 10/21 (47.6%); real 10/20 (50%) | 15/17 (88.2%) | 0 |

The heldout pre-frozen substantive narrative subset was 12/12; synthetic narrative recall was 2/2. Two heldout narrative labels (“Headline” and “How technical are you”) were conservatively ambiguous. No narrative was routed to copying. The zero-unsafe result is an observed sample outcome, not a guarantee. Many valid basic fields still hold; the implementation does not claim full known-fact readiness. Confusion matrices and denominators are in `evaluation/v009-{development,heldout}-frozen-v6.score.json`.

The heldout run covered 310 observations across 30 full forms: median 0.409 seconds, p95 1.294 seconds, 18.17 fields/second. Its 43 provider calls reported USD 0.017690652 known cost plus one unknown-cost malformed response, so total cost is null. Development had one malformed call among 117; heldout had one among 43. These failures safely held affected fields without retry. Two unchanged development-only replays passed (31,641-byte request, 23 questions and 23 valid answers), so no deterministic client/size bug was reproduced. The original malformed response detail was not retained; a transient cause is not proven. Frozen metrics were not replaced with replay results.

The isolated localhost fixture uses public employer wording with fictional approved facts and the actual runtime with submission disabled. Under v6, AutoRaptor's “Tell us about your experience” produced a `GENERATED_FROM_FACTS` packet on the first attempt, with no canonical problems and observed text filled locally. Sentence grounding scores were 0.97/0.96/0.96; packet confidence was 0.96. Exact Opus 5.5 took 4.135 seconds and reported USD 0.007816; the entire fixture took 5.029 seconds. This is one observed success, not a latency guarantee.

A separate Canals motivation question with only fictional work history stayed blank: the source gate held it, and a direct Opus sufficiency probe independently returned `NEEDS_INPUT` (5.321 seconds, USD 0.008688). Both localhost fixtures verified `submit_dispatched=false` and zero submission events. These are synthetic candidate/fact tests using real public wording, not real candidate applications. Receipts/screenshots are in `.imx/dynamic-applications/astra/grounded-fixture/` and `unknown-intent-fixture/`.

The earlier v5 complete fictional examples were conservatively held by an overly broad Jev coverage question before writing. Those development-only failures led to the separately frozen v6 typed writer sufficiency contract; no blind labels or outcomes were used to change the implementation. Earlier component-only Opus acceptance is recorded separately in `writer-component-receipt.json`.

The root integration owns browser lease safety, fresh document/control/action reinspection, final-review stopping and broader runtime regressions. AI-module and offline routing checks alone do not establish complete application execution.
