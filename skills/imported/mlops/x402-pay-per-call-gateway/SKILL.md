# x402-pay-per-call-gateway

# x402 Pay-Per-Call AI Gateway

Class of work: an **OpenAI-compatible AI inference gateway** that charges per request
in **USDC on Base** via the **x402 protocol** — no signup, no API keys, agents pay
client-side. Stack seen this session: FastAPI + uvicorn, Postgres + Redis
(docker-compose, `network_mode: host`, iptables disabled deliberately), Coinbase CDP
facilitator for `/verify` + `/settle`, and Bazaar discovery for agent discoverability.

## Source of truth — fetch fresh, the protocol moves
x402 is actively evolving. Before trusting field names, pull the live schema:
- Repo moved: **`x402-foundation/x402`** (the old `coinbase/x402` is now a dev fork).
- Live PaymentRequirements / PaymentPayload schemas:
  `typescript/packages/core/src/schemas/index.ts` (read via `raw.githubusercontent.com`).
- Bazaar docs: `docs.cdp.coinbase.com/x402/bazaar` is JS-rendered (curl gets the SPA
  shell) — inspect the SDK source instead (below).

## PaymentRequirements schema (verified against x402-foundation main)
Two versions coexist:
- **V1** (what most servers emit): fields `scheme, network, maxAmountRequired,
  mimeType, payTo, asset, extra, resource`. The 402 *response* must carry top-level
  `x402Version: 1` (it is a discriminated union on `x402Version` — strict V1 parsers
  reject a body missing it).
- **V2**: uses `amount` (not `maxAmountRequired`), drops `recipient`/`mimeType`,
  `resource`, and `network` MUST be CAIP-2 (`eip155:8453`).

**Network format — CAIP-2:** `V1 network` is `NonEmptyString` (lax) but canonical is
`eip155:8453` (Base mainnet) / `eip155:84532` (Base Sepolia). `V2 network` *requires*
CAIP-2. Set `X402_NETWORK=eip155:8453` for mainnet.
**Asset:** Base USDC contract = `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`.
**Amount:** USD → USDC atomic = `int(usd * 1_000_000)` (6 decimals). `$0.005` → `"5000"`.

## 402 flow (custom middleware, verified working)
1. Route not in price map, or free (`$0.00`) → pass through.
2. No `X-PAYMENT` header → return `402` with the `PaymentRequirements` JSON
   (top-level `x402Version:1`, `accepts:[{...}]`) + a `PAYMENT-REQUIRED` base64 header.
3. `X-PAYMENT` present (base64 JSON `PaymentPayload`) → POST to facilitator
   `{FACILITATOR}/verify` then `{FACILITATOR}/settle` with body
   `{x402Version:1, paymentPayload, paymentRequirements: accepts[0]}`. On success,
   forward request + set `X-PAYMENT-RESPONSE` header.
Facilitator URL: `https://api.cdp.coinbase.com/platform/v2/x402` (verify + settle).

**Client-side settlement needs NO wallet ETH — only a signing key.** The gateway
forwards the signed PAYMENT-SIGNATURE to the facilitator, which executes the
on-chain USDC transfer itself (it covers gas). A payer wallet with USDC but 0 ETH
can still settle. The REAL blocker for paid E2E tests is always the **payer's
private key** (EIP-3009/EIP-712 authorization), never gas. Before claiming a
wallet can't pay, check: (1) does anything on the server hold that key, (2) is
there any signing key at all. Finding a key on the server: scan for 64-hex blobs
and derive addresses with viem `privateKeyToAccount` (host) or eth_utils `keccak`
(CT venv) — NEVER print key material; `server_ecdsa.key`/`test_buyer.key`/
`~/.cortexcloud/.session` may all be DIFFERENT wallets, all possibly empty.
A funded Binance withdrawal (screenshot/txid) proves funds, not spendability.

**Dynamic pricing support:** For certain endpoints, the middleware calculates the
payment amount dynamically based on request parameters:
- **Chat/Responses (`/x402/v1/chat/completions`, `/x402/v1/responses`)**:
  Extract `model` from request body, look up its input/output cost per 1k tokens
  from the model registry, apply 27.5% markup (midpoint of 25-30% range) for
  1000 input + 1000 output tokens (2000 tokens total basis), format as 6-decimal
  dollar string. Falls back to static price if model unknown.
- **Image Generation (`/x402/v1/images/generations`)**:
  Extract `n` (number of images) from request body: price = $0.04 × n for n=1,
  or $0.035 × n for n>1 (batch discount). Falls back to static price if parsing fails.
- All other routes use static pricing from `ROUTE_PRICING`.

## Bazaar discovery (Python SDK has parity — no TS needed)

**Dynamic pricing support:** For certain endpoints, the middleware calculates the
payment amount dynamically based on request parameters:
- **Chat/Responses (`/x402/v1/chat/completions`, `/x402/v1/responses`)**:
  Extract `model` from request body, look up its input/output cost per 1k tokens
  from the model registry, apply 27.5% markup (midpoint of 25-30% range) for
  1000 input + 1000 output tokens (2000 tokens total basis), format as 6-decimal
  dollar string. Falls back to static price if model unknown.
- **Image Generation (`/x402/v1/images/generations`)**:
  Extract `n` (number of images) from request body: price = $0.04 × n for n=1,
  or $0.035 × n for n>1 (batch discount). Falls back to static price if parsing fails.
- All other routes use static pricing from `ROUTE_PRICING`.

## Bazaar discovery (Python SDK has parity — no TS needed)
`pip install x402[fastapi,evm,extensions]` (v2.14.0 this session). Module
`x402.extensions.bazaar` provides:
- `declare_discovery_extension(input=, input_schema=, path_params_schema=,
  body_type="json", output=OutputConfig(example=..., schema=...))` → returns
  `{"bazaar": {...}}` to attach as route discovery metadata. For POST JSON routes use
  `body_type="json"` + `input` (example body) + `input_schema` (JSON Schema of
  `model`/`messages`).
- `declare_mcp_discovery_extension(DeclareMcpDiscoveryConfig(tool_name=,
  input_schema=, description=, transport="sse", example=))` — **takes a single
  `DeclareMcpDiscoveryConfig` object, NOT keyword args** (the param is `config`).
  `transport` is a `Literal["sse","streamable-http"]` → pass the string `"sse"`.
- `OutputConfig` dataclass: `example` + optional `schema`.

MCP endpoint: serve SSE (`text/event-stream`) handling `initialize` → `tools/list`
→ `tools/call`. For a *discovery-first* gateway, `tools/call` should return the
endpoint URL + PaymentRequirements so the **agent pays client-side** (don't proxy
the paid inference server-side or you defeat x402). A consolidated discovery doc at
`GET /.well-known/bazaar` (per-route extensions) is what Bazaar/crawlers consume.

