import type { Metadata } from "next";
import { JobsView } from "@/components/jobs/JobsView";

export const metadata: Metadata = { title: "Preview · Jobs · Interviewmaxxing" };

export default function PreviewJobsPage() {
  return <JobsView mode="preview" />;
}
