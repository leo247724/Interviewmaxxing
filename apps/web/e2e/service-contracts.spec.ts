import { expect, test } from "@playwright/test";
import { EMPTY_FIELDS } from "../lib/pipeline/types";

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

test("an invalid handoff link is explained on the desk and is never silently discarded", async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem("imx.deskHandoff", JSON.stringify({
    applicationUrl: "http://127.0.0.1:9/jobs/fictional", company: "Fictional Tern", role: "Paid Media Manager", from: "pipeline",
    pipelineEntryId: "pipe_old", listingId: "listing_old",
  })));
  await page.route("**/api/imx/healthz", (route) => route.fulfill({ json: { status: "ok", executor: "idle", runner: "available", applicationMode: "TEST_ONLY" } }));
  await page.route("**/api/imx/candidate", (route) => route.fulfill({ json: {
    profile: { firstName: "Casey", lastName: "Fixture", email: "casey@example.test", phone: "", location: "Austin, TX", linkedinUrl: "", websiteUrl: "" },
    resumes: [{ id: "resume_fixture", fileName: "Fictional.pdf", sizeBytes: 100, uploadedAt: "2026-09-22T12:00:00Z" }], defaultResumeId: "resume_fixture",
  } }));
  let posts = 0;
  await page.route("**/api/imx/applications", (route) => {
    posts += 1;
    expect(route.request().postDataJSON()).toMatchObject({ pipelineEntryId: "pipe_old", listingId: "listing_old" });
    return route.fulfill({ status: 422, json: { error: { code: "invalid", message: "The source link needs attention.", fieldErrors: { listingId: "That listing is no longer available." } } } });
  });
  await page.goto("/");
  await expect(page.getByLabel("First name")).toHaveValue("Casey");
  await page.getByRole("button", { name: "Apply and submit" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "That listing is no longer available. Return to the Pipeline and choose the current card before starting." })).toBeVisible();
  expect(posts).toBe(1);
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
