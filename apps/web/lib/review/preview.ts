import { ServiceError } from "../service/errors";
import { PREPARED_PREVIEW_ID } from "../service/preview";
import type {
  AnswerInput,
  AnswerValue,
  ApplicationEventView,
  ApplicationView,
  EventTone,
  InputRequestView,
  JobIdentityView,
} from "../service/types";
import { allowedReuse, editProblem } from "./logic";
import type {
  ApplicationReviewView,
  ApproveInput,
  HoldView,
  ReviewQueueItemView,
  ReviewQueueView,
  ReviewRowView,
  ReviewService,
  ReviewStage,
  SubmitInput,
} from "./types";

/**
 * In-memory fixtures for `/preview/review`. Everything is fictional (example.test
 * addresses, made-up employers and fact ids); nothing is sent anywhere, and the
 * preview never submits. Approve, edit and prepare again change only this tab's
 * copy, which a reload resets.
 */

export const PREVIEW_APPROVED_ID = "pv_approved_larkspur";
export const PREVIEW_SIGN_IN_ID = "pv_signin_quarry";

/** Why the preview's Submit is always off. */
export const PREVIEW_SUBMIT_PROBLEM = "The preview never submits: there's no employer behind it.";

interface PreviewReviewRecord {
  application: ApplicationView;
  stage: ReviewStage;
  preparedPacketId: string | null;
  packetCount: number;
  approval: ApplicationReviewView["approval"];
  changed: boolean;
  captchaPending: boolean;
  rows: ReviewRowView[];
  /** Rows the "prepared again" run fills for an application held in the browser. */
  pending: ReviewRowView[];
  cost: ApplicationReviewView["providerCost"];
  stoppedAt: string;
}

const NORTHWIND_URL = "https://jobs.example.test/northwind/senior-lifecycle-marketer";

/** The fictional narrative: written from the fictional facts and story passages it cites. */
const NORTHWIND_FIT =
  "For six years I've built lifecycle programs for subscription products: onboarding, activation and win-back journeys in Braze and Customer.io, measured against retention cohorts rather than open rates.\n\nAt Harbor Lane Software I rebuilt the trial onboarding sequence around the three actions that predicted conversion, which lifted trial-to-paid conversion by 14% over two quarters. Northwind's field teams depend on the maps every day, and the posting asks for someone who can help them find the features that save time; that's the work I've been doing, and I'd like to do it for a product people carry into the field.";

const YES_NO = [
  { value: "yes", label: "Yes" },
  { value: "no", label: "No" },
];

function row(input: Partial<ReviewRowView> & Pick<ReviewRowView, "question" | "page" | "control" | "value" | "provenance">): ReviewRowView {
  return {
    questionId: null,
    wordingRecorded: true,
    required: true,
    citations: null,
    confidence: 1,
    edit: null,
    noEditReason: null,
    ...input,
  };
}

function identity(question: string, value: string, questionId: string | null, page = 1): ReviewRowView {
  return row({
    question,
    page,
    control: "text",
    value,
    provenance: { kind: "identity", label: "Your details", detail: "verified identity" },
    questionId,
    edit: questionId
      ? { control: "text", options: null, lookup: false, attestation: false, required: true, value, reuse: ["application"], note: "Your verified details are changed in your profile; this changes this application only." }
      : null,
    noEditReason: questionId ? null : "Your verified details are changed in your profile, not here.",
  });
}

function resume(fileName: string): ReviewRowView {
  return row({
    question: "Resume/CV",
    page: 1,
    control: "file",
    value: fileName,
    provenance: { kind: "resume", label: "Your resume", detail: "the resume pinned to this application" },
    noEditReason: "The resume pinned to this application can't be changed here.",
  });
}

