import type { Metadata } from "next";
import { ReviewQueue } from "@/components/review/ReviewQueue";

export const metadata: Metadata = { title: "Preview · Review · Interviewmaxxing" };

export default function PreviewReviewQueuePage() {
  return <ReviewQueue mode="preview" />;
}
