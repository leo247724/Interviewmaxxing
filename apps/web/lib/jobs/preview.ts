import { ServiceError } from "../service/errors";
import { EMPTY_FIELDS } from "../pipeline/types";
import { previewPipeline } from "../pipeline/previewStore";
import {
  DEFAULT_PREFERENCES,
  type JobsService,
  type ListingView,
  type ListingsView,
  type SearchPreferencesView,
  type SearchRunView,
  type SelectionView,
  type SourceResultView,
} from "./types";
import { validatePreferences } from "./validation";

/**
 * In-memory job search used only by /preview/jobs and tests. Companies, listings
 * and decisions are fictional; source states mirror what J1/J2 report.
 */

type PreferencesInput = Omit<SearchPreferencesView, "fingerprint">;

const OBSERVED = "2026-09-22T14:05:00Z";

function listing(partial: Partial<ListingView> & Pick<ListingView, "id" | "title" | "provenance">): ListingView {
  return {
    company: null,
    location: null,
    workArrangement: "UNKNOWN",
    remoteEligibility: null,
    compensation: null,
    description: null,
    descriptionCompleteness: "NONE",
    status: "UNKNOWN",
    postedText: null,
    observedAt: OBSERVED,
    selection: null,
    pipelineEntryId: null,
    applicationId: null,
    ...partial,
  };
}

function selection(partial: Partial<SelectionView> & Pick<SelectionView, "id" | "effectiveChoice">): SelectionView {
  return {
    modelChoice: partial.effectiveChoice,
    probabilities: null,
    confidence: null,
    requestedModel: "typesafe/jev-1.13",
    returnedModel: "typesafe/jev-1.13-20260917",
    rubricVersion: "selection-rubric-v1",
    holds: [],
    reasons: [],
    unresolved: [],
    providerError: null,
    decidedAt: "2026-09-22T14:20:00Z",
    stale: false,
    ...partial,
  };
}

