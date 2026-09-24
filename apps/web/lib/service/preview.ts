import { isEmptyAnswer, validateAnswers, validateApplyForm } from "../validation";
import { ServiceError } from "./errors";
import type {
  AnswerInput,
  ApplicationListView,
  ApplicationSummaryView,
  CandidateProfileInput,
  ConfirmationMethod,
  ApplicationService,
  ApplicationState,
  ApplicationView,
  CandidateView,
  EventTone,
  EvidenceView,
  JobIdentityView,
  ReconcileInput,
  RequiredQuestionView,
  ResumeDocumentView,
  ReviewAnswerView,
  ReviewControl,
  StartApplicationInput,
} from "./types";

/**
 * In-memory fixture implementation used only by the /preview route and tests.
 * Everything here is fictional: no network requests are made and nothing is
 * submitted anywhere. Each scenario scripts the states a real backend reports.
 */

export const PREVIEW_SCENARIOS = [
  {
    id: "straight",
    label: "Confirmed submission",
    summary: "Fills two pages, submits and observes the site's confirmation.",
  },
  {
    id: "prepared",
    label: "Prepared for review",
    summary: "Fills both pages and stops at the final review step. Nothing is submitted.",
  },
  {
    id: "lookup",
    label: "Location lookup",
    summary:
      "The site offers several matching places for the location question. Pick one or enter a different value; the form is then prepared for review.",
  },
  {
    id: "questions",
    label: "Required questions",
    summary: "Pauses for questions and statements the profile can't answer.",
  },
  { id: "sign_in", label: "Sign-in required", summary: "The site asks you to sign in before it shows the form." },
  { id: "captcha", label: "CAPTCHA", summary: "The site shows a CAPTCHA before accepting the application." },
  { id: "duplicate", label: "Already applied", summary: "A confirmed submission for this job already exists." },
  { id: "uncertain", label: "Unconfirmed submission", summary: "Submit was pressed but no confirmation appeared." },
  {
    id: "connection_drop",
    label: "Connection drops while submitting",
    summary: "Status is briefly unreachable during submission, then recovers.",
  },
  {
    id: "failure_retryable",
    label: "Site error (retryable)",
    summary: "The site fails before submission; trying again is safe.",
  },
  { id: "failure_permanent", label: "Posting closed", summary: "The job no longer accepts applications." },
  { id: "unavailable", label: "Service unavailable", summary: "The application service can't be reached at all." },
] as const;

export type PreviewScenarioId = (typeof PREVIEW_SCENARIOS)[number]["id"];

export function isPreviewScenario(value: unknown): value is PreviewScenarioId {
  return PREVIEW_SCENARIOS.some((scenario) => scenario.id === value);
}

/** The seeded, already-prepared application every preview session starts with. */
export const PREPARED_PREVIEW_ID = "pv_prepared_northwind";
/** The preview pipeline card that points at it (unlinked; matched through `list()`). */
export const PREPARED_PREVIEW_PIPELINE_ENTRY_ID = "pipe_pv_northwind";

const PREPARED_PREVIEW_URL = "https://jobs.example.test/northwind/senior-lifecycle-marketer";
const PREPARED_REVIEW_PAGE = "https://jobs.example.test/northwind/senior-lifecycle-marketer/apply/review";

/** Scenarios that prepare the form and stop at the final review step. They never submit. */
function preparesOnly(scenario: PreviewScenarioId) {
  return scenario === "prepared" || scenario === "lookup";
}

const JOBS: Record<"northwind" | "halcyon" | "juniper", JobIdentityView> = {
  northwind: { title: "Senior Lifecycle Marketer", company: "Northwind Cartography", ats: "Greenhouse" },
  halcyon: { title: "Growth Operations Lead", company: "Halcyon Freight", ats: "Workday" },
  juniper: { title: "Paid Media Manager", company: "Juniper & Vale Studio", ats: "Lever" },
};

function jobFor(scenario: PreviewScenarioId): JobIdentityView {
  if (scenario === "sign_in" || scenario === "failure_permanent") return JOBS.halcyon;
  if (scenario === "uncertain" || scenario === "captcha") return JOBS.juniper;
  return JOBS.northwind;
}

type Step = (this: PreviewApplicationService, record: PreviewRecord) => void;

interface PreviewRecord {
  view: ApplicationView;
  scenario: PreviewScenarioId;
  queue: Step[];
  lastAdvance: number;
  rechecks: number;
  statusFailuresLeft: number;
  /** The profile confirmed for this application. */
  profile: CandidateProfileInput;
  /** Stop at the final review step instead of submitting. */
  prepareOnly: boolean;
  /** The prepared form still shows a CAPTCHA to solve in the browser. */
  captchaPending: boolean;
  /** Answers the person gave for this application, shown in the review list. */
  ownAnswers: ReviewAnswerView[];
}

