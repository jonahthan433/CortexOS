# cortexcloud-api-dev

# CortexCloud API Category Development

CortexCloud (CortexCloudAPI repo) is an agent-native API platform. Each
capability ("category") is a FastAPI router behind a feature flag, with
provider abstraction, x402/MPP payment, and a PostgreSQL usage ledger.
This skill captures the proven pattern for adding a category end-to-end and
validating it on staging with **real paid USDC** before any production merge.

## Architecture facts (verified this session)
- **Repo / deploy**: `CortexCloudAPI` on `192.168.1.15` (CT105), systemd
  `cortexcloud.service` (API, :8000) + `cortexcloud-mcp.service` (MCP, :3100,
  separate `/opt/cortexcloud-mcp/mcp-bundle.cjs`). Venv `/opt/cortexcloud-venv`.
  Postgres `cortexcloud-db` docker on `127.0.0.1:5433` only (NOT public).
  Redis on `127.0.0.1:6379` only. Production secrets in
  `/etc/cortexcloud/cortexcloud.env` (mode 600; `EnvironmentFile=` in unit).
  Staging = the test bed; production is the same host. NEVER merge/deploy
  without explicit approval. Build on a branch (e.g. `ml-api`), keep
  `ML_ENABLED=false` etc. until validated.
- **Feature flag**: routes self-gate via `settings.<CAT>_ENABLED` → return
  `503` when off. Router is always mounted; the flag only flips runtime
  behavior. Mirror this exactly (do NOT gate router inclusion on the flag
  in `main.py` — inclusion is unconditional; the route checks the flag).
- **Provider abstraction**: primary + fallback (e.g. fal→Replicate for
  images, Cohere→Jina for rerank). Each provider returns
  `(status, data, provider_name, *rest)`; on primary failure fall through to
  the next. Return a clean `502 {"error":"upstream_<cat>"}` on total failure
  — never fake a 200.
- **Pricing model (immutable rule)**: `price = max(floor, provider_cost ×
  1.35 + 0.0015)`. Floors: data/image-generate ~$0.004, rerank ~$0.006.
  Keep customer price DERIVED from a provider-cost table; never hardcode a
  price that can't be traced to a provider quote.
- **MCP x402 settlement (separate service, separate repo `cortexcloud-mcp`)**:
  served by `cortexcloud-mcp.service` on :3100 (cloudflared → `/mcp`). Source
  `src/index.ts` is built to `mcp-bundle.cjs` (`npx tsc --noEmit && npx
  --no-install esbuild src/http.ts --bundle --format=cjs --platform=node
  --outfile=dist/mcp-bundle.cjs`), deployed to CT105
  `/opt/cortexcloud-mcp/mcp-bundle.cjs`. Env in `/opt/cortexcloud-mcp/mcp.env`
  (mode 600). **CRITICAL PITFALL (see below): the MCP server must NOT hold the
  payer key or it pays on the operator's wallet — defeating agent-native x402.**
  It is a DISCOVERY + FREE surface (estimate/backends/jobs); paid solve runs
  through REST/SDK where the AGENT's wallet pays.
- **x402 Bazaar auto-index**: set `"discoverable": true` in the `build_manifest()`
  dict in `app/x402/discovery.py` (the `/.well-known/x402.json` body). With it,
  the CDP facilitator + x402 Bazaar catalog auto-list every endpoint — no API
  submission. Free, high-leverage. (Separate from `bazaar.py` `_TOOLS`.)
- **Ledger margin tracking**: set `request.state.provider_cost_usd` and
  `request.state.category="<cat>"` in the route, AND add a branch in the
  middleware's pre-settle block (mirror the Data branch around line 507-563)
  so the ledger captures cost/margin even though the middleware records
  before `call_next`. Without the middleware branch, the ledger stores
  `None` for cost/margin.

