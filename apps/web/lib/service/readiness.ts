import { requestJson } from "./request";

/** Public /healthz data; contains no candidate or application information. */
export interface ServiceReadiness {
  status: "ok";
  executor: "idle" | "busy";
  runner: "available" | "unavailable";
  applicationMode: "TEST_ONLY" | "LIVE";
  /** The presentation contract version (CONTRACTS.md §12); absent means version 1. */
  presentationVersion?: string;
  /**
   * Whether the service was started with IMX_ALLOW_SUBMISSION=1, so the review lane
   * may submit approved applications after the person confirms. Absent on older
   * services, which never submit from the dashboard.
   */
  submission?: "enabled" | "disabled";
  /** Whether the service's browser opens a visible window or runs headless. Absent on older services. */
  browser?: "visible" | "headless";
}

/** Presentation contract major versions this dashboard reads (CONTRACTS.md §12). */
export const KNOWN_PRESENTATION_MAJORS: readonly number[] = [1, 2];

export interface PresentationSupport {
  /** The version the service reported: "1" when it sent none, null before the check answered. */
  version: string | null;
  /**
   * False for a major version this dashboard doesn't know, or a value it can't read:
   * the prepared panel, review list, lookup select and Prepared marks are then off.
   */
  supported: boolean;
}

/**
 * The presentation version from the readiness check. A check that hasn't answered
 * (or failed) reports no version, so the views' own fields decide as before; only a
 * reported version this dashboard doesn't know turns the version 2 additions off.
 */
export function presentationSupport(
  readiness: Pick<ServiceReadiness, "presentationVersion"> | null | undefined,
): PresentationSupport {
  if (!readiness) return { version: null, supported: true };
  const raw: unknown = readiness.presentationVersion;
  if (raw === undefined || raw === null) return { version: "1", supported: true };
  const version = typeof raw === "string" ? raw.trim() : typeof raw === "number" && Number.isFinite(raw) ? String(raw) : "";
  const major = /^(\d+)(?:\.\d+)*$/.exec(version)?.[1];
  return {
    version: version || "unreadable",
    supported: major !== undefined && KNOWN_PRESENTATION_MAJORS.includes(Number(major)),
  };
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