export interface PreviewServiceOptions {
  scenario?: PreviewScenarioId;
  /** Minimum time between scripted steps. */
  stepDelayMs?: number;
  now?: () => Date;
}

export class PreviewApplicationService implements ApplicationService {
  readonly mode = "preview" as const;
  scenario: PreviewScenarioId;
  private readonly stepDelayMs: number;
  private now: () => Date;
  private readonly records = new Map<string, PreviewRecord>();
  private counter = 0;
  private candidate: CandidateView;

  constructor(options: PreviewServiceOptions = {}) {
    this.scenario = options.scenario ?? "straight";
    this.stepDelayMs = options.stepDelayMs ?? 750;
    this.now = options.now ?? (() => new Date());
    this.candidate = {
      profile: {
        firstName: "Robin",
        lastName: "Vale",
        email: "robin.vale@example.test",
        phone: "",
        location: "Portland, OR",
        linkedinUrl: "",
        websiteUrl: "https://robinvale.example.test",
      },
      resumes: [
        {
          id: "res_pv_lifecycle",
          fileName: "Robin_Vale_Resume_Lifecycle.pdf",
          sizeBytes: 188_416,
          uploadedAt: "2026-09-12T17:20:00Z",
        },
        {
          id: "res_pv_general",
          fileName: "Robin_Vale_Resume_2026.pdf",
          sizeBytes: 171_008,
          uploadedAt: "2026-08-30T09:05:00Z",
        },
      ],
      defaultResumeId: "res_pv_lifecycle",
    };
    this.records.set(PREPARED_PREVIEW_ID, this.seedPrepared());
  }

  /**
   * The seeded application: run the prepared script once on a fixed clock so it
   * matches what the `prepared` scenario produces, with a CAPTCHA still pending.
   */
  private seedPrepared(): PreviewRecord {
    const realNow = this.now;
    let at = Date.parse("2026-09-23T15:02:04Z");
    this.now = () => new Date(at);
    try {
      const requestedAt = this.iso();
      const record: PreviewRecord = {
        view: {
          id: PREPARED_PREVIEW_ID,
          state: "REQUESTED",
          applicationUrl: PREPARED_PREVIEW_URL,
          job: { title: null, company: null, ats: null },
          requestedAt,
          updatedAt: requestedAt,
          progress: null,
          resumeFileName: "Robin_Vale_Resume_Lifecycle.pdf",
          needs: null,
          receipt: null,
          prior: null,
          failure: null,
          uncertain: null,
          events: [],
          preparation: null,
          review: [],
        },
        scenario: "prepared",
        queue: [],
        lastAdvance: 0,
        rechecks: 0,
        statusFailuresLeft: 0,
        profile: { ...this.candidate.profile },
        prepareOnly: true,
        captchaPending: true,
        ownAnswers: [],
      };
      this.event(record, "application.requested", REQUESTED_TO_PREPARE, "info");
      // Seconds each scripted step took on the fictional run.
      const steps: [number, Step][] = [
        [5, opened],
        [2, identified],
        [4, packetReady],
        [2, fillPage(1, 2)],
        [82, fillPage(2, 2)],
        [180, prepareForReview],
      ];
      for (const [seconds, step] of steps) {
        at += seconds * 1000;
        step.call(this, record);
      }
      return record;
    } finally {
      this.now = realNow;
    }
  }

  async getCandidate(): Promise<CandidateView> {
    this.guardAvailable();
    return structuredClone(this.candidate);
  }

  async uploadResume(file: File): Promise<ResumeDocumentView> {
    this.guardAvailable();
    const lower = file.name.toLowerCase();
    if (!lower.endsWith(".pdf") && !lower.endsWith(".docx") && !lower.endsWith(".doc")) {
      throw new ServiceError("invalid", "Upload a PDF or Word document.", {
        resumeFile: "Upload a PDF or Word document.",
      });
    }
    if (file.size > 10 * 1024 * 1024) {
      throw new ServiceError("invalid", "That file is over 10 MB.", {
        resumeFile: "Resumes must be 10 MB or smaller.",
      });
    }
    const resume: ResumeDocumentView = {
      id: `res_pv_upload_${++this.counter}`,
      fileName: file.name,
      sizeBytes: file.size,
      uploadedAt: this.iso(),
    };
    this.candidate.resumes = [resume, ...this.candidate.resumes];
    return structuredClone(resume);
  }

