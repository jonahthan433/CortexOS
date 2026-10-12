# x402-pay-per-call

# x402 Pay-Per-Call Gateway

Class of work: AI inference/data APIs that charge agents per call in USDC on Base mainnet via the x402 protocol, settled through Coinbase's CDP facilitator.

## When this applies
- CortexCloud `api.cortexcloud.org` (Python/FastAPI on CT105 `:8000`) or the TS gateway (`~/cortexcloud-gateway` on hermes).
- Tasks: "verify the API earns", "register on x402scan", "fix 402 challenge", "prove settlement works", "make USDC today".

## Architecture (CortexCloud)
- **Python API (live, production):** CT105 LXC `192.168.1.15`, reached via `pct exec 105` through ninyi (SSH key `~/.ssh/hermes_ninyi`). App `/opt/CortexCloudAPI`, systemd `cortexcloud.service` (restart = `pct exec 105 -- systemctl restart cortexcloud`). Cloudflare tunnel → `api.cortexcloud.org`. This is the REAL settlement path.
- **TS gateway (rebuild, separate):** `~/cortexcloud-gateway` on hermes host. `verifyPayment` is a **placeholder** (Bazaar audit FAIL) — real settlement lives in the Python API. Don't confuse the two.
- **Money path:** buyer sends `X-PAYMENT` header (base64 JSON PaymentPayload) → middleware calls CDP `/verify` then `/settle` → USDC lands at merchant wallet. **Live settlement wallet = `WALLET_ADDRESS` in `/opt/CortexCloudAPI/.env` on CT105** (read-only check via `scripts/base_balance.py`). Note: the earlier skill note naming `0xE816…` is a stale/other wallet — trust `.env` for the live one.
- **PAYER NEEDS NO ETH GAS — only a signing key.** The CDP facilitator (not the payer wallet) executes the on-chain USDC transfer during `/settle`. The client's only job is to sign the EIP-3009/EIP-712 authorization (`payment-signature` header) with the payer's private key. So "wallet has 0 ETH" is NOT a blocker for a paid round-trip — the missing piece is always the payer's **private key**, never gas. (Verified against `app/middleware/x402.py` settle path; user corrected this assumption twice.)
- **Server wallet-key inventory (CT105 + hermes host) lives in `references/wallet-keys.md`** — file → derived address. **`test_buyer.key` (CT `/root/bzcheck/`) == `~/.cortexcloud/.session` (host) == the funded buyer `0xCed8…374A` (~7.31 USDC).** An earlier note claiming `0x0c0ddf…420f` and "no key" was STALE — a 2026-08-07 real paid round-trip signed with it and recovered `0xC0…374A`. This key is the payer for paid tests.
- **Sole payout wallet (since 2026-08-08):** `WALLET_ADDRESS` = `0x5A0353Bc…88eA143` in `/opt/CortexCloudAPI/.env` + systemd `Environment=`; the old `0xab55B638…018008` and `WALLET_ADDRESS_2` are RETIRED (removed from both). **Do NOT emit duplicate `accepts[]` entries with the same payTo** — x402scan L3 validation rejects duplicate payment options (proven 2026-08-12: a `WALLET_ADDRESS_2 or WALLET_ADDRESS` fallback made the second entry identical to the first, and the listing dropped to "1 resource up"). Emit a second entry ONLY when a distinct second wallet is actually set. **Wallet-swap trap: systemd `Environment=` OVERRIDES pydantic's `.env`** — a swap needs BOTH edited + `daemon-reload` + restart, then verify live `accepts[].payTo`, the MPP log `recipient=` line, regenerated openapi, and one real paid call's on-chain Transfer `to` (full procedure in `cortexcloud-platform` skill). MCP payTo is separate: `CORTEXCLOUD_PAYTO` env in the mcp service (default already the same address).