function northwindRows(): ReviewRowView[] {
  return [
    identity("First name", "Robin", null),
    identity("Last name", "Vale", null),
    identity("Email", "robin.vale@example.test", "q_pv_nw_email"),
    resume("Robin_Vale_Resume_Lifecycle.pdf"),
    row({
      question: "Pronouns",
      page: 1,
      control: "text",
      value: null,
      required: false,
      confidence: null,
      provenance: { kind: "blank", label: "Left blank", detail: null },
      questionId: "q_pv_nw_pronouns",
      edit: { control: "text", options: null, lookup: false, attestation: false, required: false, value: null, reuse: ["application", "job", "global"], note: null },
    }),
    row({
      question: "Are you legally authorized to work in the United States?",
      page: 2,
      control: "single_select",
      value: "Yes",
      provenance: { kind: "saved_answer", label: "Saved answer", detail: "saved answer for 'Are you legally authorized to work in the United States?'" },
      questionId: "q_pv_nw_authorized",
      edit: { control: "single_select", options: YES_NO, lookup: false, attestation: false, required: true, value: "yes", reuse: ["application", "job", "global"], note: null },
    }),
    row({
      question: "Will you now or in the future require visa sponsorship?",
      page: 2,
      control: "single_select",
      value: "No",
      provenance: { kind: "derived", label: "Derived", detail: "derived from the stated U.S. work authorization status (table)" },
      noEditReason: "The form's options for this question weren't recorded when it was filled, so it can't be changed here.",
    }),
    row({
      question: "What are your base salary expectations (USD)?",
      page: 2,
      control: "text",
      value: "145000",
      provenance: { kind: "derived", label: "Derived", detail: "derived from the saved desired salary for 'Desired salary' (converted to per year)" },
      questionId: "q_pv_nw_salary",
      edit: { control: "text", options: null, lookup: false, attestation: false, required: true, value: "145000", reuse: ["application", "job", "global"], note: null },
    }),
    row({
      question: "How did you hear about this job?",
      page: 2,
      control: "single_select",
      value: "Company careers page",
      provenance: { kind: "saved_policy", label: "Saved policy", detail: "standing referral answer 'How did you hear about us?'" },
      noEditReason: "The form's options for this question weren't recorded when it was filled, so it can't be changed here.",
    }),
    row({
      question: "Do you have 5+ years of lifecycle marketing experience?",
      page: 2,
      control: "single_select",
      value: "Yes",
      confidence: 0.94,
      provenance: { kind: "fact_screener", label: "Fact-grounded screener", detail: "yes/no experience answered YES by Jev from verified facts" },
      citations: { facts: ["fact_pv_years_lifecycle", "fact_pv_role_braze"], passages: [], jobEvidence: [] },
      questionId: "q_pv_nw_years",
      edit: { control: "single_select", options: YES_NO, lookup: false, attestation: false, required: true, value: "yes", reuse: ["application", "job", "global"], note: null },
    }),
    row({
      question: "Which lifecycle platforms have you used in production?",
      page: 2,
      control: "multi_select",
      value: ["Braze", "Customer.io"],
      confidence: 0.93,
      provenance: { kind: "fact_screener", label: "Fact-grounded screener", detail: "options selected by Jev from verified facts that state them" },
      citations: { facts: ["fact_pv_role_braze", "fact_pv_role_customerio"], passages: [], jobEvidence: [] },
      questionId: "q_pv_nw_platforms",
      edit: {
        control: "multi_select",
        options: [
          { value: "braze", label: "Braze" },
          { value: "customerio", label: "Customer.io" },
          { value: "iterable", label: "Iterable" },
          { value: "klaviyo", label: "Klaviyo" },
        ],
        lookup: false,
        attestation: false,
        required: true,
        value: ["braze", "customerio"],
        reuse: ["application"],
        note: "The full wording of this question wasn't recorded, so the answer is kept for this application only.",
      },
    }),
    row({
      question: "Why are you a good fit for Northwind Cartography?",
      page: 2,
      control: "long_text",
      value: NORTHWIND_FIT,
      confidence: 0.86,
      provenance: {
        kind: "narrative",
        label: "RAG narrative",
        detail: "Opus draft with per-sentence citations and question completeness checked by Jev",
      },
      citations: {
        facts: ["fact_pv_years_lifecycle", "fact_pv_role_braze", "fact_pv_trial_conversion"],
        passages: ["story_pv_onboarding_c2", "story_pv_onboarding_c3"],
        jobEvidence: ["job_pv_northwind_req_2"],
      },
      questionId: "q_pv_nw_fit",
      edit: { control: "long_text", options: null, lookup: false, attestation: false, required: true, value: NORTHWIND_FIT, reuse: ["application", "job"], note: "A written answer is drafted for this job, so it can be kept for this application or this job, not for every application." },
    }),
    row({
      question: "When could you start?",
      page: 2,
      control: "text",
      value: "Two weeks after an offer",
      provenance: { kind: "user", label: "Your answer", detail: null },
      questionId: "q_pv_nw_start",
      edit: { control: "text", options: null, lookup: false, attestation: false, required: true, value: "Two weeks after an offer", reuse: ["application", "job", "global"], note: null },
    }),
    row({
      question: "I certify that the information I provided is true and complete.",
      page: 2,
      control: "boolean",
      value: "Yes",
      provenance: { kind: "saved_policy", label: "Saved policy", detail: "saved statement 'I certify that my answers are true and complete.' fully covers this statement (Jev)" },
      questionId: "q_pv_nw_certify",
      edit: { control: "boolean", options: null, lookup: false, attestation: true, required: true, value: true, reuse: ["application", "job", "global"], note: null },
    }),
  ];
}

