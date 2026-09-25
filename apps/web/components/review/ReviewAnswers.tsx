"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { confidenceLabel, reviewDisplay } from "@/lib/preparation";
import {
  REUSE_CHOICES,
  allowedReuse,
  answersSummary,
  badgeKind,
  citationSummary,
  copyableText,
  editProblem,
  groupByPage,
  hasCitations,
  initialEditValue,
  isBlankRow,
  provenanceDescription,
  provenanceLabel,
  rowNeedsCheck,
  unchanged,
} from "@/lib/review/logic";
import type { ReuseChoice, ReviewEditView, ReviewProvenanceView, ReviewRowView } from "@/lib/review/types";
import type { AnswerValue } from "@/lib/service/types";
import { FieldMessages, describedBy } from "../fields";

/** Saves one changed answer; resolves to the service's message for that answer, or null. */
export type SaveAnswer = (row: ReviewRowView, value: AnswerValue, reuse: ReuseChoice, prepareAgain: boolean) => Promise<string | null>;

/** Where one answer came from, styled per kind. */
export function ProvenanceBadge({ provenance }: { provenance: ReviewProvenanceView }) {
  const kind = badgeKind(provenance);
  return (
    <span className={`provenance-badge provenance-badge--${kind}`} title={provenanceDescription(kind)}>
      {provenanceLabel(provenance)}
    </span>
  );
}

/** Copies text to the clipboard and says whether it worked. */
export function CopyButton({ text, label, what }: { text: string; label: string; what: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <span className="copy">
      <button
        type="button"
        className="chip-button copy__button"
        aria-label={`${label} ${what}`}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text);
            setState("copied");
          } catch {
            setState("failed");
          }
        }}
      >
        {state === "copied" ? "Copied" : label}
      </button>
      <span className="copy__status" role="status">
        {state === "failed" ? (
          "Couldn't copy here. Select the text to copy it."
        ) : state === "copied" ? (
          <span className="visually-hidden">Copied to the clipboard.</span>
        ) : (
          ""
        )}
      </span>
    </span>
  );
}

/**
 * Every question of the prepared form in form order, page by page, with where each
 * answer came from. With `onSave`, answers the service allows to change get an
 * inline editor; without it the list only reads.
 */
export function ReviewAnswers({
  rows,
  editNote,
  onSave,
  disabled = false,
}: {
  rows: ReviewRowView[];
  editNote?: string | null;
  onSave?: SaveAnswer;
  disabled?: boolean;
}) {
  const titleId = useId();
  const [editing, setEditing] = useState<string | null>(null);
  const groups = groupByPage(rows);
  const summary = answersSummary(rows);
  const paged = groups.length > 1;

  return (
    <section className="answers" aria-labelledby={titleId}>
      <h2 id={titleId} className="answers__title">
        Answers on the form
      </h2>
      {rows.length === 0 ? (
        <p className="answers__summary">
          The service listed no answers for this application yet. The screenshot, when there is one, shows the form.
        </p>
      ) : (
        <p className="answers__summary">
          {summary.total} {summary.total === 1 ? "question" : "questions"}
          {summary.pages > 1 ? ` on ${summary.pages} pages` : ""}
          {summary.blank > 0 ? ` · ${summary.blank} left blank` : ""}
          {summary.toCheck > 0 ? ` · ${summary.toCheck} flagged to check` : ""}.
        </p>
      )}
      {editNote && <p className="field__hint answers__note">{editNote}</p>}
      {groups.map((group) => (
        <div key={group.page ?? "none"} className="answers__group">
          {paged && <h3 className="answers__page">{group.page === null ? "Page not recorded" : `Page ${group.page}`}</h3>}
          <ol className="answers__list">
            {group.items.map((row, index) => {
              const key = row.questionId ?? `${group.page}-${index}`;
              return (
                <AnswerRow
                  key={key}
                  row={row}
                  editing={editing === key}
                  disabled={disabled}
                  onEdit={onSave && row.edit && row.questionId ? () => setEditing(key) : undefined}
                  onCancel={() => setEditing(null)}
                  onSave={
                    onSave
                      ? async (value, reuse, again) => {
                          const problem = await onSave(row, value, reuse, again);
                          if (problem === null) setEditing(null);
                          return problem;
                        }
                      : undefined
                  }
                />
              );
            })}
          </ol>
        </div>
      ))}
    </section>
  );
}

