import type { Metadata } from "next";
import { PipelineView } from "@/components/pipeline/PipelineView";

export const metadata: Metadata = { title: "Preview · Pipeline · Interviewmaxxing" };

export default function PreviewPipelinePage() {
  return <PipelineView mode="preview" />;
}