## Per-endpoint gating (subset of a category ready, rest disabled)
When only SOME endpoints in a category are production-ready (e.g. ML: rerank
ready, image-generate/image-understand blocked on provider credits), do NOT
flip the master `<CAT>_ENABLED` flag — that would disable the ready one too.
Instead add per-endpoint flags (`ML_RERANK_ENABLED`, `ML_IMAGE_GENERATE_ENABLED`,
`ML_IMAGE_UNDERSTAND_ENABLED`, each defaulting `True`) and:
- Route-level: `_disabled(endpoint)` checks the per-endpoint flag (not just
  the master) and returns 503 `endpoint_disabled`.
- **Middleware-level (critical)**: the x402 middleware runs BEFORE the route,
  so the route's 503 is reached only AFTER the 402 payment challenge. To avoid
  charging a customer for a disabled endpoint, add a gate in the middleware's
  per-category branch (before the price/challenge is built) that returns 503
  `endpoint_disabled` immediately when the per-endpoint flag is false. Without
  this, a disabled endpoint still emits a 402 and would settle-then-503.
- Keep the provider fallback architecture (primary→fallback) untouched; only
  the entry gate changes. This is exactly how ML shipped on `ml-api`.

## Cost / margin comparison for funding decisions
`references/ml-cost-comparison.md` has current (Aug 2026) per-call provider
costs (fal/Replicate for image-gen, OpenRouter Gemini for vision, Cohere for
rerank), the CortexCloud margin math, and the funding-vs-breakeven protocol.

## Step-by-step (new category)
1. `app/core/config.py`: add `<CAT>_ENABLED` + provider key fields.
2. `app/x402/pricing.py`: add `<CAT>_PROVIDERS`, `estimate_cost()`,
   `price_usd()`, register routes in `ROUTE_PRICING`/`ROUTE_DESCRIPTIONS`/
   `FREE_ROUTES`. Free/`:free` provider models MUST return cost `$0` so
   margin isn't overstated.
3. `app/api/<cat>.py`: 3+ endpoints, `_disabled()` gate, `_need(key,...)`
   prefix check, provider funcs, TTLCache for read/rerank (not generate),
   `request.state` set for ledger.
4. `app/main.py`: `include_router(<cat>_router)` unconditionally.
5. `app/x402/bazaar.py`: add MCP tool entries in `_TOOLS`.
6. `app/middleware/x402.py`: add the `<cat>` cost/category branch.
7. `tests/test_<cat>.py`: unit (pricing model, flag-gate 503, fallback
   routing, cache) + guarded integration (real keys). Use the `client`
   ASGI fixture for route-logic tests; set `X402_ENABLED=False` to bypass
   payment in those tests, and use keys with the required prefixes
   (`sk-or-` for OpenRouter, `fal-` for Fal, `co-` for Cohere) or `_need`
   returns 503.
8. `scripts/<cat>_paid_smoke.py`: real x402 settlement per endpoint (reuse
   the Data smoke pattern: 402 → sign → CDP settle → 200 → assert
   cost/price/margin). Buyer key at `/root/bzcheck/test_buyer.key`.

## Validation on staging (REAL money)
- Push branch, `git checkout` it on CT105 (move untracked smoke scripts
  aside first — they block checkout). Add provider keys to the systemd
  `Environment=` line (NOT a `.env`; staging has none). `systemctl restart`.
- Run `pytest tests/test_<cat>.py` ON STAGING (local venv lacks the ECDSA
  key file `/opt/CortexCloudAPI/server_ecdsa.key` the middleware needs).
- Run the paid smoke for every endpoint + every fallback path.
- Regenerate `openapi.json` (see pitfall) and verify x402.json + Bazaar.
- Confirm ledger rows have `category=<cat>`, correct `provider_cost_usd`,
  `margin_usd`.