function AnswerRow({
  row,
  editing,
  disabled,
  onEdit,
  onCancel,
  onSave,
}: {
  row: ReviewRowView;
  editing: boolean;
  disabled: boolean;
  onEdit?: () => void;
  onCancel: () => void;
  onSave?: (value: AnswerValue, reuse: ReuseChoice, prepareAgain: boolean) => Promise<string | null>;
}) {
  const blank = isBlankRow(row);
  const check = rowNeedsCheck(row);
  const confidence = typeof row.confidence === "number" ? confidenceLabel(row.confidence) : null;
  const display = reviewDisplay({ control: row.control, value: row.value ?? [] });
  const copy = copyableText(row);
  const citations = hasCitations(row.citations) ? row.citations : null;

  return (
    <li className={`answer${check ? " is-check" : ""}${blank ? " is-blank" : ""}`} data-question-id={row.questionId ?? undefined}>
      <div className="answer__question">
        <p className="answer__text">
          {row.question}
          {row.required === false && <span className="tag">Optional</span>}
        </p>
        {!row.wordingRecorded && <p className="answer__unrecorded">Wording not recorded, see the screenshot</p>}
      </div>

      <div className="answer__value">
        {display.kind === "blank" ? (
          <span className="answer__blank">Left blank</span>
        ) : display.kind === "list" ? (
          <ul className="answer__choices">
            {display.items.map((choice, index) => (
              <li key={index}>{choice}</li>
            ))}
          </ul>
        ) : display.kind === "long_text" ? (
          <p className="answer__long">{display.text}</p>
        ) : display.kind === "file" ? (
          <span className="mono">{display.name}</span>
        ) : (
          <span className="answer__short">{display.text}</span>
        )}
        {copy && <CopyButton text={copy} label="Copy text" what={`of the answer to: ${row.question}`} />}
      </div>

      <div className="answer__meta">
        <ProvenanceBadge provenance={row.provenance} />
        {confidence && <span className="answer__confidence">{confidence}</span>}
        {check && <span className="review__flag">Check this</span>}
      </div>
      {row.provenance.detail && <p className="answer__detail">{row.provenance.detail}</p>}

      {citations && (
        <details className="answer__citations">
          <summary>{citationSummary(citations)}</summary>
          <dl>
            {citations.facts.length > 0 && <CitationList label="Facts" ids={citations.facts} />}
            {citations.passages.length > 0 && <CitationList label="Story passages" ids={citations.passages} />}
            {citations.jobEvidence.length > 0 && <CitationList label="Job description" ids={citations.jobEvidence} />}
          </dl>
        </details>
      )}

      <div className="answer__edit">
        {onEdit && row.edit ? (
          editing && onSave ? (
            <AnswerEditor row={row} edit={row.edit} onSave={onSave} onCancel={onCancel} />
          ) : (
            <button
              type="button"
              className="chip-button"
              disabled={disabled}
              aria-label={`Edit answer: ${row.question}`}
              onClick={onEdit}
            >
              Edit answer
            </button>
          )
        ) : row.noEditReason ? (
          <p className="answer__noedit">{row.noEditReason}</p>
        ) : null}
      </div>
    </li>
  );
}

function CitationList({ label, ids }: { label: string; ids: string[] }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        <ul>
          {ids.map((id) => (
            <li key={id}>
              <code>{id}</code>
            </li>
          ))}
        </ul>
      </dd>
    </div>
  );
}

/**
 * Changes one answer: the new value, how far it may be reused, then saved through
 * the answers route. "Save and prepare again" also re-reads and fills the form, so
 * the new answer is in the preparation you approve.
 */
export function AnswerEditor({
  row,
  edit,
  onSave,
  onCancel,
}: {
  row: ReviewRowView;
  edit: ReviewEditView;
  onSave: (value: AnswerValue, reuse: ReuseChoice, prepareAgain: boolean) => Promise<string | null>;
  onCancel: () => void;
}) {
  const baseId = useId();
  const [value, setValue] = useState<AnswerValue>(() => initialEditValue(edit));
  const scopes = allowedReuse(edit);
  const [reuse, setReuse] = useState<ReuseChoice>(scopes[0]);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<"again" | "save" | null>(null);
  const formRef = useRef<HTMLFormElement>(null);

  // Opening the editor moves focus to the new answer's first control.
  useEffect(() => {
    formRef.current?.querySelector<HTMLElement>("input, select, textarea")?.focus();
  }, []);

  async function save(again: boolean) {
    const problem = unchanged(edit, value) ? "That's the current answer. Change it, or cancel." : editProblem(edit, value);
    setError(problem);
    if (problem) return;
    setPending(again ? "again" : "save");
    try {
      setError(await onSave(value, reuse, again));
    } finally {
      setPending(null);
    }
  }

  return (
    <form
      ref={formRef}
      className="answer-editor"
      method="post"
      noValidate
      aria-label={`Change the answer to: ${row.question}`}
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        void save(true);
      }}
    >
      <EditControl edit={edit} id={`${baseId}-value`} question={row.question} value={value} error={error} onChange={(next) => {
        setValue(next);
        setError(null);
      }} />

      {scopes.length > 1 ? (
        <fieldset className="answer-editor__reuse">
          <legend className="field__label">Keep this answer for</legend>
          {scopes.map((scope) => (
            <label key={scope} className="choice">
              <input
                type="radio"
                name={`${baseId}-reuse`}
                value={scope}
                checked={reuse === scope}
                onChange={() => setReuse(scope)}
              />
              <span>
                {REUSE_CHOICES[scope].label}
                <span className="answer-editor__scope-hint"> · {REUSE_CHOICES[scope].hint}</span>
              </span>
            </label>
          ))}
        </fieldset>
      ) : (
        <p className="field__hint">Kept for this application only.</p>
      )}
      {edit.note && <p className="field__hint">{edit.note}</p>}

      <div className="answer-editor__actions">
        <button type="submit" className="button button--primary" disabled={pending !== null}>
          {pending === "again" ? "Saving and preparing again…" : "Save and prepare again"}
        </button>
        <button type="button" className="button button--secondary" disabled={pending !== null} onClick={() => void save(false)}>
          {pending === "save" ? "Saving…" : "Save only"}
        </button>
        <button type="button" className="text-button" disabled={pending !== null} onClick={onCancel}>
          Cancel
        </button>
      </div>
      <p className="field__hint">
        Saving withdraws any approval. The new answer is filled in when the application is prepared again, and you
        approve that preparation. Nothing is submitted.
      </p>
    </form>
  );
}

