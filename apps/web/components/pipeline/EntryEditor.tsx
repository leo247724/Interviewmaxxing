"use client";

import { useState, type FormEvent } from "react";
import {
  DATE_FIELDS,
  EMPTY_FIELDS,
  NUMBER_FIELDS,
  REFERENCE_COLUMNS,
  type PipelineEntryView,
  type PipelineFieldKey,
  type PipelineFields,
  type PipelineLaneView,
} from "@/lib/pipeline/types";
import { parseCell, validateFields } from "@/lib/pipeline/fields";
import { validateApplicationUrl } from "@/lib/validation";
import { formatDateTime } from "@/lib/format";
import type { ServiceError } from "@/lib/service/errors";
import { ErrorSummary } from "../ErrorSummary";
import { FieldMessages, describedBy } from "../fields";
import { EntryBadges } from "./Badges";

export interface EditorResult {
  lane: string;
  fields: PipelineFields;
  applicationUrl: string | null;
}

const GROUPS: { title: string; fields: PipelineFieldKey[]; note?: string }[] = [
  { title: "Role", fields: ["company", "role", "priority", "fitScore"] },
  {
    title: "Where it stands",
    fields: ["stage", "status", "nextAction", "suggestedFollowUpDate", "decisionDueText"],
    note: "Stage and status are your own words and are kept exactly as written.",
  },
  {
    title: "Interviews",
    fields: ["nextInterviewDate", "interviewTimeCT", "interviewFormat", "lastInterviewDate"],
    note: "Interview times are Central Time (America/Chicago).",
  },
  { title: "Work", fields: ["workArrangement", "locationCommute"] },
  {
    title: "Compensation",
    fields: [
      "compensationLow",
      "compensationHigh",
      "compensationBasis",
      "targetAssessment",
      "compensationBenefitsNotes",
    ],
    note: "US dollars per year. Leave blank when unknown.",
  },
  { title: "Source and fit", fields: ["sourceRecruiter", "fitRationale", "processSourceNotes"] },
];

const LONG_TEXT: ReadonlySet<PipelineFieldKey> = new Set([
  "stage",
  "status",
  "nextAction",
  "compensationBenefitsNotes",
  "fitRationale",
  "processSourceNotes",
]);

const HINTS: Partial<Record<PipelineFieldKey, string>> = {
  fitScore: "Your own score from 0 to 10.",
  interviewTimeCT: "For example 2:30 PM.",
  suggestedFollowUpDate: "A date you noted. The app never schedules or sends anything.",
  decisionDueText: "Free text, for example “End of next week”.",
  sourceRecruiter: "Recorded for your reference only. Nothing here contacts anyone.",
};

function header(field: PipelineFieldKey) {
  return REFERENCE_COLUMNS.find((column) => column.field === field)!.header;
}

function toText(fields: PipelineFields): Record<PipelineFieldKey, string> {
  return Object.fromEntries(
    (Object.keys(EMPTY_FIELDS) as PipelineFieldKey[]).map((key) => [
      key,
      fields[key] === null ? "" : String(fields[key]),
    ]),
  ) as Record<PipelineFieldKey, string>;
}

