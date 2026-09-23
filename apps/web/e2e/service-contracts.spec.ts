import { expect, test, type Page } from "@playwright/test";
import { DEFAULT_PREFERENCES, type ListingView } from "../lib/jobs/types";
import { EMPTY_FIELDS } from "../lib/pipeline/types";

const selection = {
  id: "selection_cached", effectiveChoice: "REVIEW" as const, modelChoice: "REVIEW" as const,
  probabilities: null, confidence: null, requestedModel: "fixture", returnedModel: "fixture", rubricVersion: "fixture",
  holds: [], reasons: ["Review the stated responsibilities."], unresolved: [], providerError: null,
  decidedAt: "2026-09-22T12:00:00Z", stale: false,
};
const listing: ListingView = {
  id: "listing_fixture", title: "Paid Acquisition Lead", company: "Fictional Tern", location: "Austin, TX", workArrangement: "HYBRID",
  remoteEligibility: null, compensation: null, description: "Own paid search and paid social acquisition.", descriptionCompleteness: "FULL",
  status: "OPEN", postedText: null, observedAt: "2026-09-22T12:00:00Z", selection,
  provenance: [{ source: "builtin", sourceUrl: "https://source.example.test/search", postingUrl: "https://source.example.test/job/tern", applicationUrl: null, observedAt: "2026-09-22T12:00:00Z" }],
  pipelineEntryId: null, applicationId: null, locationTier: "ONSITE_HYBRID_TARGET",
};

async function jobs(page: Page, terminal: "DONE" | "FAILED" | "INTERRUPTED") {
  let posts = 0;
  await page.route("**/api/imx/selection/preferences", (route) => route.fulfill({ json: { ...DEFAULT_PREFERENCES, fingerprint: "fixture" } }));
  await page.route("**/api/imx/jobs", (route) => route.fulfill({ json: { listings: [listing], lastRun: null } }));
  await page.route("**/api/imx/selection/jobs/listing_fixture", (route) => {
    posts += 1;
    return route.fulfill({ status: 202, json: { ...listing, decisionTask: { id: "decision_task_fixture", state: "RUNNING", error: null, resultId: null, requestedAt: "2026-09-22T12:00:00Z", updatedAt: "2026-09-22T12:00:00Z" } } });
  });
  await page.route("**/api/imx/jobs/listing_fixture", (route) => route.fulfill({ json: {
    ...listing,
    decisionTask: { id: "decision_task_fixture", state: terminal, error: terminal === "DONE" ? null : "The decision service stopped. You can ask Jev again.", resultId: terminal === "DONE" ? selection.id : null, requestedAt: "2026-09-22T12:00:00Z", updatedAt: "2026-09-22T12:00:01Z" },
  } }));
  return () => posts;
}

test("202 decision polling completes when the cached result keeps the same id", async ({ page }) => {
  const posts = await jobs(page, "DONE");
  await page.goto("/jobs");
  await page.getByRole("button", { name: "Ask Jev again", exact: true }).click();
  await expect(page.getByText("Jev is reviewing")).toBeVisible();
  await expect(page.getByRole("button", { name: "Ask Jev again", exact: true })).toBeEnabled();
  expect(posts()).toBe(1);
});

