import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ReviewActions, SubmitConfirm } from "@/components/review/ReviewActions";
import { ProvenanceBadge, ReviewAnswers } from "@/components/review/ReviewAnswers";
import { ReviewNotices } from "@/components/review/ReviewPage";
import { QueueTable, reviewHref } from "@/components/review/ReviewQueue";
import { executionBanner } from "@/components/shell/AppShell";
import { HttpReviewService } from "@/lib/review/http";
import {
  PROVENANCE_KINDS,
  allowedReuse,
  approveState,
  badgeKind,
  citationSummary,
  copyableText,
  costShort,
  costText,
  editProblem,
  editRequest,
  filterQueue,
  formatUsd,
  groupByPage,
  initialEditValue,
  listedQueue,
  nextInQueue,
  outcomeLine,
  provenanceLabel,
  queueCounts,
  reviewExecutionProblem,
  rowNeedsCheck,
  sortQueue,
  submitRequest,
  submitState,
  unchanged,
} from "@/lib/review/logic";
import { PREVIEW_APPROVED_ID, PREVIEW_SIGN_IN_ID, PREVIEW_SUBMIT_PROBLEM, PreviewReviewService } from "@/lib/review/preview";
import type {
  ApplicationReviewView,
  ProvenanceKind,
  ReviewEditView,
  ReviewQueueItemView,
  ReviewRowView,
} from "@/lib/review/types";
import { ServiceError } from "@/lib/service/errors";
import { PREPARED_PREVIEW_ID } from "@/lib/service/preview";
import type { ServiceReadiness } from "@/lib/service/readiness";

// Fictional records only: example.test addresses, made-up employers and fact ids.

function text(html: string) {
  return html
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/&rsquo;/g, "’")
    .replace(/\s+/g, " ");
}

const TEST_ONLY: ServiceReadiness = {
  status: "ok",
  executor: "idle",
  runner: "available",
  applicationMode: "TEST_ONLY",
  presentationVersion: "2",
  submission: "enabled",
  browser: "visible",
};
const LIVE: ServiceReadiness = { ...TEST_ONLY, applicationMode: "LIVE" };
const LOOPBACK_URL = "http://127.0.0.1:9/fictional-co/4012/apply";
const EMPLOYER_URL = "https://jobs.example.test/fictional-co/4012/apply";

function item(id: string, stoppedAt: string, extra: Partial<ReviewQueueItemView> = {}): ReviewQueueItemView {
  return {
    id,
    state: "NEEDS_INPUT",
    stage: "prepared",
    applicationUrl: `https://jobs.example.test/${id}/apply`,
    job: { title: "Paid Media Manager", company: `Fictional ${id} Co`, ats: "Greenhouse" },
    preparedAt: stoppedAt,
    stoppedAt,
    captchaPending: false,
    providerCost: null,
    hold: { kind: "ready", summary: "Ready to review and approve." },
    approved: false,
    ...extra,
  };
}

async function northwind(): Promise<ApplicationReviewView> {
  return new PreviewReviewService().review(PREPARED_PREVIEW_ID);
}

/** A review of the given application URL, approved and allowed to submit by the service. */
async function submittable(applicationUrl = LOOPBACK_URL): Promise<ApplicationReviewView> {
  const review = await northwind();
  return {
    ...review,
    application: { ...review.application, applicationUrl },
    approval: { packetId: "pkt_fictional_1", approvedAt: "2026-09-23T16:00:00Z", approver: "dashboard:fictional", pages: 2 },
    submit: { allowed: true, problems: [], enabled: true, opensBrowser: true, command: "IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit x --yes" },
  };
}

const TEXT_EDIT: ReviewEditView = {
  control: "text",
  options: null,
  lookup: false,
  attestation: false,
  required: true,
  value: "145000",
  reuse: ["application", "job", "global"],
  note: null,
};

