import { ServiceError } from "../service/errors";
import { parseCell, parseCsv, validateFields } from "./fields";
import {
  EMPTY_FIELDS,
  REFERENCE_COLUMNS,
  type ImportInput,
  type ImportPreviewRow,
  type ImportPreviewView,
  type ImportReceiptView,
  type PipelineBoardView,
  type PipelineEntryInput,
  type PipelineEntryView,
  type PipelineFieldKey,
  type PipelineFields,
  type PipelineLaneView,
  type PipelineMoveInput,
  type PipelineService,
  type PipelineUpdateInput,
} from "./types";

/**
 * In-memory tracker used only by /preview/pipeline and tests. Every company,
 * person and note here is fictional; the shape follows the reference workbook.
 */

export const PREVIEW_LANES: PipelineLaneView[] = [
  { id: "saved", label: "Saved" },
  { id: "applied", label: "Applied" },
  { id: "scheduling", label: "Scheduling" },
  { id: "interviewing", label: "Interviewing" },
  { id: "assessment", label: "Assessment" },
  { id: "follow-up", label: "Follow-up" },
  { id: "decision", label: "Decision" },
  { id: "offer", label: "Offer" },
  { id: "closed", label: "Closed" },
];

function fields(partial: Partial<PipelineFields>): PipelineFields {
  return { ...EMPTY_FIELDS, ...partial };
}

type Seed = Omit<PipelineEntryView, "revision" | "history" | "createdAt" | "updatedAt" | "provenance"> & {
  imported?: boolean;
};

