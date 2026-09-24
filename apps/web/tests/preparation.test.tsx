import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ApplicationWorkspace } from "@/components/ApplicationWorkspace";
import type { DeskActions } from "@/components/ApplicationDesk";
import { PreparedPanel } from "@/components/PreparedPanel";
import { ProgressRail } from "@/components/ProgressRail";
import { ReviewList } from "@/components/ReviewList";
import {
  confidenceLabel,
  groupReviewByPage,
  isPrepared,
  needsCheck,
  preparationOf,
  reviewDisplay,
  reviewOf,
  reviewPageAddress,
  reviewSummary,
  sourceLabel,
  withoutVersionedFields,
} from "@/lib/preparation";
import { isPrepared as isPreparedCard } from "@/lib/pipeline/prepared";
import { PREPARED_PREVIEW_ID, PreviewApplicationService } from "@/lib/service/preview";
import type { ApplicationView, InputRequestView, PreparationView, ReviewAnswerView } from "@/lib/service/types";
import { PREPARED_HEADLINE, describe as describeState, railIndex, railStatuses } from "@/lib/state";

const actions: DeskActions = {
  answer: async () => true,
  answerAndContinue: async () => true,
  resume: async () => true,
  reconcile: async () => true,
  checkNow: () => {},
  startAnother: () => {},
};

const profile = {
  firstName: "Robin",
  lastName: "Vale",
  email: "robin.vale@example.test",
  phone: "",
  location: "Portland, OR",
  linkedinUrl: "",
  websiteUrl: "",
};

/** The seeded, fictional prepared application (CAPTCHA pending). */
async function preparedView(): Promise<ApplicationView> {
  return new PreviewApplicationService().status(PREPARED_PREVIEW_ID);
}

/** Visible text of rendered markup, for assertions on copy. */
function text(html: string) {
  return html
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ");
}

function workspace(view: ApplicationView) {
  return renderToStaticMarkup(
    <ApplicationWorkspace view={view} mode="preview" profile={profile} actions={actions} actionError={null} lostContact={null} />,
  );
}

const QUESTIONS: InputRequestView = {
  kind: "questions",
  savedAt: null,
  errors: {},
  attestations: [],
  questions: [
    {
      id: "q_start",
      label: "When could you start?",
      help: null,
      control: "text",
      required: true,
      options: null,
      value: null,
      maxLength: null,
      reason: "Your start date isn't in your saved answers.",
    },
  ],
};

function item(overrides: Partial<ReviewAnswerView>): ReviewAnswerView {
  return {
    question: "First name",
    wordingRecorded: true,
    page: 1,
    control: "text",
    value: "Robin",
    source: "identity",
    confidence: 1,
    ...overrides,
  };
}

describe("prepared state", () => {
  it("is headed as prepared with nothing submitted, in a calm mood", async () => {
    const view = await preparedView();
    expect(isPrepared(view)).toBe(true);
    expect(describeState(view)).toEqual({ headline: PREPARED_HEADLINE, mood: "neutral" });
    expect(PREPARED_HEADLINE).toBe("Prepared for your review — nothing submitted");
  });

  it("stays prepared when questions remain, and never becomes 'Waiting for you'", async () => {
    const view = { ...(await preparedView()), needs: QUESTIONS };
    expect(describeState(view).headline).toBe(PREPARED_HEADLINE);
  });

  it("shows Filling complete and Submitting not reached on the rail", async () => {
    const view = await preparedView();
    expect(railIndex(view)).toBe(2);
    expect(railStatuses(view)).toEqual(["done", "done", "done", "todo", "todo"]);
    const html = renderToStaticMarkup(<ProgressRail view={view} />);
    expect(html).toContain("rail--prepared");
    expect(text(html)).toContain("Step 3 of 5 · Filling · done, stopped before submitting");
    expect(text(html)).toContain("Submitting (not started: nothing was submitted)");
  });

  it("keeps every other state's rail and headline unchanged", async () => {
    const view = await preparedView();
    // An older service without `preparation`: the old generic pause.
    const older: ApplicationView = { ...view, preparation: undefined, review: undefined };
    expect(isPrepared(older)).toBe(false);
    expect(describeState(older)).toEqual({ headline: "Waiting for you", mood: "attention" });
    expect(railStatuses(older)).toEqual(["done", "done", "held", "todo", "todo"]);
    const interaction: ApplicationView = {
      ...older,
      progress: null,
      needs: { kind: "interaction", interaction: "SIGN_IN", instructions: "Sign in.", pageUrl: null },
    };
    expect(railIndex(interaction)).toBe(1);
    expect(railStatuses({ ...older, state: "FILLING" })).toEqual(["done", "done", "current", "todo", "todo"]);
    expect(railStatuses({ ...older, state: "SUBMITTED" })).toEqual(["done", "done", "done", "done", "done"]);
    // `preparation` only counts while the application is paused.
    expect(isPrepared({ ...view, state: "FILLING" })).toBe(false);
    expect(describeState({ ...view, state: "FILLING" }).headline).toBe("Filling page 2 of 2");
  });

  it("treats a missing review list as empty", async () => {
    const view = await preparedView();
    expect(reviewOf({ ...view, review: undefined })).toEqual([]);
    expect(preparationOf({ ...view, preparation: undefined })).toBeNull();
  });
});

