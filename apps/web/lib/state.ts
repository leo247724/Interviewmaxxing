import type { ApplicationState, ApplicationView } from "./service/types";
import { receiptAuthority } from "./receipt";

/** States in which the service is working and the desk should keep polling. */
export const ACTIVE_STATES: ReadonlySet<ApplicationState> = new Set([
  "REQUESTED",
  "INSPECTING",
  "PACKET_READY",
  "FILLING",
  "SUBMITTING",
]);

export function isActive(state: ApplicationState) {
  return ACTIVE_STATES.has(state);
}

export const RAIL_STEPS = [
  { key: "requested", label: "Requested" },
  { key: "reading", label: "Reading form" },
  { key: "filling", label: "Filling" },
  { key: "submitting", label: "Submitting" },
  { key: "confirmed", label: "Confirmed" },
] as const;

/** Index of the rail step the application has reached; -1 when off the main track. */
export function railIndex(view: ApplicationView): number {
  switch (view.state) {
    case "REQUESTED":
      return 0;
    case "INSPECTING":
    case "PACKET_READY":
      return 1;
    case "FILLING":
      return 2;
    case "NEEDS_INPUT":
      return view.needs?.kind === "interaction" && !view.progress ? 1 : 2;
    case "SUBMITTING":
    case "SUBMISSION_UNKNOWN":
      return 3;
    case "SUBMITTED":
      return 4;
    case "FAILED_RETRYABLE":
    case "FAILED_PERMANENT":
      return view.progress ? 2 : 1;
    case "DUPLICATE":
      return 1;
    default:
      return -1;
  }
}

export type Mood = "working" | "attention" | "success" | "caution" | "failure" | "neutral";

export function describe(view: ApplicationView): { headline: string; mood: Mood } {
  const page = view.progress;
  switch (view.state) {
    case "REQUESTED":
      return { headline: "Request recorded", mood: "working" };
    case "INSPECTING":
      return {
        headline: view.job.title ? "Reading the application form" : "Opening the application page",
        mood: "working",
      };
    case "PACKET_READY":
      return { headline: "Answers prepared from your profile", mood: "working" };
    case "FILLING":
      return {
        headline: page
          ? `Filling page ${page.page}${page.pageCount ? ` of ${page.pageCount}` : ""}`
          : "Filling the form",
        mood: "working",
      };
    case "SUBMITTING":
      return { headline: "Submitting", mood: "working" };
    case "NEEDS_INPUT":
      if (view.needs?.kind === "interaction") {
        return {
          headline:
            view.needs.interaction === "SIGN_IN"
              ? "Sign in to continue"
              : view.needs.interaction === "CAPTCHA"
                ? "Complete the CAPTCHA to continue"
                : "Verification needed to continue",
          mood: "attention",
        };
      }
      if (view.needs?.kind === "questions") {
        const answers = view.needs.questions.filter((question) => question.required).length;
        const statements = view.needs.attestations.filter((item) => item.required).length;
        const parts = [
          answers ? `${answers} ${answers === 1 ? "answer" : "answers"}` : null,
          statements ? `${statements} ${statements === 1 ? "statement" : "statements"}` : null,
        ].filter(Boolean);
        return { headline: `${parts.join(" and ") || "Your input"} needed from you`, mood: "attention" };
      }
      return { headline: "Waiting for you", mood: "attention" };
    case "SUBMITTED":
      return view.receipt && receiptAuthority(view.receipt).byUser
        ? { headline: "Submitted, on your report", mood: "success" }
        : { headline: "Submitted and confirmed", mood: "success" };
    case "SUBMISSION_UNKNOWN":
      return { headline: "Submission not confirmed", mood: "caution" };
    case "FAILED_RETRYABLE":
      return { headline: "Stopped before submitting", mood: "failure" };
    case "FAILED_PERMANENT":
      return { headline: "This application can't be completed", mood: "failure" };
    case "DUPLICATE":
      return { headline: "You've already applied to this job", mood: "neutral" };
    case "WITHDRAWN":
      return { headline: "Withdrawn", mood: "neutral" };
  }
}