  async start(input: StartApplicationInput): Promise<ApplicationView> {
    this.guardAvailable();
    const fieldErrors = validateApplyForm(input);
    if (!this.candidate.resumes.some((resume) => resume.id === input.resumeId)) {
      fieldErrors.resumeId = "That resume is no longer saved. Choose another or upload it again.";
    }
    if (Object.keys(fieldErrors).length > 0) {
      throw new ServiceError("invalid", "Some details need attention before applying.", fieldErrors);
    }
    this.candidate.profile = { ...input.profile };

    const id = `pv_${Date.now().toString(36)}_${++this.counter}`;
    const scenario = this.scenario;
    const now = this.iso();
    const resume = this.candidate.resumes.find((item) => item.id === input.resumeId) ?? null;
    const view: ApplicationView = {
      id,
      state: "REQUESTED",
      applicationUrl: input.applicationUrl.trim(),
      job: { title: null, company: null, ats: null },
      requestedAt: now,
      updatedAt: now,
      progress: null,
      resumeFileName: resume?.fileName ?? null,
      needs: null,
      receipt: null,
      prior: null,
      failure: null,
      uncertain: null,
      events: [],
      preparation: null,
      review: [],
    };
    const record: PreviewRecord = {
      view,
      scenario,
      queue: scriptFor(scenario),
      lastAdvance: this.now().getTime(),
      rechecks: 0,
      statusFailuresLeft: scenario === "connection_drop" ? 2 : 0,
      profile: { ...input.profile },
      prepareOnly: preparesOnly(scenario),
      captchaPending: scenario === "lookup",
      ownAnswers: [],
    };
    this.event(
      record,
      "application.requested",
      record.prepareOnly ? REQUESTED_TO_PREPARE : "Application requested. Your request authorizes submission.",
      "info",
    );
    this.records.set(id, record);
    return this.snapshot(record);
  }

  async status(applicationId: string): Promise<ApplicationView> {
    this.guardAvailable();
    const record = this.find(applicationId);
    if (record.statusFailuresLeft > 0 && record.view.state === "SUBMITTING") {
      record.statusFailuresLeft -= 1;
      throw new ServiceError("unavailable", "The application service stopped responding.");
    }
    const due = this.now().getTime() - record.lastAdvance >= this.stepDelayMs;
    if (due && record.queue.length > 0 && !isWaiting(record.view.state)) {
      const step = record.queue.shift()!;
      step.call(this, record);
      record.lastAdvance = this.now().getTime();
    }
    return this.snapshot(record);
  }

  async answer(applicationId: string, input: AnswerInput): Promise<ApplicationView> {
    this.guardAvailable();
    const record = this.find(applicationId);
    const needs = record.view.needs;
    if (record.view.state !== "NEEDS_INPUT" || needs?.kind !== "questions") {
      throw new ServiceError("conflict", "This application isn't waiting for answers.");
    }
    const errors = validateAnswers(needs.questions, needs.attestations, input.answers, input.attestations, false);
    const salary = input.answers.q_salary;
    if (typeof salary === "string" && salary.trim() && !/^\$?\s?\d[\d,]*$/.test(salary.trim())) {
      errors.q_salary = "The site accepts a whole number in US dollars, for example 145000.";
    }
    for (const question of needs.questions) {
      if (question.id in input.answers && !errors[question.id]) question.value = input.answers[question.id];
    }
    for (const attestation of needs.attestations) {
      attestation.accepted = input.attestations[attestation.id] === true;
    }
    needs.errors = errors;
    if (Object.keys(errors).length === 0) {
      needs.savedAt = this.iso();
      const answered = needs.questions.filter((question) => question.value !== null && question.value !== "").length;
      this.event(record, "input.saved", `Saved ${answered} of ${needs.questions.length} answers.`, "info");
    } else {
      this.event(record, "input.rejected", "Some answers were not accepted. Nothing else changed.", "warning");
    }
    return this.snapshot(record);
  }