const SEEDS: Seed[] = [
  {
    id: "pipe_pv_larkspur",
    lane: "interviewing",
    origin: "import",
    imported: true,
    applicationUrl: null,
    listingId: null,
    application: null,
    selection: null,
    fields: fields({
      company: "Larkspur Health",
      role: "Director, Growth Marketing",
      stage: "Hiring manager interview (45 min, video) — completed",
      status: "Waiting on panel scheduling",
      priority: "High",
      fitScore: 8,
      interviewFormat: "Video",
      workArrangement: "Hybrid (3 days in office)",
      locationCommute: "Austin, Domain area — about 25 minutes",
      compensationLow: 165000,
      compensationHigh: 190000,
      compensationBasis: "Base, posted range",
      targetAssessment: "Within target",
      sourceRecruiter: "Recruiter: Dana Whitfield (fictional)",
      suggestedFollowUpDate: "2026-09-19",
      nextAction: "Follow up on panel times if nothing arrives by Friday",
      lastInterviewDate: "2026-09-15",
      decisionDueText: "Not stated",
      compensationBenefitsNotes: "10% bonus target mentioned; equity not discussed",
      fitRationale: "Lifecycle and paid growth ownership match; healthcare is new",
      processSourceNotes: "Applied through the company careers page",
    }),
  },
  {
    id: "pipe_pv_quarry",
    lane: "applied",
    origin: "import",
    imported: true,
    applicationUrl: null,
    listingId: null,
    application: null,
    selection: null,
    fields: fields({
      company: "Quarry & Pine Outfitters",
      role: "Senior Marketing Manager, Lifecycle",
      stage: "Applied",
      status: "Submitted on the company site; no reply yet",
      priority: "Medium",
      fitScore: 7,
      workArrangement: "Remote (US)",
      locationCommute: "Remote",
      compensationLow: 120000,
      compensationHigh: 140000,
      compensationBasis: "Base, posted range",
      targetAssessment: "At target",
      sourceRecruiter: "LinkedIn listing",
      suggestedFollowUpDate: "2026-09-17",
      nextAction: "Check the applicant portal",
    }),
  },
  {
    id: "pipe_pv_tessellate",
    lane: "assessment",
    origin: "import",
    imported: true,
    applicationUrl: null,
    listingId: null,
    application: null,
    selection: null,
    fields: fields({
      company: "Tessellate Analytics",
      role: "Marketing Director",
      stage: "Take-home case study",
      status: "Case study due end of next week",
      priority: "High",
      fitScore: 9,
      interviewFormat: "Written case, then 60-minute presentation",
      workArrangement: "Onsite",
      locationCommute: "Austin, East 6th — about 15 minutes",
      compensationLow: 175000,
      compensationBasis: "Base, recruiter verbal",
      targetAssessment: "Above target",
      sourceRecruiter: "Referral",
      nextAction: "Draft the go-to-market plan outline",
      lastInterviewDate: "2026-09-12",
      decisionDueText: "Before October",
    }),
  },
  {
    id: "pipe_pv_brightwater",
    lane: "scheduling",
    origin: "import",
    imported: true,
    applicationUrl: null,
    listingId: null,
    application: null,
    selection: null,
    fields: fields({
      company: "Brightwater Credit Union",
      role: "Marketing Manager",
      stage: "Recruiter screen",
      status: "Recruiter asked for availability",
      priority: "Medium",
      fitScore: 6,
      interviewFormat: "Phone",
      workArrangement: "Hybrid",
      locationCommute: "Round Rock — about 35 minutes",
      compensationBasis: "Not posted",
      sourceRecruiter: "Internal recruiter (fictional)",
      nextAction: "Send three time slots",
    }),
  },
  {
    id: "pipe_pv_cinder",
    lane: "follow-up",
    origin: "import",
    imported: true,
    applicationUrl: null,
    listingId: null,
    application: null,
    selection: null,
    fields: fields({
      company: "Cinder Labs",
      role: "Head of Demand Generation",
      stage: "Second interview",
      status: "No response since the second interview",
      priority: "Low",
      fitScore: 5,
      workArrangement: "Remote (US)",
      compensationLow: 150000,
      compensationHigh: 150000,
      compensationBasis: "Base, recruiter verbal",
      suggestedFollowUpDate: "2026-09-10",
      nextAction: "Decide whether to follow up once more",
      lastInterviewDate: "2026-09-03",
    }),
  },
  {
    id: "pipe_pv_halcyon",
    lane: "saved",
    origin: "jobs",
    applicationUrl: "https://halcyon.example.test/careers/growth-operations-lead/apply",
    listingId: "lst_pv_halcyon",
    application: null,
    selection: { selectionId: "sel_pv_halcyon", effectiveChoice: "APPLY", decidedAt: "2026-09-21T15:10:00Z" },
    fields: fields({
      company: "Halcyon Freight",
      role: "Growth Operations Lead",
      stage: "Saved from job search",
      workArrangement: "Hybrid",
      locationCommute: "Austin",
      compensationLow: 130000,
      compensationHigh: 155000,
      compensationBasis: "Base, posted range",
      sourceRecruiter: "Built In",
    }),
  },
  {
    id: "pipe_pv_juniper",
    lane: "applied",
    origin: "jobs",
    applicationUrl: "https://jobs.example.test/juniper-vale/paid-media-manager",
    listingId: "lst_pv_juniper",
    application: {
      applicationId: "app_pv_juniper",
      state: "SUBMITTED",
      submittedAt: "2026-09-20T18:42:00Z",
      confirmationReference: "JV-2026-0412",
      confirmationAuthority: "site",
      confirmationMethod: "SUBMISSION_OBSERVED",
    },
    selection: { selectionId: "sel_pv_juniper", effectiveChoice: "APPLY", decidedAt: "2026-09-20T18:30:00Z" },
    fields: fields({
      company: "Juniper & Vale Studio",
      role: "Paid Media Manager",
      stage: "Applied",
      status: "Confirmation received",
      priority: "Medium",
      workArrangement: "Remote (US)",
      compensationLow: 105000,
      compensationHigh: 118000,
      compensationBasis: "Base, posted range",
      sourceRecruiter: "LinkedIn Jobs",
    }),
  },
  {
    id: "pipe_pv_northwind",
    lane: "closed",
    origin: "import",
    imported: true,
    applicationUrl: null,
    listingId: null,
    application: null,
    selection: null,
    fields: fields({
      company: "Northwind Cartography",
      role: "Senior Lifecycle Marketer",
      stage: "Final round",
      status: "Declined by employer after final round",
      priority: "Medium",
      fitScore: 7,
      workArrangement: "Hybrid",
      lastInterviewDate: "2026-08-28",
      fitRationale: "Strong lifecycle overlap",
    }),
  },
];

function headerValues(values: PipelineFields): Record<string, string> {
  const out: Record<string, string> = {};
  for (const { header, field } of REFERENCE_COLUMNS) {
    const value = values[field];
    out[header] = value === null ? "" : String(value);
  }
  return out;
}

/** Conservative lane for an imported row: only obvious wording moves it off "saved". */
export function laneForImported(stage: string | null, status: string | null): string {
  const text = `${stage ?? ""} ${status ?? ""}`.toLowerCase();
  if (/\b(declin|reject|withdr|closed|not moving)/.test(text)) return "closed";
  if (/\boffer\b/.test(text)) return "offer";
  if (/(assessment|case study|take-home|exercise)/.test(text)) return "assessment";
  if (/\binterview/.test(text)) return "interviewing";
  if (/(screen|schedul|availability)/.test(text)) return "scheduling";
  if (/(applied|submitted)/.test(text)) return "applied";
  return "saved";
}

