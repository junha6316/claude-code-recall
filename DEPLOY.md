# Self-hosting the recall server on Cloudflare

Run your own recall server — your transcripts go to **your** Cloudflare account,
nobody else's. The stack: a Worker routes each bearer token to its own container
(FastAPI app), R2 is the durable store, the container disk is just a cache.

Cost ballpark: Workers Paid plan ($5/mo, required for Containers) + container
compute while awake (sleeps after 15 min idle) + R2 storage (cents) + your
Anthropic API usage for summaries.

## Prerequisites

- Cloudflare account on the **Workers Paid** plan (Containers requirement)
- `node`/`npm`, Docker (builds the container image locally), Python 3.9+
- An Anthropic API key for the summary LLM

## 1. Create the R2 bucket and credentials

In the Cloudflare dashboard: **R2 → Create bucket** (e.g. `recall-data`), then
**Manage R2 API Tokens → Create API Token** (Object Read & Write, scoped to the
bucket). Note the Access Key ID, Secret Access Key, and your account's S3
endpoint (`https://<account_id>.r2.cloudflarestorage.com`).

## 2. Pick a tenant id and token

The server seeds one tenant from an env var on every cold start (the container
disk is ephemeral, so the tenant DB is rebuilt from this seed):

```
RECALL_SEED_TENANT="<tenant_id>:<timezone>:<summary_language>:<token>"
# example: junha:Asia/Seoul:Korean:rcl_<48 hex chars>
```

Generate a token with `python3 -c "import secrets; print('rcl_' + secrets.token_hex(24))"`.
Timezone must be an IANA name (`Asia/Seoul`, `America/New_York`, …).

## 3. Deploy

```bash
cd cloudflare
npm install
npx wrangler secret put RECALL_R2_BUCKET      # e.g. recall-data
npx wrangler secret put RECALL_R2_ENDPOINT    # https://<account_id>.r2.cloudflarestorage.com
npx wrangler secret put RECALL_R2_ACCESS_KEY
npx wrangler secret put RECALL_R2_SECRET_KEY
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put RECALL_SEED_TENANT
npx wrangler deploy
```

`wrangler deploy` builds the container image with your local Docker and pushes
it to Cloudflare's registry. The Worker URL it prints is your server.

Verify:

```bash
TOKEN=rcl_...; URL=https://recall-server.<your-subdomain>.workers.dev
curl -s -H "Authorization: Bearer $TOKEN" "$URL/v1/ui/timeline"   # {"dates":[]}
```

## 4. Upload your transcripts

```bash
RECALL_SERVER_URL=$URL RECALL_TOKEN=$TOKEN python3 client/recall-upload.py
```

Uploads are incremental (per-file offsets) and capped at 256 MB/tenant/day, so
a large history may take a couple of daily runs. Each upload debounces a
server-side build (timeline → threads → consolidate → rollup) using your API key.

If you already ran the local pipeline, push its finished outputs so the server
starts with history instead of re-summarizing everything:

```bash
RECALL_SERVER_URL=$URL RECALL_TOKEN=$TOKEN python3 client/recall-push-outputs.py
```

To upload automatically, add a debounced `Stop` hook to `settings.json`:

```json
{"hooks": [{"type": "command", "async": true, "timeout": 600, "command":
  "M=\"$HOME/.claude/scripts/.recall-upload-hook.stamp\"; if [ -n \"$(find \"$M\" -mmin -10 2>/dev/null)\" ]; then exit 0; fi; touch \"$M\"; RECALL_SERVER_URL=<url> RECALL_TOKEN=<token> python3 <repo>/client/recall-upload.py >> \"$HOME/.claude/scripts/recall-upload.log\" 2>&1 || true"}]}
```

## 5. Use it

- **MCP** (from any machine): `claude mcp add --transport http recall "$URL/mcp/" --header "Authorization: Bearer $TOKEN"` — note the **trailing slash** (`/mcp` redirects with the wrong scheme behind the Worker). Tools: `recall_search`, `recall_raw`.
- **Web UI**: open `$URL/ui`, paste the token (stored in localStorage).

## Troubleshooting

- **First request after idle is slow / times out.** The container sleeps after
  15 min; the next request restores the tenant from R2 (parallel, but still
  ~minutes for a large corpus). Use a generous client timeout and **do not
  poll with short timeouts** — one un-cancelled request is the right way to
  wait. Requests during the restore all join the same pull.
- **`The container is not running` 500s that survive redeploys.** The
  platform can wedge an instance while the Durable Object holds a stale
  'healthy' state (DO storage outlives deploys). Escape hatch:
  `curl -X POST -H "Authorization: Bearer $TOKEN" "$URL/admin/reset-container"`
  — destroys only *your* container; the next request restarts it.
- **403 on uploads from custom clients.** Cloudflare bot protection rejects
  default Python user agents. The bundled clients send `User-Agent:
  recall-upload/1.0`; do the same in anything you write.
- **Redeploys wipe the container disk.** That's by design — R2 is the store.
  Expect one cold restore after each deploy; nothing is lost.
- **429 daily ingest byte cap reached.** Per-tenant cost/abuse guard
  (256 MB/day; single request cap 8 MB, LLM calls 500/day). Resume tomorrow —
  the uploader continues where it stopped.

## Security notes

- Transcripts contain whatever your sessions contained — secrets, keys, PII.
  Self-hosting keeps them in your account, but treat the bearer token like a
  password: it grants read/write to everything, including tenant deletion.
- The Worker routes *any* bearer token to a container keyed by its hash; only
  the seeded token authenticates inside the app. Rotate by updating
  `RECALL_SEED_TENANT` and redeploying (old data migrates on the next cold
  pull only if the tenant_id stays the same).