function larkspurRows(): ReviewRowView[] {
  return [
    identity("Full name", "Robin Vale", null),
    identity("Email", "robin.vale@example.test", null),
    resume("Robin_Vale_Resume_2026.pdf"),
    row({
      question: "Desired salary",
      page: 1,
      control: "text",
      value: "140000",
      provenance: { kind: "derived", label: "Derived", detail: "derived from the saved desired salary for 'Desired salary'" },
      questionId: "q_pv_lk_salary",
      edit: { control: "text", options: null, lookup: false, attestation: false, required: true, value: "140000", reuse: ["application", "job", "global"], note: null },
    }),
    row({
      question: "Are you open to working from our Portland studio two days a week?",
      page: 1,
      control: "single_select",
      value: "Yes",
      provenance: { kind: "saved_answer", label: "Saved answer", detail: "saved work-arrangement preference for 'Work arrangement' on a work-mode choice" },
      questionId: "q_pv_lk_hybrid",
      edit: { control: "single_select", options: YES_NO, lookup: false, attestation: false, required: true, value: "yes", reuse: ["application", "job", "global"], note: null },
    }),
  ];
}

function quarryRows(): ReviewRowView[] {
  return [
    identity("Legal first name", "Robin", null, 2),
    identity("Legal last name", "Vale", null, 2),
    identity("Email address", "robin.vale@example.test", null, 2),
    resume("Robin_Vale_Resume_Lifecycle.pdf"),
  ].map((item) => ({ ...item, page: 2 }));
}

function event(id: string, type: string, at: string, message: string, tone: EventTone): ApplicationEventView {
  return { id, type, at, message, tone };
}

function preparedView(
  id: string,
  job: JobIdentityView,
  applicationUrl: string,
  requestedAt: string,
  preparedAt: string,
  captchaPending: boolean,
  resumeFileName: string,
): ApplicationView {
  return {
    id,
    state: "NEEDS_INPUT",
    applicationUrl,
    job,
    requestedAt,
    updatedAt: preparedAt,
    progress: { page: 2, pageCount: 2 },
    resumeFileName,
    needs: null,
    receipt: null,
    prior: null,
    failure: null,
    uncertain: null,
    events: [
      event(`${id}_e1`, "application.requested", requestedAt, "Application request recorded.", "info"),
      event(`${id}_e2`, "application.preparation_only", requestedAt, "Preparation only: submission is disabled.", "info"),
      event(`${id}_e3`, "preparation.ready", preparedAt, "Ready for final review. Nothing was submitted.", "attention"),
      event(`${id}_e4`, "application.needs_input", preparedAt, "Paused at the final review step for you to check.", "info"),
    ],
    preparation: {
      ready: true,
      formStep: 1,
      formUrl: `${applicationUrl}/apply/review`,
      captchaPending,
      preparedAt,
      submitted: false,
      evidence: [
        {
          kind: "screenshot",
          label: "Final review page screenshot",
          value: null,
          href: "/preview-fixtures/prepared-review.svg",
          observedAt: preparedAt,
          source: "site",
        },
      ],
    },
    review: [],
  };
}

