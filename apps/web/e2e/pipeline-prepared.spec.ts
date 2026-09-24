import { expect, test, type Page } from "@playwright/test";

// Fictional records only: example.test addresses and made-up companies.

const PREPARED_HEADLINE = "Prepared for your review — nothing submitted";

const focusButton = (page: Page) => page.getByRole("button", { name: /Prepared for review/ });

test.describe("prepared applications in the preview pipeline", () => {
  test("the Prepared for review filter finds the card and Review opens it in the desk", async ({ page }) => {
    await page.goto("/preview/pipeline");
    await expect(page.getByRole("heading", { name: /^Saved/ })).toBeVisible();

    await expect(focusButton(page)).toContainText("1");
    await focusButton(page).click();
    await expect(focusButton(page)).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("status").filter({ hasText: "Showing 1 of 8." })).toBeVisible();

    const cards = page.locator("article.card");
    await expect(cards).toHaveCount(1);
    const northwind = cards.first();
    await expect(northwind).toHaveAttribute("data-entry-id", "pipe_pv_northwind");
    await expect(northwind).toContainText("Northwind Cartography");
    await expect(northwind).toContainText("Senior Lifecycle Marketer");
    await expect(northwind.locator(".mark--prepared")).toHaveText("Prepared for review · CAPTCHA to solve");
    await expect(northwind.locator(".mark--receipt")).toHaveCount(0);
    await expect(northwind.locator(".mark--application")).toHaveCount(0);
    await expect(northwind.getByRole("button", { name: /^Apply/ })).toHaveCount(0);

    // The list layout honours the same filter, and combines with the text filter.
    await page.getByRole("radio", { name: "List" }).check();
    const rows = page.locator("table.pipeline-table tbody tr");
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("Northwind Cartography");
    await expect(rows.first().locator(".mark--prepared")).toBeVisible();
    await page.getByLabel("Filter by company, role, stage or status").fill("larkspur");
    await expect(rows).toHaveCount(0);
    await page.getByLabel("Filter by company, role, stage or status").fill("northwind");
    await expect(rows).toHaveCount(1);
    await page.getByRole("radio", { name: "Board" }).check();

    await northwind.getByRole("button", { name: "Review the prepared application for Northwind Cartography" }).click();
    await expect(page).toHaveURL(/\/preview$/);
    await expect(page.locator("#case-title")).toHaveText(PREPARED_HEADLINE);
    // A review opens the existing application: no new application is prefilled.
    await expect(page.getByRole("button", { name: "Apply and submit" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: /From your pipeline/ })).toHaveCount(0);
  });

  test("other cards keep Apply, and receipts stay receipts", async ({ page }) => {
    await page.goto("/preview/pipeline");
    await expect(page.getByRole("heading", { name: /^Saved/ })).toBeVisible();
    await expect(page.locator("article.card[data-entry-id='pipe_pv_northwind'] .mark--prepared")).toBeVisible();
    await expect(page.locator(".mark--prepared")).toHaveCount(1);
    const juniper = page.locator("article.card", { hasText: "Juniper & Vale Studio" });
    await expect(juniper.locator(".mark--receipt")).toContainText("Receipt confirmed");
    await expect(juniper.getByRole("button", { name: /^Review/ })).toHaveCount(0);
    const quarry = page.locator("article.card", { hasText: "Quarry & Pine Outfitters" });
    await expect(quarry.getByRole("button", { name: /^Apply/ })).toBeVisible();
    await expect(quarry.getByRole("button", { name: /^Review/ })).toHaveCount(0);
  });

  test("a review that can't be opened says so and leaves the compose form empty", async ({ page }) => {
    await page.goto("/preview/pipeline");
    await expect(page.getByRole("heading", { name: /^Saved/ })).toBeVisible();
    await page.evaluate(() =>
      sessionStorage.setItem(
        "imx.deskHandoff",
        JSON.stringify({
          applicationUrl: "https://jobs.example.test/fictional-missing/apply",
          company: "Fictional Missing Co",
          role: "Marketing Manager",
          from: "pipeline",
          pipelineEntryId: null,
          listingId: null,
          applicationId: "pv_not_in_this_session",
        }),
      ),
    );
    await page.goto("/preview");
    await expect(page.getByRole("alert").filter({ hasText: "Couldn’t open the prepared application:" })).toBeVisible();
    await expect(page.getByLabel("Application link")).toHaveValue("");
    await expect(page.locator("#case-title")).toHaveCount(0);
    await expect(page.getByRole("heading", { name: /From your pipeline/ })).toHaveCount(0);
  });
});

