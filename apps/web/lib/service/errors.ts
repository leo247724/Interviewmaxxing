export type ServiceErrorCode =
  /** The application service could not be reached or is not configured. */
  | "unavailable"
  /** The request was rejected; `fieldErrors` explains which inputs. */
  | "invalid"
  | "not_found"
  /** The request conflicts with saved state, e.g. an unsettled submission. */
  | "conflict"
  | "unknown";

export class ServiceError extends Error {
  readonly code: ServiceErrorCode;
  readonly fieldErrors: Record<string, string>;

  constructor(code: ServiceErrorCode, message: string, fieldErrors: Record<string, string> = {}) {
    super(message);
    this.name = "ServiceError";
    this.code = code;
    this.fieldErrors = fieldErrors;
  }
}

export function asServiceError(error: unknown): ServiceError {
  if (error instanceof ServiceError) return error;
  if (error instanceof TypeError) {
    // fetch() rejects with TypeError when the network or server is unreachable.
    return new ServiceError("unavailable", "The application service could not be reached.");
  }
  return new ServiceError("unknown", "Something unexpected went wrong talking to the application service.");
}