## Middleware & streaming pitfalls (starlette 1.x — the big one)
- **`stream: true` paid calls returning 200 + `text/event-stream` with an EMPTY body** =
  a BaseHTTPMiddleware receive-state-machine violation, not an upstream problem.
  Journal shows `starlette/middleware/base.py:57 RuntimeError: Unexpected message
  received: http.request` wrapped in an anyio `ExceptionGroup`. Starlette 1.x wraps
  `receive` in a strict one-shot state machine (one `http.request`, then only
  `http.disconnect`); any middleware that re-injects `request._receive` (or triggers
  a second `http.request`) kills the streaming task after 200 is committed.
  - Do NOT re-inject `request._receive` after `await request.body()` — FastAPI
    caches the body on `request._body`, so the route re-reads from cache without a
    second receive() call. Re-injection is both unnecessary AND fatal for SSE.
    **Grep `request._receive =` — there may be several sites** (body-read block
    AND each dynamic-pricing block for chat/images re-inject their own closure);
    removing only one leaves SSE still broken. To isolate stack-vs-middleware,
    add a temp bodyless SSE probe route OUTSIDE the paid path (`/__sse_probe`,
    StreamingResponse) — if it streams, the paying middleware is the culprit.
  - If the middleware still conflicts (body read in dispatch + route body parse),
    convert the middleware to **pure ASGI** (`async def __call__(scope, receive,
    send)`) — that removes starlette's state machine entirely.
  - Provider generators test fine standalone — always confirm the failure is at the
    HTTP layer by checking for the wrapper's completion log before blaming upstream.
  Full traceback + diagnosis sequence: `references/fastapi-streaming-and-middleware.md`.
- **contextvars do NOT cross the BaseHTTPMiddleware task boundary.** A router that
  `contextvars.ContextVar.set(...)` for the structured log shows `null` in the
  middleware's post-`call_next` log line (downstream app runs in a separate anyio
  task; parent never sees child's changes). Pass request-scoped data via
  `request.state` (set in the route handler, read in the middleware) instead.
- **CDP facilitator requires an `https://` resource** (schema regex `^https://.*$`).
  You cannot local-test the paid flow by setting `X402_RESOURCE_BASE=http://localhost:8000`
  — validation fails with `doesn't match schema due to: string doesn't match the
  regular expression "^https://.*$"`. Keep the https resource; test through the public
  URL. If you DO temporarily edit `.env`, `cp .env /tmp/.env.bak.<tag>` FIRST and
  verify the value after restore (`grep ^X402_RESOURCE_BASE`), and re-check the
  service is `active` — a bash parse error in a chained ssh command can abort mid-way
  and leave the service down.
- **viem `privateKeyToAccount` requires a 0x-prefixed hex key.** `test_buyer.key` is
  raw 64-hex — wrap as `pk.startsWith("0x") ? pk : "0x" + pk` or you get
  `invalid private key, expected hex or 32 bytes, got string`.
- **Verify routes by LIVE curl, not `app.routes` introspection.** FastAPI 0.139
  includes `_IncludedRouter` wrappers in `app.routes`; `[r.path for r in app.routes]`
  raises `AttributeError: '_IncludedRouter' object has no attribute 'path'` even when
  all routes are registered. `curl -s -o /dev/null -w "%{http_code}" .../x402/v1/models`
  is the real check (use `getattr(r, "path", None)` if you must introspect).

## Payment-proof verification hardening (Section 1 — DO NOT outsource to CDP)
The facilitator is NOT a server-side verifier: the CDP SDK only builds client
auth headers, and `/validate`+`/settle` confirm settlement. A gateway that gates
purely on the facilitator round-trip will execute paid routes on forged/replayed
proofs. Verify the EIP-712 `TransferAuthorization` **in-process first** with
`eth_account` (recover signer == `authorization.from`, require your own price,
chainId==8453, canonical USDC Base asset, payTo==WALLET_ADDRESS, validBefore
window 30s-skew/5min-future), Redis-SETNX nonce replay guard, fail closed to 402.
Full checklist + ordering + the `[2:]` hex real-data pitfall + checksum-address
and deployed-module testing norms: `references/payment-verification-hardening.md`.
This extends the standalone local in-process verifier (payverify.py) that must
run BEFORE the CDP client is built.

## Smart Routing Implementation (S2)

The smart routing system selects the optimal upstream provider for each request based on:
- Current latency (tracked as rolling p95)
- Error rate (rolling 60s window)
- Cost (per-call pricing from ROUTE_PRICING)

### Key Implementation Details

1. **Provider Metrics Tracking**:
   - Maintain rolling windows of latency measurements (last 100 requests)
   - Track success/failure outcomes for error rate calculation
   - Use simple average of recent samples for responsiveness

2. **Provider Selection Algorithm**:
   - For models with multiple providers (e.g., Llama 3.3 70B on Groq and NVIDIA NIM):
     - Fetch all active models matching the requested name via `ModelRegistryService.get_models_by_name()`
     - For each provider, retrieve recent metrics:
       * Average latency from sliding window
       * Error rate = 1 - (successful requests / total requests)
     - Compute composite score: `(0.4 * normalized_latency) + (0.4 * error_rate) + (0.2 * normalized_cost)`
     - Select provider with lowest score
   - Weights prioritize latency and reliability over cost

3. **Fallback Mechanism**:
   - If primary provider fails with 429 or 5xx, check for configured `fallback_model` in model capabilities
   - Attempt one retry with fallback provider before returning error to caller
   - Fallback models are defined per-model in the capabilities field

4. **Headers and Observability**:
   - Add `X-Cortex-Provider` header to responses showing which provider was used
   - Update Prometheus metrics with per-provider latency and error rates
   - Structured logging includes `upstream_provider` and `upstream_latency_ms` fields

5. **Integration Points**:
   - Modified `ModelRouter` class in `/app/routing/router.py`
   - Extended `ModelRegistryService` with `get_models_by_name()` method
   - Updated `/x402/v1/chat/completions` and `/x402/v1/responses` endpoints to use new routing
   - Preserved existing retry logic and circuit breaker integration

### Current State (as of this session)
- Router already implements basic retry with exponential backoff
- Circuit breaker per provider is active (20% error rate over 30s → 503+Retry-After)
- Model registry supports multiple entries per model name (different providers)
- Pricing is available via `ROUTE_PRICING` and model-specific costs in database
- Implemented:
  1. `get_models_by_name` in `ModelRegistryService`
  2. Metrics collection and averaging in `ModelRouter`
  3. Provider selection logic in routing methods
  4. `X-Cortex-Provider` header injection
  5. Foundation for `/compare` endpoint (to be completed in S2)

## Performance batch (S3–S6) & paid E2E harness
Completed 2026-08: provider connection pooling, per-provider circuit breaker,
Redis response caching + request coalescing, per-payer rate limit, structured JSON
logging + Prometheus `/metrics`. Implemented patterns, deploy/verification
discipline for the LXC push loop, and the @x402/fetch paid test battery that
replaced the lost `node dist/cli.js` proxy: `references/perf-and-paid-e2e.md`.

