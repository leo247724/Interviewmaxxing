import type { Metadata } from "next";
import { ApplicationDesk } from "@/components/ApplicationDesk";

export const metadata: Metadata = {
  title: "Preview · Application desk · Interviewmaxxing",
};

export default async function PreviewPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { scenario } = await searchParams;
  return <ApplicationDesk mode="preview" initialScenario={typeof scenario === "string" ? scenario : undefined} />;
}
