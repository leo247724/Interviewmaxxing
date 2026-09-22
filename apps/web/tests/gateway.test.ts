import { createServer, type IncomingHttpHeaders, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { proxy, type GatewayConfig } from "@/lib/gateway";

const WEB = "http://127.0.0.1:4317";

interface Captured {
  method: string;
  url: string;
  headers: IncomingHttpHeaders;
  body: Buffer;
}

let server: Server;
let backendUrl = "";
let captured: Captured[] = [];
let reply: { status: number; headers: Record<string, string>; body: string } = {
  status: 200,
  headers: { "Content-Type": "application/json" },
  body: "{}",
};

beforeAll(async () => {
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      captured.push({ method: req.method!, url: req.url!, headers: req.headers, body: Buffer.concat(chunks) });
      res.writeHead(reply.status, reply.headers);
      res.end(reply.body);
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  backendUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(() => new Promise<void>((resolve) => server.close(() => resolve())));

beforeEach(() => {
  captured = [];
  reply = { status: 200, headers: { "Content-Type": "application/json" }, body: "{}" };
});

function config(overrides: Partial<GatewayConfig> = {}): GatewayConfig {
  return {
    backendUrl,
    webOrigin: WEB,
    maxUploadBytes: 1024,
    maxJsonBytes: 256,
    maxImportBytes: 4096,
    timeoutMs: 5000,
    ...overrides,
  };
}

function post(path: string, init: RequestInit & { origin?: string | null } = {}) {
  const headers = new Headers(init.headers);
  if (init.origin !== null) headers.set("Origin", init.origin ?? WEB);
  return new Request(`${WEB}/api/imx/${path}`, { ...init, method: "POST", headers });
}

async function errorOf(response: Response) {
  return ((await response.json()) as { error: { code: string; message: string; fieldErrors?: Record<string, string> } })
    .error;
}

describe("gateway mutations", () => {
  it("forwards JSON with the verified origin and exact content type", async () => {
    const body = JSON.stringify({ applicationUrl: "https://jobs.example.test/a" });
    const response = await proxy(
      post("applications", { headers: { "Content-Type": "application/json; charset=utf-8" }, body }),
      ["applications"],
      config(),
    );
    expect(response.status).toBe(200);
    expect(captured).toHaveLength(1);
    const [sent] = captured;
    expect(sent.url).toBe("/applications");
    expect(sent.headers.origin).toBe(WEB);
    expect(sent.headers["content-type"]).toBe("application/json");
    expect(sent.headers["content-length"]).toBe(String(Buffer.byteLength(body)));
    expect(sent.headers.host).toBe(new URL(backendUrl).host);
    expect(sent.body.toString()).toBe(body);
  });

  it.each([
    ["a missing origin", null],
    ["a different origin", "http://evil.example"],
    ["another local port", "http://127.0.0.1:9999"],
    ["localhost instead of 127.0.0.1", "http://localhost:4317"],
  ])("refuses %s before contacting the service", async (_label, origin) => {
    const response = await proxy(
      post("applications", { origin, headers: { "Content-Type": "application/json" }, body: "{}" }),
      ["applications"],
      config(),
    );
    expect(response.status).toBe(403);
    expect(captured).toHaveLength(0);
  });

  it("refuses cross-site fetch metadata even with a matching origin", async () => {
    const response = await proxy(
      post("applications", {
        headers: { "Content-Type": "application/json", "Sec-Fetch-Site": "cross-site" },
        body: "{}",
      }),
      ["applications"],
      config(),
    );
    expect(response.status).toBe(403);
    expect(captured).toHaveLength(0);
  });

  it("derives the expected origin from the request when none is configured", async () => {
    const response = await proxy(
      post("applications", { headers: { "Content-Type": "application/json" }, body: "{}" }),
      ["applications"],
      config({ webOrigin: null }),
    );
    expect(response.status).toBe(200);
    expect(captured[0].headers.origin).toBe(WEB);
  });

  it("requires JSON and bounds its size", async () => {
    const wrongType = await proxy(
      post("applications", { headers: { "Content-Type": "text/plain" }, body: "{}" }),
      ["applications"],
      config(),
    );
    expect(wrongType.status).toBe(415);
    const tooBig = await proxy(
      post("applications", { headers: { "Content-Type": "application/json" }, body: `"${"x".repeat(400)}"` }),
      ["applications"],
      config(),
    );
    expect(tooBig.status).toBe(413);
    const importOk = await proxy(
      post("pipeline/import/preview", {
        headers: { "Content-Type": "application/json" },
        body: `"${"x".repeat(400)}"`,
      }),
      ["pipeline", "import", "preview"],
      config(),
    );
    expect(importOk.status).toBe(200);
    expect(captured).toHaveLength(1);
  });

  it("refuses query strings everywhere", async () => {
    const response = await proxy(new Request(`${WEB}/api/imx/candidate?email=a@b.c`), ["candidate"], config());
    expect(response.status).toBe(400);
    expect(captured).toHaveLength(0);
  });
});

describe("gateway resume uploads", () => {
  it("translates multipart into octet-stream with a percent-encoded filename", async () => {
    const form = new FormData();
    const bytes = new Uint8Array([37, 80, 68, 70, 45, 49, 46, 52]);
    form.append("file", new File([bytes], "Résumé Casey (final).pdf", { type: "application/pdf" }));
    const response = await proxy(post("resumes", { body: form }), ["resumes"], config());
    expect(response.status).toBe(200);
    const [sent] = captured;
    expect(sent.url).toBe("/resumes");
    expect(sent.headers["content-type"]).toBe("application/octet-stream");
    expect(sent.headers["x-imx-filename"]).toBe(encodeURIComponent("Résumé Casey (final).pdf"));
    expect(sent.headers["content-length"]).toBe("8");
    expect(sent.headers.origin).toBe(WEB);
    expect([...sent.body]).toEqual([...bytes]);
  });

  it("rejects empty, oversized and non-multipart uploads with field errors", async () => {
    const empty = new FormData();
    empty.append("file", new File([], "empty.pdf"));
    const emptyResponse = await proxy(post("resumes", { body: empty }), ["resumes"], config());
    expect(emptyResponse.status).toBe(400);
    expect((await errorOf(emptyResponse)).fieldErrors?.resumeFile).toMatch(/empty/);

    const big = new FormData();
    big.append("file", new File([new Uint8Array(2048)], "big.pdf"));
    const bigResponse = await proxy(post("resumes", { body: big }), ["resumes"], config());
    expect(bigResponse.status).toBe(413);
    expect((await errorOf(bigResponse)).fieldErrors?.resumeFile).toMatch(/MB or smaller/);

    const raw = await proxy(
      post("resumes", { headers: { "Content-Type": "application/octet-stream" }, body: "abc" }),
      ["resumes"],
      config(),
    );
    expect(raw.status).toBe(415);
    expect(captured).toHaveLength(0);
  });
});

describe("gateway reads and failures", () => {
  it("passes evidence downloads through with their safety headers", async () => {
    reply = {
      status: 200,
      headers: {
        "Content-Type": "image/png",
        "Content-Disposition": 'inline; filename="confirmation.png"',
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
        "X-Private-Path": "/Users/someone/secret",
      },
      body: "PNG",
    };
    const response = await proxy(
      new Request(`${WEB}/api/imx/applications/app_1/evidence/ev_1`),
      ["applications", "app_1", "evidence", "ev_1"],
      config(),
    );
    expect(response.status).toBe(200);
    expect(captured[0].headers.origin).toBeUndefined();
    expect(response.headers.get("content-type")).toBe("image/png");
    expect(response.headers.get("content-disposition")).toContain("confirmation.png");
    expect(response.headers.get("content-security-policy")).toContain("sandbox");
    expect(response.headers.get("x-private-path")).toBeNull();
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("passes structured service errors through unchanged", async () => {
    reply = {
      status: 404,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ error: { code: "not_found", message: "No such application." } }),
    };
    const response = await proxy(new Request(`${WEB}/api/imx/applications/x`), ["applications", "x"], config());
    expect(response.status).toBe(404);
    expect((await errorOf(response)).code).toBe("not_found");
  });

  it("answers 503 without a configured backend, never a fabricated result", async () => {
    const response = await proxy(new Request(`${WEB}/api/imx/candidate`), ["candidate"], config({ backendUrl: null }));
    expect(response.status).toBe(503);
    expect((await errorOf(response)).code).toBe("unavailable");
  });

  it("answers 503 when the service can't be reached", async () => {
    const response = await proxy(
      new Request(`${WEB}/api/imx/candidate`),
      ["candidate"],
      config({ backendUrl: "http://127.0.0.1:9" }),
    );
    expect(response.status).toBe(503);
    expect((await errorOf(response)).message).toMatch(/isn't running/);
  });

  it("refuses path traversal segments", async () => {
    const response = await proxy(new Request(`${WEB}/api/imx/x`), ["applications", "..", "healthz"], config());
    expect(response.status).toBe(404);
    expect(captured).toHaveLength(0);
  });
});
