import { describe, expect, it } from "vitest";
import {
  KNOWN_PRESENTATION_MAJORS,
  executionProblem,
  isLoopbackApplication,
  presentationSupport,
  type ServiceReadiness,
} from "@/lib/service/readiness";

const ready: ServiceReadiness = { status: "ok", executor: "idle", runner: "available", applicationMode: "TEST_ONLY" };

describe("development execution boundary", () => {
  it.each(["http://localhost:1234/apply", "http://127.0.0.1:1234/jobs/1", "http://[::1]/apply"])("accepts a local fixture: %s", (url) => {
    expect(isLoopbackApplication(url)).toBe(true);
    expect(executionProblem(ready, url)).toBeNull();
  });
  it.each(["https://employer.example.test/apply", "http://localhost.example.test/apply", "http://127.0.0.1.example.test/apply", "http://user:pass@localhost/apply", "file:///tmp/apply"])("refuses a nonlocal or credentialed target: %s", (url) => {
    expect(isLoopbackApplication(url)).toBe(false);
    expect(executionProblem(ready, url)).not.toBeNull();
  });
  it("requires verified test mode and an available runner", () => {
    const local = "http://127.0.0.1/apply";
    expect(executionProblem(null, local)).toContain("could not be checked");
    expect(executionProblem({ ...ready, applicationMode: "LIVE" }, local)).toContain("paused");
    expect(executionProblem({ ...ready, runner: "unavailable" }, local)).toContain("not ready");
  });
});

describe("presentation version (WP11 M8)", () => {
  it("reads the versions this dashboard knows, and a missing one as version 1", () => {
    expect(KNOWN_PRESENTATION_MAJORS).toEqual([1, 2]);
    expect(presentationSupport({ ...ready, presentationVersion: "2" })).toEqual({ version: "2", supported: true });
    expect(presentationSupport({ presentationVersion: "2.1" }).supported).toBe(true);
    expect(presentationSupport({ presentationVersion: "1" }).supported).toBe(true);
    expect(presentationSupport(ready)).toEqual({ version: "1", supported: true });
  });

  it("treats an unknown major version or an unreadable value as unknown", () => {
    for (const value of ["3", "3.0", "10", "0", "two", "v2", "", " "]) {
      expect(presentationSupport({ presentationVersion: value }).supported).toBe(false);
    }
    expect(presentationSupport({ presentationVersion: "3" }).version).toBe("3");
    expect(presentationSupport({ presentationVersion: " " }).version).toBe("unreadable");
    expect(presentationSupport({ presentationVersion: 7 as unknown as string })).toEqual({ version: "7", supported: false });
    expect(presentationSupport({ presentationVersion: {} as unknown as string }).supported).toBe(false);
  });

  it("decides nothing before the readiness check has answered", () => {
    expect(presentationSupport(null)).toEqual({ version: null, supported: true });
  });
});
