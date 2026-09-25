import type { Metadata } from "next";
import { ReviewPage } from "@/components/review/ReviewPage";

export const metadata: Metadata = { title: "Review an application · Interviewmaxxing" };

export default async function ReviewApplicationPage({ params }: { params: Promise<{ applicationId: string }> }) {
  const { applicationId } = await params;
  return <ReviewPage mode="live" applicationId={applicationId} />;
}
