"use client";

import { useId, useState } from "react";
import type { PipelineEntryView, PipelineLaneView } from "@/lib/pipeline/types";
import { compensationSummary } from "@/lib/pipeline/fields";
import { formatShortDate } from "./dates";
import { EntryBadges } from "./Badges";

export function EntryCard({
  entry,
  lanes,
  onOpen,
  onMove,
  onApply,
  busy,
}: {
  entry: PipelineEntryView;
  lanes: PipelineLaneView[];
  onOpen: () => void;
  onMove: (lane: string) => void;
  onApply: () => void;
  busy: boolean;
}) {
  const { fields } = entry;
  const [target, setTarget] = useState(entry.lane);
  const selectId = useId();
  const titleId = useId();
  const comp = compensationSummary(fields);
  const due = fields.nextInterviewDate
    ? `Interview ${formatShortDate(fields.nextInterviewDate)}${fields.interviewTimeCT ? ` · ${fields.interviewTimeCT} CT` : ""}`
    : fields.suggestedFollowUpDate
      ? `Follow-up suggested ${formatShortDate(fields.suggestedFollowUpDate)}`
      : null;
  const submitted = entry.application?.state === "SUBMITTED";

  return (
    <article
      className="card"
      aria-labelledby={titleId}
      draggable
      data-entry-id={entry.id}
      onDragStart={(event) => {
        event.dataTransfer.setData("text/x-imx-entry", entry.id);
        event.dataTransfer.effectAllowed = "move";
      }}
    >
      <h3 id={titleId} className="card__title">
        <span className="card__company">{fields.company ?? "Company not recorded"}</span>
        <span className="card__role">{fields.role ?? "Role not recorded"}</span>
      </h3>
      {(fields.stage || fields.status) && (
        <p className="card__stage">
          {fields.stage && <span className="card__stage-text">{fields.stage}</span>}
          {fields.status && <span className="card__status">{fields.status}</span>}
        </p>
      )}
      <dl className="card__facts">
        {fields.priority && (
          <div>
            <dt>Priority</dt>
            <dd>{fields.priority}</dd>
          </div>
        )}
        {fields.fitScore !== null && (
          <div>
            <dt>Your fit</dt>
            <dd>{fields.fitScore}/10</dd>
          </div>
        )}
        {comp && (
          <div>
            <dt>Pay</dt>
            <dd>{comp}</dd>
          </div>
        )}
      </dl>
      {(fields.nextAction || due) && (
        <p className="card__next">
          {fields.nextAction && <span>{fields.nextAction}</span>}
          {due && <span className="card__due">{due}</span>}
        </p>
      )}
      <EntryBadges entry={entry} />
      <div className="card__controls">
        <button type="button" className="chip-button" onClick={onOpen}>
          Open<span className="visually-hidden"> {fields.company ?? fields.role}</span>
        </button>
        {!submitted && (
          <button type="button" className="chip-button" onClick={onApply}>
            Apply…<span className="visually-hidden"> to {fields.company ?? fields.role}</span>
          </button>
        )}
        <form
          className="card__move"
          method="post"
          onSubmit={(event) => {
            event.preventDefault();
            if (target !== entry.lane) onMove(target);
          }}
        >
          <label htmlFor={selectId} className="visually-hidden">
            Move {fields.company ?? fields.role} to lane
          </label>
          <select id={selectId} value={target} onChange={(event) => setTarget(event.target.value)} disabled={busy}>
            {lanes.map((lane) => (
              <option key={lane.id} value={lane.id}>
                {lane.label}
              </option>
            ))}
          </select>
          <button type="submit" className="chip-button" disabled={busy || target === entry.lane}>
            Move
          </button>
        </form>
      </div>
    </article>
  );
}
