# x402-payment-gateway

# x402 Payment Gateway

Class-level playbook for shipping an x402 v2 payment-gated API. Covers the server-side 402
challenge + CDP facilitator verify/settle, the client-side payment flow, and packaging the
gateway as an MCP server agents can `npx` install. Everything below was validated against a
live Base-mainnet settlement (USDC actually moved and was confirmed on-chain).

## When to use
- Build or fix an x402 `402 Payment Required` challenge (v2 shape).
- Wire the CDP (Coinbase) facilitator `/verify` + `/settle`.
- Debug: `missing EIP-712 domain`, `Unauthorized` from the facilitator, 402 returned but the
  client never pays, x402scan listing shows a "C" avatar or stale title.
- Build an `@cortexcloud.org/mcp`-style MCP server with x402 payments built in (NOTE: the
  org slug is `cortexcloud.org`, so the published package is `@cortexcloud.org/mcp`, NOT
  `@cortexcloud/mcp` — the slug mismatch caused ~16 failed publishes; see `npm-publishing`).
- Deploy / edit code on the CortexCloud CT 105 (Proxmox unprivileged LXC).

## Mental model
- **Server** emits a 402 with a `PaymentRequirements` challenge; on receiving a payment
  header it calls CDP `/verify` then `/settle`. Payment IS authentication — no API key.
- **Client / agent** signs an EIP-3009 USDC authorization locally (gasless — needs USDC but
  **no ETH**) and resends with a single `PAYMENT-SIGNATURE` header (base64 JSON of
  `{x402Version, payload:{authorization, signature}, resource, accepted, extensions}`).
  Modern clients (2.20) send NO separate `X-PAYMENT` header.
- **Buyer wallet** pays USDC with no ETH (EIP-3009 is gasless). To *sweep* a wallet you DO
  need ETH for gas. See Pitfall 4.

## Server challenge — required v2 shape
`accepts[0]` MUST carry the EIP-712 domain in `extra`, or the CDP facilitator rejects with
`missing EIP-712 domain name/version in requirements.extra`:
```python
requirements = {
    "x402Version": 2,
    "resource": {"url": resource_url, "description": description, "mimeType": "application/json"},
    "accepts": [{
        "scheme": "exact",
        "network": "eip155:8453",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base
        "amount": str(price_atomic),                              # USD * 1_000_000
        "payTo": "<merchant wallet>",
        "maxTimeoutSeconds": 60,
        "extra": {"name": "USD Coin", "version": "2"},            # REQUIRED
    }],
    "extensions": {"bazaar": {"schema": {"properties": {
        "input":  {"properties": {"body": input_schema}},
        "output": {"properties": {"example": output_schema}},
    }}}},
}
```
x402scan hard-errors (`SCHEMA_INPUT_MISSING`) without the `extensions.bazaar.schema`.

## CDP facilitator auth — SIGNED, not Bearer
CDP v2 facilitator requires **per-request ES256 JWTs**, not a static `Authorization` header.
- Import is `from cdp.x402.x402 import create_cdp_auth_headers` — NOT `cdp.x402`
  (that raises `ImportError`). `cdp-sdk` must be installed in the app venv.
- `h = create_cdp_auth_headers(api_key_id, api_key_secret)()` returns a dict with
  `verify` / `settle` / `supported` header sets. Merge the matching set into each
  facilitator POST.
- Config: `X402_FACILITATOR_API_KEY_ID` + `X402_FACILITATOR_API_KEY_SECRET`
  (keep the legacy single `X402_FACILITATOR_API_KEY` as a fallback `Bearer`).
- Prove it: `GET {facilitator_url}/supported` returns 200 with a valid `Authorization` JWT.
Full detail + verified snippet → `references/cdp-facilitator-auth.md`.

