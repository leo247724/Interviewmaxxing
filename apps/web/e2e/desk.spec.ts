import { expect, test, type Page } from "@playwright/test";

const JOB_URL = "https://jobs.example.test/northwind/senior-lifecycle-marketer";

async function openPreview(page: Page, scenario: string) {
  await page.goto(`/preview?scenario=${scenario}`);
  await expect(page.getByLabel("First name")).toHaveValue("Robin");
}

async function applyTo(page: Page, url = JOB_URL) {
  await page.getByLabel("Application link").fill(url);
  await page.getByRole("button", { name: "Apply and submit" }).click();
}

const heading = (page: Page) => page.locator("#case-title");

test.describe("live desk without a backend", () => {
  test("reports the service as unavailable and never shows a submission", async ({ page }) => {
    const requests: string[] = [];
    page.on("request", (request) => requests.push(request.url()));

    await page.goto("/");
    await expect(page.getByRole("heading", { name: /application service isn.t connected/ })).toBeVisible();
    await expect(page.getByText("Service not connected")).toBeVisible();

    await page.getByLabel("First name").fill("Casey");
    await page.getByLabel("Last name").fill("Fixture");
    await page.getByLabel("Email").fill("casey.fixture@example.test");
    await page.getByLabel("Application link").fill(JOB_URL);
    await page.getByRole("button", { name: "Apply and submit" }).click();
    // No resume can be uploaded or chosen without the service, so that is reported truthfully.
    await expect(page.locator("#resume-choices-error")).toContainText("Choose a saved resume or upload one");

    await page.locator("#resume-file").setInputFiles({
      name: "Casey_Fixture.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-1.4 fictional"),
    });
    await expect(page.locator("#resume-file-error")).toContainText("The resume wasn't uploaded");
    await expect(page.getByText(/Submitted/)).toHaveCount(0);

    // Personal data never travels in a URL.
    expect(requests.some((url) => url.includes("casey") || url.includes("Casey"))).toBe(false);
    expect(new URL(page.url()).search).toBe("");
  });

  test("the apply action itself reports the outage", async ({ page }) => {
    await page.route("**/api/imx/candidate", (route) =>
      route.fulfill({
        json: {
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
        },
      }),
    );
    await page.goto("/");
    await expect(page.getByText("Service connected")).toBeVisible();
    await applyTo(page);
    await expect(page.getByRole("alert").filter({ hasText: "Couldn't start the application" })).toBeVisible();
    await expect(page.getByText("Service not connected")).toBeVisible();
    await expect(heading(page)).toHaveCount(0);
  });
});

