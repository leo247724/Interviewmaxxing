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

  test("stopping during a delayed restore keeps the stopped application from reappearing", async ({ page }) => {
    await seedActiveId(page);
    await page.route("**/api/imx/candidate", (route) => route.fulfill({ json: candidate }));
    let statusCalls = 0;
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/imx/applications/*", async (route) => {
      statusCalls += 1;
      if (statusCalls === 1) {
        return route.fulfill({ status: 503, json: { error: { code: "unavailable", message: "Not running." } } });
      }
      await held;
      return route.fulfill({ json: submittingView });
    });

    await page.goto("/");
    const notice = page.getByRole("alert").filter({ hasText: "Couldn’t reload the application you were following" });
    await expect(notice).toBeVisible();

    const heldResponse = page.waitForResponse((response) => response.url().endsWith(`/applications/${APP_ID}`));
    await notice.getByRole("button", { name: "Check again" }).click();
    await expect(notice.getByRole("button", { name: "Checking…" })).toBeVisible();
    await notice.getByRole("button", { name: "Stop following it on this page" }).click();
    expect(await savedId(page)).toBeNull();
    await expect(page.getByLabel("Application link")).toBeVisible();

    release();
    await heldResponse;
    // Let the resolved restore settle; it must be discarded.
    await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 300)));
    await expect(page.locator("#case-title")).toHaveCount(0);
    await expect(page.getByText("Couldn’t reload the application")).toHaveCount(0);
    await expect(page.getByLabel("Application link")).toBeVisible();
    expect(await savedId(page)).toBeNull();
  });

  test("a delayed restore never replaces a newly started application", async ({ page }) => {
    await seedActiveId(page);
    await page.route("**/api/imx/healthz", (route) => route.fulfill({ json: { status: "ok", executor: "idle", runner: "available", applicationMode: "TEST_ONLY" } }));
    await page.route("**/api/imx/candidate", (route) => route.fulfill({ json: candidate }));
    const newView = {
      ...submittingView,
      id: "app_new_fixture",
      state: "INSPECTING",
      applicationUrl: "http://127.0.0.1:4876/halcyon/apply",
      job: { title: "Growth Operations Lead", company: "Halcyon Freight", ats: "Workday" },
      events: [],
    };
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/imx/applications", (route) => route.fulfill({ status: 202, json: newView }));
    await page.route("**/api/imx/applications/*", async (route) => {
      if (route.request().url().endsWith(`/applications/${APP_ID}`)) {
        await held;
        return route.fulfill({ json: submittingView });
      }
      return route.fulfill({ json: newView });
    });

    const heldResponse = page.waitForResponse((response) => response.url().endsWith(`/applications/${APP_ID}`));
    await page.goto("/");
    await expect(page.getByText("Service connected")).toBeVisible();
    await page.getByLabel("Application link").fill(newView.applicationUrl);
    await page.getByRole("button", { name: "Apply and submit" }).click();
    await expect(page.locator(".case__company")).toHaveText("Halcyon Freight");
    expect(await savedId(page)).toBe("app_new_fixture");

    release();
    await heldResponse;
    await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 300)));
    await expect(page.locator(".case__company")).toHaveText("Halcyon Freight");
    await expect(page.locator(".case__id")).toHaveText("app_new_fixture");
    expect(await savedId(page)).toBe("app_new_fixture");
  });
});
