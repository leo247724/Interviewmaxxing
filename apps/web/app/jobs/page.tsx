import type { Metadata } from "next";
import { JobsView } from "@/components/jobs/JobsView";

export const metadata: Metadata = { title: "Jobs · Interviewmaxxing" };

export default function JobsPage() {
  return <JobsView mode="live" />;
}
