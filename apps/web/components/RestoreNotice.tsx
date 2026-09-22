"use client";

import { useState } from "react";
import type { RestoreOutcome } from "@/lib/restore";

type Problem = Exclude<RestoreOutcome, { kind: "none" | "restored" }>;

/** Explains an application this page was following but couldn't reload. */
export function RestoreNotice({
  problem,
  onRetry,
  onStopFollowing,
  onDismiss,
}: {
  problem: Problem;
  onRetry: () => Promise<void>;
  onStopFollowing: () => void;
  onDismiss: () => void;
}) {
  const [checking, setChecking] = useState(false);

  if (problem.kind === "gone") {
    return (
      <section className="notice notice--restore" role="status" aria-labelledby="restore-notice-title">
        <h2 id="restore-notice-title" className="notice__title">
          The earlier application is no longer on record
        </h2>
        <p>
          The service has no application <span className="mono">{problem.applicationId}</span>, so this page stopped
          following it.
        </p>
        <div className="notice__actions">
          <button type="button" className="text-button" onClick={onDismiss}>
            Dismiss
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="notice notice--restore" role="alert" aria-labelledby="restore-notice-title">
      <h2 id="restore-notice-title" className="notice__title">
        Couldn&rsquo;t reload the application you were following
      </h2>
      <p>
        Application <span className="mono">{problem.applicationId}</span> may still be in progress, including
        submitting. Don&rsquo;t apply to that job again; check again to see its saved state.
      </p>
      <p className="notice__detail">{problem.message}</p>
      <div className="notice__actions">
        <button
          type="button"
          className="button button--primary"
          disabled={checking}
          onClick={async () => {
            setChecking(true);
            await onRetry();
            setChecking(false);
          }}
        >
          {checking ? "Checking…" : "Check again"}
        </button>
        <button type="button" className="text-button" onClick={onStopFollowing}>
          Stop following it on this page
        </button>
      </div>
      <p className="field__hint">Stopping only affects this page. The service keeps the application&rsquo;s record.</p>
    </section>
  );
}
