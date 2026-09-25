/**
 * The review lane: the Prepared queue, the review page of one application, approve
 * and submit. These mirror the service's presentation models
 * (`apps/service/src/interviewmaxxing_service/models.py`, "the review lane"),
 * field for field, in camelCase. Canonical state stays in the service; nothing here
 * is decided by the dashboard.
 */

import type {
  AnswerInput,
  AnswerValue,
  ApplicationState,
  ApplicationView,
  JobIdentityView,
  QuestionControl,
  QuestionOption,
  ReviewControl,
} from "../service/types";

/** How far a changed answer may be reused: this application only, this job, or every application. */
export type ReuseChoice = "application" | "job" | "global";

/**
 * Where an application stands for the review lane: `prepared` (stopped at the final
 * review step, nothing submitted), `browser_action` (held only by a sign-in, a CAPTCHA
 * or another step the person does in the browser) or `other`.
 */
export type ReviewStage = "prepared" | "browser_action" | "other";

export type HoldKind = "ready" | "approved" | "edited" | "questions" | "sign_in" | "captcha" | "browser_action";

/** Where one answer on the prepared form came from. */
export type ProvenanceKind =
  | "identity" // your verified contact details
  | "resume" // the resume file pinned to the application
  | "saved_answer" // an answer you saved for this question
  | "saved_policy" // a standing answer rule (a policy, or an answer saved for every application)
  | "derived" // derived from a saved answer (salary, work authorization status, start date)
  | "fact_screener" // answered from verified facts that state it
  | "narrative" // written from your facts and stories, with citations
  | "user" // your own answer for this application
  | "blank"; // left blank

/** AI provider usage recorded for an application (every `provider.budget` event). */
export interface ProviderCostView {
  knownUsd: number;
  calls: number;
  /** Calls whose cost the provider did not report (not in `knownUsd`). */
  unknownCostCalls: number;
}

export interface HoldView {
  kind: HoldKind;
  /** One plain line, composed by the service: what the application waits for. */
  summary: string;
}

/** One application in the Prepared queue (`GET /review`). */
export interface ReviewQueueItemView {
  id: string;
  state: ApplicationState;
  stage: ReviewStage;
  applicationUrl: string;
  job: JobIdentityView;
  /** When the preparation behind a `prepared` stop was recorded. */
  preparedAt: string | null;
  /** When the current stop was recorded (the queue's order, newest first). */
  stoppedAt: string;
  captchaPending: boolean;
  providerCost: ProviderCostView | null;
  hold: HoldView;
  /** A valid approval of this preparation exists. */
  approved: boolean;
}

export interface ReviewQueueView {
  applications: ReviewQueueItemView[];
}

export interface ReviewProvenanceView {
  kind: ProvenanceKind;
  label: string;
  /** The resolver's own note (what it derived the answer from), when it recorded one. */
  detail: string | null;
}

export interface ReviewCitationsView {
  /** Candidate fact ids the answer cites. */
  facts: string[];
  /** Story passage (chunk) ids a narrative cites. */
  passages: string[];
  /** Job-description evidence ids a narrative cites (context, not candidate facts). */
  jobEvidence: string[];
}

/**
 * How an answer can be changed through `POST /applications/{id}/answers`: the answer
 * goes under the row's `questionId` (in `attestations` when `attestation`), with one of
 * `reuse` as its scope; `resume` then prepares the application again.
 */
export interface ReviewEditView {
  control: QuestionControl;
  options: QuestionOption[] | null;
  lookup: boolean;
  attestation: boolean;
  required: boolean;
  /** The current answer in the form's own terms (option values, a boolean, text). */
  value: string | string[] | boolean | null;
  reuse: ReuseChoice[];
  note: string | null;
}

/** One question of the prepared form, in form order: its answer, or a blank. */
export interface ReviewRowView {
  /** The question's id for an edit; null when it can't be edited here. */
  questionId: string | null;
  question: string;
  wordingRecorded: boolean;
  /** Form page, 1-based. */
  page: number;
  control: ReviewControl;
  /** Text, option label(s), "Yes"/"No" or a file name; null when the field was left blank. */
  value: string | string[] | null;
  required: boolean | null;
  provenance: ReviewProvenanceView;
  citations: ReviewCitationsView | null;
  confidence: number | null;
  edit: ReviewEditView | null;
  /** Why the answer can't be changed here, when `edit` is null. */
  noEditReason: string | null;
}

export interface ApprovalView {
  packetId: string;
  approvedAt: string;
  approver: string;
  /** Form pages the approval pins (one packet each). */
  pages: number;
}

export interface SubmitReadinessView {
  /** Everything but the person's confirmation is in place. */
  allowed: boolean;
  /** What is missing, in plain words; empty when `allowed`. */
  problems: string[];
  /** The service was started with IMX_ALLOW_SUBMISSION=1. */
  enabled: boolean;
  /** The submission run opens a visible browser window (the person can act in it). */
  opensBrowser: boolean;
  /** The command-line equivalent. */
  command: string;
}

export interface BrowserActionView {
  /** `POST /applications/{id}/resume` opens a visible browser window for this application now. */
  available: boolean;
  reason: string | null;
  /** `interviewmaxxing resume APP --act`, to run in a terminal instead. */
  command: string;
}

/** `GET /applications/{id}/review`: the desk's view plus what the review lane needs. */
export interface ApplicationReviewView {
  application: ApplicationView;
  stage: ReviewStage;
  /** The packet an approval pins now; null unless stopped at a completed preparation. */
  preparedPacketId: string | null;
  approval: ApprovalView | null;
  /** Answers were saved after this preparation: prepare it again before approving. */
  changedSincePreparation: boolean;
  providerCost: ProviderCostView | null;
  answers: ReviewRowView[];
  /** Why answers can't be edited at all (for example an older preparation), or null. */
  editNote: string | null;
  submit: SubmitReadinessView;
  browser: BrowserActionView;
}

export interface ApproveInput {
  /** The packet the person reviewed (`preparedPacketId`); anything else is refused. */
  packetId: string;
}

export interface SubmitInput {
  /** The approved packet the person confirmed (`approval.packetId`). */
  packetId: string;
  /** The person's explicit confirmation; nothing else submits. */
  confirm: true;
}

export interface ReviewService {
  readonly mode: "live" | "preview";
  /** Every application at its final review step or held only by a browser action, newest first. */
  queue(): Promise<ReviewQueueView>;
  review(applicationId: string): Promise<ApplicationReviewView>;
  /** Approve the reviewed packet. Submits nothing. */
  approve(applicationId: string, input: ApproveInput): Promise<ApplicationReviewView>;
  /** Submit exactly the approved packet, only with the person's confirmation. */
  submit(applicationId: string, input: SubmitInput): Promise<ApplicationReviewView>;
  /**
   * Save changed answers through the answers route (with reuse scopes). An answer the
   * service refuses comes back as `invalid` with `fieldErrors` keyed by question id.
   */
  editAnswer(applicationId: string, input: AnswerInput): Promise<ApplicationView>;
  /** Prepare again (the resume route): re-read and fill the form, stop at the review step. */
  prepareAgain(applicationId: string): Promise<ApplicationView>;
  /** Answer the questions that remain (the answers route, as on the desk). */
  answer(applicationId: string, input: AnswerInput): Promise<ApplicationView>;
}

export type { AnswerValue };