const LISTINGS: ListingView[] = [
  listing({
    id: "lst_pv_meridian",
    title: "Senior Marketing Manager, Demand Generation",
    company: "Meridian Loop Software",
    location: "Austin, TX",
    workArrangement: "HYBRID",
    compensation: {
      rawText: "$125,000 – $145,000 a year",
      minimum: 125000,
      maximum: 145000,
      currency: "USD",
      period: "YEAR",
    },
    description:
      "Own pipeline targets for the mid-market segment. Lead paid search, paid social and lifecycle programs with a team of three. Hybrid: two days a week in the Austin office.",
    descriptionCompleteness: "FULL",
    status: "OPEN",
    postedText: "Posted 3 days ago",
    provenance: [
      {
        source: "builtin",
        sourceUrl: "https://builtin.example.test/job/meridian-loop/senior-marketing-manager-demand-gen/481516",
        applicationUrl: "https://careers.meridianloop.example.test/jobs/4815/apply",
        observedAt: OBSERVED,
      },
      {
        source: "google",
        sourceUrl: "https://www.google.example.test/search?ibp=htl;jobs#meridian-4815",
        applicationUrl: "https://careers.meridianloop.example.test/jobs/4815/apply",
        observedAt: OBSERVED,
      },
    ],
    selection: selection({
      id: "sel_pv_meridian",
      effectiveChoice: "APPLY",
      probabilities: { APPLY: 0.81, REVIEW: 0.15, SKIP: 0.04 },
      confidence: 0.81,
      reasons: [
        "Title and scope match a senior marketing manager target.",
        "Hybrid in Austin matches the onsite preference.",
        "Posted pay ($125,000–$145,000 a year) is above the $100,000 floor.",
      ],
    }),
  }),
  listing({
    id: "lst_pv_saltgrass",
    title: "Director of Marketing",
    company: "Saltgrass Outdoor Co.",
    location: "Remote",
    workArrangement: "REMOTE",
    remoteEligibility: null,
    compensation: { rawText: "Competitive", minimum: null, maximum: null, currency: null, period: null },
    description: "Lead brand and growth marketing for a direct-to-consumer outdoor brand. Fully remote team.",
    descriptionCompleteness: "PARTIAL",
    status: "OPEN",
    postedText: "Posted 1 week ago",
    provenance: [
      {
        source: "indeed",
        sourceUrl: "https://www.indeed.example.test/viewjob?jk=saltgrass-dir-mktg",
        applicationUrl: null,
        observedAt: OBSERVED,
      },
    ],
    selection: selection({
      id: "sel_pv_saltgrass",
      effectiveChoice: "REVIEW",
      modelChoice: "REVIEW",
      probabilities: { APPLY: 0.38, REVIEW: 0.55, SKIP: 0.07 },
      confidence: 0.55,
      reasons: [
        "Director scope matches a target title.",
        "Remote role, but the listing doesn't say where you can work from.",
      ],
      unresolved: [
        "Pay isn't stated (“Competitive”).",
        "Remote eligibility isn't stated, so US-wide eligibility is unconfirmed.",
      ],
    }),
  }),
  listing({
    id: "lst_pv_bluebonnet",
    title: "Marketing Manager",
    company: "Bluebonnet Dental Partners",
    location: "Austin, TX",
    workArrangement: "ONSITE",
    compensation: {
      rawText: "$78,000 – $92,000 per year",
      minimum: 78000,
      maximum: 92000,
      currency: "USD",
      period: "YEAR",
    },
    description: "Manage local marketing for twelve dental offices across Central Texas.",
    descriptionCompleteness: "PARTIAL",
    status: "OPEN",
    postedText: "Posted today",
    provenance: [
      {
        source: "indeed",
        sourceUrl: "https://www.indeed.example.test/viewjob?jk=bluebonnet-mm",
        applicationUrl: "https://bluebonnet.example.test/careers/marketing-manager/apply",
        observedAt: OBSERVED,
      },
    ],
    selection: selection({
      id: "sel_pv_bluebonnet",
      effectiveChoice: "SKIP",
      modelChoice: "APPLY",
      probabilities: { APPLY: 0.52, REVIEW: 0.3, SKIP: 0.18 },
      confidence: 0.52,
      holds: [{ code: "HARD_CONSTRAINT", detail: "Posted maximum $92,000 a year is below your $100,000 minimum." }],
      reasons: ["Title and Austin onsite location match."],
    }),
  }),
  listing({
    id: "lst_pv_northwind",
    title: "Marketing Director, Lifecycle",
    company: "Northwind Cartography",
    location: "Remote (United States)",
    workArrangement: "REMOTE",
    remoteEligibility: "United States",
    compensation: { rawText: "$150,000 – $170,000", minimum: 150000, maximum: 170000, currency: "USD", period: "YEAR" },
    descriptionCompleteness: "PARTIAL",
    description: "Lead lifecycle marketing across email, in-app and push.",
    status: "CLOSED",
    postedText: "No longer accepting applications",
    provenance: [
      {
        source: "linkedin",
        sourceUrl: "https://www.linkedin.example.test/jobs/view/northwind-lifecycle-director",
        applicationUrl: null,
        observedAt: OBSERVED,
      },
    ],
    selection: selection({
      id: "sel_pv_northwind",
      effectiveChoice: "SKIP",
      modelChoice: null,
      probabilities: null,
      confidence: null,
      returnedModel: null,
      holds: [{ code: "LISTING_CLOSED", detail: "The source says this job no longer accepts applications." }],
    }),
  }),
  listing({
    id: "lst_pv_tessera",
    title: "Product Marketing Manager",
    company: "Tessera Robotics",
    location: "Austin, TX",
    workArrangement: "HYBRID",
    compensation: { rawText: "$140K - $160K", minimum: 140000, maximum: 160000, currency: "USD", period: "YEAR" },
    description:
      "Launch and position warehouse robotics products. Partner with sales on enablement. Three days a week in the Austin office.",
    descriptionCompleteness: "FULL",
    status: "OPEN",
    postedText: "Posted 2 days ago",
    provenance: [
      {
        source: "builtin",
        sourceUrl: "https://builtin.example.test/job/tessera-robotics/product-marketing-manager/77120",
        applicationUrl: "https://tessera.example.test/careers/pmm/apply",
        observedAt: OBSERVED,
      },
    ],
  }),
  listing({
    id: "lst_pv_copperline",
    title: "Head of Marketing",
    company: "Copperline Credit",
    location: "Remote (US)",
    workArrangement: "REMOTE",
    remoteEligibility: "United States",
    compensation: { rawText: "$150,000/yr", minimum: 150000, maximum: 150000, currency: "USD", period: "YEAR" },
    description: "Build the marketing function at a Series B fintech.",
    descriptionCompleteness: "PARTIAL",
    status: "OPEN",
    postedText: "Posted 5 days ago",
    provenance: [
      {
        source: "google",
        sourceUrl: "https://www.google.example.test/search?ibp=htl;jobs#copperline-hom",
        applicationUrl: "https://jobs.copperline.example.test/head-of-marketing",
        observedAt: OBSERVED,
      },
    ],
    selection: selection({
      id: "sel_pv_copperline",
      effectiveChoice: "REVIEW",
      modelChoice: null,
      probabilities: null,
      confidence: null,
      returnedModel: null,
      holds: [{ code: "PROVIDER_ERROR", detail: "Jev couldn't be reached, so nothing was decided." }],
      providerError: { code: "402", message: "The decision provider reported insufficient credits.", retryable: false },
    }),
  }),
  listing({
    id: "lst_pv_larkloom",
    title: "Senior Manager, Brand Marketing",
    company: "Lark & Loom",
    location: "United States",
    workArrangement: "UNKNOWN",
    compensation: null,
    description: "Guide brand campaigns for a home goods label.",
    descriptionCompleteness: "PARTIAL",
    status: "OPEN",
    postedText: null,
    provenance: [
      {
        source: "google",
        sourceUrl: "https://www.google.example.test/search?ibp=htl;jobs#larkloom-brand",
        applicationUrl: null,
        observedAt: OBSERVED,
      },
    ],
  }),
];