  async resume(applicationId: string): Promise<ApplicationView> {
    this.guardAvailable();
    const record = this.find(applicationId);
    const { view } = record;

    if (view.state === "NEEDS_INPUT" && view.needs?.kind === "questions") {
      const needs = view.needs;
      const answers = Object.fromEntries(needs.questions.map((question) => [question.id, question.value]));
      const accepted = Object.fromEntries(needs.attestations.map((item) => [item.id, item.accepted]));
      const errors = {
        ...validateAnswers(needs.questions, needs.attestations, answers, accepted, true),
        ...needs.errors,
      };
      if (Object.keys(errors).length > 0) {
        needs.errors = errors;
        throw new ServiceError("invalid", "Some required answers are still missing.", errors);
      }
      this.event(
        record,
        "input.resolved",
        "Answers accepted. Re-reading the current page before continuing.",
        "progress",
      );
      const page = view.progress?.page ?? 1;
      const own = needs.questions
        .map((question) => ownReview(question, page))
        .filter((item): item is ReviewAnswerView => item !== null);
      const kept = record.ownAnswers.filter((item) => !own.some((next) => next.question === item.question));
      record.ownAnswers = [...kept, ...own];
      view.needs = null;
      this.transition(record, "FILLING");
      record.queue = [fillPage(2, 2), ...finish(record)];
    } else if (view.state === "NEEDS_INPUT" && view.needs?.kind === "interaction") {
      const kind = view.needs.interaction;
      view.needs = null;
      this.event(
        record,
        "interaction.completed",
        kind === "SIGN_IN" ? "Sign-in detected. Continuing with the application." : "Check completed. Continuing.",
        "progress",
      );
      if (kind === "SIGN_IN") {
        this.transition(record, "INSPECTING");
        record.queue = [packetReady, fillPage(1, 2), fillPage(2, 2), ...finish(record)];
      } else {
        this.transition(record, "FILLING");
        record.queue = finish(record);
      }
    } else if (view.state === "NEEDS_INPUT" && view.preparation) {
      // Prepare again: read the site and fill every page again, then stop at the review step.
      this.event(
        record,
        "preparation.restarted",
        "Preparing again: re-reading the site and filling the form. Nothing will be submitted.",
        "progress",
      );
      this.transition(record, "INSPECTING");
      record.queue = [packetReady, fillPage(1, 2), fillPage(2, 2), prepareForReview];
    } else if (view.state === "FAILED_RETRYABLE") {
      view.failure = null;
      this.event(record, "application.retry", "Trying again from the start of the form.", "progress");
      this.transition(record, "INSPECTING");
      record.queue = [packetReady, fillPage(1, 2), fillPage(2, 2), ...finish(record)];
    } else {
      throw new ServiceError("conflict", "This application can't be continued from its current state.");
    }
    // Every continuation leaves the prepared stop behind.
    view.preparation = null;
    record.lastAdvance = this.now().getTime();
    return this.snapshot(record);
  }

  async reconcile(applicationId: string, input: ReconcileInput): Promise<ApplicationView> {
    this.guardAvailable();
    const record = this.find(applicationId);
    const { view } = record;
    if (view.state !== "SUBMISSION_UNKNOWN" || !view.uncertain) {
      throw new ServiceError("conflict", "Only an unconfirmed submission can be reconciled.");
    }
    const now = this.iso();

    if (input.kind === "recheck") {
      record.rechecks += 1;
      this.event(
        record,
        "reconcile.started",
        "Checking the site for a confirmation or an application record.",
        "progress",
      );
      if (record.rechecks < 2) {
        view.uncertain.lastCheckedAt = now;
        view.uncertain.lastCheckResult =
          "Checked the application page and the applicant portal. No confirmation or application record yet. Sites can take a few minutes to list new applications.";
        this.event(record, "reconcile.inconclusive", "Still unconfirmed. Resubmission stays locked.", "warning");
      } else {
        this.confirm(
          record,
          null,
          [
            {
              kind: "portal",
              label: "Applicant portal record",
              value: "The applicant portal lists this application with status “Received”.",
              href: null,
              observedAt: now,
              source: "site",
            },
          ],
          "ATS_CANDIDATE_PORTAL",
        );
      }
    } else if (input.kind === "user_found_confirmation") {
      const where =
        input.foundIn === "email"
          ? "a confirmation email"
          : input.foundIn === "portal"
            ? "the applicant portal"
            : "another source";
      // Like the service, keep the site artifacts from the uncertain attempt alongside
      // the user's statement; the receipt's authority says who confirmed it.
      const earlier = view.uncertain.evidence;
      this.confirm(
        record,
        input.reference?.trim() || null,
        [
          ...earlier,
          {
            kind: "user_report",
            label: `You reported a confirmation in ${where}`,
            value: input.note?.trim() || null,
            href: null,
            observedAt: now,
            source: "user",
          },
        ],
        "USER_CONFIRMED",
      );
    } else {
      if (view.uncertain) {
        view.uncertain.lastCheckedAt = this.iso();
        view.uncertain.lastCheckResult = "You reported that the employer has no record of this application. The outcome is still unconfirmed; a site check must establish that nothing was submitted before another attempt is allowed.";
      }
      this.event(
        record,
        "reconcile.not_received",
        "Recorded your report of non-receipt. The application remains locked while the site outcome is unconfirmed.",
        "attention",
      );
    }
    return this.snapshot(record);
  }

