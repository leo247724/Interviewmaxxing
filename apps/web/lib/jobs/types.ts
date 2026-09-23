/**
 * Presentation models for job search (J1) and Jev selection (J2), mirroring the
 * core D0 discovery contracts. Only observed facts are shown; anything a source
 * didn't state stays unknown. A Jev decision is never an application receipt.
 */

export type SourceName = "linkedin" | "builtin" | "indeed" | "google" | (string & {});
export type WorkArrangement = "ONSITE" | "HYBRID" | "REMOTE" | "UNKNOWN";
export type CompensationPeriod = "YEAR" | "MONTH" | "WEEK" | "DAY" | "HOUR";
export type SelectionChoice = "APPLY" | "SKIP" | "REVIEW";

/** D0 `LocationPriority`. A ranking preference only: remote stays eligible under every value. */
export type LocationPriority = "STRONGLY_PREFER_ONSITE_HYBRID" | "BALANCED" | "PREFER_REMOTE";

export const LOCATION_PRIORITIES: readonly LocationPriority[] = [
  "STRONGLY_PREFER_ONSITE_HYBRID",
  "BALANCED",
  "PREFER_REMOTE",
];

/**
 * How a listing's observed location relates to the targets. Set by the service when
 * it computes one; otherwise the UI derives it from the listing's stated facts.
 */
export type LocationTier =
  /** Onsite or hybrid in a target city, as the listing states. */
  | "ONSITE_HYBRID_TARGET"
  /** Remote and stated as open to the remote region. */
  | "REMOTE_ELIGIBLE"
  /** Remote, but the listing doesn't say where you can work from. */
  | "REMOTE_UNCONFIRMED"
  /** Onsite or hybrid somewhere other than a target city. */
  | "OUTSIDE_TARGET"
  /** Location or arrangement isn't stated well enough to place it. */
  | "UNRESOLVED";

/** Service ordering, separate from a listing's observed location match. */
export type PriorityTier = "PREFERRED" | "EQUAL" | "SECONDARY" | "UNRANKED";

export interface OnsiteTargetView {
  location: string;
  arrangements: ("ONSITE" | "HYBRID")[];
}

export interface RemoteTargetView {
  /** Where a remote role must allow the candidate to work, e.g. "United States". */
  eligibleRegion: string;
}

export interface CompensationFloorView {
  amount: number;
  currency: string;
  period: CompensationPeriod;
}

export interface SearchPreferencesView {
  titlePhrases: string[];
  /** Responsibilities Jev should look for; search titles are examples, not an allowlist. */
  roleFocus: string;
  keywords: string[];
  excludedKeywords: string[];
  excludedCompanies: string[];
  onsite: OnsiteTargetView[];
  remote: RemoteTargetView | null;
  minimumCompensation: CompensationFloorView | null;
  unknownCompensation: "KEEP" | "REVIEW";
  /** How strongly target-city onsite/hybrid roles rank above remote ones. */
  locationPriority: LocationPriority;
  sources: SourceName[];
  maxResultsPerSource: number;
  /** Changes whenever a selection-relevant preference changes. */
  fingerprint: string | null;
}

export type SourceState = "QUEUED" | "RUNNING" | "OK" | "PARTIAL" | "NEEDS_USER" | "BLOCKED" | "ERROR" | "SKIPPED";

