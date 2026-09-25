/**
 * Pure helpers for the review lane: provenance badges, the queue's order and
 * filters, provider cost, the approve and submit guards, the edit request and the
 * execution gate. The service stays the authority on every decision; these only
 * decide what the page shows and which requests it may send.
 */

import { receiptAuthority } from "../receipt";
import { isEmptyAnswer } from "../validation";
import { isLoopbackApplication, type ServiceReadiness } from "../service/readiness";
import type { AnswerInput, AnswerValue, ApplicationState } from "../service/types";
import { isActive } from "../state";
import type {
  ApplicationReviewView,
  HoldKind,
  ProvenanceKind,
  ProviderCostView,
  ReuseChoice,
  ReviewCitationsView,
  ReviewEditView,
  ReviewProvenanceView,
  ReviewQueueItemView,
  ReviewRowView,
  SubmitInput,
} from "./types";

// ---- provenance badges ----

export const PROVENANCE_KINDS: readonly ProvenanceKind[] = [
  "identity",
  "resume",
  "saved_answer",
  "saved_policy",
  "derived",
  "fact_screener",
  "narrative",
  "user",
  "blank",
];

const PROVENANCE_LABELS: Record<ProvenanceKind, string> = {
  identity: "Your details",
  resume: "Your resume",
  saved_answer: "Saved answer",
  saved_policy: "Saved policy",
  derived: "Derived",
  fact_screener: "Fact-grounded screener",
  narrative: "RAG narrative",
  user: "Your answer",
  blank: "Left blank",
};

const PROVENANCE_DESCRIPTIONS: Record<ProvenanceKind, string> = {
  identity: "Copied from your verified contact details.",
  resume: "The resume file pinned to this application.",
  saved_answer: "An answer you saved for this question.",
  saved_policy: "One of your standing answer rules: a policy, or an answer you saved for every application.",
  derived: "Derived from one of your saved answers (a salary, your work authorization status or a start date).",
  fact_screener: "Answered from verified facts that state it.",
  narrative: "Written from your verified facts and stories; the facts and passages it cites are listed.",
  user: "Your own answer for this application.",
  blank: "This field was left blank.",
};

export function isProvenanceKind(value: unknown): value is ProvenanceKind {
  return typeof value === "string" && (PROVENANCE_KINDS as readonly string[]).includes(value);
}

/** The badge's kind: an unknown kind from a newer service gets a neutral badge. */
export function badgeKind(provenance: Partial<ReviewProvenanceView> | null | undefined): ProvenanceKind | "other" {
  return isProvenanceKind(provenance?.kind) ? provenance.kind : "other";
}

/** The service's own label, else a plain name for the kind. */
export function provenanceLabel(provenance: Partial<ReviewProvenanceView> | null | undefined): string {
  const label = typeof provenance?.label === "string" ? provenance.label.trim() : "";
  if (label) return label;
  const kind = badgeKind(provenance);
  return kind === "other" ? "Filled in by the desk" : PROVENANCE_LABELS[kind];
}

export function provenanceDescription(kind: ProvenanceKind | "other"): string {
  return kind === "other" ? "Filled in by the desk." : PROVENANCE_DESCRIPTIONS[kind];
}

// ---- rows ----

/** Answers below this confidence are flagged for the person to check. */
export const CHECK_BELOW = 0.9;

export function rowNeedsCheck(row: Pick<ReviewRowView, "confidence">): boolean {
  return typeof row.confidence === "number" && Number.isFinite(row.confidence) && row.confidence < CHECK_BELOW;
}

export function isBlankRow(row: Pick<ReviewRowView, "value" | "provenance">): boolean {
  if (badgeKind(row.provenance) === "blank") return true;
  const values = Array.isArray(row.value) ? row.value : [row.value];
  return values.every((value) => typeof value !== "string" || value.trim() === "");
}

/** Long text worth copying in full: narratives and written answers. */
export function copyableText(row: Pick<ReviewRowView, "control" | "value" | "provenance">): string | null {
  if (typeof row.value !== "string" || row.value.trim() === "") return null;
  return row.control === "long_text" || badgeKind(row.provenance) === "narrative" ? row.value : null;
}

export interface PageGroup<T> {
  /** 1-based form page; null when the service reported no usable page. */
  page: number | null;
  items: T[];
}

/** Rows by form page in page order, keeping the form order within a page. */
export function groupByPage<T extends { page: number }>(rows: readonly T[]): PageGroup<T>[] {
  const groups = new Map<number | null, T[]>();
  for (const row of rows) {
    const page = Number.isInteger(row.page) && row.page >= 1 ? row.page : null;
    const group = groups.get(page);
    if (group) group.push(row);
    else groups.set(page, [row]);
  }
  return [...groups.entries()]
    .map(([page, items]) => ({ page, items }))
    .sort((a, b) => (a.page ?? Number.POSITIVE_INFINITY) - (b.page ?? Number.POSITIVE_INFINITY));
}

