import type { PipelineEntryView } from "@/lib/pipeline/types";
import { formatDate } from "@/lib/format";

/**
 * Three kinds of marks, kept visually distinct: the user's own tracking origin,
 * a Jev decision, and an application receipt (only from a linked application).
 */
export function EntryBadges({ entry }: { entry: PipelineEntryView }) {
  const application = entry.application;
  const reported = application?.confirmationAuthority === "user" || application?.confirmationMethod === "USER_CONFIRMED";
  const confirmed = !reported && (application?.confirmationAuthority === "site" || ["SUBMISSION_OBSERVED", "SITE_CONFIRMATION", "ATS_CANDIDATE_PORTAL", "CONFIRMATION_EMAIL"].includes(application?.confirmationMethod ?? ""));
  return (
    <ul className="marks" aria-label="Record details">
      {application?.state === "SUBMITTED" ? (
        <li className={`mark ${confirmed ? "mark--receipt" : "mark--application"}`}>
          {reported ? "Submitted · your report" : confirmed ? "Receipt confirmed" : "Submitted · confirmation type not recorded"}
          {application.submittedAt ? ` · ${formatDate(application.submittedAt)}` : ""}
        </li>
      ) : application ? (
        <li className="mark mark--application">Application: {applicationLabel(application.state)}</li>
      ) : null}
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
