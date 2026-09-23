import { describe, expect, it } from "vitest";
import { ServiceError } from "@/lib/service/errors";
import { PreviewApplicationService, type PreviewScenarioId } from "@/lib/service/preview";
import type { ApplicationView } from "@/lib/service/types";

const START = {
  applicationUrl: "https://jobs.example.test/northwind/apply",
  profile: {
    firstName: "Robin",
    lastName: "Vale",
    email: "robin.vale@example.test",
    phone: "",
    location: "",
    linkedinUrl: "",
    websiteUrl: "",
  },
  resumeId: "res_pv_lifecycle",
};

function service(scenario: PreviewScenarioId) {
  return new PreviewApplicationService({ scenario, stepDelayMs: 0 });
}

async function runUntilIdle(svc: PreviewApplicationService, view: ApplicationView) {
  const seen = [view.state];
  for (
    let i = 0;
    i < 50 && ["REQUESTED", "INSPECTING", "PACKET_READY", "FILLING", "SUBMITTING"].includes(view.state);
    i++
  ) {
    try {
      view = await svc.status(view.id);
    } catch (error) {
      if (!(error instanceof ServiceError && error.code === "unavailable")) throw error;
      continue;
    }
    if (seen.at(-1) !== view.state) seen.push(view.state);
  }
  return { view, seen };
}

describe("PreviewApplicationService", () => {
  it("persists SUBMITTING before reporting a confirmed receipt", async () => {
    const svc = service("straight");
    const { view, seen } = await runUntilIdle(svc, await svc.start(START));
    expect(seen).toEqual(["REQUESTED", "INSPECTING", "PACKET_READY", "FILLING", "SUBMITTING", "SUBMITTED"]);
    expect(view.receipt?.confirmationReference).toBe("NWC-24-0922-7731");
    expect(view.receipt?.evidence.every((item) => item.source === "site")).toBe(true);
    expect(view.events.at(-1)?.type).toBe("application.submitted");
  });

  it("rejects start with invalid input instead of guessing", async () => {
    const svc = service("straight");
    await expect(svc.start({ ...START, applicationUrl: "nope", resumeId: "missing" })).rejects.toMatchObject({
      code: "invalid",
      fieldErrors: { applicationUrl: expect.any(String), resumeId: expect.any(String) },
    });
  });

  it("pauses for questions, keeps consent unchecked, and resumes only when complete", async () => {
    const svc = service("questions");
    let { view } = await runUntilIdle(svc, await svc.start(START));
    expect(view.state).toBe("NEEDS_INPUT");
    const needs = view.needs!;
    if (needs.kind !== "questions") throw new Error("expected questions");
    expect(needs.attestations.every((item) => !item.accepted)).toBe(true);
    expect(needs.questions.find((q) => q.id === "q_gender")?.options?.map((o) => o.value)).toContain("decline");

    await expect(svc.resume(view.id)).rejects.toMatchObject({ code: "invalid" });

    view = await svc.answer(view.id, { answers: { q_salary: "lots" }, attestations: {} });
    expect(view.needs?.kind === "questions" && view.needs.errors.q_salary).toMatch(/whole number/);

    view = await svc.answer(view.id, {
      answers: {
        q_work_auth: true,
        q_sponsorship: "no",
        q_salary: "145000",
        q_source: "linkedin",
        q_why: "Maps.",
        q_tools: [],
        q_gender: "decline",
      },
      attestations: { a_truthful: true },
    });
    expect(view.needs?.kind === "questions" && view.needs.savedAt).toBeTruthy();
    view = await svc.resume(view.id);
    ({ view } = await runUntilIdle(svc, view));
    expect(view.state).toBe("SUBMITTED");
  });

  it("never retries an unconfirmed submission without reconciliation", async () => {
    const svc = service("uncertain");
    let { view } = await runUntilIdle(svc, await svc.start(START));
    expect(view.state).toBe("SUBMISSION_UNKNOWN");
    await expect(svc.resume(view.id)).rejects.toMatchObject({ code: "conflict" });

    view = await svc.reconcile(view.id, { kind: "recheck" });
    expect(view.state).toBe("SUBMISSION_UNKNOWN");
    expect(view.uncertain?.lastCheckResult).toMatch(/No confirmation/);

    view = await svc.reconcile(view.id, { kind: "user_confirmed_not_received" });
    expect(view.state).toBe("FAILED_RETRYABLE");
    view = await svc.resume(view.id);
    ({ view } = await runUntilIdle(svc, view));
    expect(view.state).toBe("SUBMITTED");
  });

  it("records a user-reported confirmation as user evidence", async () => {
    const svc = service("uncertain");
    const { view } = await runUntilIdle(svc, await svc.start(START));
    const settled = await svc.reconcile(view.id, {
      kind: "user_found_confirmation",
      foundIn: "email",
      reference: "JV-1",
      note: null,
    });
    expect(settled.state).toBe("SUBMITTED");
    expect(settled.receipt?.confirmationReference).toBe("JV-1");
    expect(settled.receipt?.evidence.some((item) => item.source === "user" && item.kind === "user_report")).toBe(true);
    expect(settled.receipt).toMatchObject({ confirmationMethod: "USER_CONFIRMED", confirmationAuthority: "user" });
  });

  it("reports a prior submission without sending", async () => {
    const svc = service("duplicate");
    const { view, seen } = await runUntilIdle(svc, await svc.start(START));
    expect(view.state).toBe("DUPLICATE");
    expect(seen).not.toContain("SUBMITTING");
    expect(view.prior?.confirmationReference).toBe("NWC-24-0903-1188");
  });

  it("reports unavailability rather than simulating success", async () => {
    const svc = service("unavailable");
    await expect(svc.getCandidate()).rejects.toMatchObject({ code: "unavailable" });
    await expect(svc.start(START)).rejects.toMatchObject({ code: "unavailable" });
  });
});
