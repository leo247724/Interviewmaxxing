"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { formatDateTime } from "@/lib/format";
import { writeHandoff } from "@/lib/handoff";
import { reviewPageAddress } from "@/lib/preparation";
import {
  STAGE_LABELS,
  costText,
  editRequest,
  listedQueue,
  nextInQueue,
  outcomeLine,
  reviewExecutionProblem,
  submitRequest,
  submitState,
} from "@/lib/review/logic";
import type { ApplicationReviewView, ReviewQueueItemView } from "@/lib/review/types";
import { ServiceError, asServiceError } from "@/lib/service/errors";
import { PREPARED_PREVIEW_ID } from "@/lib/service/preview";
import type { AnswerInput, ApplicationView } from "@/lib/service/types";
import { describe, isActive } from "@/lib/state";
import type { DeskActions } from "../ApplicationDesk";
import { EvidenceList } from "../Evidence";
import { QuestionsForm } from "../QuestionsForm";
import { ServiceNotice } from "../ServiceNotice";
import { AppShell, PreviewStrip, type Connection } from "../shell/AppShell";
import { Timeline } from "../Timeline";
import { useReadiness } from "../useReadiness";
import { ReviewActions, SubmitConfirm, type ReviewAction } from "./ReviewActions";
import { ReviewAnswers, type SaveAnswer } from "./ReviewAnswers";
import { PREVIEW_REVIEW_NOTE, queueHref, reviewHref, reviewService } from "./ReviewQueue";

const WORKING_COPY: Partial<Record<ApplicationView["state"], string>> = {
  REQUESTED: "Opening the application page.",
  INSPECTING: "Reading the form on the site.",
  PACKET_READY: "Matching each question to your answers.",
  FILLING: "Filling the form. It stops at the final review step; nothing is submitted.",
  SUBMITTING:
    "Submitting. The attempt was recorded before Submit was pressed, so it can't be sent twice by accident. Waiting for the site to confirm it.",
};

/**
 * One application under review: every answer on the prepared form with where it came
 * from, and the decisions: change an answer, approve, submit (gated by the service and
 * the person's confirmation), prepare again or finish a step in the browser.
 */
