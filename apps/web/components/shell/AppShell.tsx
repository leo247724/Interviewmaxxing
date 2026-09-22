"use client";

import Link from "next/link";
import type { ReactNode } from "react";

export type Connection = "checking" | "connected" | "unavailable";
export type Section = "desk" | "pipeline" | "jobs";

const SECTIONS: { id: Section; label: string; live: string; preview: string }[] = [
  { id: "desk", label: "Desk", live: "/", preview: "/preview" },
  { id: "pipeline", label: "Pipeline", live: "/pipeline", preview: "/preview/pipeline" },
  { id: "jobs", label: "Jobs", live: "/jobs", preview: "/preview/jobs" },
];

/** Page frame shared by the desk, pipeline and jobs views. */
export function AppShell({
  mode,
  section,
  connection,
  previewBar,
  colophon,
  skipLabel,
  children,
}: {
  mode: "live" | "preview";
  section: Section;
  connection: Connection;
  previewBar?: ReactNode;
  colophon: string;
  skipLabel: string;
  children: ReactNode;
}) {
  return (
    <>
      <a className="skip-link" href="#main">
        {skipLabel}
      </a>
      {mode === "preview" && previewBar}
      <div className={`desk desk--${section}`}>
        <header className="masthead">
          <div className="masthead__brand">
            <span className="wordmark">Interviewmaxxing</span>
          </div>
          <nav className="sections" aria-label="Sections">
            <ul>
              {SECTIONS.map((item) => (
                <li key={item.id}>
                  <Link
                    href={mode === "preview" ? item.preview : item.live}
                    aria-current={item.id === section ? "page" : undefined}
                  >
                    {item.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
          <ConnectionBadge mode={mode} connection={connection} />
        </header>

        <main id="main" className="desk__main" tabIndex={-1}>
          {children}
        </main>

        <footer className="colophon">
          <p>{colophon}</p>
        </footer>
      </div>
    </>
  );
}

export function ConnectionBadge({ mode, connection }: { mode: "live" | "preview"; connection: Connection }) {
  if (mode === "preview") {
    return (
      <p className="badge badge--preview">
        <span className="badge__dot" aria-hidden="true" />
        Preview · nothing is sent
      </p>
    );
  }
  const label =
    connection === "connected"
      ? "Service connected"
      : connection === "checking"
        ? "Checking service…"
        : "Service not connected";
  return (
    <p className={`badge badge--${connection}`} role="status">
      <span className="badge__dot" aria-hidden="true" />
      {label}
    </p>
  );
}

/** Preview banner without scenario controls, for the pipeline and jobs fixtures. */
export function PreviewStrip({ note }: { note: string }) {
  return (
    <section className="preview-bar" aria-label="Preview notice">
      <div className="preview-bar__inner">
        <p className="preview-bar__tag">Preview</p>
        <p className="preview-bar__note">{note}</p>
      </div>
    </section>
  );
}
