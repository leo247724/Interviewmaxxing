# interviewmaxxing-generation

Resolves one inspected application form step into an `ApplicationPacket`
(CONTRACTS.md §4). Answers come only from the user's inputs, saved answers bound to
the exact question, the supplied resume, the verified identity and verified facts.
Every answer carries provenance. Each required field without a grounded answer is
returned as a scoped `MissingInput`. The resolver is deterministic and makes no
network, LLM or Jev calls. Its only dependency is `interviewmaxxing-core`.

## Public API

```python
from interviewmaxxing_generation import FactualPacketResolver, PacketResolutionError

resolver = FactualPacketResolver()                  # implements core PacketResolver
resolver = FactualPacketResolver(clock=utc_now)     # clock: () -> aware UTC datetime,
                                                    # used for ApplicationPacket.created_at
packet = await resolver.resolve(context)            # context: interviewmaxxing_core.PacketContext
```

- `FactualPacketResolver(*, clock: Callable[[], datetime] = utc_now)` needs no services.
- `async resolve(context: PacketContext) -> ApplicationPacket`: the result always
  satisfies `context.problems(packet) == []`. The resolver never returns an invalid
  packet. It raises `PacketResolutionError` (`.problems: list[str]`) instead, and that
  error indicates a resolver bug.
- `resolve_packet(context, *, packet_id=None, created_at=None) -> ApplicationPacket` is
  the synchronous equivalent.
- `missing_input_id(form, field) -> str` returns the stable `MissingInput.id` of a
  question.
- `QuestionText`, `wording_key`, `question_key`, `is_neutral_hint`,
  `saved_answer_matches` and `display_question` expose the question-binding and
  display rules below, for UIs and tests.

A packet gets a new `id` on every resolution. Answers are keyed by `field_id`.
`MissingInput.id` is derived from `(form scope, field id, field fingerprint)`. It
stays the same when the same question is re-inspected, including after a resume or
with new selectors or timestamps. It changes when the question's wording, help text,
placeholder or options change.

## Resolution order (per field)

1. **User input** for this exact question (`context.user_inputs`; the latest wins).
   If it no longer fits the field, for example because the field became required, the
   question is asked again.
2. **Saved answer** that `applies_to(job)`, has the field's semantic type (or none) and
   was given for **this exact question wording** (see below). A job-scoped answer
   overrides a global one. Saved answers that disagree give an `AMBIGUOUS` item with
   the valid candidate values.
3. **Explicit-answer types** (`EXPLICIT_ANSWER_REQUIRED`: work authorization,
   sponsorship, salary, consent, attestation, EEO and pronouns) stop here. They are
   never inferred from facts or identity.
4. **Resume** (FILE `RESUME` fields): the supplied file after its digest is rechecked,
   provided it satisfies `accept`.
5. **Verified identity** for `PROFILE_IDENTITY_TYPES`. `LOCATION` is
   `"city, region, country"`.
6. **Verified facts**, looked up by key:
   - `current_company`, `current_title`
   - `education_level` or `highest_education_level`
   - `university`, `degree`
   - years of experience: `years_experience`, `years_experience.total`,
     `years_professional_experience`, or `years_experience.<area>` for a question
     about that exact area

   Facts that disagree are never chosen between. Unverified facts are never used.
7. **Factual free text** (`CUSTOM_TEXT`, `CUSTOM_LONG_TEXT`, `CUSTOM_SELECT`). This
   applies only when the whole question is a direct fact lookup:
   - "What is your current job title?"
   - "Current employer"
   - "What is your current role?" (answered as "`<title>` at `<company>`",
     `GENERATED_FROM_FACTS`)
   - a years-of-experience question

   Motivation, opinion and qualification questions are never drafted.

If none of these apply, an optional field stays blank and a required field becomes
`MissingInput.for_field(...)`. Its reason is one of:

| Reason | When |
| --- | --- |
| `UNSUPPORTED_CONTROL` | the control is unsupported |
| `UNCOVERED_ATTESTATION` | the field is an `ATTESTATION` |
| `EXPLICIT_ANSWER_REQUIRED` | the field has another explicit-answer type |
| `AMBIGUOUS` | the saved data conflicts or maps to more than one option |
| `NO_ANSWER` | anything else |