export function ReviewPage({ mode, applicationId }: { mode: "live" | "preview"; applicationId: string }) {
  const { readiness, refresh: refreshReadiness } = useReadiness(mode);
  const service = useMemo(() => reviewService(mode), [mode]);
  const router = useRouter();
  const [review, setReview] = useState<ApplicationReviewView | null>(null);
  const [loadError, setLoadError] = useState<ServiceError | null>(null);
  const [connection, setConnection] = useState<Connection>(mode === "preview" ? "connected" : "checking");
  const [queue, setQueue] = useState<ReviewQueueItemView[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [pending, setPending] = useState<ReviewAction | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [lostContact, setLostContact] = useState<string | null>(null);
  const [pollNonce, setPollNonce] = useState(0);
  const failures = useRef(0);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const focused = useRef(false);

  const load = useCallback(async (): Promise<ApplicationReviewView | null> => {
    try {
      const next = await service.review(applicationId);
      setReview(next);
      setLoadError(null);
      setConnection("connected");
      failures.current = 0;
      setLostContact(null);
      return next;
    } catch (caught) {
      const serviceError = asServiceError(caught);
      setLoadError(serviceError);
      setConnection(serviceError.code === "unavailable" ? "unavailable" : "connected");
      return null;
    }
  }, [service, applicationId]);

  const loadQueue = useCallback(async () => {
    try {
      setQueue(listedQueue(await service.queue()));
    } catch {
      setQueue([]); // the review works without the queue; only "Next" is missing
    }
  }, [service]);

  useEffect(() => {
    void load();
    void loadQueue();
  }, [load, loadQueue]);

  // The headline takes focus once the application is shown.
  useEffect(() => {
    if (review && !focused.current) {
      focused.current = true;
      headingRef.current?.focus();
    }
  }, [review]);

  // Poll while the service works on the application (preparing again or submitting).
  useEffect(() => {
    if (!review || !isActive(review.application.state)) return;
    let cancelled = false;
    const base = mode === "preview" ? 500 : 1500;
    const delay = failures.current === 0 ? base : Math.min(10_000, base * 2 ** failures.current);
    const timer = window.setTimeout(async () => {
      try {
        const next = await service.review(applicationId);
        if (cancelled) return;
        failures.current = 0;
        setLostContact(null);
        setConnection("connected");
        setReview(next);
        if (!isActive(next.application.state)) void loadQueue();
      } catch (caught) {
        if (cancelled) return;
        failures.current += 1;
        setLostContact(asServiceError(caught).message);
        setPollNonce((nonce) => nonce + 1);
      }
    }, delay);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [review, service, applicationId, mode, pollNonce, loadQueue]);

  /** Why browser work may not start now (live mode checks fresh readiness), or null. */
  const executionCheck = useCallback(async (): Promise<string | null> => {
    if (mode !== "live" || !review) return null;
    return reviewExecutionProblem(await refreshReadiness(), review.application.applicationUrl);
  }, [mode, review, refreshReadiness]);

  const act = useCallback(async (kind: ReviewAction, action: () => Promise<void>) => {
    setActionError(null);
    setPending(kind);
    try {
      await action();
    } catch (caught) {
      const serviceError = asServiceError(caught);
      if (serviceError.code === "unavailable") setConnection("unavailable");
      setActionError(serviceError.message);
    } finally {
      setPending(null);
    }
  }, []);

  const approve = () =>
    act("approve", async () => {
      if (!review?.preparedPacketId) return;
      setReview(await service.approve(applicationId, { packetId: review.preparedPacketId }));
      setAnnouncement("Approved. Nothing was submitted.");
      void loadQueue();
    });

  const prepareAgain = (kind: "prepare" | "browser") =>
    act(kind, async () => {
      const problem = await executionCheck();
      if (problem) throw new ServiceError("invalid", problem);
      await service.prepareAgain(applicationId);
      await load();
      setAnnouncement(
        kind === "browser"
          ? "A browser window is opening on the application. Finish the step there."
          : "Preparing again. The form is read and filled again, then it stops at the review step.",
      );
      void loadQueue();
    });

  const confirmSubmit = () =>
    act("submit", async () => {
      if (!review) return;
      try {
        if (mode === "live") {
          const current = await refreshReadiness();
          const problem =
            reviewExecutionProblem(current, review.application.applicationUrl) ??
            (current?.submission === "disabled"
              ? "Submission is turned off in the service. Start it with IMX_ALLOW_SUBMISSION=1 to submit from here."
              : null);
          if (problem) throw new ServiceError("invalid", problem);
        }
        setReview(await service.submit(applicationId, submitRequest(review)));
        setAnnouncement("Submitting. The page shows the outcome when the site answers.");
      } finally {
        setConfirmOpen(false);
      }
    });

  const saveAnswer: SaveAnswer = async (row, value, reuse, again) => {
    setActionError(null);
    try {
      await service.editAnswer(applicationId, editRequest(row, value, reuse));
    } catch (caught) {
      const serviceError = asServiceError(caught);
      if (serviceError.code === "unavailable") setConnection("unavailable");
      return (row.questionId ? serviceError.fieldErrors[row.questionId] : undefined) ?? serviceError.message;
    }
    if (again) {
      let problem = await executionCheck();
      if (!problem) {
        try {
          await service.prepareAgain(applicationId);
        } catch (caught) {
          problem = asServiceError(caught).message;
        }
      }
      if (problem) setActionError(`Saved. It couldn't be prepared again yet: ${problem}`);
      else setAnnouncement("Saved. Preparing again with the new answer.");
    } else {
      setAnnouncement("Saved. Prepare it again to fill in the new answer.");
    }
    await load();
    void loadQueue();
    return null;
  };

  // The questions that remain are answered as on the desk.
  const answerRemaining = useCallback(
    async (input: AnswerInput): Promise<{ saved: boolean; rejected: boolean }> => {
      setActionError(null);
      try {
        const next = await service.answer(applicationId, input);
        const rejected = next.needs?.kind === "questions" && Object.keys(next.needs.errors).length > 0;
        setReview((current) => (current ? { ...current, application: next } : current));
        return { saved: true, rejected };
      } catch (caught) {
        const serviceError = asServiceError(caught);
        if (serviceError.code === "unavailable") setConnection("unavailable");
        setActionError(serviceError.message);
        return { saved: false, rejected: false };
      }
    },
    [service, applicationId],
  );

  const deskActions: DeskActions = {
    answer: async (input) => (await answerRemaining(input)).saved,
    answerAndContinue: async (input) => {
      const { saved, rejected } = await answerRemaining(input);
      if (!saved) return false;
      if (rejected) {
        setActionError("The site didn't accept some answers. They're marked below; nothing was submitted.");
        return false;
      }
      await prepareAgain("prepare");
      return true;
    },
    resume: async () => {
      await prepareAgain("prepare");
      return true;
    },
    reconcile: async () => false,
    checkNow: () => void load(),
    startAnother: () => router.push(queueHref(mode)),
  };

  function openInDesk(application: ApplicationView) {
    writeHandoff({
      applicationUrl: application.applicationUrl,
      company: application.job.company,
      role: application.job.title,
      from: "pipeline",
      pipelineEntryId: null,
      listingId: null,
      applicationId: application.id,
    });
    router.push(mode === "preview" ? "/preview" : "/");
  }

  const next = nextInQueue(queue, applicationId);
  const nextLink = next ? (
    <Link className="text-link" href={reviewHref(mode, next.id)}>
      Next in queue: {next.job.company ?? next.job.title ?? "next application"} →
    </Link>
  ) : null;

  return (
    <AppShell
      mode={mode}
      section="review"
      connection={connection}
      readiness={readiness}
      skipLabel="Skip to the review"
      previewBar={<PreviewStrip note={PREVIEW_REVIEW_NOTE} />}
      colophon="Approving submits nothing. An application is submitted only after you approve it and confirm, and only when the service has submission turned on."
    >
      <nav className="review-nav" aria-label="Prepared queue">
        <Link className="text-link" href={queueHref(mode)}>
          ← Prepared queue
        </Link>
        {nextLink}
      </nav>

      {!review ? (
        loadError ? (
          <LoadProblem mode={mode} error={loadError} onRetry={async () => void (await load())} />
        ) : (
          <p className="lede">Loading the application…</p>
        )
      ) : (
        <>
          <div className="review-layout">
            <div className="review-main">
              <ReviewHeader review={review} headingRef={headingRef} />
              <p className="visually-hidden" role="status" aria-live="polite">
                {announcement}
              </p>
              {lostContact && isActive(review.application.state) && (
                <div className="notice notice--lost" role="alert">
                  <p className="notice__title">Lost contact with the application service</p>
                  <p>
                    {review.application.state === "SUBMITTING"
                      ? "It may still be submitting. Don't submit it again; this page shows the saved outcome when contact returns."
                      : "Showing the last saved state. Checking again automatically."}{" "}
                    <span className="notice__detail">{lostContact}</span>
                  </p>
                </div>
              )}
              {actionError && (
                <p className="form-alert" role="alert">
                  {actionError}
                </p>
              )}
              <ReviewNotices
                review={review}
                onOpenDesk={mode === "live" || review.application.id === PREPARED_PREVIEW_ID ? openInDesk : null}
              />
              {review.application.needs?.kind === "questions" && !isActive(review.application.state) && (
                <section className="review-questions" aria-labelledby="review-questions-title">
                  <h2 id="review-questions-title" className="answers__title">
                    Questions still open
                  </h2>
                  <QuestionsForm
                    key={`${review.application.id}:${review.application.updatedAt}`}
                    needs={review.application.needs}
                    actions={deskActions}
                  />
                </section>
              )}
              <ReviewAnswers
                rows={Array.isArray(review.answers) ? review.answers : []}
                editNote={review.editNote}
                onSave={review.application.state === "NEEDS_INPUT" ? saveAnswer : undefined}
                disabled={pending !== null || isActive(review.application.state)}
              />
              {review.application.preparation?.evidence && review.application.preparation.evidence.length > 0 && (
                <div className="review-evidence">
                  <EvidenceList items={review.application.preparation.evidence} title="The filled review page" />
                </div>
              )}
              <details className="review-history">
                <summary>History ({review.application.events.length} events)</summary>
                <Timeline events={review.application.events} />
              </details>
            </div>

            <aside className="review-side" aria-label="Decisions">
              <ReviewActions
                review={review}
                submit={submitState(review, readiness, mode)}
                pending={pending}
                onApprove={() => void approve()}
                onSubmit={() => setConfirmOpen(true)}
                onPrepareAgain={() => void prepareAgain("prepare")}
                onResumeInBrowser={() => void prepareAgain("browser")}
              />
              <div className="review-side__links">
                {(mode === "live" || review.application.id === PREPARED_PREVIEW_ID) && (
                  <button type="button" className="text-button" onClick={() => openInDesk(review.application)}>
                    Open in the desk
                  </button>
                )}
                {nextLink}
              </div>
            </aside>
          </div>
          <SubmitConfirm
            open={confirmOpen}
            review={review}
            pending={pending === "submit"}
            onConfirm={() => void confirmSubmit()}
            onClose={() => setConfirmOpen(false)}
          />
        </>
      )}
    </AppShell>
  );
}

function LoadProblem({ mode, error, onRetry }: { mode: "live" | "preview"; error: ServiceError; onRetry: () => Promise<void> }) {
  if (error.code === "unavailable") return <ServiceNotice mode={mode} message={error.message} onRetry={onRetry} />;
  return (
    <section className="notice" role="alert" aria-labelledby="review-load-problem">
      <h2 id="review-load-problem" className="notice__title">
        {error.code === "not_found" ? "This application couldn't be found" : "This application couldn't be loaded"}
      </h2>
      <p>
        {error.code === "not_found" && error.message === "No such route."
          ? "This service doesn't provide the review page yet. Update the Interviewmaxxing service to review here."
          : error.message}
      </p>
      <div className="notice__actions">
        <button type="button" className="button button--secondary" onClick={() => void onRetry()}>
          Check again
        </button>
        <Link className="text-link" href={queueHref(mode)}>
          Back to the prepared queue
        </Link>
      </div>
    </section>
  );
}

function ReviewHeader({
  review,
  headingRef,
}: {
  review: ApplicationReviewView;
  headingRef: RefObject<HTMLHeadingElement | null>;
}) {
  const app = review.application;
  const preparedAt = app.preparation?.preparedAt;
  const address = reviewPageAddress(app.preparation?.formUrl);
  const stage = STAGE_LABELS[review.stage] ?? describe(app).headline;
  return (
    <header className="review-head">
      <p className="eyebrow">
        {app.job.ats ?? "Backend not identified"} · {stage}
      </p>
      <h1 className="display display--review" tabIndex={-1} ref={headingRef}>
        {app.job.title ?? "Application"}
      </h1>
      <p className="review-head__company">{app.job.company ?? "Employer not reported"}</p>
      <dl className="review-head__facts">
        <div>
          <dt>{review.stage === "prepared" && preparedAt ? "Prepared" : "Updated"}</dt>
          <dd>{formatDateTime(review.stage === "prepared" && preparedAt ? preparedAt : app.updatedAt)}</dd>
        </div>
        <div>
          <dt>AI cost</dt>
          <dd>{costText(review.providerCost)}</dd>
        </div>
        <div>
          <dt>Application</dt>
          <dd className="mono">{app.id}</dd>
        </div>
        {address && (
          <div>
            <dt>Final review page</dt>
            <dd className="mono">{address}</dd>
          </div>
        )}
      </dl>
    </header>
  );
}

/** What the application waits for, in words, above the answers. */
export function ReviewNotices({
  review,
  onOpenDesk,
}: {
  review: ApplicationReviewView;
  onOpenDesk: ((application: ApplicationView) => void) | null;
}) {
  const app = review.application;
  const working = isActive(app.state);
  const outcome = outcomeLine(review);
  const interaction = app.needs?.kind === "interaction" ? app.needs : null;
  return (
    <div className="review-notices">
      {working && (
        <p className="review-working" data-testid="review-working">
          <span className="working-line" aria-hidden="true" />
          {WORKING_COPY[app.state] ?? "Working on it."}
        </p>
      )}
      {outcome && (
        <div className={`notice notice--outcome notice--${app.state === "SUBMITTED" ? "success" : "caution"}`} data-testid="review-outcome">
          <p className="notice__title">{describe(app).headline}</p>
          <p>{outcome}</p>
          {onOpenDesk && (
            <div className="notice__actions">
              <button type="button" className="button button--secondary" onClick={() => onOpenDesk(app)}>
                {app.state === "SUBMITTED" ? "See the receipt on the desk" : "Open in the desk"}
              </button>
            </div>
          )}
        </div>
      )}
      {review.stage === "prepared" && app.preparation?.captchaPending === true && (
        <div className="notice notice--captcha" data-testid="captcha-note">
          <p className="notice__title">A CAPTCHA is waiting on the form</p>
          <p>
            It must be solved in a visible browser window when this application is submitted
            {review.submit?.opensBrowser ? ": the submission opens one for you." : "."} The desk doesn&rsquo;t solve
            CAPTCHAs.
          </p>
        </div>
      )}
      {review.changedSincePreparation && (
        <div className="notice notice--changed" data-testid="changed-note">
          <p className="notice__title">You changed answers since this preparation</p>
          <p>
            They&rsquo;re saved, but the form still holds the earlier answers. Prepare it again so they&rsquo;re filled
            in, then approve the new preparation.
          </p>
        </div>
      )}
      {review.stage === "browser_action" && interaction && (
        <div className="notice notice--browser" data-testid="browser-note">
          <p className="notice__title">
            {interaction.interaction === "SIGN_IN"
              ? "Sign in to continue"
              : interaction.interaction === "CAPTCHA"
                ? "Complete the CAPTCHA to continue"
                : "Finish a step in the browser"}
          </p>
          <p>{interaction.instructions}</p>
          {/* The page's address only: a query or fragment can hold a per-session token. */}
          {reviewPageAddress(interaction.pageUrl) && (
            <p className="mono notice__detail">{reviewPageAddress(interaction.pageUrl)}</p>
          )}
        </div>
      )}
    </div>
  );
}