const FINAL_RESULTS: Record<string, Omit<SourceResultView, "source" | "startedAt" | "finishedAt">> = {
  linkedin: {
    state: "NEEDS_USER",
    resultCount: 0,
    message: "LinkedIn asked for a sign-in before showing results.",
    userAction: "Sign in to LinkedIn in the imx-jobs-linkedin browser window, then search again.",
    sessionName: "imx-jobs-linkedin",
  },
  builtin: { state: "OK", resultCount: 2, message: null, userAction: null, sessionName: "imx-jobs-builtin" },
  indeed: {
    state: "PARTIAL",
    resultCount: 2,
    message: "Stopped after page 1: the next page didn't load.",
    userAction: null,
    sessionName: "imx-jobs-indeed",
  },
  google: {
    state: "OK",
    resultCount: 3,
    message: "1 listing also appeared on Built In.",
    userAction: null,
    sessionName: "imx-jobs-google",
  },
};

export class PreviewJobsService implements JobsService {
  readonly mode = "preview" as const;
  private prefs: SearchPreferencesView = {
    ...structuredClone(DEFAULT_PREFERENCES),
    fingerprint: fingerprint(DEFAULT_PREFERENCES),
  };
  private listingsState: ListingView[] = [];
  private run: (SearchRunView & { polls: number; sources: string[] }) | null = null;
  private counter = 0;
  private readonly stepPolls: number;

  constructor(options: { seeded?: boolean; stepPolls?: number } = {}) {
    this.stepPolls = options.stepPolls ?? 2;
    if (options.seeded !== false) this.listingsState = structuredClone(LISTINGS);
  }

  async preferences(): Promise<SearchPreferencesView> {
    return structuredClone(this.prefs);
  }

  async savePreferences(input: PreferencesInput): Promise<SearchPreferencesView> {
    const errors = validatePreferences(input);
    if (Object.keys(errors).length) throw new ServiceError("invalid", "Some preferences need attention.", errors);
    const { fingerprint: _previous, ...current } = this.prefs;
    const changed = JSON.stringify(input) !== JSON.stringify(current);
    this.prefs = { ...structuredClone(input), fingerprint: fingerprint(input) };
    if (changed) {
      for (const item of this.listingsState) if (item.selection) item.selection.stale = true;
    }
    return structuredClone(this.prefs);
  }

  async startSearch(input: PreferencesInput): Promise<SearchRunView> {
    const errors = validatePreferences(input);
    if (Object.keys(errors).length) throw new ServiceError("invalid", "Some search settings need attention.", errors);
    const now = new Date().toISOString();
    this.run = {
      id: `run_pv_${++this.counter}`,
      startedAt: now,
      finishedAt: null,
      polls: 0,
      sources: input.sources,
      results: ["linkedin", "builtin", "indeed", "google"].map((source) => ({
        source,
        state: input.sources.includes(source) ? "QUEUED" : "SKIPPED",
        resultCount: 0,
        message: input.sources.includes(source) ? null : "Not selected for this search.",
        userAction: null,
        sessionName: null,
        startedAt: null,
        finishedAt: input.sources.includes(source) ? null : now,
      })),
    };
    return this.snapshotRun();
  }

  async searchStatus(runId: string): Promise<SearchRunView> {
    if (!this.run || this.run.id !== runId) throw new ServiceError("not_found", "That search isn't in this preview.");
    const run = this.run;
    run.polls += 1;
    const now = new Date().toISOString();
    const active = run.results.filter((item) => item.state !== "SKIPPED");
    const step = Math.floor(run.polls / this.stepPolls);
    active.forEach((item, index) => {
      if (item.finishedAt) return;
      if (step > index + 1) {
        Object.assign(item, FINAL_RESULTS[item.source], { finishedAt: now });
      } else if (step >= index) {
        item.state = "RUNNING";
        item.startedAt ??= now;
      }
    });
    if (active.every((item) => item.finishedAt)) {
      run.finishedAt ??= now;
      const sources = new Set(
        active.filter((item) => item.state === "OK" || item.state === "PARTIAL").map((item) => item.source),
      );
      const found = LISTINGS.filter((item) => item.provenance.some((source) => sources.has(source.source)));
      for (const item of found) {
        if (!this.listingsState.some((existing) => existing.id === item.id))
          this.listingsState.push(structuredClone(item));
      }
    }
    return this.snapshotRun();
  }