test.describe("preview desk", () => {
  test("is clearly labelled as fictional", async ({ page }) => {
    await openPreview(page, "straight");
    await expect(page.getByRole("region", { name: "Preview controls" })).toContainText("nothing is submitted");
    await expect(page).toHaveTitle(/Preview/);
  });

  test("validates the URL and profile, focusing a linked summary", async ({ page }) => {
    await openPreview(page, "straight");
    await page.getByLabel("Email").fill("");
    await page.getByLabel("First name").fill("");
    await page.getByLabel("Application link").fill("jobs.example.test/apply");
    await page.getByLabel("Website or portfolio").fill("robinvale.example");
    await page.getByRole("button", { name: "Apply and submit" }).click();

    const summary = page.locator(".error-summary");
    await expect(summary).toBeFocused();
    await expect(summary.getByRole("link")).toHaveCount(4);
    await expect(page.getByLabel("Application link")).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByLabel("Email")).toHaveAttribute("aria-invalid", "true");

    await summary.getByRole("link", { name: /email address/ }).click();
    await expect(page.getByLabel("Email")).toBeFocused();

    await page.getByLabel("Email").fill("robin.vale@example.test");
    await page.getByLabel("First name").fill("Robin");
    await page.getByLabel("Website or portfolio").fill("");
    await applyTo(page);
    await expect(heading(page)).toBeVisible();
  });

  test("uploads a resume and rejects unsupported files", async ({ page }) => {
    await openPreview(page, "straight");
    await page
      .locator("#resume-file")
      .setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("x") });
    await expect(page.locator("#resume-file-error")).toContainText("PDF or Word");
    await page.locator("#resume-file").setInputFiles({
      name: "Robin_Vale_New.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-1.4 fictional"),
    });
    await expect(page.getByRole("radio", { name: /Robin_Vale_New\.pdf/ })).toBeChecked();
    await expect(page.locator("#resume-file-error")).toHaveCount(0);
  });

  test("shows live status updates, then a confirmed receipt with evidence", async ({ page }) => {
    await openPreview(page, "straight");
    await applyTo(page);
    await expect(heading(page)).toHaveText(
      /Reading the application form|Opening the application page|Request recorded/,
    );
    await expect(page.locator(".docket")).toContainText("Identified Senior Lifecycle Marketer");
    await expect(heading(page)).toHaveText(/Filling page/, { timeout: 15_000 });
    await expect(page.locator(".rail__step.is-current")).toContainText("Filling");
    await expect(heading(page)).toHaveText("Submitted and confirmed", { timeout: 15_000 });

    const receipt = page.getByRole("article", { name: "Submission receipt" });
    await expect(receipt).toContainText("Senior Lifecycle Marketer");
    await expect(receipt).toContainText("Northwind Cartography");
    await expect(receipt.getByRole("link", { name: JOB_URL })).toBeVisible();
    await expect(receipt.locator("time")).toHaveAttribute("datetime", /^\d{4}-\d{2}-\d{2}T/);
    await expect(receipt).toContainText("NWC-24-0922-7731");
    await expect(receipt.getByRole("img", { name: "Confirmation page screenshot" })).toBeVisible();
    await expect(receipt).toContainText("Preview receipt — fictional, nothing was sent.");
    await expect(page.locator(".docket")).toContainText("Recorded the attempt, then pressed Submit.");
    await expect(heading(page)).toBeFocused();

    await receipt.getByRole("button", { name: "Start another application" }).click();
    await expect(page.getByLabel("Application link")).toHaveValue("");
    await expect(page.getByLabel("Email")).toHaveValue("robin.vale@example.test");
  });

  test("required questions use the site's options, validate, save and resume", async ({ page }) => {
    await openPreview(page, "questions");
    await applyTo(page);
    await expect(heading(page)).toHaveText("6 answers and 1 statement needed from you", { timeout: 15_000 });

    const source = page.getByRole("group", { name: /How did you hear/ });
    await expect(source.getByRole("radio")).toHaveCount(5);
    await expect(source.getByLabel("Employee referral")).toBeVisible();
    await expect(page.getByRole("group", { name: /gender/ }).getByLabel("I don't wish to answer")).not.toBeChecked();
    await expect(page.getByLabel(/I certify/)).not.toBeChecked();
    await expect(page.getByLabel(/keep my application on file/)).not.toBeChecked();

    await page.getByRole("button", { name: "Save answers and continue" }).click();
    await expect(page.locator(".error-summary")).toBeFocused();
    await expect(page.locator(".error-summary li")).toHaveCount(7);
    await expect(page.locator("#a_truthful-error")).toContainText("Check it only if it is true");

    await page
      .getByRole("group", { name: /legally authorized/ })
      .getByLabel("Yes")
      .check();
    await page
      .getByRole("group", { name: /sponsorship/ })
      .getByLabel("No")
      .check();
    await page.getByLabel(/base salary/).fill("lots");
    await source.getByLabel("LinkedIn").check();
    await page.getByLabel(/Why are you interested/).fill("Maps are how people understand places.");
    await page
      .getByRole("group", { name: /lifecycle platforms/ })
      .getByLabel("Braze")
      .check();
    await page
      .getByRole("group", { name: /gender/ })
      .getByLabel("I don't wish to answer")
      .check();
    await page.getByLabel(/I certify/).check();

    await page.getByRole("button", { name: "Save answers and continue" }).click();
    await expect(page.locator("#q_salary-error")).toContainText("whole number in US dollars");
    await expect(heading(page)).toHaveText("6 answers and 1 statement needed from you");

    await page.getByLabel(/base salary/).fill("145000");
    await page.getByRole("button", { name: "Save for later" }).click();
    await expect(page.getByText(/Saved at .* stays paused/)).toBeVisible();
    await expect(heading(page)).toHaveText("6 answers and 1 statement needed from you");

    await page.getByRole("button", { name: "Save answers and continue" }).click();
    await expect(heading(page)).toHaveText("Submitted and confirmed", { timeout: 15_000 });
    await expect(page.locator(".docket")).toContainText("Answers accepted");
  });

  test("an unconfirmed submission offers reconciliation, not a retry", async ({ page }) => {
    await openPreview(page, "uncertain");
    await applyTo(page);
    await expect(heading(page)).toHaveText("Submission not confirmed", { timeout: 15_000 });
    await expect(page.getByRole("button", { name: /retry|try again/i })).toHaveCount(0);
    await expect(page.getByText("There’s no retry button here on purpose.")).toBeVisible();

    await page.getByRole("button", { name: "Check the site again" }).click();
    await expect(page.getByText(/Last check/)).toBeVisible();
    await expect(heading(page)).toHaveText("Submission not confirmed");

    await page.getByRole("button", { name: "Check the site again" }).click();
    await expect(heading(page)).toHaveText("Submitted and confirmed");
    const receipt = page.getByRole("article", { name: "Submission receipt" });
    await expect(receipt).toContainText("None shown. The evidence below is the confirmation.");
    await expect(receipt).toContainText("Applicant portal record");
  });

  test("a user-reported confirmation is recorded as such", async ({ page }) => {
    await openPreview(page, "uncertain");
    await applyTo(page);
    await expect(heading(page)).toHaveText("Submission not confirmed", { timeout: 15_000 });
    await page.getByRole("button", { name: "I found a confirmation" }).click();
    await page.getByRole("button", { name: "Record confirmation" }).click();
    await expect(page.locator("#foundIn-error")).toBeVisible();
    await page.getByLabel("Confirmation email").check();
    await page.getByLabel("Confirmation reference").fill("JV-2026-44");
    await page.getByRole("button", { name: "Record confirmation" }).click();
    await expect(heading(page)).toHaveText("Submitted, on your report");
    const receipt = page.getByRole("article", { name: "Submission receipt" });
    await expect(receipt.getByText("Marked submitted on your report")).toBeVisible();
    await expect(receipt.getByTestId("confirmation-method")).toContainText("You reported");
    // The earlier site screenshot stays listed, but doesn't turn this into a site confirmation.
    await expect(receipt.getByText("Observed on the site")).toBeVisible();
    await expect(receipt.getByText("Reported by you")).toBeVisible();
    await expect(page.getByTestId("state-stamp")).toContainText("Reported");
    await expect(page.getByText("JV-2026-44")).toBeVisible();
  });

  test("not-received requires an explicit check before applying again is unlocked", async ({ page }) => {
    await openPreview(page, "uncertain");
    await applyTo(page);
    await expect(heading(page)).toHaveText("Submission not confirmed", { timeout: 15_000 });
    await page.getByRole("button", { name: "The employer has no record of it" }).click();
    await page.getByRole("button", { name: "Record as not received" }).click();
    await expect(page.locator("#not-received-confirm-error")).toBeVisible();
    await expect(heading(page)).toHaveText("Submission not confirmed");

    await page.getByLabel(/this application was not received/).check();
    await page.getByRole("button", { name: "Record as not received" }).click();
    await expect(heading(page)).toHaveText("Stopped before submitting");
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(heading(page)).toHaveText("Submitted and confirmed", { timeout: 15_000 });
  });

  for (const [scenario, title, button] of [
    ["sign_in", "Sign in to continue", "I've signed in — continue"],
    ["captcha", "Complete the CAPTCHA to continue", "I've completed it — continue"],
  ] as const) {
    test(`${scenario} waits for the user, then continues`, async ({ page }) => {
      await openPreview(page, scenario);
      await applyTo(page);
      await expect(heading(page)).toHaveText(title, { timeout: 15_000 });
      await expect(page.getByText("Page in the browser window")).toBeVisible();
      await page.getByRole("button", { name: button }).click();
      await expect(heading(page)).toHaveText("Submitted and confirmed", { timeout: 15_000 });
    });
  }

  test("a prior submission is shown and nothing is sent", async ({ page }) => {
    await openPreview(page, "duplicate");
    await applyTo(page);
    await expect(heading(page)).toHaveText("You've already applied to this job", { timeout: 15_000 });
    await expect(page.getByText("NWC-24-0903-1188")).toBeVisible();
    await expect(page.getByText(/Nothing was sent this time/)).toBeVisible();
    await expect(page.locator(".docket")).not.toContainText("pressed Submit");
  });

  test("a closed posting is a permanent failure without retry", async ({ page }) => {
    await openPreview(page, "failure_permanent");
    await applyTo(page);
    await expect(heading(page)).toHaveText("This application can't be completed", { timeout: 15_000 });
    await expect(page.getByText("This posting is closed.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Start a different application" })).toBeVisible();
  });

  test("a retryable site error can be tried again", async ({ page }) => {
    await openPreview(page, "failure_retryable");
    await applyTo(page);
    await expect(heading(page)).toHaveText("Stopped before submitting", { timeout: 15_000 });
    await expect(page.getByText(/Nothing was submitted/).first()).toBeVisible();
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(heading(page)).toHaveText("Submitted and confirmed", { timeout: 15_000 });
  });

  test("losing contact while submitting warns against starting again, then recovers", async ({ page }) => {
    await openPreview(page, "connection_drop");
    await applyTo(page);
    await expect(page.getByText("Lost contact with the application service")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/may still be submitting. Don't start it again/)).toBeVisible();
    await expect(heading(page)).toHaveText("Submitting");
    await page.getByRole("button", { name: "Check now" }).click();
    await expect(heading(page)).toHaveText("Submitted and confirmed", { timeout: 20_000 });
  });

  test("an unreachable service is reported, never simulated", async ({ page }) => {
    await page.goto("/preview?scenario=unavailable");
    await expect(page.getByRole("heading", { name: /application service isn.t connected/ })).toBeVisible();
    await page.getByLabel("First name").fill("Robin");
    await page.getByLabel("Last name").fill("Vale");
    await page.getByLabel("Email").fill("robin.vale@example.test");
    await applyTo(page);
    await expect(page.locator("#resume-choices-error")).toBeVisible();
    await expect(heading(page)).toHaveCount(0);
  });
});

test.describe("keyboard and motion", () => {
  test("the whole happy path works from the keyboard", async ({ page, isMobile }) => {
    test.skip(isMobile, "keyboard path is a desktop check");
    await openPreview(page, "straight");
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "Skip to the application" })).toBeFocused();
    await page.getByLabel("Application link").focus();
    await page.keyboard.type(JOB_URL);
    await page.keyboard.press("Enter");
    await expect(heading(page)).toHaveText("Submitted and confirmed", { timeout: 15_000 });
    await expect(heading(page)).toBeFocused();
    await page.keyboard.press("Tab");
    const focused = await page.evaluate(() => document.activeElement?.tagName);
    expect(["A", "BUTTON"]).toContain(focused);
  });

  test("reduced motion turns off animation", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openPreview(page, "straight");
    await applyTo(page);
    await expect(heading(page)).toHaveText("Submitted and confirmed", { timeout: 15_000 });
    const animation = await page.getByTestId("state-stamp").evaluate((el) => getComputedStyle(el).animationName);
    expect(animation).toBe("none");
  });
});
