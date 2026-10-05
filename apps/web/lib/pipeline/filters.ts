import type { AutoApplyStatus, AutoApplyView, PipelineEntryView, PipelineHistoryItem } from "./types";

/**
 * Pure helpers for the pipeline's summary buttons and the backend /
 * autonomous-apply filter chips. Nothing here talks to the service.
 */

// ---------- Summary buttons ----------

export type PipelineSummary = "found" | "applied" | "interested" | "interviewing" | "waiting";

export const SUMMARY_ORDER: readonly PipelineSummary[] = ["found", "applied", "interested", "interviewing", "waiting"];

export const SUMMARY_LABELS: Record<PipelineSummary, string> = {
  found: "Total Jobs Found",
  applied: "Total Jobs Applied",
  interested: "Total Jobs Currently Interested",
  interviewing: "Total Jobs Currently Interviewing",
  waiting: "Total Jobs Waiting for Offer",
};

/** Found and Applied count every card ever; the other three reflect the lanes cards sit in now. */
export const SUMMARY_DESCRIPTIONS: Record<PipelineSummary, string> = {
  found: "All-time counter: every job on the board.",
  applied: "All-time counter: every job you have applied to, including ones closed or rejected since.",
  interested: "Reflects the current lanes: Interest through Awaiting Decision.",
  interviewing: "Reflects the current lanes: the interview rounds.",
  waiting: "Reflects the current lanes: Awaiting Decision.",
};

/** Lanes a card can only reach after applying. */
export const APPLIED_LANES: ReadonlySet<string> = new Set([
  "applied",
  "interest",
  "interviewing",
  "interview-2",
  "interview-3",
  "decision",
  "offer",
]);

/** Lanes each summary covers; `null` means every lane. */
export const SUMMARY_LANES: Record<PipelineSummary, ReadonlySet<string> | null> = {
  found: null,
  applied: APPLIED_LANES,
  interested: new Set(["interest", "interviewing", "interview-2", "interview-3", "decision"]),
  interviewing: new Set(["interviewing", "interview-2", "interview-3"]),
  waiting: new Set(["decision"]),
};

export type SummaryEntry = Pick<PipelineEntryView, "lane"> & {
  history?: readonly Pick<PipelineHistoryItem, "toLane">[] | null;
};

/**
 * All-time: the card sits in a lane only reachable after applying, or was
 * moved to Applied at some point (closed or rejected cards still count).
 */
export function everApplied(entry: SummaryEntry): boolean {
  return APPLIED_LANES.has(entry.lane) || Boolean(entry.history?.some((item) => item.toLane === "applied"));
}

export function isSummary(value: unknown): value is PipelineSummary {
  return typeof value === "string" && (SUMMARY_ORDER as readonly string[]).includes(value);
}

/** Whether a lane belongs to a summary by itself (Applied can also include other lanes' once-applied cards). */
export function laneInSummary(lane: string, summary: PipelineSummary): boolean {
  const lanes = SUMMARY_LANES[summary];
  return lanes === null || lanes.has(lane);
}

export function entryInSummary(entry: SummaryEntry, summary: PipelineSummary): boolean {
  return summary === "applied" ? everApplied(entry) : laneInSummary(entry.lane, summary);
}

export function summaryCounts(entries: readonly SummaryEntry[]): Record<PipelineSummary, number> {
  const counts: Record<PipelineSummary, number> = { found: 0, applied: 0, interested: 0, interviewing: 0, waiting: 0 };
  for (const entry of entries) {
    for (const summary of SUMMARY_ORDER) if (entryInSummary(entry, summary)) counts[summary] += 1;
  }
  return counts;
}

// ---------- Labels ----------

export const UNKNOWN_BACKEND = "Unknown";

export const STATUS_LABELS: Record<AutoApplyStatus, string> = {
  submitted: "Applied",
  ready: "Ready to apply",
  held: "Held",
  blocked: "Blocked",
  unsupported: "Unsupported",
  not_attempted: "Not attempted",
  closed: "Closed",
};

const STATUS_ORDER: readonly string[] = ["submitted", "ready", "held", "blocked", "unsupported", "not_attempted", "closed"];

export const BOTTLENECK_LABELS: Record<string, string> = {
  none: "",
  needs_facts: "Needs your facts",
  writer_held: "Writer held",
  captcha: "CAPTCHA (you)",
  login_required: "Login required",
  no_driver: "No driver",
  company_cap: "Company cap (1/week)",
  send_failed: "Send failed",
  no_form: "No form found",
  spam_refused: "Refused as spam",
  needs_code: "Email code pending",
  aggregator_link: "Aggregator link (re-home)",
};

function humanize(value: string): string {
  const text = value.replace(/[_-]+/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status as AutoApplyStatus] ?? humanize(status);
}

/** Human label for a bottleneck; "" for none or a missing value. */
export function bottleneckLabel(bottleneck: string | null | undefined): string {
  if (!bottleneck) return "";
  return BOTTLENECK_LABELS[bottleneck] ?? humanize(bottleneck);
}

export function backendKey(autoApply: AutoApplyView | null | undefined): string {
  return autoApply?.backendLabel?.trim() || autoApply?.backend?.trim() || UNKNOWN_BACKEND;
}