## Bazaar visibility root cause: the payTo wallet must be CDP-provisioned
"Zero Bazaar visibility even though settlements succeed" has ONE canonical cause:
the `payTo`/`WALLET_ADDRESS` is a **bare external EOA**. Settlement is permissionless
(`transferWithAuthorization`), so an EOA settles fine, but **Bazaar indexing is tied to
an account/developer-linked payee**. Symptoms that all point here:
- `GET {FACILITATOR}/supported` → 200, `.get("extensions")` lists `bazaar` ✓
  (facilitator config is fine — don't chase it).
- `POST {FACILITATOR}/settle` returns a proper 4xx/JSON (auth works), and
  `EXTENSION-RESPONSES` header is **absent** — an upstream gap, do NOT try to force it.
- Deploy has `X402_FACILITATOR_API_KEY_ID`/`_SECRET` set, so "setup is done", yet no index.

**Root cause check** — grep the codebase for wallet provisioning before assuming setup:
`grep -rniE "createWallet|\.create\(" app/ site-packages/cdp/` — if `WALLET_ADDRESS` is just a
literal in `.env` with no `create_account` path, it's an external EOA.

**Migrating to a CDP wallet requires a Server Signer first.** The installed CDP SDK
(`cdp.cdp_client.CdpClient`, `evm.create_account(name=...)`) can mint a wallet using the
SAME key pair as the facilitator (`api_key_id`=`X402_FACILITATOR_API_KEY_ID`,
`api_key_secret`=`X402_FACILITATOR_API_KEY_SECRET`), but creation **fails at auth** until a
console-provisioned secret exists:
```
ValueError: Wallet Secret not configured. Please set the CDP_WALLET_SECRET environment variable...
```
`CDP_WALLET_SECRET` is emitted when you generate a **Server Signer** in the CDP Developer
Platform console — a console action, not something an agent can mint from the facilitator
key. So the migration is: user creates Server Signer → pastes `CDP_WALLET_SECRET` → set
env → `cdp.evm.create_account(name="cortexcloud-payee")` → set `WALLET_ADDRESS` to the
wallet to the returned wallet address → restart. **Without a console-created Server Signer the
wallet migration is blocked — this is a real stop-and-ask user checkpoint, not a fixable-in-code
gap.** Changing `WALLET_ADDRESS` is a business change, flag it first.

## Standalone diagnostic scripts must run from the project cwd
Pydantic `SettingsConfigDict(env_file=".env")` resolves relative to **cwd**. A probe script
launched with `cd /tmp` (or any non-project dir) loads `.env` and resolves `settings.WALLET_ADDRESS`
to `None`, `_cdp_auth_headers()` to `{}` → every CDP call 401s and you misread it as an auth
failure. **Always run inside the container with `cd /opt/CortexCloudAPI && .../python probe.py`**,
or the script silently reads unconfigured settings. (Observed: `payTo: None` + `cdp auth header
keys: {}` → the whole facilitator "broken" diagnosis was garbage until re-run from the project dir.)

## Dashboard/live-stats accuracy — the two-wallet drift and dead-counter traps
- **Wrong-wallet constant:** a landing-page `/activity` (or any stats) endpoint that hardcodes a
  `MERCHANT = 0x...` literal can drift from `settings.WALLET_ADDRESS` — it then queries on-chain
  USDC Transfers for the WRONG wallet, finds nothing, and falls back to a **fabricated placeholder**
  settlement. Grep for the hardcoded address and compare to `.env`'s `WALLET_ADDRESS` in the same
  pass; derive live metrics from `settings.WALLET_ADDRESS`, never a copy.
- **Dead counter:** a stats endpoint that *reads* a tally key it expects the middleware to *write*
  returns permanent zeros. `grep -rn "<key>:" app/ --include=*.py` — if the only hit is the reader
  (middleware never `HINCRBY`s it), the pipeline is dead, not slow. Fix the writer or drop the field.
- **SSR vs hydration — diagnose before fixing:** the user's "widgets render 0/blank on no-JS fetch"
  can be either (a) normal Next.js static-export behavior (values hardcoded in the JS bundle /
  client-rendered; no-JS HTML has none) or (b) a dead metrics pipeline. Determine WHICH before
  touching anything: grep the built JS for the fetch target + the hardcoded `{value:...}` defaults;
  grep backend for the counter writer. Hardcoded marketing numbers (e.g. `180ms` median first
  token, `40+` models) are not measured — per the user's rules, DON'T fabricate; omit or label.

## PUBLIC surface when toggling out of single-tenant mode (2026-09)
The middleware has TWO coexisting gates: the `PRIVATE_API_KEY` blanket check (single-tenant mode) and the `_PATH_PRICING` per-route x402 check (public mode). When moving from one to the other, **edit the blanket block, don't delete it** — replace `if path != "/health"` with an explicit allowlist:
```python
_PUBLIC_PATHS = (
    "/health", "/openapi.json", "/llms.txt", "/favicon.ico",
    "/.well-known/x402.json", "/.well-known/bazaar", "/.well-known/openid-configuration",
    "/v1/capabilities", "/v1/backends", "/mcp",
)
if settings.PRIVATE_API_KEY:
    path = request.url.path
    if (path != "/health"
        and path not in _PUBLIC_PATHS
        and not path.startswith("/.well-known/")
        and not path.startswith("/mcp")):
        # ... 401 ...
```
Then REMOVE `PRIVATE_API_KEY` from env and confirm `X402_ENABLED=true`. The price table already 402s paid routes; the only behavioral change is that discovery + MCP become public. Verify externally: `/openapi.json`, `/llms.txt`, `/.well-known/x402.json`, `/.well-known/bazaar`, `/v1/capabilities`, `/v1/backends` all return 200; `/v1/optimize` (paid) returns 402 with the correct `payment-required` challenge; `/internal/metrics` (admin) returns 401 even. Keep admin routes unauthenticated-by-default UNLESS you wire a dedicated bearer path.

## `systemctl reset-failed` BEFORE restarting after a boot-loop burst (2026-09)
After ~5–8 fast `systemctl restart cortexcloud` failures (e.g. the `enforce_secrets_in_production` validator rejecting a missing `CDP_WALLET_SECRET`), systemd rate-limits further restarts. The next restart — even with a corrected env — silently fails with `Failed with result 'exit-code'` and the service stays DOWN. Always `systemctl reset-failed cortexcloud` BEFORE restarting after a burst of failures; confirm with `NRestarts=0` after the restart.

## Safety rules (the user's standing constraints for CortexCloud)
- **Wallet/payment logic is high-risk.** The recipient `WALLET_ADDRESS` receives REAL
  USDC on Base mainnet — explain any change to payment/network code before applying.
- **First public exposure needs explicit go-ahead** (DNS A + router 80/443 forward +
  user says "go"). Caddy certs fail with NXDOMAIN until DNS exists — that is expected
  and NOT "exposed".
- **Bazaar/x402scan indexing needs a real on-chain USDC tx** (even small). Per the
  user's rule, confirm before triggering the first settle that indexes a route.
  x402scan.com/resources/register is URL-based + auto-validates schema (simpler);
  Bazaar is the higher-priority surface (native MCP for agent frameworks).
- Don't silently change `POSTGRES_PASSWORD` (weak default `postgres` must match
  docker-compose) — flag it, leave it.