function importKey(values: PipelineFields) {
  return `${(values.company ?? "").trim().toLowerCase()}|${(values.role ?? "").trim().toLowerCase()}`;
}

interface PendingImport {
  preview: ImportPreviewView;
  sourceId: string;
  rows: { rowNumber: number; values: PipelineFields; raw: Record<string, string>; entryId?: string }[];
}

export class PreviewPipelineService implements PipelineService {
  readonly mode = "preview" as const;
  private entries = new Map<string, PipelineEntryView>();
  private pending = new Map<string, PendingImport>();
  private counter = 0;
  private readonly now: () => Date;

  constructor(options: { now?: () => Date; empty?: boolean } = {}) {
    this.now = options.now ?? (() => new Date());
    if (options.empty) return;
    for (const seed of SEEDS) {
      const { imported, ...rest } = seed;
      const created = "2026-09-08T14:00:00Z";
      this.entries.set(seed.id, {
        ...structuredClone(rest),
        revision: 1,
        provenance: imported
          ? {
              importId: "imp_pv_workbook",
              fileName: "pipeline-fictional.csv",
              sourceDigest: "3f1d0c2a9e8b7d6c5b4a39281706f5e4d3c2b1a0998877665544332211000fed",
              sourceRow: SEEDS.indexOf(seed) + 2,
              importedAt: created,
              importedValues: headerValues(rest.fields),
              sourceId: "fixture-workbook",
              latestImportedValues: headerValues(rest.fields),
              firstImportedAt: created,
              versionCount: 1,
            }
          : null,
        history: [
          {
            at: created,
            kind: imported ? "imported" : "created",
            summary: imported ? "Imported from pipeline-fictional.csv" : "Saved from job search",
            fromLane: null,
            toLane: rest.lane,
          },
        ],
        createdAt: created,
        updatedAt: created,
      });
    }
  }

  async board(): Promise<PipelineBoardView> {
    return structuredClone({ lanes: PREVIEW_LANES, entries: [...this.entries.values()] });
  }

  async create(input: PipelineEntryInput): Promise<PipelineEntryView> {
    this.checkLane(input.lane);
    const errors = validateFields(input.fields);
    if (Object.keys(errors).length)
      throw new ServiceError("invalid", "Some fields need attention.", errors as Record<string, string>);
    const now = this.iso();
    const entry: PipelineEntryView = {
      id: `pipe_pv_${++this.counter}_${Date.now().toString(36)}`,
      lane: input.lane,
      revision: 1,
      fields: { ...input.fields },
      applicationUrl: input.applicationUrl,
      listingId: input.listingId ?? null,
      origin: input.listingId ? "jobs" : "manual",
      application: null,
      selection: null,
      provenance: null,
      history: [{ at: now, kind: "created", summary: "Added by you", fromLane: null, toLane: input.lane }],
      createdAt: now,
      updatedAt: now,
    };
    this.entries.set(entry.id, entry);
    return structuredClone(entry);
  }

  async update(entryId: string, input: PipelineUpdateInput): Promise<PipelineEntryView> {
    const entry = this.find(entryId);
    this.checkRevision(entry, input.revision);
    const next = { ...entry.fields, ...(input.fields ?? {}) };
    const errors = validateFields(next);
    if (Object.keys(errors).length)
      throw new ServiceError("invalid", "Some fields need attention.", errors as Record<string, string>);
    const changed = (Object.keys(input.fields ?? {}) as PipelineFieldKey[]).filter(
      (key) => entry.fields[key] !== next[key],
    );
    const urlChanged = input.applicationUrl !== undefined && input.applicationUrl !== entry.applicationUrl;
    entry.fields = next;
    if (input.applicationUrl !== undefined) entry.applicationUrl = input.applicationUrl;
    if (changed.length || urlChanged) {
      const labels = changed.map((key) => REFERENCE_COLUMNS.find((column) => column.field === key)?.header ?? key);
      if (urlChanged) labels.push("Application link");
      this.touch(entry, { kind: "edited", summary: `Edited ${labels.join(", ")}`, fromLane: null, toLane: null });
    }
    return structuredClone(entry);
  }

