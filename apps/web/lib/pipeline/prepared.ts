import { isPrepared } from "../preparation";
import { asServiceError, type ServiceError } from "../service/errors";
import type { PresentationSupport } from "../service/readiness";
import type { ApplicationListView, ApplicationService, ApplicationSummaryView } from "../service/types";
import type { PipelineEntryView } from "./types";

/**
 * Joins pipeline cards to prepared applications (`GET /applications`). A prepared
 * application was filled and stopped at the final review step; nothing was
 * submitted. The join only drives the Prepared mark, the "Prepared for review"
 * filter and the Review action. It never shows a receipt or links a card.
 */

/** Card id -> the prepared application it points at. */
export type PreparedByEntry = ReadonlyMap<string, ApplicationSummaryView>;

/** A prepared stop: NEEDS_INPUT with a ready, unsubmitted preparation, the desk's own check. */
export { isPrepared };

/**
 * The Closed lane of the default board (ended, withdrawn or declined). A card there
 * is never marked prepared or offered for review, whatever its application's state.
 */
export const CLOSED_LANE = "closed";

/**
 * The summaries the board may mark cards from: none when the service reports a
 * presentation version this dashboard doesn't read.
 */
export function markableApplications(
  applications: readonly ApplicationSummaryView[],
  presentation: PresentationSupport,
): readonly ApplicationSummaryView[] {
  return presentation.supported ? applications : [];
}

/**
 * The summaries of a list response, or none when it isn't the expected shape: an
 * older or misbehaving service must never produce a Prepared mark.
 */
export function listedApplications(list: ApplicationListView | null | undefined): ApplicationSummaryView[] {
  const applications: unknown = (list as { applications?: unknown } | null | undefined)?.applications;
  if (!Array.isArray(applications)) return [];
  return applications.filter(
    (item): item is ApplicationSummaryView =>
      typeof item === "object" && item !== null && typeof (item as { id?: unknown }).id === "string",
  );
}

/**
 * Load the application list for the board. Any failure (an older service without
 * the route, 404/405, the service unavailable) yields no applications, so no
 * card is marked, plus the error for a quiet note.
 */
export async function loadApplicationSummaries(
  service: Pick<ApplicationService, "list">,
): Promise<{ applications: ApplicationSummaryView[]; error: ServiceError | null }> {
  try {
    return { applications: listedApplications(await service.list()), error: null };
  } catch (error) {
    return { applications: [], error: asServiceError(error) };
  }
}

/**
 * One quiet line for the board when the list couldn't be read or can't be trusted
 * (a presentation version this dashboard doesn't read); never an alarm.
 */
export function preparedListNote(error: ServiceError | null, presentation?: PresentationSupport): string | null {
  if (presentation && !presentation.supported) {
    return `This service reports applications in a format this dashboard doesn't read (presentation version ${presentation.version}), so none are marked prepared.`;
  }
  if (!error) return null;
  return error.code === "not_found"
    ? "This service doesn't report prepared applications yet, so none are marked."
    : "Prepared applications couldn't be checked just now, so none are marked.";
}

/**
 * Card id -> prepared application, for the cards on the board. A card counts when
 * it is linked to the prepared application or is listed in its `pipelineEntryIds`
 * (the service lists linked cards and unlinked cards whose application link
 * resolves to it). The card's own linked application wins when both apply. A
 * card linked to a submitted application keeps its receipt and is never marked,
 * and neither is a card in the Closed lane.
 */
export function preparedByEntry(
  entries: readonly PipelineEntryView[],
  applications: readonly ApplicationSummaryView[],
): Map<string, ApplicationSummaryView> {
  const found = new Map<string, ApplicationSummaryView>();
  const prepared = applications.filter(isPrepared);
  if (prepared.length === 0) return found;
  const byId = new Map(prepared.map((summary) => [summary.id, summary]));
  for (const entry of entries) {
    if (entry.lane === CLOSED_LANE || entry.application?.state === "SUBMITTED") continue;
    const summary =
      (entry.application ? byId.get(entry.application.applicationId) : undefined) ??
      // The service lists the most recently updated first.
      prepared.find(
        (item) => Array.isArray(item.pipelineEntryIds) && item.pipelineEntryIds.includes(entry.id),
      );
    if (summary) found.set(entry.id, summary);
  }
  return found;
}

/** The mark on a prepared card. */
export function preparedLabel(summary: Pick<ApplicationSummaryView, "preparation">): string {
  return `Prepared for review${summary.preparation?.captchaPending ? " · CAPTCHA to solve" : ""}`;
}

export type PipelineFocus = "all" | "actions" | "interviews" | "prepared";

export interface FocusContext {
  prepared: PreparedByEntry;
  /** Interview today or later (America/Chicago); the caller knows today's date. */
  upcoming: (entry: PipelineEntryView) => boolean;
}

export function matchesFocus(entry: PipelineEntryView, focus: PipelineFocus, context: FocusContext): boolean {
  switch (focus) {
    case "all":
      return true;
    case "actions":
      return Boolean(entry.fields.nextAction);
    case "interviews":
      return context.upcoming(entry);
    case "prepared":
      return context.prepared.has(entry.id);
  }
}

/** Card counts for each focus filter. */
export function focusCounts(
  entries: readonly PipelineEntryView[],
  context: FocusContext,
): Record<PipelineFocus, number> {
  const count = (focus: PipelineFocus) => entries.filter((entry) => matchesFocus(entry, focus, context)).length;
  return { all: entries.length, actions: count("actions"), interviews: count("interviews"), prepared: count("prepared") };
}

/** Cards matching the text filter (company, role, stage or status) and the focus filter. */
export function visibleEntries(
  entries: readonly PipelineEntryView[],
  query: string,
  focus: PipelineFocus,
  context: FocusContext,
): PipelineEntryView[] {
  const needle = query.trim().toLowerCase();
  return entries.filter(
    (entry) =>
      (!needle ||
        [entry.fields.company, entry.fields.role, entry.fields.stage, entry.fields.status].some(
          (value) => value?.toLowerCase().includes(needle) ?? false,
        )) &&
      matchesFocus(entry, focus, context),
  );
}
