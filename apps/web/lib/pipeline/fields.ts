import { DATE_FIELDS, NUMBER_FIELDS, type PipelineFieldKey, type PipelineFields } from "./types";

export type FieldErrors = Partial<Record<PipelineFieldKey | "applicationUrl" | "lane", string>>;

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export function isValidDate(value: string): boolean {
  if (!DATE_PATTERN.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

/** Validate tracker fields as entered. Blank stays blank; nothing is filled in. */
export function validateFields(fields: PipelineFields): FieldErrors {
  const errors: FieldErrors = {};
  if (!fields.company?.trim() && !fields.role?.trim()) {
    errors.company = "Enter a company or a role so the card can be recognised.";
  }
  if (fields.fitScore !== null && (!Number.isFinite(fields.fitScore) || fields.fitScore < 0 || fields.fitScore > 10)) {
    errors.fitScore = "Fit is scored from 0 to 10.";
  }
  for (const key of ["compensationLow", "compensationHigh"] as const) {
    const value = fields[key];
    if (value !== null && (!Number.isFinite(value) || value < 0)) {
      errors[key] = "Enter a yearly amount in US dollars, digits only.";
    }
  }
  if (
    !errors.compensationLow &&
    !errors.compensationHigh &&
    fields.compensationLow !== null &&
    fields.compensationHigh !== null &&
    fields.compensationLow > fields.compensationHigh
  ) {
    errors.compensationHigh = "The high end is below the low end.";
  }
  for (const key of DATE_FIELDS) {
    const value = fields[key] as string | null;
    if (value !== null && !isValidDate(value)) errors[key] = "Use a real date (YYYY-MM-DD).";
  }
  return errors;
}

/** Parse a form or import cell into a field value. Blank → null, never 0. */
export function parseCell(key: PipelineFieldKey, raw: string): { value: string | number | null; error?: string } {
  const text = raw.trim();
  if (!text) return { value: null };
  if (NUMBER_FIELDS.has(key)) {
    const cleaned = text.replace(/[$,\s]/g, "");
    if (!/^\d+(\.\d+)?$/.test(cleaned)) {
      return {
        value: null,
        error: key === "fitScore" ? "Fit must be a number from 0 to 10." : "Not a plain yearly amount.",
      };
    }
    return { value: Number(cleaned) };
  }
  if (DATE_FIELDS.has(key)) {
    if (!isValidDate(text)) return { value: null, error: "Dates must be YYYY-MM-DD." };
    return { value: text };
  }
  return { value: text };
}

export function formatMoney(value: number | null): string | null {
  if (value === null) return null;
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(value);
}

export function compensationSummary(fields: PipelineFields): string | null {
  const low = formatMoney(fields.compensationLow);
  const high = formatMoney(fields.compensationHigh);
  if (low && high) return low === high ? low : `${low}–${high}`;
  if (low) return `from ${low}`;
  if (high) return `up to ${high}`;
  return null;
}

/** Minimal RFC 4180 CSV reader (quoted fields, escaped quotes, CRLF). */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (quoted) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i++;
        } else {
          quoted = false;
        }
      } else {
        cell += char;
      }
    } else if (char === '"' && cell === "") {
      quoted = true;
    } else if (char === ",") {
      row.push(cell);
      cell = "";
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else {
      cell += char;
    }
  }
  if (quoted) throw new Error("A quoted cell is never closed.");
  if (cell !== "" || row.length > 0) {
    row.push(cell);
    rows.push(row);
  }
  return rows.filter((cells) => cells.some((value) => value.trim() !== ""));
}