The prompt shows the complete question (`field.question_text`) and says why no answer
was used.
For example: "is not one of the options", "longer than the 5-character limit" or "the
supplied resume file has changed". It also lists the usable options.

## Question binding

Question text is compared with `wording_key`. Only these differences are ignored:

- case and whitespace;
- sentence punctuation: `. , ; : ! ? ' " ( ) [ ]`;
- a dash standing alone between spaces;
- trailing `*` or `(required)` markers.

Everything that can carry meaning is kept:

- comparison symbols: `< > =`;
- currency symbols: `$ € £`;
- `% + # & / @`;
- attached hyphens;
- punctuation inside numbers, as in `1.5` or `100,000`.

A saved answer's `question` or one of its `match_phrases` must **equal** one of these:

- the field's full question, `field.question_text` (core `render_question`: label,
  help text and placeholder). This is what `UserInput.answering(...)` and
  `to_saved_answer(...)` store, so an answer saved with JOB or GLOBAL reuse answers
  the identical question again;
- the label alone, when there is no help text and the placeholder is a genuinely
  neutral format hint (see below).

A placeholder is neutral only if it is empty, a prompt such as "Select..." or
"Your answer", a date format such as "MM/YYYY", or an example number marked as one,
such as "e.g. 5". A placeholder with any of the following is part of the question:

- a currency or other symbol (`€`, `$`, `%`, `<`);
- a unit or any other word ("years", "per hour", "USD");
- a bare number or scale ("1-5", "100000").

Examples:

- "Will you require visa sponsorship?" does not answer "Will you require
  sponsorship?".
- "Relocate to Austin" does not answer "Relocate to Denver".
- A saved "Yes" to "Have you managed a budget > $100,000?" does not answer "... <
  $100,000?".
- A salary saved for "Expected salary $" does not fill "Expected salary" with
  placeholder "€".
- An "I agree" answer given for one attestation's help text does not answer an "I
  agree" checkbox whose help text states a different attestation.

Match phrases are whole alternative phrasings, never substrings.

Prompts show the complete question through `display_question(field)`, which returns
core's `field.question_text`: label, help text and placeholder, one part per line,
symbols kept verbatim. This is the same text as `MissingInput.label`. Matching does
not depend on display text.

Tied conflicting saved answers kept by the candidate loader (C2) are all visible to
the resolver. For example, two JOB salary answers with the same confirmation time
give an `AMBIGUOUS` item listing both values, never the GLOBAL answer as a fallback.

## Value translation

- **Choices:** an answer selects an actual option that is enabled and has a non-empty
  value. The option is found by its visible **label**, never by comparing the user's
  text with the machine value (`"0"` may mean "No").
  - `True`/`False` map to Yes/True and No/False labels.
  - `COUNTRY` and `STATE` also accept a small table of equivalent spellings, such as
    US ↔ United States of America and OR ↔ Oregon.
  - Years-of-experience facts match numeric-range labels such as "6-10 years", "10+"
    or "Less than 1". A value inside two overlapping ranges is `AMBIGUOUS`.
  - "Yes" never maps to "Yes, a full license".
- **Zero and false:** `0` fills text as `"0"`. `False` is a real answer and is never
  dropped. A saved `False` on a required checkbox is reported and never flipped.
- **Text:** text is never truncated or reformatted. A value that exceeds
  `max_length`, or a non-number for `input_type="number"`, is reported instead.

## Verification

```bash
uv venv --python 3.12 .venv-task
uv pip install --python .venv-task/bin/python -e packages/core -e packages/candidate -e packages/generation pytest ruff mypy
.venv-task/bin/python -m pytest tests/generation
.venv-task/bin/ruff check packages/generation tests/generation
.venv-task/bin/mypy --strict packages/generation/src
```

Fixtures in `tests/fixtures/generation/` are fictional. `candidate_additions.json`
extends the core Avery Example profile. `screening_form.json` covers every edge case
above. The `expected_*_packet.json` files are the golden projections of the resolved
packets.
