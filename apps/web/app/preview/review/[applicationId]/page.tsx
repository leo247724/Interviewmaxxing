import type { Metadata } from "next";
import { ReviewPage } from "@/components/review/ReviewPage";

export const metadata: Metadata = { title: "Preview · Review an application · Interviewmaxxing" };

export default async function PreviewReviewApplicationPage({
  params,
}: {
  params: Promise<{ applicationId: string }>;
}) {
  const { applicationId } = await params;
  return <ReviewPage mode="preview" applicationId={applicationId} />;
}