- DB model *registry rows* (e.g. seeding Gemini/Groq/OpenRouter/NVIDIA models) are NOT
  a migration — safe to insert via the app's `ModelRegistryService`, no schema change.

See `references/x402-schema-notes.md` for the exact verified 402 JSON and SDK call
shapes captured this session. The authoritative v2 challenge body + headless
`validatePaymentRequiredDetailed` recipe + the 405 method-agnostic fix are in
`references/x402scan-v2-validator.md`.

## x402scan discovery & registration (the part that actually blocks listing)
This consumed the most time in-session and is the #1 cause of "No valid x402 response
found (HTTP 405)" failures. Full contract + audit/register workflow in
`references/x402scan-discovery.md`. Key points:
- **Discovery precedence is OpenAPI-FIRST, then `/.well-known/x402` (NO `.json`).**
  x402scan reads `https://yourdomain.com/openapi.json` before any well-known
  manifest. Its compatibility fallback is `GET /.well-known/x402` — NOT
  `/.well-known/x402.json`. A gateway that only serves the `.json` manifest (e.g.
  from `build_manifest`) is INVISIBLE to x402scan beyond whatever openapi.json
  contains. "x402scan only shows 13" = the served openapi.json is stale/small —
  fix openapi.json, not the manifest.
- **The `/.well-known/x402` CONTENT matters — v1 resource-list schema, not your v2 manifest (2026-08, verified from the x402scan-mcp npm bundle).** `discover_resources` parses the bare path with `DiscoveryDocumentSchema = {version: z.number().refine(v=>v===1), resources: z.array(z.string().url()), ownershipProofs?: [urls], instructions?: string}`. Serving the rich v2 pricing manifest (`{"version":2, endpoints:[...]}`) there FAILS the zod parser **silently** — the tool reports `"Found llms.txt but no structured x402 discovery document"` and falls back to llms.txt. tryponcho.com fails the same probe, so it's strict-parser behavior, not your domain. **Serve BOTH:** `/.well-known/x402` = v1 doc `{"version":1, "resources":["https://<origin>/v1/optimize", ...]}` (URLs only); `/.well-known/x402.json` = the rich v2 manifest (registration/bazaar consumers). Probe order the tool uses: `/.well-known/x402` → DNS TXT `_x402.<hostname>` (via cloudflare-dns.com) → `llms.txt`.
- **Headless verification of x402 discovery tools:** spawn `npx -y x402scan-mcp@latest` as stdio and drive JSON-RPC (`initialize` → `notifications/initialized` → `tools/list` → `tools/call` `{name:"discover_resources", arguments:{url:"<origin>"}}`). Parse the returned JSON: `source:"well-known"` = structured doc parsed; `source:"llms-txt"` = parser failed (check `attemptedSources` — it lists every path probed). Other tools: `query_endpoint` (probe 402 prices without paying), `validate_payment`, `execute_call` (auto-paid), `check_balance`, `authed_call` (SIWX).
- **Keep `llms.txt` discovery links ABSOLUTE and CURRENT.** It's the fallback discovery source for crawlers — a stale path there (observed: dead `/x402/v1/mcp` after the MCP moved to `/mcp`) misleads every tool that falls through to it. Absolute URLs for all `.well-known` + `/openapi.json` links.
- **Static openapi override trap:** `app.openapi = lambda: _OPENAPI_SPEC` (loaded
  from a committed file at import) means `app.openapi()` returns the STALE spec —
  a regeneration script that calls it silently reproduces the old count. Bypass
  with the unbound class method: `spec = FastAPI.openapi(app)`. Regeneration
  recipe: build from live routers with `FastAPI.openapi(app)`, then stamp every
  paid path from `ROUTE_PRICING` (single source of truth) with `x-payment-info`
  (`{mode:"fixed",currency:"USD",amount:price}`), add `responses["402"]`, add
  `x-discovery.ownershipProofs: [merchant_wallet]`, write openapi.json, commit,
  RESTART (spec is served from memory), verify live count via
  `curl /openapi.json`. See `references/openapi-regen-for-x402scan.md`.
- **OpenAPI at `/openapi.json` is canonical.** Override FastAPI's auto-spec with
  `app.openapi = lambda: SPEC` (the auto-spec leaks dashboard/admin routes and shadows
  a custom `/openapi.json` route). Per paid op declare `x-payment-info` with STRUCTURED
  `price` (`{mode:"fixed",currency:"USD",amount:"0.005"}`) and `protocols:[{"x402":{}}]`
  (array of OBJECTS, not strings), plus `responses["402"]`. Free op -> `security: []`.
  Add `info.contact.email` (Poncho ownership) and `info.x-guidance` (agent usage text).
- **405 root cause = paywall fires after method routing.** x402scan probes
  HEAD/GET/POST/OPTIONS and a HEAD status-capture; POST-only routes 405 on the others.
  Fix: match `ROUTE_PRICING` by **PATH only** and return 402 for ANY method on paid
  routes (see reference for the exact `_PATH_PRICING` snippet). Verified: all methods
  -> 402 (paid) / 200 (free).
- **The SAME 405 hits FREE routes too — and the path-only paywall does NOT fix
  those.** FastAPI 0.139 does not auto-add HEAD to GET routes, so HEAD/OPTIONS on
  /health, dashboard, admin, /llms.txt, and free /x402/v1 GETs 405. Fix is a
  SEPARATE outermost ProbeMiddleware (rewrite HEAD->GET, 200+Allow for OPTIONS).
  See `references/x402scan-discovery.md` "CRITICAL FIX #2" for the code + the two
  build pitfalls (raw_headers is a list; don't re-inject request._receive).
- **Local `discover` audit ≠ live crawler.** `npx @agentcash/discovery discover`
  reads the SPEC (exit 0, zero errors) and does NOT method-probe — a change that
  405s every live route still passes the CLI. Verify with LIVE curl HEAD/OPTIONS
  probes after any middleware/routing change; the user's x402scan error list (405
  on everything) was only visible from the server-side crawl.
- **Audit before registering:** `npx -y @agentcash/discovery@latest discover
  "https://api.cortexcloud.org"` (needs node>=18). Clear L2/L3/L4 warnings; `FAVICON_MISSING`
  is cosmetic and does NOT block.
- **Registration IS completable by the agent — no SIWX needed for the base listing.**
  Verified 2026-08: `https://www.x402scan.com/resources/register` (and
  `https://www.mppscan.com/register`) are simple origin forms — type the bare
  hostname (no `https://`), the page live-probes every endpoint for ~20-25s, then
  the button enables as `Add API (N resources)` (or `0 valid resources` + a
  per-endpoint error list you must fix first). No wallet/SIWX sign-in was required
  to list on either registry. Full flow + the "No valid x402 response found"
  amount-type bug + validator-source decoding: `references/agentcash-registration.md`.