export function EntryEditor({
  entry,
  lanes,
  defaultLane,
  onSave,
  onReload,
}: {
  entry: PipelineEntryView | null;
  lanes: PipelineLaneView[];
  defaultLane: string;
  onSave: (result: EditorResult) => Promise<ServiceError | null>;
  onReload: () => void;
}) {
  const [text, setText] = useState(() => toText(entry?.fields ?? EMPTY_FIELDS));
  const [lane, setLane] = useState(entry?.lane ?? defaultLane);
  const [applicationUrl, setApplicationUrl] = useState(entry?.applicationUrl ?? "");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [attempt, setAttempt] = useState(0);
  const [alert, setAlert] = useState<{ message: string; conflict: boolean } | null>(null);
  const [saving, setSaving] = useState(false);

  const order = ["lane", "applicationUrl", ...GROUPS.flatMap((group) => group.fields)];
  const summary = order.filter((key) => errors[key]).map((key) => ({ fieldId: `pf-${key}`, message: errors[key] }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    setAlert(null);
    const found: Record<string, string> = {};
    const fields = { ...EMPTY_FIELDS };
    for (const key of Object.keys(EMPTY_FIELDS) as PipelineFieldKey[]) {
      const parsed = parseCell(key, text[key]);
      if (parsed.error) found[key] = parsed.error;
      (fields as Record<string, unknown>)[key] = parsed.value;
    }
    for (const [key, message] of Object.entries(validateFields(fields))) {
      if (!found[key]) found[key] = message!;
    }
    const url = applicationUrl.trim();
    if (url) {
      const urlError = validateApplicationUrl(url);
      if (urlError) found.applicationUrl = urlError;
    }
    setErrors(found);
    setAttempt((count) => count + 1);
    if (Object.keys(found).length) return;

    setSaving(true);
    const error = await onSave({ lane, fields, applicationUrl: url || null });
    setSaving(false);
    if (error) {
      if (Object.keys(error.fieldErrors).length) {
        setErrors(error.fieldErrors);
        setAttempt((count) => count + 1);
      }
      setAlert({ message: error.message, conflict: error.code === "conflict" });
    }
  }

  return (
    <form className="editor" method="post" noValidate onSubmit={submit}>
      {entry && (
        <div className="editor__summary">
          <EntryBadges entry={entry} />
          {entry.application && (
            <p className="field__hint">
              Linked application {entry.application.applicationId}
              {entry.application.confirmationReference ? ` · reference ${entry.application.confirmationReference}` : ""}
              . Its state comes from the application record, not from this card.
            </p>
          )}
        </div>
      )}

      <ErrorSummary
        title={summary.length === 1 ? "One field needs attention" : `${summary.length} fields need attention`}
        items={summary}
        attempt={attempt}
      />
      {alert && (
        <div className="form-alert" role="alert">
          <p>{alert.message}</p>
          {alert.conflict && (
            <button type="button" className="button button--secondary" onClick={onReload}>
              Reload the latest version
            </button>
          )}
        </div>
      )}

      <div className="editor__top">
        <div className={`field${errors.lane ? " is-invalid" : ""}`}>
          <label htmlFor="pf-lane" className="field__label">
            Lane
          </label>
          <select
            id="pf-lane"
            className="input"
            value={lane}
            onChange={(event) => setLane(event.target.value)}
            aria-describedby="pf-lane-hint"
          >
            {lanes.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
          <FieldMessages
            id="pf-lane"
            hint="Your tracking category. Moving a card never submits anything."
            error={errors.lane}
          />
        </div>
        <div className={`field${errors.applicationUrl ? " is-invalid" : ""}`}>
          <label htmlFor="pf-applicationUrl" className="field__label">
            Application link
          </label>
          <input
            id="pf-applicationUrl"
            className="input mono"
            type="url"
            inputMode="url"
            spellCheck={false}
            value={applicationUrl}
            aria-invalid={errors.applicationUrl ? true : undefined}
            aria-describedby={describedBy("pf-applicationUrl", "hint", errors.applicationUrl)}
            onChange={(event) => setApplicationUrl(event.target.value)}
          />
          <FieldMessages
            id="pf-applicationUrl"
            hint="The page where the application form starts. A company homepage isn't one."
            error={errors.applicationUrl}
          />
        </div>
      </div>

      {GROUPS.map((group) => (
        <fieldset key={group.title} className="editor__group">
          <legend className="editor__legend">{group.title}</legend>
          {group.note && <p className="field__hint editor__note">{group.note}</p>}
          <div className="editor__grid">
            {group.fields.map((key) => {
              const id = `pf-${key}`;
              const hint = HINTS[key];
              const long = LONG_TEXT.has(key);
              return (
                <div key={key} className={`field${long ? " field--wide" : ""}${errors[key] ? " is-invalid" : ""}`}>
                  <label htmlFor={id} className="field__label">
                    {header(key)}
                  </label>
                  {long ? (
                    <textarea
                      id={id}
                      className="input"
                      rows={2}
                      value={text[key]}
                      aria-invalid={errors[key] ? true : undefined}
                      aria-describedby={describedBy(id, hint, errors[key])}
                      onChange={(event) => setText((current) => ({ ...current, [key]: event.target.value }))}
                    />
                  ) : (
                    <input
                      id={id}
                      className="input"
                      type={DATE_FIELDS.has(key) ? "date" : "text"}
                      inputMode={NUMBER_FIELDS.has(key) ? "decimal" : undefined}
                      value={text[key]}
                      aria-invalid={errors[key] ? true : undefined}
                      aria-describedby={describedBy(id, hint, errors[key])}
                      onChange={(event) => setText((current) => ({ ...current, [key]: event.target.value }))}
                    />
                  )}
                  <FieldMessages id={id} hint={hint} error={errors[key]} />
                </div>
              );
            })}
          </div>
        </fieldset>
      ))}

      <div className="editor__actions">
        <button type="submit" className="button button--primary" disabled={saving}>
          {saving ? "Saving…" : entry ? "Save changes" : "Add to pipeline"}
        </button>
      </div>

      {entry?.provenance && (
        <details className="editor__details">
          <summary>
            Imported from {entry.provenance.fileName ?? "a file"}, row {entry.provenance.sourceRow}
          </summary>
          <p className="field__hint">
            Imported {formatDateTime(entry.provenance.importedAt)} · file digest{" "}
            <span className="mono">{entry.provenance.sourceDigest.slice(0, 12)}…</span>. Original values, kept as
            imported:
          </p>
          <dl className="provenance">
            {Object.entries(entry.provenance.importedValues)
              .filter(([, value]) => value !== "")
              .map(([name, value]) => (
                <div key={name}>
                  <dt>{name}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
          </dl>
        </details>
      )}

      {entry && entry.history.length > 0 && (
        <details className="editor__details">
          <summary>History ({entry.history.length})</summary>
          <ol className="history">
            {[...entry.history].reverse().map((item, index) => (
              <li key={`${item.at}-${index}`}>
                <time dateTime={item.at}>{formatDateTime(item.at)}</time> {item.summary}
              </li>
            ))}
          </ol>
        </details>
      )}
    </form>
  );
}
