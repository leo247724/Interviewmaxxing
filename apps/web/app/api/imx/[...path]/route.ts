/**
 * Same-origin gateway for the live application service.
 *
 * Forwards to `IMX_BACKEND_URL` when configured; otherwise answers 503 so the
 * desk reports that nothing can be sent. It never simulates a result and never
 * logs request bodies, which carry personal data.
 */

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

function unavailable(message: string) {
  return Response.json({ error: { code: "unavailable", message } }, { status: 503 });
}

async function forward(request: Request, context: Context) {
  const backend = process.env.IMX_BACKEND_URL?.trim();
  if (!backend) {
    return unavailable("This web app has no application service configured.");
  }

  const { path } = await context.params;
  const target = new URL(path.map(encodeURIComponent).join("/"), backend.endsWith("/") ? backend : `${backend}/`);

  const headers = new Headers({ Accept: "application/json" });
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer(),
      cache: "no-store",
      signal: AbortSignal.timeout(30_000),
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") ?? "application/json",
        "Cache-Control": "no-store",
      },
    });
  } catch (error) {
    const code = (error as { cause?: { code?: string } })?.cause?.code;
    const neverDelivered = code === "ECONNREFUSED" || code === "ENOTFOUND" || code === "EAI_AGAIN";
    if (neverDelivered || request.method === "GET") {
      return unavailable(
        "The application service isn't running or can't be reached. Check that the Interviewmaxxing service is running, then try again.",
      );
    }
    return unavailable(
      "The application service stopped answering after receiving this request. It may still be acting on it; check the application's status before trying again.",
    );
  }
}

export const GET = forward;
export const POST = forward;