function EditControl({
  edit,
  id,
  question,
  value,
  error,
  onChange,
}: {
  edit: ReviewEditView;
  id: string;
  question: string;
  value: AnswerValue;
  error: string | null;
  onChange: (value: AnswerValue) => void;
}) {
  const hint = edit.lookup ? "The site looks this up as you type: the answer is typed into its search box exactly." : null;
  const describe = describedBy(id, hint, error);

  if (edit.attestation) {
    return (
      <div className={`field${error ? " is-invalid" : ""}`}>
        <label className="check answer-editor__statement" htmlFor={id}>
          <input
            id={id}
            type="checkbox"
            checked={value === true}
            aria-invalid={error ? true : undefined}
            aria-describedby={describe}
            onChange={(event) => onChange(event.target.checked)}
          />
          <span>Checked on the form: {question}</span>
        </label>
        <FieldMessages id={id} hint={hint} error={error} />
      </div>
    );
  }

  const options =
    edit.control === "boolean"
      ? [
          { value: "yes", label: "Yes" },
          { value: "no", label: "No" },
        ]
      : (edit.options ?? []);
  const choice = !edit.lookup && (edit.control === "boolean" || edit.control === "single_select" || edit.control === "multi_select") && options.length > 0;

  if (choice && edit.control !== "multi_select" && options.length > 6) {
    return (
      <div className={`field${error ? " is-invalid" : ""}`}>
        <label htmlFor={id} className="field__label">
          New answer
        </label>
        <select
          id={id}
          className="input"
          value={typeof value === "string" ? value : ""}
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

  if (choice) {
    const multi = edit.control === "multi_select";
    const selected = (option: string) =>
      multi
        ? Array.isArray(value) && value.includes(option)
        : edit.control === "boolean"
          ? (value === true && option === "yes") || (value === false && option === "no")
          : value === option;
    return (
      <fieldset id={id} className={`field choice-group${error ? " is-invalid" : ""}`} aria-describedby={describe}>
        <legend className="field__label">New answer</legend>
        <div className={`choices${options.length <= 3 ? " choices--inline" : ""}`}>
          {options.map((option) => (
            <label key={option.value} className="choice">
              <input
                type={multi ? "checkbox" : "radio"}
                name={id}
                value={option.value}
                checked={selected(option.value)}
                aria-invalid={error ? true : undefined}
                onChange={(event) => {
                  if (multi) {
                    const current = Array.isArray(value) ? value : [];
                    onChange(event.target.checked ? [...current, option.value] : current.filter((item) => item !== option.value));
                  } else if (edit.control === "boolean") {
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
  return (
    <div className={`field${error ? " is-invalid" : ""}`}>
      <label htmlFor={id} className="field__label">
        New answer
      </label>
      {edit.control === "long_text" ? (
        <textarea
          id={id}
          className="input"
          rows={8}
          value={text}
          aria-invalid={error ? true : undefined}
          aria-describedby={describe}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <input
          id={id}
          className="input"
          type={edit.control === "email" || edit.control === "tel" || edit.control === "url" ? edit.control : "text"}
          inputMode={edit.control === "number" ? "numeric" : undefined}
          autoComplete="off"
          value={text}
          aria-invalid={error ? true : undefined}
          aria-describedby={describe}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      <FieldMessages id={id} hint={hint} error={error} />
    </div>
  );
}
