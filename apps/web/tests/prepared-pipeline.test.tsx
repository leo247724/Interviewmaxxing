import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { EntryBadges } from "@/components/pipeline/Badges";
import { EntryCard } from "@/components/pipeline/EntryCard";
import {
  focusCounts,
  isPrepared,
  listedApplications,
  loadApplicationSummaries,
  preparedByEntry,
  preparedLabel,
  preparedListNote,
  visibleEntries,
  type FocusContext,
} from "@/lib/pipeline/prepared";
import { PreviewPipelineService, PREVIEW_LANES } from "@/lib/pipeline/preview";
import { EMPTY_FIELDS, type LinkedApplicationView, type PipelineEntryView } from "@/lib/pipeline/types";
import { ServiceError } from "@/lib/service/errors";
import { PreviewApplicationService } from "@/lib/service/preview";
import type { ApplicationListView, ApplicationSummaryView, PreparationView } from "@/lib/service/types";

// Fictional records only.

function entry(id: string, overrides: Partial<PipelineEntryView> = {}, fields: Partial<PipelineEntryView["fields"]> = {}): PipelineEntryView {
  return {
    id,
    lane: "saved",
    revision: 1,
    fields: { ...EMPTY_FIELDS, company: `Fictional ${id}`, role: "Marketing Manager", ...fields },
    applicationUrl: null,
    listingId: null,
    origin: "manual",
    application: null,
    selection: null,
    provenance: null,
    history: [],
    createdAt: "2026-09-20T12:00:00Z",
    updatedAt: "2026-09-20T12:00:00Z",
    ...overrides,
  };
}

function linked(applicationId: string, state: string, extra: Partial<LinkedApplicationView> = {}): LinkedApplicationView {
  return { applicationId, state, submittedAt: null, confirmationReference: null, ...extra };
}

const PREPARATION: PreparationView = {
  ready: true,
  formStep: 2,
  formUrl: "https://jobs.example.test/fictional/apply/review",
  captchaPending: false,
  preparedAt: "2026-09-23T15:00:00Z",
  submitted: false,
  evidence: [],
};

function summary(id: string, overrides: Partial<ApplicationSummaryView> = {}): ApplicationSummaryView {
  return {
    id,
    state: "NEEDS_INPUT",
    applicationUrl: `https://jobs.example.test/fictional/${id}`,
    job: { title: "Marketing Manager", company: `Fictional ${id}`, ats: "Greenhouse" },
    requestedAt: "2026-09-23T14:00:00Z",
    updatedAt: "2026-09-23T15:00:00Z",
    preparation: PREPARATION,
    pipelineEntryIds: [],
    ...overrides,
  };
}

const noUpcoming = () => false;