export function answersSummary(rows: readonly ReviewRowView[]) {
  return {
    total: rows.length,
    blank: rows.filter(isBlankRow).length,
    toCheck: rows.filter(rowNeedsCheck).length,
    pages: new Set(rows.map((row) => row.page)).size,
  };
}

export function hasCitations(citations: ReviewCitationsView | null | undefined): citations is ReviewCitationsView {
  return Boolean(
    citations &&
      ((citations.facts?.length ?? 0) > 0 ||
        (citations.passages?.length ?? 0) > 0 ||
        (citations.jobEvidence?.length ?? 0) > 0),
  );
}

function count(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "Cites 3 facts, 2 story passages and 1 job note", or null without citations. */
export function citationSummary(citations: ReviewCitationsView | null | undefined): string | null {
  if (!hasCitations(citations)) return null;
  const parts = [
    citations.facts.length ? count(citations.facts.length, "fact", "facts") : null,
    citations.passages.length ? count(citations.passages.length, "story passage", "story passages") : null,
    citations.jobEvidence.length ? count(citations.jobEvidence.length, "job note", "job notes") : null,
  ].filter((part): part is string => part !== null);
  const list = parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}` : parts[0];
  return `Cites ${list}`;
}

// ---- the queue ----

export type QueueFilter = "all" | "review" | "approved" | "browser";

export const QUEUE_FILTERS: readonly { id: QueueFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "review", label: "To review" },
  { id: "approved", label: "Approved" },
  { id: "browser", label: "In the browser" },
];

export function matchesFilter(item: ReviewQueueItemView, filter: QueueFilter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "review":
      return item.stage === "prepared" && !item.approved;
    case "approved":
      return item.approved === true;
    case "browser":
      return item.stage === "browser_action";
  }
}

function timeOf(value: string | null | undefined): number {
  const parsed = typeof value === "string" ? Date.parse(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

/** Newest first by the time the current stop was recorded; the service's order breaks ties. */
export function sortQueue(items: readonly ReviewQueueItemView[]): ReviewQueueItemView[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => timeOf(b.item.stoppedAt) - timeOf(a.item.stoppedAt) || a.index - b.index)
    .map(({ item }) => item);
}

export function filterQueue(items: readonly ReviewQueueItemView[], filter: QueueFilter): ReviewQueueItemView[] {
  return sortQueue(items).filter((item) => matchesFilter(item, filter));
}

export function queueCounts(items: readonly ReviewQueueItemView[]): Record<QueueFilter, number> {
  return {
    all: items.length,
    review: items.filter((item) => matchesFilter(item, "review")).length,
    approved: items.filter((item) => matchesFilter(item, "approved")).length,
    browser: items.filter((item) => matchesFilter(item, "browser")).length,
  };
}

/** The queue entries of a response, or none when it isn't the expected shape. */
export function listedQueue(view: unknown): ReviewQueueItemView[] {
  const applications: unknown = (view as { applications?: unknown } | null | undefined)?.applications;
  if (!Array.isArray(applications)) return [];
  return applications.filter(
    (item): item is ReviewQueueItemView =>
      typeof item === "object" && item !== null && typeof (item as { id?: unknown }).id === "string",
  );
}

/** The application after `currentId` in queue order (the first one when it isn't queued). */
export function nextInQueue(items: readonly ReviewQueueItemView[], currentId: string): ReviewQueueItemView | null {
  const order = sortQueue(items);
  const index = order.findIndex((item) => item.id === currentId);
  const after = index >= 0 ? order.slice(index + 1) : order;
  return after.find((item) => item.id !== currentId) ?? null;
}

export type HoldTone = "ready" | "approved" | "attention" | "browser";

export function holdTone(kind: HoldKind | string): HoldTone {
  switch (kind) {
    case "ready":
      return "ready";
    case "approved":
      return "approved";
    case "sign_in":
    case "captcha":
    case "browser_action":
      return "browser";
    default:
      return "attention";
  }
}

export const STAGE_LABELS: Record<string, string> = {
  prepared: "Prepared · nothing submitted",
  browser_action: "Needs you in the browser",
};

// ---- provider cost ----

export function formatUsd(amount: number): string {
  if (!Number.isFinite(amount) || amount <= 0) return "$0.00";
  if (amount < 0.01) return "under $0.01";
  return `$${amount.toFixed(2)}`;
}

/** "$0.42 · 37 calls, 2 without a reported cost", or a plain note when nothing was recorded. */
export function costText(cost: ProviderCostView | null | undefined): string {
  if (!cost || !Number.isFinite(cost.calls) || cost.calls <= 0) return "No AI calls recorded";
  const unknown = cost.unknownCostCalls > 0 ? `, ${cost.unknownCostCalls} without a reported cost` : "";
  return `${formatUsd(cost.knownUsd)} · ${count(cost.calls, "call", "calls")}${unknown}`;
}

/** The queue's compact cost cell: the known amount, marked when some calls reported none. */
export function costShort(cost: ProviderCostView | null | undefined): string {
  if (!cost || !Number.isFinite(cost.calls) || cost.calls <= 0) return "—";
  return `${formatUsd(cost.knownUsd)}${cost.unknownCostCalls > 0 ? "+" : ""}`;
}

// ---- the execution gate ----

/**
 * Why the review lane may not start browser work (prepare again, resume in the
 * browser, submit) for this application now, or null. The service enforces the same
 * rules: a TEST_ONLY service acts only on local test applications, a LIVE service on
 * the employer's site. The desk keeps its own, stricter test-only gate.
 */
export function reviewExecutionProblem(readiness: ServiceReadiness | null, applicationUrl: string): string | null {
  if (!readiness) {
    return "The local service's readiness couldn't be checked. Reconnect it and try again.";
  }
  if (readiness.runner !== "available") {
    return "The application browser isn't ready. Try again after the local service recovers.";
  }
  if (readiness.applicationMode === "TEST_ONLY") {
    return isLoopbackApplication(applicationUrl)
      ? null
      : "The service is in TEST_ONLY mode, so it acts only on local test applications (localhost, 127.0.0.1 or ::1). Start it in LIVE mode to work on this one.";
  }
  if (readiness.applicationMode === "LIVE") return null;
  return "The service reports an application mode this dashboard doesn't know, so nothing is started from here.";
}

// ---- approve ----

export interface ApproveState {
  available: boolean;
  /** Why approving isn't possible now; null when it is, or when it's already approved. */
  reason: string | null;
}

function openRequired(review: ApplicationReviewView): number {
  const needs = review.application.needs;
  if (needs?.kind !== "questions") return 0;
  return (
    needs.questions.filter((question) => question.required).length +
    needs.attestations.filter((attestation) => attestation.required).length
  );
}

export function approveState(review: ApplicationReviewView): ApproveState {
  if (isActive(review.application.state)) return { available: false, reason: "Wait for the current run to finish." };
  if (review.approval) return { available: false, reason: null };
  if (review.changedSincePreparation) {
    return {
      available: false,
      reason: "You changed answers since this preparation. Prepare it again, then approve the new preparation.",
    };
  }
  if (!review.preparedPacketId) {
    return {
      available: false,
      reason: "Only an application stopped at its final review step can be approved.",
    };
  }
  if (openRequired(review) > 0) {
    return { available: false, reason: "Answer the remaining questions and prepare it again first." };
  }
  return { available: true, reason: null };
}

// ---- submit ----

export interface SubmitState {
  /** The Submit button may open the confirmation. */
  enabled: boolean;
  /** Everything that stops it, the service's own words first. */
  problems: string[];
}

export function submitState(
  review: ApplicationReviewView,
  readiness: ServiceReadiness | null,
  mode: "live" | "preview",
): SubmitState {
  const problems = Array.isArray(review.submit?.problems)
    ? review.submit.problems.filter((problem) => typeof problem === "string" && problem.trim())
    : [];
  const add = (problem: string | null) => {
    if (problem && !problems.includes(problem)) problems.push(problem);
  };
  if (review.submit?.allowed !== true && problems.length === 0) {
    add("The service reports that this application can't be submitted now.");
  }
  if (isActive(review.application.state)) add("Wait for the current run to finish.");
  if (review.submit?.allowed === true && !review.approval) add("Approve this application first.");
  if (mode === "live" && review.submit?.allowed === true) {
    add(reviewExecutionProblem(readiness, review.application.applicationUrl));
    if (readiness?.submission === "disabled") {
      add("Submission is turned off in the service. Start it with IMX_ALLOW_SUBMISSION=1 to submit from here.");
    }
  }
  return { enabled: review.submit?.allowed === true && problems.length === 0, problems };
}

/** The confirmed request: exactly the approved packet, with the person's confirmation. */
export function submitRequest(review: Pick<ApplicationReviewView, "approval">): SubmitInput {
  if (!review.approval) throw new Error("There is no approval to submit.");
  return { packetId: review.approval.packetId, confirm: true };
}

// ---- edit an answer ----

export const REUSE_CHOICES: Record<ReuseChoice, { label: string; hint: string }> = {
  application: { label: "This application only", hint: "Nothing is saved for reuse." },
  job: { label: "This job", hint: "Also saved for reuse whenever this job asks it." },
  global: { label: "Every application", hint: "Also saved for reuse wherever this question is asked." },
};

/** The reuse scopes the service allows for this answer, in a fixed order; this application at least. */
export function allowedReuse(edit: Pick<ReviewEditView, "reuse">): ReuseChoice[] {
  const allowed = new Set(Array.isArray(edit.reuse) ? edit.reuse : []);
  const scopes = (["application", "job", "global"] as const).filter((scope) => allowed.has(scope));
  return scopes.length ? scopes : ["application"];
}

/** The editor's starting value, in the form's own terms. */
export function initialEditValue(edit: ReviewEditView): AnswerValue {
  const value = edit.value;
  if (edit.attestation || edit.control === "boolean") return typeof value === "boolean" ? value : null;
  if (edit.control === "multi_select") return Array.isArray(value) ? [...value] : [];
  return typeof value === "string" ? value : "";
}

function sameValue(a: AnswerValue, b: AnswerValue): boolean {
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    const other = new Set(b);
    return a.every((item) => other.has(item));
  }
  if (typeof a === "string" && typeof b === "string") return a.trim() === b.trim();
  return a === b;
}

/** True when the edited value is the current answer (nothing to save). */
export function unchanged(edit: ReviewEditView, value: AnswerValue): boolean {
  return sameValue(initialEditValue(edit), value);
}

/** Why the edited value can't be saved, before anything is sent; null when it can. */
export function editProblem(edit: ReviewEditView, value: AnswerValue): string | null {
  if (edit.attestation) {
    if (typeof value !== "boolean") return "Check or uncheck the statement.";
    return edit.required && !value ? "The site requires this statement, so it can only stay checked." : null;
  }
  if (isEmptyAnswer(value)) {
    // The answers route skips a blank answer, so a blank can't replace an answer.
    return edit.control === "single_select" || edit.control === "boolean"
      ? "Choose an answer to save."
      : edit.control === "multi_select"
        ? "Choose at least one option to save."
        : "Enter an answer to save. An answer can't be cleared here.";
  }
  const options = Array.isArray(edit.options) ? edit.options.map((option) => option.value) : null;
  if (!edit.lookup && options && edit.control === "single_select" && typeof value === "string" && !options.includes(value)) {
    return "Choose one of the listed options.";
  }
  if (options && edit.control === "multi_select" && Array.isArray(value) && value.some((item) => !options.includes(item))) {
    return "Choose only from the listed options.";
  }
  return null;
}

/**
 * The answers-route body for one changed answer: under its question id, in
 * `attestations` for a statement and in `answers` otherwise, with a reuse scope the
 * service allows for it (this application when the requested one isn't allowed).
 */
export function editRequest(
  row: Pick<ReviewRowView, "questionId" | "edit">,
  value: AnswerValue,
  reuse: ReuseChoice,
): AnswerInput {
  const { edit, questionId } = row;
  if (!edit || !questionId) throw new Error("This answer can't be changed here.");
  const scopes = allowedReuse(edit);
  const scope = scopes.includes(reuse) ? reuse : scopes[0];
  if (edit.attestation) {
    return { answers: {}, attestations: { [questionId]: value === true }, reuse: { [questionId]: scope } };
  }
  const sent = typeof value === "string" && edit.control !== "long_text" ? value.trim() : value;
  return { answers: { [questionId]: sent }, attestations: {}, reuse: { [questionId]: scope } };
}

// ---- states ----

export function isWorkingState(state: ApplicationState): boolean {
  return isActive(state);
}

/** The review page's own outcome line once a submission ran (or was settled), else null. */
export function outcomeLine(review: ApplicationReviewView): string | null {
  const app = review.application;
  switch (app.state) {
    case "SUBMITTED": {
      // A receipt the person reported (settled on the desk) is never shown as the site's.
      const confirmed = app.receipt && receiptAuthority(app.receipt).byUser
        ? "Submitted, on your report of a confirmation."
        : "Submitted and confirmed by the site.";
      return app.receipt?.confirmationReference
        ? `${confirmed} Reference ${app.receipt.confirmationReference}.`
        : confirmed;
    }
    case "SUBMISSION_UNKNOWN":
      return "Submitted, but the site didn't confirm it. It's never sent again automatically; settle it on the desk.";
    case "FAILED_RETRYABLE":
      return `Stopped before submitting: ${app.failure?.reason ?? "the run couldn't continue"}. Nothing was sent.`;
    case "FAILED_PERMANENT":
      return `This application can't be completed: ${app.failure?.reason ?? "the site refused it"}.`;
    case "DUPLICATE":
      return "You've already applied to this job. Nothing was sent.";
    case "WITHDRAWN":
      return "This application was withdrawn.";
    default:
      return null;
  }
}
