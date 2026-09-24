"use client";

import { useState, type FormEvent } from "react";
import type { AnswerValue, InputRequestView, RequiredQuestionView } from "@/lib/service/types";
import { validateAnswers, type FieldErrors } from "@/lib/validation";
import { formatClock } from "@/lib/format";
import {
  LOOKUP_OTHER,
  lookupAnswer,
  lookupSelectValue,
  lookupSuggestions,
  startsWithOtherValue,
} from "@/lib/preparation";
import type { DeskActions } from "./ApplicationDesk";
import { ErrorSummary } from "./ErrorSummary";
import { FieldMessages, RequirementTag, describedBy } from "./fields";

type QuestionsNeeds = Extract<InputRequestView, { kind: "questions" }>;

function initialValue(question: RequiredQuestionView): AnswerValue {
  if (question.value !== null && question.value !== undefined) return question.value;
  return question.control === "multi_select" ? [] : question.control === "boolean" ? null : "";
}

export function QuestionsForm({ needs, actions }: { needs: QuestionsNeeds; actions: DeskActions }) {
  const [answers, setAnswers] = useState<Record<string, AnswerValue>>(() =>
    Object.fromEntries(needs.questions.map((question) => [question.id, initialValue(question)])),
  );
  const [accepted, setAccepted] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(needs.attestations.map((attestation) => [attestation.id, attestation.accepted])),
  );
  // Lookup questions whose "Enter a different value…" text box is in use.
  const [lookupOther, setLookupOther] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(
      needs.questions.filter((question) => startsWithOtherValue(question)).map((question) => [question.id, true]),
    ),
  );
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [attempt, setAttempt] = useState(0);
  const [pending, setPending] = useState<"continue" | "save" | null>(null);
  const [edited, setEdited] = useState<Set<string>>(new Set());

  // Service errors stay visible until the user edits that answer.
  const serviceErrors = Object.fromEntries(Object.entries(needs.errors).filter(([id]) => !edited.has(id)));
  const errors: FieldErrors = { ...serviceErrors, ...clientErrors };
  const order = [...needs.questions.map((question) => question.id), ...needs.attestations.map((item) => item.id)];
  const summary = order
    .filter((id) => errors[id])
    // A different lookup value is typed into its own box, so the summary links there.
    .map((id) => ({ fieldId: lookupOther[id] ? `${id}-other` : id, message: errors[id] }));
  const requiredCount = needs.questions.filter((question) => question.required).length;

  function update(id: string, value: AnswerValue) {
    setAnswers((current) => ({ ...current, [id]: value }));
    setEdited((current) => new Set(current).add(id));
    setClientErrors(({ [id]: _removed, ...rest }) => rest);
  }

  function chooseLookup(id: string, other: boolean, value: AnswerValue) {
    setLookupOther((current) => ({ ...current, [id]: other }));
    update(id, value);
  }

  async function submit(mode: "continue" | "save") {
    const found = validateAnswers(
      needs.questions,
      needs.attestations,
      answers,
      accepted,
      mode === "continue",
      lookupOther,
    );
    setClientErrors(found);
    setAttempt((count) => count + 1);
    if (Object.keys(found).length > 0) return;
    setPending(mode);
    setEdited(new Set());
    const sent = Object.fromEntries(Object.entries(answers).map(([id, value]) => [id, lookupAnswer(value)]));
    const input = { answers: sent, attestations: accepted };
    if (mode === "continue") await actions.answerAndContinue(input);
    else await actions.answer(input);
    setPending(null);
    setAttempt((count) => count + 1);
  }

  return (
    <form
      className="questions"
      method="post"
      noValidate
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        void submit("continue");
      }}
      aria-describedby="questions-intro"
    >
      <p id="questions-intro" className="lede">
        {`The site asks ${needs.questions.length} ${needs.questions.length === 1 ? "question" : "questions"} (${requiredCount} required) that your saved profile doesn’t answer.`}{" "}
        Nothing here is guessed. Answer them and the desk picks up where it paused.
      </p>

      <ErrorSummary
        title={summary.length === 1 ? "One answer needs attention" : `${summary.length} answers need attention`}
        items={summary}
        attempt={attempt}
      />

      <ol className="questions__list">
        {needs.questions.map((question, index) => (
          <li key={question.id} className={`question${errors[question.id] ? " is-invalid" : ""}`}>
            <span className="question__number" aria-hidden="true">
              {String(index + 1).padStart(2, "0")}
            </span>
            {lookupSuggestions(question).length > 0 ? (
              <LookupControl
                question={question}
                value={answers[question.id]}
                other={lookupOther[question.id] === true}
                error={errors[question.id]}
                onChoose={(other, value) => chooseLookup(question.id, other, value)}
              />
            ) : (
              <QuestionControl
                question={question}
                value={answers[question.id]}
                error={errors[question.id]}
                onChange={(value) => update(question.id, value)}
              />
            )}
          </li>
        ))}
      </ol>

      {needs.attestations.length > 0 && (
        <fieldset className="attestations">
          <legend className="attestations__legend">Statements from the site</legend>
          <p className="field__hint">Check a statement only if it&rsquo;s true for you. None are checked for you.</p>
          {needs.attestations.map((attestation) => (
            <div key={attestation.id} className={`attestation${errors[attestation.id] ? " is-invalid" : ""}`}>
              <input
                id={attestation.id}
                type="checkbox"
                checked={accepted[attestation.id] ?? false}
                aria-invalid={errors[attestation.id] ? true : undefined}
                aria-describedby={describedBy(attestation.id, null, errors[attestation.id])}
                onChange={(event) => {
                  const checked = event.target.checked;
                  setAccepted((current) => ({ ...current, [attestation.id]: checked }));
                  setClientErrors(({ [attestation.id]: _removed, ...rest }) => rest);
                }}
              />
              <label htmlFor={attestation.id}>
                <span className="attestation__statement">{attestation.statement}</span>
                <RequirementTag required={attestation.required} />
              </label>
              <FieldMessages id={attestation.id} error={errors[attestation.id]} />
            </div>
          ))}
        </fieldset>
      )}

      <div className="questions__actions">
        <button type="submit" className="button button--primary" disabled={pending !== null}>
          {pending === "continue" ? "Saving and continuing…" : "Save answers and continue"}
          <span aria-hidden="true" className="button__arrow">
            →
          </span>
        </button>
        <button
          type="button"
          className="button button--secondary"
          disabled={pending !== null}
          onClick={() => void submit("save")}
        >
          {pending === "save" ? "Saving…" : "Save for later"}
        </button>
        <p className="questions__saved" role="status">
          {needs.savedAt
            ? `Saved at ${formatClock(needs.savedAt)}. The application stays paused until you continue.`
            : ""}
        </p>
      </div>
    </form>
  );
}

