import { asServiceError } from "./service/errors";
import type { ApplicationService, ApplicationView } from "./service/types";

export const ACTIVE_ID_KEY = "imx.activeApplicationId";

export type RestoreOutcome =
  | { kind: "none" }
  | { kind: "restored"; applicationId: string; view: ApplicationView }
  /** The service definitively has no such application; the saved id was cleared. */
  | { kind: "gone"; applicationId: string }
  /** Status could not be read. The id is kept so a later attempt can restore it. */
  | { kind: "pending"; applicationId: string; code: string; message: string };

/**
 * Reload the application this browser session was following. An application
 * may be mid-submission, so its id is only forgotten on a definitive
 * `not_found`; any other failure keeps it for a retry.
 */
export async function restoreActiveApplication(
  service: Pick<ApplicationService, "status">,
  storage: Pick<Storage, "getItem" | "removeItem">,
): Promise<RestoreOutcome> {
  const applicationId = storage.getItem(ACTIVE_ID_KEY);
  if (!applicationId) return { kind: "none" };
  try {
    return { kind: "restored", applicationId, view: await service.status(applicationId) };
  } catch (error) {
    const serviceError = asServiceError(error);
    if (serviceError.code === "not_found") {
      // Only forget it if nothing newer has been saved while the request was in flight.
      if (storage.getItem(ACTIVE_ID_KEY) === applicationId) storage.removeItem(ACTIVE_ID_KEY);
      return { kind: "gone", applicationId };
    }
    return { kind: "pending", applicationId, code: serviceError.code, message: serviceError.message };
  }
}