  async move(entryId: string, input: PipelineMoveInput): Promise<PipelineEntryView> {
    const entry = this.find(entryId);
    this.checkRevision(entry, input.revision);
    this.checkLane(input.lane);
    if (entry.lane !== input.lane) {
      const from = entry.lane;
      entry.lane = input.lane;
      this.touch(entry, {
        kind: "moved",
        summary: `Moved from ${this.label(from)} to ${this.label(input.lane)}`,
        fromLane: from,
        toLane: input.lane,
      });
    }
    return structuredClone(entry);
  }

  async previewImport(input: ImportInput): Promise<ImportPreviewView> {
    const sourceId = input.sourceId ?? "preview-default";
    let records: Record<string, string>[];
    try {
      records = input.format === "csv" ? csvRecords(input.content) : jsonRecords(input.content);
    } catch (error) {
      throw new ServiceError("invalid", `The file couldn't be read: ${(error as Error).message}`, {
        importFile: (error as Error).message,
      });
    }
    if (records.length === 0)
      throw new ServiceError("invalid", "The file has no rows.", { importFile: "No rows found." });

    const rows: PendingImport["rows"] = [];
    const previewRows: ImportPreviewRow[] = [];
    const seen = new Set<string>();
    records.forEach((record, index) => {
      const rowNumber = input.format === "csv" ? index + 2 : index + 1;
      const values = { ...EMPTY_FIELDS };
      const errors: ImportPreviewRow["errors"] = [];
      if (record[CELL_COUNT_KEY]) {
        errors.push({
          field: "Row",
          message: `Has ${record[CELL_COUNT_KEY]} cells but the header has fewer. Put quotes around text that contains commas.`,
        });
      }
      for (const { header, field } of REFERENCE_COLUMNS) {
        const raw = record[header] ?? record[field] ?? "";
        const parsed = parseCell(field, String(raw));
        if (parsed.error) errors.push({ field: header, message: parsed.error });
        (values as Record<string, unknown>)[field] = parsed.value;
      }
      for (const [field, message] of Object.entries(validateFields(values))) {
        const header = REFERENCE_COLUMNS.find((column) => column.field === field)?.header ?? field;
        if (!errors.some((error) => error.field === header)) errors.push({ field: header, message: message! });
      }
      const key = importKey(values);
      if (!errors.length && seen.has(key))
        errors.push({ field: "Company", message: "Duplicates an earlier row in this file." });
      seen.add(key);
      const existing = [...this.entries.values()].find((entry) => entry.provenance?.sourceId === sourceId &&
        `${(entry.provenance.importedValues.Company ?? "").trim().toLowerCase()}|${(entry.provenance.importedValues.Role ?? "").trim().toLowerCase()}` === key);
      const raw = headerValues(values);
      const action: ImportPreviewRow["action"] = errors.length
        ? "error"
        : !existing
          ? "create"
          : JSON.stringify(existing.provenance?.latestImportedValues ?? existing.provenance?.importedValues) === JSON.stringify(raw)
            ? "unchanged"
            : "update";
      previewRows.push({ rowNumber, action, company: values.company, role: values.role, errors });
      rows.push({ rowNumber, values, raw, entryId: existing?.id });
    });

    const counts = { create: 0, update: 0, unchanged: 0, error: 0 };
    for (const row of previewRows) counts[row.action] += 1;
    const preview: ImportPreviewView = {
      previewId: `impv_${++this.counter}`,
      fileName: input.fileName,
      sourceDigest: await digest(input.content),
      rows: previewRows,
      counts,
    };
    this.pending.set(preview.previewId, { preview, rows, sourceId });
    return structuredClone(preview);
  }