const FIELDS = {
  company: null,
  role: null,
  stage: null,
  status: null,
  priority: null,
  fitScore: null,
  nextInterviewDate: null,
  interviewTimeCT: null,
  interviewFormat: null,
  workArrangement: null,
  locationCommute: null,
  compensationLow: null,
  compensationHigh: null,
  compensationBasis: null,
  targetAssessment: null,
  sourceRecruiter: null,
  suggestedFollowUpDate: null,
  nextAction: null,
  lastInterviewDate: null,
  decisionDueText: null,
  compensationBenefitsNotes: null,
  fitRationale: null,
  processSourceNotes: null,
};

function liveEntry(id: string, company: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    lane: "saved",
    revision: 1,
    fields: { ...FIELDS, company, role: "Brand Marketing Manager" },
    applicationUrl: `https://jobs.example.test/${id}/apply`,
    listingId: null,
    origin: "manual",
    application: null,
    selection: null,
    provenance: null,
    history: [],
    createdAt: "2026-09-22T12:00:00Z",
    updatedAt: "2026-09-22T12:00:00Z",
    ...extra,
  };
}

const LANES = [
  { id: "saved", label: "Saved" },
  { id: "applied", label: "Applied" },
  { id: "closed", label: "Closed" },
];

const PREPARED_ID = "app_live_prepared";
const preparation = {
  ready: true,
  formStep: 1,
  formUrl: "https://jobs.example.test/pipe_live_prepared/apply/review",
  captchaPending: false,
  preparedAt: "2026-09-23T15:00:00Z",
  submitted: false,
  evidence: [],
};
const job = { title: "Brand Marketing Manager", company: "Fictional Harbor Co", ats: "Greenhouse" };
const preparedView = {
  id: PREPARED_ID,
  state: "NEEDS_INPUT",
  applicationUrl: "https://jobs.example.test/pipe_live_prepared/apply",
  job,
  requestedAt: "2026-09-23T14:00:00Z",
  updatedAt: "2026-09-23T15:00:00Z",
  progress: { page: 2, pageCount: 2 },
  resumeFileName: "Casey_Fixture.pdf",
  needs: null,
  receipt: null,
  prior: null,
  failure: null,
  uncertain: null,
  events: [
    {
      id: "e1",
      type: "preparation.ready",
      at: "2026-09-23T15:00:00Z",
      message: "Ready for final review. Nothing was submitted.",
      tone: "attention",
    },
  ],
  preparation,
  review: [
    { question: "Email", wordingRecorded: false, page: 1, control: "text", value: "casey@example.test", source: "identity", confidence: 1 },
  ],
};
const candidate = {
  profile: { firstName: "Casey", lastName: "Fixture", email: "casey@example.test", phone: "", location: "", linkedinUrl: "", websiteUrl: "" },
  resumes: [{ id: "r1", fileName: "Casey_Fixture.pdf", sizeBytes: 1000, uploadedAt: "2026-09-01T00:00:00Z" }],
  defaultResumeId: "r1",
};

