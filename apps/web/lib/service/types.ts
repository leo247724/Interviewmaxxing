/**
 * Frontend service interface for the supplied-URL application flow.
 *
 * These are presentation view models, not canonical backend contracts. The
 * canonical models live in the core Python package (C1); F2 maps them onto
 * these shapes. State names mirror ARCHITECTURE.md section 6 so the mapping
 * stays one-to-one.
 */

export type ApplicationState =
  | "REQUESTED"
  | "INSPECTING"
  | "PACKET_READY"
  | "FILLING"
  | "NEEDS_INPUT"
  | "SUBMITTING"
  | "SUBMITTED"
  | "SUBMISSION_UNKNOWN"
  | "FAILED_RETRYABLE"
  | "FAILED_PERMANENT"
  | "DUPLICATE"
  | "WITHDRAWN";

export interface CandidateProfileInput {
  firstName: string;
  lastName: string;
  email: string;
  phone: string;
  location: string;
  linkedinUrl: string;
  websiteUrl: string;
}

export interface ResumeDocumentView {
  id: string;
  fileName: string;
  sizeBytes: number;
  uploadedAt: string;
}

export interface CandidateView {
  profile: CandidateProfileInput;
  resumes: ResumeDocumentView[];
  defaultResumeId: string | null;
}

export interface StartApplicationInput {
  applicationUrl: string;
  /** The profile the user confirmed on screen for this request. */
  profile: CandidateProfileInput;
  resumeId: string;
}

export interface JobIdentityView {
  title: string | null;
  company: string | null;
  /** Human-readable name of the application system, when identified. */
  ats: string | null;
}

export type EventTone = "info" | "progress" | "attention" | "success" | "warning" | "error";

export interface ApplicationEventView {
  id: string;
  /** Machine event name, e.g. `application.submitted`. */
  type: string;
  at: string;
  message: string;
  tone: EventTone;
}

export type QuestionControl =
  "text" | "long_text" | "email" | "tel" | "url" | "number" | "single_select" | "multi_select" | "boolean";

export interface QuestionOption {
  value: string;
  label: string;
}

export type AnswerValue = string | string[] | boolean | null;

export interface RequiredQuestionView {
  id: string;
  label: string;
  help: string | null;
  control: QuestionControl;
  required: boolean;
  /** The site's own options, verbatim. Null for free-entry controls. */
  options: QuestionOption[] | null;
  /** Previously saved answer, if any. Never a guess. */
  value: AnswerValue;
  maxLength: number | null;
  /** Why the system could not answer this itself. */
  reason: string | null;
}

export interface AttestationView {
  id: string;
  statement: string;
  required: boolean;
  /** True only if the user has already accepted this statement. */
  accepted: boolean;
}

export type InteractionKind = "SIGN_IN" | "CAPTCHA" | "VERIFICATION";

export type InputRequestView =
  | {
      kind: "questions";
      questions: RequiredQuestionView[];
      attestations: AttestationView[];
      savedAt: string | null;
      /** Validation errors reported by the service, keyed by question/attestation id. */
      errors: Record<string, string>;
    }
  | {
      kind: "interaction";
      interaction: InteractionKind;
      instructions: string;
      pageUrl: string | null;
    };

export type EvidenceKind = "screenshot" | "page_text" | "page_url" | "email" | "portal" | "user_report";

export interface EvidenceView {
  kind: EvidenceKind;
  label: string;
  /** Text excerpt, reference or note. */
  value: string | null;
  /** Link to an artifact (screenshot, page). */
  href: string | null;
  observedAt: string;
  source: "site" | "user";
}

/** How acceptance was established (S1 `ConfirmationMethod`). */
export type ConfirmationMethod =
  "SUBMISSION_OBSERVED" | "SITE_CONFIRMATION" | "ATS_CANDIDATE_PORTAL" | "CONFIRMATION_EMAIL" | "USER_CONFIRMED";

export interface SubmissionReceiptView {
  receiptId: string;
  submittedAt: string;
  confirmationReference: string | null;
  evidence: EvidenceView[];
  /**
   * How acceptance was established. Optional until every service version sends it;
   * when present it outranks anything inferred from the evidence list.
   */
  confirmationMethod?: ConfirmationMethod;
  /**
   * Who established it: "user" exactly when the method is USER_CONFIRMED, even if
   * older site artifacts (e.g. a screenshot of the uncertain page) are listed.
   */
  confirmationAuthority?: "site" | "user";
}

export interface PriorSubmissionView {
  applicationId: string;
  applicationUrl: string;
  submittedAt: string;
  confirmationReference: string | null;
}

export interface FailureView {
  reason: string;
  detail: string | null;
  retryable: boolean;
  evidence: EvidenceView[];
}

export interface UncertainSubmissionView {
  attemptedAt: string;
  reason: string;
  evidence: EvidenceView[];
  lastCheckedAt: string | null;
  lastCheckResult: string | null;
}

export interface ApplicationView {
  id: string;
  state: ApplicationState;
  applicationUrl: string;
  job: JobIdentityView;
  requestedAt: string;
  updatedAt: string;
  /** Which page of a multi-step form is being worked on, when known. */
  progress: { page: number; pageCount: number | null } | null;
  resumeFileName: string | null;
  needs: InputRequestView | null;
  receipt: SubmissionReceiptView | null;
  prior: PriorSubmissionView | null;
  failure: FailureView | null;
  uncertain: UncertainSubmissionView | null;
  events: ApplicationEventView[];
}

export interface AnswerInput {
  answers: Record<string, AnswerValue>;
  attestations: Record<string, boolean>;
}

export type ReconcileInput =
  | { kind: "recheck" }
  | {
      kind: "user_found_confirmation";
      foundIn: "email" | "portal" | "other";
      reference: string | null;
      note: string | null;
    }
  | { kind: "user_confirmed_not_received" };

export type ServiceMode = "live" | "preview";

export interface ApplicationService {
  readonly mode: ServiceMode;
  /** Saved profile and resumes. */
  getCandidate(): Promise<CandidateView>;
  uploadResume(file: File): Promise<ResumeDocumentView>;
  /** Record the request. The request itself authorizes submission. */
  start(input: StartApplicationInput): Promise<ApplicationView>;
  status(applicationId: string): Promise<ApplicationView>;
  /** Save answers and attestations without continuing. */
  answer(applicationId: string, input: AnswerInput): Promise<ApplicationView>;
  /** Continue after answers, a sign-in/CAPTCHA, or a retryable failure. */
  resume(applicationId: string): Promise<ApplicationView>;
  /** Settle an uncertain submission. Never a blind retry. */
  reconcile(applicationId: string, input: ReconcileInput): Promise<ApplicationView>;
}
