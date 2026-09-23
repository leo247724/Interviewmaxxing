import { describe, expect, it } from "vitest";
import { executionProblem, isLoopbackApplication, type ServiceReadiness } from "@/lib/service/readiness";

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
