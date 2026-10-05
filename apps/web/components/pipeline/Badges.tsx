import type { PipelineEntryView } from "@/lib/pipeline/types";
import { preparedLabel } from "@/lib/pipeline/prepared";
import type { ApplicationSummaryView } from "@/lib/service/types";
import { formatDate } from "@/lib/format";
import { autoApplyLine } from "@/lib/pipeline/filters";

/**
 * Three kinds of marks, kept visually distinct: the user's own tracking origin,
 * a Jev decision, and an application receipt (only from a linked application).
 * A prepared application (filled, stopped before submitting) gets its own mark;
 * it is never a receipt.
 */
export function EntryBadges({
  entry,
  prepared = null,
}: {
  entry: PipelineEntryView;
  /** The prepared application this card points at, if any. */
  prepared?: Pick<ApplicationSummaryView, "id" | "preparation"> | null;
}) {
  const application = entry.application;
  const reported = application?.confirmationAuthority === "user" || application?.confirmationMethod === "USER_CONFIRMED";
  const confirmed = !reported && (application?.confirmationAuthority === "site" || ["SUBMISSION_OBSERVED", "SITE_CONFIRMATION", "ATS_CANDIDATE_PORTAL", "CONFIRMATION_EMAIL"].includes(application?.confirmationMethod ?? ""));
  const submitted = application?.state === "SUBMITTED";
  return (
    <ul className="marks" aria-label="Record details">
      {submitted ? (
        <li className={`mark ${confirmed ? "mark--receipt" : "mark--application"}`}>
          {reported ? "Submitted · your report" : confirmed ? "Receipt confirmed" : "Submitted · confirmation type not recorded"}
          {application.submittedAt ? ` · ${formatDate(application.submittedAt)}` : ""}
        </li>
      ) : (
        <>
          {prepared && <li className="mark mark--prepared">{preparedLabel(prepared)}</li>}
          {application && (!prepared || application.applicationId !== prepared.id) && (
            <li className="mark mark--application">Application: {applicationLabel(application.state)}</li>
          )}
        </>
      )}
      {entry.selection && (
        <li className={`mark mark--jev choice-${entry.selection.effectiveChoice.toLowerCase()}`}>
          Jev: {entry.selection.effectiveChoice}
        </li>
      )}
      <li className="mark mark--origin">
        {entry.origin === "import" ? "Imported" : entry.origin === "jobs" ? "From job search" : "Added by you"}
      </li>
    </ul>
  );
}

export function applicationLabel(state: string) {
  switch (state) {
    case "SUBMITTED":
      return "submitted";
    case "SUBMISSION_UNKNOWN":
      return "not confirmed";
    case "NEEDS_INPUT":
      return "waiting for you";
    case "DUPLICATE":
      return "already applied";
    case "FAILED_RETRYABLE":
    case "FAILED_PERMANENT":
      return "stopped";
    default:
      return "in progress";
  }
}

/**
 * The autonomous-apply marks: the backend on every card, and, for Saved cards,
 * where the apply stands and what holds it up (the detail as a one-line excerpt
 * with the full text in the tooltip). Cards without autoApply show nothing.
 */
export function AutoApplyMarks({ entry, showStatus }: { entry: PipelineEntryView; showStatus: boolean }) {
  const autoApply = entry.autoApply;
  if (!autoApply) return null;
  const line = autoApplyLine(autoApply);
  return (
    <div className="auto-apply">
      <ul className="marks" aria-label="Autonomous apply">
        <li className="mark mark--backend" title={`Apply backend: ${autoApply.backendLabel}`}>
          {autoApply.backendLabel}
        </li>
      </ul>
      {showStatus && (
        <p className={`auto-apply__status auto-apply__status--${autoApply.status}`} title={autoApply.detail ?? line}>
          <span className="auto-apply__line">{line}</span>
          {autoApply.detail && <span className="auto-apply__detail">{autoApply.detail}</span>}
        </p>
      )}
    </div>
  );
}
