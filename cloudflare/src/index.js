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
// Single source with the Python app (recall_server/ui.py reads the same file).
// Served from the Worker because an unauthenticated request has no tenant key
// to route a container by — and booting billable containers for anonymous
// hits would hand scanners a cost lever.
import UI_PAGE from "../../server/recall_server/ui_page.html";

export class RecallContainer extends Container {
  defaultPort = 8300;
  sleepAfter = "15m";   // > build debounce, so a build isn't cut mid-flight

  // Worker secrets (wrangler secret put ...) forwarded into the container.
  // RECALL_SEED_TENANT rides along because the container's tenants.db is
  // rebuilt on every cold start (see recall_server/app.py seed bridge).
  envVars = {
    RECALL_R2_BUCKET: this.env.RECALL_R2_BUCKET,
    RECALL_R2_ENDPOINT: this.env.RECALL_R2_ENDPOINT,
    RECALL_R2_ACCESS_KEY: this.env.RECALL_R2_ACCESS_KEY,
    RECALL_R2_SECRET_KEY: this.env.RECALL_R2_SECRET_KEY,
    ANTHROPIC_API_KEY: this.env.ANTHROPIC_API_KEY,
    // Optional: point the SDK at a proxy (tunnel-Meridian mode) instead of
    // api.anthropic.com — subscription LLM without an API key.
    ANTHROPIC_BASE_URL: this.env.ANTHROPIC_BASE_URL,
    RECALL_SEED_TENANT: this.env.RECALL_SEED_TENANT,
  };
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
    if (new URL(request.url).pathname === "/ui" && request.method === "GET") {
      return new Response(UI_PAGE, {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }
    const key = await tenantKey(request);
    if (key === null) {
      return new Response("invalid or missing bearer token", { status: 401 });
    }
    // Same tenant token → same DO id → same container instance.
    const container = getContainer(env.RECALL_CONTAINER, key);
    return container.fetch(request);
  },
};
