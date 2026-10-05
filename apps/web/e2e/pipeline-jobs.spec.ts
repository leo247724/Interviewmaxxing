import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

async function openBoard(page: Page) {
  await page.goto("/preview/pipeline");
  await expect(page.getByRole("heading", { name: /^Saved/ })).toBeVisible();
}

const card = (page: Page, company: string) => page.locator("article.card", { hasText: company });

test.describe("pipeline preview", () => {
  test("shows readable cards and keeps tracking, Jev and receipts distinct", async ({ page }) => {
    await openBoard(page);
    const larkspur = card(page, "Larkspur Health");
    await expect(larkspur).toContainText("Director, Growth Marketing");
    await expect(larkspur).toContainText("Hiring manager interview (45 min, video) — completed");
    await expect(larkspur).toContainText("$165,000–$190,000");
    await expect(larkspur).toContainText("8/10");
    await expect(larkspur.locator(".mark--receipt")).toHaveCount(0);

    const juniper = card(page, "Juniper & Vale Studio");
    await expect(juniper.locator(".mark--receipt")).toContainText("Receipt confirmed");
    await expect(juniper.locator(".mark--jev")).toHaveText("Jev: APPLY");
    await expect(juniper.getByRole("button", { name: /^Apply/ })).toHaveCount(0);

    await expect(card(page, "Halcyon Freight").locator(".mark--jev")).toHaveText("Jev: APPLY");
    await expect(card(page, "Halcyon Freight").locator(".mark--receipt")).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Pipeline", exact: true })).toHaveAttribute("aria-current", "page");
  });

  test("moves a card with the keyboard and records it in history", async ({ page }) => {
    await openBoard(page);
    const larkspur = card(page, "Larkspur Health");
    await larkspur.getByLabel("Move Larkspur Health to lane").focus();
    await larkspur.getByLabel("Move Larkspur Health to lane").selectOption("decision");
    await larkspur.getByRole("button", { name: "Move" }).press("Enter");
    const decision = page.locator("section.lane", { has: page.getByRole("heading", { name: /^Awaiting Decision/ }) });
    await expect(decision.locator("article.card", { hasText: "Larkspur Health" })).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: "Moved Larkspur Health to Awaiting Decision." })).toBeAttached();

    await card(page, "Larkspur Health").getByRole("button", { name: "Open Larkspur Health" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByText(/^History/).click();
    await expect(dialog.locator(".history")).toContainText("Moved from 1st round interview to Awaiting Decision");
  });

  test("edits all reference fields with validation, then saves", async ({ page }) => {
    await openBoard(page);
    await card(page, "Tessellate Analytics").getByRole("button", { name: "Open Tessellate Analytics" }).click();
    const dialog = page.getByRole("dialog", { name: /Tessellate Analytics/ });
    await expect(dialog).toBeVisible();
    for (const label of [
      "Company",
      "Role",
      "Stage",
      "Status",
      "Priority",
      "Fit / 10",
      "Next interview date",
      "Time (CT)",
      "Interview format",
      "Work arrangement",
      "Location / commute",
      "Comp low (USD/year)",
      "Comp high (USD/year)",
      "Comp basis",
      "Target assessment",
      "Source / recruiter",
      "Follow-up date (suggested)",
      "Next action",
      "Last interview date",
      "Decision due",
      "Comp / benefits notes",
      "Fit rationale",
      "Process / source notes",
    ]) {
      await expect(dialog.getByLabel(label, { exact: true })).toBeVisible();
    }
    await expect(dialog.getByLabel("Stage", { exact: true })).toHaveValue("Take-home case study");
    await expect(dialog.getByLabel("Comp high (USD/year)", { exact: true })).toHaveValue("");

    await dialog.getByLabel("Fit / 10", { exact: true }).fill("14");
    await dialog.getByLabel("Comp high (USD/year)", { exact: true }).fill("150000");
    await dialog.getByLabel("Application link").fill("tessellate.example.test");
    await dialog.getByRole("button", { name: "Save changes" }).click();
    await expect(dialog.locator(".error-summary")).toBeFocused();
    await expect(dialog.locator(".error-summary li")).toHaveCount(3);

    await dialog.getByLabel("Fit / 10", { exact: true }).fill("9");
    await dialog.getByLabel("Comp high (USD/year)", { exact: true }).fill("195000");
    await dialog.getByLabel("Application link").fill("");
    await dialog.getByLabel("Next interview date", { exact: true }).fill("2026-10-01");
    await dialog.getByLabel("Time (CT)", { exact: true }).fill("2:30 PM");
    await dialog.getByRole("button", { name: "Save changes" }).click();
    await expect(dialog).toBeHidden();
    const tessellate = card(page, "Tessellate Analytics");
    await expect(tessellate).toContainText("$175,000–$195,000");
    await expect(tessellate).toContainText("2:30 PM CT");
  });



  test("asks for an application link before applying, then only prefills the desk", async ({ page }) => {
    await openBoard(page);
    await card(page, "Quarry & Pine Outfitters")
      .getByRole("button", { name: /^Apply/ })
      .click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("no application link yet");
    await dialog.getByRole("button", { name: "Continue to the desk" }).click();
    await expect(dialog.locator("#apply-url-error")).toBeVisible();
    await dialog.getByLabel(/Application link/).fill("https://quarrypine.example.test/careers/lifecycle/apply");
    await dialog.getByRole("button", { name: "Continue to the desk" }).click();

    await expect(page).toHaveURL(/\/preview$/);
    await expect(page.getByRole("heading", { name: /From your pipeline: Quarry & Pine Outfitters/ })).toBeVisible();
    await expect(page.getByLabel("Application link")).toHaveValue(
      "https://quarrypine.example.test/careers/lifecycle/apply",
    );
    await expect(page.locator("#case-title")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Apply and submit" })).toBeEnabled();

    await page.getByRole("link", { name: "Pipeline", exact: true }).click();
    await card(page, "Quarry & Pine Outfitters").getByRole("button", { name: "Open Quarry & Pine Outfitters" }).click();
    await expect(page.getByRole("dialog").getByLabel("Application link")).toHaveValue(
      "https://quarrypine.example.test/careers/lifecycle/apply",
    );
  });

  test("offers a list layout that works on small screens", async ({ page }) => {
    await openBoard(page);
    await page.getByRole("radio", { name: "List" }).check();
    const table = page.locator("table.pipeline-table");
    await expect(table.locator("tbody tr")).toHaveCount(8);
    await expect(table).toContainText("Larkspur Health");
    await page.getByLabel("Filter by company, role, stage or status").fill("brightwater");
    await expect(table.locator("tbody tr")).toHaveCount(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
});

test.describe("live pipeline without a backend", () => {
  test("say the service isn't available instead of showing made-up data", async ({ page }) => {
    await page.goto("/pipeline");
    await expect(page.getByRole("heading", { name: "The pipeline couldn't be loaded" })).toBeVisible();
    // The connection badge is hidden on the pipeline page by design (2026-09-30); the error state alone says the service is down.
    await expect(page.locator("article.card")).toHaveCount(0);
  });

  test("report missing service routes distinctly", async ({ page }) => {
    await page.route("**/api/imx/pipeline", (route) =>
      route.fulfill({ status: 404, json: { error: { code: "not_found", message: "No such route." } } }),
    );
    await page.goto("/pipeline");
    await expect(
      page.getByRole("heading", { name: "The pipeline isn't available from the service yet" }),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "See the pipeline with fictional data" })).toHaveAttribute(
      "href",
      "/preview/pipeline",
    );
  });

  test("a stale move reloads the board instead of overwriting", async ({ page }) => {
    const entry = {
      id: "pipe_live_1",
      lane: "saved",
      revision: 3,
      fields: {
        company: "Fictional Live Co",
        role: "Marketing Manager",
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
      },
      applicationUrl: null,
      listingId: null,
      origin: "manual",
      application: null,
      selection: null,
      provenance: null,
      history: [],
      createdAt: "2026-09-22T12:00:00Z",
      updatedAt: "2026-09-22T12:00:00Z",
    };
    let boardCalls = 0;
    await page.route("**/api/imx/pipeline", (route) => {
      boardCalls += 1;
      const current = boardCalls === 1 ? entry : { ...entry, revision: 4, lane: "applied" };
      return route.fulfill({
        json: {
          lanes: [
            { id: "saved", label: "Saved" },
            { id: "applied", label: "Applied" },
            { id: "closed", label: "Closed" },
          ],
          entries: [current],
        },
      });
    });
    let moveBody: unknown = null;
    await page.route("**/api/imx/pipeline/entries/pipe_live_1/move", (route) => {
      moveBody = route.request().postDataJSON();
      return route.fulfill({
        status: 409,
        json: { error: { code: "conflict", message: "This card changed since you opened it." } },
      });
    });
    await page.goto("/pipeline");
    const live = card(page, "Fictional Live Co");
    await live.getByLabel("Move Fictional Live Co to lane").selectOption("closed");
    await live.getByRole("button", { name: "Move" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "this move wasn't saved" })).toBeVisible();
    expect(moveBody).toEqual({ revision: 3, lane: "closed" });
    const applied = page.locator("section.lane", { has: page.getByRole("heading", { name: /^Applied/ }) });
    await expect(applied.locator("article.card", { hasText: "Fictional Live Co" })).toBeVisible();
  });

  test("summary buttons, backend and apply chips narrow the board and are remembered", async ({ page }) => {
    await openBoard(page);
    const totals = page.getByRole("group", { name: "Pipeline totals" }).getByRole("button");
    await expect(totals).toHaveText([
      /8\s*Total Jobs Found/,
      /6\s*Total Jobs Applied/,
      /4\s*Total Jobs Currently Interested/,
      /3\s*Total Jobs Currently Interviewing/,
      /1\s*Total Jobs Waiting for Offer/,
    ]);
    const found = totals.nth(0);
    const applied = totals.nth(1);
    const interviewing = totals.nth(3);
    await expect(found).toHaveAttribute("aria-pressed", "true");
    await expect(applied).toHaveAttribute("title", /^All-time counter/);
    await expect(interviewing).toHaveAttribute("title", /^Reflects the current lanes/);

    // Total Jobs Applied counts every card that reached Applied or a later lane.
    await applied.click();
    await expect(applied).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("article.card")).toHaveCount(6);
    await expect(page.getByRole("heading", { name: /^Saved/ })).toHaveCount(0);
    await expect(page.getByRole("status").filter({ hasText: "Showing 6 of 8 cards" })).toBeVisible();
    await applied.click();
    await expect(found).toHaveAttribute("aria-pressed", "true");

    await interviewing.click();
    await expect(interviewing).toHaveAttribute("aria-pressed", "true");
    await expect(found).toHaveAttribute("aria-pressed", "false");
    await expect(page.locator("article.card")).toHaveCount(3);
    await expect(page.getByRole("heading", { name: /^Saved/ })).toHaveCount(0);
    await expect(page.getByRole("status").filter({ hasText: "Showing 3 of 8 cards" })).toBeVisible();
    // Clicking the active button again returns to every job.
    await interviewing.click();
    await expect(found).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("article.card")).toHaveCount(8);

    // Saved cards carry the backend and the apply line; the detail is a tooltip.
    const halcyon = card(page, "Halcyon Freight");
    await expect(halcyon.locator(".mark--backend")).toHaveText("Employer site");
    await expect(halcyon.locator(".auto-apply__line")).toHaveText("Held · Needs your facts");
    await expect(halcyon.locator(".auto-apply__status")).toHaveAttribute("title", /notice period/);
    await expect(card(page, "Larkspur Health").locator(".mark--backend")).toHaveText("Greenhouse");
    await expect(card(page, "Larkspur Health").locator(".auto-apply__status")).toHaveCount(0);
    await expect(card(page, "Brightwater Credit Union").locator(".auto-apply")).toHaveCount(0);

    const filters = page.getByRole("region", { name: "Filter cards" });
    await filters.getByRole("button", { name: /^Greenhouse/ }).click();
    await expect(page.getByRole("status").filter({ hasText: "Showing 2 of 8 cards" })).toBeVisible();
    await filters.getByRole("button", { name: /^Unknown/ }).click();
    await expect(page.getByRole("status").filter({ hasText: "Showing 3 of 8 cards" })).toBeVisible();
    // Groups combine with AND: no Greenhouse or Unknown card is held.
    await filters.getByRole("button", { name: /^Held/ }).click();
    await expect(page.locator("article.card")).toHaveCount(0);

    await page.reload();
    await expect(filters.getByRole("button", { name: /^Held/ })).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("article.card")).toHaveCount(0);
    await filters.getByRole("button", { name: "Clear filters" }).click();
    await expect(page.locator("article.card")).toHaveCount(8);
    await expect(filters.getByRole("button", { name: /^Held/ })).toHaveAttribute("aria-pressed", "false");

    // Hide filters collapses the chip groups; the count and Clear filters stay while a chip is active.
    await filters.getByRole("button", { name: /^Greenhouse/ }).click();
    const toggle = filters.getByRole("button", { name: "Hide filters" });
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await toggle.click();
    const show = filters.getByRole("button", { name: "Show filters" });
    await expect(show).toHaveAttribute("aria-expanded", "false");
    await expect(filters.getByRole("button", { name: /^Greenhouse/ })).toBeHidden();
    await expect(page.getByRole("status").filter({ hasText: "Showing 2 of 8 cards" })).toBeVisible();
    await expect(filters.getByRole("button", { name: "Clear filters" })).toBeEnabled();
    // The hidden state is remembered with the chips.
    await page.reload();
    await expect(show).toBeVisible();
    await expect(page.locator("article.card")).toHaveCount(2);
    // With no chip active, the collapsed bar is just the toggle.
    await filters.getByRole("button", { name: "Clear filters" }).click();
    await expect(page.locator("article.card")).toHaveCount(8);
    await expect(filters.getByRole("button", { name: "Clear filters" })).toHaveCount(0);
    await show.click();
    await expect(filters.getByRole("button", { name: /^Greenhouse/ })).toBeVisible();
    await expect(filters.getByRole("button", { name: "Hide filters" })).toBeVisible();
  });
});