describe("provenance badges", () => {
  it("gives every provenance kind its own badge, with the service's label", () => {
    for (const kind of PROVENANCE_KINDS) {
      const html = renderToStaticMarkup(
        <ProvenanceBadge provenance={{ kind, label: `Label for ${kind}`, detail: null }} />,
      );
      expect(html).toContain(`provenance-badge--${kind}`);
      expect(text(html)).toContain(`Label for ${kind}`);
    }
  });

  it("names each kind plainly when the service sends no label, and an unknown kind neutrally", () => {
    const labels = Object.fromEntries(
      PROVENANCE_KINDS.map((kind) => [kind, provenanceLabel({ kind, label: "", detail: null })]),
    );
    expect(labels).toEqual({
      identity: "Your details",
      resume: "Your resume",
      saved_answer: "Saved answer",
      saved_policy: "Saved policy",
      derived: "Derived",
      fact_screener: "Fact-grounded screener",
      narrative: "RAG narrative",
      user: "Your answer",
      blank: "Left blank",
    });
    const future = { kind: "from_the_future" as ProvenanceKind, label: "", detail: null };
    expect(badgeKind(future)).toBe("other");
    expect(provenanceLabel(future)).toBe("Filled in by the desk");
    expect(renderToStaticMarkup(<ProvenanceBadge provenance={future} />)).toContain("provenance-badge--other");
  });

  it("renders every row of the prepared form in form order, blanks and citations included", async () => {
    const review = await northwind();
    const html = renderToStaticMarkup(<ReviewAnswers rows={review.answers} />);
    const visible = text(html);
    for (const kind of PROVENANCE_KINDS) expect(html).toContain(`provenance-badge--${kind}`);
    expect(visible).toContain("Left blank");
    expect(visible).toContain("Page 1");
    expect(visible).toContain("Page 2");
    expect(visible.indexOf("First name")).toBeLessThan(visible.indexOf("Why are you a good fit"));
    // The narrative is shown in full, with a copy button and its cited fact and passage ids.
    const narrative = review.answers.find((row) => row.provenance.kind === "narrative")!;
    expect(visible).toContain("retention cohorts rather than open rates");
    expect(visible).toContain("a product people carry into the field");
    expect(visible).toContain("Copy text");
    expect(visible).toContain("Cites 3 facts, 2 story passages and 1 job note");
    for (const id of [...narrative.citations!.facts, ...narrative.citations!.passages, ...narrative.citations!.jobEvidence]) {
      expect(html).toContain(id);
    }
    // Without a save handler the list only reads: no Edit buttons.
    expect(visible).not.toContain("Edit answer");
    expect(visible).toContain("The form's options for this question weren't recorded");
  });

  it("offers Edit only where the service allows it", async () => {
    const review = await northwind();
    const html = renderToStaticMarkup(<ReviewAnswers rows={review.answers} onSave={async () => null} />);
    const editable = review.answers.filter((row) => row.edit && row.questionId).length;
    expect((html.match(/>Edit answer</g) ?? []).length).toBe(editable);
    expect(html).toContain('aria-label="Edit answer: What are your base salary expectations (USD)?"');
    expect(editable).toBeGreaterThan(3);
  });

  it("flags low-confidence answers and copies only long text", () => {
    const row = (extra: Partial<ReviewRowView>): ReviewRowView => ({
      questionId: null,
      question: "Fictional question",
      wordingRecorded: true,
      page: 1,
      control: "text",
      value: "Fictional",
      required: true,
      provenance: { kind: "saved_answer", label: "Saved answer", detail: null },
      citations: null,
      confidence: 1,
      edit: null,
      noEditReason: null,
      ...extra,
    });
    expect(rowNeedsCheck(row({ confidence: 0.86 }))).toBe(true);
    expect(rowNeedsCheck(row({ confidence: 0.9 }))).toBe(false);
    expect(rowNeedsCheck(row({ confidence: null }))).toBe(false);
    expect(copyableText(row({}))).toBeNull();
    expect(copyableText(row({ control: "long_text", value: "Two\n\nparagraphs" }))).toBe("Two\n\nparagraphs");
    expect(copyableText(row({ provenance: { kind: "narrative", label: "", detail: null } }))).toBe("Fictional");
    expect(citationSummary({ facts: ["f1"], passages: [], jobEvidence: [] })).toBe("Cites 1 fact");
    expect(citationSummary({ facts: [], passages: [], jobEvidence: [] })).toBeNull();
    expect(groupByPage([row({ page: 2 }), row({ page: 1 }), row({ page: 2 })]).map((group) => group.page)).toEqual([1, 2]);
  });
});

