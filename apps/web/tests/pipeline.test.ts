import { describe, expect, it } from "vitest";
import { ServiceError } from "@/lib/service/errors";
import { PreviewPipelineService, laneForImported } from "@/lib/pipeline/preview";
import { parseCell, parseCsv, validateFields } from "@/lib/pipeline/fields";
import { EMPTY_FIELDS, REFERENCE_COLUMNS } from "@/lib/pipeline/types";
import { takeHandoff, writeHandoff } from "@/lib/handoff";

const HEADER = REFERENCE_COLUMNS.map((column) => column.header).join(",");

function csvRow(values: Record<string, string>) {
  return REFERENCE_COLUMNS.map(({ header }) => {
    const value = values[header] ?? "";
    return /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
  }).join(",");
}

describe("reference schema", () => {
  it("covers all 23 workbook columns once, in order", () => {
    expect(REFERENCE_COLUMNS).toHaveLength(23);
    expect(new Set(REFERENCE_COLUMNS.map((column) => column.field)).size).toBe(23);
    expect(Object.keys(EMPTY_FIELDS)).toEqual(REFERENCE_COLUMNS.map((column) => column.field));
  });
});

describe("field parsing and validation", () => {
  it("keeps blank separate from zero", () => {
    expect(parseCell("fitScore", "  ")).toEqual({ value: null });
    expect(parseCell("fitScore", "0")).toEqual({ value: 0 });
    expect(parseCell("compensationLow", "$120,000")).toEqual({ value: 120000 });
  });

  it("rejects malformed numbers and impossible dates", () => {
    expect(parseCell("compensationHigh", "about 150k").error).toBeTruthy();
    expect(parseCell("nextInterviewDate", "2026-02-30").error).toBeTruthy();
    expect(parseCell("nextInterviewDate", "2026-10-02")).toEqual({ value: "2026-10-02" });
    expect(parseCell("decisionDueText", "End of next week")).toEqual({ value: "End of next week" });
  });

  it("validates score range and compensation bounds without filling anything in", () => {
    const errors = validateFields({
      ...EMPTY_FIELDS,
      company: "Fictional Co",
      fitScore: 11,
      compensationLow: 200,
      compensationHigh: 100,
    });
    expect(Object.keys(errors).sort()).toEqual(["compensationHigh", "fitScore"]);
    expect(validateFields({ ...EMPTY_FIELDS })).toHaveProperty("company");
  });

  it("reads quoted CSV with commas, quotes and newlines", () => {
    expect(parseCsv('a,b\n"x, y","say ""hi""\nthere"\r\n')).toEqual([
      ["a", "b"],
      ["x, y", 'say "hi"\nthere'],
    ]);
    expect(() => parseCsv('a\n"unterminated')).toThrow(/never closed/);
  });

  it("maps imported wording to a lane conservatively", () => {
    expect(laneForImported("Final round", "Declined by employer")).toBe("closed");
    expect(laneForImported("Take-home case study", null)).toBe("assessment");
    expect(laneForImported("Phone screen", null)).toBe("scheduling");
    expect(laneForImported("Networking coffee", "Waiting")).toBe("saved");
  });
});

