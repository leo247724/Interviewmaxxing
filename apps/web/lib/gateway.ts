/**
 * Same-origin gateway between the browser and the local Interviewmaxxing service (S1).
 *
 * - Mutations (POST) must carry `Origin` exactly equal to this app's origin and must
 *   not be `Sec-Fetch-Site: cross-site`. The verified origin is sent upstream, where S1
 *   checks it against `IMX_SERVICE_ORIGIN`.
 * - Browser multipart resume uploads become S1's raw transport:
 *   `application/octet-stream` plus a percent-encoded `X-Imx-Filename` header.
 * - JSON bodies are bounded and passed through byte for byte. Query strings are refused.
 * - With no backend configured, or when it can't be reached, the gateway answers 503.
 *   It never invents a result and never logs bodies, ids or URLs.
 */

export const FILENAME_HEADER = "X-Imx-Filename";

export interface GatewayConfig {
  /** S1 base URL, e.g. http://127.0.0.1:8765. Null when not configured. */
  backendUrl: string | null;
  /** This app's exact origin, e.g. http://127.0.0.1:4317. Null: derive from the request. */
  webOrigin: string | null;
  maxUploadBytes: number;
  maxJsonBytes: number;
  maxImportBytes: number;
  timeoutMs: number;
}

export function gatewayConfigFromEnv(env: Record<string, string | undefined> = process.env): GatewayConfig {
  const upload = Number(env.IMX_WEB_MAX_UPLOAD);
  return {
    backendUrl: env.IMX_BACKEND_URL?.trim() || null,
    webOrigin: normalizeOrigin(env.IMX_WEB_ORIGIN),
    maxUploadBytes: Number.isFinite(upload) && upload > 0 ? upload : 10 * 1024 * 1024,
    maxJsonBytes: 64 * 1024,
    maxImportBytes: 2 * 1024 * 1024,
    timeoutMs: 30_000,
  };
}

export function normalizeOrigin(value: string | undefined | null): string | null {
  if (!value?.trim()) return null;
  try {
    const url = new URL(value.trim());
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.origin;
  } catch {
    return null;
  }
}

type ErrorCode = "unavailable" | "invalid" | "not_found" | "conflict" | "unknown";

export function errorResponse(status: number, code: ErrorCode, message: string, fieldErrors?: Record<string, string>) {
  return Response.json(
    { error: fieldErrors ? { code, message, fieldErrors } : { code, message } },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

const PASSED_HEADERS = ["content-type", "content-disposition", "content-security-policy", "x-content-type-options"];

/** Paths whose JSON bodies may be larger than ordinary requests (tracker imports). */
function isImportPath(path: string[]) {
  return path[0] === "pipeline" && path.includes("import");
}

export async function proxy(
  request: Request,
  path: string[],
  config: GatewayConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  const incoming = new URL(request.url);
  if (incoming.search) {
    return errorResponse(400, "invalid", "Query strings are not accepted; send data in the request body.");
  }
  if (path.length === 0 || path.some((segment) => !segment || segment === "." || segment === "..")) {
    return errorResponse(404, "not_found", "No such route.");
  }

  const expectedOrigin = config.webOrigin ?? incoming.origin;
  const origin = request.headers.get("origin");
  if (request.method === "POST") {
    if (origin !== expectedOrigin) {
      return errorResponse(403, "invalid", "Changes are only accepted from the Interviewmaxxing app itself.");
    }
    if (request.headers.get("sec-fetch-site")?.toLowerCase() === "cross-site") {
      return errorResponse(403, "invalid", "Cross-site requests are not accepted.");
    }
  } else if (origin !== null && origin !== expectedOrigin) {
    return errorResponse(403, "invalid", "Requests from that origin are not accepted.");
  }

  if (!config.backendUrl) {
    return errorResponse(503, "unavailable", "This web app has no application service configured.");
  }

  const base = config.backendUrl.endsWith("/") ? config.backendUrl : `${config.backendUrl}/`;
  const target = new URL(path.map(encodeURIComponent).join("/"), base);
  const headers = new Headers({ Accept: "application/json" });
  let body: Uint8Array | undefined;

  if (request.method === "POST") {
    headers.set("Origin", expectedOrigin);
    const contentType = (request.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
    const declared = Number(request.headers.get("content-length") ?? "0");

    if (path.length === 1 && path[0] === "resumes") {
      if (contentType !== "multipart/form-data") {
        return errorResponse(415, "invalid", "Upload the resume as a file.", {
          resumeFile: "Choose a file to upload.",
        });
      }
      if (declared > config.maxUploadBytes + 64 * 1024) return uploadTooLarge(config);
      let file: FormDataEntryValue | null;
      try {
        file = (await request.formData()).get("file");
      } catch {
        return errorResponse(400, "invalid", "The upload couldn't be read.", { resumeFile: "Upload the file again." });
      }
      if (!(file instanceof Blob) || !("name" in file)) {
        return errorResponse(400, "invalid", "No file was attached.", { resumeFile: "Choose a file to upload." });
      }
      if (file.size === 0) {
        return errorResponse(400, "invalid", "That file is empty.", { resumeFile: "That file is empty." });
      }
      if (file.size > config.maxUploadBytes) return uploadTooLarge(config);
      body = new Uint8Array(await file.arrayBuffer());
      headers.set("Content-Type", "application/octet-stream");
      headers.set(FILENAME_HEADER, encodeURIComponent((file as File).name || "resume"));
    } else {
      if (contentType !== "application/json") {
        return errorResponse(415, "invalid", "Send JSON with Content-Type: application/json.");
      }
      const limit = isImportPath(path) ? config.maxImportBytes : config.maxJsonBytes;
      if (declared > limit) return errorResponse(413, "invalid", "The request body is too large.");
      body = new Uint8Array(await request.arrayBuffer());
      if (body.byteLength > limit) return errorResponse(413, "invalid", "The request body is too large.");
      headers.set("Content-Type", "application/json");
    }
  }

  let upstream: Response;
  try {
    upstream = await fetchImpl(target, {
      method: request.method,
      headers,
      body: body as BodyInit | undefined,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(config.timeoutMs),
    });
  } catch (error) {
    const code = (error as { cause?: { code?: string } })?.cause?.code;
    const neverDelivered = code === "ECONNREFUSED" || code === "ENOTFOUND" || code === "EAI_AGAIN";
    if (neverDelivered || request.method === "GET") {
      return errorResponse(
        503,
        "unavailable",
        "The application service isn't running or can't be reached. Check that the Interviewmaxxing service is running, then try again.",
      );
    }
    return errorResponse(
      503,
      "unavailable",
      "The application service stopped answering after receiving this request. It may still be acting on it; check its status before trying again.",
    );
  }

  const out = new Headers({ "Cache-Control": "no-store" });
  for (const name of PASSED_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  if (!out.has("content-type")) out.set("Content-Type", "application/json");
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

function uploadTooLarge(config: GatewayConfig) {
  const mb = Math.round(config.maxUploadBytes / (1024 * 1024));
  return errorResponse(413, "invalid", `Resumes must be ${mb} MB or smaller.`, {
    resumeFile: `Resumes must be ${mb} MB or smaller.`,
  });
}
