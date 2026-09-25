import { expect, test, type Page } from "@playwright/test";

// Fictional records only: example.test addresses, a loopback test site and made-up employers.

const PREVIEW_SUBMIT_PROBLEM = "The preview never submits: there's no employer behind it.";

function trackMutations(page: Page) {
  const posts: { path: string; body: unknown }[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/imx/") && request.method() !== "GET") {
      posts.push({ path: url.pathname, body: request.postDataJSON() });
    }
  });
  return posts;
}

const answers = (page: Page) => page.getByRole("region", { name: "Answers on the form" });
const decisions = (page: Page) => page.getByRole("complementary", { name: "Decisions" });

test.describe("the review lane in the preview", () => {
  test("queue, review with every provenance kind, approve, then change an answer and prepare again", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const posts = trackMutations(page);
    await page.goto("/preview/review");
    await expect(page.getByRole("heading", { level: 1, name: "Prepared queue" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Sections" }).getByRole("link", { name: "Review" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    const rows = page.locator("tr.review-queue__row");
    await expect(rows).toHaveCount(3);
    await expect(rows.first()).toContainText("Northwind Cartography");
    await expect(rows.first()).toContainText("CAPTCHA to solve");
    await expect(rows.first()).toContainText("$0.42");
    const filters = page.getByRole("group", { name: "Filter the queue" });
    await filters.getByRole("button", { name: /Approved/ }).click();
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("Larkspur Field Guides");
    await filters.getByRole("button", { name: /In the browser/ }).click();
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("Quarry & Pine Outfitters");
    await filters.getByRole("button", { name: /All$/ }).click();
    await expect(rows).toHaveCount(3);

    await page.getByRole("link", { name: "Review: Northwind Cartography, Senior Lifecycle Marketer" }).click();
    await expect(page).toHaveURL(/\/preview\/review\/pv_prepared_northwind$/);
    await expect(page.getByRole("heading", { level: 1, name: "Senior Lifecycle Marketer" })).toBeVisible();
    await expect(page.getByTestId("captcha-note")).toBeVisible();

    // Every answer with where it came from, the narrative in full with its sources.
    const list = answers(page);
    for (const label of [
      "Your details",
      "Your resume",
      "Saved answer",
      "Saved policy",
      "Derived",
      "Fact-grounded screener",
      "RAG narrative",
      "Your answer",
      "Left blank",
    ]) {
      await expect(list.locator(".provenance-badge", { hasText: label }).first()).toBeVisible();
    }
    await expect(list.getByText(/a product people carry into the field/)).toBeVisible();
    await list.getByText("Cites 3 facts, 2 story passages and 1 job note").click();
    await expect(list.getByText("story_pv_onboarding_c2")).toBeVisible();
    await expect(list.getByText("job_pv_northwind_req_2")).toBeVisible();
    const copy = list.getByRole("button", { name: /Copy text of the answer to: Why are you a good fit/ });
    await copy.click();
    await expect(copy).toContainText("Copied");

    // Submit stays off in the preview and says why.
    const panel = decisions(page);
    await expect(panel.getByRole("button", { name: "Submit application…" })).toBeDisabled();
    await expect(panel.getByText(PREVIEW_SUBMIT_PROBLEM)).toBeVisible();

    await panel.getByRole("button", { name: "Approve these answers" }).click();
    await expect(panel.getByTestId("approval-block")).toContainText("Approved");
    await expect(panel.getByRole("button", { name: "Approve these answers" })).toHaveCount(0);

    // Change an answer: saved, prepared again, and the approval is withdrawn.
    await list.getByRole("button", { name: "Edit answer: What are your base salary expectations (USD)?" }).click();
    const editor = list.getByRole("form", { name: /Change the answer to: What are your base salary expectations/ });
    await editor.getByLabel("New answer").fill("150000");
    await editor.getByRole("radio", { name: /This application only/ }).check();
    await editor.getByRole("button", { name: "Save and prepare again" }).click();
    const salary = list.locator("li.answer", { hasText: "What are your base salary expectations (USD)?" });
    await expect(salary).toContainText("150000");
    await expect(salary.locator(".provenance-badge")).toHaveText("Your answer");
    await expect(panel.getByRole("button", { name: "Approve these answers" })).toBeVisible();

    // "Save only" leaves the form as it was until it's prepared again.
    await list.getByRole("button", { name: "Edit answer: Pronouns" }).click();
    const pronouns = list.getByRole("form", { name: /Change the answer to: Pronouns/ });
    await pronouns.getByLabel("New answer").fill("they/them");
    await pronouns.getByRole("button", { name: "Save only" }).click();
    await expect(page.getByTestId("changed-note")).toBeVisible();
    await expect(panel.getByText(/You changed answers since this preparation/)).toBeVisible();
    await panel.getByRole("button", { name: "Prepare again" }).click();
    await expect(page.getByTestId("changed-note")).toHaveCount(0);
    await expect(panel.getByRole("button", { name: "Approve these answers" })).toBeVisible();

    // The preview sends nothing anywhere.
    expect(posts).toEqual([]);
  });

  test("an application held by a sign-in is finished in the browser", async ({ page }) => {
    await page.goto("/preview/review/pv_signin_quarry");
    await expect(page.getByRole("heading", { level: 1, name: "Lifecycle Marketing Lead" })).toBeVisible();
    await expect(page.getByTestId("browser-note")).toContainText("Sign in to continue");
    await expect(decisions(page).getByRole("button", { name: "Approve these answers" })).toHaveCount(0);
    await decisions(page).getByRole("button", { name: "Resume in browser" }).click();
    await expect(page.locator(".review-head .eyebrow")).toContainText("Prepared · nothing submitted");
    await expect(decisions(page).getByRole("button", { name: "Approve these answers" })).toBeVisible();
  });
});

// ---- the live dashboard against routed fixtures ----

const APP_ID = "app_live_1";
const LOOPBACK_URL = "http://127.0.0.1:9/fictional-harbor/4012/apply";
const SUBMISSION_OFF =
  "Submission is turned off in this service. Start it with IMX_ALLOW_SUBMISSION=1 to submit approved applications from the dashboard.";

function health(extra: Record<string, unknown> = {}) {
  return {
    status: "ok",
    executor: "idle",
    runner: "available",
    applicationMode: "TEST_ONLY",
    presentationVersion: "2",
    submission: "disabled",
    browser: "visible",
    ...extra,
  };
}

function application(state = "NEEDS_INPUT", extra: Record<string, unknown> = {}) {
  const prepared = state === "NEEDS_INPUT";
  return {
    id: APP_ID,
    state,
    applicationUrl: LOOPBACK_URL,
    job: { title: "Brand Marketing Manager", company: "Fictional Harbor Co", ats: "Greenhouse" },
    requestedAt: "2026-09-23T14:00:00Z",
    updatedAt: "2026-09-23T15:00:00Z",
    progress: { page: 1, pageCount: null },
    resumeFileName: "Casey_Fixture.pdf",
    needs: null,
    receipt: null,
    prior: null,
    failure: null,
    uncertain: null,
    events: [
      { id: "e1", type: "preparation.ready", at: "2026-09-23T15:00:00Z", message: "Ready for final review. Nothing was submitted.", tone: "attention" },
    ],
    preparation: prepared
      ? {
          ready: true,
          formStep: 0,
          formUrl: LOOPBACK_URL,
          captchaPending: false,
          preparedAt: "2026-09-23T15:00:00Z",
          submitted: false,
          evidence: [],
        }
      : null,
    review: [],
    ...extra,
  };
}

const ROWS = [
  {
    questionId: null,
    question: "Email",
    wordingRecorded: true,
    page: 1,
    control: "text",
    value: "casey@example.test",
    required: true,
    provenance: { kind: "identity", label: "Your details", detail: "verified identity: email" },
    citations: null,
    confidence: 1,
    edit: null,
    noEditReason: "Your verified details are changed in your profile, not here.",
  },
  {
    questionId: "q_live_auth",
    question: "Are you authorized to work in the United States?",
    wordingRecorded: true,
    page: 1,
    control: "single_select",
    value: "Yes",
    required: true,
    provenance: { kind: "saved_policy", label: "Saved policy", detail: null },
    citations: null,
    confidence: 1,
    edit: {
      control: "single_select",
      options: [
        { value: "1", label: "Yes" },
        { value: "0", label: "No" },
      ],
      lookup: false,
      attestation: false,
      required: true,
      value: "1",
      reuse: ["application", "job", "global"],
      note: null,
    },
    noEditReason: null,
  },
];

function review(extra: Record<string, unknown> = {}) {
  return {
    application: application(),
    stage: "prepared",
    preparedPacketId: "pkt_live_1",
    approval: null,
    changedSincePreparation: false,
    providerCost: { knownUsd: 0.21, calls: 9, unknownCostCalls: 0 },
    answers: ROWS,
    editNote: null,
    submit: {
      allowed: false,
      problems: [SUBMISSION_OFF],
      enabled: false,
      opensBrowser: true,
      command: `IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit ${APP_ID} --yes`,
    },
    browser: { available: true, reason: null, command: `interviewmaxxing resume ${APP_ID} --act` },
    ...extra,
  };
}

const APPROVAL = { packetId: "pkt_live_1", approvedAt: "2026-09-23T15:30:00Z", approver: "dashboard:fixture", pages: 1 };

const QUEUE = {
  applications: [
    {
      id: APP_ID,
      state: "NEEDS_INPUT",
      stage: "prepared",
      applicationUrl: LOOPBACK_URL,
      job: { title: "Brand Marketing Manager", company: "Fictional Harbor Co", ats: "Greenhouse" },
      preparedAt: "2026-09-23T15:00:00Z",
      stoppedAt: "2026-09-23T15:00:00Z",
      captchaPending: false,
      providerCost: { knownUsd: 0.21, calls: 9, unknownCostCalls: 0 },
      hold: { kind: "ready", summary: "Ready to review and approve." },
      approved: false,
    },
  ],
};

test.describe("the review lane against a live service (routed fixtures)", () => {
  test("Submit stays off and says why when the service has submission off; nothing is sent", async ({ page }) => {
    const posts = trackMutations(page);
    await page.route("**/api/imx/healthz", (route) => route.fulfill({ json: health() }));
    await page.route("**/api/imx/review", (route) => route.fulfill({ json: QUEUE }));
    await page.route(`**/api/imx/applications/${APP_ID}/review`, (route) =>
      route.fulfill({ json: review({ approval: APPROVAL }) }),
    );
    await page.goto(`/review/${APP_ID}`);
    await expect(page.getByRole("heading", { level: 1, name: "Brand Marketing Manager" })).toBeVisible();
    await expect(page.getByText("Submission off", { exact: true })).toBeVisible();
    const panel = decisions(page);
    await expect(panel.getByTestId("approval-block")).toContainText("Approved");
    await expect(panel.getByRole("button", { name: "Submit application…" })).toBeDisabled();
    await expect(panel.getByText(SUBMISSION_OFF)).toBeVisible();
    await expect(answers(page).getByText("Your verified details are changed in your profile, not here.")).toBeVisible();
    expect(posts).toEqual([]);
  });

  test("an approved application is submitted only after the confirmation, with exactly the approved packet", async ({ page }) => {
    const posts = trackMutations(page);
    let state: "NEEDS_INPUT" | "SUBMITTING" | "SUBMITTED" = "NEEDS_INPUT";
    const allowed = { allowed: true, problems: [], enabled: true, opensBrowser: true, command: "IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit app_live_1 --yes" };
    const current = () =>
      state === "NEEDS_INPUT"
        ? review({ approval: APPROVAL, submit: allowed })
        : review({
            stage: "other",
            preparedPacketId: null,
            approval: null,
            submit: { ...allowed, allowed: false, problems: ["This application was already submitted."] },
            application: application(state, {
              receipt:
                state === "SUBMITTED"
                  ? { receiptId: "rcpt_1", submittedAt: "2026-09-23T15:40:00Z", confirmationReference: "FIC-000001", evidence: [] }
                  : null,
            }),
          });
    await page.route("**/api/imx/healthz", (route) => route.fulfill({ json: health({ submission: "enabled" }) }));
    await page.route("**/api/imx/review", (route) => route.fulfill({ json: QUEUE }));
    await page.route(`**/api/imx/applications/${APP_ID}/review`, (route) => {
      const json = current();
      if (state === "SUBMITTING") state = "SUBMITTED";
      return route.fulfill({ json });
    });
    await page.route(`**/api/imx/applications/${APP_ID}/submit`, (route) => {
      state = "SUBMITTING";
      return route.fulfill({ json: current() });
    });

    await page.goto(`/review/${APP_ID}`);
    await expect(page.getByText("Submission on", { exact: true })).toBeVisible();
    await decisions(page).getByRole("button", { name: "Submit application…" }).click();
    const dialog = page.getByRole("dialog", { name: "Submit this application?" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(/A browser window opens/)).toBeVisible();
    const confirm = dialog.getByRole("button", { name: "Submit to Fictional Harbor Co" });
    await expect(confirm).toBeDisabled();
    expect(posts).toEqual([]);
    await dialog.getByLabel(/I reviewed every answer/).check();
    await confirm.click();
    await expect(page.getByTestId("review-outcome")).toContainText("Submitted and confirmed by the site. Reference FIC-000001.");
    expect(posts).toEqual([{ path: `/api/imx/applications/${APP_ID}/submit`, body: { packetId: "pkt_live_1", confirm: true } }]);
  });

  test("approve pins the reviewed packet; an edit goes through the answers route, then prepares again", async ({ page }) => {
    const posts = trackMutations(page);
    let current = review();
    let answersSeen = 0;
    await page.route("**/api/imx/healthz", (route) => route.fulfill({ json: health() }));
    await page.route("**/api/imx/review", (route) => route.fulfill({ json: QUEUE }));
    await page.route(`**/api/imx/applications/${APP_ID}/review`, (route) => route.fulfill({ json: current }));
    await page.route(`**/api/imx/applications/${APP_ID}/approve`, (route) => {
      current = review({ approval: APPROVAL });
      return route.fulfill({ json: current });
    });
    await page.route(`**/api/imx/applications/${APP_ID}/answers`, (route) => {
      answersSeen += 1;
      if (answersSeen === 1) {
        return route.fulfill({
          status: 422,
          json: {
            error: {
              code: "invalid",
              message: "Some of the request is missing or malformed.",
              fieldErrors: { q_live_auth: "This answer can only be kept for this application." },
            },
          },
        });
      }
      current = review({ changedSincePreparation: true, preparedPacketId: null });
      return route.fulfill({ json: application() });
    });
    await page.route(`**/api/imx/applications/${APP_ID}/resume`, (route) => {
      current = review({ preparedPacketId: "pkt_live_2" });
      return route.fulfill({ json: application() });
    });

    await page.goto(`/review/${APP_ID}`);
    const panel = decisions(page);
    await panel.getByRole("button", { name: "Approve these answers" }).click();
    await expect(panel.getByTestId("approval-block")).toContainText("Approved");

    const list = answers(page);
    await list.getByRole("button", { name: "Edit answer: Are you authorized to work in the United States?" }).click();
    const editor = list.getByRole("form", { name: /Change the answer to: Are you authorized/ });
    await editor.getByRole("radio", { name: "No", exact: true }).check();
    await editor.getByRole("radio", { name: /Every application/ }).check();
    await editor.getByRole("button", { name: "Save and prepare again" }).click();
    // The service refused it: shown on the answer, and nothing was prepared.
    await expect(editor.getByText("This answer can only be kept for this application.")).toBeVisible();
    await editor.getByRole("radio", { name: /This application only/ }).check();
    await editor.getByRole("button", { name: "Save and prepare again" }).click();
    await expect(editor).toHaveCount(0);
    await expect(panel.getByRole("button", { name: "Approve these answers" })).toBeVisible();

    expect(posts).toEqual([
      { path: `/api/imx/applications/${APP_ID}/approve`, body: { packetId: "pkt_live_1" } },
      {
        path: `/api/imx/applications/${APP_ID}/answers`,
        body: { answers: { q_live_auth: "0" }, attestations: {}, reuse: { q_live_auth: "global" } },
      },
      {
        path: `/api/imx/applications/${APP_ID}/answers`,
        body: { answers: { q_live_auth: "0" }, attestations: {}, reuse: { q_live_auth: "application" } },
      },
      { path: `/api/imx/applications/${APP_ID}/resume`, body: {} },
    ]);
  });

  test("a TEST_ONLY service doesn't act on an employer's site from the review page", async ({ page }) => {
    const posts = trackMutations(page);
    const employer = "https://jobs.example.test/fictional-harbor/4012/apply";
    await page.route("**/api/imx/healthz", (route) => route.fulfill({ json: health({ submission: "enabled" }) }));
    await page.route("**/api/imx/review", (route) => route.fulfill({ json: QUEUE }));
    await page.route(`**/api/imx/applications/${APP_ID}/review`, (route) =>
      route.fulfill({
        json: review({
          application: application("NEEDS_INPUT", { applicationUrl: employer }),
          approval: APPROVAL,
          submit: { allowed: true, problems: [], enabled: true, opensBrowser: true, command: "x" },
        }),
      }),
    );
    await page.goto(`/review/${APP_ID}`);
    const panel = decisions(page);
    await expect(panel.getByRole("button", { name: "Submit application…" })).toBeDisabled();
    await expect(panel.getByText(/TEST_ONLY mode/)).toBeVisible();
    await panel.getByRole("button", { name: "Prepare again" }).click();
    await expect(page.getByRole("alert").filter({ hasText: /TEST_ONLY mode/ })).toBeVisible();
    expect(posts).toEqual([]);
  });

  test("an older service without the review routes gets a quiet note", async ({ page }) => {
    await page.route("**/api/imx/healthz", (route) => route.fulfill({ json: health() }));
    await page.route("**/api/imx/review", (route) =>
      route.fulfill({ status: 404, json: { error: { code: "not_found", message: "No such route." } } }),
    );
    await page.goto("/review");
    await expect(page.getByRole("heading", { name: "This service doesn’t provide the review queue yet" })).toBeVisible();
    // No alarm: the quiet note only, no alert or error line.
    await expect(page.locator('.form-alert, .notice[role="alert"]')).toHaveCount(0);
  });

  test("the queue lists newest first and opens a review", async ({ page }) => {
    await page.route("**/api/imx/healthz", (route) => route.fulfill({ json: health() }));
    const older = { ...QUEUE.applications[0], id: "app_live_0", job: { ...QUEUE.applications[0].job, company: "Fictional Older Co" }, stoppedAt: "2026-09-22T09:00:00Z", preparedAt: "2026-09-22T09:00:00Z" };
    await page.route("**/api/imx/review", (route) => route.fulfill({ json: { applications: [older, QUEUE.applications[0]] } }));
    await page.route(`**/api/imx/applications/${APP_ID}/review`, (route) => route.fulfill({ json: review() }));
    await page.goto("/review");
    const rows = page.locator("tr.review-queue__row");
    await expect(rows).toHaveCount(2);
    await expect(rows.first()).toContainText("Fictional Harbor Co");
    await expect(rows.nth(1)).toContainText("Fictional Older Co");
    await page.getByRole("link", { name: "Review: Fictional Harbor Co, Brand Marketing Manager" }).click();
    await expect(page).toHaveURL(new RegExp(`/review/${APP_ID}$`));
    await expect(page.getByRole("heading", { level: 1, name: "Brand Marketing Manager" })).toBeVisible();
  });
});
