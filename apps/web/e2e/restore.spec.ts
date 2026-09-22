import { expect, test, type Page } from "@playwright/test";

const APP_ID = "app_restore_fixture";

const candidate = {
  profile: {
    firstName: "Casey",
    lastName: "Fixture",
    email: "casey@example.test",
    phone: "",
    location: "",
    linkedinUrl: "",
    websiteUrl: "",
  },
  resumes: [{ id: "r1", fileName: "Casey.pdf", sizeBytes: 1000, uploadedAt: "2026-09-01T00:00:00Z" }],
  defaultResumeId: "r1",
};

const submittingView = {
  id: APP_ID,
  state: "SUBMITTING",
  applicationUrl: "https://jobs.example.test/northwind/apply",
  job: { title: "Senior Lifecycle Marketer", company: "Northwind Cartography", ats: "Greenhouse" },
  requestedAt: "2026-09-22T20:00:00Z",
  updatedAt: "2026-09-22T20:01:00Z",
  progress: { page: 2, pageCount: 2 },
  resumeFileName: "Casey.pdf",
  needs: null,
  receipt: null,
  prior: null,
  failure: null,
  uncertain: null,
  events: [
    {
      id: "e1",
      type: "application.submitting",
      at: "2026-09-22T20:01:00Z",
      message: "Recorded the attempt, then pressed Submit.",
      tone: "progress",
    },
  ],
};

/** Seed the saved application id once, as if an earlier page load had started it. */
async function seedActiveId(page: Page) {
  await page.addInitScript((id) => {
    if (!sessionStorage.getItem("imx.test.seeded")) {
      sessionStorage.setItem("imx.test.seeded", "1");
      sessionStorage.setItem("imx.activeApplicationId", id);
    }
  }, APP_ID);
}

const savedId = (page: Page) => page.evaluate(() => sessionStorage.getItem("imx.activeApplicationId"));

test.describe("restoring the application this page was following", () => {
  test("a transient status failure keeps the id and a later check restores it", async ({ page }) => {
    await seedActiveId(page);
    await page.route("**/api/imx/candidate", (route) => route.fulfill({ json: candidate }));
    let statusCalls = 0;
    const requestedIds: string[] = [];
    await page.route(`**/api/imx/applications/*`, (route) => {
      requestedIds.push(new URL(route.request().url()).pathname.split("/").pop()!);
      statusCalls += 1;
      if (statusCalls === 1) {
        return route.fulfill({
          status: 503,
          json: {
            error: { code: "unavailable", message: "The application service isn't running or can't be reached." },
          },
        });
      }
      return route.fulfill({ json: submittingView });
    });

    await page.goto("/");
    const notice = page.getByRole("alert").filter({ hasText: "Couldn’t reload the application you were following" });
    await expect(notice).toBeVisible();
    await expect(notice).toContainText(APP_ID);
    await expect(notice).toContainText("may still be in progress");
    await expect(page.getByText("Service not connected")).toBeVisible();
    expect(await savedId(page)).toBe(APP_ID);

    await notice.getByRole("button", { name: "Check again" }).click();
    await expect(page.locator("#case-title")).toHaveText("Submitting");
    await expect(page.getByText("Couldn’t reload the application")).toHaveCount(0);
    expect(await savedId(page)).toBe(APP_ID);
    expect(new Set(requestedIds)).toEqual(new Set([APP_ID]));
  });

  test("the id survives a reload after a failed restore", async ({ page }) => {
    await seedActiveId(page);
    await page.route("**/api/imx/candidate", (route) => route.fulfill({ json: candidate }));
    let fail = true;
    await page.route(`**/api/imx/applications/*`, (route) =>
      fail
        ? route.fulfill({ status: 502, json: { error: { code: "unavailable", message: "Bad gateway." } } })
        : route.fulfill({ json: submittingView }),
    );

    await page.goto("/");
    await expect(page.getByText("Couldn’t reload the application you were following")).toBeVisible();
    fail = false;
    await page.reload();
    await expect(page.locator("#case-title")).toHaveText("Submitting");
    expect(await savedId(page)).toBe(APP_ID);
  });

  test("a definitive not-found clears the id and says so", async ({ page }) => {
    await seedActiveId(page);
    await page.route("**/api/imx/candidate", (route) => route.fulfill({ json: candidate }));
    await page.route(`**/api/imx/applications/*`, (route) =>
      route.fulfill({ status: 404, json: { error: { code: "not_found", message: "No such application." } } }),
    );

    await page.goto("/");
    await expect(page.getByText("The earlier application is no longer on record")).toBeVisible();
    expect(await savedId(page)).toBeNull();
    await expect(page.getByText("Service connected")).toBeVisible();
  });

  test("an unreachable service keeps the id without trying to restore", async ({ page }) => {
    await seedActiveId(page);
    await page.goto("/");
    await expect(page.getByText("Couldn’t reload the application you were following")).toBeVisible();
    await expect(page.getByText("Service not connected")).toBeVisible();
    expect(await savedId(page)).toBe(APP_ID);
  });
});
