/**
 * Same-origin gateway to the local Interviewmaxxing service. The rules live in
 * `lib/gateway.ts`: exact-origin mutations, upload translation, bounded bodies and
 * an honest 503 when no service is configured or reachable.
 */

import { gatewayConfigFromEnv, proxy } from "@/lib/gateway";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

async function handle(request: Request, context: Context) {
  const { path } = await context.params;
  return proxy(request, path, gatewayConfigFromEnv());
}

export const GET = handle;
export const POST = handle;