## Switching auth modes: single-tenant (`PRIVATE_API_KEY`) ↔ public x402
The runtime has TWO modes, gated at boot by `enforce_secrets_in_production`
(`app/core/config.py`): with `PRIVATE_API_KEY` set, the validator only
requires `PRIVATE_API_KEY` itself (the static key replaces blockchain
settlement — `PRIVATE_API_KEY` global gate in middleware blocks every path
except `/health`); with `X402_ENABLED=true` and `PRIVATE_API_KEY` UNSET,
it demands `[WALLET_ADDRESS, CDP_WALLET_SECRET, X402_FACILITATOR_API_KEY_ID,
X402_FACILITATOR_API_KEY_SECRET, AUTOMATION_WEBHOOK_SECRET]`. **Switching
from single-tenant to public x402 is a credential-sourcing problem, not a
config-flip problem** — prod was booted with only `WALLET_ADDRESS` and
`AUTOMATION_WEBHOOK_SECRET`; the CDP trio is missing. Before flipping
`X402_ENABLED=true`, decide where the CDP credentials come from (staging
`.env`, AWS Secrets Manager, fresh Coinbase CDP dashboard). The boot
validator fails FAST — `pydantic ValidationError [S7] ENV=production
requires CDP_WALLET_SECRET in environment` — and systemd restart-loops
every 5 s. Recovery = either source the creds, OR flip `X402_ENABLED=false`
in env to fall back to single-tenant. Procedure + verification: see
`references/mode-switch-x402-public.md`.

## Pitfalls
- **OpenAPI regen — use the SCRIPT, not the one-liner.** `main.py` overrides
  `app.openapi()` to return the STATIC `openapi.json` (see below), so a manual
  `create_app(override_openapi=False).openapi()` dump DOES NOT add the x402
  `security`/`x-payment-info` tags or the bazaar request bodies — it produced
  a spec with 0 security tags and broke `test_openapi_spec_payments`. The
  correct, idempotent regen is `scripts/update_openapi_v2.py` (run on staging
  with unit env exported: `export $(grep '^Environment='
  /etc/systemd/system/cortexcloud.service | sed 's/^Environment=//'); export
  ENV=production; python update_openapi_v2.py`). It reads `ROUTE_PRICING`
  (for security+x-payment-info) and `INPUT_SCHEMAS` (for request bodies) and
  writes `openapi.json`. After regen, **restart** so the running app re-reads
  the file. Commit the regenerated `openapi.json` to the branch (scp the
  staging file back if you regenerated on staging).
- **INPUT_SCHEMAS + INPUT_EXAMPLES are REQUIRED per new route.** The x402
  bazaar preflight validates every paid route against `INPUT_SCHEMAS[path]`
  and `INPUT_EXAMPLES[path]`. If a route is missing from BOTH, the discovery
  extension falls back to the OPTIMIZE/qubo body, so the 402 preflight fails
  with `(root).input.body: <field> is required` and the paid smoke can never
  settle (402 loops forever). Add both dicts in `app/middleware/x402.py`
  (spread pattern: `INPUT_SCHEMAS = {**INPUT_SCHEMAS, "/v1/<cat>/x": {...}}`)
  for EVERY paid route. Self-hosted categories (e.g. Automation) use
  `provider_cost=0` and a flat floor price via `pegged_price(0.0, floor=X)`.
- **llms.txt + agentsearch.txt are HARDCODED templates** in
  `app/x402/discovery.py` (NOT auto-derived from routes). On every category
  launch they go STALE — they claimed "six categories including Automation"
  but listed zero Automation endpoints. After shipping, grep both for the new
  category and add a concise section (endpoints, prices, examples, safety
  notes). This is the recurring discovery-audit step — do it on every release.
- **Durable drift guard (root cause of the "old page keeps showing" bug).**
  `git pull` does NOT fail loudly — when the prod tree diverges it just
  no-ops, so the box keeps serving stale files. Two divergence sources:
  (1) the app regenerates `openapi.json` at runtime, dirtying the tree;
  (2) a stray local commit/untracked script (e.g. probe scripts) makes a
  fast-forward impossible. Symptom: you push + restart, but the OLD landing
  or 404 routes persist. The one-off `git reset --hard origin/main` fixes it
  ONCE but drifts again. **Durable fix = a self-heal cron, NOT an
  `ExecStartPre`.** An `ExecStartPre=/bin/bash -lc 'git fetch && git reset
  --hard origin/main'` fights systemd's StartLimitBurst (rapid restarts during
  testing trip "Start request repeated too quickly" and the unit enters
  failed state — a boot-loop risk) and can block startup if `git fetch` ever
  fails offline. Instead run the same reset from a **separate scheduled job**
  every 15 min (see `scripts/prod_selfheal.sh` + `references/prod-selfheal.md`).
  It force-syncs prod to origin/main continuously, cannot block startup, and
  can't loop. Proven this session: injected stale `index.html` → cron reverted
  it → live 200 with correct theme.
