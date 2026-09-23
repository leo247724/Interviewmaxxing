import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Live-desk integration regressions against mocked service responses:
 * - a USER_CONFIRMED receipt that also lists older site artifacts stays user-reported;
 * - the "City and region" field reaches the service exactly as typed.
 */

const candidate = {
  profile: {
    firstName: "Casey",
    lastName: "Fixture",
    email: "casey@example.test",
    phone: "",
    location: "Austin, TX",
    linkedinUrl: "",
    websiteUrl: "",
  },
  resumes: [{ id: "r1", fileName: "Casey.pdf", sizeBytes: 1000, uploadedAt: "2026-09-01T00:00:00Z" }],
  defaultResumeId: "r1",
};

const siteShot = {
  kind: "screenshot",
  label: "Page after pressing Submit",
  value: null,
  href: "/preview-fixtures/unresponsive.svg",
  observedAt: "2026-09-22T20:00:00Z",
  source: "site",
};
const userNote = {
  kind: "user_report",
  label: "You reported a confirmation in a confirmation email",
  value: "Email from the employer",
  href: null,
  observedAt: "2026-09-22T20:05:00Z",
  source: "user",
};

function submitted(receipt: Record<string, unknown>) {
  return {
    id: "app_live_1",
    state: "SUBMITTED",
    applicationUrl: "https://jobs.example.test/fictional/apply",
    job: { title: "Marketing Manager", company: "Fictional Co", ats: "Lever" },
    requestedAt: "2026-09-22T19:59:00Z",
    updatedAt: "2026-09-22T20:05:00Z",
    progress: null,
    resumeFileName: "Casey.pdf",
    needs: null,
    receipt: { receiptId: "rcpt_live_1", submittedAt: "2026-09-22T20:05:00Z", confirmationReference: null, ...receipt },
    prior: null,
    failure: null,
    uncertain: null,
    events: [],
  };
}

async function startWith(page: Page, receipt: Record<string, unknown>) {
  const bodies: unknown[] = [];
  await page.route("**/api/imx/healthz", (route) => route.fulfill({ json: { status: "ok", executor: "idle", runner: "available", applicationMode: "TEST_ONLY" } }));
  await page.route("**/api/imx/candidate", (route) => route.fulfill({ json: candidate }));
  await page.route("**/api/imx/applications", (route: Route) => {
    bodies.push(route.request().postDataJSON());
    return route.fulfill({ status: 201, json: submitted(receipt) });
  });
  await page.goto("/");
  await expect(page.getByText("Service connected")).toBeVisible();
  await page.getByLabel("Application link").fill("http://127.0.0.1:4876/fictional/apply");
  await page.getByRole("button", { name: "Apply and submit" }).click();
  return bodies;
}

test("a user-confirmed receipt with older site screenshots stays labelled as user-reported", async ({ page }) => {
  await startWith(page, {
    evidence: [siteShot, userNote],
    confirmationMethod: "USER_CONFIRMED",
    confirmationAuthority: "user",
  });
  await expect(page.locator("#case-title")).toHaveText("Submitted, on your report");
  const receipt = page.getByRole("article", { name: "Submission receipt" });
  await expect(receipt.getByText("Marked submitted on your report")).toBeVisible();
  await expect(receipt.getByTestId("confirmation-method")).toContainText("You reported");
  await expect(receipt.getByText("Observed on the site")).toBeVisible();
  await expect(receipt).toContainText("None recorded.");
  await expect(page.getByTestId("state-stamp")).toContainText("Reported");
});

test("an older service without the new fields still can't pass a user report off as site-confirmed", async ({
  page,
}) => {
  await startWith(page, { evidence: [siteShot, userNote] });
  await expect(page.locator("#case-title")).toHaveText("Submitted, on your report");
  await expect(page.getByText("Marked submitted on your report")).toBeVisible();
});

test("a site-confirmed receipt shows how the site confirmed it", async ({ page }) => {
  await startWith(page, {
    evidence: [siteShot],
    confirmationMethod: "SUBMISSION_OBSERVED",
    confirmationAuthority: "site",
  });
  await expect(page.locator("#case-title")).toHaveText("Submitted and confirmed");
  await expect(page.getByTestId("confirmation-method")).toContainText("right after the application was submitted");
  await expect(page.getByText("Marked submitted on your report")).toHaveCount(0);
  await expect(page.getByTestId("state-stamp")).toContainText("Received");
});

test("City and region reaches the service exactly as typed", async ({ page }) => {
  const bodies = await startWith(page, {
    evidence: [siteShot],
    confirmationMethod: "SUBMISSION_OBSERVED",
    confirmationAuthority: "site",
  });
  await expect(page.locator("#case-title")).toBeVisible();
  expect(bodies).toHaveLength(1);
  expect((bodies[0] as { profile: { location: string } }).profile.location).toBe("Austin, TX");
});