const SIGN_IN_NEEDS: InputRequestView = {
  kind: "interaction",
  interaction: "SIGN_IN",
  instructions:
    "The site asks you to sign in. Choose Resume in browser: a browser window opens on the application, sign in there and the application carries on.",
  pageUrl: "https://jobs.example.test/quarry-pine/sign-in",
};

export interface PreviewReviewOptions {
  now?: () => Date;
}

export class PreviewReviewService implements ReviewService {
  readonly mode = "preview" as const;
  private readonly records = new Map<string, PreviewReviewRecord>();
  private readonly now: () => Date;
  private counter = 0;

  constructor(options: PreviewReviewOptions = {}) {
    this.now = options.now ?? (() => new Date());
    const northwind = preparedView(
      PREPARED_PREVIEW_ID,
      { title: "Senior Lifecycle Marketer", company: "Northwind Cartography", ats: "Greenhouse" },
      NORTHWIND_URL,
      "2026-09-23T15:02:04.000Z",
      "2026-09-23T15:06:39.000Z",
      true,
      "Robin_Vale_Resume_Lifecycle.pdf",
    );
    this.records.set(PREPARED_PREVIEW_ID, {
      application: northwind,
      stage: "prepared",
      preparedPacketId: "pkt_pv_northwind_1",
      packetCount: 1,
      approval: null,
      changed: false,
      captchaPending: true,
      rows: northwindRows(),
      pending: [],
      cost: { knownUsd: 0.4213, calls: 37, unknownCostCalls: 2 },
      stoppedAt: "2026-09-23T15:06:39.000Z",
    });

    const larkspur = preparedView(
      PREVIEW_APPROVED_ID,
      { title: "Growth Marketing Manager", company: "Larkspur Field Guides", ats: "Lever" },
      "https://jobs.example.test/larkspur/growth-marketing-manager",
      "2026-09-23T13:31:10.000Z",
      "2026-09-23T13:40:02.000Z",
      false,
      "Robin_Vale_Resume_2026.pdf",
    );
    larkspur.events.push(
      event(`${PREVIEW_APPROVED_ID}_e5`, "application.approved", "2026-09-23T14:05:00.000Z", "Application approved.", "info"),
    );
    this.records.set(PREVIEW_APPROVED_ID, {
      application: larkspur,
      stage: "prepared",
      preparedPacketId: "pkt_pv_larkspur_1",
      packetCount: 1,
      approval: { packetId: "pkt_pv_larkspur_1", approvedAt: "2026-09-23T14:05:00.000Z", approver: "preview", pages: 1 },
      changed: false,
      captchaPending: false,
      rows: larkspurRows(),
      pending: [],
      cost: { knownUsd: 0.1874, calls: 12, unknownCostCalls: 0 },
      stoppedAt: "2026-09-23T13:40:02.000Z",
    });

    const quarryAt = "2026-09-23T11:20:45.000Z";
    const quarry: ApplicationView = {
      id: PREVIEW_SIGN_IN_ID,
      state: "NEEDS_INPUT",
      applicationUrl: "https://jobs.example.test/quarry-pine/lifecycle-marketing-lead",
      job: { title: "Lifecycle Marketing Lead", company: "Quarry & Pine Outfitters", ats: "Workday" },
      requestedAt: "2026-09-23T11:18:02.000Z",
      updatedAt: quarryAt,
      progress: { page: 2, pageCount: null },
      resumeFileName: "Robin_Vale_Resume_Lifecycle.pdf",
      needs: SIGN_IN_NEEDS,
      receipt: null,
      prior: null,
      failure: null,
      uncertain: null,
      events: [
        event(`${PREVIEW_SIGN_IN_ID}_e1`, "application.requested", "2026-09-23T11:18:02.000Z", "Application request recorded.", "info"),
        event(`${PREVIEW_SIGN_IN_ID}_e2`, "application.needs_input", quarryAt, "Waiting for you.", "attention"),
      ],
      preparation: null,
      review: [],
    };
    this.records.set(PREVIEW_SIGN_IN_ID, {
      application: quarry,
      stage: "browser_action",
      preparedPacketId: null,
      packetCount: 0,
      approval: null,
      changed: false,
      captchaPending: false,
      rows: [],
      pending: quarryRows(),
      cost: null,
      stoppedAt: quarryAt,
    });
  }