  async list(): Promise<ApplicationListView> {
    this.guardAvailable();
    const applications: ApplicationSummaryView[] = [...this.records.values()]
      .map(({ view }) => ({
        id: view.id,
        state: view.state,
        applicationUrl: view.applicationUrl,
        job: view.job,
        requestedAt: view.requestedAt,
        updatedAt: view.updatedAt,
        preparation: view.preparation ?? null,
        pipelineEntryIds: view.id === PREPARED_PREVIEW_ID ? [PREPARED_PREVIEW_PIPELINE_ENTRY_ID] : [],
      }))
      .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt));
    return structuredClone({ applications });
  }

  // ---- script helpers (called with `this` bound to the service) ----

  transition(record: PreviewRecord, state: ApplicationState) {
    record.view.state = state;
    record.view.updatedAt = this.iso();
  }

  event(record: PreviewRecord, type: string, message: string, tone: EventTone) {
    record.view.events.push({
      id: `${record.view.id}_e${record.view.events.length + 1}`,
      type,
      at: this.iso(),
      message,
      tone,
    });
    record.view.updatedAt = this.iso();
  }

  confirm(
    record: PreviewRecord,
    reference: string | null,
    evidence: EvidenceView[],
    method: ConfirmationMethod = "SUBMISSION_OBSERVED",
  ) {
    const { view } = record;
    view.uncertain = null;
    const byUser = method === "USER_CONFIRMED";
    view.receipt = {
      receiptId: `rcpt_${view.id}`,
      submittedAt: this.iso(),
      confirmationReference: reference,
      evidence,
      confirmationMethod: method,
      confirmationAuthority: byUser ? "user" : "site",
    };
    this.event(
      record,
      "application.submitted",
      byUser ? "Marked submitted on your report. Receipt saved." : "Submission confirmed. Receipt saved.",
      "success",
    );
    this.transition(record, "SUBMITTED");
  }

  iso() {
    return this.now().toISOString();
  }

  private find(applicationId: string): PreviewRecord {
    const record = this.records.get(applicationId);
    if (!record) throw new ServiceError("not_found", "That application isn't in this preview session.");
    return record;
  }

  private snapshot(record: PreviewRecord): ApplicationView {
    return structuredClone(record.view);
  }

  private guardAvailable() {
    if (this.scenario === "unavailable") {
      throw new ServiceError(
        "unavailable",
        "The application service isn't responding. Nothing was sent. Your entries are still on this page.",
      );
    }
  }
}

const REQUESTED_TO_PREPARE =
  "Application requested. The desk prepares it for your review and doesn't submit it.";

/** What follows the last page: submitting, or stopping at the final review step. */
function finish(record: PreviewRecord): Step[] {
  return record.prepareOnly ? [prepareForReview] : [submitting, submitted];
}

function reviewControl(question: RequiredQuestionView): ReviewControl {
  switch (question.control) {
    case "long_text":
    case "single_select":
    case "multi_select":
    case "boolean":
      return question.control;
    default:
      return "text";
  }
}

/** A person's own answer as a review row: labels, never option ids. */
function ownReview(question: RequiredQuestionView, page: number): ReviewAnswerView | null {
  const value = question.value;
  if (isEmptyAnswer(value) || value === null) return null;
  const label = (answer: string) => question.options?.find((option) => option.value === answer)?.label ?? answer;
  return {
    question: question.label,
    wordingRecorded: true,
    page,
    control: reviewControl(question),
    value: typeof value === "boolean" ? (value ? "Yes" : "No") : Array.isArray(value) ? value.map(label) : label(value),
    source: "user",
    confidence: 1,
  };
}

/** Fictional answers the desk entered on the two pages, with the person's own answers in place. */
function previewReview(record: PreviewRecord): ReviewAnswerView[] {
  const entered: ReviewAnswerView[] = [
    {
      question: "First name",
      wordingRecorded: true,
      page: 1,
      control: "text",
      value: record.profile.firstName,
      source: "identity",
      confidence: 1,
    },
    {
      question: "Email",
      wordingRecorded: false,
      page: 1,
      control: "text",
      value: record.profile.email,
      source: "identity",
      confidence: 1,
    },
    {
      question: "Resume/CV",
      wordingRecorded: true,
      page: 1,
      control: "file",
      value: record.view.resumeFileName ?? "Robin_Vale_Resume_Lifecycle.pdf",
      source: "resume",
      confidence: 1,
    },
    {
      question: "Location (city)",
      wordingRecorded: true,
      page: 1,
      control: "single_select",
      value: "Portland, OR, USA",
      source: "fact",
      confidence: 0.96,
    },
    {
      question: "Are you legally authorized to work in the United States?",
      wordingRecorded: true,
      page: 2,
      control: "boolean",
      value: "Yes",
      source: "saved_answer",
      confidence: 1,
    },
    {
      question: "What are your base salary expectations (USD)?",
      wordingRecorded: true,
      page: 2,
      control: "text",
      value: "145000",
      source: "user",
      confidence: 1,
    },
    {
      question: "Which lifecycle platforms have you used in production?",
      wordingRecorded: true,
      page: 2,
      control: "multi_select",
      value: ["Braze", "Customer.io"],
      source: "fact",
      confidence: 0.93,
    },
    {
      question: "Why are you interested in Northwind Cartography?",
      wordingRecorded: true,
      page: 2,
      control: "long_text",
      value:
        "I've spent six years building lifecycle programs for subscription products, most recently onboarding and win-back journeys in Braze and Customer.io.\n\nNorthwind's maps are used every day by field teams, and I'd like to help more of them find the features that save them time.",
      source: "generated",
      confidence: 0.86,
    },
  ];
  const own = record.ownAnswers;
  const merged = entered.map((item) => own.find((answer) => answer.question === item.question) ?? item);
  const extra = own.filter((answer) => !entered.some((item) => item.question === answer.question));
  // Form order: page by page, the person's extra answers after the desk's on their page.
  return [...merged, ...extra].sort((a, b) => a.page - b.page).map((item) => structuredClone(item));
}

