"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import type { ServiceReadiness } from "@/lib/service/readiness";

export type Connection = "checking" | "connected" | "unavailable";
export type Section = "desk" | "pipeline" | "jobs";

const SECTIONS: { id: Section; label: string; live: string; preview: string }[] = [
  { id: "jobs", label: "Jobs", live: "/jobs", preview: "/preview/jobs" },
  { id: "pipeline", label: "Pipeline", live: "/pipeline", preview: "/preview/pipeline" },
  { id: "desk", label: "Desk", live: "/", preview: "/preview" },
];

/** Page frame shared by the desk, pipeline and jobs views. */
export function AppShell({
  mode,
  section,
  connection,
  readiness,
  previewBar,
  colophon,
  skipLabel,
  children,
}: {
  mode: "live" | "preview";
  section: Section;
  connection: Connection;
  readiness?: ServiceReadiness | null;
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
            <svg className="brand-symbol" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="currentColor"/><path d="M8 22V10h4v8l4-6 4 6v-8h4v12h-4l-4-6-4 6Z" fill="var(--paper-raised)"/></svg>
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
                    <SectionIcon section={item.id} />
                    {item.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
          <ConnectionBadge mode={mode} connection={connection} />
        </header>

        <main id="main" className="desk__main" tabIndex={-1}>
          {mode === "live" && readiness && <div className={`execution-banner${readiness.applicationMode !== "TEST_ONLY" ? " execution-banner--blocked" : ""}`} role="status">
            <span className="execution-banner__label">{readiness.applicationMode === "TEST_ONLY" ? "Test mode" : "Applications paused"}</span>
            <span>{readiness.applicationMode === "TEST_ONLY" ? "Local test applications only. Real employer submissions are disabled." : "This development workspace requires the service to be in TEST_ONLY mode."}</span>
          </div>}
          {children}
        </main>

        <footer className="colophon">
          <p>{colophon}</p>
        </footer>
      </div>
    </>
  );
}

function SectionIcon({ section }: { section: Section }) {
  return <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
    {section === "jobs" ? <><circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/></> : section === "pipeline" ? <><rect x="2.5" y="4" width="4" height="12" rx="1"/><rect x="8" y="4" width="4" height="8" rx="1"/><rect x="13.5" y="4" width="4" height="10" rx="1"/></> : <><rect x="3" y="2.5" width="12" height="15" rx="2"/><path d="M6.5 6h5M6.5 9h5M6.5 12H10m3.5.5 2 2 3-4"/></>}
  </svg>;
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
