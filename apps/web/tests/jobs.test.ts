import { describe, expect, it } from "vitest";
import { PreviewJobsService } from "@/lib/jobs/preview";
import { validatePreferences } from "@/lib/jobs/validation";
import { DEFAULT_PREFERENCES } from "@/lib/jobs/types";
import { previewPipeline } from "@/lib/pipeline/previewStore";

describe("search defaults", () => {
  it("are the user's confirmed targets, with nationwide remote", () => {
    expect(DEFAULT_PREFERENCES.titlePhrases).toEqual([
      "paid media manager", "senior paid media manager", "performance marketing manager",
      "growth marketing manager", "demand generation manager", "digital marketing manager",
      "marketing manager", "marketing director",
    ]);
    expect(DEFAULT_PREFERENCES.roleFocus).toContain("Judge actual responsibilities and ownership");
    expect(DEFAULT_PREFERENCES.onsite).toEqual([{ location: "Austin, TX", arrangements: ["ONSITE", "HYBRID"] }]);
    expect(DEFAULT_PREFERENCES.remote).toEqual({ eligibleRegion: "United States" });
    expect(DEFAULT_PREFERENCES.minimumCompensation).toEqual({ amount: 100000, currency: "USD", period: "YEAR" });
    expect(DEFAULT_PREFERENCES.unknownCompensation).toBe("KEEP");
    expect(validatePreferences(DEFAULT_PREFERENCES)).toEqual({});
  });

  it("rejects searches that can't be run", () => {
    const errors = validatePreferences({
      ...DEFAULT_PREFERENCES,
      titlePhrases: [" "],
      onsite: [],
      remote: null,
      sources: [],
      maxResultsPerSource: 0,
      minimumCompensation: { amount: -1, currency: "USD", period: "YEAR" },
    });
    expect(Object.keys(errors).sort()).toEqual([
      "maxResultsPerSource",
      "minimumCompensation",
      "onsite",
      "sources",
      "titlePhrases",
    ]);
  });
});

describe("PreviewJobsService", () => {
  it("reports each source separately and never hides a sign-in as an empty result", async () => {
    const service = new PreviewJobsService({ seeded: false, stepPolls: 1 });
    const run = await service.startSearch(DEFAULT_PREFERENCES);
    expect(run.results.every((item) => item.state === "QUEUED")).toBe(true);
    let current = run;
    for (let i = 0; i < 20 && !current.finishedAt; i++) current = await service.searchStatus(run.id);
    const byName = Object.fromEntries(current.results.map((item) => [item.source, item]));
    expect(byName.linkedin).toMatchObject({ state: "NEEDS_USER", sessionName: "imx-jobs-linkedin" });
    expect(byName.linkedin.userAction).toMatch(/Sign in/);
    expect(byName.indeed.state).toBe("PARTIAL");
    expect(byName.builtin.state).toBe("OK");
    const { listings } = await service.listings();
    expect(listings.length).toBeGreaterThan(0);
    expect(listings.every((item) => item.provenance.some((source) => source.source !== "linkedin"))).toBe(true);
  });

  it("marks unselected sources as skipped", async () => {
    const service = new PreviewJobsService({ seeded: false });
    const run = await service.startSearch({ ...DEFAULT_PREFERENCES, sources: ["builtin"] });
    expect(run.results.find((item) => item.source === "google")?.state).toBe("SKIPPED");
  });

  it("keeps unknown facts unresolved and applies hard rules after Jev", async () => {
    const service = new PreviewJobsService();
    const unknown = await service.decide("lst_pv_larkloom");
    expect(unknown.selection?.effectiveChoice).toBe("REVIEW");
    expect(unknown.selection?.unresolved.join(" ")).toMatch(/Work arrangement/);
    expect(unknown.workArrangement).toBe("UNKNOWN");
    expect(unknown.compensation).toBeNull();

    const low = (await service.listings()).listings.find((item) => item.id === "lst_pv_bluebonnet")!;
    expect(low.selection?.modelChoice).toBe("APPLY");
    expect(low.selection?.effectiveChoice).toBe("SKIP");
    expect(low.selection?.holds[0].code).toBe("HARD_CONSTRAINT");

    const closed = await service.decide("lst_pv_northwind");
    expect(closed.selection?.effectiveChoice).toBe("SKIP");
    expect(closed.selection?.holds[0].code).toBe("LISTING_CLOSED");

    const failed = (await service.listings()).listings.find((item) => item.id === "lst_pv_copperline")!;
    expect(failed.selection?.effectiveChoice).not.toBe("APPLY");
    expect(failed.selection?.providerError).not.toBeNull();
  });

  it("marks earlier decisions stale when preferences change", async () => {
    const service = new PreviewJobsService();
    const before = await service.preferences();
    const same = await service.savePreferences({ ...DEFAULT_PREFERENCES });
    expect(same.fingerprint).toBe(before.fingerprint);
    expect((await service.listings()).listings.some((item) => item.selection?.stale)).toBe(false);
    const changed = await service.savePreferences({ ...DEFAULT_PREFERENCES, keywords: ["lifecycle"] });
    expect(changed.fingerprint).not.toBe(before.fingerprint);
    const { listings } = await service.listings();
    expect(listings.filter((item) => item.selection).every((item) => item.selection!.stale)).toBe(true);
  });

  it("persists an edited semantic focus and marks earlier decisions stale", async () => {
    const service = new PreviewJobsService();
    const next = { ...DEFAULT_PREFERENCES, roleFocus: "Own paid acquisition and growth as a hands-on leader." };
    await service.savePreferences(next);
    expect((await service.preferences()).roleFocus).toBe(next.roleFocus);
    expect((await service.listings()).listings.filter((item) => item.selection).every((item) => item.selection!.stale)).toBe(true);
  });

  it("tracks a listing as a pipeline card without applying", async () => {
    const service = new PreviewJobsService();
    const tracked = await service.track("lst_pv_tessera");
    expect(tracked.pipelineEntryId).toBeTruthy();
    expect(tracked.applicationId).toBeNull();
    const again = await service.track("lst_pv_tessera");
    expect(again.pipelineEntryId).toBe(tracked.pipelineEntryId);
    const entry = (await previewPipeline().board()).entries.find((item) => item.id === tracked.pipelineEntryId)!;
    expect(entry).toMatchObject({ lane: "saved", origin: "jobs", application: null, listingId: "lst_pv_tessera" });
    expect(entry.applicationUrl).toBe("https://tessera.example.test/careers/pmm/apply");
    expect(entry.fields.compensationLow).toBe(140000);
  });
});