  async commitImport(previewId: string): Promise<ImportReceiptView> {
    const pending = this.pending.get(previewId);
    if (!pending) throw new ServiceError("not_found", "That import preview has expired. Choose the file again.");
    if (pending.preview.counts.error > 0) {
      throw new ServiceError("conflict", "Fix the rows with problems first; nothing was imported.");
    }
    this.pending.delete(previewId);
    const importedAt = this.iso();
    const importId = `imp_${this.counter}`;
    for (const row of pending.rows) {
      const action = pending.preview.rows.find((item) => item.rowNumber === row.rowNumber)!.action;
      const provenance = {
        importId,
        fileName: pending.preview.fileName,
        sourceDigest: pending.preview.sourceDigest,
        sourceRow: row.rowNumber,
        importedAt,
        importedValues: row.raw,
        latestImportedValues: row.raw,
        sourceId: pending.sourceId,
        firstImportedAt: importedAt,
        versionCount: 1,
      };
      if (action === "create") {
        const lane = laneForImported(row.values.stage, row.values.status);
        const entry: PipelineEntryView = {
          id: `pipe_pv_${++this.counter}_${Date.now().toString(36)}`,
          lane,
          revision: 1,
          fields: row.values,
          applicationUrl: null,
          listingId: null,
          origin: "import",
          application: null,
          selection: null,
          provenance,
          history: [
            {
              at: importedAt,
              kind: "imported",
              summary: `Imported from ${pending.preview.fileName}`,
              fromLane: null,
              toLane: lane,
            },
          ],
          createdAt: importedAt,
          updatedAt: importedAt,
        };
        this.entries.set(entry.id, entry);
      } else if (action === "update") {
        const entry = this.find(row.entryId!);
        const previous = entry.provenance!.latestImportedValues ?? entry.provenance!.importedValues;
        // Only fields the user hasn't edited since the last import take the new value.
        for (const { header, field } of REFERENCE_COLUMNS) {
          const current = entry.fields[field] === null ? "" : String(entry.fields[field]);
          if (current === (previous[header] ?? ""))
            (entry.fields as unknown as Record<string, unknown>)[field] = row.values[field];
        }
        entry.provenance = {
          ...provenance,
          importedValues: entry.provenance!.importedValues,
          firstImportedAt: entry.provenance!.firstImportedAt ?? entry.provenance!.importedAt,
          versionCount: (entry.provenance!.versionCount ?? 1) + 1,
        };
        this.touch(entry, {
          kind: "imported",
          summary: `Updated from ${pending.preview.fileName}`,
          fromLane: null,
          toLane: null,
        });
      }
    }
    return {
      importId,
      fileName: pending.preview.fileName,
      sourceDigest: pending.preview.sourceDigest,
      importedAt,
      created: pending.preview.counts.create,
      updated: pending.preview.counts.update,
      unchanged: pending.preview.counts.unchanged,
    };
  }

  private touch(entry: PipelineEntryView, item: Omit<PipelineEntryView["history"][number], "at">) {
    const at = this.iso();
    entry.revision += 1;
    entry.updatedAt = at;
    entry.history = [...entry.history, { at, ...item }];
  }

  private find(entryId: string) {
    const entry = this.entries.get(entryId);
    if (!entry) throw new ServiceError("not_found", "That card isn't in this preview.");
    return entry;
  }

  private checkRevision(entry: PipelineEntryView, revision: number) {
    if (entry.revision !== revision) {
      throw new ServiceError("conflict", "This card changed since you opened it. Reload it to see the latest version.");
    }
  }

  private checkLane(lane: string) {
    if (!PREVIEW_LANES.some((item) => item.id === lane)) {
      throw new ServiceError("invalid", "That lane doesn't exist.", { lane: "Choose a lane." });
    }
  }

  private label(lane: string) {
    return PREVIEW_LANES.find((item) => item.id === lane)?.label ?? lane;
  }

  private iso() {
    return this.now().toISOString();
  }
}

/** Marks a CSV row with more cells than the header row. */
const CELL_COUNT_KEY = "\u0000cells";

function csvRecords(content: string): Record<string, string>[] {
  const [header, ...rows] = parseCsv(content);
  if (!header) return [];
  const names = header.map((name) => name.trim());
  const unknown = names.filter((name) => name && !REFERENCE_COLUMNS.some((column) => column.header === name));
  if (unknown.length) throw new Error(`Unrecognised column${unknown.length > 1 ? "s" : ""}: ${unknown.join(", ")}`);
  if (!names.includes("Company") && !names.includes("Role")) throw new Error("The header row needs Company or Role.");
  return rows.map((cells) => {
    const record: Record<string, string> = Object.fromEntries(names.map((name, index) => [name, cells[index] ?? ""]));
    if (cells.length > names.length) record[CELL_COUNT_KEY] = `${cells.length}`;
    return record;
  });
}

function jsonRecords(content: string): Record<string, string>[] {
  const data: unknown = JSON.parse(content, (_key, value) => {
    if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Numbers must be finite.");
    return value;
  });
  const rows = Array.isArray(data) ? data : (data as { rows?: unknown })?.rows;
  if (!Array.isArray(rows)) throw new Error("Expected a list of rows.");
  return rows.map((row, index) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) throw new Error(`Row ${index + 1} isn't an object.`);
    return Object.fromEntries(
      Object.entries(row as Record<string, unknown>).map(([key, value]) => [key, value === null ? "" : String(value)]),
    );
  });
}

async function digest(content: string) {
  const bytes = new TextEncoder().encode(content);
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