test.describe("prepared applications in the live pipeline (routed fixtures)", () => {
  test("an older service without the list keeps the board as it was, with a quiet note", async ({ page }) => {
    await page.route("**/api/imx/pipeline", (route) =>
      route.fulfill({
        json: {
          lanes: LANES,
          entries: [
            liveEntry("pipe_live_waiting", "Fictional Waiting Co", {
              application: { applicationId: "app_live_waiting", state: "NEEDS_INPUT", submittedAt: null, confirmationReference: null },
            }),
            liveEntry("pipe_live_plain", "Fictional Plain Co"),
          ],
        },
      }),
    );
    await page.route("**/api/imx/applications", (route) =>
      route.fulfill({ status: 404, json: { error: { code: "not_found", message: "No such route." } } }),
    );
    await page.goto("/pipeline");
    const waiting = page.locator("article.card", { hasText: "Fictional Waiting Co" });
    await expect(waiting.locator(".mark--application")).toHaveText("Application: waiting for you");
    await expect(page.locator(".mark--prepared")).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^Review/ })).toHaveCount(0);
    await expect(page.locator("article.card", { hasText: "Fictional Plain Co" }).getByRole("button", { name: /^Apply/ })).toBeVisible();
    await expect(focusButton(page)).toContainText("0");
    await expect(page.getByText("This service doesn't report prepared applications yet, so none are marked.")).toBeVisible();
    // No alarm: no notice or alert about it, only the quiet line.
    await expect(page.locator(".notice, .form-alert")).toHaveCount(0);
  });

  test("Review opens the prepared application by reading it only", async ({ page }) => {
    const mutations: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/imx/") && request.method() !== "GET") mutations.push(`${request.method()} ${request.url()}`);
    });
    await page.route("**/api/imx/pipeline", (route) =>
      route.fulfill({
        json: {
          lanes: LANES,
          entries: [liveEntry("pipe_live_prepared", "Fictional Harbor Co"), liveEntry("pipe_live_plain", "Fictional Plain Co")],
        },
      }),
    );
    await page.route("**/api/imx/applications", (route) =>
      route.fulfill({
        json: {
          applications: [
            {
              id: PREPARED_ID,
              state: "NEEDS_INPUT",
              applicationUrl: preparedView.applicationUrl,
              job,
              requestedAt: preparedView.requestedAt,
              updatedAt: preparedView.updatedAt,
              preparation,
              pipelineEntryIds: ["pipe_live_prepared"],
            },
            {
              id: "app_live_questions",
              state: "NEEDS_INPUT",
              applicationUrl: "https://jobs.example.test/pipe_live_plain/apply",
              job: { title: "Brand Marketing Manager", company: "Fictional Plain Co", ats: "Lever" },
              requestedAt: "2026-09-23T13:00:00Z",
              updatedAt: "2026-09-23T13:30:00Z",
              preparation: null,
              pipelineEntryIds: ["pipe_live_plain"],
            },
          ],
        },
      }),
    );
    const statusIds: string[] = [];
    await page.route("**/api/imx/applications/*", (route) => {
      statusIds.push(new URL(route.request().url()).pathname.split("/").pop()!);
      return route.fulfill({ json: preparedView });
    });
    await page.route("**/api/imx/candidate", (route) => route.fulfill({ json: candidate }));

    await page.goto("/pipeline");
    await expect(focusButton(page)).toContainText("1");
    const harbor = page.locator("article.card", { hasText: "Fictional Harbor Co" });
    await expect(harbor.locator(".mark--prepared")).toHaveText("Prepared for review");
    await expect(page.locator("article.card", { hasText: "Fictional Plain Co" }).locator(".mark--prepared")).toHaveCount(0);

    await harbor.getByRole("button", { name: "Review the prepared application for Fictional Harbor Co" }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.locator("#case-title")).toHaveText(PREPARED_HEADLINE);
    expect(statusIds).toContain(PREPARED_ID);
    expect(await page.evaluate(() => sessionStorage.getItem("imx.activeApplicationId"))).toBe(PREPARED_ID);
    expect(mutations).toEqual([]);
  });
});
