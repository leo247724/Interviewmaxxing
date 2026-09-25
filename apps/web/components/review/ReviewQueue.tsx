"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { formatDateTime } from "@/lib/format";
import { HttpReviewService } from "@/lib/review/http";
import {
  QUEUE_FILTERS,
  costShort,
  costText,
  filterQueue,
  holdTone,
  listedQueue,
  queueCounts,
  type QueueFilter,
} from "@/lib/review/logic";
import { previewReview } from "@/lib/review/preview";
import type { ReviewQueueItemView, ReviewService } from "@/lib/review/types";
import { asServiceError, type ServiceError } from "@/lib/service/errors";
import { ServiceNotice } from "../ServiceNotice";
import { AppShell, PreviewStrip, type Connection } from "../shell/AppShell";
import { useReadiness } from "../useReadiness";

export function queueHref(mode: "live" | "preview"): string {
  return mode === "preview" ? "/preview/review" : "/review";
}

export function reviewHref(mode: "live" | "preview", applicationId: string): string {
  return `${queueHref(mode)}/${encodeURIComponent(applicationId)}`;
}

export function reviewService(mode: "live" | "preview"): ReviewService {
  return mode === "preview" ? previewReview() : new HttpReviewService();
}

export const PREVIEW_REVIEW_NOTE =
  "Fictional prepared applications. Approving and changing answers affect this tab's copy only, and the preview never submits.";

/**
 * The Prepared queue: every application stopped at its final review step (or held
 * only by a step in the browser), newest first. Reading it changes nothing.
 */
export function ReviewQueue({ mode }: { mode: "live" | "preview" }) {
  const { readiness } = useReadiness(mode);
  const service = useMemo(() => reviewService(mode), [mode]);
  const [items, setItems] = useState<ReviewQueueItemView[] | null>(null);
  const [error, setError] = useState<ServiceError | null>(null);
  const [connection, setConnection] = useState<Connection>(mode === "preview" ? "connected" : "checking");
  const [filter, setFilter] = useState<QueueFilter>("all");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const view = await service.queue();
      setItems(listedQueue(view));
      setError(null);
      setConnection("connected");
    } catch (caught) {
      const serviceError = asServiceError(caught);
      setError(serviceError);
      setConnection(serviceError.code === "unavailable" ? "unavailable" : "connected");
    } finally {
      setLoading(false);
    }
  }, [service]);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = queueCounts(items ?? []);
  const visible = filterQueue(items ?? [], filter);

  return (
    <AppShell
      mode={mode}
      section="review"
      connection={connection}
      readiness={readiness}
      skipLabel="Skip to the prepared queue"
      previewBar={<PreviewStrip note={PREVIEW_REVIEW_NOTE} />}
      colophon="Reviewing changes nothing. Applications are submitted only from their review page, after you approve them and confirm."
    >
      <header className="page-head">
        <p className="eyebrow">Review and submit</p>
        <h1 className="display">Prepared queue</h1>
        <p className="lede">
          Applications stopped at the site&rsquo;s final review step, newest first. Nothing here has been submitted.
          Open one, check every answer, approve it, and submit it once submission is on.
        </p>
      </header>

      {items === null ? (
        error ? (
          <QueueProblem mode={mode} error={error} onRetry={load} />
        ) : (
          <p className="lede">Loading the prepared queue…</p>
        )
      ) : (
        <>
          {error && (
            <p className="form-alert" role="alert">
              Showing the last loaded queue. {error.message}
            </p>
          )}
          <div className="review-toolbar">
            <div className="review-filters" role="group" aria-label="Filter the queue">
              {QUEUE_FILTERS.map(({ id, label }) => (
                <button key={id} type="button" aria-pressed={filter === id} onClick={() => setFilter(id)}>
                  <span className="review-filters__count">{counts[id]}</span> <span>{label}</span>
                </button>
              ))}
            </div>
            <button type="button" className="chip-button" disabled={loading} onClick={() => void load()}>
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
          {filter !== "all" && (
            <p className="field__hint" role="status">
              Showing {visible.length} of {items.length}.
            </p>
          )}
          {items.length === 0 ? (
            <p className="empty-note">
              No prepared applications yet. An application appears here when a run stops at the site&rsquo;s final review
              step, whether it started on the desk or in <code>interviewmaxxing prepare-batch</code>.
            </p>
          ) : visible.length === 0 ? (
            <p className="empty-note">Nothing matches this filter.</p>
          ) : (
            <QueueTable mode={mode} items={visible} />
          )}
        </>
      )}
    </AppShell>
  );
}

