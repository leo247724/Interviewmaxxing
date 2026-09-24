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
    expect(view.state).toBe("SUBMISSION_UNKNOWN");
    expect(view.uncertain?.lastCheckResult).toContain("You reported");
    await expect(svc.resume(view.id)).rejects.toMatchObject({ code: "conflict" });
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

describe("PreviewApplicationService preparation", () => {
  it("prepares both pages, stops at the final review step and never submits", async () => {
    const svc = service("prepared");
    const { view, seen } = await runUntilIdle(svc, await svc.start(START));
    expect(seen).toEqual(["REQUESTED", "INSPECTING", "PACKET_READY", "FILLING", "NEEDS_INPUT"]);
    expect(view.needs).toBeNull();
    expect(view.receipt).toBeNull();
    expect(view.progress).toEqual({ page: 2, pageCount: 2 });
    expect(view.preparation).toMatchObject({
      ready: true,
      formStep: 1,
      formUrl: "https://jobs.example.test/northwind/senior-lifecycle-marketer/apply/review",
      captchaPending: false,
      submitted: false,
      evidence: [{ kind: "screenshot", href: "/preview-fixtures/prepared-review.svg", source: "site" }],
    });
    expect(view.events.slice(-2).map(({ type, message, tone }) => ({ type, message, tone }))).toEqual([
      { type: "preparation.ready", message: "Ready for final review. Nothing was submitted.", tone: "attention" },
      { type: "application.needs_input", message: "Paused at the final review step for you to check.", tone: "info" },
    ]);
    expect(view.events.some((event) => event.type.startsWith("application.submit"))).toBe(false);

    const review = view.review ?? [];
    expect(review.length).toBeGreaterThanOrEqual(8);
    expect(new Set(review.map((item) => item.page))).toEqual(new Set([1, 2]));
    expect(new Set(review.map((item) => item.control))).toEqual(
      new Set(["text", "long_text", "single_select", "multi_select", "boolean", "file"]),
    );
    expect(new Set(review.map((item) => item.source))).toEqual(
      new Set(["identity", "saved_answer", "fact", "user", "generated", "resume"]),
    );
    expect(review.filter((item) => !item.wordingRecorded)).toHaveLength(1);
    expect(review.some((item) => item.source === "generated" && item.confidence < 0.9)).toBe(true);
    expect(review.map((item) => item.page)).toEqual([...review.map((item) => item.page)].sort());
  });

  it("prepares again on resume, still without submitting", async () => {
    const svc = service("prepared");
    let { view } = await runUntilIdle(svc, await svc.start(START));
    view = await svc.resume(view.id);
    expect(view.state).toBe("INSPECTING");
    expect(view.preparation).toBeNull();
    const again = await runUntilIdle(svc, view);
    expect(again.seen).not.toContain("SUBMITTING");
    expect(again.view.state).toBe("NEEDS_INPUT");
    expect(again.view.preparation?.submitted).toBe(false);
    expect(again.view.events.map((event) => event.type)).toContain("preparation.restarted");
    expect(again.view.events.filter((event) => event.type === "preparation.ready")).toHaveLength(2);
  });

  it("asks a location lookup, then prepares the form with the typed value", async () => {
    const svc = service("lookup");
    let { view, seen } = await runUntilIdle(svc, await svc.start(START));
    expect(view.state).toBe("NEEDS_INPUT");
    expect(view.preparation).toBeNull();
    const needs = view.needs;
    if (needs?.kind !== "questions") throw new Error("expected questions");
    expect(needs.questions).toHaveLength(1);
    const [question] = needs.questions;
    expect(question).toMatchObject({ lookup: true, control: "single_select", required: true });
    expect(question.reason).toBeTruthy();
    expect(question.options!.length).toBeGreaterThan(1);
    expect(question.options!.every((option) => option.value === option.label)).toBe(true);

    await expect(svc.resume(view.id)).rejects.toMatchObject({ code: "invalid" });
    view = await svc.answer(view.id, { answers: { [question.id]: "Beaverton, OR, USA" }, attestations: {} });
    expect(view.needs?.kind === "questions" && view.needs.errors).toEqual({});
    view = await svc.resume(view.id);
    const after = await runUntilIdle(svc, view);
    seen = [...seen, ...after.seen];
    expect(seen).not.toContain("SUBMITTING");
    expect(after.view.state).toBe("NEEDS_INPUT");
    expect(after.view.needs).toBeNull();
    expect(after.view.preparation?.captchaPending).toBe(true);
    expect(after.view.review).toContainEqual(
      expect.objectContaining({
        question: question.label,
        value: "Beaverton, OR, USA",
        source: "user",
        wordingRecorded: true,
        confidence: 1,
      }),
    );
    expect(after.view.review?.filter((item) => item.question === question.label)).toHaveLength(1);
  });

  it("accepts one of the lookup's suggestions too", async () => {
    const svc = service("lookup");
    let { view } = await runUntilIdle(svc, await svc.start(START));
    if (view.needs?.kind !== "questions") throw new Error("expected questions");
    const [question] = view.needs.questions;
    view = await svc.answer(view.id, { answers: { [question.id]: question.options![1].value }, attestations: {} });
    ({ view } = await runUntilIdle(svc, await svc.resume(view.id)));
    expect(view.preparation?.ready).toBe(true);
    expect(view.review).toContainEqual(expect.objectContaining({ value: question.options![1].label, source: "user" }));
  });

  it("starts every session with the seeded prepared application, listed with its pipeline card", async () => {
    const svc = service("straight");
    const seeded = await svc.status("pv_prepared_northwind");
    expect(seeded).toMatchObject({
      state: "NEEDS_INPUT",
      needs: null,
      applicationUrl: "https://jobs.example.test/northwind/senior-lifecycle-marketer",
      job: { title: "Senior Lifecycle Marketer", company: "Northwind Cartography", ats: "Greenhouse" },
      preparation: {
        captchaPending: true,
        evidence: [{ kind: "screenshot", href: "/preview-fixtures/prepared-review.svg" }],
      },
    });
    expect(seeded.review?.length).toBeGreaterThanOrEqual(8);

    const before = await svc.list();
    expect(before.applications).toEqual([
      expect.objectContaining({
        id: "pv_prepared_northwind",
        state: "NEEDS_INPUT",
        pipelineEntryIds: ["pipe_pv_northwind"],
        preparation: seeded.preparation,
      }),
    ]);

    const started = await svc.start(START);
    expect(started.preparation).toBeNull();
    expect(started.review).toEqual([]);
    const after = await svc.list();
    expect(after.applications.map((item) => item.id)).toEqual([started.id, "pv_prepared_northwind"]);
    expect(after.applications[0]).toMatchObject({ pipelineEntryIds: [], preparation: null, state: "REQUESTED" });
  });

  it("keeps the seeded application out of the scenario flow", async () => {
    const svc = service("straight");
    const { view, seen } = await runUntilIdle(svc, await svc.start(START));
    expect(seen.at(-1)).toBe("SUBMITTED");
    expect(view.preparation).toBeNull();
    expect((await svc.status("pv_prepared_northwind")).state).toBe("NEEDS_INPUT");
    await expect(service("unavailable").list()).rejects.toMatchObject({ code: "unavailable" });
  });
});