describe("prepared applications", () => {
  it("counts only a ready, unsubmitted NEEDS_INPUT stop as prepared", () => {
    expect(isPrepared(summary("a"))).toBe(true);
    expect(isPrepared(summary("a", { preparation: null }))).toBe(false);
    expect(isPrepared(summary("a", { state: "SUBMITTED" }))).toBe(false);
    expect(isPrepared(summary("a", { state: "FILLING" }))).toBe(false);
    expect(isPrepared(summary("a", { preparation: { ...PREPARATION, ready: false as unknown as true } }))).toBe(false);
    expect(isPrepared(summary("a", { preparation: { ...PREPARATION, submitted: true as unknown as false } }))).toBe(false);
  });

  it("joins a card linked to the prepared application", () => {
    const card = entry("linked", { application: linked("app_linked", "NEEDS_INPUT") });
    const map = preparedByEntry([card], [summary("app_linked")]);
    expect(map.get("linked")?.id).toBe("app_linked");
  });

  it("joins an unlinked card the service matched by its application link", () => {
    const card = entry("url_matched", { applicationUrl: "https://jobs.example.test/fictional/app_url" });
    const map = preparedByEntry([card, entry("other")], [summary("app_url", { pipelineEntryIds: ["url_matched"] })]);
    expect([...map.keys()]).toEqual(["url_matched"]);
  });

  it("ignores applications that aren't prepared", () => {
    const cards = [
      entry("questions", { application: linked("app_questions", "NEEDS_INPUT") }),
      entry("listed"),
      entry("submitted_listed"),
    ];
    const map = preparedByEntry(cards, [
      summary("app_questions", { preparation: null, pipelineEntryIds: ["questions"] }),
      summary("app_listed", { state: "FILLING", preparation: null, pipelineEntryIds: ["listed"] }),
      summary("app_done", { state: "SUBMITTED", preparation: null, pipelineEntryIds: ["submitted_listed"] }),
    ]);
    expect(map.size).toBe(0);
  });

  it("never marks a card whose linked application was submitted", () => {
    const card = entry("receipt", {
      application: linked("app_submitted", "SUBMITTED", { submittedAt: "2026-09-20T18:42:00Z", confirmationAuthority: "site" }),
    });
    expect(preparedByEntry([card], [summary("app_other", { pipelineEntryIds: ["receipt"] })]).size).toBe(0);
  });

  it("prefers the card's own linked application, then the most recently updated", () => {
    const card = entry("both", { application: linked("app_own", "NEEDS_INPUT") });
    const unlinked = entry("twice");
    const map = preparedByEntry(
      [card, unlinked],
      [
        summary("app_newer", { pipelineEntryIds: ["both", "twice"] }),
        summary("app_own", { pipelineEntryIds: ["both"] }),
        summary("app_older", { pipelineEntryIds: ["twice"] }),
      ],
    );
    expect(map.get("both")?.id).toBe("app_own");
    expect(map.get("twice")?.id).toBe("app_newer");
  });

  it("marks nothing when the list can't be read", async () => {
    const card = entry("linked", { application: linked("app_linked", "NEEDS_INPUT") });
    const older = { list: () => Promise.reject(new ServiceError("not_found", "No such route.")) };
    const notAllowed = { list: () => Promise.reject(new ServiceError("unknown", "The application service answered with HTTP 405.")) };
    const unreachable = { list: () => Promise.reject(new TypeError("Failed to fetch")) };
    const missing = {} as { list(): Promise<ApplicationListView> };

    const first = await loadApplicationSummaries(older);
    expect(first.applications).toEqual([]);
    expect(first.error?.code).toBe("not_found");
    expect(preparedByEntry([card], first.applications).size).toBe(0);
    expect(preparedListNote(first.error)).toBe("This service doesn't report prepared applications yet, so none are marked.");

    for (const service of [notAllowed, unreachable, missing]) {
      const result = await loadApplicationSummaries(service);
      expect(result.applications).toEqual([]);
      expect(preparedByEntry([card], result.applications).size).toBe(0);
      expect(preparedListNote(result.error)).toBe("Prepared applications couldn't be checked just now, so none are marked.");
    }
    expect(preparedListNote(null)).toBeNull();
  });

  it("drops a list response of the wrong shape instead of guessing", async () => {
    expect(listedApplications({} as ApplicationListView)).toEqual([]);
    expect(listedApplications(null)).toEqual([]);
    expect(listedApplications({ applications: [null, 7, { id: "app_ok" }] } as unknown as ApplicationListView)).toEqual([{ id: "app_ok" }]);
    const odd = await loadApplicationSummaries({ list: async () => ({ items: [] }) as unknown as ApplicationListView });
    expect(odd).toEqual({ applications: [], error: null });
  });
});

describe("prepared focus filter", () => {
  const cards = [
    entry("northwind", {}, { company: "Fictional Northwind", nextAction: "Check the answers" }),
    entry("harbor", {}, { company: "Fictional Harbor", role: "Brand Manager" }),
    entry("summit", {}, { company: "Fictional Summit", nextAction: "Send availability" }),
  ];
  const context: FocusContext = {
    prepared: preparedByEntry(cards, [summary("app_nw", { pipelineEntryIds: ["northwind", "harbor"] })]),
    upcoming: noUpcoming,
  };

  it("counts prepared cards next to the other filters", () => {
    expect(focusCounts(cards, context)).toEqual({ all: 3, actions: 2, interviews: 0, prepared: 2 });
    expect(focusCounts(cards, { prepared: new Map(), upcoming: noUpcoming }).prepared).toBe(0);
  });

  it("selects prepared cards and combines with the text filter", () => {
    expect(visibleEntries(cards, "", "prepared", context).map((card) => card.id)).toEqual(["northwind", "harbor"]);
    expect(visibleEntries(cards, "harbor", "prepared", context).map((card) => card.id)).toEqual(["harbor"]);
    expect(visibleEntries(cards, "summit", "prepared", context)).toEqual([]);
    expect(visibleEntries(cards, "  BRAND ", "all", context).map((card) => card.id)).toEqual(["harbor"]);
    expect(visibleEntries(cards, "", "actions", context).map((card) => card.id)).toEqual(["northwind", "summit"]);
    expect(visibleEntries(cards, "", "all", context)).toHaveLength(3);
  });
});

