import { LOCATION_PRIORITIES, type SearchPreferencesView } from "./types";

type PreferencesInput = Omit<SearchPreferencesView, "fingerprint">;

/** Check search preferences before sending them. Missing pay floor means no floor. */
export function validatePreferences(input: PreferencesInput): Record<string, string> {
  const errors: Record<string, string> = {};
  if (input.titlePhrases.filter((phrase) => phrase.trim()).length === 0) {
    errors.titlePhrases = "Add at least one job title to search for.";
  }
  if (input.onsite.length === 0 && !input.remote) {
    errors.onsite = "Choose an onsite location, remote roles, or both.";
  }
  for (const target of input.onsite) {
    if (!target.location.trim()) errors.onsite = "Enter the city for onsite and hybrid roles.";
    else if (target.arrangements.length === 0) errors.onsite = "Choose onsite, hybrid or both for that city.";
  }
  if (input.remote && !input.remote.eligibleRegion.trim()) {
    errors.remote = "Enter where remote roles must allow you to work, e.g. United States.";
  }
  if (
    input.minimumCompensation &&
    (!Number.isFinite(input.minimumCompensation.amount) || input.minimumCompensation.amount <= 0)
  ) {
    errors.minimumCompensation = "Enter a minimum above zero, or leave it blank for no minimum.";
  }
  if (!LOCATION_PRIORITIES.includes(input.locationPriority)) {
    errors.locationPriority = "Choose how to rank onsite, hybrid and remote roles.";
  }
  if (input.sources.length === 0) errors.sources = "Choose at least one source.";
  if (
    !Number.isInteger(input.maxResultsPerSource) ||
    input.maxResultsPerSource < 1 ||
    input.maxResultsPerSource > 500
  ) {
    errors.maxResultsPerSource = "Choose between 1 and 500 results per source.";
  }
  return errors;
}
