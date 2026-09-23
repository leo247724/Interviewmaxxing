/**
 * Presentation models for the pipeline tracker. The durable repository is P1
 * (`packages/pipeline`), exposed through S1. Field names follow the 23-column
 * reference workbook. Board lanes are the user's tracking categories and are
 * never an application submission state.
 */

/** The 23 reference fields, in workbook column order. */
export interface PipelineFields {
  company: string | null;
  role: string | null;
  /** Free text, kept verbatim (may describe an interview in detail). */
  stage: string | null;
  /** Free text, kept verbatim. */
  status: string | null;
  priority: string | null;
  /** The user's own 0–10 score. Never a Jev confidence. */
  fitScore: number | null;
  /** YYYY-MM-DD. */
  nextInterviewDate: string | null;
  /** Clock time in Central Time (America/Chicago), as entered. */
  interviewTimeCT: string | null;
  interviewFormat: string | null;
  workArrangement: string | null;
  locationCommute: string | null;
  /** USD per year. */
  compensationLow: number | null;
  /** USD per year. */
  compensationHigh: number | null;
  compensationBasis: string | null;
  targetAssessment: string | null;
  sourceRecruiter: string | null;
  /** YYYY-MM-DD. Historical suggestion; the app never schedules it. */
  suggestedFollowUpDate: string | null;
  nextAction: string | null;
  /** YYYY-MM-DD. */
  lastInterviewDate: string | null;
  /** Free text, e.g. "End of next week". */
  decisionDueText: string | null;
  compensationBenefitsNotes: string | null;
  fitRationale: string | null;
  processSourceNotes: string | null;
}

export type PipelineFieldKey = keyof PipelineFields;

export interface PipelineLaneView {
  id: string;
  label: string;
}

export interface PipelineHistoryItem {
  at: string;
  kind: "created" | "imported" | "moved" | "edited";
  summary: string;
  fromLane: string | null;
  toLane: string | null;
}

export interface PipelineProvenanceView {
  importId: string;
  fileName: string | null;
  sourceDigest: string;
  sourceRow: number;
  importedAt: string;
  /** Original cell text by reference header, verbatim. */
  importedValues: Record<string, string>;
  sourceId?: string;
  latestImportedValues?: Record<string, string>;
  firstImportedAt?: string;
  versionCount?: number;
}

/** Summary of a linked canonical application. Only this can show a receipt. */
export interface LinkedApplicationView {
  applicationId: string;
  state: string;
  submittedAt: string | null;
  confirmationReference: string | null;
  confirmationAuthority?: "site" | "user";
  confirmationMethod?: string;
}

export interface LinkedSelectionView {
  selectionId: string;
  effectiveChoice: "APPLY" | "SKIP" | "REVIEW";
  decidedAt: string;
}

export interface PipelineEntryView {
  id: string;
  lane: string;
  /** Optimistic-concurrency token; stale writes answer 409 conflict. */
  revision: number;
  fields: PipelineFields;
  /** Where the application form starts. Imported rows often have none. */
  applicationUrl: string | null;
  listingId: string | null;
  origin: "manual" | "import" | "jobs";
  application: LinkedApplicationView | null;
  selection: LinkedSelectionView | null;
  provenance: PipelineProvenanceView | null;
  history: PipelineHistoryItem[];
  createdAt: string;
  updatedAt: string;
}

export interface PipelineBoardView {
  lanes: PipelineLaneView[];
  entries: PipelineEntryView[];
}

export interface PipelineEntryInput {
  lane: string;
  fields: PipelineFields;
  applicationUrl: string | null;
  listingId?: string | null;
}

export interface PipelineUpdateInput {
  revision: number;
  fields?: Partial<PipelineFields>;
  applicationUrl?: string | null;
}

export interface PipelineMoveInput {
  revision: number;
  lane: string;
}

export interface ImportRowError {
  field: string;
  message: string;
}

export interface ImportPreviewRow {
  rowNumber: number;
  action: "create" | "update" | "unchanged" | "error";
  company: string | null;
  role: string | null;
  errors: ImportRowError[];
}

export interface ImportPreviewView {
  previewId: string;
  fileName: string;
  sourceDigest: string;
  rows: ImportPreviewRow[];
  counts: { create: number; update: number; unchanged: number; error: number };
}

export interface ImportReceiptView {
  importId: string;
  fileName: string;
  sourceDigest: string;
  importedAt: string;
  created: number;
  updated: number;
  unchanged: number;
}

export interface ImportInput {
  format: "csv" | "json";
  fileName: string;
  content: string;
  /** Stable name for the tracker; reuse across revised exports. */
  sourceId?: string;
}

export interface PipelineService {
  readonly mode: "live" | "preview";
  board(): Promise<PipelineBoardView>;
  create(input: PipelineEntryInput): Promise<PipelineEntryView>;
  update(entryId: string, input: PipelineUpdateInput): Promise<PipelineEntryView>;
  move(entryId: string, input: PipelineMoveInput): Promise<PipelineEntryView>;
  previewImport(input: ImportInput): Promise<ImportPreviewView>;
  /** Refused while the preview has error rows: no partial silent imports. */
  commitImport(previewId: string): Promise<ImportReceiptView>;
}

export const EMPTY_FIELDS: PipelineFields = {
  company: null,
  role: null,
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
};

/** Reference workbook headers, in column order, mapped to fields. */
export const REFERENCE_COLUMNS: { header: string; field: PipelineFieldKey }[] = [
  { header: "Company", field: "company" },
  { header: "Role", field: "role" },
  { header: "Stage", field: "stage" },
  { header: "Status", field: "status" },
  { header: "Priority", field: "priority" },
  { header: "Fit / 10", field: "fitScore" },
  { header: "Next interview date", field: "nextInterviewDate" },
  { header: "Time (CT)", field: "interviewTimeCT" },
  { header: "Interview format", field: "interviewFormat" },
  { header: "Work arrangement", field: "workArrangement" },
  { header: "Location / commute", field: "locationCommute" },
  { header: "Comp low (USD/year)", field: "compensationLow" },
  { header: "Comp high (USD/year)", field: "compensationHigh" },
  { header: "Comp basis", field: "compensationBasis" },
  { header: "Target assessment", field: "targetAssessment" },
  { header: "Source / recruiter", field: "sourceRecruiter" },
  { header: "Follow-up date (suggested)", field: "suggestedFollowUpDate" },
  { header: "Next action", field: "nextAction" },
  { header: "Last interview date", field: "lastInterviewDate" },
  { header: "Decision due", field: "decisionDueText" },
  { header: "Comp / benefits notes", field: "compensationBenefitsNotes" },
  { header: "Fit rationale", field: "fitRationale" },
  { header: "Process / source notes", field: "processSourceNotes" },
];

export const NUMBER_FIELDS: ReadonlySet<PipelineFieldKey> = new Set([
  "fitScore",
  "compensationLow",
  "compensationHigh",
]);
export const DATE_FIELDS: ReadonlySet<PipelineFieldKey> = new Set([
  "nextInterviewDate",
  "suggestedFollowUpDate",
  "lastInterviewDate",
]);
