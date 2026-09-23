import type { Metadata } from "next";
import { PipelineView } from "@/components/pipeline/PipelineView";

export const metadata: Metadata = { title: "Pipeline · Interviewmaxxing" };

export default function PipelinePage() {
  return <PipelineView mode="live" />;
}
