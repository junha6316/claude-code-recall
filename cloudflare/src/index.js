// Worker router: fronts the recall container.
// UNTESTED SCAFFOLD — see wrangler.jsonc.
//
// Concurrency design: ONE container instance per tenant. The Durable Object
// id is derived from the bearer token's tenant, so every request for a tenant
// lands on the same instance — this is what makes "local disk = cache-of-
// record while awake" (r2.py) safe without cross-instance locks.
//
// Phase 2 MVP routes on a hash of the token itself (no Worker-side D1 lookup
// yet). Distinct tokens for the same tenant would map to distinct instances —
// acceptable while tenants have exactly one token; replace with a D1
// token→tenant lookup when multi-token support lands.
import { Container, getContainer } from "@cloudflare/containers";

export class RecallContainer extends Container {
  defaultPort = 8300;
  sleepAfter = "15m";   // > build debounce, so a build isn't cut mid-flight
}

async function tenantKey(request) {
  const auth = request.headers.get("authorization") || "";
  if (!auth.startsWith("Bearer ")) return null;
  const data = new TextEncoder().encode(auth.slice(7).trim());
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(request, env) {
    const key = await tenantKey(request);
    if (key === null) {
      return new Response("invalid or missing bearer token", { status: 401 });
    }
    // Same tenant token → same DO id → same container instance.
    const container = getContainer(env.RECALL_CONTAINER, key);
    return container.fetch(request);
  },
};