- **Deploy step (manual, when you also restart):** `git fetch origin && git
  reset --hard origin/main`, remove untracked junk (`rm -f free_probe.py
  run_*.sh *.bak`), then `systemctl restart`. Verify new routes return 402
  (not 404) before enabling the feature flag. The self-heal cron makes this
  idempotent but the manual reset is still the explicit deploy action.
- **MCP `/mcp` needs an `initialize` handshake** before `tools/list`.
  A bare `tools/list` POST returns `{"error":"Server not initialized"}` — that
  is CORRECT protocol behavior, not a failure. The bazaar (`/.well-known/bazaar`)
  is the tool source-of-truth; confirm automation tools appear there. The
  suite's `test_mcp_tools_list` is intentionally skipped (MCP served
  out-of-band).
- **systemd drop-in `mpp.conf`** under
  `/etc/systemd/system/cortexcloud.service.d/` adds MPP/AI/RESEARCH env vars
  and does NOT override the main unit's `Environment=`, so per-category flags
  set in the main unit ARE picked up. Verify with `systemctl show -p
  Environment` (merged) and the running process environ (`tr '\0' '\n' <
  /proc/$(systemctl show -p MainPID | cut -d= -f2)/environ | grep KEY=`).
- **Plain `curl` to the public host can 403 (Cloudflare).** Simple curls
  without a proper `User-Agent`/Accept sometimes get `403 code 1010` or a
  challenge HTML, which looks like a 5xx. Use the x402 SDK smoke script
  (scripts/<cat>_paid_smoke.py) or `curl -s` with a UA header for real
  checks; don't trust a bare curl 403 as an app failure.
- **NO-REFUND limitation (platform-wide, by design).** x402 v2 settles
  USDC IRREVERSIBLY in the middleware BEFORE the route runs; if the upstream
  provider then 5xx, the customer is charged with no automatic refund. There
  is NO refund/reverse code anywhere in the repo (only a `refund` enum value
  in `billing.py`, no execution). A real refund would need a send-capable
  merchant key + on-chain USDC transfer + idempotency — a separate
  platform-wide feature. Do NOT add a per-category refund; document the
  limitation. This is uniform across ALL categories (AI/Research/Data/
  Quantum/ML), so changing it for one category is inconsistent.
- **systemd env editing**: a single `Environment=` line holds all vars.
  `sed` edits can collide (duplicate keys, stray processes). After editing,
  verify the KEY IS IN THE RUNNING PROCESS (`tr '\0' '\n' <
  /proc/$(systemctl show -p MainPID --no-pager | cut -d= -f2)/environ |
  grep KEY=`), not just the unit file — `pgrep` may match stray SSH-session
  uvicorn processes. Kill stray uvicorns from SSH runs.
- **Free provider validation**: "free" tiers are often unreliable — OpenRouter
  `:free` models are throttled at the provider (429) even with a BYOK key for
  some backends (Google); Replicate needs actual CREDITS not just a payment
  method (402 "requires credit"); fal accounts hit TOP_UP locks. Report
  these as provider-account funding blockers, never fake a 200.
- **Provider key prefix checks**: `_need(key, name, prefix)` enforces
  prefixes (e.g. `sk-or-`). Test keys must match or the route 503s.
