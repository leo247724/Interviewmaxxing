import { PreviewPipelineService } from "./preview";

/**
 * One fictional tracker per browser tab, shared by the preview Pipeline and Jobs
 * views so a job tracked from search shows up on the preview board.
 */
let shared: PreviewPipelineService | null = null;

export function previewPipeline(): PreviewPipelineService {
  shared ??= new PreviewPipelineService();
  return shared;
}