## KEY TECHNIQUE — prove settlement auth is LIVE without a real payment
You can't send real USDC to test (needs funded wallet + on-chain tx). Instead probe the CDP facilitator with a **malformed** payload:
- POST `{X402_FACILITATOR_URL}/verify` with the app's signed JWT headers + a bogus `paymentPayload`.
- **HTTP 400** with structured `"paymentPayload is invalid"` = auth JWT was ACCEPTED → settlement path is live. ✅
- **HTTP 401** = auth failed → CDP key id/secret wrong/expired. ❌
- A bare `curl` with no auth also returns 401 — that proves nothing about the app. Must use the app's headers.
- See `scripts/verify_facilitator.py` (run inside CT105) and `references/cdp-facilitator-verification.md`.

## REAL paid round-trip (the definitive proof — spend ~$0.005, don't fake it)
When the user wants to prove settlement actually lands ("run paid tests"), do a REAL `@x402/fetch` payment, then read on-chain balances. Reuse `~/cortexcloud-mcp/proxy` (esbuild-bundled TS, deps installed). Buyer key = `test_buyer.key` staged to host tmp (see wallet-keys ref).
- **Target the right wallet with `paymentRequirementsSelector`.** The `@x402` client DEFAULT selects `accepts[0]` (the ORIGINAL wallet). To pay the SECOND/new wallet pass `new x402Client((_v, accepts) => accepts.find(a => a.payTo?.toLowerCase() === NEW.toLowerCase()))`. Constructor signature is `x402Client(selector)`. `wrapFetchWithPayment(fetch, client)` (NOT `(fetch, {account, client})`).
- Force the model + tiny max_tokens, then confirm on-chain: read both payer and target `balanceOf` on Base USDC via public RPC (web3). Success = target up exactly `0.005000`, payer down.
- **Debug tip — see what the client sends:** monkeypatch `globalThis.fetch` and read the `Request.headers` via `.entries()` (the wrapper passes a `Request` object, not separate init — a plain `Object.entries(headers)` returns nothing). Decode `payment-signature` header (base64 JSON): `{x402Version, accepted, payload:{authorization{from,to,value,validAfter,validBefore,nonce}, signature}}`.
- **viem `privateKeyToAccount` REQUIRES a `0x`-prefixed hex key.** Raw 64-hex key files (e.g. `test_buyer.key`) fail with `invalid private key, expected hex or 32 bytes, got string`. Prefix: `pk = raw.startsWith("0x") ? raw : "0x" + raw`. (Shell-quoting raw keys into `node -e` also mangles them — write the test to a `.mjs` file and scp/push it instead.)
- **Get the settlement tx HASH via `eth_getLogs` on the public Base RPC** (`https://mainnet.base.org`). BaseScan V1 API is deprecated ("You are using a deprecated V1 endpoint"), Etherscan V2 requires a paid plan for chain 8453. Filter: address = USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, Transfer topic `0xddf252ad…523b3ef`, args `{from: payer, to: payTo}`, scan back ~1200 blocks (~40 min at 2s/block). Confirms amount + block + txHash for the report.

## Pitfalls — in-process payverify (payverify.py) that silently SWALLOWED every real payment
Section-1 hardening added a local EIP-712 proof verify BEFORE the CDP /settle round-trip. It was never exercised by a real `@x402` client until this session, and TWO bugs rejected every real payment with 402. Fixes (all in `app/middleware/payverify.py`):
- **Signature is a SIBLING of authorization.** `@x402` v2 returns `payload:{authorization{...}, signature}` — NOT `authorization.signature`. `verify_proof` computed `auth = payload.payload.authorization` and read `auth.signature` → always `"missing signature"`. Attach: `if not auth.get("signature") and (payload.get("payload") or {}).get("signature"): auth["signature"] = payload["payload"]["signature"]`.
- **Wrong EIP-712 `primaryType` name.** The client signs `primaryType: "TransferWithAuthorization"`; the verifier declared the struct as `"TransferAuthorization"`. The EIP-712 type hash encodes the type NAME, so recovery always failed → `"signature does not match payer"`. Rename `_AUTH_TYPES` key AND the `primaryType` to `TransferWithAuthorization` (exactly as the `@x402/evm/client` `authorizationTypes` does). Do NOT "fix" the client — mirror the real one.
- Chain of 402 errors while debugging a paid call: `"missing signature"` = sig not found in auth (sibling issue); `"signature does not match payer"` = type-name mismatch; `"recipient mismatch"` = payTo not in {WALLET_ADDRESS, WALLET_ADDRESS_2}. Old fixes to `request._receive`/body re-inject were for the TS gateway, unrelated to these.

