import type { Metadata } from "next";
import { ReviewQueue } from "@/components/review/ReviewQueue";

export const metadata: Metadata = { title: "Review · Interviewmaxxing" };

export default function ReviewQueuePage() {
  return <ReviewQueue mode="live" />;
}
