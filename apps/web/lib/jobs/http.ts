import { requestJson } from "../service/request";
import type { JobsService, ListingView, ListingsView, SearchPreferencesView, SearchRunView } from "./types";

type PreferencesInput = Omit<SearchPreferencesView, "fingerprint">;

/** HTTP binding of {@link JobsService}; routes are documented in apps/web/README.md. */
export class HttpJobsService implements JobsService {
  readonly mode = "live" as const;

  constructor(private readonly baseUrl = "/api/imx") {}

  preferences(): Promise<SearchPreferencesView> {
    return requestJson(this.baseUrl, "GET", "/selection/preferences");
  }

  savePreferences(input: PreferencesInput): Promise<SearchPreferencesView> {
    return requestJson(this.baseUrl, "POST", "/selection/preferences", input);
  }

  startSearch(input: PreferencesInput): Promise<SearchRunView> {
    return requestJson(this.baseUrl, "POST", "/jobs/search", input);
  }

  searchStatus(runId: string): Promise<SearchRunView> {
    return requestJson(this.baseUrl, "GET", `/jobs/search/${encodeURIComponent(runId)}`);
  }

  listings(): Promise<ListingsView> {
    return requestJson(this.baseUrl, "GET", "/jobs");
  }

  decide(listingId: string): Promise<ListingView> {
    return requestJson(this.baseUrl, "POST", `/selection/jobs/${encodeURIComponent(listingId)}`, {});
  }

  track(listingId: string): Promise<ListingView> {
    return requestJson(this.baseUrl, "POST", `/jobs/${encodeURIComponent(listingId)}/track`, {});
  }
}
