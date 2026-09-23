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
    const decision = page.locator("section.lane", { has: page.getByRole("heading", { name: /^Decision/ }) });
    await expect(decision.locator("article.card", { hasText: "Larkspur Health" })).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: "Moved Larkspur Health to Decision." })).toBeAttached();

    await card(page, "Larkspur Health").getByRole("button", { name: "Open Larkspur Health" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByText(/^History/).click();
    await expect(dialog.locator(".history")).toContainText("Moved from Interviewing to Decision");
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

  test("tracks a new job by hand", async ({ page }) => {
    await openBoard(page);
    await page.getByRole("button", { name: "Track a job" }).click();
    const dialog = page.getByRole("dialog", { name: "Track a job" });
    await dialog.getByRole("button", { name: "Add to pipeline" }).click();
    await expect(dialog.locator(".error-summary")).toContainText("company or a role");
    await dialog.getByLabel("Company", { exact: true }).fill("Fictional Ember Co");
    await dialog.getByLabel("Role", { exact: true }).fill("Marketing Manager");
    await dialog.getByLabel("Lane").selectOption("applied");
    await dialog.getByRole("button", { name: "Add to pipeline" }).click();
    await expect(dialog).toBeHidden();
    const applied = page.locator("section.lane", { has: page.getByRole("heading", { name: /^Applied/ }) });
    await expect(applied.locator("article.card", { hasText: "Fictional Ember Co" })).toContainText("Added by you");
  });

  test("imports with a preview, blocks bad files and never duplicates on reimport", async ({ page }, info) => {
    const good = join(tmpdir(), `imx-good-${info.project.name}.csv`);
    const bad = join(tmpdir(), `imx-bad-${info.project.name}.csv`);
    writeFileSync(
      good,
      'Company,Role,Stage,Status,Fit / 10,Comp low (USD/year)\nFictional Orchard,Brand Marketing Manager,Recruiter screen,"Booked, awaiting time",6,110000\n',
    );
    writeFileSync(
      bad,
      "Company,Role,Fit / 10,Next interview date\nFictional Pebble,Marketing Director,11,2026-02-30\n",
    );

    await openBoard(page);
    await page.getByRole("button", { name: "Import" }).click();
    const dialog = page.getByRole("dialog", { name: "Import tracker rows" });
    await dialog.locator("#import-file").setInputFiles(bad);
    await expect(dialog.locator(".import__counts")).toContainText("1 with problems");
    await expect(dialog.locator(".import__errors")).toContainText("Fit / 10");
    await expect(dialog.locator(".import__errors")).toContainText("Next interview date");
    await expect(dialog.getByRole("button", { name: /^Import \d/ })).toHaveCount(0);

    await dialog.locator("#import-file").setInputFiles(good);
    await expect(dialog.locator(".import__counts")).toContainText("1 new");
    await dialog.getByRole("button", { name: "Import 1 row" }).click();
    await expect(dialog.locator(".import__receipt")).toContainText("1 new, 0 updated, 0 unchanged");

    await dialog.locator("#import-file").setInputFiles(good);
    await expect(dialog.locator(".import__counts")).toContainText("0 new · 0 updated · 1 unchanged");
    await dialog.getByRole("button", { name: "Record this import" }).click();
    await dialog.getByRole("button", { name: "Close" }).click();

    const scheduling = page.locator("section.lane", { has: page.getByRole("heading", { name: /^Scheduling/ }) });
    await expect(scheduling.locator("article.card", { hasText: "Fictional Orchard" })).toHaveCount(1);
    await expect(scheduling.locator("article.card", { hasText: "Fictional Orchard" })).toContainText(
      "Booked, awaiting time",
    );
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

test.describe("jobs preview", () => {
  test("starts from the user's defaults with nationwide remote", async ({ page }) => {
    await page.goto("/preview/jobs");
    await expect(page.getByRole("region", { name: "Current search preferences" })).toContainText("Performance marketing operator");
    await page.getByRole("button", { name: "Edit preferences" }).click();
    await expect(page.getByLabel("Job titles")).toHaveValue("paid media manager\nsenior paid media manager\nperformance marketing manager\ngrowth marketing manager\ndemand generation manager\ndigital marketing manager\nmarketing manager\nmarketing director");
    await expect(page.getByLabel("Role focus", { exact: true })).toContainText("Judge actual responsibilities");
    await expect(page.getByLabel("City for onsite and hybrid roles")).toHaveValue("Austin, TX");
    await expect(page.getByLabel("Onsite", { exact: true })).toBeChecked();
    await expect(page.getByLabel("Hybrid", { exact: true })).toBeChecked();
    await expect(page.getByLabel("Where remote roles must allow you to work")).toHaveValue("United States");
    await expect(page.getByLabel("Minimum pay in US dollars")).toHaveValue("100000");
    await expect(page.getByLabel("Pay period")).toHaveValue("YEAR");
    await expect(page.getByText("Remote roles are searched nationwide")).toBeVisible();
  });

  test("ranks Austin onsite/hybrid well above nationwide remote by default, without dropping remote", async ({
    page,
  }) => {
    await page.goto("/preview/jobs");
    await page.getByRole("button", { name: "Edit preferences" }).click();
    await expect(page.getByRole("radio", { name: /Strongly prefer onsite or hybrid/ })).toBeChecked();
    await expect(
      page.getByText(
        "Austin onsite and hybrid roles come first. Remote roles open to United States are still included",
      ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Close Search preferences" }).click();

    const tiers = page.locator(".tier__title");
    await expect(tiers.first()).toContainText("Austin onsite or hybrid");
    const titles = await tiers.allTextContents();
    const austinIndex = titles.findIndex((text) => text.includes("Austin onsite or hybrid"));
    const remoteIndex = titles.findIndex((text) => text.includes("Remote, open to United States"));
    expect(austinIndex).toBe(0);
    expect(remoteIndex).toBeGreaterThan(austinIndex);
    await expect(page.locator(".tier", { hasText: "Remote, open to United States" })).toContainText(
      "Copperline Credit",
    );
    await expect(page.locator(".tier", { hasText: "Location not established" })).toContainText("Lark & Loom");

    const order = await page.locator("article.listing .listing__company").allInnerTexts();
    expect(order.indexOf("Meridian Loop Software")).toBeLessThan(order.indexOf("Copperline Credit"));

    await page.getByRole("button", { name: "Edit preferences" }).click();
    await page.getByRole("radio", { name: /Prefer remote/ }).check();
    await page.getByRole("button", { name: "Save preferences" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Preferences saved" })).toBeVisible();
    await expect(tiers.first()).toContainText("Remote, open to United States");
    await expect(page.locator("article.listing", { hasText: "Meridian Loop Software" })).toBeVisible();
  });

  test("runs a search with honest per-source states", async ({ page }) => {
    await page.goto("/preview/jobs");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    const sources = page.locator(".sources");
    await expect(sources).toContainText("Searching sources");
    await expect(sources).toContainText(/Last search/, { timeout: 15_000 });
    const linkedin = sources.locator(".source", { hasText: "LinkedIn Jobs" });
    await expect(linkedin).toContainText("Needs you");
    await expect(linkedin).toContainText("imx-jobs-linkedin");
    await expect(linkedin).not.toContainText("listing");
    await expect(sources.locator(".source", { hasText: "Indeed" })).toContainText("Partly done");
  });

  test("validates search settings before running", async ({ page }) => {
    await page.goto("/preview/jobs");
    await page.getByRole("button", { name: "Edit preferences" }).click();
    await page.getByLabel("Job titles").fill("");
    await page.getByLabel("Include remote roles open to").uncheck();
    await page.getByLabel("Include roles in a city").uncheck();
    await page.getByRole("dialog").getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.locator(".error-summary")).toBeFocused();
    await expect(page.locator(".error-summary li")).toHaveCount(2);
    await expect(page.locator(".sources")).toHaveCount(0);
  });

  test("shows decisions, holds and unknown facts, and hides closed listings by default", async ({ page }) => {
    await page.goto("/preview/jobs");
    const listing = (title: string) => page.locator("article.listing", { hasText: title });

    const meridian = listing("Senior Marketing Manager, Demand Generation");
    await expect(meridian.locator(".decision__choice")).toHaveText("APPLY");
    await expect(meridian).toContainText("confidence 81%");
    await expect(meridian.getByRole("link", { name: /Application page/ })).toHaveAttribute(
      "href",
      "https://careers.meridianloop.example.test/jobs/4815/apply",
    );
    await expect(meridian.getByRole("link", { name: /Built In/ })).toBeVisible();
    await expect(meridian.getByRole("link", { name: /Google Jobs/ })).toBeVisible();

    const bluebonnet = listing("Bluebonnet Dental Partners");
    await expect(bluebonnet.locator(".decision__choice")).toHaveText("SKIP");
    await expect(bluebonnet).toContainText("Jev chose APPLY");
    await expect(bluebonnet).toContainText("below your $100,000 minimum");

    const larkloom = listing("Lark & Loom");
    await expect(larkloom).toContainText("Arrangement not stated");
    await expect(larkloom).toContainText("Pay not stated");
    await larkloom.getByRole("button", { name: "Ask Jev" }).click();
    await expect(larkloom.locator(".decision__choice")).toHaveText("REVIEW");
    await expect(larkloom.locator(".decision__unresolved")).toContainText("Work arrangement isn't stated");

    await expect(listing("Northwind Cartography")).toHaveCount(0);
    await page.getByLabel(/Hide 1 closed/).uncheck();
    await expect(listing("Northwind Cartography")).toContainText("Closed on the source");
    await expect(listing("Northwind Cartography").getByRole("button", { name: /Apply/ })).toHaveCount(0);

    await page.getByRole("radio", { name: /^Apply \d/ }).check();
    await expect(page.locator("article.listing")).toHaveCount(1);
  });

  test("changed preferences mark decisions out of date", async ({ page }) => {
    await page.goto("/preview/jobs");
    await page.getByRole("button", { name: "Edit preferences" }).click();
    await page.getByLabel("Extra keywords").fill("lifecycle");
    await page.getByRole("button", { name: "Save preferences" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Preferences saved" })).toBeVisible();
    const meridian = page.locator("article.listing", { hasText: "Meridian Loop Software" });
    await expect(meridian.locator(".decision__stale")).toBeVisible();
    await expect(meridian.getByRole("button", { name: "Ask Jev again (preferences changed)" })).toBeVisible();
  });

  test("tracking adds a card to the pipeline; applying only prefills the desk", async ({ page }) => {
    await page.goto("/preview/jobs");
    const tessera = page.locator("article.listing", { hasText: "Tessera Robotics" });
    await tessera.getByRole("button", { name: "Track in pipeline" }).click();
    await expect(tessera.getByRole("link", { name: "In your pipeline" })).toBeVisible();

    await tessera.getByRole("button", { name: /^Apply/ }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByLabel(/Application link/)).toHaveValue("https://tessera.example.test/careers/pmm/apply");
    await dialog.getByRole("button", { name: "Continue to the desk" }).click();
    await expect(page).toHaveURL(/\/preview$/);
    await expect(page.getByRole("heading", { name: /From your job search: Tessera Robotics/ })).toBeVisible();
    await expect(page.getByLabel("Application link")).toHaveValue("https://tessera.example.test/careers/pmm/apply");
    await expect(page.locator("#case-title")).toHaveCount(0);

    await page.getByRole("link", { name: "Pipeline", exact: true }).click();
    await expect(card(page, "Tessera Robotics")).toContainText("From job search");
  });

  test("warns when applying against Jev's decision, without blocking the user", async ({ page }) => {
    await page.goto("/preview/jobs");
    const saltgrass = page.locator("article.listing", { hasText: "Saltgrass Outdoor Co." });
    await saltgrass.getByRole("button", { name: /^Apply/ }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("current decision is REVIEW");
    await expect(dialog).toContainText("no application link yet");
    await expect(dialog.getByLabel(/Application link/)).toHaveValue("");
  });
});

test.describe("live pipeline and jobs without a backend", () => {
  test("say the service isn't available instead of showing made-up data", async ({ page }) => {
    await page.goto("/pipeline");
    await expect(page.getByRole("heading", { name: "The pipeline couldn't be loaded" })).toBeVisible();
    await expect(page.getByText("Service not connected")).toBeVisible();
    await expect(page.locator("article.card")).toHaveCount(0);

    await page.goto("/jobs");
    await expect(page.getByRole("heading", { name: "Job search couldn't be loaded" })).toBeVisible();
    await expect(page.locator("article.listing")).toHaveCount(0);
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
    await expect(page.getByRole("alert").filter({ hasText: "nothing was moved" })).toBeVisible();
    expect(moveBody).toEqual({ revision: 3, lane: "closed" });
    const applied = page.locator("section.lane", { has: page.getByRole("heading", { name: /^Applied/ }) });
    await expect(applied.locator("article.card", { hasText: "Fictional Live Co" })).toBeVisible();
  });
});
