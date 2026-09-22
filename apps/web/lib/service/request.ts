import { ServiceError, asServiceError, type ServiceErrorCode } from "./errors";

/**
 * One JSON request through the same-origin `/api/imx` gateway. Bodies are JSON or
 * FormData; nothing personal is ever placed in the URL.
 */
export async function requestJson<T>(
  baseUrl: string,
  method: "GET" | "POST",
  path: string,
  body?: unknown,
): Promise<T> {
  let response: Response;
  try {
    const init: RequestInit = { method, cache: "no-store", headers: { Accept: "application/json" } };
    if (body instanceof FormData) {
      init.body = body;
    } else if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers = { ...init.headers, "Content-Type": "application/json" };
    }
    response = await fetch(`${baseUrl}${path}`, init);
  } catch (error) {
    throw asServiceError(error);
  }

  const payload: unknown = await response.json().catch(() => null);
  if (response.ok) return payload as T;
  throw errorFromResponse(response.status, payload);
}

export function errorFromResponse(status: number, payload: unknown): ServiceError {
  const error = (payload as { error?: { code?: string; message?: string; fieldErrors?: Record<string, string> } })
    ?.error;
  const fallback: ServiceErrorCode =
    status === 503 || status === 502 || status === 504
      ? "unavailable"
      : status === 400 || status === 403 || status === 413 || status === 415 || status === 422
        ? "invalid"
        : status === 404
          ? "not_found"
          : status === 409
            ? "conflict"
            : "unknown";
  const code = isCode(error?.code) ? error.code : fallback;
  const message = error?.message ?? `The application service answered with HTTP ${status}.`;
  return new ServiceError(code, message, error?.fieldErrors ?? {});
}

function isCode(value: unknown): value is ServiceErrorCode {
  return (
    value === "unavailable" ||
    value === "invalid" ||
    value === "not_found" ||
    value === "conflict" ||
    value === "unknown"
  );
}