/** "Held · Needs your facts", or just the status when nothing is holding it up. */
export function autoApplyLine(autoApply: Pick<AutoApplyView, "status" | "bottleneck">): string {
  const bottleneck = bottleneckLabel(autoApply.bottleneck);
  return bottleneck ? `${statusLabel(autoApply.status)} · ${bottleneck}` : statusLabel(autoApply.status);
}

// ---------- Filter chips ----------

export interface AutoApplyFilters {
  /** Backend labels (or "Unknown"); empty = any. */
  backends: string[];
  /** Status ids; empty = any. */
  statuses: string[];
  /** Bottleneck ids; empty = any. */
  bottlenecks: string[];
}

export const EMPTY_FILTERS: AutoApplyFilters = { backends: [], statuses: [], bottlenecks: [] };

export function filtersActive(filters: AutoApplyFilters): boolean {
  return filters.backends.length + filters.statuses.length + filters.bottlenecks.length > 0;
}

/**
 * Multi-select within a group (any chip matches), groups combine with AND. A
 * card without autoApply matches the "Unknown" backend chip and fails any
 * status or bottleneck chip.
 */
export function matchesAutoApplyFilters(entry: Pick<PipelineEntryView, "autoApply">, filters: AutoApplyFilters): boolean {
  const autoApply = entry.autoApply ?? null;
  if (filters.backends.length && !filters.backends.includes(backendKey(autoApply))) return false;
  if (filters.statuses.length && (!autoApply || !filters.statuses.includes(autoApply.status))) return false;
  if (filters.bottlenecks.length && (!autoApply || !filters.bottlenecks.includes(autoApply.bottleneck))) return false;
  return true;
}

export interface FilterOption {
  /** What is stored in the filter. */
  value: string;
  label: string;
  count: number;
}

function sortedOptions(counts: Map<string, number>, label: (value: string) => string, order?: readonly string[]): FilterOption[] {
  return [...counts.entries()]
    .map(([value, count]) => ({ value, label: label(value), count }))
    .sort((a, b) => {
      if (order) {
        const ai = order.indexOf(a.value);
        const bi = order.indexOf(b.value);
        if (ai !== bi) return (ai === -1 ? order.length : ai) - (bi === -1 ? order.length : bi);
      }
      return b.count - a.count || a.label.localeCompare(b.label);
    });
}

/** Chips to offer, built from what is present in the data, with counts. */
export function filterOptions(entries: readonly Pick<PipelineEntryView, "autoApply">[]): {
  backends: FilterOption[];
  statuses: FilterOption[];
  bottlenecks: FilterOption[];
} {
  const backends = new Map<string, number>();
  const statuses = new Map<string, number>();
  const bottlenecks = new Map<string, number>();
  const bump = (map: Map<string, number>, key: string) => map.set(key, (map.get(key) ?? 0) + 1);
  for (const entry of entries) {
    const autoApply = entry.autoApply ?? null;
    bump(backends, backendKey(autoApply));
    if (!autoApply) continue;
    bump(statuses, autoApply.status);
    if (bottleneckLabel(autoApply.bottleneck)) bump(bottlenecks, autoApply.bottleneck);
  }
  const backendOptions = sortedOptions(backends, (value) => value);
  // "Unknown" always comes last, whatever its count.
  backendOptions.sort((a, b) => Number(a.value === UNKNOWN_BACKEND) - Number(b.value === UNKNOWN_BACKEND));
  return {
    backends: backendOptions,
    statuses: sortedOptions(statuses, statusLabel, STATUS_ORDER),
    bottlenecks: sortedOptions(bottlenecks, bottleneckLabel),
  };
}

export function toggleValue(values: readonly string[], value: string): string[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

// ---------- Persistence ----------

export const FILTER_STORAGE_KEY = "imx.pipelineFilters";

export interface StoredPipelineFilters extends AutoApplyFilters {
  summary: PipelineSummary;
  /** The Backend and Autonomous apply chip groups are collapsed. */
  hidden: boolean;
}

const stringList = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

/** Parses stored filters; anything malformed falls back to no filters. */
export function parseStoredFilters(raw: string | null | undefined): StoredPipelineFilters {
  const fallback: StoredPipelineFilters = { summary: "found", ...EMPTY_FILTERS, hidden: false };
  if (!raw) return fallback;
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== "object" || value === null) return fallback;
    const record = value as Record<string, unknown>;
    return {
      summary: isSummary(record.summary) ? record.summary : "found",
      backends: stringList(record.backends),
      statuses: stringList(record.statuses),
      bottlenecks: stringList(record.bottlenecks),
      hidden: record.hidden === true,
    };
  } catch {
    return fallback;
  }
}

type Storage = Pick<globalThis.Storage, "getItem" | "setItem">;

export function readStoredFilters(storage?: Storage | null): StoredPipelineFilters {
  try {
    const store = storage ?? (typeof window !== "undefined" ? window.localStorage : null);
    return parseStoredFilters(store?.getItem(FILTER_STORAGE_KEY));
  } catch {
    return parseStoredFilters(null);
  }
}

export function writeStoredFilters(filters: StoredPipelineFilters, storage?: Storage | null): void {
  try {
    const store = storage ?? (typeof window !== "undefined" ? window.localStorage : null);
    store?.setItem(FILTER_STORAGE_KEY, JSON.stringify(filters));
  } catch {
    // Storage blocked or full: filters simply aren't remembered.
  }
}