## x402scan registration (agent-runnable via web form, NO SIWX)
- Discovery/audit CLI: `npx -y @agentcash/discovery@latest discover "https://api.cortexcloud.org"` — reads `/openapi.json`, validates 402 v2 challenges, lists routes. `check "https://api.cortexcloud.org/<path>"` audits one endpoint (works as of 2026-08; use it to isolate a single route's status). **`check` on the ORIGIN URL (`check https://origin`) returns "Not found — no spec data for this endpoint" even for REGISTERED origins — it's registry-side; always pass a full endpoint path.** Zero critical errors = clean; L2/L3 warnings are advisory.
- **Resource "up/down" on the dashboard = L3 spec validation, NOT endpoint uptime.** Proven 2026-08-12: every endpoint returned correct codes (200/402/204/404) yet the listing showed "1 resource up" out of 15. The scanner's checker (`@agentcash/discovery`) infers per-route auth mode from the OPENAPI SPEC: an operation with neither a security scheme nor `x-payment-info` nor `security: []` gets `L2/L3_AUTH_MODE_MISSING` and fails. Required declarations:
  - Paid route (`/v1/optimize`): `"x-payment-info"` → authMode `paid`.
  - Every FREE route: `"security": []` → authMode `unprotected`. (Empty array, not missing.)
  - `info.contact` (email+url) and `info.x-guidance` (a short guidance string) on the root info object.
  - With these, `discover` prints routes as `POST /v1/optimize  paid 0.05 USD [x402, mpp]` and every free route as `unprotected`.
- **Run the validator LOCALLY instead of waiting for the dashboard crawl.** `npm pack @agentcash/discovery@<version>` → `tar xzf` → `npm install` in the extracted dir → `node bin/discovery.js https://origin` (optionally with `?cb=$(date +%s)` cache-buster). Prints every route + authMode + all L2/L3/L4 warnings. The validator source is readable (`dist/index.js`): key functions `inferAuthMode`, `resolvePaymentInfo`, `extractSchemas2` (v2 bazaar schema must be `extensions.bazaar.schema.properties.input.properties.{body|queryParams}` and `.output.properties.example`), `isUsableStatus` (402 or 2xx). Read it before debugging a probe rejection.
- **Structured `x-payment-info` beats legacy flat.** The flat format (`pricingMode/price/minPrice/maxPrice`) triggers `L2_PAYMENT_INFO_LEGACY` (advisory only). Structured:
  ```json
  "x-payment-info": {
    "price": {"mode": "dynamic", "currency": "USD", "min": "0.05", "max": "1.503"},
    "protocols": [{"x402": {"network": "eip155:8453", "asset": "USDC"}},
                  {"mpp": {"method": "tempo", "intent": "charge", "currency": "USD"}}]
  }
  ```
  `mode` is `fixed` (with `amount`) or `dynamic` (with `min`/`max`); currency ISO-4217.
- **openapi.json is loaded into MEMORY at app startup** (`application.openapi = lambda: _OPENAPI_SPEC` in main.py). Patching the file has NO effect until `systemctl restart cortexcloud.service`. Verify against localhost first (`http://127.0.0.1:8000/openapi.json` via `pct exec`), then the public edge with a cache-buster.
- **Register-page probe "No valid x402 response found" on a route = its v2 challenge failed deep validation.** Hit 2026-08-08: numeric `accepts[].amount` (`20000`) failed the schema (`amount` must be a **string** `"20000"`); `discover` stayed green (it reads the spec, not the runtime header). Fix = `str(required)` in the middleware challenge; the register page re-probes live and the button flips to "Add API (N resources)". The AgentCash package's own schema is in `node_modules/@agentcash/discovery/dist/cli.js` (`PaymentRequirementsV2Schema.amount: z.string().min(1)`) — read it before debugging a probe rejection.
- **Registration: agent does it in the browser** at `https://www.x402scan.com/resources/register` — type the bare origin, wait for the live endpoint crawl, click "Add API (N resources)". No wallet connect, no SIWX signature (as of 2026-08). Merchant page = `tryponcho.com/m/api.cortexcloud.org`.
- **Spec correctness gates the crawl count.** Per-op `security` controls which routes are probed for 402: `[{x402:[]}]` = paid (probe); `[]` = free/legacy (excluded); a free route carrying `x-payment-info` still fails → drop the block entirely. The OpenAPI generator must be **idempotent on `security`** (`op.pop("security",None)` before re-apply) or stale keys compound errors.
- **MPP (Tempo) is LIVE since 2026-08-08** — mppscan registration succeeded (14 resources, same merchant page). Dual-protocol 402: `payment-required` header (x402) AND `WWW-Authenticate: Payment id=…, realm="api.cortexcloud.org", method="tempo", intent="charge"` (MPP), `x-payment-protocol: "x402, mpp"`. Server-side implementation = **pympp** (official Python SDK), NOT the Node router:
  - `pip install "pympp[tempo]"` **plus `aiosqlite` explicitly** — the extra does NOT pull aiosqlite; without it MPP init fails with `No module named 'aiosqlite'` (caught + logged as "MPP disabled").
  - **No operator private key is ever needed server-side** (contrary to merchants.md's `MPP_OPERATOR_KEY` — that's the Node router). pympp verifies credentials on-chain via Tempo RPC; `recipient` is just the address (`settings.WALLET_ADDRESS`); clients pay their own gas. Nothing secret to store beyond `MPP_SECRET_KEY`.
  - `MPP_SECRET_KEY` = `openssl rand -hex 32`, persisted in `/etc/systemd/system/cortexcloud.service.d/mpp.conf` (rotating invalidates outstanding challenges). `realm=` MUST be the public origin (`api.cortexcloud.org`) — auto-detect picks `localhost`.
  - Wiring: lazy singleton `_get_mpp()` in `app/middleware/x402.py`; `Mpp.create(method=tempo(currency="0x20C000000000000000000000b9537d11c60E8b50", intents={"charge": ChargeIntent()}, recipient=…), realm=…, secret_key=…, store=await SQLiteStore.create("mpp-store.sqlite"))`. Credential path: `Authorization` header present → `mpp.charge(authorization=…, amount=price.lstrip("$"))`; Challenge → 402+WWW-Authenticate; `(credential, receipt)` → record Payment, proceed. OpenAPI protocols: `[{"x402": {}}, {"mpp": {"method": "tempo", "intent": "charge", "currency": "<tempo usdc>"}}]`.
  - `mpp-store.sqlite` (replay protection) is gitignored.
- After registering, verify the listing crawls the paid endpoints (title = "CortexCloud API • Poncho").
- Full mechanics in `references/x402scan-registration.md` (form walkthrough, security-declaration rules, mppscan).

## Pitfalls
- **Changing a mode's price touches TWO price sources + derived refs.** Customer price = `MODE_PRICE_USD` in `app/x402/pricing.py` (drives challenge amount via `price_for_mode`/`usd_to_usdc_atomic`); the solver's own `*_PRICE_USD` (e.g. `CLASSICAL_PRICE_USD` in `app/solvers/classical.py`) is the PROVIDER-COST side of the benchmark margin ledger (`margin = price_usd - provider_cost`). For local solvers set it to `0.0` so margin = full customer price. Also update `ROUTE_PRICING` string, middleware `OUTPUT_EXAMPLES`, `examples/agents.json` pricing refs, README/landing price table, and the test asserting atomic amount (`tests/test_api.py`), then regenerate `openapi.json` + restart. Verify with one real paid call: challenge amount, on-chain Transfer value, ledger `margin_usd`. (2026-08-08: classical $0.02 → $0.05.)
- **Cloudflare caches stale 404 for `/favicon.ico`** (~4h). A "favicon missing" warning may be a stale cache, not a real gap. Verify with cache-buster `?v=2`; can't purge without a CF token with Cache Purge perm.
- **CDP headers are built inline**, not a module-level helper: `from cdp.x402.x402 import create_cdp_auth_headers; create_cdp_auth_headers(key_id, key_secret)()`. You cannot `from app.middleware.x402 import _cdp_headers` — that symbol doesn't exist at module level.
- **x402 v2 challenge must include `extensions.bazaar.schema`** (input/output) or discovery validator flags `SCHEMA_INPUT_MISSING`. `accepts[].amount` is an atomic-amount STRING (e.g. `"5000"`), not `maxAmountRequired`. Network = `eip155:8453` (Base mainnet), asset `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` (USDC).
- **Cloudflare/TLS-terminating-proxy trap (breaks REAL prod payments, not just smoke):** behind Cloudflare, `request.url.scheme` inside the middleware is `http` even though the public URL is `https`. The x402 challenge `resource.url` then comes out `http://…` and the CDP facilitator rejects it (`^https://.*$` regex on `/validate` + `/settle`). Fix in `_resource_url`: `scheme = request.headers.get("x-forwarded-proto", request.url.scheme)`. After the fix the public `https://api.cortexcloud.org/...` challenge is https natively — that's the real smoke target (no cloudflared tunnel needed for prod). The `X402_RESOURCE_BASE` env is a fallback only; the proxied-request path is what prod hits.
- **systemd `Environment=` overrides pydantic `.env`.** For the CT105 `cortexcloud.service`, prod flags (AI_ENABLED, X402_RESOURCE_BASE, INTERNAL_TOKEN) live in the drop-in `/etc/systemd/system/cortexcloud.service.d/mpp.conf`, NOT `.env`. Editing `.env` for a prod flag is a no-op — add to drop-in, `daemon-reload`, restart.
- **`/internal/metrics` 500s with `func.interval("24 hours")`** — SQLAlchemy's `func.interval()` renders `interval($1::VARCHAR)` which Postgres rejects (syntax error near `$3`). Use `text("interval '24 hours'")` literally instead. This endpoint is disabled (503) unless `INTERNAL_TOKEN` is set; enable it in the drop-in to verify AI revenue/margin aggregates after a paid run.
- **`info.contact.email` is required for ownership/merchant-page** (x402scan `OPENAPI_CONTACT_MISSING`). Set it to the user's real contact (user chose `ampwerajonathan50@gmail.com` for now) — a stale domain email on the page is the "fake data" class the user bans.
- **A stale/incorrect `security` proofreading pass before registration saves a cycle.** One bad generator run pushed the probe error count 3→34; the fix was making `security` idempotent and clearing `x-payment-info` off free routes. Verify the spec at the public edge (Chrome UA) before re-crawling.
- **L3_INPUT_SCHEMA_MISSING on genuinely-parameterless GETs is NOT a critical failure.** Paid GETs like `defillama/chains`, `crypto/list`, `fx/list` return full datasets with zero query params — `parameters: []` is the honest schema. `discover`/`check` still emit L3_INPUT_SCHEMA_MISSING for them; do NOT fabricate a fake `limit`/optional param the upstream ignores (that's the "fake data" class the user bans). Leave `parameters: []`; L3 is advisory, not blocking — silence it only by adding a real honored param to the handler, not by lying in the spec.

## Money-path hardening: settle LAST, after every rejection the endpoint would raise
The x402 middleware settles BEFORE FastAPI's input-validation middleware runs —
a malformed paid body got CHARGED (proven: a 422-destined request settled $0.05).
Guard order in the middleware, all BEFORE the settle call (both x402 and MPP
branches — guard once before `if not payment_signature:`):
1. **Body-shape validation** → 422 (mirror the endpoint's required fields:
   `problem` dict, `n` int, `data` dict, `problem_type` in allowed set).
2. **Mode whitelist** → 422 (`auto|classical|hybrid|quantum`; unknown mode would
   otherwise charge then fail).
3. **Solver availability** → 409 `{"error":"no available solver for requested
   mode","mode":...}` (e.g. quantum with all QPUs offline). Charge-then-fail on
   an unavailable backend is the same bug as charge-then-422.
Price/amount checks (effective price, margin guard) also run pre-settle. Live
proof pattern: attempt the paid flow with a bad body → expect 422 and a ledger
row count UNCHANGED; with an unavailable mode → 409 and ledger unchanged.

## Test isolation: pytest must never touch the production DB
A session-scoped conftest `_clean_tables` fixture TRUNCATING
`x402_payments`/`benchmarks`/`opt_jobs` on the LIVE database wiped real settled
payments. Rules for the conftest:
- Point at a dedicated scratch DB (`cortexcloud_test`, created once via
  `createdb`): set `os.environ["DATABASE_URL"]` **before importing any app
  module** (engine binds at import), and **read the DB password from the app's
  `.env`** (pytest doesn't export it — the env-var fallback silently connects
  with the wrong password and every test errors).
- `_create_test_tables` must `import app.models` FIRST (Base.metadata is empty
  until model modules import → create_all creates nothing).
- Prove isolation with a sentinel row in PROD before a test run; assert it
  survives (not just "test DB has rows").
- `docker exec` needs `-i` to pass heredoc stdin.

## Quantum backend discovery: exclude AHS-only devices
Braket discovery filtering `deviceType == "QPU"` + ONLINE accepts QuEra Aquila
(AHS/analog only) — a paid QUBO job on it fails after charging. Filter on
`deviceCapabilities` containing any of `jaqcd | annealing | openqasm`; AHS-only
devices can't run the QUBO IR. Availability then honestly reports "no ONLINE QPU"
instead of routing money into a guaranteed failure.

## Honest ceiling on "make money today"
Cold x402scan discovery traffic is thin — organic agent calls in one day are unlikely. Realistic same-day revenue = register on x402scan AND route your own agent workloads through your own API (you're the most likely buyer). Dogfooding the paid loop proves it end-to-end.

## Support files
- `references/cdp-facilitator-verification.md` — facilitator endpoint, auth model, the probe, result reading, real e2e self-test.
- `references/x402scan-registration.md` — discover/check CLI, clean-state example, SIWX registration steps, discovery-spec gotchas.
- `references/endpoint-battery.md` — test ALL endpoints: stale-openapi trap, Cloudflare 530 flake, origin test, expected status map (200/402/401/503), bash `local code body` gotcha.
- `references/wallet-keys.md` — key-file inventory (both machines) + how to safely find a payer key by address.
- `scripts/verify_facilitator.py` — re-runnable auth-liveness probe for CT105.
- `scripts/base_balance.py` — read ETH + USDC (Base) balance of the live `WALLET_ADDRESS` via public RPC (no keys). Run from Hermes host; pass an address arg to check any wallet.
- `scripts/scan_keys.py` — sweep a dir for raw 64-hex keys, derive+print only addresses (never key material). Run on CT venv python.
