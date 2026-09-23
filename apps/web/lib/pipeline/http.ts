import { requestJson } from "../service/request";
import type {
  ImportInput,
  ImportPreviewView,
  ImportReceiptView,
  PipelineBoardView,
  PipelineEntryInput,
  PipelineEntryView,
  PipelineMoveInput,
  PipelineService,
  PipelineUpdateInput,
} from "./types";

/** HTTP binding of {@link PipelineService}; routes are documented in apps/web/README.md. */
export class HttpPipelineService implements PipelineService {
  readonly mode = "live" as const;

  constructor(private readonly baseUrl = "/api/imx") {}

  board(): Promise<PipelineBoardView> {
    return requestJson(this.baseUrl, "GET", "/pipeline");
  }

  create(input: PipelineEntryInput): Promise<PipelineEntryView> {
    return requestJson(this.baseUrl, "POST", "/pipeline/entries", input);
  }

  update(entryId: string, input: PipelineUpdateInput): Promise<PipelineEntryView> {
    return requestJson(this.baseUrl, "POST", `/pipeline/entries/${encodeURIComponent(entryId)}`, input);
  }

  move(entryId: string, input: PipelineMoveInput): Promise<PipelineEntryView> {
    return requestJson(this.baseUrl, "POST", `/pipeline/entries/${encodeURIComponent(entryId)}/move`, input);
  }

  previewImport(input: ImportInput): Promise<ImportPreviewView> {
    return requestJson(this.baseUrl, "POST", "/pipeline/import/preview", input);
  }

  commitImport(previewId: string): Promise<ImportReceiptView> {
    return requestJson(this.baseUrl, "POST", `/pipeline/import/${encodeURIComponent(previewId)}/commit`, {});
  }
}
