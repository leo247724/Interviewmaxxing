import { requestJson } from "../service/request";
import type { AnswerInput, ApplicationView } from "../service/types";
import type {
  ApplicationReviewView,
  ApproveInput,
  ReviewQueueView,
  ReviewService,
  SubmitInput,
} from "./types";

/**
 * HTTP binding of {@link ReviewService} through the same-origin `/api/imx` gateway.
 * Application ids travel in the path (they are opaque ids, never personal data);
 * everything else travels in request bodies.
 */
export class HttpReviewService implements ReviewService {
  readonly mode = "live" as const;

  constructor(private readonly baseUrl = "/api/imx") {}

  queue(): Promise<ReviewQueueView> {
    return requestJson(this.baseUrl, "GET", "/review");
  }

  review(applicationId: string): Promise<ApplicationReviewView> {
    return requestJson(this.baseUrl, "GET", `${this.path(applicationId)}/review`);
  }

  approve(applicationId: string, input: ApproveInput): Promise<ApplicationReviewView> {
    return requestJson(this.baseUrl, "POST", `${this.path(applicationId)}/approve`, input);
  }

  submit(applicationId: string, input: SubmitInput): Promise<ApplicationReviewView> {
    return requestJson(this.baseUrl, "POST", `${this.path(applicationId)}/submit`, input);
  }

  editAnswer(applicationId: string, input: AnswerInput): Promise<ApplicationView> {
    return requestJson(this.baseUrl, "POST", `${this.path(applicationId)}/answers`, input);
  }

  prepareAgain(applicationId: string): Promise<ApplicationView> {
    return requestJson(this.baseUrl, "POST", `${this.path(applicationId)}/resume`, {});
  }

  answer(applicationId: string, input: AnswerInput): Promise<ApplicationView> {
    return requestJson(this.baseUrl, "POST", `${this.path(applicationId)}/answers`, input);
  }

  private path(applicationId: string): string {
    return `/applications/${encodeURIComponent(applicationId)}`;
  }
}