- **"No valid x402 response found" on the registration probe despite a real 402** =
  check `accepts[].amount` TYPE: the probe validates the live challenge with
  `PaymentRequiredV2Schema` where `amount` is `z.string().min(1)` — a numeric
  `20000` fails, `"20000"` passes. The earlier "registration needs SIWX wallet
  auth" note was wrong — that session's blocker was a schema-failing challenge,
  not auth.
- **x402scan requires an x402 v2 challenge WITH a Bazaar input schema.** This is the
  #1 cause of "No valid x402 response found (HTTP 402)" even when the endpoint already
  returns 402. The discovery lib runs `@agentcash/discovery`'s
  `validatePaymentRequiredDetailed(payload)` on the LIVE 402 body; if it returns
  `summary.errorCount > 0` the endpoint is rejected. Requirements (verified against the
  npm package source `node_modules/@agentcash/discovery/dist/index.js` —
  `validatePaymentRequiredDetailed` ~line 16530, `PaymentRequiredV2Schema` ~line 16186,
  `extractSchemas2` for the schema path):
  - Emit **`x402Version: 2`**. v1 is accepted by the parser but emits a WARN
    "v1 not supported by agentcash"; v2 is what agentcash wants.
  - `accepts[]` entry MUST carry: `scheme`, `network` (CAIP-2 `eip155:8453`),
    `asset` (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`), `amount` (**STRING of
    atomic USDC**, e.g. `"5000"` — v2 uses `amount`, NOT `maxAmountRequired`),
    `payTo` (wallet), and `maxTimeoutSeconds` (integer, e.g. 60). Missing any → hard error.
  - `resource` is an **object** `{url, description, mimeType}` in v2 (v1 used a string URL).
  - **Input schema is MANDATORY** (hard error `SCHEMA_INPUT_MISSING` if absent). v2 path:
    `extensions.bazaar.schema.properties.input.properties.body` (or `.queryParams`).
    Output schema path: `extensions.bazaar.schema.properties.output.properties.example`
    (missing → `SCHEMA_OUTPUT_MISSING`, downgraded to a WARN, so it does NOT block).
  - Exact verified body + headless validator recipe + issue table:
    `references/x402scan-v2-validator.md`.
- **OpenAPI `x-payment-info.protocols` MUST be `[{"x402": {}}]` (array of OBJECTS), NOT
  `["x402"]` (array of strings).** The string form makes x402scan's audit emit
  `L2_PROTOCOLS_MISSING_ON_PAID` + `L2_PRICE_MISSING_ON_PAID`. `price` must also be the
  structured object `{mode:"fixed",currency:"USD",amount:"0.005"}` (not a bare number).
- **Audit BEFORE registering with the `@agentcash/discovery` CLI (needs node>=18):**
  `npx -y @agentcash/discovery@latest discover "https://api.cortexcloud.org"` lists
  routes + L2/L3/L4 warnings; `npx -y @agentcash/discovery@latest check
  "https://api.cortexcloud.org/x402/v1/chat/completions"` validates the LIVE 402 body via
  `validatePaymentRequiredDetailed`. A clean run prints `paid  [x402]` with no errors.
  `FAVICON_MISSING` is cosmetic and does NOT block.
- Cloudflare Tunnel gotcha: after adding/fixing a route, Cloudflare may serve a STALE
  404 (cached pre-fix) — verify with `?cb=1`; a DNS/Read-only API token can't purge it.

## Real paid E2E + on-chain tx proof (first real settlement verified 2026-08-08)
Full recipe: `references/real-paid-e2e-tx-proof.md`. Key facts:
- **On-chain tx-hash retrieval: BaseScan V1 API is deprecated and Etherscan V2
  refuses Base on the free plan.** The working method is `eth_getLogs` on
  `https://mainnet.base.org` filtering USDC `Transfer(from=buyer, to=payTo)` (viem
  snippet in the reference; ~40 min window = `latest - 1200n` at 2s/block).
- `x402_payments` has NO tx-hash column — eth_getLogs is the only on-chain proof
  link; verify exact atomic amount (`20000` units = $0.02).
- **Never inline `node -e` through nested ssh quotes** — escaped quotes mangle a
  hex key and reproduce the "invalid private key" error on a VALID file. Write the
  `.mjs` locally, scp → `pct push` → run by path.
- Paid `/v1/optimize` returns `{job_id, status:"queued", price_usd}` → poll
  `GET /v1/jobs/{id}` until terminal; ledger row lands `status="settled"` with the
  EIP-3009 nonce.

## All-modes paid test harness + money-path bug classes (hit 2026-08-08)
Recipe + script shapes: `references/paid-endpoint-testing.md`. The tests cost real
USDC (test buyer wallet → merchant) and found TWO charge-without-value bugs:

- **BUG CLASS — settle happens BEFORE body validation.** In a middleware stack
  [x402 payment gate → rate limit → input validation], the gate settles (CDP
  settle + `_record_payment`) before the inner validation rejects a malformed
  body. Proof: a paid `/v1/optimize` with `{mode, problem:{problem_type:"qubo"}}`
  (missing `n`/`data`) got 422 **and still settled $0.05** — ledger had 2 rows for
  1 successful run. **Fix (one guard, both settle branches):** after parsing the
  body in the middleware, if the request carries payment credentials
  (`payment_signature` OR (`mpp and authorization`)), validate the body against the
  route's input schema FIRST and return 422 before any settle. Verify by running
  the paid flow with a malformed body → expect 422 AND paid-count unchanged.
  This applies to MPP (`mpp.charge`) and x402 paths alike — they share the hole.
- **BUG CLASS — backend auto-routing picks a device that cannot run the job.**
  Quantum discovery accepted any ONLINE QPU; a paid quantum run charged $0.85 then
  failed `CreateQuantumTask: Device .../quera/Aquila supports ['braket.ir.ahs.program']`
  — Aquila is AHS-only (analog), can't run the QUBO/Ising IR. **Fix:** filter
  discovered devices by `deviceCapabilities` — require one of
  `jaqcd`/`annealing`/`openqasm` (gate/annealing IRs), skip AHS-only devices.
  General lesson: any solver/backend discovery must be **capability-filtered, not
  just availability-filtered**, and a paid job that fails on a bad route is a
  charge-without-value bug, not an ops note.
- **One-off live-execution toggle discipline:** to test a gated path (e.g.
  `QUANTUM_LIVE_EXECUTION=false` default), add the env to the systemd unit,
  `daemon-reload` + restart, run the test, REMOVE the env, restart — verify the
  flag is gone (`systemctl show <unit> -p Environment`) before reporting done.
  The margin guard + cost cap stay on during the test (they are the safety net,
  not the thing being tested).
- **Test-buyer wallet facts:** `test_buyer.key` (raw 64-hex, no `0x`) on the CT;
  eth_account derives the address; the x402 Python SDK needs the real import
  paths (`from x402 import x402Client, prefer_network` /
  `from x402.http.x402_http_client import x402HTTPClient` /
  `from x402.mechanisms.evm.exact import ExactEvmScheme` — top-level
  `from x402 import ExactEvmScheme` raises ImportError).