function isWaiting(state: ApplicationState) {
  return !["REQUESTED", "INSPECTING", "PACKET_READY", "FILLING", "SUBMITTING"].includes(state);
}

// ---- scripted steps ----

const opened: Step = function (this: PreviewApplicationService, record) {
  this.transition(record, "INSPECTING");
  this.event(record, "page.opened", "Opened the application page.", "progress");
};

const identified: Step = function (this: PreviewApplicationService, record) {
  const job = jobFor(record.scenario);
  record.view.job = job;
  this.event(record, "job.identified", `Identified ${job.title} at ${job.company} (${job.ats}).`, "info");
  if (record.scenario !== "duplicate") {
    this.event(record, "duplicate.checked", "No earlier submission for this job.", "info");
  }
};

const packetReady: Step = function (this: PreviewApplicationService, record) {
  this.transition(record, "PACKET_READY");
  record.view.progress = { page: 1, pageCount: 2 };
  this.event(record, "form.discovered", "Read page 1 of 2: 11 fields, 1 file upload.", "info");
  this.event(record, "packet.ready", "Prepared answers from your profile and resume.", "info");
};

function fillPage(page: number, pageCount: number): Step {
  return function (this: PreviewApplicationService, record) {
    this.transition(record, "FILLING");
    record.view.progress = { page, pageCount };
    if (page === 1) {
      this.event(record, "page.filling", `Filling page 1 of ${pageCount}.`, "progress");
      this.event(record, "document.uploaded", `Attached ${record.view.resumeFileName ?? "your resume"}.`, "info");
    } else {
      this.event(record, "page.completed", `Page ${page - 1} accepted by the site.`, "info");
      this.event(record, "page.filling", `Filling page ${page} of ${pageCount}.`, "progress");
    }
  };
}

const submitting: Step = function (this: PreviewApplicationService, record) {
  this.event(record, "form.validated", "Every required field is complete.", "info");
  this.transition(record, "SUBMITTING");
  this.event(record, "application.submitting", "Recorded the attempt, then pressed Submit.", "progress");
};

const submitted: Step = function (this: PreviewApplicationService, record) {
  const now = this.iso();
  const company = record.view.job.company ?? "the employer";
  const reference = "NWC-24-0922-7731";
  this.event(record, "confirmation.observed", "Confirmation page detected.", "info");
  this.confirm(record, reference, [
    {
      kind: "screenshot",
      label: "Confirmation page screenshot",
      value: null,
      href: "/preview-fixtures/confirmation.svg",
      observedAt: now,
      source: "site",
    },
    {
      kind: "page_text",
      label: "Confirmation message",
      value: `Thank you for applying to ${company}. Your application reference is ${reference}. We'll be in touch if your background fits the role.`,
      href: null,
      observedAt: now,
      source: "site",
    },
    {
      kind: "page_url",
      label: "Confirmation page address",
      value: "https://jobs.example.test/northwind/applications/confirmation",
      href: null,
      observedAt: now,
      source: "site",
    },
  ]);
};

/** Stop at the site's final review step with every page filled. Nothing is submitted. */
const prepareForReview: Step = function (this: PreviewApplicationService, record) {
  const now = this.iso();
  const { view } = record;
  view.progress = { page: 2, pageCount: 2 };
  view.needs = null;
  view.review = previewReview(record);
  view.preparation = {
    ready: true,
    formStep: 1,
    formUrl: PREPARED_REVIEW_PAGE,
    captchaPending: record.captchaPending,
    preparedAt: now,
    submitted: false,
    evidence: [
      {
        kind: "screenshot",
        label: "Final review page screenshot",
        value: null,
        href: "/preview-fixtures/prepared-review.svg",
        observedAt: now,
        source: "site",
      },
    ],
  };
  this.event(record, "preparation.ready", "Ready for final review. Nothing was submitted.", "attention");
  this.transition(record, "NEEDS_INPUT");
  this.event(record, "application.needs_input", "Paused at the final review step for you to check.", "info");
};

