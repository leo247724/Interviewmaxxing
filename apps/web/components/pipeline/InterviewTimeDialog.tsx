"use client";

import { useEffect, useState, type FormEvent } from "react";
import type { PipelineEntryView } from "@/lib/pipeline/types";
import { isValidDate } from "@/lib/pipeline/fields";
import { Modal } from "../Modal";
import { FieldMessages, describedBy } from "../fields";

/** Fields the dialog saves; each is a tracker field of the card. */
export interface InterviewTimeResult {
  nextInterviewDate: string | null;
  interviewTimeCT: string | null;
  interviewFormat: string | null;
}

/** Turn "2:30 PM", "14:30" or "2pm" into 24-hour "HH:MM" (Central); null when blank; undefined when unreadable. */
export function normaliseTimeCT(raw: string): string | null | undefined {
  const value = raw.trim();
  if (!value) return null;
  const match = /^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/i.exec(value);
  if (!match) return undefined;
  let hours = Number(match[1]);
  const minutes = match[2] ? Number(match[2]) : 0;
  const meridiem = match[3]?.toLowerCase();
  if (minutes > 59) return undefined;
  if (meridiem) {
    if (hours < 1 || hours > 12) return undefined;
    hours = (hours % 12) + (meridiem === "pm" ? 12 : 0);
  } else if (hours > 23) {
    return undefined;
  }
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
}

/**
 * Asked right after a card lands in Scheduling or Interviewing: the interview's date, time and format.
 * Saving writes the fields to the service; skipping leaves the card as it was moved.
 */
export function InterviewTimeDialog({
  entry,
  laneLabel,
  onSave,
  onSkip,
}: {
  entry: PipelineEntryView | null;
  laneLabel: string;
  onSave: (result: InterviewTimeResult) => Promise<string | null>;
  onSkip: () => void;
}) {
  const [date, setDate] = useState("");
  const [time, setTime] = useState("");
  const [format, setFormat] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [serviceMessage, setServiceMessage] = useState<string | null>(null);

  useEffect(() => {
    setDate(entry?.fields.nextInterviewDate ?? "");
    setTime(entry?.fields.interviewTimeCT ?? "");
    setFormat(entry?.fields.interviewFormat ?? "");
    setErrors({});
    setServiceMessage(null);
    setSaving(false);
  }, [entry]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next: Record<string, string> = {};
    const trimmedDate = date.trim();
    if (trimmedDate && !isValidDate(trimmedDate)) next.nextInterviewDate = "Use a real date (YYYY-MM-DD).";
    const normalisedTime = normaliseTimeCT(time);
    if (normalisedTime === undefined) next.interviewTimeCT = "Use a time like 2:30 PM or 14:30 (Central).";
    setErrors(next);
    if (Object.keys(next).length) return;
    setSaving(true);
    const message = await onSave({
      nextInterviewDate: trimmedDate || null,
      interviewTimeCT: normalisedTime ?? null,
      interviewFormat: format.trim() || null,
    });
    setSaving(false);
    if (message) setServiceMessage(message);
  }

  const name = entry ? [entry.fields.company, entry.fields.role].filter(Boolean).join(" — ") : "";

  return (
    <Modal open={entry !== null} title="Interview time" onClose={onSkip}>
      <form className="editor" onSubmit={submit} noValidate>
        <p className="lede">
          {name || "This card"} is now in {laneLabel}. When is the interview? Leave a field blank if you don’t know yet.
        </p>
        {serviceMessage && (
          <p className="notice notice--error" role="alert">
            {serviceMessage}
          </p>
        )}
        <div className="editor__grid">
          <div className={`field${errors.nextInterviewDate ? " is-invalid" : ""}`}>
            <label htmlFor="it-date" className="field__label">
              Next interview date
            </label>
            <input
              id="it-date"
              className="input"
              type="date"
              value={date}
              aria-invalid={errors.nextInterviewDate ? true : undefined}
              aria-describedby={describedBy("it-date", undefined, errors.nextInterviewDate)}
              onChange={(event) => setDate(event.target.value)}
            />
            <FieldMessages id="it-date" hint={undefined} error={errors.nextInterviewDate} />
          </div>
          <div className={`field${errors.interviewTimeCT ? " is-invalid" : ""}`}>
            <label htmlFor="it-time" className="field__label">
              Time (Central)
            </label>
            <input
              id="it-time"
              className="input"
              type="text"
              placeholder="2:30 PM"
              value={time}
              aria-invalid={errors.interviewTimeCT ? true : undefined}
              aria-describedby={describedBy("it-time", "Saved as 24-hour Central time.", errors.interviewTimeCT)}
              onChange={(event) => setTime(event.target.value)}
            />
            <FieldMessages id="it-time" hint="Saved as 24-hour Central time." error={errors.interviewTimeCT} />
          </div>
          <div className="field">
            <label htmlFor="it-format" className="field__label">
              Format
            </label>
            <input
              id="it-format"
              className="input"
              type="text"
              placeholder="Video call, phone, onsite"
              value={format}
              onChange={(event) => setFormat(event.target.value)}
            />
          </div>
        </div>
        <div className="editor__actions">
          <button type="submit" className="button button--primary" disabled={saving}>
            {saving ? "Saving…" : "Save interview time"}
          </button>
          <button type="button" className="button button--secondary" onClick={onSkip} disabled={saving}>
            Not now
          </button>
        </div>
      </form>
    </Modal>
  );
}
