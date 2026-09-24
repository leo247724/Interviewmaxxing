/**
 * Carries a chosen job from the Pipeline or Jobs view to the application desk.
 * A handoff without `applicationId` only prefills the link; the user still
 * presses Apply, which is the submission authorization. A review handoff
 * (`applicationId` set) opens that existing application, e.g. one prepared for
 * final review, so the user can check it. Nothing here starts, resumes or
 * submits an application.
 */

const HANDOFF_KEY = "imx.deskHandoff";

export interface DeskHandoff {
  /** Where the application form starts. Required to prefill; a review handoff may carry it too. */
  applicationUrl: string | null;
  company: string | null;
  role: string | null;
  from: "pipeline" | "jobs";
  pipelineEntryId: string | null;
  listingId: string | null;
  /** Review handoff: the existing application to open instead of prefilling a new one. */
  applicationId: string | null;
}

/** What callers write; a prefill handoff may leave out `applicationId`. */
export type DeskHandoffInput = Omit<DeskHandoff, "applicationId"> & { applicationId?: string | null };

/** Editing the URL starts an independent application, not a link to the old card. */
export function applicationLinks(handoff: DeskHandoff | null, applicationUrl: string): { pipelineEntryId?: string; listingId?: string } {
  // A review handoff opens an existing application; it never links a new one.
  if (!handoff || handoff.applicationId || !handoff.applicationUrl) return {};
  if (handoff.applicationUrl.trim() !== applicationUrl.trim()) return {};
  return {
    ...(handoff.pipelineEntryId ? { pipelineEntryId: handoff.pipelineEntryId } : {}),
    ...(handoff.listingId ? { listingId: handoff.listingId } : {}),
  };
}

export function writeHandoff(handoff: DeskHandoffInput, storage: Pick<Storage, "setItem"> = window.sessionStorage) {
  const value: DeskHandoff = { ...handoff, applicationId: handoff.applicationId ?? null };
  storage.setItem(HANDOFF_KEY, JSON.stringify(value));
}

/** Read and clear the handoff so it applies once. */
export function takeHandoff(
  storage: Pick<Storage, "getItem" | "removeItem"> = window.sessionStorage,
): DeskHandoff | null {
  const raw = storage.getItem(HANDOFF_KEY);
  if (!raw) return null;
  storage.removeItem(HANDOFF_KEY);
  try {
    return parseHandoff(JSON.parse(raw));
  } catch {
    return null;
  }
}

/** A prefill handoff needs its link; a review handoff needs its application id. */
export function parseHandoff(raw: unknown): DeskHandoff | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const value = raw as Partial<Record<keyof DeskHandoff, unknown>>;
  const applicationUrl = typeof value.applicationUrl === "string" && value.applicationUrl ? value.applicationUrl : null;
  const applicationId =
    typeof value.applicationId === "string" && value.applicationId.trim() ? value.applicationId : null;
  if (!applicationUrl && !applicationId) return null;
  return {
    applicationUrl,
    company: typeof value.company === "string" ? value.company : null,
    role: typeof value.role === "string" ? value.role : null,
    from: value.from === "jobs" ? "jobs" : "pipeline",
    pipelineEntryId: typeof value.pipelineEntryId === "string" ? value.pipelineEntryId : null,
    listingId: typeof value.listingId === "string" ? value.listingId : null,
    applicationId,
  };
}