- **BUG CLASS #3 — settle happens BEFORE solver-availability is checked.**
  With the body guard in place, an explicit `mode=quantum` while all QPUs are
  offline charged $0.85, then the runner failed "no available solver for
  requested mode". **Fix (approved, deployed):** in the SAME money-path guard
  block, after body validation, call `mode_has_available_solver(mode)` (registry
  helper: any `for_mode(mode)` solver with `availability().available`) and return
  `409 {"error":"no available solver for requested mode","mode":...}` BEFORE any
  settle — covers x402 and MPP branches (guard sits before `if not
  payment_signature:`). Live proof: quantum attempt → 409, ledger byte-identical
  before/after. **The money path is only complete with all three guards:**
  body shape → 422, mode whitelist (`auto|classical|hybrid|quantum`) → 422,
  solver availability → 409. Any paid path that can fail AFTER settle is a
  charge-without-value bug.
- **TEST-ISOLATION LANDMINE — pytest conftest truncating PRODUCTION PG.**
  A session-scoped `_clean_tables` fixture doing `TRUNCATE x402_payments
  RESTART IDENTITY CASCADE` against the app's real DATABASE_URL silently wiped
  real settled-payment rows on every `pytest` run (found when the paid-test
  ledger went 4 → 0 after a suite run). **Fix (verified):** dedicated
  `cortexcloud_test` DB — conftest sets `os.environ["DATABASE_URL"]` to
  `postgresql+asyncpg://postgres:<pw>@127.0.0.1:5432/cortexcloud_test` BEFORE
  importing app modules (engine binds at import); reads the real password from
  `.env` (pytest doesn't export it — fallback "postgres" silently connects with
  the wrong password → confusing 44-error walls); a session autouse fixture
  does `import app.models` FIRST (populates Base.metadata) then
  `Base.metadata.create_all` via `engine.begin()` + `run_sync`. Create the DB
  once: `createdb cortexcloud_test`. Prove isolation with a sentinel row in the
  PROD table → run suite → sentinel must survive; assert "count unchanged"
  (not "== 0") in tests since the session-scoped truncate leaves rows from
  earlier tests.
- **Monitoring-preference (user): recurring alerts must stay MINIMAL.** The
  10-min infra watchdog was removed on request — the only accepted recurring
  CortexCloud alerts are the paid-call watchdog (every 30m, silent-ok) and the
  weekly GTM funnel report (Mon 09:00). Don't add more cron reminders without
  asking.
- **PRICING ARCHITECTURE — one effective price, everywhere (2026-08).**
  `charged = max(list_price, provider_cost × MARKUP)` must be computed with the
  REQUEST's variables, and the SAME value must appear in: the 402 challenge
  amount, the job's `price_usd`, the estimate decision block, and
  `/v1/backends` effective prices. Classic drift bugs: the optimize route
  stored the flat LIST price while the middleware charged effective (job showed
  $0.05, charged $0.85); the 402 challenge used `price_for_mode(mode)` without
  `n` so size-tier prices never reached the amount. **Fix:** one function
  `effective_price_usd(mode, provider_cost, n)` (size tiers for classical/auto:
  n≤20 $0.05 / ≤200 $0.10 / else $0.25), thread `n` through middleware + route,
  and make the margin guard + backend decision prices use EFFECTIVE (not list)
  — otherwise a provider cost above list but below effective passes the guard
  while the sale is still profitable, and cost bumps silently underprice.
  Size-tier prices only apply when the request actually carries `n`; without it
  fall back to the list floor (keeps `/v1/backends` stable).
- **Pricing honesty for quantum:** cost must be a conservative buffer over the
  OBSERVED per-run bill (fixed shots → flat cost is valid), not a stale
  constant; keep `sellable = cost ≤ effective` so ionq-class providers surface
  at $6.80 with `sellable_at_mode_price=false` instead of silently subsidizing.

## Volume & distribution (making money — the BlockRun playbook)
A clean x402scan listing alone earns nothing. The top servers (BlockRun 96k tx,
dTelecom ~$2/call voice, Cluster Protocol 685 buyers) win on DISTRIBUTION, not
pricing: an MCP server agents install in one line
(`claude mcp add <name> -- npx -y @<org>/mcp`), auto-paying via `@x402/fetch`.
Funnel instrumentation (landing beacon → referrals table → paid/wallets/repeat
metrics → weekly report, no fabricated numbers): `references/gtm-funnel-instrumentation.md`.
- Ship `templates/x402-mcp-server-index.ts` (verified: builds, boots, 15 tools,
  live calls) — copy, swap endpoints/prices, publish to npm, done.
- Builder codes (`BuilderCodeClientExtension("bc_...")`, `@x402/extensions`) are
  client-side attribution strings — no registration, attach your own code.
- Verified client stack (packages @2.20.0, 2026-08): `@x402/fetch`
  `wrapFetchWithPayment(fetch, client)` auto-handles 402 → sign → facilitator
  settle → retry. Client = `x402Client().register('eip155:8453',
  new ExactEvmScheme(viemAccount, { rpcUrl }))` where the signer is a viem
  `LocalAccount` (`privateKeyToAccount('0x...')`); RPC default
  `https://mainnet.base.org`. The payment leg CANNOT be end-to-end tested
  without a wallet funded with USDC — test the free endpoint (e.g. /models)
  to prove the full MCP path instead. npm publish of the MCP package is
  headless-BLOCKED by the user's passkey-only 2FA — must publish from his
  Lenovo (fingerprint) or via iPhone FaceID.
- Price unique data 5-10x above commodity; keep chat/embeddings cheap as traffic.
- Market pricing ground truth (x402 median $0.028/call, Braket QPU costs,
  Gurobi/CPLEX benchmarks, recommended pricing shape):
  `references/pricing-market-benchmarks.md`.
- Full evidence table + resources-page data-extraction recipe:
  `references/x402scan-marketplace-volume.md`.
- **BlockRun deep-dive** (their 92-model registry, x402scan profile showing
  RESOURCE-level 9-resource registration, 1.05-margin/$0.002-floor pricing
  formula, ClawRouter 4-tier classifier, Cloud Run stack): 
  `references/blockrun-competitive-intel.md`.
- **Beyond x402scan — other x402 marketplaces** (the402.ai, agoragentic.com,
  orbisapi, hugen.tokyo): registration endpoints, the `/api` prefix gotcha,
  Agora's Agent Trap Shield (human-approval gate on AI-initiated listings),
  and the402's webhook_url requirement. See
  `references/marketplace-registration.md` + runnable starter
  `templates/the402-onboard.mjs`.