  async listings(): Promise<ListingsView> {
    return structuredClone({ listings: this.listingsState, lastRun: this.run ? this.snapshotRun() : null });
  }

  async decide(listingId: string): Promise<ListingView> {
    const item = this.find(listingId);
    const floor = this.prefs.minimumCompensation?.amount ?? 0;
    const pay = item.compensation;
    const now = new Date().toISOString();
    if (item.status === "CLOSED") {
      item.selection = selection({
        id: `sel_pv_${++this.counter}`,
        effectiveChoice: "SKIP",
        modelChoice: null,
        returnedModel: null,
        holds: [{ code: "LISTING_CLOSED", detail: "The source says this job no longer accepts applications." }],
        decidedAt: now,
      });
    } else if (pay?.maximum != null && pay.period === "YEAR" && pay.maximum < floor) {
      item.selection = selection({
        id: `sel_pv_${++this.counter}`,
        effectiveChoice: "SKIP",
        modelChoice: "APPLY",
        probabilities: { APPLY: 0.5, REVIEW: 0.3, SKIP: 0.2 },
        confidence: 0.5,
        holds: [
          {
            code: "HARD_CONSTRAINT",
            detail: `Posted maximum is below your $${floor.toLocaleString("en-US")} minimum.`,
          },
        ],
        decidedAt: now,
      });
    } else if (item.workArrangement === "UNKNOWN" || !pay) {
      item.selection = selection({
        id: `sel_pv_${++this.counter}`,
        effectiveChoice: "REVIEW",
        modelChoice: "REVIEW",
        probabilities: { APPLY: 0.34, REVIEW: 0.58, SKIP: 0.08 },
        confidence: 0.58,
        reasons: ["The title matches a target role."],
        unresolved: [
          ...(item.workArrangement === "UNKNOWN" ? ["Work arrangement isn't stated (onsite, hybrid or remote)."] : []),
          ...(!pay ? ["Pay isn't stated."] : []),
        ],
        decidedAt: now,
      });
    } else {
      item.selection = selection({
        id: `sel_pv_${++this.counter}`,
        effectiveChoice: "APPLY",
        probabilities: { APPLY: 0.77, REVIEW: 0.18, SKIP: 0.05 },
        confidence: 0.77,
        reasons: [
          "The title and responsibilities match a marketing manager target.",
          `${item.workArrangement === "REMOTE" ? "Remote" : "Hybrid in Austin"} matches your location preferences.`,
          `Posted pay (${pay.rawText}) is above your minimum.`,
        ],
        decidedAt: now,
      });
    }
    return structuredClone(item);
  }

  async track(listingId: string): Promise<ListingView> {
    const item = this.find(listingId);
    if (!item.pipelineEntryId) {
      const pipeline = previewPipeline();
      const entry = await pipeline.create({
        lane: "saved",
        listingId: item.id,
        applicationUrl: item.provenance.find((source) => source.applicationUrl)?.applicationUrl ?? null,
        fields: {
          ...EMPTY_FIELDS,
          company: item.company,
          role: item.title,
          stage: "Saved from job search",
          workArrangement: item.workArrangement === "UNKNOWN" ? null : titleCase(item.workArrangement),
          locationCommute: item.location,
          compensationLow: pay(item, "minimum"),
          compensationHigh: pay(item, "maximum"),
          compensationBasis: item.compensation?.rawText ? `As posted: ${item.compensation.rawText}` : null,
          sourceRecruiter: item.provenance.map((source) => source.source).join(", "),
        },
      });
      item.pipelineEntryId = entry.id;
    }
    return structuredClone(item);
  }

  private find(listingId: string) {
    const item = this.listingsState.find((entry) => entry.id === listingId);
    if (!item) throw new ServiceError("not_found", "That listing isn't in this preview.");
    return item;
  }

  private snapshotRun(): SearchRunView {
    const { polls: _polls, sources: _sources, ...run } = this.run!;
    return structuredClone(run);
  }
}

/** Content fingerprint (FNV-1a) so identical preferences keep the same value. */
function fingerprint(input: PreferencesInput) {
  let hash = 0x811c9dc5;
  for (const char of JSON.stringify(input)) {
    hash ^= char.codePointAt(0)!;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `pv-${hash.toString(16).padStart(8, "0")}`;
}

function pay(item: ListingView, bound: "minimum" | "maximum") {
  const value = item.compensation?.[bound];
  return value != null && item.compensation?.currency === "USD" && item.compensation.period === "YEAR" ? value : null;
}

function titleCase(value: string) {
  return value.charAt(0) + value.slice(1).toLowerCase();
}