  async queue(): Promise<ReviewQueueView> {
    const applications = [...this.records.values()]
      .map((record) => this.item(record))
      .sort((a, b) => Date.parse(b.stoppedAt) - Date.parse(a.stoppedAt));
    return structuredClone({ applications });
  }

  async review(applicationId: string): Promise<ApplicationReviewView> {
    return structuredClone(this.view(this.record(applicationId)));
  }

  async approve(applicationId: string, input: ApproveInput): Promise<ApplicationReviewView> {
    const record = this.record(applicationId);
    if (record.stage !== "prepared" || !record.preparedPacketId) {
      throw new ServiceError("conflict", "Only an application stopped at its final review step can be approved.");
    }
    if (record.changed) {
      throw new ServiceError(
        "conflict",
        "Answers were saved after this application was prepared. Prepare it again, then approve the new preparation.",
      );
    }
    if (input.packetId !== record.preparedPacketId) {
      throw new ServiceError(
        "conflict",
        "This application was prepared again since you opened it. Reload it and review the new preparation.",
      );
    }
    if (!record.approval) {
      const approvedAt = this.now().toISOString();
      record.approval = { packetId: record.preparedPacketId, approvedAt, approver: "preview", pages: this.pages(record) };
      record.application.events.push(
        event(`${applicationId}_approved_${++this.counter}`, "application.approved", approvedAt, "Application approved.", "info"),
      );
    }
    return structuredClone(this.view(record));
  }

  async submit(applicationId: string, _input: SubmitInput): Promise<ApplicationReviewView> {
    this.record(applicationId);
    throw new ServiceError("invalid", PREVIEW_SUBMIT_PROBLEM);
  }

  async editAnswer(applicationId: string, input: AnswerInput): Promise<ApplicationView> {
    const record = this.record(applicationId);
    if (record.application.state !== "NEEDS_INPUT") {
      throw new ServiceError("conflict", "This application isn't waiting for answers right now.");
    }
    const sent: [string, AnswerValue, boolean][] = [
      ...Object.entries(input.answers).map(([id, value]): [string, AnswerValue, boolean] => [id, value, false]),
      ...Object.entries(input.attestations).map(([id, value]): [string, AnswerValue, boolean] => [id, value, true]),
    ];
    const errors: Record<string, string> = {};
    const changes: [ReviewRowView, AnswerValue][] = [];
    for (const [id, value, asStatement] of sent) {
      const target = record.rows.find((item) => item.questionId === id);
      if (!target?.edit || target.edit.attestation !== asStatement) {
        throw new ServiceError("conflict", "These questions have changed. Reload to see the current questions.", {
          [id]: "These questions have changed. Reload to see the current questions.",
        });
      }
      const scope = input.reuse?.[id] ?? "application";
      const problem =
        editProblem(target.edit, value) ??
        (allowedReuse(target.edit).includes(scope) ? null : "This answer can only be kept for this application.");
      if (problem) errors[id] = problem;
      else changes.push([target, value]);
    }
    if (Object.keys(errors).length > 0) {
      throw new ServiceError("invalid", "Some of the request is missing or malformed.", errors);
    }
    for (const [target, value] of changes) {
      const edit = target.edit!;
      target.value = displayValue(edit.control, edit.options, value);
      target.edit = { ...edit, value: value as string | string[] | boolean | null };
      target.provenance = { kind: "user", label: "Your answer", detail: null };
      target.citations = null;
      target.confidence = 1;
    }
    if (changes.length > 0) {
      record.changed = true;
      record.approval = null;
      record.application.events.push(
        event(`${applicationId}_input_${++this.counter}`, "input.received", this.now().toISOString(), "Your answers were saved.", "info"),
      );
    }
    return structuredClone(record.application);
  }