- **Provider webhooks for marketplaces (the402.ai relay — built + verified):**
  marketplaces dispatch purchased jobs to YOUR webhook and health-check it.
  `POST /x402/v1/webhook` on the gateway: HMAC-SHA256 verify
  (`sha256=<hex>` of `${timestamp}.${rawBody}`, 5-min replay guard, compare via
  `hmac.compare_digest`), execute the `brief` with `ModelRouter.route_chat_completion`
  (same path as paid routes — reuse, don't reimplement; `model_validate(brief)` not
  `**brief`), deliver to `callback_url` with `X-API-Key`. **Fail closed:** no
  `THE402_WEBHOOK_SECRET` env → 503; the path-only paywall means unlisted paths are
  automatically exempt (no exclusion list). Full contract, activation steps, and
  deploy pitfalls (pct pull writes to HOST /tmp — stream via `pct exec ... cat`;
  502 for ~5s after restart) in `references/the402-webhook-relay.md`.
- **openapi.json prices are static and drift**: sync every op's
  `x-payment-info.price.amount` from `ROUTE_PRICING` in the regen script, then
  RESTART (the app serves the spec from memory).

## Landing page / storefront (CortexCloud — user's anti-slop rules)
The landing page is NOT a build: it is a single static file at
`/opt/CortexCloudAPI/site/index.html` served per-request by FastAPI
(FileResponse at `/`) — edit the file, done. No restart, no build step.
Backup before replacing (`cp index.html index.html.bak.<tag>`); the app also
serves `/docs`, `/openapi.json`, `/robots.txt`, `/sitemap.xml` from the app.
- **HARD user rules (stated repeatedly):** zero AI slop — no fake
  live-settlement tables/stats, no invented testimonials, no fake customer
  logos, no "LIVE" indicators (the site has zero traffic; never fake liveness),
  no blob/gradient scroll-animation theater. Real data only: prices from
  `ROUTE_PRICING`, real wallet address, real links (npm package is
  `@cortexcloud/mcp` — the old page linked a wrong `@cortexcloud.org/mcp`).
- **Preferred structure (user: "similar to api.jarvisclaw.ai"):** light theme;
  sticky nav → hero (badge, headline with one gradient-accent phrase, sub, 2
  CTAs, protocol pills `MCP · OpenAI-compatible · x402 · REST`) + dark terminal
  mock of the 402→pay→200 flow labeled "example flow" (NOT live) → numbered
  5-step pipeline (Agent→Route→Execute→Settle→Receipt) → API category card
  card grid with REAL endpoint counts + prices → 3-step get started → footer with
  wallet + builder code. System font stack, single file (~18KB).
- **Theme toggle + mobile (added 2026-08):** CSS custom properties for the whole
  palette (`:root[data-theme="dark"]` overrides), a ~10-line pre-paint script in
  `<head>` (`localStorage` pref, else `prefers-color-scheme`) to avoid flash, and a
  ☀️/🌙 button swapping `document.documentElement.dataset.theme`. Mobile ≤900px: pure
  CSS media queries — hero stacks, pipeline goes vertical with ↓ arrows, cards
  2-col ≤480px; the nav links collapse into a ☰ hamburger (toggle `.open` class,
  ~6 lines of JS). Total JS stays ~22 lines, no deps.
- **Logo/favicon swap:** no ImageMagick/PIL on this box — use **ffmpeg** for image
  work. Crop the emblem from a full lockup, scale to 512 PNG for nav/footer and
  32×32 ICO for favicon: `ffmpeg -i in.jpg -vf "crop=W:H:X:Y,scale=512:512" -frames:v 1 out.png`
  then `ffmpeg -i emblem.png -vf scale=32:32 -frames:v 1 favicon.ico`. Push into the
  site dir (`/opt/CortexCloudAPI/site/`), update the 2 `<img src>` refs (nav +
  footer), add `border-radius` so a black-background emblem reads as an intentional
  app-icon tile on the light theme. Keep old assets as `.bak` in site/. The full
  lockup file can be saved as `logo-full.png` for future OG/social use.
- **User strips sections aggressively** (removed the pricing table, live
  widget, and diagram on request) — ship the leanest honest version first; he
  removes more, rarely adds.

## AgentCash merchant discovery — the merchants.md workflow (discovery hardening)
Guide of record: `https://agentcash.dev/merchants.md` (curl it — the site renders
JS for some pages, but merchants.md is plain markdown). Decision tree: classify the
API (greenfield / existing / has-x402 / has-MPP / both) and follow the branch. For a
live x402 API the branch is "expand to both protocols + discovery hardening". Full
session detail + exact pympp wiring: `references/mpp-agentcash-discovery.md`. Key facts:
- **`npx -y @agentcash/discovery@latest discover <origin>` = LIVE spec validation.**
  Clean output (routes + `paid ... [x402]`, no warnings) means the discovery doc is
  done. **`check <origin>` is a REGISTRY-side lookup** — it prints "Not found — no
  spec data" for ANY unregistered origin, even a registered one (verified with
  tryponcho.com). Do NOT chase `check` failures pre-registration.
- **Legacy flat `x-payment-info` triggers warnings.** `{scheme, network, asset, price,
  price_atomic_usdc, decimals}` (the old CortexCloud shape) → `L2_PAYMENT_INFO_LEGACY`
  + `L2_PROTOCOLS_MISSING_ON_PAID`. Canonical shape:
  `{price: {mode: "fixed", currency: "USD", amount: "0.020000"}, protocols: [{x402: {}}]}`
  — OpenAPI amount is DECIMAL USD; runtime x402 `accepts[].amount` stays atomic
  (`0.02` → `"20000"`). Keep the legacy keys alongside the canonical ones for
  backward compat with existing x402scan listings (it parsed the flat fields).
- **Runtime 402 must fire BEFORE body/query validation** ("Expected 402, got 400" is
  a top registration failure). Probe with a garbage body, expect 402 not 400.
- **`x-discovery.ownershipProofs` format is undocumented** — optional; the validator
  never flags its absence. Ownership verification happens via `info.contact.email`
  at registration time. Don't invent a proof format.
- Registration is browser-completable (no SIWX) — the only "manual" part is the
  user rule: never register without explicit approval; show the listing preview
  first. Both x402scan and mppscan forms accept a bare hostname and live-probe
  all endpoints before enabling Add (see references/agentcash-registration.md).

## MPP (Machine Payments Protocol) via pympp — dual-protocol expansion
Official Python SDK: `pip install "pympp[tempo]"` **plus `aiosqlite` separately**
(the extra does NOT pull it; missing → `No module named 'aiosqlite'` at first use).
Full verified server wiring, event hooks, and the two-wallet myth:
`references/mpp-agentcash-discovery.md`. Key facts:
- **pympp server needs NO private key.** `recipient` is just the payee ADDRESS
  (e.g. `settings.WALLET_ADDRESS`); pympp verifies credentials on-chain via Tempo
  RPC (default `https://rpc.tempo.xyz`, chain 4217). `MPP_OPERATOR_KEY`/fee-payer
  belong to the Node `@agentcash/router` / gas-sponsorship — omit both and clients
  pay their own gas. The user CANNOT "give you a key" because there is none to store.