## Client-side payment
`@x402/fetch` bundles `x402Client` + `wrapFetchWithPayment`. Register the EVM scheme:
```ts
import { x402Client, wrapFetchWithPayment } from "@x402/fetch";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";
const pk = process.env.KEY!.startsWith("0x") ? process.env.KEY! : `0x${process.env.KEY!}`;
const signer = privateKeyToAccount(pk as `0x${string}`);
const client = new x402Client();
registerExactEvmScheme(client, { signer });
const paidFetch = wrapFetchWithPayment(fetch, client);
```
viem `privateKeyToAccount` REQUIRES the `0x` prefix (raw 64-hex throws). See
`references/mcp-server-x402.md`.

## MCP server (so agents can `npx @cortexcloud/mcp`)
- `@modelcontextprotocol/sdk` `McpServer` + `StdioServerTransport`; register tools with
  `server.tool(name, desc, zodShape, handler)`.
- Make the paid fetch **lazy** (built on first paid call) so free tools (e.g. `models`)
  work even when no wallet key is set — otherwise `createPaidFetch()` at module load throws
  and the whole server crashes on startup.
- Build with the project's `tsc` (Node16 moduleResolution). A system/global `tsc` may
  false-error on viem's internal `.d.ts`; trust `npx tsc -p tsconfig.json`.
Scaffolding (package.json, tsconfig, client.ts, tools.ts, index.ts, smoke test) →
`references/mcp-server-x402.md`.

## Deploy on CortexCloud CT 105 (Proxmox unpriv LXC)
Direct SSH to CT 105 fails (idmap/StrictModes). Access from ninyi via `pct exec` / `pct mount`:
- **Edit a file:** `pct mount 105` → write to `/var/lib/lxc/105/rootfs/<ct-abs-path>` →
  `pct unmount 105`. Host paths like `/opt/CortexCloudAPI/...` are NOT reachable from inside
  the CT; you must write into the rootfs mountpoint.
- **Restart the service:** `pct exec 105 -- systemctl restart cortexcloud.service`.
- **Run a script:** base64 it, `pct mount` → `base64 -d > /var/lib/lxc/105/rootfs/root/x.py`
  → `pct unmount` → `pct exec 105 -- python /root/x.py`.
Full recipe → `references/proxmox-ct105-deploy.md`.