  async prepareAgain(applicationId: string): Promise<ApplicationView> {
    const record = this.record(applicationId);
    const at = this.now().toISOString();
    const application = record.application;
    if (record.stage === "browser_action") {
      // The preview has no browser: it acts as if you signed in and the form was filled.
      record.stage = "prepared";
      record.rows = record.pending;
      record.pending = [];
      application.needs = null;
      application.progress = { page: 2, pageCount: 2 };
      application.preparation = preparedView(
        application.id,
        application.job,
        application.applicationUrl,
        application.requestedAt,
        at,
        false,
        application.resumeFileName ?? "",
      ).preparation;
    } else if (record.stage === "prepared") {
      application.preparation = { ...application.preparation!, preparedAt: at };
    } else {
      throw new ServiceError("conflict", "This application can't be prepared again now.");
    }
    record.packetCount += 1;
    record.preparedPacketId = `pkt_pv_${applicationId}_${record.packetCount}`;
    record.approval = null;
    record.changed = false;
    record.stoppedAt = at;
    application.updatedAt = at;
    application.events.push(
      event(`${applicationId}_ready_${++this.counter}`, "preparation.ready", at, "Ready for final review. Nothing was submitted.", "attention"),
      event(`${applicationId}_stop_${++this.counter}`, "application.needs_input", at, "Paused at the final review step for you to check.", "info"),
    );
    return structuredClone(application);
  }

  async answer(applicationId: string, _input: AnswerInput): Promise<ApplicationView> {
    return structuredClone(this.record(applicationId).application);
  }

  private record(applicationId: string): PreviewReviewRecord {
    const record = this.records.get(applicationId);
    if (!record) throw new ServiceError("not_found", "No application with that id.");
    return record;
  }

  private pages(record: PreviewReviewRecord): number {
    return new Set(record.rows.map((item) => item.page)).size;
  }

  private hold(record: PreviewReviewRecord): HoldView {
    if (record.stage === "browser_action") {
      return { kind: "sign_in", summary: "Sign in on the site in a browser window; then it carries on to the review step." };
    }
    if (record.changed) {
      return { kind: "edited", summary: "Answers changed since this preparation · prepare it again before approving." };
    }
    if (record.approval) return { kind: "approved", summary: "Approved · the preview never submits." };
    return {
      kind: "ready",
      summary: record.captchaPending
        ? "Ready to review and approve · a CAPTCHA must be solved in the browser when it's submitted."
        : "Ready to review and approve.",
    };
  }

  private item(record: PreviewReviewRecord): ReviewQueueItemView {
    const application = record.application;
    return {
      id: application.id,
      state: application.state,
      stage: record.stage,
      applicationUrl: application.applicationUrl,
      job: application.job,
      preparedAt: record.stage === "prepared" ? (application.preparation?.preparedAt ?? null) : null,
      stoppedAt: record.stoppedAt,
      captchaPending: record.captchaPending,
      providerCost: record.cost,
      hold: this.hold(record),
      approved: record.approval !== null,
    };
  }

  private view(record: PreviewReviewRecord): ApplicationReviewView {
    const id = record.application.id;
    return {
      application: record.application,
      stage: record.stage,
      preparedPacketId: record.stage === "prepared" && !record.changed ? record.preparedPacketId : null,
      approval: record.approval,
      changedSincePreparation: record.changed,
      providerCost: record.cost,
      answers: record.rows,
      editNote: record.stage === "prepared" ? null : "Answers can be changed once the application is prepared.",
      submit: {
        allowed: false,
        problems: [PREVIEW_SUBMIT_PROBLEM],
        enabled: false,
        opensBrowser: false,
        command: `IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit ${id} --yes`,
      },
      browser: {
        available: record.stage === "browser_action",
        reason: record.stage === "browser_action" ? null : "The preview has no browser window.",
        command: `interviewmaxxing resume ${id} --act`,
      },
    };
  }
}

/** How a changed answer reads in the review list: option labels, "Yes"/"No" or the text. */
function displayValue(
  control: string,
  options: { value: string; label: string }[] | null,
  value: AnswerValue,
): string | string[] | null {
  const label = (item: string) => options?.find((option) => option.value === item)?.label ?? item;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.map(label);
  if (typeof value !== "string" || value.trim() === "") return null;
  return control === "single_select" ? label(value) : value;
}

/** One preview review service per tab, shared by the queue and the review pages. */
let shared: PreviewReviewService | null = null;

export function previewReview(): PreviewReviewService {
  shared ??= new PreviewReviewService();
  return shared;
}
