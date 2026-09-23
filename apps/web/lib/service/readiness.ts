import { requestJson } from "./request";

/** Public /healthz data; contains no candidate or application information. */
export interface ServiceReadiness {
  status: "ok";
  executor: "idle" | "busy";
  runner: "available" | "unavailable";
  applicationMode: "TEST_ONLY" | "LIVE";
}

export function getReadiness(): Promise<ServiceReadiness> {
  return requestJson("/api/imx", "GET", "/healthz");
}

export function isLoopbackApplication(value: string): boolean {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return false;
    return url.hostname === "localhost" || url.hostname === "[::1]" || /^127(?:\.\d{1,3}){3}$/.test(url.hostname);
  } catch { return false; }
}

/** This build is for test execution. Real-employer execution needs a later deployment. */
export function executionProblem(readiness: ServiceReadiness | null, applicationUrl: string): string | null {
  if (!readiness) return "Application readiness could not be checked. Reconnect the local service before starting.";
  if (readiness.applicationMode !== "TEST_ONLY") return "Applications are paused in this development workspace. The service must be in TEST_ONLY mode.";
  if (!isLoopbackApplication(applicationUrl)) return "Test mode accepts only a local application page on localhost, 127.0.0.1 or ::1. Real employer applications are disabled.";
  if (readiness.runner !== "available") return "The application browser is not ready. Try this action again after the local service recovers.";
  return null;
}