export interface SourceResultView {
  source: SourceName;
  state: SourceState;
  resultCount: number;
  message: string | null;
  /** What the user must do, e.g. "Sign in to LinkedIn in the imx-jobs-linkedin window". */
  userAction: string | null;
  sessionName: string | null;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface SearchRunView {
  id: string;
  startedAt: string;
  finishedAt: string | null;
  results: SourceResultView[];
}

export interface CompensationView {
  rawText: string | null;
  minimum: number | null;
  maximum: number | null;
  currency: string | null;
  period: CompensationPeriod | null;
}

export interface ListingSourceView {
  source: SourceName;
  sourceUrl: string;
  /** Job-specific posting; sourceUrl may instead be the search page. */
  postingUrl?: string | null;
  applicationUrl: string | null;
  observedAt: string;
}

export interface PolicyHoldView {
  code: string;
  detail: string;
}

export interface SelectionView {
  id: string;
  effectiveChoice: SelectionChoice;
  /** Jev's own answer, before policy holds. Null when the provider failed. */
  modelChoice: SelectionChoice | null;
  probabilities: Partial<Record<SelectionChoice, number>> | null;
  confidence: number | null;
  requestedModel: string;
  returnedModel: string | null;
  rubricVersion: string;
  holds: PolicyHoldView[];
  reasons: string[];
  /** Facts the decision needed but couldn't establish. */
  unresolved: string[];
  providerError: { code: string; message: string; retryable: boolean } | null;
  decidedAt: string;
  /** True when preferences or listing evidence changed after this decision. */
  stale: boolean;
}

export interface ListingView {
  id: string;
  title: string;
  company: string | null;
  location: string | null;
  workArrangement: WorkArrangement;
  remoteEligibility: string | null;
  compensation: CompensationView | null;
  description: string | null;
  descriptionCompleteness: "FULL" | "PARTIAL" | "NONE";
  status: "OPEN" | "CLOSED" | "UNKNOWN";
  postedText: string | null;
  observedAt: string;
  /** Every source that showed this listing; at least one. */
  provenance: ListingSourceView[];
  selection: SelectionView | null;
  pipelineEntryId: string | null;
  applicationId: string | null;
  /** Optional service-computed tier; absent until S1 provides it. */
  locationTier?: LocationTier | null;
  priorityTier?: PriorityTier | null;
  rankReason?: string | null;
  /** HTTP-only progress marker for a 202 decision response. Never a decision. */
  decisionPending?: boolean;
  decisionTask?: {
    id: string;
    state: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "INTERRUPTED";
    error: string | { code?: string; message: string } | null;
    resultId?: string | null;
  } | null;
}

export interface ListingsView {
  listings: ListingView[];
  lastRun: SearchRunView | null;
}

export interface JobsService {
  readonly mode: "live" | "preview";
  preferences(): Promise<SearchPreferencesView>;
  savePreferences(input: Omit<SearchPreferencesView, "fingerprint">): Promise<SearchPreferencesView>;
  startSearch(input: Omit<SearchPreferencesView, "fingerprint">): Promise<SearchRunView>;
  searchStatus(runId: string): Promise<SearchRunView>;
  listings(): Promise<ListingsView>;
  listing(listingId: string): Promise<ListingView>;
  /** Ask Jev for a decision on one listing. Never applies. */
  decide(listingId: string): Promise<ListingView>;
  /** Add the listing to the pipeline tracker. */
  track(listingId: string): Promise<ListingView>;
}

export const DEFAULT_PREFERENCES: Omit<SearchPreferencesView, "fingerprint"> = {
  titlePhrases: [
    "paid media manager", "senior paid media manager", "performance marketing manager",
    "growth marketing manager", "demand generation manager", "digital marketing manager",
    "marketing manager", "marketing director",
  ],
  roleFocus: "Performance marketing operator: hands-on paid acquisition, paid media, " +
    "performance and growth marketing, demand generation, and digital marketing leadership. " +
    "Judge actual responsibilities and ownership, not an exact job-title match. " +
    "Semantically similar acquisition, lead and director roles are eligible. " +
    "Pure data, software or platform engineering and unrelated marketing specialties " +
    "are outside this focus.",
  keywords: [],
  excludedKeywords: [],
  excludedCompanies: [],
  onsite: [{ location: "Austin, TX", arrangements: ["ONSITE", "HYBRID"] }],
  remote: { eligibleRegion: "United States" },
  minimumCompensation: { amount: 100_000, currency: "USD", period: "YEAR" },
  unknownCompensation: "KEEP",
  locationPriority: "STRONGLY_PREFER_ONSITE_HYBRID",
  sources: ["linkedin", "builtin", "indeed", "google"],
  maxResultsPerSource: 50,
};

export const SOURCE_LABELS: Record<string, string> = {
  linkedin: "LinkedIn Jobs",
  builtin: "Built In",
  indeed: "Indeed",
  google: "Google Jobs",
};
