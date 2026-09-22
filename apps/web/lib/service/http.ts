import { requestJson } from "./request";
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

  private request<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
    return requestJson<T>(this.baseUrl, method, path, body);
  }
}