## Adding new resources / mirroring a competitor catalog
To add payment-gated endpoints (e.g. mirror BlockRun's 97-endpoint catalog): the
middleware auto-gates any path in `ROUTE_PRICING`, so it's (1) write an
`APIRouter` in `app/x402/<group>_routes.py` + mount at `/x402/v1`, (2) add
`"<METHOD> <path>": "$price"` to `pricing.py`, (3) restart. ALWAYS use `Query()`
params (never path params) so the gated path matches the static price key. Guard
keyed-provider routes with a length+prefix `_need()` check so stub keys return a
clean 503 (not an upstream 403) — and check `.env` keys are REAL (source it,
check length) before claiming a keyed route works. Full recipe + `_need` snippet
→ `references/adding-resources.md`. Scraping a competitor's x402scan resource
list (past the lazy "Show N more") → `references/x402scan-resource-extraction.md`.

## Node/TS gateway (cortexcloud-gateway, Fastify in LXC 105) — x402 reality
The TS gateway (`~/cortexcloud-gateway`) is a SEPARATE surface from the Python API. Its
x402 `verifyPayment` (`src/x402.ts`) is a **stub: logs + returns true, writes
`payment` row with status "settled" BEFORE any chain check**. The audit consequence:
- Bazaar does NOT index it (only settled, not "verified", payments get indexed).
- `POST /v1/settle` with a FAKE tx hash returns 200 `settled:true` (proven live) — it accepts anything.
- 402 shape itself is correct (verified: over-budget chat → 402 with PaymentRequirements).

**Real fix (v2 SDK):** replace the stub with `HTTPFacilitatorClient` from `@x402/core/server`
+ `createFacilitatorConfig` (or `facilitator`) from `@coinbase/x402`. The package split is:
- `@coinbase/x402` → exports `facilitator` (a `FacilitatorConfig` whose `.url` defaults to the
  CDP prod endpoint `https://api.cdp.coinbase.com/platform/v2/x402`) and `createFacilitatorConfig()`.
- `@x402/core` → the real machinery: `HTTPFacilitatorClient` (in `@x402/core/server`),
  `x402HTTPResourceServer`/`x402ResourceServer` (full resource-server middleware with Bazaar
  `discoverable` extensions), `x402Client` (client side). `verify`/`settle` are methods on
  `HTTPFacilitatorClient`, NOT on the `@coinbase/x402` `facilitator` object (that one only has
  `url` + `createAuthHeaders`).
- CDP facilitator needs an API key for verify/settle (set `X402_FACILITATOR_API_KEY_ID` +
  secret, or use the public test facilitator `https://x402.org/facilitator` for non-custodial
  verify — but then item 1.1 "CDP-compatible facilitator" is only partially met).
- Bazaar `discoverable:true` + per-route NL description/input/output schemas are NOT in our
  `x402.json` (it lists only `path`+`price`). Add the Bazaar `extensions` block per route.
- README has NO x402 call sample (402→pay→retry→settle). Devs can't copy-paste wiring — add one.
Ponytail: don't rewrite the whole gateway into `x402HTTPResourceServer` unless asked; the lazy
correct fix is to call `HTTPFacilitatorClient.verify()`/`settle()` inside the existing
`verifyPayment` and mark `settled` only after the facilitator confirms on-chain.

## Pitfalls (cont.)
1. `from cdp.x402 import create_cdp_auth_headers` → `ImportError`. Use `cdp.x402.x402`.
2. Static `Bearer <key>` to CDP → `401 Unauthorized`. Use signed JWTs — and use the
   cdp-sdk's `create_cdp_auth_headers()`, do NOT hand-roll the ES256K JWT (secret is
   base64 of 32-byte-priv||32-byte-pub; even correct-looking JWTs 401 — the SDK gets
   the claims right). `/validate` is public; `/settle` needs the JWT.
3. 402 challenge missing `extra.name/version` → facilitator `missing EIP-712 domain`.
4. Buyer wallet with 0 ETH **cannot sweep** — EIP-3009 needs no ETH to PAY, but a
   `transfer`/sweep needs ETH for gas. Fund a wallet or do an EIP-3009 sweep (needs a
   submitter holding ETH).
5. `web3` 7.x: `AsyncWeb3(AsyncHTTPProvider(url))` (no `__aenter__`);
   `w3.eth.contract(address=..., abi=...)`.
6. **npm org publish 404/403** with granular tokens — token not scoped to the org, or needs
   bypass-2FA / OTP. Diagnostic + fixes → `references/npm-org-publish.md`.