describe("prepared desk", () => {
  it("renders the prepared panel with a Prepared stamp, screenshot and review, and no questions", async () => {
    const view = await preparedView();
    const html = workspace(view);
    const visible = text(html);
    expect(visible).toContain(PREPARED_HEADLINE);
    // Dated preparedAt (Sep 23, 2026 UTC); the day depends on the local time zone.
    expect(html).toMatch(/<span class="stamp__word">Prepared<\/span><span class="stamp__date">2[34] SEP 2026<\/span>/);
    expect(visible).toContain("Nothing was submitted");
    expect(visible).toContain("the site hasn’t received this application");
    expect(html).toContain('<img src="/preview-fixtures/prepared-review.svg" alt="Final review page screenshot"');
    expect(visible).toContain("Final review page https://jobs.example.test/northwind/senior-lifecycle-marketer/apply/review");
    expect(visible).toContain("A CAPTCHA is waiting on the form");
    expect(visible).toContain("Prepare again");
    expect(visible).toContain("Submitting stays turned off");
    expect(visible).toContain("Start a different application");
    // A prepared review is not a request for input.
    expect(html).not.toContain('class="questions"');
    expect(html).not.toContain("panel--interaction");
    expect(visible).not.toContain("Waiting for you");
    expect(visible).not.toMatch(/Submitted and confirmed|Submission receipt/);
  });

  it("lists every answer with its source, and flags low confidence", async () => {
    const view = await preparedView();
    const visible = text(workspace(view));
    for (const label of [
      "Your details",
      "Saved answer",
      "From your profile",
      "Your answer",
      "Drafted from your facts",
      "Your resume",
    ]) {
      expect(visible).toContain(label);
    }
    expect(visible).toContain("robin.vale@example.test");
    expect(visible).toContain("Wording not recorded, see the screenshot");
    expect(visible).toContain("Robin_Vale_Resume_Lifecycle.pdf");
    expect(visible).toContain("86% confident Check this");
    expect(visible).toContain("93% confident");
    expect(visible).not.toContain("100% confident");
    expect(visible).toContain("8 answers on 2 pages. 1 flagged to check");
    expect(visible).toMatch(/Page 1 .* Page 2/);
  });

  it("shows the CAPTCHA note only while one is pending", async () => {
    const view = await preparedView();
    const solved = { ...view, preparation: { ...view.preparation!, captchaPending: false } };
    const html = renderToStaticMarkup(<PreparedPanel view={solved} actions={actions} />);
    expect(html).not.toContain("captcha-note");
    expect(text(html)).not.toContain("CAPTCHA");
  });

  it("asks remaining questions inside the prepared review, never an interaction", async () => {
    const view = { ...(await preparedView()), needs: QUESTIONS };
    const html = workspace(view);
    expect(html).toContain('class="questions"');
    expect(text(html)).toContain("When could you start?");
    expect(html).not.toContain("panel--interaction");
    expect(text(html)).toContain(PREPARED_HEADLINE);
  });

  it("never shows a prepared panel for an older service's generic pause", async () => {
    const view = await preparedView();
    const html = workspace({ ...view, preparation: undefined, review: undefined });
    expect(text(html)).toContain("Waiting for you");
    expect(html).not.toContain("prepared__evidence");
    expect(html).not.toContain('data-testid="state-stamp"');
  });

  it("says so when no screenshot or answers were reported", async () => {
    const view = await preparedView();
    const bare = { ...view, preparation: { ...view.preparation!, evidence: [], formUrl: null }, review: [] };
    const visible = text(renderToStaticMarkup(<PreparedPanel view={bare} actions={actions} />));
    expect(visible).toContain("The service saved no screenshot of the review page.");
    expect(visible).toContain("The service didn’t list the answers it entered.");
    expect(visible).not.toContain("Final review page https://");
  });
});

