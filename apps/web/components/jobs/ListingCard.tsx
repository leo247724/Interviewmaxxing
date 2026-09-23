"use client";

import { useId } from "react";
import { SOURCE_LABELS, type ListingView, type SelectionView } from "@/lib/jobs/types";
import { formatDateTime } from "@/lib/format";

const ARRANGEMENT: Record<ListingView["workArrangement"], string> = {
  ONSITE: "Onsite",
  HYBRID: "Hybrid",
  REMOTE: "Remote",
  UNKNOWN: "Arrangement not stated",
};

const HOLD_LABELS: Record<string, string> = {
  MISSING_PROFILE: "Profile incomplete",
  INSUFFICIENT_EVIDENCE: "Not enough evidence",
  PROVIDER_ERROR: "Decision service error",
  LOW_CONFIDENCE: "Low confidence",
  HARD_CONSTRAINT: "Breaks a preference",
  UNKNOWN_COMPENSATION: "Pay unknown",
  LISTING_CLOSED: "Listing closed",
  DUPLICATE_APPLICATION: "Already applied",
};

function payText(listing: ListingView): { text: string; unknown: boolean } {
  const pay = listing.compensation;
  if (!pay || (!pay.rawText && pay.minimum === null && pay.maximum === null))
    return { text: "Pay not stated", unknown: true };
  if (pay.minimum !== null || pay.maximum !== null) {
    const fmt = (value: number) => value.toLocaleString("en-US");
    const range =
      pay.minimum !== null && pay.maximum !== null && pay.minimum !== pay.maximum
        ? `${fmt(pay.minimum)}–${fmt(pay.maximum)}`
        : fmt((pay.minimum ?? pay.maximum)!);
    const period = pay.period ? ` / ${pay.period.toLowerCase()}` : "";
    return { text: `${pay.currency ?? ""} ${range}${period}`.trim(), unknown: false };
  }
  return { text: `“${pay.rawText}” (no amount stated)`, unknown: true };
}

export function applicationUrlOf(listing: ListingView) {
  return listing.provenance.find((source) => source.applicationUrl)?.applicationUrl ?? null;
}

export function ListingCard({
  listing,
  tierLabel,
  tierUnknown,
  busy,
  pipelineHref,
  onDecide,
  onTrack,
  onApply,
}: {
  listing: ListingView;
  /** Which location group the listing was ranked into. */
  tierLabel: string;
  tierUnknown: boolean;
  busy: "decide" | "track" | null;
  pipelineHref: string;
  onDecide: () => void;
  onTrack: () => void;
  onApply: () => void;
}) {
  const titleId = useId();
  const pay = payText(listing);
  const closed = listing.status === "CLOSED";

  return (
    <article className={`listing${closed ? " is-closed" : ""}`} aria-labelledby={titleId}>
      <header className="listing__head">
        <h3 id={titleId} className="listing__title">
          {listing.title}
        </h3>
        <p className="listing__company">{listing.company ?? "Company not stated"}</p>
        <p className={`listing__tier${tierUnknown ? " is-unknown" : ""}`}>Location match: {tierLabel}</p>
        {closed && <p className="listing__closed">Closed on the source · no longer accepting applications</p>}
      </header>

      <dl className="listing__facts">
        <div>
          <dt>Where</dt>
          <dd className={listing.location ? undefined : "is-unknown"}>{listing.location ?? "Location not stated"}</dd>
        </div>
        <div>
          <dt>Arrangement</dt>
          <dd className={listing.workArrangement === "UNKNOWN" ? "is-unknown" : undefined}>
            {ARRANGEMENT[listing.workArrangement]}
            {listing.workArrangement === "REMOTE" &&
              (listing.remoteEligibility ? ` · open to ${listing.remoteEligibility}` : " · eligibility not stated")}
          </dd>
        </div>
        <div>
          <dt>Pay</dt>
          <dd className={pay.unknown ? "is-unknown" : undefined}>
            {pay.text}
            {!pay.unknown && listing.compensation?.rawText && (
              <span className="listing__raw"> as posted: “{listing.compensation.rawText}”</span>
            )}
          </dd>
        </div>
        {listing.postedText && (
          <div>
            <dt>Posted</dt>
            <dd>{listing.postedText}</dd>
          </div>
        )}
      </dl>

      {listing.description && (
        <details className="listing__description">
          <summary>
            {listing.descriptionCompleteness === "FULL" ? "Description" : "Description excerpt"}
            {listing.descriptionCompleteness === "PARTIAL" && <span className="tag"> partial</span>}
          </summary>
          <p>{listing.description}</p>
        </details>
      )}

      <div className="listing__sources">
        <p className="listing__sources-label">Seen on</p>
        <ul>
          {listing.provenance.map((source, index) => (
            <li key={`${source.source}-${index}`}>
              <a href={source.sourceUrl} target="_blank" rel="noreferrer noopener">
                {SOURCE_LABELS[source.source] ?? source.source}
                <span className="visually-hidden"> listing (opens in a new tab)</span>
              </a>
            </li>
          ))}
          {applicationUrlOf(listing) && (
            <li>
              <a href={applicationUrlOf(listing)!} target="_blank" rel="noreferrer noopener">
                Application page<span className="visually-hidden"> (opens in a new tab)</span>
              </a>
            </li>
          )}
        </ul>
        <p className="listing__observed">Observed {formatDateTime(listing.observedAt)}</p>
      </div>

      <Decision selection={listing.selection} />

      <div className="listing__actions">
        {!closed && (
          <button type="button" className="button button--secondary" disabled={busy !== null} onClick={onDecide}>
            {busy === "decide"
              ? "Asking Jev…"
              : listing.selection
                ? listing.selection.stale
                  ? "Ask Jev again (preferences changed)"
                  : "Ask Jev again"
                : "Ask Jev"}
          </button>
        )}
        {listing.pipelineEntryId ? (
          <a className="text-link" href={pipelineHref}>
            In your pipeline
          </a>
        ) : (
          <button type="button" className="button button--secondary" disabled={busy !== null} onClick={onTrack}>
            {busy === "track" ? "Adding…" : "Track in pipeline"}
          </button>
        )}
        {!closed && !listing.applicationId && (
          <button type="button" className="button button--primary" disabled={busy !== null} onClick={onApply}>
            Apply…<span className="visually-hidden"> to {listing.title}</span>
          </button>
        )}
        {listing.applicationId && <span className="mark mark--application">Application started</span>}
      </div>
    </article>
  );
}