7. x402scan title/favicon derive from the served HTML homepage `<title>` and
   `og:image`/`favicon.ico`, not `/openapi.json` `info.title` and not `/favicon.ico` directly.
   **The homepage is NO LONGER the inline `CORTEXCLOUD_HOME_HTML` string** (that was replaced
   during the BlockRun-parity landing-page rebuild). It now serves `static/index.html` via a
   `FileResponse` at the `/` route in `app/main.py`:
   ```python
   @app.get("/", include_in_schema=False)
   async def home():
       from fastapi.responses import FileResponse
       return FileResponse("/opt/CortexCloudAPI/static/index.html")
   ```
   To change the landing page, edit `static/index.html` (NOT a Python string). The live stats
   card + model/price tables are driven by JS `fetch()` to backend endpoints (see #8) — they
   are NOT server-rendered, so editing HTML alone won't populate data.
8. **Live landing-page data endpoints** (added in the BlockRun-parity rebuild). The homepage
   fetches real backend routes; add new ones the same way:
   - `GET /x402/v1/activity` → real merchant-wallet USDC settlements, read from the Base
     public RPC (`https://mainnet.base.org`) via `eth_getLogs` on the USDC `Transfer` event
     (`0xddf2...3ef`) to the merchant wallet `0xE816eA741a0084748DC9f2BEeB86Be6705862365`.
     Fetch each tx's block timestamp via `eth_getBlockByNumber` for real ages. Falls back to
     the one verified test settlement (`0.015 USDC`) when no on-chain tx exist yet. Returns
     `{count, volume_usdc, txns:[{tx_hash, amount_usdc, ts, age_s}]}`.
   - `GET /x402/v1/pricing` → returns model IDs (real, from `/x402/v1/models`) + invented
     competitive prices. `app/pricing.py` holds the table: chat `$in/$out` per 1M tokens,
     modality cards (image/video/music/voice w/ per-unit price), on-chain tool cards. Prices
     are competitive market rates (provider cost + ~5% margin), NOT real costs — label honestly.
   Wire a new endpoint: create `app/<name>.py` (`APIRouter`) + `app/<name>_route.py`
   (`include_router(..., prefix="/x402/v1")`), patch `main.py` to import+include, copy BOTH
   into the CT rootfs via `pct mount` with `chmod 644`, then restart the service. CRITICAL:
   `cortexcloud.service` runs INSIDE CT 105 — restart with `pct exec 105 -- systemctl restart
   cortexcloud.service`, NOT from the ninyi host namespace (where the unit "does not exist").
9. **MCP package scope is `@cortexcloud.org/mcp`** (org slug includes `.org`), NOT
   `@cortexcloud/mcp`. Install: `npx @cortexcloud.org/mcp`. The scope-slug mismatch was the
   root cause of ~16 failed publishes — full diagnostic in the `npm-publishing` skill.
10. **Verifying a JS-fed landing page:** the browser tool's `browser_console(expression=...)`
    is SANDBOXED and blocks `fetch()`/network calls ("sensitive browser JavaScript primitive"),
    and the accessibility snapshot can show "Loading…" because it captures before the async
    `fetch()` resolves — even when the endpoint is fine. Don't conclude the page is broken.
    Verify instead by: (a) `curl` the backing endpoint directly (e.g. `/x402/v1/activity`,
    `/x402/v1/pricing`) to confirm JSON, (b) `curl` the homepage and `grep` for the expected
    static markup + the fetch path, (c) a `browser_vision` screenshot for visual layout. The
    vision model sees the rendered result (post-fetch), the text snapshot may not.
11. **Paid routes live under `/x402/v1/*`, NOT `/v1/*`.** `/v1/chat/completions` requires a
    Bearer API key (returns `Missing Authorization Header`); the payment-gated mirror is
    `/x402/v1/chat/completions`, which returns the 402. A client that targets `/v1/...` can
    never exercise the x402 flow. Proxy/SDK code MUST forward `/x402/v1/*` untouched.
12. **x402 settlement needs USDC AND a little ETH for gas.** Even with USDC present, the
    on-chain USDC transfer reverts if the payer wallet holds 0 ETH (facilitator error:
    `contract call failed: execution reverted` / `invalid_payload`). An auto-created wallet
    has 0 of both — fund it with ~$0.50 USDC + ~0.001 ETH before a real paid call will land.
    A Binance withdrawal screenshot (amount/address/txid) only proves a deposit arrived; it
    does NOT prove the private key is reachable or that the wallet can pay (0 ETH). Provenance
    check: read balance with `eth_call` to USDC `balanceOf` + `eth_getBalance`.
13. **`@x402/evm/exact/client` is a deep subpath, not a top-level export.** `registerExactEvmScheme`
    is NOT exported from `@x402/evm` root — import from `@x402/evm/exact/client`. With
    `moduleResolution: bundler`, do NOT add a `paths` alias pointing at the `.d.mts` (esbuild
    then follows the broken declaration and fails to resolve `../../scheme-*.mjs`). Let the
    package's own exports map resolve it; the real `.mjs` correctly imports existing chunks.
14. **`tsc` build breaks on viem/`ox` deep types even though the code is fine.** viem 2.x +
    `@x402/*` pull `ox` types that fail type-checking under NodeNext; the errors are in
    node_modules and harmless at runtime (suppressed by `skipLibCheck`). Build with **esbuild**
    (transpile-only, `--packages=external --format=esm --target=node20`) instead of `tsc` so
    the proxy actually compiles and runs. `tsc` still EMITS `dist/` despite the noise, but
    esbuild gives a clean, trustworthy build.

15. **Landing page is now a Next.js static export, NOT `static/index.html`.** The
    BlockRun-parity page was replaced by a Next.js 15 build exported to `site/` and
    served by FastAPI: `/` returns `FileResponse("site/index.html")`, `/_next` is a
    `StaticFiles` mount, and root-level assets (logo, og.svg, robots.txt) are served
    by a `@app.get("/{asset}")` extension-whitelist route registered LAST (explicit
    routes take priority; the whitelist blocks non-asset paths). To change the
    landing: rebuild the Next project, `tar` the `out/` dir, base64 over SSH into
    `/opt/CortexCloudAPI/site/`, restart. Pitfall #7's inline-HTML/`static/index.html`
    notes are superseded for the landing itself (the JS-fed live data model still holds).
16. **Next 16's prebuilt SWC binary Bus-errors (exit 135) on Debian 13 / glibc 2.41.**
    `node -e "require('@next/swc-linux-x64-gnu')"` crashes with a bare Bus error and no
    stack. Fix: pin **Next 15** (`npm i next@15.5.4 eslint-config-next@15.5.4 --save-exact`)
    — its SWC binary loads fine. Not an OOM (raising `--max-old-space-size` doesn't help);
    it's a native glibc/CPU incompat. Next 15 also uses the FlatCompat eslint config
    (`@eslint/eslintrc` + `FlatCompat().extends("next/core-web-vitals")`), not Next 16's flat style.
17. **Cloudflare fronts `api.cortexcloud.org` and caches 404s for ~4h.** If you request
    an asset before deploying it, the edge caches the 404 (`cf-cache-status: HIT`,
    `age: NNN`, `cache-control: max-age=14400`) and keeps serving it even after the origin
    (uvicorn :8000) returns 200. Diagnose: `curl -sI <public-url>` shows `cf-cache-status: HIT`
    while `pct exec 105 -- curl -sI http://127.0.0.1:8000/<path>` shows 200. Fix within your
    control: deploy the asset under a **fresh, never-cached filename** (e.g. `logo-v2.png`)
    — or purge the CF cache if you have dashboard access. Always verify the ORIGIN
    (127.0.0.1:8000 inside CT) before assuming your code is broken.
18. **CDP facilitator `/validate`+`/settle` body is TOP-LEVEL** `{resource, method,
    headers, body}` — a `{payload:{...}}` wrapper → `400 property "resource" is missing`.
    `body` must be a STRING (bytes → `Object of type bytes is not JSON serializable`).
    This path had never been exercised — the gateway couldn't settle a payment since day one.
19. **CDP `/validate` runs a discovery preflight that REQUIRES `extensions.bazaar.info`**
    (`input.type/method/bodyType/body` + `output.type/format/example`) — missing →
    `simulation.rejectionReason: "invalid discovery configuration"`. Also: validate
    response field is `valid`, not `success`. Full contract + checklist →
    `references/cdp-facilitator-settlement.md`.
20. **"Client never pays" (returns raw 402) usually means the SERVER rejected the
    payment headers** — check the gateway log for facilitator errors, not the client.
    Server gate must accept the single `payment-signature` header alone (requiring a
    legacy `x-payment` too blocks every modern client).
21. **Stateless/local verification belongs in-process, not only via the facilitator.**
    A hardened gateway verifies the proof itself (ECDSA signer recovery via `eth_account`,
    amount >= your price, chainId==8453, asset==USDC, payTo==your wallet, validBefore
    30s/5min window) BEFORE calling the facilitator — and fail-closes to 402 on any
    error. CDP `/settle` then remains the actual on-chain broadcast rail. Two traps
    that silently reject EVERY real client payment (found + fixed + on-chain verified
    when adding a second wallet):
    - **primaryType name**: the @x402 client (2.20) signs `TransferWithAuthorization` —
      your `_AUTH_TYPES` key MUST be that exact name. `TransferAuthorization` (off by
      one word) passes unit tests but makes ECDSA recovery fail for real signatures →
      `signature does not match payer`.
    - **signature location**: the client stores the 65-byte sig at `payload.signature`,
      a SIBLING of `payload.authorization` — `_extract_sig(auth)` finds nothing →
      `missing signature`. Merge the sibling into auth before extracting.
    Full corrected struct + merge snippet + the missing-sig/mismatch debug ladder →
    `references/in-process-proof-verification.md`.
22. **Middleware ordering in FastAPI is "add LAST, run FIRST" (outermost).** The last
    `add_middleware()` is the outermost wrapper. Put a body-reading validation
    middleware last so it short-circuits malformed/oversized bodies before the payment
    gateway touches them. Read the body with `await request.body()` — starlette buffers
    it so downstream middleware (x402) can re-read safely; do NOT re-inject
    `request._receive` (breaks SSE on starlette 1.3.x). For path matching use a suffix
    (`"/data/" in path`) not `path.startswith("/data/")` — paid data routes live under
    `/x402/v1/data/...`.
    **PROVEN consequence of skipping it (2026-08 paid sweep):** the x402 gate settles
    BEFORE the input-validation middleware runs — a malformed-body request (422) still
    charged the buyer $0.05 (ledger showed 2 classical payments for 1 successful run).
    Without the outermost body-validation short-circuit, every rejected paid request
    bills the customer. When debugging "charged but failed", diff `x402_payments` rows
    against successful jobs.
26. **Quantum/braket backend auto-routing must respect device CAPABILITY, not just
    ONLINE status.** Discovery (`braket.py`) accepts any `deviceType==QPU` + ONLINE —
    but QuEra **Aquila is AHS-only** (analog program IR), so a routed job charges
    $0.85 then fails `CreateQuantumTask ... Device arn:aws:braket:.../quera/Aquila
    supports ['braket.ir.ahs.program']`. Fix: filter discovered devices on
    `deviceCapabilities` containing the gate-model IR (`braket.ir.jaqcd`) and/or
    restrict auto-route to benchmark-verified devices (Cepheus us-west-1) until a
    device earns a benchmark row. Test paid quantum via the one-off flag only:
    add `QUANTUM_LIVE_EXECUTION=true` to the unit's Environment, restart, run, then
    REMOVE the flag and restart (margin guard + `QUANTUM_MAX_COST_USD` stay intact).
27. **Paid-sweep harness for the current `/v1/optimize` surface (2026-08):** python
    x402 SDK client flow — `x402Client()` + `register("eip155:8453",
    ExactEvmScheme(signer=buyer))` + `prefer_network` policy + `x402HTTPClient`:
    POST `/v1/optimize` → expect 402 → `get_payment_required_response` →
    `create_payment_payload` → `encode_payment_signature_header` → resend → poll
    `/v1/jobs/{id}`. Request body is `{"mode": <classical|hybrid|quantum>,
    "problem": {"problem_type":"qubo","n":4,"data":{...}}}` (a `problem` WRAPPER is
    required — flat body 422s). Buyer key: `/root/bzcheck/test_buyer.key` on CT105
    (derive address via `eth_account`); SDK lives in `/opt/cortexcloud-venv`.
    Imports: `from x402 import x402Client, prefer_network` +
    `from x402.http.x402_http_client import x402HTTPClient` +
    `from x402.mechanisms.evm.exact import ExactEvmScheme` (NOT `from x402 import
    ExactEvmScheme` — ImportError).
23. **`pct push` reads SOURCE files from the Proxmox HOST, not your workstation.**
    `scp <file> root@ninyi:/tmp/ && pct push 105 /tmp/<file> <ct-path>` — the file must
    be on ninyi first, or you get `failed to open <path> for reading`.
24. **A default `x402Client` pays `accepts[0]` — if the server advertises multiple payout
    wallets, a test/sweep harness silently settles to the FIRST one.** The constructor's
    default paymentRequirementsSelector returns index 0. When the challenge lists
    `[primary, second]`, `(_v, a) => a[0]` pays the primary (proven: a $0.19 sweep landed
    on the primary while the "new" wallet only ever got the one targeted call). To force a
    specific recipient, pin the selector to its `payTo`:
    ```ts
    const TARGET = "0x5a03...";                            // lowercased
    const client = new x402Client((_v, accepts) => {
      const hit = accepts.find((a) => a.payTo?.toLowerCase() === TARGET);
      if (!hit) throw new Error("target wallet not in challenge accepts");
      return hit;
    });
    ```
    Verify the recipient BEFORE running a sweep by reading both wallets' `balanceOf`
    (eth_call to USDC on `https://mainnet.base.org`, curl with a UA — bot-filtered
    otherwise). Don't assume which wallet got the money.
25. **Testing ALL paid endpoints (a real-money sweep):** the same signer loop over every
    path in the live spec that declares a `responses.402` proves the payment gate is open
    on each plus surfaces upstream/provider/param faults. `$0.005`×38 ≈ `$0.19` from a
    ~$7 buyer — cheap. Classify each result:
    - **200** → gate + upstream OK.
    - **non-402** → the gate accepted payment (that's the pass condition); a 400/401/5xx
      is a separate upstream/param problem, NOT a payment failure.
    - **402** with a reject reason → payment genuinely failed; see the missing-sig/mismatch
      ladder (pitfall 21 → `references/in-process-proof-verification.md`).
    Common causes of "failures" that are NOT bugs: wrong model id (`/models` lists the real
    ids; the Bazaar examples in SDK code can be stale — e.g. `text-embedding-3-small` vs the
    registered `gemini-text-embedding-004`); an address-shaped param (`pair`/`token`) that
    passes a *symbol*; and missing AUDIO base64. Also note the `/data/*` address guard is
    ERC-55 (checksum hex) only — a Solana base58 address → `invalid address param`. That's a
    real gateway bug (solana/balance unreachable); fix is a base58 branch in the shared
    `_valid_addr`, not per-caller.

## Principles (CortexCloud)
- Keep control: own CT 105 app server, own merchant wallet, CDP facilitator allowed (user
  chose it). No external managed key services.
- Confirm before the first real on-chain payment (done via funded test buyer), before public
  internet exposure, wallet/payment-recipient change, or DB migration.

## References
- `references/adding-resources.md` — add new x402-gated routes; `_need` key guard; keyless upstream quirks.
- `references/x402scan-resource-extraction.md` — scrape a competitor's full resource catalog past lazy render.
- `references/cdp-facilitator-auth.md` — exact CDP import, signed-auth snippet, /supported probe.
- `references/cdp-facilitator-settlement.md` — verified validate/settle contract: request shape,
  bazaar.info preflight checklist, `valid` field, body re-injection, JWT-via-SDK rule, buyer-wallet key.
- `references/eip712-usdc-domain.md` — the exact error and the one-line fix.
- `references/mcp-server-x402.md` — full MCP package scaffolding + smoke test.
- `references/proxmox-ct105-deploy.md` — pct mount/edit/restart/copy-script recipes.
- `references/npm-org-publish.md` — granular-token scoping 404/403 diagnostic + resolutions.
- `references/blockrun-volume-model.md` — how BlockRun pulls 15.45M tx via agent-native
  auto-paying clients (ClawRouter/Franklin/MCP); our parity status + proxy approach.
- `references/in-process-proof-verification.md` — in-process EIP-712 proof verification
  (amount/chain/asset/payTo/expiry guards + `eth_account` signer recovery) + test pitfalls.