function QueueProblem({ mode, error, onRetry }: { mode: "live" | "preview"; error: ServiceError; onRetry: () => Promise<void> }) {
  if (error.code === "unavailable") {
    return <ServiceNotice mode={mode} message={error.message} onRetry={onRetry} />;
  }
  if (error.code === "not_found") {
    // An older service without the review routes: say so quietly, no alarm.
    return (
      <section className="notice notice--quiet" aria-labelledby="review-queue-missing">
        <h2 id="review-queue-missing" className="notice__title">
          This service doesn&rsquo;t provide the review queue yet
        </h2>
        <p>
          Update the Interviewmaxxing service to review, approve and submit prepared applications here. The desk and the
          pipeline keep working.
        </p>
      </section>
    );
  }
  return (
    <section className="notice" role="alert" aria-labelledby="review-queue-failed">
      <h2 id="review-queue-failed" className="notice__title">
        The prepared queue couldn&rsquo;t be loaded
      </h2>
      <p>{error.message}</p>
      <div className="notice__actions">
        <button type="button" className="button button--secondary" onClick={() => void onRetry()}>
          Check again
        </button>
      </div>
    </section>
  );
}

/** Newest first: one row per application, stacked into cards on a narrow screen. */
export function QueueTable({ mode, items }: { mode: "live" | "preview"; items: readonly ReviewQueueItemView[] }) {
  return (
    <div className="review-queue">
      <table className="review-queue__table">
        <caption className="visually-hidden">Prepared applications, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Employer and role</th>
            <th scope="col">Backend</th>
            <th scope="col">Prepared</th>
            <th scope="col">AI cost</th>
            <th scope="col">Status</th>
            <th scope="col">
              <span className="visually-hidden">Open</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <QueueRow key={item.id} mode={mode} item={item} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function QueueRow({ mode, item }: { mode: "live" | "preview"; item: ReviewQueueItemView }) {
  const company = item.job.company ?? "Employer not reported";
  const title = item.job.title ?? "Role not reported";
  const when = item.stage === "prepared" ? (item.preparedAt ?? item.stoppedAt) : item.stoppedAt;
  return (
    <tr className={`review-queue__row tone-${holdTone(item.hold?.kind ?? "")}`} data-application-id={item.id}>
      <th scope="row" data-label="Employer and role">
        <span className="review-queue__company">{company}</span>
        <span className="review-queue__role">{title}</span>
      </th>
      <td data-label="Backend">{item.job.ats ?? <span className="is-unknown">Not identified</span>}</td>
      <td data-label={item.stage === "prepared" ? "Prepared" : "Stopped"}>
        {when ? <time dateTime={when}>{formatDateTime(when)}</time> : "—"}
        {item.stage !== "prepared" && <span className="review-queue__sub">Stopped, not prepared</span>}
      </td>
      <td data-label="AI cost" title={costText(item.providerCost)}>
        <span aria-hidden="true">{costShort(item.providerCost)}</span>
        <span className="visually-hidden">{costText(item.providerCost)}</span>
      </td>
      <td data-label="Status">
        <p className="review-queue__hold">{item.hold?.summary}</p>
        <div className="marks">
          {item.approved && <span className="mark mark--approved">Approved</span>}
          {item.captchaPending && <span className="mark mark--captcha">CAPTCHA to solve</span>}
          {item.stage === "browser_action" && <span className="mark mark--browser">In the browser</span>}
        </div>
      </td>
      <td className="review-queue__open">
        <Link
          className="chip-button chip-button--review"
          href={reviewHref(mode, item.id)}
          aria-label={`Review: ${company}, ${title}`}
        >
          Review
        </Link>
      </td>
    </tr>
  );
}