describe("PreviewPipelineService", () => {
  it("moves cards with history and rejects stale revisions", async () => {
    const service = new PreviewPipelineService();
    const board = await service.board();
    const entry = board.entries.find((item) => item.id === "pipe_pv_larkspur")!;
    const moved = await service.move(entry.id, { revision: entry.revision, lane: "decision" });
    expect(moved.lane).toBe("decision");
    expect(moved.revision).toBe(entry.revision + 1);
    expect(moved.history.at(-1)).toMatchObject({ kind: "moved", fromLane: "interviewing", toLane: "decision" });
    expect(moved.application).toBeNull();
    await expect(service.move(entry.id, { revision: entry.revision, lane: "offer" })).rejects.toMatchObject({
      code: "conflict",
    });
  });

  it("edits fields and records which ones changed", async () => {
    const service = new PreviewPipelineService();
    const entry = (await service.board()).entries[0];
    const updated = await service.update(entry.id, {
      revision: entry.revision,
      fields: { nextAction: "Send availability", fitScore: 9 },
    });
    expect(updated.fields.nextAction).toBe("Send availability");
    expect(updated.history.at(-1)?.summary).toMatch(/Fit \/ 10.*Next action|Next action.*Fit \/ 10/);
    await expect(
      service.update(entry.id, {
        revision: updated.revision,
        fields: { compensationLow: 300000, compensationHigh: 100 },
      }),
    ).rejects.toBeInstanceOf(ServiceError);
  });

  it("imports idempotently, refuses rows with errors and keeps manual edits", async () => {
    const service = new PreviewPipelineService({ empty: true });
    const file = [
      HEADER,
      csvRow({ Company: "Fictional Harbor", Role: "Marketing Director", Stage: "Recruiter screen", "Fit / 10": "7" }),
      csvRow({
        Company: "Fictional Summit",
        Role: "Marketing Manager",
        Stage: "Applied",
        "Comp low (USD/year)": "110000",
      }),
    ].join("\n");

    const first = await service.previewImport({ format: "csv", fileName: "fictional.csv", content: file });
    expect(first.counts).toEqual({ create: 2, update: 0, unchanged: 0, error: 0 });
    const receipt = await service.commitImport(first.previewId);
    expect(receipt).toMatchObject({ created: 2, updated: 0, unchanged: 0, fileName: "fictional.csv" });
    expect(receipt.sourceDigest).toMatch(/^[0-9a-f]{64}$/);

    let board = await service.board();
    const harbor = board.entries.find((item) => item.fields.company === "Fictional Harbor")!;
    expect(harbor.lane).toBe("scheduling");
    expect(harbor.applicationUrl).toBeNull();
    expect(harbor.application).toBeNull();
    expect(harbor.provenance?.importedValues["Fit / 10"]).toBe("7");
    expect(harbor.provenance?.importedValues["Comp high (USD/year)"]).toBe("");

    // A manual edit survives a later import that changes the same row.
    await service.update(harbor.id, { revision: harbor.revision, fields: { fitScore: 9 } });

    const again = await service.previewImport({ format: "csv", fileName: "fictional.csv", content: file });
    expect(again.counts).toEqual({ create: 0, update: 0, unchanged: 2, error: 0 });
    await service.commitImport(again.previewId);
    expect((await service.board()).entries).toHaveLength(2);

    const changed = file.replace("Recruiter screen", "Hiring manager interview");
    const third = await service.previewImport({ format: "csv", fileName: "fictional-v2.csv", content: changed });
    expect(third.counts.update).toBe(1);
    await service.commitImport(third.previewId);
    board = await service.board();
    const after = board.entries.find((item) => item.fields.company === "Fictional Harbor")!;
    expect(after.fields.stage).toBe("Hiring manager interview");
    expect(after.fields.fitScore).toBe(9);
    expect(after.lane).toBe("scheduling");
  });

  it("reports malformed rows precisely and imports nothing", async () => {
    const service = new PreviewPipelineService({ empty: true });
    const file = [
      HEADER,
      csvRow({ Company: "Fictional Ok", Role: "Marketing Manager" }),
      csvRow({ Company: "Fictional Bad", Role: "Director", "Fit / 10": "eleven", "Next interview date": "13/40/2026" }),
      csvRow({ Company: "Fictional Ok", Role: "Marketing Manager" }),
    ].join("\n");
    const preview = await service.previewImport({ format: "csv", fileName: "bad.csv", content: file });
    expect(preview.counts.error).toBe(2);
    const bad = preview.rows.find((row) => row.rowNumber === 3)!;
    expect(bad.errors.map((error) => error.field).sort()).toEqual(["Fit / 10", "Next interview date"]);
    expect(preview.rows.find((row) => row.rowNumber === 4)!.errors[0].message).toMatch(/Duplicates/);
    await expect(service.commitImport(preview.previewId)).rejects.toMatchObject({ code: "conflict" });
    expect((await service.board()).entries).toHaveLength(0);
  });

  it("rejects unknown columns, non-finite JSON and non-object rows", async () => {
    const service = new PreviewPipelineService({ empty: true });
    await expect(
      service.previewImport({ format: "csv", fileName: "x.csv", content: "Company,Salary guess\nA,1" }),
    ).rejects.toMatchObject({ code: "invalid" });
    await expect(
      service.previewImport({ format: "json", fileName: "x.json", content: '[{"Company":"A","Fit / 10":1e999}]' }),
    ).rejects.toMatchObject({ code: "invalid" });
    await expect(service.previewImport({ format: "json", fileName: "x.json", content: "[1]" })).rejects.toMatchObject({
      code: "invalid",
    });
    const ok = await service.previewImport({
      format: "json",
      fileName: "x.json",
      content: JSON.stringify([{ company: "Fictional Json", role: "Marketing Manager", fitScore: 6 }]),
    });
    expect(ok.counts.create).toBe(1);
  });
});

describe("desk handoff", () => {
  it("applies once and never carries anything but the chosen job", () => {
    const store = new Map<string, string>();
    const storage = {
      setItem: (key: string, value: string) => void store.set(key, value),
      getItem: (key: string) => store.get(key) ?? null,
      removeItem: (key: string) => void store.delete(key),
    };
    writeHandoff(
      {
        applicationUrl: "https://jobs.example.test/a/apply",
        company: "Fictional Co",
        role: "Marketing Manager",
        from: "pipeline",
        pipelineEntryId: "pipe_1",
        listingId: null,
      },
      storage,
    );
    expect(takeHandoff(storage)?.applicationUrl).toBe("https://jobs.example.test/a/apply");
    expect(takeHandoff(storage)).toBeNull();
    store.set("imx.deskHandoff", "{not json");
    expect(takeHandoff(storage)).toBeNull();
  });
});