- **Audit MUST probe runner completion, not just job acceptance.** A paid
  job that is accepted (402→sign→settle→200, row in `opt_jobs`) but never
  reaches `status=succeeded` / `finished_at` is a SILENT FAILURE: the
  customer is charged, no result is returned, and `asyncio.create_task`
  swallows the exception ("Task exception was never retrieved"). The prior
  audit caught a `Benchmark(...) TypeError` (`provider_cost_usd` kwarg
  rejected by SQLAlchemy ORM even though the DB column exists — model
  refactor on Aug 23 left `runner.py:200` calling a kwarg the ORM no longer
  declares) that hung EVERY optimize job for 9 days. Audit procedure:
  after running `verify_integration.py --mode all` (which only checks settle,
  not completion), poll the returned `job_id` for 30s. If still
  `status=running, backend=null, finished_at=null`, that is the smoking gun
  for a runner crash. Then grep journalctl for `Task exception was never
  retrieved` + `TypeError` — that pair is the canonical silent-fail
  signature. Also grep `runner.py` for any unguarded ORM writes
  (`db.add(ORMModel(... kwarg=...))`); every successful-completion path
  should be wrapped or the crash halts the job silently.
- **ORM/DB drift pitfall (manifests as the audit gap above).** SQLAlchemy
  ORM uses the model class, not the live DB schema. If the DB column exists
  (e.g. from an older migration) but the ORM class no longer declares it,
  `Model(**kwargs)` raises `TypeError: '<col>' is an invalid keyword
  argument`. This happens silently in `asyncio.create_task` and the job
  hangs. After ANY model refactor that moves a column (e.g.
  `provider_cost_usd` was moved from `Benchmark` to `Payment` in commit
  `f90401e`), grep the codebase for callers that still pass the moved kwarg
  to the OLD model. The fix is one of: (a) add the column to the model
  class, (b) remove the kwarg from the caller, or (c) wrap the insert in
  `try/except` so the job fails loudly instead of hanging.
- **SAVEPOINT pattern for any runner write that can fail independently.**
  Once you have the fix above, don't just `try/except: pass` — that swallows
  the evidence. The correct root-cause-safe pattern is `async with
  db.begin_nested(): db.add(Model(...))` so the failure only rolls back
  that one row's SAVEPOINT, leaving the outer transaction (exec_row +
  job.status + finished_at) intact. Then `except Exception: logger.exception
  + alert(...)` on the outer try. A future ORM/DB drift on this insert
  logs loudly AND the job still completes with `status=succeeded` and
  `finished_at` set. Verified pattern: `app/optimizer/runner.py` post-P0-1.
- **Runner-side ledger write-back (post-solve cost).** When the x402
  middleware records the `x402_payments` row at settle time, it can't know
  the actual `provider_cost_usd` for `/v1/optimize` (the quantum cost is
  only known after `pick_solver` runs). The cleanest fix: thread the
  payer from the route handler through `create_job(payer=...)` into
  `OptimizeJob.request["payer"]`, then in the runner after `_cost_ledger()`
  call a backfill helper that runs an UPDATE on `x402_payments` matching
  `endpoint='/v1/optimize'` AND `payer=` AND `occurred_at` within a
  tight window (`started_at - 2s` to `started_at + 30s`) AND
  `provider_cost_usd IS NULL`, setting `provider_cost_usd` and
  `margin_usd = amount_usd - provider_cost_usd`. Best-effort:
  log+continue on failure. Worked for the test_buyer (single tenant);
  upgrade to a real `payment_id` FK on `OptimizeJob` if multi-tenant
  concurrent jobs become a concern.
- **Pre-route SSRF guard (blocked targets must NEVER charge).** SSRF
  validation in the route handler runs AFTER the x402 middleware has
  settled — a customer paying for `http://169.254.169.254/` is charged
  and then gets the SSRF error. That's "pay for nothing." The fix is to
  move the `is_safe_url()` check into `InputValidationMiddleware` for
  the two egress endpoints (`/v1/automation/http-request` and
  `/v1/automation/webhook`): parse the JSON body (the middleware reads it
  for size/depth anyway), pull `data["url"]`, call `is_safe_url()`,
  return 400 `ssrf_blocked: ...` if unsafe. The route handler keeps its
  in-route check as defense in depth. After the fix, 0 ledger rows for
  rejected requests — verified by querying `x402_payments` for the test
  payer's calls in the last 5 min.
