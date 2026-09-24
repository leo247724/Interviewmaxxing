import type {
  AnswerValue,
  ApplicationView,
  PreparationView,
  QuestionOption,
  RequiredQuestionView,
  ReviewAnswerView,
  ReviewSource,
} from "./service/types";

/**
 * Pure helpers for prepared applications (filled, stopped at the site's final
 * review step, nothing submitted) and for site lookup questions.
 */

type PreparedFields = Pick<ApplicationView, "state"> & Partial<Pick<ApplicationView, "preparation">>;

/**
 * A prepared application: every page was filled and the run stopped at the
 * site's final review step without submitting. Services before presentation
 * version 2 omit `preparation`, which counts as not prepared.
 */
export function isPrepared(view: PreparedFields): boolean {
  return view.state === "NEEDS_INPUT" && Boolean(view.preparation);
}

export function preparationOf(view: PreparedFields): PreparationView | null {
  return isPrepared(view) ? (view.preparation ?? null) : null;
}

/** Answers the service filled in; older services omit the list. */
export function reviewOf(view: Partial<Pick<ApplicationView, "review">>): ReviewAnswerView[] {
  return Array.isArray(view.review) ? view.review : [];
}

// ---- Review list ----

const SOURCE_LABELS: Record<ReviewSource, string> = {
  identity: "Your details",
  saved_answer: "Saved answer",
  fact: "From your profile",
  user: "Your answer",
  generated: "Drafted from your facts",
  resume: "Your resume",
};

/** Plain words for where an answer came from. */
export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source as ReviewSource] ?? "Filled in by the desk";
}

/** Answers below this confidence are flagged for the person to check. */
export const LOW_CONFIDENCE = 0.9;

/** "86% confident", or null when the resolver was certain (or reported nothing usable). */
export function confidenceLabel(confidence: number): string | null {
  if (typeof confidence !== "number" || !Number.isFinite(confidence) || confidence >= 1) return null;
  // Never round a doubt up to 100%.
  const percent = Math.min(99, Math.max(0, Math.round(confidence * 100)));
  return `${percent}% confident`;
}

export function needsCheck(item: Pick<ReviewAnswerView, "confidence">): boolean {
  return typeof item.confidence === "number" && Number.isFinite(item.confidence) && item.confidence < LOW_CONFIDENCE;
}

export interface ReviewPageGroup {
  /** 1-based form page; null when the service reported no usable page. */
  page: number | null;
  items: ReviewAnswerView[];
}

function pageOf(item: Pick<ReviewAnswerView, "page">): number | null {
  return Number.isInteger(item.page) && item.page >= 1 ? item.page : null;
}

/** Group answers by form page in page order, keeping the form order within a page. */
export function groupReviewByPage(items: ReviewAnswerView[]): ReviewPageGroup[] {
  const groups = new Map<number | null, ReviewAnswerView[]>();
  for (const item of items) {
    const page = pageOf(item);
    const group = groups.get(page);
    if (group) group.push(item);
    else groups.set(page, [item]);
  }
  return [...groups.entries()]
    .map(([page, grouped]) => ({ page, items: grouped }))
    .sort((a, b) => (a.page ?? Number.POSITIVE_INFINITY) - (b.page ?? Number.POSITIVE_INFINITY));
}

export function reviewSummary(items: ReviewAnswerView[]) {
  return {
    total: items.length,
    pages: new Set(items.map(pageOf)).size,
    toCheck: items.filter(needsCheck).length,
  };
}

export type ReviewDisplay =
  | { kind: "text"; text: string }
  | { kind: "long_text"; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "file"; name: string }
  | { kind: "blank" };

function yesNo(text: string): string {
  const lower = text.trim().toLowerCase();
  if (lower === "yes" || lower === "true") return "Yes";
  if (lower === "no" || lower === "false") return "No";
  return text;
}

/** How one filled answer reads on screen. */
export function reviewDisplay(item: Pick<ReviewAnswerView, "control" | "value">): ReviewDisplay {
  const values = (Array.isArray(item.value) ? item.value : [item.value])
    .filter((value): value is string => typeof value === "string")
    .filter((value) => value.trim() !== "");
  if (values.length === 0) return { kind: "blank" };
  switch (item.control) {
    case "multi_select":
      return { kind: "list", items: values };
    case "long_text":
      return { kind: "long_text", text: values.join("\n\n") };
    case "boolean":
      return { kind: "text", text: values.map(yesNo).join(", ") };
    case "file":
      return { kind: "file", name: values.join(", ") };
    default:
      return { kind: "text", text: values.join(", ") };
  }
}

// ---- Lookup questions (location, school or company search boxes) ----

/** Select value for "Enter a different value…". Never sent as an answer. */
export const LOOKUP_OTHER = "__imx_lookup_other__";

/** The site's suggestions for a lookup question; empty when it isn't a lookup or offered none. */
export function lookupSuggestions(question: Pick<RequiredQuestionView, "lookup" | "options">): QuestionOption[] {
  return question.lookup && Array.isArray(question.options) ? question.options : [];
}

/**
 * A saved lookup answer that isn't one of the site's suggestions was typed by
 * the person, so the control starts in "different value" mode with that text.
 */
export function startsWithOtherValue(question: Pick<RequiredQuestionView, "lookup" | "options" | "value">): boolean {
  const suggestions = lookupSuggestions(question);
  if (suggestions.length === 0) return false;
  const value = question.value;
  return typeof value === "string" && value.trim() !== "" && !suggestions.some((option) => option.value === value);
}

/** What the lookup's select shows for the current answer. */
export function lookupSelectValue(
  question: Pick<RequiredQuestionView, "lookup" | "options">,
  value: AnswerValue | undefined,
  other: boolean,
): string {
  if (other) return LOOKUP_OTHER;
  return typeof value === "string" && lookupSuggestions(question).some((option) => option.value === value) ? value : "";
}

/** The answer to send: the sentinel never leaves the page. */
export function lookupAnswer(value: AnswerValue): AnswerValue {
  return value === LOOKUP_OTHER ? "" : value;
}