describe("review list", () => {
  it("shows page headings only when answers span more than one page", () => {
    const one = renderToStaticMarkup(<ReviewList items={[item({}), item({ question: "Last name", value: "Vale" })]} />);
    expect(one).not.toContain("Page 1");
    expect(one).toContain("<dl");
    expect(text(one)).toContain("2 answers.");
    const two = renderToStaticMarkup(<ReviewList items={[item({ page: 2 }), item({ page: 1, question: "Email" })]} />);
    expect(text(two)).toMatch(/Page 1 Email .* Page 2 First name/);
  });

  it("renders each control readably", () => {
    const html = renderToStaticMarkup(
      <ReviewList
        items={[
          item({ control: "multi_select", value: ["Braze", "Klaviyo"], question: "Platforms" }),
          item({ control: "long_text", value: "Line one.\n\nLine two.", question: "Why us?" }),
          item({ control: "boolean", value: "yes", question: "Authorized?" }),
          item({ control: "file", value: "Robin_Vale.pdf", question: "Resume", source: "resume" }),
          item({ control: "text", value: "", question: "Middle name" }),
        ]}
      />,
    );
    expect(html).toContain('<ul class="review__choices"><li>Braze</li><li>Klaviyo</li></ul>');
    expect(html).toContain('<p class="review__long">Line one.\n\nLine two.</p>');
    expect(text(html)).toContain("Authorized? Yes");
    expect(html).toContain('<span class="review__file mono">Robin_Vale.pdf</span>');
    expect(text(html)).toContain("Middle name Left blank");
  });
});

describe("preparation helpers", () => {
  it("names every source plainly", () => {
    expect(
      (["identity", "saved_answer", "fact", "user", "generated", "resume"] as const).map((source) => sourceLabel(source)),
    ).toEqual([
      "Your details",
      "Saved answer",
      "From your profile",
      "Your answer",
      "Drafted from your facts",
      "Your resume",
    ]);
    expect(sourceLabel("something_new")).toBe("Filled in by the desk");
  });

  it("words confidence only below 1 and never rounds a doubt up to 100%", () => {
    expect(confidenceLabel(1)).toBeNull();
    expect(confidenceLabel(0.86)).toBe("86% confident");
    expect(confidenceLabel(0.29)).toBe("29% confident");
    expect(confidenceLabel(0.996)).toBe("99% confident");
    expect(confidenceLabel(Number.NaN)).toBeNull();
    expect(needsCheck({ confidence: 0.89 })).toBe(true);
    expect(needsCheck({ confidence: 0.9 })).toBe(false);
    expect(needsCheck({ confidence: 1 })).toBe(false);
  });

  it("groups by page in page order, keeping form order within a page", () => {
    const groups = groupReviewByPage([
      item({ page: 2, question: "b" }),
      item({ page: 1, question: "a" }),
      item({ page: 2, question: "c" }),
      item({ page: 0, question: "?" }),
    ]);
    expect(groups.map((group) => [group.page, group.items.map((entry) => entry.question)])).toEqual([
      [1, ["a"]],
      [2, ["b", "c"]],
      [null, ["?"]],
    ]);
    expect(reviewSummary([item({}), item({ page: 2, confidence: 0.5 })])).toEqual({ total: 2, pages: 2, toCheck: 1 });
  });

  it("formats values by control", () => {
    expect(reviewDisplay({ control: "multi_select", value: "Braze" })).toEqual({ kind: "list", items: ["Braze"] });
    expect(reviewDisplay({ control: "single_select", value: ["A", "B"] })).toEqual({ kind: "text", text: "A, B" });
    expect(reviewDisplay({ control: "boolean", value: "No" })).toEqual({ kind: "text", text: "No" });
    expect(reviewDisplay({ control: "file", value: "cv.pdf" })).toEqual({ kind: "file", name: "cv.pdf" });
    expect(reviewDisplay({ control: "text", value: "  " })).toEqual({ kind: "blank" });
    expect(reviewDisplay({ control: "multi_select", value: [] })).toEqual({ kind: "blank" });
  });
});