function Decision({ selection }: { selection: SelectionView | null }) {
  if (!selection) {
    return <p className="decision decision--none">No Jev decision yet.</p>;
  }
  const effective = selection.effectiveChoice;
  const overridden = selection.modelChoice !== null && selection.modelChoice !== effective;
  const percent = (value: number) => `${Math.round(value * 100)}%`;

  return (
    <section className={`decision choice-${effective.toLowerCase()}`} aria-label="Jev decision">
      <p className="decision__head">
        <span className="decision__choice">{effective}</span>
        <span className="decision__label">
          {effective === "APPLY"
            ? "Jev recommends applying"
            : effective === "SKIP"
              ? "Not recommended"
              : "Held for your review"}
          {selection.confidence !== null && !overridden && ` · confidence ${percent(selection.confidence)}`}
        </span>
      </p>
      {selection.stale && (
        <p className="decision__stale">
          Your preferences or this listing changed after this decision. Ask again to update it.
        </p>
      )}
      {overridden && (
        <p className="decision__note">
          Jev chose {selection.modelChoice}
          {selection.confidence !== null && ` (confidence ${percent(selection.confidence)})`}, but a rule changed the
          outcome:
        </p>
      )}
      {selection.holds.length > 0 && (
        <ul className="decision__holds">
          {selection.holds.map((hold, index) => (
            <li key={index}>
              <strong>{HOLD_LABELS[hold.code] ?? hold.code}:</strong> {hold.detail}
            </li>
          ))}
        </ul>
      )}
      {selection.reasons.length > 0 && (
        <ul className="decision__reasons">
          {selection.reasons.map((reason, index) => (
            <li key={index}>{reason}</li>
          ))}
        </ul>
      )}
      {selection.unresolved.length > 0 && (
        <div className="decision__unresolved">
          <p>Not established</p>
          <ul>
            {selection.unresolved.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      )}
      {selection.providerError && (
        <p className="decision__error">
          Decision service: {selection.providerError.message}
          {selection.providerError.retryable ? " You can ask again." : ""}
        </p>
      )}
      <p className="decision__meta">
        {selection.probabilities &&
          (["APPLY", "REVIEW", "SKIP"] as const)
            .filter((choice) => selection.probabilities?.[choice] !== undefined)
            .map((choice) => `${choice} ${percent(selection.probabilities![choice]!)}`)
            .join(" · ")}
        {selection.probabilities && " · "}
        {selection.returnedModel ?? selection.requestedModel} · rubric {selection.rubricVersion} ·{" "}
        {formatDateTime(selection.decidedAt)}
      </p>
    </section>
  );
}
