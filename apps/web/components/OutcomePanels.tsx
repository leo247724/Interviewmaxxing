"use client";

import { useState } from "react";
import type { ApplicationView } from "@/lib/service/types";
import { formatDateTime } from "@/lib/format";
import type { DeskActions } from "./ApplicationDesk";
import { EvidenceList } from "./Evidence";

export function FailurePanel({ view, actions }: { view: ApplicationView; actions: DeskActions }) {
  const [pending, setPending] = useState(false);
  const failure = view.failure;

  return (
    <div className="panel panel--failure">
      <p className="lede">{failure?.reason ?? "The service stopped this application without giving a reason."}</p>
      {failure?.detail && <p>{failure.detail}</p>}
      {failure && <EvidenceList items={failure.evidence} title="What the site showed" />}
      <div className="panel__actions">
        {failure?.retryable && (
          <button
            type="button"
            className="button button--primary"
            disabled={pending}
            onClick={async () => {
              setPending(true);
              await actions.resume();
              setPending(false);
            }}
          >
            {pending ? "Starting again…" : "Try again"}
            <span aria-hidden="true" className="button__arrow">
              →
            </span>
          </button>
        )}
        <button type="button" className="button button--secondary" onClick={actions.startAnother}>
          Start a different application
        </button>
      </div>
    </div>
  );
}

export function DuplicatePanel({ view, actions }: { view: ApplicationView; actions: DeskActions }) {
  const prior = view.prior;
  return (
    <div className="panel panel--duplicate">
      <p className="lede">
        {prior ? (
          <>
            A confirmed application for this job was submitted on{" "}
            <time dateTime={prior.submittedAt}>{formatDateTime(prior.submittedAt)}</time>. Nothing was sent this time.
          </>
        ) : (
          "An earlier application for this job exists. Nothing was sent this time."
        )}
      </p>
      {prior && (
        <dl className="receipt__rows receipt__rows--compact">
          <div>
            <dt>Earlier application</dt>
            <dd className="mono">{prior.applicationId}</dd>
          </div>
          <div>
            <dt>Confirmation reference</dt>
            <dd className="mono">{prior.confirmationReference ?? "None shown by the site"}</dd>
          </div>
          <div>
            <dt>Application page</dt>
            <dd className="mono receipt__url">{prior.applicationUrl}</dd>
          </div>
        </dl>
      )}
      <div className="panel__actions">
        <button type="button" className="button button--secondary" onClick={actions.startAnother}>
          Start a different application
        </button>
      </div>
    </div>
  );
}
