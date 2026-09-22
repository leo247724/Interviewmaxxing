/**
 * Carries a chosen job from the Pipeline or Jobs view to the application desk.
 * The desk only prefills the link; the user still presses Apply, which is the
 * submission authorization. Nothing here starts an application.
 */

const HANDOFF_KEY = "imx.deskHandoff";

export interface DeskHandoff {
  applicationUrl: string;
  company: string | null;
  role: string | null;
  from: "pipeline" | "jobs";
  pipelineEntryId: string | null;
  listingId: string | null;
}

export function writeHandoff(handoff: DeskHandoff, storage: Pick<Storage, "setItem"> = window.sessionStorage) {
  storage.setItem(HANDOFF_KEY, JSON.stringify(handoff));
}

/** Read and clear the handoff so it applies once. */
export function takeHandoff(
  storage: Pick<Storage, "getItem" | "removeItem"> = window.sessionStorage,
): DeskHandoff | null {
  const raw = storage.getItem(HANDOFF_KEY);
  if (!raw) return null;
  storage.removeItem(HANDOFF_KEY);
  try {
    const value = JSON.parse(raw) as Partial<DeskHandoff>;
    if (typeof value.applicationUrl !== "string" || !value.applicationUrl) return null;
    return {
      applicationUrl: value.applicationUrl,
      company: typeof value.company === "string" ? value.company : null,
      role: typeof value.role === "string" ? value.role : null,
      from: value.from === "jobs" ? "jobs" : "pipeline",
      pipelineEntryId: typeof value.pipelineEntryId === "string" ? value.pipelineEntryId : null,
      listingId: typeof value.listingId === "string" ? value.listingId : null,
    };
  } catch {
    return null;
  }
}