- `MPP_SECRET_KEY` is required (HMAC for stateless challenge IDs; `openssl rand -hex
  32`; persist across restarts — systemd drop-in, since pympp reads os.environ
  directly, not the app's .env). **realm must be set EXPLICITLY to the public
  origin** (`realm="api.cortexcloud.org"`) — auto-detect yields `localhost`.
- Server flow: `mpp = Mpp.create(method=tempo(currency=TEMPO_USDC,
  intents={"charge": ChargeIntent()}, recipient=...), realm=..., secret_key=...,
  store=await SQLiteStore.create(path))`; then `mpp.charge(authorization=
  request.headers.get("authorization"), amount="0.02")` returns a `Challenge`
  (→ 402 + `WWW-Authenticate: ch.to_www_authenticate(realm)`) or
  `(credential, receipt)` (→ proceed, record `Payment` with `payer=credential.source`).
- Tempo USDC: `0x20C000000000000000000000b9537d11c60E8b50`, 6 decimals, chain 4217.
- **Dual-protocol 402**: return BOTH `payment-required` (x402 v2) AND a genuine
  `WWW-Authenticate` MPP challenge, `x-payment-protocol: "x402, mpp"`. Garbage MPP
  credential → 402 re-challenge (never 500). OpenAPI protocols:
  `[{x402: {}}, {mpp: {method: "tempo", intent: "charge", currency: TEMPO_USDC}}]`.
- Lazy-init the Mpp singleton on first paid request (logs `MPP enabled: realm=...`)
  so missing config degrades to x402-only, not a boot failure.
- **Gitignore the replay store file** (`mpp-store.sqlite`) — it's runtime state, a
  `git add -A` WILL capture it.
- Paid MPP E2E needs REAL Tempo USDC — verify challenge format + rejection paths
  (garbage credential → 402) instead; say so honestly.

## Adding new x402-gated endpoints (verified process)
When adding new payment-gated endpoints to an x402 gateway:

1. **Implement the endpoint handler** in the appropriate route file:
   - Include proper input/output schemas for BAZAR discovery (required to avoid SCHEMA_INPUT_MISSING)
   - Handle request/response formatting according to the upstream API specification
   - Add proper error handling and logging
   - For dynamic pricing (chat/responses, embeddings, image generation), extract parameters from request body to calculate correct x402 challenge

2. **Update pricing configuration**:
   - Add route/method to `ROUTE_PRICING` with the appropriate USD price string (e.g., `"POST /x402/v1/search": "$0.015"`)
   - Add descriptive text to `ROUTE_DESCRIPTIONS` for the x402 challenge
   - Ensure the `usd_to_usdc_atomic` helper function is present for converting prices

3. **Register the route** in the main application:
   - Import the router in the x402-enabled section
   - Add `app.include_router(new_router, prefix="/x402/v1", tags=["x402 Feature Name"])`
   - Ensure the import is inside the x402-enabled conditional block

4. **Update OpenAPI specification**:
   - Add the new endpoint under `paths` with proper `operationId`, `summary`, `description`
   - Include `x-payment-info` with structured price (`{mode: "fixed", currency: "USD", amount: "0.015"}`) and `protocols: [{"x402": {}}]`
   - Add `requestBody` schema with required fields marked as `"required": true`
   - Add `responses` with both `200` (after payment) and `402` (payment required) responses
   - For GET endpoints with query parameters, ensure required parameters are marked as `"required": true` in the schema

5. **Set x402 environment variables** (if not already configured):
   - Configure `X402_ENABLED=true` and `WALLET_ADDRESS=<your_address>`
   - Reload service configuration and restart the service

6. **Restart the service** and verify it's active

7. **Test the endpoint**:
   - Verify it returns HTTP 402 with the v2 challenge in the JSON body AND the
     `payment-required` base64 header. WWW-Authenticate is fine ONLY when it
     carries a REAL MPP challenge (pympp `Challenge.to_www_authenticate(realm)`)
     — a bare/empty WWW-Authenticate trips the audit's unconditional MPP check;
     a genuine one validates clean and is required for MPP agents (verified
     2026-08: `discover` clean with `x-payment-protocol: x402, mpp`)
   - Verify it processes requests correctly when valid payment headers are provided (returns 200 with actual data)
   - Check that the OpenAPI spec includes the new endpoint with correct x402 payment info
   - Use `curl -v` to inspect headers and response body

8. **Update documentation** (if applicable):
   - Ensure the endpoint appears in the public-facing documentation
   - Verify x402scan can discover and validate the endpoint

### Dynamic pricing implementation details
For endpoints requiring dynamic pricing based on request parameters:

**Chat/Responses (`/x402/v1/chat/completions`, `/x402/v1/responses`)**:
- Extract `model` from request body
- Look up model's input/output cost per 1k tokens from model registry
- Calculate base cost: (input_cost_per_1k + output_cost_per_1k) for standard token amounts (typically 1000 each)
- Apply markup (typically 27.5% as midpoint of 25-30% range)
- Format as 6-decimal dollar string for x402 challenge
- Fall back to static price if model unknown or error occurs
- Preserve request body after reading for downstream handlers

**Image Generation (`/x402/v1/images/generations`)**:
- Extract `n` (number of images) from request body
- Pricing: $0.04 per image for n=1, or $0.035 × n for n>1 (batch discount)
- Total price = n × price_per_image
- Fall back to static price if parsing fails

**All other routes**: Use static pricing from `ROUTE_PRICING`

### Verification steps for new endpoints
1. Request without payment headers → examine 402 challenge amount (should match calculated/static price)
2. Request with known parameters → verify amount matches expected calculation
3. Request with unknown/invalid parameters → should fall back to static price
4. After successful payment, verify endpoint returns expected data
5. Confirm OpenAPI spec shows correct pricing and schemas

### Dynamic pricing implementation details
For endpoints requiring dynamic pricing based on request parameters:

**Chat/Responses (`/x402/v1/chat/completions`, `/x402/v1/responses`)**:
- Extract `model` from request body
- Look up model's input/output cost per 1k tokens from model registry
- Calculate base cost: (input_cost_per_1k + output_cost_per_1k) for standard token amounts (typically 1000 each)
- Apply markup (typically 27.5% as midpoint of 25-30% range)
- Format as 6-decimal dollar string for x402 challenge
- Fall back to static price if model unknown or error occurs
- Preserve request body after reading for downstream handlers

**Image Generation (`/x402/v1/images/generations`)**:
- Extract `n` (number of images) from request body
- Pricing: $0.04 per image for n=1, or $0.035 × n for n>1 (batch discount)
- Total price = n × price_per_image
- Fall back to static price if parsing fails

**All other routes**: Use static pricing from `ROUTE_PRICING`

### Verification steps for new endpoints
1. Request without payment headers → examine 402 challenge amount (should match calculated/static price)
2. Request with known parameters → verify amount matches expected calculation
3. Request with unknown/invalid parameters → should fall back to static price
4. After successful payment, verify endpoint returns expected data
5. Confirm OpenAPI spec shows correct pricing and schemas
