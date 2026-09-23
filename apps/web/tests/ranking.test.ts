import { describe, expect, it } from "vitest";
import { locationTier, rankListings } from "@/lib/jobs/ranking";
import { DEFAULT_PREFERENCES, type ListingView } from "@/lib/jobs/types";

function listing(id: string, partial: Partial<ListingView>): ListingView {
  return {
    id,
    title: "Marketing Manager",
    company: "Fictional Co",
    location: null,
    workArrangement: "UNKNOWN",
    remoteEligibility: null,
    compensation: null,
    description: null,
    descriptionCompleteness: "NONE",
    status: "OPEN",
    postedText: null,
    observedAt: "2026-09-22T00:00:00Z",
    provenance: [
      {
        source: "builtin",
        sourceUrl: `https://x.example.test/${id}`,
        applicationUrl: null,
        observedAt: "2026-09-22T00:00:00Z",
      },
    ],
    selection: null,
    pipelineEntryId: null,
    applicationId: null,
    ...partial,
  };
}

const remoteUs = listing("remote-us", {
  workArrangement: "REMOTE",
  location: "Remote",
  remoteEligibility: "United States",
});
const remoteVague = listing("remote-vague", { workArrangement: "REMOTE", location: "Remote" });
const austinHybrid = listing("austin-hybrid", { workArrangement: "HYBRID", location: "Austin, TX" });
const austinOnsite = listing("austin-onsite", { workArrangement: "ONSITE", location: "Austin, Texas" });
const dallas = listing("dallas", { workArrangement: "ONSITE", location: "Dallas, TX" });
const unknown = listing("unknown", { workArrangement: "UNKNOWN", location: "United States" });
const noLocation = listing("no-location", { workArrangement: "HYBRID", location: null });
const all = [remoteUs, remoteVague, dallas, unknown, austinHybrid, noLocation, austinOnsite];

describe("location tiers", () => {
  it("places listings using only what they state", () => {
    const tiers = Object.fromEntries(all.map((item) => [item.id, locationTier(item, DEFAULT_PREFERENCES)]));
    expect(tiers).toEqual({
      "remote-us": "REMOTE_ELIGIBLE",
      "remote-vague": "REMOTE_UNCONFIRMED",
      dallas: "OUTSIDE_TARGET",
      unknown: "UNRESOLVED",
      "austin-hybrid": "ONSITE_HYBRID_TARGET",
      "no-location": "UNRESOLVED",
      "austin-onsite": "ONSITE_HYBRID_TARGET",
    });
  });

  it("respects a tier the service computed", () => {
    expect(locationTier({ ...remoteVague, locationTier: "REMOTE_ELIGIBLE" }, DEFAULT_PREFERENCES)).toBe(
      "REMOTE_ELIGIBLE",
    );
  });

  it("only counts arrangements the target asks for", () => {
    const onsiteOnly = {
      ...DEFAULT_PREFERENCES,
      onsite: [{ location: "Austin, TX", arrangements: ["ONSITE" as const] }],
    };
    expect(locationTier(austinHybrid, onsiteOnly)).toBe("OUTSIDE_TARGET");
  });
});

describe("ranking by location priority", () => {
  const ids = (groups: ReturnType<typeof rankListings>) => groups.map((group) => group.listings.map((item) => item.id));

  it("defaults to Austin onsite/hybrid well above nationwide remote, keeping remote and unknowns", () => {
    expect(DEFAULT_PREFERENCES.locationPriority).toBe("STRONGLY_PREFER_ONSITE_HYBRID");
    expect(DEFAULT_PREFERENCES.minimumCompensation).toEqual({ amount: 100000, currency: "USD", period: "YEAR" });
    expect(ids(rankListings(all, DEFAULT_PREFERENCES))).toEqual([
      ["austin-hybrid", "austin-onsite"],
      ["remote-us"],
      ["remote-vague"],
      ["unknown", "no-location"],
      ["dallas"],
    ]);
  });

  it("balanced ranks target-city and eligible remote together", () => {
    const groups = rankListings(all, { ...DEFAULT_PREFERENCES, locationPriority: "BALANCED" });
    expect(groups[0].tiers).toEqual(["ONSITE_HYBRID_TARGET", "REMOTE_ELIGIBLE"]);
    expect(groups[0].listings.map((item) => item.id)).toEqual(["remote-us", "austin-hybrid", "austin-onsite"]);
  });

  it("prefer remote puts eligible remote first without dropping anything", () => {
    const groups = rankListings(all, { ...DEFAULT_PREFERENCES, locationPriority: "PREFER_REMOTE" });
    expect(groups[0].listings.map((item) => item.id)).toEqual(["remote-us"]);
    expect(groups.flatMap((group) => group.listings)).toHaveLength(all.length);
  });

  it("never loses a listing under any priority", () => {
    for (const locationPriority of ["STRONGLY_PREFER_ONSITE_HYBRID", "BALANCED", "PREFER_REMOTE"] as const) {
      const ranked = rankListings(all, { ...DEFAULT_PREFERENCES, locationPriority }).flatMap((group) => group.listings);
      expect(new Set(ranked.map((item) => item.id))).toEqual(new Set(all.map((item) => item.id)));
    }
  });
});
