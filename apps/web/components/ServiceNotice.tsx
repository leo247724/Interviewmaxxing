"use client";

import Link from "next/link";
import { useState } from "react";

export function ServiceNotice({
  mode,
  message,
  onRetry,
}: {
  mode: "live" | "preview";
  message: string | null;
  onRetry: () => Promise<void>;
}) {
  const [checking, setChecking] = useState(false);

  return (
    <section className="notice notice--unavailable" aria-labelledby="service-notice-title">
      <h2 id="service-notice-title" className="notice__title">
        The application service isn&rsquo;t connected
      </h2>
      <p>Nothing can be submitted from this desk until it is. What you type here stays on this page.</p>
      {message && <p className="notice__detail">{message}</p>}
      <div className="notice__actions">
        <button
          type="button"
          className="button button--secondary"
          disabled={checking}
          onClick={async () => {
            setChecking(true);
            await onRetry();
            setChecking(false);
          }}
        >
          {checking ? "Checking…" : "Check again"}
        </button>
        {mode === "live" && (
          <Link className="text-link" href="/preview">
            Explore the desk with fictional data
          </Link>
        )}
      </div>
      {mode === "live" && (
        <details className="notice__setup">
          <summary>Setup details</summary>
          <p>
            Start the Interviewmaxxing service on this computer, then start the web app with{" "}
            <code>IMX_BACKEND_URL</code> pointing at it, for example <code>http://127.0.0.1:8765</code>. See{" "}
            <code>apps/web/README.md</code>.
          </p>
        </details>
      )}
    </section>
  );
}
