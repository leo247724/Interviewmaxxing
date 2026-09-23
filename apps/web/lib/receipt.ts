import type { ConfirmationMethod, SubmissionReceiptView } from "./service/types";

export interface ReceiptAuthority {
  byUser: boolean;
  method: ConfirmationMethod | null;
  /** Plain-language description of how the submission was confirmed. */
  description: string;
}

const METHOD_TEXT: Record<ConfirmationMethod, string> = {
  SUBMISSION_OBSERVED: "The site showed a confirmation right after the application was submitted.",
  SITE_CONFIRMATION: "A later check of the site found its confirmation.",
  ATS_CANDIDATE_PORTAL: "A later check of the applicant portal found the application.",
  CONFIRMATION_EMAIL: "A confirmation email from the employer was found.",
  USER_CONFIRMED: "You reported that the employer confirmed it. The site itself wasn't seen confirming it.",
};

/**
 * Decide who established a receipt. The service's explicit method/authority wins.
 * Receipts from services that don't send them yet fall back to the evidence: they
 * count as user-reported when any user statement is present, so a user report is
 * never shown as site-confirmed just because an older screenshot is also listed.
 */
export function receiptAuthority(receipt: SubmissionReceiptView): ReceiptAuthority {
  const method = receipt.confirmationMethod ?? null;
  const byUser =
    receipt.confirmationAuthority !== undefined
      ? receipt.confirmationAuthority === "user"
      : method !== null
        ? method === "USER_CONFIRMED"
        : receipt.evidence.some((item) => item.source === "user" || item.kind === "user_report");
  const description = method
    ? METHOD_TEXT[method]
    : byUser
      ? METHOD_TEXT.USER_CONFIRMED
      : "Confirmed from the site evidence below.";
  return { byUser: byUser || method === "USER_CONFIRMED", method, description };
}
