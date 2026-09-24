import { describe, expect, it } from "vitest";
import { applicationLinks, parseHandoff, takeHandoff, writeHandoff, type DeskHandoff } from "../lib/handoff";

const URL_FIXTURE = "http://127.0.0.1:9000/jobs/fictional";
const handoff: DeskHandoff = { applicationUrl: URL_FIXTURE, company: "Fictional Co", role: "Paid Media Manager", from: "pipeline", pipelineEntryId: "pipe_fixture", listingId: "listing_fixture", applicationId: null };

function memoryStorage() {
  const store = new Map<string, string>();
  return {
    store,
    setItem: (key: string, value: string) => void store.set(key, value),
    getItem: (key: string) => store.get(key) ?? null,
    removeItem: (key: string) => void store.delete(key),
  };
}

describe("application source links", () => {
  it("carries the chosen card and listing only while the application link matches", () => {
    expect(applicationLinks(handoff, URL_FIXTURE)).toEqual({ pipelineEntryId: "pipe_fixture", listingId: "listing_fixture" });
    expect(applicationLinks(handoff, "http://127.0.0.1:9000/jobs/different")).toEqual({});
    expect(applicationLinks(null, URL_FIXTURE)).toEqual({});
  });

  it("never links a new application to a review handoff", () => {
    expect(applicationLinks({ ...handoff, applicationId: "app_fixture" }, URL_FIXTURE)).toEqual({});
  });
});

describe("review handoff", () => {
  it("writes and reads the application to open, once", () => {
    const storage = memoryStorage();
    writeHandoff(
      {
        applicationUrl: "https://jobs.example.test/northwind/senior-lifecycle-marketer",
        company: "Northwind Cartography",
        role: "Senior Lifecycle Marketer",
        from: "pipeline",
        pipelineEntryId: "pipe_pv_northwind",
        listingId: null,
        applicationId: "pv_prepared_northwind",
      },
      storage,
    );
    expect(takeHandoff(storage)).toEqual({
      applicationUrl: "https://jobs.example.test/northwind/senior-lifecycle-marketer",
      company: "Northwind Cartography",
      role: "Senior Lifecycle Marketer",
      from: "pipeline",
      pipelineEntryId: "pipe_pv_northwind",
      listingId: null,
      applicationId: "pv_prepared_northwind",
    });
    expect(takeHandoff(storage)).toBeNull();
  });

  it("keeps a prefill handoff without an application id working as before", () => {
    const storage = memoryStorage();
    writeHandoff(
      { applicationUrl: URL_FIXTURE, company: null, role: "Paid Media Manager", from: "jobs", pipelineEntryId: null, listingId: "listing_fixture" },
      storage,
    );
    expect(JSON.parse(storage.store.get("imx.deskHandoff")!)).toMatchObject({ applicationId: null });
    expect(takeHandoff(storage)).toEqual({
      applicationUrl: URL_FIXTURE,
      company: null,
      role: "Paid Media Manager",
      from: "jobs",
      pipelineEntryId: null,
      listingId: "listing_fixture",
      applicationId: null,
    });
  });

  it("parses defensively: a review needs its id, a prefill needs its link", () => {
    expect(parseHandoff({ applicationId: "app_fixture" })).toEqual({
      applicationUrl: null,
      company: null,
      role: null,
      from: "pipeline",
      pipelineEntryId: null,
      listingId: null,
      applicationId: "app_fixture",
    });
    expect(parseHandoff({ applicationUrl: URL_FIXTURE, applicationId: 42 })?.applicationId).toBeNull();
    expect(parseHandoff({ applicationUrl: URL_FIXTURE, applicationId: "  " })?.applicationId).toBeNull();
    expect(parseHandoff({ applicationId: "" })).toBeNull();
    expect(parseHandoff({ applicationId: null, applicationUrl: "" })).toBeNull();
    expect(parseHandoff({ company: "Fictional Co" })).toBeNull();
    expect(parseHandoff(null)).toBeNull();
    expect(parseHandoff(["app_fixture"])).toBeNull();
    expect(parseHandoff("app_fixture")).toBeNull();

    const storage = memoryStorage();
    storage.store.set("imx.deskHandoff", "{not json");
    expect(takeHandoff(storage)).toBeNull();
    expect(storage.store.has("imx.deskHandoff")).toBe(false);
  });
});
