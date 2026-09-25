"use client";

import { useId, useState, type FormEvent } from "react";
import { formatDateTime } from "@/lib/format";
import { approveState, type SubmitState } from "@/lib/review/logic";
import type { ApplicationReviewView } from "@/lib/review/types";
import { Modal } from "../Modal";
import { CopyButton } from "./ReviewAnswers";

export type ReviewAction = "approve" | "submit" | "prepare" | "browser";

/** A command to run in a terminal on this computer, with a copy button. */
export function CommandLine({ command, what }: { command: string; what: string }) {
  return (
    <div className="command-line">
      <code className="command-line__text">{command}</code>
      <CopyButton text={command} label="Copy" what={what} />
    </div>
  );
}

/**
 * The decisions on one application: approve what was reviewed, submit what was
 * approved (only when the service allows it and the person confirms), prepare it
 * again, or finish a step in the browser. Every button explains itself when it
 * can't be used.
 */
export function ReviewActions({
  review,
  submit,
  pending,
  onApprove,
  onSubmit,
  onPrepareAgain,
  onResumeInBrowser,
}: {
  review: ApplicationReviewView;
  submit: SubmitState;
  pending: ReviewAction | null;
  onApprove: () => void;
  onSubmit: () => void;
  onPrepareAgain: () => void;
  onResumeInBrowser: () => void;
}) {
  const approve = approveState(review);
  const problemsId = useId();
  const app = review.application;
  const busy = pending !== null;
  const browserStep = review.stage === "browser_action";

  return (
    <section className="review-actions" aria-labelledby="review-actions-title">
      <h2 id="review-actions-title" className="review-actions__title">
        Decide
      </h2>

      <div className="review-actions__block" data-testid="approval-block">
        <h3 className="review-actions__label">1 · Approve</h3>
        {review.approval ? (
          <p className="review-actions__done">
            <span className="mark mark--approved">Approved</span>{" "}
            {`${formatDateTime(review.approval.approvedAt)}. The approval pins exactly these answers (${review.approval.pages} ${review.approval.pages === 1 ? "page" : "pages"}). A new preparation or a changed answer withdraws it.`}
          </p>
        ) : approve.available ? (
          <>
            <button type="button" className="button button--primary" disabled={busy} onClick={onApprove}>
              {pending === "approve" ? "Approving…" : "Approve these answers"}
            </button>
            <p className="field__hint">Approving records your review of exactly these answers. It submits nothing.</p>
          </>
        ) : (
          <p className="field__hint">{approve.reason ?? "This application can't be approved now."}</p>
        )}
      </div>

      <div className="review-actions__block" data-testid="submit-block">
        <h3 className="review-actions__label">2 · Submit</h3>
        <button
          type="button"
          className={`button ${submit.enabled ? "button--primary" : "button--secondary"}`}
          disabled={!submit.enabled || busy}
          aria-describedby={submit.problems.length ? problemsId : undefined}
          onClick={onSubmit}
        >
          {pending === "submit" ? "Submitting…" : "Submit application…"}
        </button>
        {submit.problems.length > 0 ? (
          <div id={problemsId} className="submit-problems">
            <p className="submit-problems__title">Why Submit is off</p>
            <ul>
              {submit.problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="field__hint">You&rsquo;ll confirm before anything is sent.</p>
        )}
        {review.submit?.command && (
          <details className="review-actions__terminal">
            <summary>Submit from a terminal instead</summary>
            <CommandLine command={review.submit.command} what="the submit command" />
          </details>
        )}
      </div>

      <div className="review-actions__block">
        <h3 className="review-actions__label">{browserStep ? "Finish in the browser" : "Other actions"}</h3>
        {browserStep &&
          (review.browser?.available ? (
            <>
              <button type="button" className="button button--primary" disabled={busy} onClick={onResumeInBrowser}>
                {pending === "browser" ? "Opening the browser…" : "Resume in browser"}
              </button>
              <p className="field__hint">
                A browser window opens on the application. Complete the step there; the application carries on and stops
                at the final review step. Nothing is submitted.
              </p>
            </>
          ) : (
            <>
              <p className="field__hint">
                {review.browser?.reason ? `${review.browser.reason} ` : ""}Run this in a terminal on this computer; a
                browser window opens for you to finish the step:
              </p>
              {review.browser?.command && <CommandLine command={review.browser.command} what="the resume command" />}
            </>
          ))}
        {(review.stage === "prepared" || app.state === "FAILED_RETRYABLE") && (
          <>
            <button
              type="button"
              className={`button ${review.changedSincePreparation ? "button--primary" : "button--secondary"}`}
              disabled={busy}
              onClick={onPrepareAgain}
            >
              {pending === "prepare" ? "Preparing again…" : "Prepare again"}
            </button>
            <p className="field__hint">
              Re-reads the site and fills the form again with your current answers, then stops at the same review step.
              It withdraws an approval; nothing is submitted.
            </p>
          </>
        )}
      </div>
    </section>
  );
}

/** The explicit confirmation before anything is sent to the employer. */
export function SubmitConfirm({
  open,
  review,
  pending,
  onConfirm,
  onClose,
}: {
  open: boolean;
  review: ApplicationReviewView;
  pending: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const checkId = useId();
  const company = review.application.job.company ?? "the employer";
  const title = review.application.job.title;
  const answered = review.answers.filter((row) => row.value !== null).length;
  const pages = review.approval?.pages ?? new Set(review.answers.map((row) => row.page)).size;

  return (
    <Modal
      open={open}
      title="Submit this application?"
      onClose={() => {
        setConfirmed(false);
        onClose();
      }}
    >
      <form
        className="confirm-submit"
        method="post"
        noValidate
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          if (confirmed && !pending) onConfirm();
        }}
      >
        <p>
          This sends your application to <strong>{company}</strong>
          {title ? (
            <>
              {" "}
              for <strong>{title}</strong>
            </>
          ) : null}
          , exactly as you approved it: {answered} {answered === 1 ? "answer" : "answers"} on {pages}{" "}
          {pages === 1 ? "page" : "pages"}. It can&rsquo;t be undone.
        </p>
        {review.submit?.opensBrowser && (
          <p>
            A browser window opens and fills the form from your approval. If the site shows a CAPTCHA or asks you to sign
            in, complete it in that window.
          </p>
        )}
        <p className="field__hint">
          If the form changed since you approved it, nothing is sent and the application comes back for another review.
        </p>
        <label className="check confirm-submit__check" htmlFor={checkId}>
          <input
            id={checkId}
            type="checkbox"
            required
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          <span>I reviewed every answer and want to submit this application.</span>
        </label>
        <div className="panel__actions">
          <button type="submit" className="button button--primary" disabled={!confirmed || pending}>
            {pending ? "Submitting…" : `Submit to ${company}`}
          </button>
          <button
            type="button"
            className="button button--secondary"
            onClick={() => {
              setConfirmed(false);
              onClose();
            }}
          >
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
}