describe("EntryBadges", () => {
  it("shows the prepared mark instead of 'waiting for you'", () => {
    const card = entry("linked", { application: linked("app_linked", "NEEDS_INPUT") });
    const html = renderToStaticMarkup(<EntryBadges entry={card} prepared={summary("app_linked")} />);
    expect(html).toContain('<li class="mark mark--prepared">Prepared for review</li>');
    expect(html).not.toContain("waiting for you");
    expect(html).not.toContain("mark--receipt");

    const unlinked = renderToStaticMarkup(<EntryBadges entry={entry("unlinked")} prepared={summary("app_url")} />);
    expect(unlinked).toContain(">Prepared for review<");
    expect(unlinked).not.toContain("Application:");
  });

  it("adds the CAPTCHA note when one must be solved", () => {
    const prepared = summary("app_captcha", { preparation: { ...PREPARATION, captchaPending: true } });
    expect(preparedLabel(prepared)).toBe("Prepared for review · CAPTCHA to solve");
    const html = renderToStaticMarkup(<EntryBadges entry={entry("captcha")} prepared={prepared} />);
    expect(html).toContain('<li class="mark mark--prepared">Prepared for review · CAPTCHA to solve</li>');
  });

  it("keeps other application marks as they were", () => {
    const waiting = entry("waiting", { application: linked("app_waiting", "NEEDS_INPUT") });
    expect(renderToStaticMarkup(<EntryBadges entry={waiting} />)).toContain(
      '<li class="mark mark--application">Application: waiting for you</li>',
    );
    // A card linked to one application but pointed at by another prepared one shows both.
    const html = renderToStaticMarkup(<EntryBadges entry={waiting} prepared={summary("app_other")} />);
    expect(html).toContain(">Prepared for review<");
    expect(html).toContain(">Application: waiting for you<");
  });

  it("leaves receipts exactly as they are", () => {
    const submitted = entry("receipt", {
      origin: "jobs",
      application: linked("app_submitted", "SUBMITTED", {
        submittedAt: "2026-09-20T18:42:00Z",
        confirmationReference: "JV-2026-0412",
        confirmationAuthority: "site",
        confirmationMethod: "SUBMISSION_OBSERVED",
      }),
    });
    const before = renderToStaticMarkup(<EntryBadges entry={submitted} />);
    expect(before).toContain('class="mark mark--receipt">Receipt confirmed · ');
    expect(renderToStaticMarkup(<EntryBadges entry={submitted} prepared={summary("app_submitted")} />)).toBe(before);
    expect(renderToStaticMarkup(<EntryBadges entry={submitted} prepared={null} />)).toBe(before);
    expect(before).not.toContain("Prepared");
  });
});

describe("EntryCard", () => {
  const noop = () => {};

  it("offers Review instead of Apply for a prepared card", () => {
    const card = entry("northwind", {}, { company: "Fictional Northwind" });
    const html = renderToStaticMarkup(
      <EntryCard entry={card} lanes={PREVIEW_LANES} busy={false} onOpen={noop} onMove={noop} onApply={noop} onReview={noop} prepared={summary("app_nw")} />,
    );
    expect(html).toContain('class="chip-button chip-button--review"');
    expect(html).toContain("Review<span class=\"visually-hidden\"> the prepared application for Fictional Northwind</span>");
    expect(html).not.toContain("Apply…");
    expect(html).toContain(">Prepared for review<");
  });

  it("keeps Apply for other cards", () => {
    const html = renderToStaticMarkup(
      <EntryCard entry={entry("plain")} lanes={PREVIEW_LANES} busy={false} onOpen={noop} onMove={noop} onApply={noop} />,
    );
    expect(html).toContain("Apply…");
    expect(html).not.toContain("Review");
  });
});

describe("preview fixtures", () => {
  it("mark the unlinked Northwind card through the seeded prepared application", async () => {
    const board = await new PreviewPipelineService().board();
    const northwind = board.entries.find((item) => item.id === "pipe_pv_northwind")!;
    expect(northwind.application).toBeNull();
    const { applications, error } = await loadApplicationSummaries(new PreviewApplicationService());
    expect(error).toBeNull();
    const prepared = preparedByEntry(board.entries, applications);
    expect([...prepared.keys()]).toEqual(["pipe_pv_northwind"]);
    const summaryView = prepared.get("pipe_pv_northwind")!;
    expect(summaryView.id).toBe("pv_prepared_northwind");
    expect(preparedLabel(summaryView)).toBe("Prepared for review · CAPTCHA to solve");
  });
});