/** The site's location search offered several places; the person picks one or types another. */
const needsLookup: Step = function (this: PreviewApplicationService, record) {
  this.transition(record, "NEEDS_INPUT");
  record.view.needs = {
    kind: "questions",
    savedAt: null,
    errors: {},
    questions: [
      {
        id: "q_location",
        label: "Location (city)",
        help: null,
        control: "single_select",
        required: true,
        lookup: true,
        options: [
          { value: "Portland, OR, USA", label: "Portland, OR, USA" },
          { value: "Portland, ME, USA", label: "Portland, ME, USA" },
          { value: "Portland, TX, USA", label: "Portland, TX, USA" },
        ],
        value: null,
        maxLength: null,
        reason: "The site suggested several places for “Portland”, and the desk doesn't choose between them for you.",
      },
    ],
    attestations: [],
  };
  this.event(
    record,
    "field.unresolved",
    "The site's location search offered 3 places. Paused on page 1 for your choice.",
    "attention",
  );
};

const needsQuestions: Step = function (this: PreviewApplicationService, record) {
  this.transition(record, "NEEDS_INPUT");
  record.view.needs = {
    kind: "questions",
    savedAt: null,
    errors: {},
    questions: [
      {
        id: "q_work_auth",
        label: "Are you legally authorized to work in the United States?",
        help: null,
        control: "boolean",
        required: true,
        options: null,
        value: null,
        maxLength: null,
        reason: "Work authorization isn't in your saved answers.",
      },
      {
        id: "q_sponsorship",
        label: "Will you now or in the future require sponsorship for employment visa status?",
        help: null,
        control: "single_select",
        required: true,
        options: [
          { value: "no", label: "No" },
          { value: "yes", label: "Yes" },
        ],
        value: null,
        maxLength: null,
        reason: "Your sponsorship needs aren't in your saved answers.",
      },
      {
        id: "q_salary",
        label: "What are your base salary expectations (USD)?",
        help: "The site asks for a single whole number.",
        control: "text",
        required: true,
        options: null,
        value: null,
        maxLength: 20,
        reason: "Salary is never guessed from other data.",
      },
      {
        id: "q_source",
        label: "How did you hear about this role?",
        help: null,
        control: "single_select",
        required: true,
        options: [
          { value: "company_site", label: "Company website" },
          { value: "linkedin", label: "LinkedIn" },
          { value: "referral", label: "Employee referral" },
          { value: "job_board", label: "Job board" },
          { value: "other", label: "Other" },
        ],
        value: null,
        maxLength: null,
        reason: "Only you know how you found this job.",
      },
      {
        id: "q_why",
        label: "Why are you interested in Northwind Cartography?",
        help: "Up to 600 characters.",
        control: "long_text",
        required: true,
        options: null,
        value: null,
        maxLength: 600,
        reason: "Nothing in your profile addresses this company.",
      },
      {
        id: "q_tools",
        label: "Which lifecycle platforms have you used in production?",
        help: "Optional. Select all that apply.",
        control: "multi_select",
        required: false,
        options: [
          { value: "braze", label: "Braze" },
          { value: "iterable", label: "Iterable" },
          { value: "customer_io", label: "Customer.io" },
          { value: "klaviyo", label: "Klaviyo" },
          { value: "none", label: "None of these" },
        ],
        value: null,
        maxLength: null,
        reason: null,
      },
      {
        id: "q_gender",
        label: "Voluntary self-identification: gender",
        help: "The site asks every applicant. Your answer is not inferred from anything else.",
        control: "single_select",
        required: true,
        options: [
          { value: "woman", label: "Woman" },
          { value: "man", label: "Man" },
          { value: "nonbinary", label: "Non-binary" },
          { value: "decline", label: "I don't wish to answer" },
        ],
        value: null,
        maxLength: null,
        reason: "Protected characteristics are only ever answered by you.",
      },
    ],
    attestations: [
      {
        id: "a_truthful",
        statement:
          "I certify that the information in this application is true and complete, and I understand that false or misleading information may result in my application being rejected.",
        required: true,
        accepted: false,
      },
      {
        id: "a_talent_pool",
        statement: "Northwind Cartography may keep my application on file for future openings for up to 24 months.",
        required: false,
        accepted: false,
      },
    ],
  };
  this.event(
    record,
    "field.unresolved",
    "7 questions and 2 statements need your answer. Paused on page 1.",
    "attention",
  );
};