test("an interrupted decision is recovered after reload without another POST", async ({ page }) => {
  const posts = await jobs(page, "INTERRUPTED");
  await page.goto("/jobs");
  await page.getByRole("button", { name: "Ask Jev again", exact: true }).click();
  await expect(page.getByText("Jev is reviewing")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("alert").filter({ hasText: "The decision service stopped" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Ask Jev again", exact: true })).toBeEnabled();
  expect(posts()).toBe(1);
});

test("a fresh tab resumes a durable running decision without a local marker or another POST", async ({ page }) => {
  const posts = await jobs(page, "DONE");
  await page.route("**/api/imx/jobs", (route) => route.fulfill({ json: { listings: [{
    ...listing, decisionTask: { id: "decision_task_fixture", state: "RUNNING", error: null, resultId: null,
      requestedAt: "2026-09-22T12:00:00Z", updatedAt: "2026-09-22T12:00:00Z" },
  }], lastRun: null } }));
  await page.goto("/jobs");
  await expect(page.getByText("Jev is reviewing")).toBeVisible();
  await expect(page.getByRole("button", { name: "Asking Jev…", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Ask Jev again", exact: true })).toBeEnabled();
  expect(posts()).toBe(0);
});

test("a job's posting URL is used before any source search URL", async ({ page }) => {
  await jobs(page, "DONE");
  await page.goto("/jobs");
  await expect(page.getByRole("link", { name: /Built In listing/ })).toHaveAttribute("href", "https://source.example.test/job/tern");
  await page.getByRole("button", { name: /^Apply/ }).click();
  await expect(page.getByRole("dialog").getByLabel(/Application link/)).toHaveValue("https://source.example.test/job/tern");
});

test("top-level posting links work and missing posting links never become source search links", async ({ page }) => {
  await jobs(page, "DONE");
  await page.route("**/api/imx/jobs", (route) => route.fulfill({ json: { listings: [{
    ...listing, postingUrl: "https://employer.example.test/job/tern", provenance: [{ ...listing.provenance[0], postingUrl: null }],
  }], lastRun: null } }));
  await page.goto("/jobs");
  await expect(page.locator('a[href="https://source.example.test/search"]')).toHaveCount(0);
  await page.getByRole("button", { name: /^Apply/ }).click();
  await expect(page.getByRole("dialog").getByLabel(/Application link/)).toHaveValue("https://employer.example.test/job/tern");
});

test("test mode rejects real employer dispatch before any application POST", async ({ page }) => {
  let posts = 0;
  await page.route("**/api/imx/healthz", (route) => route.fulfill({ json: { status: "ok", executor: "idle", runner: "available", applicationMode: "TEST_ONLY" } }));
  await page.route("**/api/imx/candidate", (route) => route.fulfill({ json: {
    profile: { firstName: "Casey", lastName: "Fixture", email: "casey@example.test", phone: "", location: "Austin, TX", linkedinUrl: "", websiteUrl: "" },
    resumes: [{ id: "resume_fixture", fileName: "Fictional.pdf", sizeBytes: 100, uploadedAt: "2026-09-22T12:00:00Z" }], defaultResumeId: "resume_fixture",
  } }));
  await page.route("**/api/imx/applications", (route) => { posts += 1; return route.abort(); });
  await page.goto("/");
  await expect(page.getByText("Test mode", { exact: true })).toBeVisible();
  await page.getByLabel("Application link").fill("https://employer.example.test/apply");
  await page.getByRole("button", { name: "Apply and submit" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Real employer applications are disabled" })).toBeVisible();
  expect(posts).toBe(0);
});

test("a mounted desk rechecks the recovered runner before starting", async ({ page }) => {
  let available = false;
  let healthReads = 0;
  let posts = 0;
  await page.route("**/api/imx/healthz", (route) => {
    healthReads += 1;
    return route.fulfill({ json: { status: "ok", executor: "idle", runner: available ? "available" : "unavailable", applicationMode: "TEST_ONLY" } });
  });
  await page.route("**/api/imx/candidate", (route) => route.fulfill({ json: {
    profile: { firstName: "Casey", lastName: "Fixture", email: "casey@example.test", phone: "", location: "Austin, TX", linkedinUrl: "", websiteUrl: "" },
    resumes: [{ id: "resume_fixture", fileName: "Fictional.pdf", sizeBytes: 100, uploadedAt: "2026-09-22T12:00:00Z" }], defaultResumeId: "resume_fixture",
  } }));
  await page.route("**/api/imx/applications", (route) => {
    posts += 1;
    return route.fulfill({ status: 409, json: { error: { code: "conflict", message: "Fixture accepted the request; nothing was executed." } } });
  });
  await page.goto("/");
  await expect(page.getByLabel("First name")).toHaveValue("Casey");
  await page.getByLabel("Application link").fill("http://127.0.0.1:9/fictional/apply");
  await page.getByRole("button", { name: "Apply and submit" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "application browser is not ready" })).toBeVisible();
  expect(posts).toBe(0);
  available = true;
  await page.getByRole("button", { name: "Apply and submit" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Fixture accepted the request" })).toBeVisible();
  expect(posts).toBe(1);
  expect(healthReads).toBeGreaterThanOrEqual(3);
});

test("a failed refresh after a move conflict labels the cached board and offers retry", async ({ page }) => {
  let unavailable = false;
  const entry = { id: "pipeline_fixture", revision: 3, lane: "saved", fields: { ...EMPTY_FIELDS, company: "Fictional Tern", role: "Paid Acquisition Lead" },
    applicationUrl: null, listingId: null, origin: "manual", application: null, selection: null, provenance: null, history: [], createdAt: "2026-09-22T12:00:00Z", updatedAt: "2026-09-22T12:00:00Z" };
  await page.route("**/api/imx/pipeline", (route) => unavailable
    ? route.fulfill({ status: 503, json: { error: { code: "unavailable", message: "The local service stopped." } } })
    : route.fulfill({ json: { lanes: [{ id: "saved", label: "Saved" }, { id: "applied", label: "Applied" }], entries: [entry] } }));
  await page.route("**/api/imx/pipeline/entries/pipeline_fixture/move", (route) => {
    unavailable = true;
    return route.fulfill({ status: 409, json: { error: { code: "conflict", message: "This card changed elsewhere." } } });
  });
  await page.goto("/pipeline");
  await page.getByLabel("Move Fictional Tern to lane").selectOption("applied");
  await page.getByRole("button", { name: "Move", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Showing the last loaded board" })).toBeVisible();
  await expect(page.getByText(/board now shows the latest version/)).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Saved 1" })).toContainText("Fictional Tern");
  unavailable = false;
  await page.getByRole("button", { name: "Refresh board" }).click();
  await expect(page.getByRole("heading", { name: "Showing the last loaded board" })).toHaveCount(0);
});
