import { describe, expect, it } from "vitest";
import { applicationLinks, type DeskHandoff } from "../lib/handoff";

const handoff: DeskHandoff = { applicationUrl: "http://127.0.0.1:9000/jobs/fictional", company: "Fictional Co", role: "Paid Media Manager", from: "pipeline", pipelineEntryId: "pipe_fixture", listingId: "listing_fixture" };

describe("application source links", () => {
  it("carries the chosen card and listing only while the application link matches", () => {
    expect(applicationLinks(handoff, handoff.applicationUrl)).toEqual({ pipelineEntryId: "pipe_fixture", listingId: "listing_fixture" });
    expect(applicationLinks(handoff, "http://127.0.0.1:9000/jobs/different")).toEqual({});
    expect(applicationLinks(null, handoff.applicationUrl)).toEqual({});
  });
});