describe("the prepared queue", () => {
  const items = [
    item("older", "2026-09-23T10:00:00Z"),
    item("approved", "2026-09-23T12:00:00Z", { approved: true, hold: { kind: "approved", summary: "Approved." } }),
    item("browser", "2026-09-23T11:00:00Z", {
      stage: "browser_action",
      preparedAt: null,
      hold: { kind: "sign_in", summary: "Sign in on the site." },
    }),
    item("newest", "2026-09-23T13:00:00Z", { captchaPending: true }),
  ];

  it("lists newest first and filters by what each application waits for", () => {
    expect(sortQueue(items).map((entry) => entry.id)).toEqual(["newest", "approved", "browser", "older"]);
    expect(queueCounts(items)).toEqual({ all: 4, review: 2, approved: 1, browser: 1 });
    expect(filterQueue(items, "review").map((entry) => entry.id)).toEqual(["newest", "older"]);
    expect(filterQueue(items, "approved").map((entry) => entry.id)).toEqual(["approved"]);
    expect(filterQueue(items, "browser").map((entry) => entry.id)).toEqual(["browser"]);
  });

  it("moves to the next application in queue order", () => {
    expect(nextInQueue(items, "newest")?.id).toBe("approved");
    expect(nextInQueue(items, "older")).toBeNull();
    expect(nextInQueue(items, "not-queued")?.id).toBe("newest");
  });

  it("ignores a malformed queue response instead of inventing entries", () => {
    expect(listedQueue(null)).toEqual([]);
    expect(listedQueue({ applications: "nope" })).toEqual([]);
    expect(listedQueue({ applications: [{ id: 3 }, item("ok", "2026-09-23T10:00:00Z")] }).map((entry) => entry.id)).toEqual(["ok"]);
  });

  it("shows provider cost plainly", () => {
    expect(formatUsd(0.4213)).toBe("$0.42");
    expect(formatUsd(0.004)).toBe("under $0.01");
    expect(costText({ knownUsd: 0.4213, calls: 37, unknownCostCalls: 2 })).toBe(
      "$0.42 · 37 calls, 2 without a reported cost",
    );
    expect(costText({ knownUsd: 0.1, calls: 1, unknownCostCalls: 0 })).toBe("$0.10 · 1 call");
    expect(costText(null)).toBe("No AI calls recorded");
    expect(costShort({ knownUsd: 0.4213, calls: 37, unknownCostCalls: 2 })).toBe("$0.42+");
    expect(costShort(null)).toBe("—");
  });

  it("renders a row per application with its hold, marks and review link", () => {
    const html = renderToStaticMarkup(<QueueTable mode="live" items={sortQueue(items)} />);
    const visible = text(html);
    expect((html.match(/<tr class="review-queue__row/g) ?? []).length).toBe(4);
    expect(visible).toContain("Sign in on the site.");
    expect(visible).toContain("CAPTCHA to solve");
    expect(visible).toContain("In the browser");
    expect(html).toContain('href="/review/newest"');
    expect(reviewHref("preview", "pv x")).toBe("/preview/review/pv%20x");
    expect(visible.indexOf("Fictional newest Co")).toBeLessThan(visible.indexOf("Fictional older Co"));
  });
});

describe("the execution gate", () => {
  it("follows the service's mode: TEST_ONLY needs a local test site, LIVE doesn't", () => {
    expect(reviewExecutionProblem(null, LOOPBACK_URL)).toMatch(/readiness couldn't be checked/);
    expect(reviewExecutionProblem({ ...TEST_ONLY, runner: "unavailable" }, LOOPBACK_URL)).toMatch(/browser isn't ready/);
    expect(reviewExecutionProblem(TEST_ONLY, LOOPBACK_URL)).toBeNull();
    expect(reviewExecutionProblem(TEST_ONLY, EMPLOYER_URL)).toMatch(/TEST_ONLY mode/);
    expect(reviewExecutionProblem(LIVE, EMPLOYER_URL)).toBeNull();
    expect(
      reviewExecutionProblem({ ...TEST_ONLY, applicationMode: "SOMETHING" as ServiceReadiness["applicationMode"] }, LOOPBACK_URL),
    ).toMatch(/doesn't know/);
  });

  it("says what the banner means in each mode", () => {
    expect(executionBanner({ ...TEST_ONLY, submission: undefined })).toMatchObject({ label: "Test mode", submission: null });
    expect(executionBanner(TEST_ONLY).submission).toBe("Submission on");
    const live = executionBanner({ ...LIVE, submission: "disabled" });
    expect(live).toMatchObject({ tone: "live", label: "Live mode", submission: "Submission off" });
    expect(live.text).toContain("real employer sites");
    expect(live.text).toContain("approve and confirm");
    expect(executionBanner({ ...LIVE, applicationMode: "OTHER" as ServiceReadiness["applicationMode"] }).tone).toBe("blocked");
  });
});

describe("approve and submit guards", () => {
  it("approves only a current, unchanged preparation without open questions", async () => {
    const review = await northwind();
    expect(approveState(review)).toEqual({ available: true, reason: null });
    expect(approveState({ ...review, changedSincePreparation: true }).reason).toMatch(/Prepare it again/);
    expect(approveState({ ...review, preparedPacketId: null }).reason).toMatch(/final review step/);
    expect(approveState({ ...review, application: { ...review.application, state: "FILLING" } }).reason).toMatch(/Wait/);
    const approved = { ...review, approval: { packetId: "pkt", approvedAt: "2026-09-23T16:00:00Z", approver: "x", pages: 2 } };
    expect(approveState(approved)).toEqual({ available: false, reason: null });
    const open = {
      ...review,
      application: {
        ...review.application,
        needs: {
          kind: "questions" as const,
          questions: [
            { id: "q", label: "Fictional?", help: null, control: "text" as const, required: true, options: null, value: null, maxLength: null, reason: null },
          ],
          attestations: [],
          savedAt: null,
          errors: {},
        },
      },
    };
    expect(approveState(open).reason).toMatch(/remaining questions/);
  });

  it("keeps Submit off with the service's reasons, and adds the dashboard's own checks", async () => {
    const review = await northwind();
    expect(submitState(review, null, "preview")).toEqual({ enabled: false, problems: [PREVIEW_SUBMIT_PROBLEM] });

    const ready = await submittable();
    expect(submitState(ready, TEST_ONLY, "live")).toEqual({ enabled: true, problems: [] });
    // A TEST_ONLY service never acts on an employer's site; a LIVE one may.
    const employer = await submittable(EMPLOYER_URL);
    const blocked = submitState(employer, TEST_ONLY, "live");
    expect(blocked.enabled).toBe(false);
    expect(blocked.problems.join(" ")).toMatch(/TEST_ONLY mode/);
    expect(submitState(employer, LIVE, "live")).toEqual({ enabled: true, problems: [] });
    // Readiness that says submission is off wins over a stale page.
    expect(submitState(ready, { ...TEST_ONLY, submission: "disabled" }, "live").problems.join(" ")).toMatch(
      /IMX_ALLOW_SUBMISSION=1/,
    );
    expect(submitState({ ...ready, approval: null }, TEST_ONLY, "live").problems).toContain("Approve this application first.");
    expect(
      submitState({ ...ready, application: { ...ready.application, state: "SUBMITTING" } }, TEST_ONLY, "live").enabled,
    ).toBe(false);
    expect(submitState({ ...ready, submit: { ...ready.submit, allowed: false, problems: [] } }, TEST_ONLY, "live").problems).toEqual([
      "The service reports that this application can't be submitted now.",
    ]);
  });

  it("sends exactly the approved packet with the confirmation", async () => {
    const ready = await submittable();
    expect(submitRequest(ready)).toEqual({ packetId: "pkt_fictional_1", confirm: true });
    expect(() => submitRequest({ approval: null })).toThrow();
  });

  it("shows why Submit is off, and the confirmation asks before sending", async () => {
    const review = await northwind();
    const off = renderToStaticMarkup(
      <ReviewActions
        review={review}
        submit={submitState(review, null, "preview")}
        pending={null}
        onApprove={() => {}}
        onSubmit={() => {}}
        onPrepareAgain={() => {}}
        onResumeInBrowser={() => {}}
      />,
    );
    expect(text(off)).toContain("Why Submit is off");
    expect(text(off)).toContain(PREVIEW_SUBMIT_PROBLEM);
    expect(off).toMatch(/<button[^>]*disabled=""[^>]*>Submit application…/);
    expect(text(off)).toContain("Approve these answers");

    const ready = await submittable();
    const confirm = renderToStaticMarkup(
      <SubmitConfirm open review={ready} pending={false} onConfirm={() => {}} onClose={() => {}} />,
    );
    expect(text(confirm)).toContain("I reviewed every answer");
    expect(text(confirm)).toContain("It can’t be undone");
    expect(text(confirm)).toContain("A browser window opens");
    // The final button waits for the checkbox.
    expect(confirm).toMatch(/<button type="submit"[^>]*disabled=""[^>]*>Submit to Northwind Cartography/);
  });

  it("describes a finished submission instead of offering it again", async () => {
    const review = await northwind();
    const submitted = {
      ...review,
      application: {
        ...review.application,
        state: "SUBMITTED" as const,
        receipt: {
          receiptId: "rcpt_1",
          submittedAt: "2026-09-23T16:10:00Z",
          confirmationReference: "FIC-000001",
          evidence: [],
        },
      },
    };
    expect(outcomeLine(submitted)).toBe("Submitted and confirmed by the site. Reference FIC-000001.");
    const reported = {
      ...submitted,
      application: {
        ...submitted.application,
        receipt: { ...submitted.application.receipt, confirmationMethod: "USER_CONFIRMED" as const, confirmationAuthority: "user" as const },
      },
    };
    expect(outcomeLine(reported)).toBe("Submitted, on your report of a confirmation. Reference FIC-000001.");
    const html = renderToStaticMarkup(<ReviewNotices review={submitted} onOpenDesk={() => {}} />);
    expect(text(html)).toContain("See the receipt on the desk");
    expect(outcomeLine({ ...review, application: { ...review.application, state: "SUBMISSION_UNKNOWN" } })).toMatch(
      /never sent again automatically/,
    );
  });
});

describe("editing an answer", () => {
  it("sends one answer under its question id, as an answer or a statement, with an allowed reuse scope", () => {
    const row = { questionId: "q_salary", edit: TEXT_EDIT };
    expect(editRequest(row, " 150000 ", "global")).toEqual({
      answers: { q_salary: "150000" },
      attestations: {},
      reuse: { q_salary: "global" },
    });
    const statement = { questionId: "q_certify", edit: { ...TEXT_EDIT, control: "boolean" as const, attestation: true, value: true } };
    expect(editRequest(statement, true, "application")).toEqual({
      answers: {},
      attestations: { q_certify: true },
      reuse: { q_certify: "application" },
    });
    // A scope the service doesn't allow for this answer falls back to this application.
    const local = { questionId: "q_fit", edit: { ...TEXT_EDIT, control: "long_text" as const, reuse: ["application" as const] } };
    expect(editRequest(local, "Line one.\n\nLine two. ", "global")).toEqual({
      answers: { q_fit: "Line one.\n\nLine two. " },
      attestations: {},
      reuse: { q_fit: "application" },
    });
    expect(() => editRequest({ questionId: null, edit: TEXT_EDIT }, "x", "application")).toThrow();
    expect(allowedReuse({ reuse: [] })).toEqual(["application"]);
    expect(allowedReuse({ reuse: ["global", "application"] })).toEqual(["application", "global"]);
  });

  it("refuses blanks, options the form doesn't have and an unchecked required statement", () => {
    expect(editProblem(TEXT_EDIT, "  ")).toMatch(/can't be cleared/);
    const choice: ReviewEditView = {
      ...TEXT_EDIT,
      control: "single_select",
      options: [
        { value: "yes", label: "Yes" },
        { value: "no", label: "No" },
      ],
      value: "yes",
    };
    expect(editProblem(choice, "maybe")).toBe("Choose one of the listed options.");
    expect(editProblem(choice, "no")).toBeNull();
    expect(editProblem({ ...choice, control: "multi_select", value: ["yes"] }, ["yes", "maybe"])).toMatch(/only from/);
    const statement: ReviewEditView = { ...TEXT_EDIT, control: "boolean", attestation: true, value: true };
    expect(editProblem(statement, false)).toMatch(/only stay checked/);
    expect(editProblem({ ...statement, required: false }, false)).toBeNull();
    expect(initialEditValue(choice)).toBe("yes");
    expect(initialEditValue({ ...choice, control: "multi_select", value: null })).toEqual([]);
    expect(initialEditValue(statement)).toBe(true);
    expect(unchanged(TEXT_EDIT, " 145000")).toBe(true);
    expect(unchanged({ ...choice, control: "multi_select", value: ["a", "b"] }, ["b", "a"])).toBe(true);
  });
});

describe("the preview review service", () => {
  it("queues the fictional applications newest first", async () => {
    const service = new PreviewReviewService();
    const { applications } = await service.queue();
    expect(applications.map((entry) => entry.id)).toEqual([PREPARED_PREVIEW_ID, PREVIEW_APPROVED_ID, PREVIEW_SIGN_IN_ID]);
    expect(applications.map((entry) => entry.hold.kind)).toEqual(["ready", "approved", "sign_in"]);
    expect(applications[0]).toMatchObject({ captchaPending: true, approved: false, stage: "prepared" });
    expect(applications[2]).toMatchObject({ stage: "browser_action", preparedAt: null });
  });

  it("approves the reviewed packet only, and an edit withdraws the approval until it's prepared again", async () => {
    const service = new PreviewReviewService({ now: () => new Date("2026-09-23T16:00:00Z") });
    const first = await service.review(PREPARED_PREVIEW_ID);
    await expect(service.approve(PREPARED_PREVIEW_ID, { packetId: "pkt_other" })).rejects.toMatchObject({ code: "conflict" });
    const approved = await service.approve(PREPARED_PREVIEW_ID, { packetId: first.preparedPacketId! });
    expect(approved.approval).toMatchObject({ packetId: first.preparedPacketId, pages: 2 });
    expect((await service.queue()).applications[0]).toMatchObject({ approved: true, hold: { kind: "approved" } });

    const salary = approved.answers.find((row) => row.questionId === "q_pv_nw_salary")!;
    await service.editAnswer(PREPARED_PREVIEW_ID, editRequest(salary, "150000", "job"));
    const edited = await service.review(PREPARED_PREVIEW_ID);
    expect(edited.approval).toBeNull();
    expect(edited.changedSincePreparation).toBe(true);
    expect(edited.preparedPacketId).toBeNull();
    const changed = edited.answers.find((row) => row.questionId === "q_pv_nw_salary")!;
    expect(changed).toMatchObject({ value: "150000", provenance: { kind: "user" } });
    await expect(service.approve(PREPARED_PREVIEW_ID, { packetId: first.preparedPacketId! })).rejects.toMatchObject({
      code: "conflict",
    });

    await service.prepareAgain(PREPARED_PREVIEW_ID);
    const again = await service.review(PREPARED_PREVIEW_ID);
    expect(again.changedSincePreparation).toBe(false);
    expect(again.preparedPacketId).not.toBe(first.preparedPacketId);
    expect((await service.approve(PREPARED_PREVIEW_ID, { packetId: again.preparedPacketId! })).approval).not.toBeNull();
  });

  it("refuses answers the question doesn't take, keyed by question id", async () => {
    const service = new PreviewReviewService();
    const review = await service.review(PREPARED_PREVIEW_ID);
    const authorized = review.answers.find((row) => row.questionId === "q_pv_nw_authorized")!;
    const refused = await service
      .editAnswer(PREPARED_PREVIEW_ID, { answers: { q_pv_nw_authorized: "maybe" }, attestations: {}, reuse: {} })
      .catch((error: unknown) => error);
    expect(refused).toBeInstanceOf(ServiceError);
    expect((refused as ServiceError).fieldErrors).toEqual({ q_pv_nw_authorized: "Choose one of the listed options." });
    const narrowed = await service
      .editAnswer(PREPARED_PREVIEW_ID, { answers: { q_pv_nw_platforms: ["braze"] }, attestations: {}, reuse: { q_pv_nw_platforms: "global" } })
      .catch((error: unknown) => error);
    expect((narrowed as ServiceError).fieldErrors.q_pv_nw_platforms).toMatch(/this application/);
    await expect(
      service.editAnswer(PREPARED_PREVIEW_ID, { answers: { q_unknown: "x" }, attestations: {}, reuse: {} }),
    ).rejects.toMatchObject({ code: "conflict" });
    // Nothing changed: the approval is still possible on the same packet.
    expect((await service.review(PREPARED_PREVIEW_ID)).preparedPacketId).toBe(review.preparedPacketId);
    expect(authorized.edit?.options?.map((option) => option.label)).toEqual(["Yes", "No"]);
  });

  it("never submits, and finishes a browser step by preparing the form", async () => {
    const service = new PreviewReviewService();
    const approved = await service.review(PREVIEW_APPROVED_ID);
    expect(approved.submit).toMatchObject({ allowed: false, problems: [PREVIEW_SUBMIT_PROBLEM] });
    await expect(service.submit(PREVIEW_APPROVED_ID, { packetId: approved.approval!.packetId, confirm: true })).rejects.toMatchObject({
      code: "invalid",
      message: PREVIEW_SUBMIT_PROBLEM,
    });
    const held = await service.review(PREVIEW_SIGN_IN_ID);
    expect(held).toMatchObject({ stage: "browser_action", browser: { available: true } });
    expect(held.application.needs?.kind).toBe("interaction");
    await service.prepareAgain(PREVIEW_SIGN_IN_ID);
    const prepared = await service.review(PREVIEW_SIGN_IN_ID);
    expect(prepared.stage).toBe("prepared");
    expect(prepared.application.preparation?.ready).toBe(true);
    expect(prepared.answers.length).toBeGreaterThan(0);
    await expect(service.review("pv_missing")).rejects.toMatchObject({ code: "not_found" });
  });
});

describe("the review lane's HTTP routes", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads and acts through the gateway with bodies only", async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, method: init.method ?? "GET", body: init.body ? JSON.parse(String(init.body)) : undefined });
        return new Response(JSON.stringify({ applications: [] }), { status: 200, headers: { "Content-Type": "application/json" } });
      }),
    );
    const service = new HttpReviewService();
    await service.queue();
    await service.review("app/1");
    await service.approve("app_1", { packetId: "pkt_1" });
    await service.submit("app_1", { packetId: "pkt_1", confirm: true });
    await service.editAnswer("app_1", { answers: { q: "v" }, attestations: {}, reuse: { q: "job" } });
    await service.prepareAgain("app_1");
    expect(calls).toEqual([
      { url: "/api/imx/review", method: "GET", body: undefined },
      { url: "/api/imx/applications/app%2F1/review", method: "GET", body: undefined },
      { url: "/api/imx/applications/app_1/approve", method: "POST", body: { packetId: "pkt_1" } },
      { url: "/api/imx/applications/app_1/submit", method: "POST", body: { packetId: "pkt_1", confirm: true } },
      { url: "/api/imx/applications/app_1/answers", method: "POST", body: { answers: { q: "v" }, attestations: {}, reuse: { q: "job" } } },
      { url: "/api/imx/applications/app_1/resume", method: "POST", body: {} },
    ]);
  });

  it("maps a refused edit to field errors keyed by question id", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              error: { code: "invalid", message: "Some of the request is missing or malformed.", fieldErrors: { q_1: "Choose one of the listed options." } },
            }),
            { status: 422, headers: { "Content-Type": "application/json" } },
          ),
      ),
    );
    const error = await new HttpReviewService().editAnswer("app_1", { answers: { q_1: "x" }, attestations: {} }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ServiceError);
    expect(error).toMatchObject({ code: "invalid", fieldErrors: { q_1: "Choose one of the listed options." } });
  });
});
