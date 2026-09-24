import { expect, test, type Page } from "@playwright/test";

const JOB_URL = "https://jobs.example.test/northwind/senior-lifecycle-marketer";
const PREPARED = "Prepared for your review — nothing submitted";

async function openPreview(page: Page, scenario: string) {
  await page.goto(`/preview?scenario=${scenario}`);
  await expect(page.getByLabel("First name")).toHaveValue("Robin");
}

async function applyTo(page: Page, url = JOB_URL) {
  await page.getByLabel("Application link").fill(url);
  await page.getByRole("button", { name: "Apply and submit" }).click();
}

const heading = (page: Page) => page.locator("#case-title");

async function expectNothingSubmitted(page: Page) {
  await expect(page.getByText(/Submitted and confirmed/)).toHaveCount(0);
  await expect(page.getByRole("article", { name: "Submission receipt" })).toHaveCount(0);
  await expect(page.locator(".docket")).not.toContainText("pressed Submit");
}

test.describe("prepared applications", () => {
  test("a location lookup offers the site's suggestions, then the form is prepared for review", async ({ page }) => {
    await openPreview(page, "lookup");
    await applyTo(page);
    await expect(heading(page)).toHaveText("1 answer needed from you", { timeout: 15_000 });

    const location = page.getByRole("combobox", { name: /Location \(city\)/ });
    await expect(location.locator("option")).toHaveText([
      "Choose…",
      "Portland, OR, USA",
      "Portland, ME, USA",
      "Portland, TX, USA",
      "Enter a different value…",
    ]);
    await expect(page.getByText(/These are the site's suggestions for what was typed/)).toBeVisible();
    await expect(page.getByLabel(/Different value for the site/)).toHaveCount(0);

    await location.selectOption({ label: "Enter a different value…" });
    const different = page.getByLabel(/Different value for the site/);
    await expect(different).toBeVisible();
    await expect(different).toHaveValue("");

    // A blank different value is caught before anything is sent, and the summary leads to its box.
    await page.getByRole("button", { name: "Save answers and continue" }).click();
    await expect(page.locator("#q_location-error")).toContainText("search box");
    await page.locator(".error-summary").getByRole("link").click();
    await expect(different).toBeFocused();

    await different.fill("Beaverton, OR, USA");
    await page.getByRole("button", { name: "Save answers and continue" }).click();

    await expect(heading(page)).toHaveText(PREPARED, { timeout: 15_000 });
    await expect(heading(page)).toBeFocused();
    await expect(page.locator("form.questions")).toHaveCount(0);
    await expect(page.getByTestId("state-stamp")).toContainText("Prepared");
    await expect(page.getByTestId("captcha-note")).toBeVisible();
    await expect(page.getByTestId("captcha-note")).toContainText(
      "solved in the browser before this application can be submitted",
    );
    await expect(page.getByRole("img", { name: "Final review page screenshot" })).toBeVisible();
    const review = page.getByRole("region", { name: "What the desk entered" });
    await expect(review).toContainText("Beaverton, OR, USA");
    await expect(review).toContainText("Your answer");
    await expect(page.locator(".docket")).toContainText("Paused at the final review step for you to check.");
    await expectNothingSubmitted(page);
  });

  test("a prepared application is reviewed page by page and can be prepared again", async ({ page }) => {
    await openPreview(page, "prepared");
    await applyTo(page);
    await expect(heading(page)).toHaveText(PREPARED, { timeout: 15_000 });
    await expect(page.getByTestId("captcha-note")).toHaveCount(0);
    await expect(page.getByRole("img", { name: "Final review page screenshot" })).toBeVisible();
    await expect(page.locator(".rail__step.is-done")).toHaveCount(3);

    const review = page.getByRole("region", { name: "What the desk entered" });
    await expect(review.getByRole("heading", { name: "Page 1" })).toBeVisible();
    await expect(review.getByRole("heading", { name: "Page 2" })).toBeVisible();
    await expect(review).toContainText("Drafted from your facts");
    await expect(review).toContainText("86% confident");
    await expect(review.getByText("Check this", { exact: true })).toBeVisible();
    await expect(review.getByText("Wording not recorded, see the screenshot")).toBeVisible();
    await expect(page.locator(".panel__where")).toContainText(
      "https://jobs.example.test/northwind/senior-lifecycle-marketer/apply/review",
    );

    await page.getByRole("button", { name: "Prepare again" }).click();
    await expect(page.locator(".docket")).toContainText("Preparing again");
    await expect(page.locator(".timeline__message", { hasText: "Ready for final review" })).toHaveCount(2, {
      timeout: 15_000,
    });
    await expect(heading(page)).toHaveText(PREPARED);
    await expectNothingSubmitted(page);
  });
});
