"use client";

import { useEffect, useRef } from "react";
import type { ApplicationView, CandidateProfileInput } from "@/lib/service/types";
import { describe, isActive, type Mood } from "@/lib/state";
import { receiptAuthority } from "@/lib/receipt";
import { formatDateTime } from "@/lib/format";
import type { DeskActions } from "./ApplicationDesk";
import { ProgressRail } from "./ProgressRail";
import { Timeline } from "./Timeline";
import { Stamp } from "./Stamp";
import { QuestionsForm } from "./QuestionsForm";
import { InteractionPanel } from "./InteractionPanel";
import { UncertainPanel } from "./UncertainPanel";
import { Receipt } from "./Receipt";
import { FailurePanel, DuplicatePanel } from "./OutcomePanels";

const WORKING_COPY: Partial<Record<ApplicationView["state"], string>> = {
  REQUESTED: "Opening the page, identifying the job and checking for an earlier application.",
  INSPECTING: "Reading the form and checking this job against your earlier applications.",
  PACKET_READY: "Matching each field to your profile, resume and saved answers.",
  FILLING: "Entering your details. You'll only be asked if something is missing.",
  SUBMITTING:
    "The attempt was recorded before Submit was pressed, so it can't be sent twice by accident. Waiting for the site to confirm it.",
};

export function ApplicationWorkspace({
  view,
  mode,
  profile,
  actions,
  actionError,
  lostContact,
}: {
  view: ApplicationView;
  mode: "live" | "preview";
  profile: CandidateProfileInput;
  actions: DeskActions;
  actionError: string | null;
  lostContact: string | null;
}) {
  const { headline, mood } = describe(view);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const phase = `${view.state}:${view.needs?.kind ?? ""}`;
  const firstPhase = useRef(true);

  // Move focus to the headline when the application needs the user or finishes.
  useEffect(() => {
    if (firstPhase.current) {
      firstPhase.current = false;
      headingRef.current?.focus();
      return;
    }
    if (!isActive(view.state)) headingRef.current?.focus();
  }, [phase, view.state]);

  const jobLine =
    view.job.title && view.job.company ? (
      <>
        <span className="case__role">{view.job.title}</span>
        <span className="case__company">{view.job.company}</span>
      </>
    ) : (
      <span className="case__company case__company--pending">Identifying the job…</span>
    );

  return (
    <div className={`workspace mood-${mood}`}>
      <section className="case" aria-labelledby="case-title">
        <div className="case__meta">
          <span>
            Application <span className="case__id">{view.id}</span>
          </span>
          <span>Requested {formatDateTime(view.requestedAt)}</span>
        </div>
        <p className="case__job">{jobLine}</p>
        <p className="case__url">
          <a href={view.applicationUrl} target="_blank" rel="noreferrer noopener">
            {view.applicationUrl}
            <span className="visually-hidden"> (opens in a new tab)</span>
          </a>
        </p>

        <ProgressRail view={view} />

        <div className="case__headline">
          <h1 id="case-title" className="display display--case" tabIndex={-1} ref={headingRef}>
            {headline}
          </h1>
          <StateStamp view={view} mood={mood} />
        </div>
        <p className="visually-hidden" role="status" aria-live="polite">
          {headline}
        </p>

        {lostContact && isActive(view.state) && (
          <div className="notice notice--lost" role="alert">
            <p className="notice__title">Lost contact with the application service</p>
            <p>
              {view.state === "SUBMITTING"
                ? "The application may still be submitting. Don't start it again; this page will show the saved outcome when contact returns."
                : "Showing the last saved state. Checking again automatically."}{" "}
              <span className="notice__detail">{lostContact}</span>
            </p>
            <button type="button" className="button button--secondary" onClick={actions.checkNow}>
              Check now
            </button>
          </div>
        )}

        {actionError && (
          <p className="form-alert" role="alert">
            {actionError}
          </p>
        )}

        <StateBody view={view} mode={mode} actions={actions} />

        <p className="case__as">
          Applying as{" "}
          <strong>
            {profile.firstName} {profile.lastName}
          </strong>
          {profile.email && <> · {profile.email}</>}
          {view.resumeFileName && <> · resume {view.resumeFileName}</>}
        </p>
      </section>

      <aside className="docket" aria-labelledby="docket-title">
        <h2 id="docket-title" className="docket__title">
          Docket
        </h2>
        <p className="docket__note">Everything the desk has done, as reported by the service.</p>
        <Timeline events={view.events} />
      </aside>
    </div>
  );
}

function StateStamp({ view, mood }: { view: ApplicationView; mood: Mood }) {
  switch (view.state) {
    case "SUBMITTED":
      return view.receipt && receiptAuthority(view.receipt).byUser ? (
        <Stamp tone="caution" word="Reported" date={view.receipt.submittedAt} />
      ) : (
        <Stamp tone="success" word="Received" date={view.receipt?.submittedAt ?? view.updatedAt} />
      );
    case "SUBMISSION_UNKNOWN":
      return <Stamp tone="caution" word="Unconfirmed" date={view.uncertain?.attemptedAt ?? view.updatedAt} />;
    case "DUPLICATE":
      return <Stamp tone="neutral" word="Already filed" date={view.prior?.submittedAt ?? view.updatedAt} />;
    case "FAILED_PERMANENT":
    case "FAILED_RETRYABLE":
      return <Stamp tone="failure" word="Not sent" date={view.updatedAt} />;
    default:
      return mood === "working" ? <span className="working-line" aria-hidden="true" /> : null;
  }
}

function StateBody({ view, mode, actions }: { view: ApplicationView; mode: "live" | "preview"; actions: DeskActions }) {
  if (isActive(view.state)) {
    return <p className="lede">{WORKING_COPY[view.state]}</p>;
  }
  switch (view.state) {
    case "NEEDS_INPUT":
      if (view.needs?.kind === "questions") {
        return <QuestionsForm key={view.id} needs={view.needs} actions={actions} />;
      }
      if (view.needs?.kind === "interaction") {
        return <InteractionPanel needs={view.needs} actions={actions} />;
      }
      return (
        <p className="lede">
          The service is waiting for input it hasn&rsquo;t described. Check the application window.
        </p>
      );
    case "SUBMITTED":
      return view.receipt ? (
        <Receipt view={view} receipt={view.receipt} mode={mode} onStartAnother={actions.startAnother} />
      ) : (
        <p className="lede">Submitted, but the service returned no receipt details.</p>
      );
    case "SUBMISSION_UNKNOWN":
      return <UncertainPanel view={view} actions={actions} />;
    case "FAILED_RETRYABLE":
    case "FAILED_PERMANENT":
      return <FailurePanel view={view} actions={actions} />;
    case "DUPLICATE":
      return <DuplicatePanel view={view} actions={actions} />;
    default:
      return (
        <div className="panel">
          <p className="lede">This application was withdrawn.</p>
          <button type="button" className="button button--secondary" onClick={actions.startAnother}>
            Start a different application
          </button>
        </div>
      );
  }
}