- **Stray `/opt/CortexCloudAPI/.env` (RESOLVED 2026-09-01).** The prior
  audit pitfall above described the duplicated secrets file. As of
  2026-09-01 it has been deleted via `shred -u -n 3`, after consolidating
  `POSTGRES_PASSWORD` into `/etc/cortexcloud/cortexcloud.env` and
  updating both `/root/cc_backup.sh` AND `/root/cc_restore_test.sh` to
  read the same file. The backup script now also `source`s the env at
  start so cron runs work without external env setup. Before any future
  rotation of `POSTGRES_PASSWORD`, edit ONLY `/etc/cortexcloud/cortexcloud.env`.
- **Live paid-smoke scripts (canonical, verified Aug 2026).** Use these
  names — `data_paid_smoke.py` and `research_paid_smoke.py` referenced
  from older memory do NOT exist. The current set:
  - `scripts/automation_paid_smoke.py` — 5 endpoints + estimate; full
    402→sign→CDP→200+ledger cycle. Default base: pass as `argv[1]`.
  - `scripts/agent_smoke.py` — discovery + estimate + 402 challenge shape
    (no real settle).
  - `scripts/verify_integration.py` — `--mode public` (free checks),
    `--mode private` (needs `--api-key`), `--mode all` (real settle
    against `--base`).
  Always pass the public base `https://api.cortexcloud.org` to verify the
  Cloudflare-fronted production. Localhost `http://127.0.0.1:8000` fails
  the CDP settle leg because the x402 challenge resource URL is built
  from the incoming request URL and CDP requires `https://`.
- **systemd inline `Environment=` is NOT authoritative for keys sourced
  from `EnvironmentFile=`.** When the same key (e.g. `X402_ENABLED`)
  appears in BOTH the unit's inline `Environment=` line AND
  `/etc/cortexcloud/cortexcloud.env`, the running process picks up the
  env-file value, not the unit-inline one — even when systemd's own
  `systemctl show -p Environment` shows both. Always grep the live
  process environ (`tr '\0' '\n' < /proc/$(systemctl show -p MainPID
  --value)/environ | grep KEY=`) to confirm what's actually loaded. If
  you intend the unit-inline to win, REMOVE the key from the env file
  (or the env-file value will keep winning in practice). The reasoning
  is subtle and depends on systemd's parser treating `Environment=` as
  one line of multiple pairs vs `EnvironmentFile=` lines; the empirical
  rule is: trust only the running process, never just the file.
- **`pct push <vmid> <src> <dst>` over SSH silently no-ops when `<src>`
  is on the AGENT host, not the Proxmox host.** `pct push` resolves
  `<src>` on the host where pct itself runs (= the PVE host). Driving
  it as `ssh ninyi "pct push 105 /tmp/x.py /opt/x.py"` with `/tmp/x.py`
  existing only on the Hermes host reports "pushed" (exit 0) but
  writes nothing. The reliable push for an agent-host-only file is the
  stdin-tee pattern: `ssh -i ~/.ssh/hermes_ninyi root@192.168.1.11 "pct
  exec 105 -- tee /opt/.../x.py > /dev/null" < /local/x.py` followed by
  a marker `grep` to confirm. For bulk edits to `/etc/cortexcloud/...`
  use `pct push` ONLY after `scp` to the PVE host first.

## References
- `references/staging-validation.md` — exact commands for checkout, key
  injection, openapi regen, paid-smoke run, ledger check.
- `references/ml-cost-comparison.md` — current provider per-call costs
  (fal/Replicate/OpenRouter/Cohere), CortexCloud margin math, and the
  funding-vs-breakeven protocol for gating endpoints on provider credits.
- `references/backup-restore-test.md` — the actual restore pattern (off-host
  push via rclone + weekly cron that restores the latest dump into a fresh
  Postgres DB and verifies 4 critical tables match live counts). Always
  pair backup creation with a restore test; backup-only is theater.
- `references/prod-selfheal.md` — why prod silently drifts (git divergence),
  the self-heal cron pattern, and the ExecStartPre anti-pattern.
- `scripts/prod_selfheal.sh` — SSH-to-prod `git fetch && reset --hard
  origin/main` self-heal (run on a 15-min schedule).