function QuestionControl({
  question,
  value,
  error,
  onChange,
}: {
  question: RequiredQuestionView;
  value: AnswerValue;
  error?: string;
  onChange: (value: AnswerValue) => void;
}) {
  const { id, help } = question;
  const hint = [
    help,
    question.lookup ? "The site looks this up as you type. Your answer is typed into its search box exactly." : null,
    question.reason ? `Asked because: ${question.reason}` : null,
  ]
    .filter(Boolean)
    .join(" ");
  const describe = describedBy(id, hint, error);
  // A lookup without suggestions is always a text box, whatever control the site reported.
  const choice =
    !question.lookup &&
    (question.control === "boolean" || question.control === "single_select" || question.control === "multi_select");

  if (choice) {
    const options =
      question.control === "boolean"
        ? [
            { value: "yes", label: "Yes" },
            { value: "no", label: "No" },
          ]
        : (question.options ?? []);
    const multi = question.control === "multi_select";
    const selected = (option: string) =>
      multi
        ? Array.isArray(value) && value.includes(option)
        : question.control === "boolean"
          ? (value === true && option === "yes") || (value === false && option === "no")
          : value === option;

    if (!multi && options.length > 6) {
      return (
        <div className="field">
          <label htmlFor={id} className="field__label question__label">
            {question.label} <RequirementTag required={question.required} />
          </label>
          <select
            id={id}
            className="input"
            value={typeof value === "string" ? value : ""}
            required={question.required}
            aria-invalid={error ? true : undefined}
            aria-describedby={describe}
            onChange={(event) => onChange(event.target.value)}
          >
            <option value="">Choose…</option>
            {options.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <FieldMessages id={id} hint={hint} error={error} />
        </div>
      );
    }

    return (
      <fieldset id={id} className="field choice-group" aria-describedby={describe}>
        <legend className="field__label question__label">
          {question.label} <RequirementTag required={question.required} />
        </legend>
        <div className={`choices${options.length <= 3 ? " choices--inline" : ""}`}>
          {options.map((option) => (
            <label key={option.value} className="choice">
              <input
                type={multi ? "checkbox" : "radio"}
                name={id}
                value={option.value}
                required={question.required && !multi}
                aria-invalid={error ? true : undefined}
                checked={selected(option.value)}
                onChange={(event) => {
                  if (multi) {
                    const current = Array.isArray(value) ? value : [];
                    onChange(
                      event.target.checked
                        ? [...current, option.value]
                        : current.filter((item) => item !== option.value),
                    );
                  } else if (question.control === "boolean") {
                    onChange(option.value === "yes");
                  } else {
                    onChange(option.value);
                  }
                }}
              />
              <span>{option.label}</span>
            </label>
          ))}
        </div>
        <FieldMessages id={id} hint={hint} error={error} />
      </fieldset>
    );
  }

  const text = typeof value === "string" ? value : "";
  const common = {
    id,
    name: id,
    className: "input",
    value: text,
    required: question.required,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": describe,
  } as const;

  return (
    <div className="field">
      <label htmlFor={id} className="field__label question__label">
        {question.label} <RequirementTag required={question.required} />
      </label>
      {question.control === "long_text" ? (
        <>
          <textarea {...common} rows={5} onChange={(event) => onChange(event.target.value)} />
          {question.maxLength && (
            <p className={`counter${text.length > question.maxLength ? " is-over" : ""}`}>
              {text.length} / {question.maxLength}
            </p>
          )}
        </>
      ) : (
        <input
          {...common}
          type={question.lookup || question.control === "number" || question.control === "text" ? "text" : question.control}
          inputMode={question.control === "number" ? "numeric" : undefined}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      <FieldMessages id={id} hint={hint} error={error} />
    </div>
  );
}

/**
 * A site lookup with suggestions: always a select listing what the site offered
 * for the typed value, plus "Enter a different value…" for anything else. The
 * answer is the chosen suggestion or the typed text, never the menu sentinel.
 */
function LookupControl({
  question,
  value,
  other,
  error,
  onChoose,
}: {
  question: RequiredQuestionView;
  value: AnswerValue;
  other: boolean;
  error?: string;
  onChoose: (other: boolean, value: AnswerValue) => void;
}) {
  const { id, help } = question;
  const suggestions = lookupSuggestions(question);
  // Remember the typed value while a suggestion is chosen, in case the person switches back.
  const [typed, setTyped] = useState(() => (other && typeof value === "string" ? value : ""));
  const hint = [
    help,
    "These are the site's suggestions for what was typed. If none is right, choose “Enter a different value…” and type it: it goes into the site's search box exactly as written.",
    question.reason ? `Asked because: ${question.reason}` : null,
  ]
    .filter(Boolean)
    .join(" ");
  const text = other && typeof value === "string" ? value : typed;
  const otherId = `${id}-other`;
  const errorId = error ? `${id}-error` : null;

  return (
    <div className="field lookup">
      <label htmlFor={id} className="field__label question__label">
        {question.label} <RequirementTag required={question.required} />
      </label>
      <select
        id={id}
        name={id}
        className="input"
        value={lookupSelectValue(question, value, other)}
        required={question.required}
        aria-invalid={error && !other ? true : undefined}
        aria-describedby={[`${id}-hint`, other ? null : errorId].filter(Boolean).join(" ")}
        onChange={(event) => {
          const next = event.target.value;
          if (next === LOOKUP_OTHER) onChoose(true, typed);
          else onChoose(false, next);
        }}
      >
        <option value="">Choose…</option>
        {suggestions.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
        <option value={LOOKUP_OTHER}>Enter a different value…</option>
      </select>
      <FieldMessages id={id} hint={hint} />
      {other && (
        <div className="field lookup__other">
          <label htmlFor={otherId} className="field__label">
            Different value for the site&rsquo;s search box
          </label>
          <input
            id={otherId}
            name={otherId}
            className="input"
            type="text"
            autoComplete="off"
            value={text}
            required={question.required}
            aria-invalid={error ? true : undefined}
            aria-describedby={[`${otherId}-hint`, errorId].filter(Boolean).join(" ")}
            onChange={(event) => {
              setTyped(event.target.value);
              onChoose(true, event.target.value);
            }}
          />
          <p id={`${otherId}-hint`} className="field__hint">
            Typed into the site&rsquo;s search box exactly as written.
          </p>
        </div>
      )}
      <FieldMessages id={id} error={error} />
    </div>
  );
}