describe("the strict prepared check (WP11 M7)", () => {
  const malformed: unknown[] = [
    {},
    { ready: true },
    { submitted: false },
    { ready: true, submitted: true },
    { ready: "true", submitted: false },
    { ready: 1, submitted: 0 },
    "prepared",
  ];

  it("counts only ready: true with submitted: false, on the desk and the board alike", async () => {
    const view = await preparedView();
    expect(isPrepared(view)).toBe(true);
    expect(isPreparedCard(view)).toBe(true);
    for (const preparation of malformed) {
      const odd = { ...view, preparation: preparation as PreparationView };
      expect(isPrepared(odd)).toBe(false);
      expect(isPreparedCard(odd)).toBe(false);
      expect(preparationOf(odd)).toBeNull();
      expect(describeState(odd).headline).not.toBe(PREPARED_HEADLINE);
    }
  });

  it("never says 'Prepared for your review — nothing submitted' for a malformed preparation", async () => {
    const view = await preparedView();
    for (const preparation of [{}, { ...view.preparation!, submitted: true }]) {
      const html = workspace({ ...view, preparation: preparation as unknown as PreparationView });
      expect(text(html)).not.toContain(PREPARED_HEADLINE);
      expect(html).not.toContain("prepared__evidence");
      expect(text(html)).toContain("Waiting for you");
    }
  });
});

describe("the final review page address (WP11 L8)", () => {
  it("keeps the scheme, host and path only", () => {
    expect(reviewPageAddress("https://jobs.example.test/fictional/apply/review?draft=tok_123&step=3#top")).toBe(
      "https://jobs.example.test/fictional/apply/review",
    );
    expect(reviewPageAddress("http://avery:secret@127.0.0.1:9/apply/review?session=abc")).toBe("http://127.0.0.1:9/apply/review");
    expect(reviewPageAddress("javascript:alert(1)")).toBeNull();
    expect(reviewPageAddress("not an address")).toBeNull();
    expect(reviewPageAddress(null)).toBeNull();
  });

  it("shows no draft token on the prepared panel", async () => {
    const view = await preparedView();
    const tokened = {
      ...view,
      preparation: {
        ...view.preparation!,
        formUrl: "https://jobs.example.test/northwind/apply/review?draft=tok_fictional#page-3",
      },
    };
    const html = renderToStaticMarkup(<PreparedPanel view={tokened} actions={actions} />);
    expect(text(html)).toContain("Final review page https://jobs.example.test/northwind/apply/review");
    expect(html).not.toContain("tok_fictional");
    expect(html).not.toContain("page-3");
  });
});

describe("an unknown presentation version (WP11 M8)", () => {
  const unknown = { version: "3", supported: false };

  function render(view: ApplicationView, presentation = unknown) {
    return renderToStaticMarkup(
      <ApplicationWorkspace
        view={presentation.supported ? view : withoutVersionedFields(view)}
        presentation={presentation}
        mode="live"
        profile={profile}
        actions={actions}
        actionError={null}
        lostContact={null}
      />,
    );
  }

  it("shows a prepared stop as the generic pause, with a visible notice and no review list", async () => {
    const html = render(await preparedView());
    const visible = text(html);
    expect(html).toContain('data-testid="presentation-notice"');
    expect(visible).toContain("presentation version 3");
    expect(visible).toContain("Waiting for you");
    expect(visible).not.toContain(PREPARED_HEADLINE);
    expect(html).not.toContain("prepared__evidence");
    expect(visible).not.toContain("What the desk entered");
  });

  it("asks a lookup question as a plain question", async () => {
    const view: ApplicationView = {
      ...(await preparedView()),
      preparation: null,
      review: [],
      needs: {
        ...QUESTIONS,
        questions: [
          {
            ...QUESTIONS.questions[0],
            id: "q_city",
            label: "City",
            control: "single_select",
            options: [
              { value: "Portland, OR, USA", label: "Portland, OR, USA" },
              { value: "Portland, ME, USA", label: "Portland, ME, USA" },
            ],
            lookup: true,
          },
        ],
      },
    };
    expect(render(view, { version: "2", supported: true })).toContain("Enter a different value…");
    const plain = render(view);
    expect(plain).not.toContain("Enter a different value…");
    expect(text(plain)).toContain("Portland, OR, USA");
  });

  it("changes nothing for a version this dashboard reads", async () => {
    const view = await preparedView();
    const html = render(view, { version: "2", supported: true });
    expect(text(html)).toContain(PREPARED_HEADLINE);
    expect(html).not.toContain("presentation-notice");
  });
});
