import { describe, expect, it } from "vitest";
import type { AttestationView, RequiredQuestionView } from "@/lib/service/types";
import { validateAnswers, validateApplicationUrl, validateApplyForm } from "@/lib/validation";

const profile = {
  firstName: "Robin",
  lastName: "Vale",
  email: "robin.vale@example.test",
  phone: "",
  location: "",
  linkedinUrl: "",
  websiteUrl: "",
};

describe("validateApplicationUrl", () => {
  it("requires a link", () => expect(validateApplicationUrl("  ")).toMatch(/Paste the link/));
  it("rejects partial addresses", () => expect(validateApplicationUrl("jobs.example.com/apply")).toMatch(/https/));
  it("rejects non-web schemes", () => expect(validateApplicationUrl("mailto:jobs@example.com")).toMatch(/web address/));
  it("rejects hosts without a domain", () => expect(validateApplicationUrl("https://careers")).toMatch(/domain/));
  it("accepts https application pages", () =>
    expect(validateApplicationUrl("https://jobs.example.test/northwind/apply")).toBeNull());
});

describe("validateApplyForm", () => {
  it("reports every missing essential without inventing values", () => {
    const errors = validateApplyForm({
      applicationUrl: "",
      profile: { ...profile, firstName: "", email: "not-an-email" },
      resumeId: null,
    });
    expect(Object.keys(errors).sort()).toEqual(["applicationUrl", "email", "firstName", "resumeId"]);
  });

  it("treats phone and links as optional but checks their format", () => {
    expect(validateApplyForm({ applicationUrl: "https://a.example/x", profile, resumeId: "r1" })).toEqual({});
    const errors = validateApplyForm({
      applicationUrl: "https://a.example/x",
      profile: { ...profile, phone: "12", linkedinUrl: "linkedin.com/in/x" },
      resumeId: "r1",
    });
    expect(Object.keys(errors).sort()).toEqual(["linkedinUrl", "phone"]);
  });
});

describe("validateAnswers", () => {
  const question = (id: string, control: RequiredQuestionView["control"], required = true): RequiredQuestionView => ({
    id,
    label: id,
    help: null,
    control,
    required,
    options: control === "single_select" ? [{ value: "a", label: "A" }] : null,
    value: null,
    maxLength: control === "long_text" ? 10 : null,
    reason: null,
  });
  const questions = [question("pick", "single_select"), question("essay", "long_text"), question("opt", "text", false)];
  const attestations: AttestationView[] = [
    { id: "truth", statement: "True", required: true, accepted: false },
    { id: "pool", statement: "Pool", required: false, accepted: false },
  ];

  it("requires answers and required statements when continuing", () => {
    const errors = validateAnswers(questions, attestations, {}, {}, true);
    expect(Object.keys(errors).sort()).toEqual(["essay", "pick", "truth"]);
  });

  it("allows saving an incomplete draft", () => {
    expect(validateAnswers(questions, attestations, {}, {}, false)).toEqual({});
  });

  it("still checks formats in a draft", () => {
    const errors = validateAnswers(questions, attestations, { essay: "far too long an answer" }, {}, false);
    expect(errors.essay).toMatch(/under 10 characters/);
  });

  it("never requires optional statements", () => {
    const errors = validateAnswers(questions, attestations, { pick: "a", essay: "ok" }, { truth: true }, true);
    expect(errors).toEqual({});
  });
});

describe("validateAnswers for site lookups", () => {
  const places = ["Portland, OR, USA", "Portland, ME, USA", "Portland, TX, USA"];
  const lookup = (overrides: Partial<RequiredQuestionView> = {}): RequiredQuestionView => ({
    id: "where",
    label: "Location (city)",
    help: null,
    control: "single_select",
    required: true,
    lookup: true,
    options: places.map((place) => ({ value: place, label: place })),
    value: null,
    maxLength: null,
    reason: null,
    ...overrides,
  });

  it("gives a clear error for a blank different value", () => {
    const errors = validateAnswers([lookup()], [], { where: "" }, {}, true, { where: true });
    expect(errors.where).toMatch(/Type the value to enter in the site's search box/);
    const spaces = validateAnswers([lookup()], [], { where: "   " }, {}, true, { where: true });
    expect(spaces.where).toBe(errors.where);
  });

  it("asks for a suggestion or a different value when nothing is chosen", () => {
    expect(validateAnswers([lookup()], [], {}, {}, true).where).toBe(
      "Choose one of the site's suggestions, or enter a different value.",
    );
    expect(validateAnswers([lookup({ control: "text", options: null })], [], {}, {}, true).where).toMatch(
      /site should look up/,
    );
  });

  it("accepts any non-blank text, not only the suggestions", () => {
    expect(validateAnswers([lookup()], [], { where: "Beaverton, OR, USA" }, {}, true, { where: true })).toEqual({});
    expect(validateAnswers([lookup()], [], { where: "Portland, ME, USA" }, {}, true)).toEqual({});
    expect(validateAnswers([lookup({ control: "text", options: null })], [], { where: "Salem" }, {}, true)).toEqual({});
  });

  it("lets a draft leave a lookup blank, and optional lookups stay optional", () => {
    expect(validateAnswers([lookup()], [], { where: "" }, {}, false, { where: true })).toEqual({});
    expect(validateAnswers([lookup({ required: false })], [], { where: "" }, {}, true, { where: true })).toEqual({});
  });

  it("keeps the usual message for ordinary choice questions", () => {
    const plain = lookup({ lookup: undefined });
    expect(validateAnswers([plain], [], {}, {}, true).where).toBe("Choose an answer to continue.");
  });
});
