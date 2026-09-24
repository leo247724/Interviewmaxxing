import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { DeskActions } from "@/components/ApplicationDesk";
import { QuestionsForm } from "@/components/QuestionsForm";
import {
  LOOKUP_OTHER,
  lookupAnswer,
  lookupSelectValue,
  lookupSuggestions,
  startsWithOtherValue,
} from "@/lib/preparation";
import type { InputRequestView, RequiredQuestionView } from "@/lib/service/types";

const actions: DeskActions = {
  answer: async () => true,
  answerAndContinue: async () => true,
  resume: async () => true,
  reconcile: async () => true,
  checkNow: () => {},
  startAnother: () => {},
};

const PORTLANDS = ["Portland, OR, USA", "Portland, ME, USA", "Portland, TX, USA"];

function lookup(overrides: Partial<RequiredQuestionView> = {}): RequiredQuestionView {
  return {
    id: "q_location",
    label: "Location (city)",
    help: null,
    control: "single_select",
    required: true,
    lookup: true,
    options: PORTLANDS.map((place) => ({ value: place, label: place })),
    value: null,
    maxLength: null,
    reason: "The site suggested several places for “Portland”.",
    ...overrides,
  };
}

function form(...questions: RequiredQuestionView[]) {
  const needs: Extract<InputRequestView, { kind: "questions" }> = {
    kind: "questions",
    questions,
    attestations: [],
    savedAt: null,
    errors: {},
  };
  return renderToStaticMarkup(<QuestionsForm needs={needs} actions={actions} />);
}

function optionTexts(html: string) {
  return [...html.matchAll(/<option[^>]*>([^<]*)<\/option>/g)].map((match) => match[1]);
}

describe("lookup questions", () => {
  it("always offer the site's suggestions in a select, ending with a different-value choice", () => {
    const html = form(lookup());
    expect(html).toContain('<select id="q_location"');
    expect(html).not.toContain('type="radio"');
    expect(optionTexts(html)).toEqual(["Choose…", ...PORTLANDS, "Enter a different value…"]);
    expect(html).toContain(`<option value="${LOOKUP_OTHER}">Enter a different value…</option>`);
    expect(html).toContain("These are the site&#x27;s suggestions for what was typed.");
    expect(html).toContain("it goes into the site&#x27;s search box exactly as written");
    expect(html).toContain("Asked because: The site suggested several places");
    // Nothing is chosen for the person, and no text box shows until they ask for one.
    expect(html).toContain('<option value="" selected="">Choose…</option>');
    expect(html).not.toContain('id="q_location-other"');
  });

  it("shows a chosen suggestion as selected", () => {
    const html = form(lookup({ value: "Portland, ME, USA" }));
    expect(html).toContain('<option value="Portland, ME, USA" selected="">Portland, ME, USA</option>');
    expect(html).not.toContain('id="q_location-other"');
  });

  it("starts in different-value mode when the saved answer isn't a suggestion", () => {
    const html = form(lookup({ value: "Beaverton, OR, USA" }));
    expect(html).toContain(`<option value="${LOOKUP_OTHER}" selected="">Enter a different value…</option>`);
    expect(html).toMatch(/<label for="q_location-other"[^>]*>Different value for the site’s search box<\/label>/);
    expect(html).toMatch(/<input id="q_location-other"[^>]*value="Beaverton, OR, USA"/);
    expect(html).toContain("Typed into the site’s search box exactly as written.");
  });

  it("uses a plain text box, with a lookup hint, when the site offered no suggestions", () => {
    const html = form(lookup({ control: "text", options: null }));
    expect(html).not.toContain("<select");
    expect(html).toMatch(/<input id="q_location"[^>]*type="text"/);
    expect(html).toContain("The site looks this up as you type.");
  });

  it("leaves ordinary short choice questions as radio buttons", () => {
    const html = form(lookup({ lookup: undefined }));
    expect(html).not.toContain("<select");
    expect(html.match(/type="radio"/g)).toHaveLength(3);
    expect(html).not.toContain("Enter a different value");
  });
});

describe("lookup helpers", () => {
  it("find suggestions only on lookups", () => {
    expect(lookupSuggestions(lookup())).toHaveLength(3);
    expect(lookupSuggestions(lookup({ lookup: false }))).toEqual([]);
    expect(lookupSuggestions(lookup({ options: null }))).toEqual([]);
  });

  it("start in different-value mode only for a saved non-suggestion", () => {
    expect(startsWithOtherValue(lookup())).toBe(false);
    expect(startsWithOtherValue(lookup({ value: "Portland, TX, USA" }))).toBe(false);
    expect(startsWithOtherValue(lookup({ value: "Beaverton, OR, USA" }))).toBe(true);
    expect(startsWithOtherValue(lookup({ value: "   " }))).toBe(false);
    expect(startsWithOtherValue(lookup({ value: "Beaverton", lookup: false }))).toBe(false);
  });

  it("map answers to the select and never send the sentinel", () => {
    expect(lookupSelectValue(lookup(), "Portland, OR, USA", false)).toBe("Portland, OR, USA");
    expect(lookupSelectValue(lookup(), "Beaverton", false)).toBe("");
    expect(lookupSelectValue(lookup(), "Beaverton", true)).toBe(LOOKUP_OTHER);
    expect(lookupAnswer(LOOKUP_OTHER)).toBe("");
    expect(lookupAnswer("Beaverton, OR, USA")).toBe("Beaverton, OR, USA");
  });
});
