import type { ListingView, LocationPriority, LocationTier, SearchPreferencesView } from "./types";

type Targets = Pick<SearchPreferencesView, "onsite" | "remote">;

function cityOf(location: string) {
  return location.split(",")[0].trim().toLowerCase();
}

/**
 * Place a listing relative to the location targets using only what it states.
 * A missing location or arrangement is UNRESOLVED, never assumed to be Austin.
 */
export function locationTier(listing: ListingView, targets: Targets): LocationTier {
  // Older services sent a priority enum here. Never drop those listings from
  // every group: derive the observed match until the corrected DTO arrives.
  const known: string[] = ["ONSITE_HYBRID_TARGET", "REMOTE_ELIGIBLE", "REMOTE_UNCONFIRMED", "OUTSIDE_TARGET", "UNRESOLVED"];
  if (listing.locationTier && known.includes(listing.locationTier)) return listing.locationTier;
  const arrangement = listing.workArrangement;
  if (arrangement === "REMOTE") {
    if (!targets.remote) return "REMOTE_UNCONFIRMED";
    const region = targets.remote.eligibleRegion.trim().toLowerCase();
    const stated = [listing.remoteEligibility, listing.location].filter(Boolean).join(" ").toLowerCase();
    const us = region === "united states" && /\b(united states|usa|u\.s\.|us)\b/.test(stated);
    return stated.includes(region) || us ? "REMOTE_ELIGIBLE" : "REMOTE_UNCONFIRMED";
  }
  if (arrangement === "UNKNOWN" || !listing.location?.trim()) return "UNRESOLVED";
  const place = listing.location.toLowerCase();
  const matches = targets.onsite.some(
    (target) => target.arrangements.includes(arrangement) && place.includes(cityOf(target.location)),
  );
  return matches ? "ONSITE_HYBRID_TARGET" : "OUTSIDE_TARGET";
}

const ORDER: Record<LocationPriority, LocationTier[][]> = {
  STRONGLY_PREFER_ONSITE_HYBRID: [
    ["ONSITE_HYBRID_TARGET"],
    ["REMOTE_ELIGIBLE"],
    ["REMOTE_UNCONFIRMED"],
    ["UNRESOLVED"],
    ["OUTSIDE_TARGET"],
  ],
  BALANCED: [["ONSITE_HYBRID_TARGET", "REMOTE_ELIGIBLE"], ["REMOTE_UNCONFIRMED"], ["UNRESOLVED"], ["OUTSIDE_TARGET"]],
  PREFER_REMOTE: [
    ["REMOTE_ELIGIBLE"],
    ["ONSITE_HYBRID_TARGET"],
    ["REMOTE_UNCONFIRMED"],
    ["UNRESOLVED"],
    ["OUTSIDE_TARGET"],
  ],
};

export interface RankedGroup {
  tiers: LocationTier[];
  listings: ListingView[];
}

/** Stable grouping by tier in the order the location priority asks for. */
export function rankListings(
  listings: ListingView[],
  prefs: Targets & Pick<SearchPreferencesView, "locationPriority">,
): RankedGroup[] {
  return ORDER[prefs.locationPriority]
    .map((tiers) => ({ tiers, listings: listings.filter((item) => tiers.includes(locationTier(item, prefs))) }))
    .filter((group) => group.listings.length > 0);
}

export function tierHeading(tiers: LocationTier[], prefs: Targets): string {
  const city = prefs.onsite[0] ? prefs.onsite[0].location.split(",")[0].trim() : "Your city";
  const region = prefs.remote?.eligibleRegion ?? "your region";
  if (tiers.length > 1) return `${city} onsite or hybrid, and remote in ${region}`;
  switch (tiers[0]) {
    case "ONSITE_HYBRID_TARGET":
      return `${city} onsite or hybrid`;
    case "REMOTE_ELIGIBLE":
      return `Remote, open to ${region}`;
    case "REMOTE_UNCONFIRMED":
      return "Remote, eligibility not stated";
    case "UNRESOLVED":
      return "Location not established";
    case "OUTSIDE_TARGET":
      return `Onsite or hybrid outside ${city}`;
  }
}

export const PRIORITY_LABELS: Record<
  LocationPriority,
  { label: string; detail: (city: string, region: string) => string }
> = {
  STRONGLY_PREFER_ONSITE_HYBRID: {
    label: "Strongly prefer onsite or hybrid",
    detail: (city, region) =>
      `${city} onsite and hybrid roles come first. Remote roles open to ${region} are still included, ranked below them.`,
  },
  BALANCED: {
    label: "Balanced",
    detail: (city, region) => `${city} onsite/hybrid and remote roles open to ${region} rank equally.`,
  },
  PREFER_REMOTE: {
    label: "Prefer remote",
    detail: (city, region) => `Remote roles open to ${region} come first; ${city} onsite and hybrid roles follow.`,
  },
};
