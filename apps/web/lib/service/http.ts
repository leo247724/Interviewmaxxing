import { ServiceError, asServiceError, type ServiceErrorCode } from "./errors";
import type {
  AnswerInput,
  ApplicationService,
  ApplicationView,
  CandidateView,
  ReconcileInput,
  ResumeDocumentView,
  StartApplicationInput,
} from "./types";

/**
 * HTTP binding of {@link ApplicationService}. Requests go to the same-origin
 * `/api/imx` route, which forwards to `IMX_BACKEND_URL` or answers 503 when no
 * backend is configured. Personal data travels only in request bodies.
 */
export class HttpApplicationService implements ApplicationService {
  readonly mode = "live" as const;

  constructor(private readonly baseUrl = "/api/imx") {}

  getCandidate(): Promise<CandidateView> {
    return this.request("GET", "/candidate");
  }

  uploadResume(file: File): Promise<ResumeDocumentView> {
    const body = new FormData();
    body.append("file", file);
    return this.request("POST", "/resumes", body);
  }

  start(input: StartApplicationInput): Promise<ApplicationView> {
    return this.request("POST", "/applications", input);
  }

  status(applicationId: string): Promise<ApplicationView> {
    return this.request("GET", `/applications/${encodeURIComponent(applicationId)}`);
  }

  answer(applicationId: string, input: AnswerInput): Promise<ApplicationView> {
    return this.request("POST", `/applications/${encodeURIComponent(applicationId)}/answers`, input);
  }

  resume(applicationId: string): Promise<ApplicationView> {
    return this.request("POST", `/applications/${encodeURIComponent(applicationId)}/resume`, {});
  }

  reconcile(applicationId: string, input: ReconcileInput): Promise<ApplicationView> {
    return this.request("POST", `/applications/${encodeURIComponent(applicationId)}/reconcile`, input);
  }

  private async request<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
    let response: Response;
    try {
      const init: RequestInit = { method, cache: "no-store", headers: { Accept: "application/json" } };
      if (body instanceof FormData) {
        init.body = body;
      } else if (body !== undefined) {
        init.body = JSON.stringify(body);
        init.headers = { ...init.headers, "Content-Type": "application/json" };
      }
      response = await fetch(`${this.baseUrl}${path}`, init);
    } catch (error) {
      throw asServiceError(error);
    }

    const payload: unknown = await response.json().catch(() => null);
    if (response.ok) return payload as T;
    throw errorFromResponse(response.status, payload);
  }
}

function errorFromResponse(status: number, payload: unknown): ServiceError {
  const error = (payload as { error?: { code?: string; message?: string; fieldErrors?: Record<string, string> } })
    ?.error;
  const fallback: ServiceErrorCode =
    status === 503 || status === 502 || status === 504
      ? "unavailable"
      : status === 400 || status === 422
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