function needsInteraction(kind: "SIGN_IN" | "CAPTCHA"): Step {
  return function (this: PreviewApplicationService, record) {
    this.transition(record, "NEEDS_INPUT");
    record.view.needs =
      kind === "SIGN_IN"
        ? {
            kind: "interaction",
            interaction: "SIGN_IN",
            instructions:
              "Halcyon Freight's careers site requires a candidate account before it shows the application. A browser window is open on this computer at the sign-in page. Sign in or create the account there, then continue here.",
            pageUrl: "https://halcyon.example.test/careers/login",
          }
        : {
            kind: "interaction",
            interaction: "CAPTCHA",
            instructions:
              "The site is showing a CAPTCHA before it will accept the application. Complete it in the open browser window, then continue here. Your answers on the page are kept.",
            pageUrl: "https://jobs.example.test/juniper-vale/apply",
          };
    this.event(
      record,
      "interaction.required",
      kind === "SIGN_IN" ? "The site requires you to sign in." : "The site is showing a CAPTCHA.",
      "attention",
    );
  };
}

const duplicate: Step = function (this: PreviewApplicationService, record) {
  this.transition(record, "DUPLICATE");
  record.view.prior = {
    applicationId: "pv_prior_0903",
    applicationUrl: "https://jobs.example.test/northwind/senior-lifecycle-marketer",
    submittedAt: "2026-09-03T15:42:10Z",
    confirmationReference: "NWC-24-0903-1188",
  };
  this.event(
    record,
    "duplicate.found",
    "Found a confirmed submission for this job from Sep 3. Nothing was sent.",
    "attention",
  );
};

const unknownOutcome: Step = function (this: PreviewApplicationService, record) {
  const now = this.iso();
  record.view.uncertain = {
    attemptedAt: record.view.updatedAt,
    reason:
      "Submit was pressed, then the page stopped responding. No confirmation page, message or reference appeared within 60 seconds.",
    evidence: [
      {
        kind: "screenshot",
        label: "Page after pressing Submit",
        value: null,
        href: "/preview-fixtures/unresponsive.svg",
        observedAt: now,
        source: "site",
      },
    ],
    lastCheckedAt: null,
    lastCheckResult: null,
  };
  this.event(
    record,
    "application.submission_unknown",
    "Submission not confirmed. Resubmission is locked until this is settled.",
    "warning",
  );
  this.transition(record, "SUBMISSION_UNKNOWN");
};

const retryableFailure: Step = function (this: PreviewApplicationService, record) {
  record.view.failure = {
    reason: "The site returned a server error (HTTP 502) while loading page 2.",
    detail: "Nothing was submitted. This site only receives answers on the final submit, so trying again is safe.",
    retryable: true,
    evidence: [
      {
        kind: "page_text",
        label: "Message shown by the site",
        value: "502 Bad Gateway — please try again in a few minutes.",
        href: null,
        observedAt: this.iso(),
        source: "site",
      },
    ],
  };
  this.event(
    record,
    "application.failed",
    "Stopped: the site returned a server error. Nothing was submitted.",
    "error",
  );
  this.transition(record, "FAILED_RETRYABLE");
};

const permanentFailure: Step = function (this: PreviewApplicationService, record) {
  record.view.failure = {
    reason: "This posting is closed.",
    detail: "The page says “This position is no longer accepting applications.” Nothing was submitted.",
    retryable: false,
    evidence: [
      {
        kind: "page_text",
        label: "Message shown by the site",
        value: "This position is no longer accepting applications.",
        href: null,
        observedAt: this.iso(),
        source: "site",
      },
    ],
  };
  this.event(record, "application.failed", "Stopped: the posting is closed. Nothing was submitted.", "error");
  this.transition(record, "FAILED_PERMANENT");
};

function scriptFor(scenario: PreviewScenarioId): Step[] {
  switch (scenario) {
    case "prepared":
      return [opened, identified, packetReady, fillPage(1, 2), fillPage(2, 2), prepareForReview];
    case "lookup":
      return [opened, identified, packetReady, fillPage(1, 2), needsLookup];
    case "questions":
      return [opened, identified, packetReady, fillPage(1, 2), needsQuestions];
    case "sign_in":
      return [opened, identified, needsInteraction("SIGN_IN")];
    case "captcha":
      return [opened, identified, packetReady, fillPage(1, 2), fillPage(2, 2), needsInteraction("CAPTCHA")];
    case "duplicate":
      return [opened, identified, duplicate];
    case "uncertain":
      return [opened, identified, packetReady, fillPage(1, 2), fillPage(2, 2), submitting, unknownOutcome];
    case "failure_retryable":
      return [opened, identified, packetReady, fillPage(1, 2), retryableFailure];
    case "failure_permanent":
      return [opened, identified, permanentFailure];
    case "straight":
    case "connection_drop":
    case "unavailable":
    default:
      return [opened, identified, packetReady, fillPage(1, 2), fillPage(2, 2), submitting, submitted];
  }
}
