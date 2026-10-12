# x402-ai-gateway

# x402 AI Inference Gateway

Class of work: an **OpenAI-compatible AI gateway** that charges **per call in
USDC on Base** via the [x402 protocol](https://x402.gitbook.io/x402) instead of
API keys / subscriptions. Agents discover it via **Bazaar** (MCP-native
discovery) and **x402scan**, pay with an `X-PAYMENT` header, and get a response.
Target user: CortexCloud (Jonathan / @jonahthan433, repo
`github.com/jonahthan433/CortexCloudAPI`, container CT 105 on Proxmox `ninyi`).

## Mental model

```
agent --POST /chat/completions, no API key--> gateway
  gateway: no X-PAYMENT? --> 402 + PaymentRequirements JSON (accepts[])
  agent: pays USDC on Base, sends X-PAYMENT (base64 PaymentPayload)
  gateway: verifies+settles via CDP facilitator --> routes to Gemini/Groq/OpenRouter/NVIDIA
  gateway: StreamingResponse / JSON, X-PAYMENT-RESPONSE header
```

No auth beyond payment. **Current receiving (merchant) address:
`0x311760952bd4f26b08f9d868f07bc52a16c63d62`** (changed 2026-08-02 per user;
old `0xE816eA741a0084748DC9f2BEeB86Be6705862365` appears in older notes). All
wallet/payment code is HIGH-RISK; explain changes before applying; never
silently alter the recipient.

## ⚠️ CHANGING THE RECEIVING ADDRESS (payTo) — the systemd-Environment trap
`WALLET_ADDRESS` lives in **TWO places**: `/opt/CortexCloudAPI/.env` AND the
systemd unit `/etc/systemd/system/cortexcloud.service` — the unit has the var
in `Environment=` **twice** (lines 8–9, duplicate). pydantic-settings gives
process env vars precedence over `env_file`, so editing only `.env` +
`systemctl restart` changes NOTHING (the 402 challenge keeps serving the old
payTo). Correct sequence:
1. `sed -i "s|WALLET_ADDRESS=<old>|WALLET_ADDRESS=<new>|g"` the unit file
   (fixes BOTH duplicate lines), `sed` the `.env` too (keep them in sync).
2. `systemctl daemon-reload && systemctl restart cortexcloud.service`.
3. Verify the live challenge: `curl -X POST localhost:8000/x402/v1/chat/completions -H content-type:application/json -d '{}' | grep -oE '"payTo":"[^"]*"'`.
4. Confirm funds land: USDC `balanceOf(<new>)` via `eth_call` on
   `https://mainnet.base.org` (`0x70a08231` + addr, 32-byte left-padded),
   hex / 1e6 = USDC. Proven: first payment to the new address = exactly 5000
   atomic (`0x1388` = $0.005) with `POST /settle → 200` in journalctl.
Diagnosis shortcut when payTo looks stale despite a restart: check
`cat /proc/<uvicorn-pid>/environ | tr '\0' '\n' | grep WALLET` — a baked env
var there proves the unit (not `.env`) is the source.

### ⚠️ Deriving the REAL payTo from a CDP_WALLET_SECRET (PKCS#8 EC key)
A `CDP_WALLET_SECRET` in `.env` is a base64 **PKCS#8 SEC1** key, and if the
configured `WALLET_ADDRESS` was hand-copied from the CDP dashboard it may NOT
match the key's actual address — leaving every signed challenge advertising the
wrong recipient. Derive the truth from the key and compare before deploying:
```python
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives import serialization
from Crypto.Hash import keccak   # pycryptodome — NOT hashlib.sha3_256!
pem = "-----BEGIN PRIVATE KEY-----\n"+secret+"\n-----END PRIVATE KEY-----"
key = load_pem_private_key(pem.encode(), None)
pub = key.public_key().public_bytes(serialization.Encoding.X962,
                                    serialization.PublicFormat.UncompressedPoint)
k = keccak.new(digest_bits=256); k.update(pub[1:])   # skip 0x04 prefix byte
derived = "0x" + k.hexdigest()[-40:]                  # == CDP dashboard address
```
**CRITICAL:** `hashlib.sha3_256` is the NIST variant and yields a DIFFERENT
address (I 100% got a wrong address for a second before catching it). Ethereum
uses keccak-256 = pycryptodome `Crypto.Hash.keccak` / `eth_keys`. Then keep the
derived address in sync in BOTH `.env` AND the systemd unit (above), restart,
and re-probe the live payTo. `cryptography` may not exist on the host — run the
derivation inside the CT with `/opt/cortexcloud-venv/bin/python`.

## Live protocol facts (verified against x402-foundation/x402 `main`, 2026-07)

- **x402 has V1 and V2.** V1 `PaymentRequirements`: `scheme, network,
  maxAmountRequired, mimeType, payTo, asset, extra` (+ optional `resource`,
  `recipient`). V2 uses `amount` (not `maxAmountRequired`), no `mimeType`/
  `recipient`, and a stricter `network`.
- **The 402 response MUST carry a valid `x402Version` + a Bazaar input schema.** For
  x402scan's `@agentcash/discovery` validator (`validatePaymentRequiredDetailed`), emit
  **`x402Version: 2`** with `accepts[]` carrying `scheme, network` (CAIP-2 `eip155:8453`),
  `asset`, `amount` (**STRING atomic USDC**, v2 uses `amount` not `maxAmountRequired`),
  `payTo`, and `maxTimeoutSeconds`, plus `extensions.bazaar.schema.properties.input.properties.body`
  (input schema is MANDATORY — missing it is a hard error). v1 bodies are accepted by the
  parser but warn "v1 not supported by agentcash". **This v2+input-schema requirement is
  the #1 cause of "No valid x402 response found (HTTP 402)"** even when the endpoint
  already returns 402. Full shape + headless validator: `mlops/x402-pay-per-call-gateway`
  → `references/x402scan-v2-validator.md`.
- **`network` format:** V1 accepts any non-empty string, but the canonical /
  expected value is CAIP-2: **`eip155:8453`** (Base mainnet) or `eip155:84532`
  (Base Sepolia). Do NOT send bare `"base"` — it parses but breaks discovery
  validators. Set `X402_NETWORK=eip155:8453` for mainnet.
- **USDC on Base contract:** `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
  (correct; this is what `asset` must be).
- **`maxAmountRequired`** is a STRING of USDC atomic units (6 decimals):
  `"$0.005"` → `"5000"`. Don't send a float.
## CDP facilitator contract (VERIFIED 2026-08-02 — first real settlement landed)

- **Client v2 header:** ONE `PAYMENT-SIGNATURE` header (base64 of `{x402Version, payload:{authorization,signature}, extensions, resource, accepted}`). Legacy `x-payment` split is DEAD — gate only on `payment-signature`.
- **/validate envelope:** TOP-LEVEL `{resource, method, headers, body}`. A `{payload:{…}}` wrapper → 400 `property "resource" is missing`. Response: `{valid: bool, preflight: [...], simulation, statusCode}` — check `valid`, NOT `success`.
- **/settle envelope:** `{x402Version, paymentPayload, paymentRequirements}`. `paymentPayload` = the DECODED PAYMENT-SIGNATURE HEADER ITSELF (v2 requires `accepted`). `paymentRequirements` = `{x402Version:2, **accepted-flattened, resource, extensions}` (v2 requires `scheme` at top).
- **Auth:** per-request ES256 JWT via `cdp.x402.x402.create_cdp_auth_headers(api_key_id, api_key_secret)()` → `{verify, settle, supported, list}` headers. cdp-sdk 1.47.1 in CT venv. Hand-rolled JWT = 401; secret is base64(32B key || 32B pub). NEVER static bearer.
- **Bazaar preflight:** CDP validate REQUIRES `extensions.bazaar.info = {input:{type:"http", method, bodyType:"json", body:{…}}, output:{type, format, example}}` in the 402 challenge — missing it → `simulation.outcome: "rejected", rejectionReason: "invalid discovery configuration"`. All 12 other preflight checks (returns_402, x402_version, accepts[], amount ≥ $0.001, payTo, …) must stay green.
- **Body re-injection:** middleware reads `await request.body()` for the facilitator envelope → must re-inject via `request._receive` before `call_next`, else downstream routes hang/empty.
- **On success:** set `X-PAYMENT-RESPONSE` = base64(`{success:true, details:settle_data}`) — the 2.20 client's `processPaymentResult` reads it.
- **E2E proof:** proxy (2.20 @x402, wallet 0xCed8…374A) → 402 → sign → validate → settle → 200 llama "PAID", balance −$0.001928. Restart service after deploys (systemd, CT105).

## High-volume performance (S1 landed 2026-08-02 — proof cache + nonce dedup)

For throughput, do NOT call CDP validate+settle on every request. Verified design
(live in `app/core/cache.py` + `app/middleware/x402.py`):- **Proof cache:** after a successful settle, store `sha256(payment_signature)` →
  `x402:proof:<hash>` TTL **60s**. A request whose proof hash is cached skips CDP
  entirely (still sets `X-PAYMENT-RESPONSE`). Verified: settle 3.2s → cache-hit
  0.7s, zero CDP calls in journal.
- **Nonce dedup:** EIP-3009 `authorization.nonce` claimed via Redis `SETNX`
  (`x402:nonce:<nonce>`, TTL = `validBefore − now`, clamp [1, 3600]). Reject
  402 `"Payment nonce already used"` BEFORE CDP. Same-proof replays ride the
  cache (by design — that's what the cache is for); a DIFFERENT proof reusing a
  nonce hits the gate. Since proof TTL == nonce TTL, the nonce gate only fires
  on proof-hash-differing replays within the window — after both expire the
  payment re-verifies (correct). Defense in depth: even if our gate lapses, CDP
  itself rejects replayed nonces ("authorization nonce already submitted").
- **FAIL-OPEN is mandatory:** every cache/Redis call catches exceptions and
  falls through to the real CDP path. A Redis outage must never block a payment.
- Decode `payment_signature` ONCE at the top of the payment branch (it feeds the
  cache check, nonce gate, payer address, and the settle envelope).
- Full design + test transcript: `references/payment-caching.md`. Replay-test
  harness (captures a real signed header for cache/nonce testing):
  `scripts/sign_replay.mjs` — run with `EVM_PRIVATE_KEY=0x… node sign_replay.mjs
  > sig.txt`, then replay via curl with `-H "payment-signature: $(cat sig.txt)"`.
  Gotcha: `x402HTTPClient().encodePaymentSignatureHeader(payload)` returns the
  header key UPPERCASE `PAYMENT-SIGNATURE`, and `createPaymentPayload(challenge)`
  is the 2.20 method that mints the payload (both confirmed against the esm dist).

## S2 — connection pooling (landed 2026-08-02)

`app/core/http.py` — `shared_client(name, timeout, headers)` returns ONE
persistent `httpx.AsyncClient` per (name, timeout, headers) key with
`Limits(max_connections=50, max_keepalive_connections=20)`; never open a client
per request. Wired into:
- `x402/onchain_routes.py::_rpc_call` → `shared_client("base_rpc", 10.0)`; RPC
  failure responses are **503** `upstream_base_rpc` (were 502 — the 10s hard
  timeout means httpx raises instead of hanging, so the error path is the 503).
- `middleware/x402.py` CDP validate/settle → `shared_client("cdp", 30.0)` — 2
  calls per fresh payment, the biggest per-request win.
Verified: paid chat 200 via pooled client; paid `balance` GET 200 with real
data; unpaid 402 intact. Test harness: sign per-resource via `sign_replay.mjs`
with `RESOURCE=... BODY='{}'` env vars (GET routes take the signature as a
`payment-signature` HEADER — POSTing a GET-only route → 405 Method Not Allowed).

## ⚠️ Deploy pitfalls (each cost a failed deploy or a full-route outage)

- **Unprivileged LXC: `pct push` cannot overwrite `nobody`-owned files.** CT
  root maps to host uid 100000 and has no DAC-override over host-mapped files,
  so overwriting a `nobody:nogroup` file fails with `Permission denied` while
  the rest of the push chain silently stops. Fix: `pct exec 105 -- rm -f
  <path>` FIRST, then push. Check ownership with `ls -la` if a push fails.
- **Verify imports BEFORE restarting the service.** `main.py` wraps the x402
  router includes in a try/except (`Failed to initialize x402 payment gateway:
  <err>`), so an import-time error in a middleware/route module (e.g. a
  module-level `await` from a mangled dedent) silently drops the ENTIRE
  `/x402/v1` router set → everything 404s while `/openapi.json` still lists the
  paths (it's the static `app.openapi = lambda: _OPENAPI_SPEC` override, NOT
  routing truth). Pre-flight: `cd /opt/CortexCloudAPI &&
  /opt/cortexcloud-venv/bin/python -c "import app.main"` (and `ast.parse` the
  file you edited) before `systemctl restart`. Post-restart check: journal
  `--since` grep for `Failed to initialize x402 payment gateway`.

## CDP n AUTH IS PER-REQUEST ES256 JWT, NOT A STATIC BEARER (verified
  this session — the #1 settlement blocker).** The v2 facilitator REQUIRES signed
  `Authorization` JWTs built per request via `cdp.x402.x402.create_cdp_auth_headers(
  api_key_id, api_key_secret)`. A static `Bearer <X402_FACILITATOR_API_KEY>` returns
  **401 Unauthorized** on mainnet — real USDC CANNOT settle with a static token.
  Correct wiring (cdp-sdk 1.47.1 installed on CT):
  ```python
  from cdp.x402.x402 import create_cdp_auth_headers   # NOTE: NOT cdp.x402 (not re-exported)
  def _cdp_headers():
      return create_cdp_auth_headers(
          settings.X402_FACILITATOR_API_KEY_ID,
          settings.X402_FACILITATOR_API_KEY_SECRET,
      )()   # -> {"verify":{...},"settle":{...},"supported":{...},"list":{...}}
  # merge per-endpoint: headers={**facilitator_headers, **_cdp_headers().get("verify", {})}
  ```
  The signed headers carry `Authorization` (ES256 JWT) + `Correlation-Context`.
  Config needs TWO env vars, not one: `X402_FACILITATOR_API_KEY_ID` + `_SECRET`
  (add both to `app/core/config.py`). `cdp-sdk` also upgrades `web3` to 7.x — see
  web3-7.x note below. Verify the key works BEFORE burning buyer USDC: a quick
  `create_cdp_auth_headers(id,secret)()` + `GET {FACILITATOR}/supported` must return
  **200** (proves JWT signing + key valid). Then `/verify`+`/settle` work.

- **⚠️ INGESTING THE CDP KEY THE USER PASTES — validate, never guess.** Pasted
  secrets get mangled by chat clients: a UUID can arrive **truncated** (e.g.
  `...c418` missing its final hex char) and Cyrillic look-alikes can replace ASCII
  (`В`→B, `С`→C, `Е`→E, `Т`→T, `Н`→H, `х`→x) so base64 becomes invalid and a static
  bearer fails with 401. **Never clean or guess around the corruption.** Detect
  non-ASCII in an allegedly-ASCII secret (`any(ord(c) > 127 for c in secret)`) and
  verify the key ID is a full 36-char UUID; if either fails, RE-REQUEST a clean copy
  — ideally via screenshot (the CDP "Save your API key" screen shows the exact
  string) or by having the user write it to a file on the CT. Confirmed this session:
  the first paste had 6 Cyrillic chars + a truncated ID; the screenshot re-send was
  clean and worked first try.
- **⚠️ 402 `accepts[]` MUST carry the EIP-712 domain `extra` (verified this session).**
  CDP's facilitator verifies the EIP-3009 signature against the USDC EIP-712 domain.
  If `accepts[0]` omits it, settlement returns
  `missing EIP-712 domain name/version in requirements.extra` and the payment fails
  (buyer USDC untouched). The challenge MUST include:
  ```python
  "accepts": [{
      "scheme": "exact",
      "network": settings.X402_NETWORK,            # eip155:8453
      "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      "amount": str(price_atomic),                 # STRING atomic USDC
      "payTo": settings.WALLET_ADDRESS,
      "maxTimeoutSeconds": 60,
      "extra": {"name": "USD Coin", "version": "2"},   # REQUIRED for EIP-3009 verify
  }]
  ```
  This `extra` field is separate from `extensions.bazaar.schema` and from any
  `resource` object — all three coexist in the v2 body.
- **⚠️ PAYMENT HEADER NAME (real interop bug, caught by the e2e test).** The x402
  **SDK v2 client sends `PAYMENT-SIGNATURE`**, NOT `X-PAYMENT`. A middleware that
  only reads `X-PAYMENT` rejects EVERY real SDK client ("Invalid X-PAYMENT
  header"). **Accept BOTH**: `request.headers.get("X-PAYMENT") or
  request.headers.get("PAYMENT-SIGNATURE")`. Also forward the challenge
  `x402Version` (v2) into the facilitator validation body — hardcoding
  `x402Version: 1` makes v2 payloads fail verification. Both fixes applied this
  session; the e2e test is what exposed them, proving `discover`-clean audits
  alone are NOT sufficient.
- **Repo moved:** `coinbase/x402` is now a fork; canonical is
  **`x402-foundation/x402`**. Fetch the spec from there, not training memory.

## In-process proof verification (S1 hardening — do NOT trust CDP /validate to be your only check)

Default posture is to hand the whole `payment-signature` to the CDP facilitator
`/verify`+`/settle`. **That in itself is a paid-acceptance bug**: a gateway that
only validates via the facilitator accepts any proof the facilitator returns
`valid` — if that external check is wrong you get free service or stolen funds. Harden
with a **local (in-gateway) EIP-712 `TransferAuthorization` verifier** run in the
payment middleware BEFORE the CDP call. Checks exactly (fail-closed → any failure
is a hard 402, never `call_next`):

- **ECDSA signer recovery in-process** — `eth_account` recovers the signer from
  and requires it equals `authorization.from`. Never
  outsource signature verification to a third-party validator.
- **amount ≥ required** — compare caller's paid atomic vs OUR price table (the
  same `usd_to_usdc_atomic(price_str)` the 402 challenge carried), NEVER the
  caller's stated price.
- **chainId == 8453** (parse `eip155:8453` or bare int), **asset == USDC on Base
  `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`**, **payTo == `WALLET_ADDRESS`**
  (all before any RPC), and **expiry**: reject `validBefore` < now−30s (clock
  skew) or > now+300s (proof stockpiling).
- **Constant-time hex compare** (`hmac.compare_digest`, lowercased) for
  asset/payTo/hash checks — never `==` on attacker-controlled hex.
- Keep nonce replay via Redis `SETNX` (existing) and the 60s proof cache — the
  local verifier is additive; the CDP call still does the real on-chain settle.

The full documented fail-closed module (`app/middleware/payverify.py`) with the
EIP-712 `_AUTH_TYPES` domain/message and `_extract_sig` (65-byte r||s||v hex +
v/r/s split) is in **`references/payment-verify-inprocess.md`** — copy it.

**Self-check (mandatory before relying on it):** sign a real EIP-712
TransferAuthorization with `eth_account`, prove recover round-trips, and assert
every negative branch (`tests/payverify.py` style — the session's module
`9/9`). Reuse the deployed module's own `_AUTH_TYPES` so the test and production
sign the byte-identical struct. Two gotchas seen in practice: `HexBytes.hex()`
already has NO `0x` (don't `[2:]` it — corrupts bytes); a test payTo must be a
**valid-checksum** address or ABI encoding throws (`EncodingTypeError`), so use
`Account.create().address` for the mismatch case, not a hand-typed string.
Net: the declarative fields compare against the server's own 402 challenge
(guaranteed-correct); the EIP-712 struct is the ONE thing that must match the
real client — no signed proof in logs means the first live payment is the real
test, and failure is fail-closed (safe 402, not fraud).

## Bazaar discovery (the priority discovery surface)

Bazaar is how autonomous agents find your routes — it has **native MCP support**
agent frameworks consume directly. Higher priority than x402scan.

- The **Python x402 SDK has full Bazaar parity** — `x402[extensions]` ships
  `x402.extensions.bazaar` with `declare_discovery_extension`,
  `declare_mcp_discovery_extension`, `resource_service`, `facilitator`. NOT
  TS-only. Confirmed installed at v2.14.0.
- Per route, declare: `input_schema` (JSON Schema of body fields like
  `model`/`messages`), `output=OutputConfig(example=<response shape>)`,
  `description`, and `body_type="json"` for POST routes. Returns
  `{"bazaar": {...}}` to attach as `extensions` in a `RouteConfig`.
- Expose an MCP endpoint (`declare_mcp_discovery_extension`) so agents call
  directly. Keep the existing (verified) custom payment middleware for the
  actual charging — discovery metadata is additive, doesn't touch payment flow.

### Client-side MCP server (ship one — the agent-install surface)
Bazaar is programmatic discovery; agents also need a **published MCP server** they
can `claude mcp add` / `npx`. This is the *client-side* counterpart: an MCP server
that exposes your gateway's tools and pays per call via x402 from the agent's own
wallet. Building it (TS: `@modelcontextprotocol/sdk` + `@x402/fetch` +
`@x402/evm`) is a distinct, reusable technique — full build + verification +
pitfalls in **`references/mcp-server-build.md`** (built + verified as
`@cortexcloud/mcp`, 2026-07-12). Load-bearing lessons: lazy-init the paid fetch so
the free `models` tool works without a wallet key; viem needs a `0x`-prefixed key;
import `registerExactEvmScheme` from `@x402/evm/exact/client` (not `@x402/evm`).
The install line should mirror the rival: `npx @cortexcloud/mcp`.

### Client-side OpenAI-compatible proxy (the ClawRouter equivalent — volume engine)
The MCP server reaches MCP-aware agents only. The bigger distribution channel is a
**local OpenAI-compatible proxy** any OpenAI-SDK app can use by flipping `base_url`.
BlockRun's 15.45M tx came from exactly this (ClawRouter): each `npx` install
auto-creates a self-custody buyer wallet that self-funds and pays our merchant
`0xE816…2365` per call — that's what populates the x402scan "buyers" column.
Built + verified as `@cortexcloud.org/proxy` (2026-07-15): esbuild build (NOT tsc —
`ox`/`viem` deep-types break tsc; the `paths` alias for `@x402/evm/exact/client` also
breaks esbuild — see the recipe), runs on `:8402`, forwards `/v1/*` + `/x402/v1/*`,
verified the full 402→sign→settle handshake live (failed only on unfunded wallet).
**Route gotcha:** keyless paid endpoint is `/x402/v1/chat/completions`, NOT
`/v1/chat/completions` (the latter needs a Bearer key). Full code, build fix,
verification recipe, and the real-settlement options (incl. the gotcha that the
funded test buyer `0xCed8…374A` key is NOT persisted on CT) →
**`references/proxy-build.md`**.

### Bazaar SDK call-shape GOTCHAS (verified against x402 2.14.0 — easy to get wrong)
- **`declare_discovery_extension` (POST/JSON routes)** needs BOTH an example and a
  schema, plus `body_type="json"`:
  ```python
  declare_discovery_extension(
      input={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
      input_schema={"type": "object", "properties": {"model": {"type": "string"}, "messages": {"type": "array", "items": {"type": "object"}}}, "required": ["model", "messages"]},
      body_type="json",
      output=OutputConfig(example={<response shape>}),
  )
  ```
  Omitting `body_type="json"` produces a *query* extension (wrong for POST).
- **`declare_mcp_discovery_extension` takes a CONFIG OBJECT, NOT kwargs.** This is
  the #1 footgun — calling it with `tool_name=...` as kwargs FAILS (signature is
  `(config: DeclareMcpDiscoveryConfig)`):
  ```python
  from x402.extensions.bazaar import DeclareMcpDiscoveryConfig, declare_mcp_discovery_extension
  declare_mcp_discovery_extension(
      DeclareMcpDiscoveryConfig(
          tool_name="chat_completions",
          description="...",
          input_schema={"type": "object", "properties": {...}, "required": [...]},
          transport="sse",          # Literal["sse","streamable-http"] — plain string is fine
          example={"endpoint": "https://.../x402/v1/chat/completions", "method": "POST", "payment": "x402 USDC on Base"},
      )
  )
  ```
- **MCP SSE endpoint (minimal, discovery-focused):** serve `text/event-stream`;
  handle `initialize` → result, `tools/list` → list of `{name, description,
  inputSchema}`, `tools/call` → return the x402 endpoint URL + PaymentRequirements
  (agent pays client-side; the MCP tool does NOT itself settle). `tools/call`
  yielding the endpoint + `maxAmountRequired`/`payTo`/`network` is the correct,
  honest design for a pay-per-call gateway.
- **Indexing requires a REAL on-chain USDC transaction** (even a tiny one). Per
  user rule, CONFIRM before triggering the first settlement that puts a route in
  Bazaar. **Decision record:** Jonathan chose **mainnet (`eip155:8453`) from the
  start** for CortexCloud — do NOT default to Sepolia. Sepolia only if *he* later
  asks for a dry-run. Set `X402_NETWORK=eip155:8453`.

## Adding a provider (e.g. NVIDIA NIM) — wiring checklist
NIM is OpenAI-compatible, so a provider is one file + 4 registry touches:
1. `app/providers/nvidia.py`: `class NvidiaProvider(OpenAIProvider): def __init__(self, base_url="https://integrate.api.nvidia.com/v1"): super().__init__(base_url=base_url)`
2. `app/providers/__init__.py`: import + add `"NvidiaProvider"` to `__all__`
3. `app/routing/router.py`: add `"nvidia": NvidiaProvider` to `PROVIDER_MAP`,
   add `"nvidia": settings.NVIDIA_API_KEY` to `_get_provider_api_key`'s `key_map`,
   and `from app.providers import ( NvidiaProvider,` at the import block.
4. `app/core/config.py`: add `NVIDIA_API_KEY: Optional[str] = Field(default=None, env="NVIDIA_API_KEY")`
5. `.env`: add `NVIDIA_API_KEY=` (empty — user pastes the real key; do NOT store it).
- **Models are DB rows, not config.** `ModelRegistryService.create_model(name,
  provider, provider_model_name, context_length, prompt_token_price,
  completion_token_price, capabilities, is_active=True)` creates them. There is
  **NO seed script** and **no admin user seeded**, so the admin REST route
  (`/v1/admin`) is unusable without auth. Insert model rows via a script that
  calls `ModelRegistryService.create_model` directly. Open a session with
  `app.database.session.AsyncSessionLocal` (NOT `async_session_factory` — that
  name does NOT exist in this repo; the session module exports `AsyncSessionLocal`
  and a `get_db()` generator, no `async_session_factory`). Run the script on the CT
  with `PYTHONPATH=/opt/CortexCloudAPI /opt/cortexcloud-venv/bin/python script.py`
  (the project root must be on sys.path or `app` won't import). This is a DB write
  but NOT a migration (no schema change) — proportionate; verify via
  `GET /x402/v1/models`.

## Safe-edit pattern for a LIVE payment gateway (CT 105 via Proxmox)

See `proxmox-homelab-administration` for reaching the CT. The gateway runs under
systemd (`cortexcloud.service`, `uvicorn :8000`, `Restart=always`). After code
changes: `systemctl restart cortexcloud` — NEVER a foreground uvicorn (dies on
disconnect). Verify with a read-only `curl -X POST .../x402/v1/chat/completions`
→ expect HTTP 402 with the compliant JSON (no payment fires). Patch code via
in-place Python `assert old in src; replace` (Pattern C in that skill) to avoid
rewriting the whole file.

## x402scan registration (verified against the x402scan source, 2026-07)

Registration is **not** a simple POST to a public API — read this before attempting.

- The register route is `POST /api/x402/registry/register` and is **protected by
  SIWX (Sign-In with X)**. Direct API calls get rejected without a session. The
  realistic path is the **web form** at `https://www.x402scan.com/resources/register`
  (user signs in with X, submits the origin URL). The agent can prepare everything
  server-side but cannot complete the SIWX step.
- x402scan uses **`@agentcash/discovery`** (OpenAPI-first). Its
  `fetchDiscoveryDocument(origin)` fetches **`openapi.json` at the origin root**
  and 404s if missing, then checks the submitted endpoint is listed in the spec.
  So an `openapi.json` at the origin root is **mandatory** for registration.
- **Ownership verification = `info.contact.email`** in `openapi.json`. x402scan
  reads `contact.email` to verify origin ownership and to show a merchant contact
  on Poncho pages. If it's absent you get a warning and can't verify. Set it to the
  operator's real email (e.g. `ampwerajonathan50@gmail.com`).
- The discovery lib reads the OpenAPI spec's **`x-payment-info`** per paid operation
  (NOT a legacy `x402` extension). Required per op: `x-payment-info.price` as the
  structured object `{mode:"fixed",currency:"USD",amount:"0.005"}` and
  `x-payment-info.protocols: [{"x402": {}}]` (**ARRAY OF OBJECTS** — `["x402"]` strings
  triggers `L2_PROTOCOLS_MISSING_ON_PAID`), plus `responses["402"]`. Free op →
  `security: []`. The RUNTIME 402 response must be **x402 v2 with a Bazaar input
  schema** or x402scan reports "No valid x402 response found (HTTP 402)". Exact body
  shape + headless validator: see `mlops/x402-pay-per-call-gateway` →
  `references/x402scan-v2-validator.md`.

### FastAPI `/openapi.json` SHADOWING pitfall (cost real debugging time)
Adding `@app.get("/openapi.json")` does **NOT** override FastAPI's built-in
auto-generated spec — FastAPI already registered `/openapi.json` from
`app.openapi()`, and the custom route is shadowed (the served JSON is the huge
auto-spec listing every `/v1/dashboard/*` route, NOT your curated one). Fix:
**override the function**, don't add a route:
```python
import json as _json
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "openapi.json")) as _f:
    _OPENAPI_SPEC = _json.load(_f)
app.openapi = lambda: _OPENAPI_SPEC
```
Place this right after `app = FastAPI(...)`.

### Patching `main.py` tail — double-close `}` trap
When appending to `main.py` by replacing the last real line (e.g. the
`"docs": "..."` line of the `x402_discovery` return), do NOT also leave the
original closing `}` in place — the file already ends with `    }` after that
line, so inserting another `    }` before your new code leaves a **dangling
`}`** → `SyntaxError: unmatched '}'` at startup. Either replace the whole trailing
`"docs": ...\n    }\n` block, or post-trim any stray lone `}` at EOF. Always restart
and `curl` the endpoint to confirm the curated spec (not the auto one) is served.

## Hard rules for THIS project (user-mandated)

- Ask before: DB migration, wallet/payment-recipient change, docker-compose
  Postgres creds, destructive git (force push / history rewrite), first public
  exposure.
- `POSTGRES_PASSWORD` is the weak default `postgres` — FLAG it, never change
  silently (must stay in sync with docker-compose).
- Docker `iptables:false` is deliberate (default rules broke CT DNS). Don't
  re-enable.
- Home lab: no k8s / service mesh / heavy tooling for a single-container deploy.
- When unsure about current x402/x402scan schema, FETCH the live docs — the
  protocol is new and changing.

## Build order (proven this session)

1. **Reconcile** live container vs repo (read-only): `git status`,
   `git log origin/main..HEAD`, `docker ps`, `systemctl is-active cortexcloud`.
2. **Verify x402 correctness** — 402 shape vs live spec (add `x402Version`,
   `eip155:8453`, `resource`).
3. **Bazaar discovery** — wrap routes with `declare_discovery_extension` + MCP.
4. **Providers** — add NVIDIA (NIM) + confirm Groq; OpenRouter as broad fallback.
5. **Public exposure (ask first)** — **Cloudflare Tunnel on the ninyi HOST**
   (`/etc/cloudflared/config.yml`, ingress hostname-based with optional `path:`
   matching; seen live: `/mcp*` → CT:3100 for the MCP server, default →
   `192.168.1.15:8000` for the API). Reload with `systemctl reload cloudflared`
   (or `restart`). User does the DNS CNAME to Cloudflare. NOT Caddy/nginx here —
   verify the live proxy (`ss -ltnp`, `pgrep -af cloudflared`); an earlier build
   wrongly assumed Caddy.
6. **Listings** — x402scan.com/resources/register (URL-based, auto-validates)
   + Bazaar indexing (after go-ahead on the tx).
7. **Gap analysis vs rival gateways** — benchmark against BlockRun / jarvisclaw.ai
   with `@agentcash/discovery` on their origin + reading their site via browser.
   Method + BlockRun snapshot: `references/competitor-gap-analysis.md`.

## Hardening & operations (Phase C — proven this session)

After the gateway passes discovery, harden before adding surface area:

1. **Keep `openapi.json` in sync with code.** The curated spec is a STATIC file
   served via `app.openapi = lambda: _OPENAPI_SPEC`. If you add a route in code
   (e.g. `/responses`) it will NOT appear in discovery until you also add the path
   to `openapi.json`. Re-run `npx -y @agentcash/discovery@latest discover` after
   every route change to confirm the route count and zero warnings.
2. **Per-IP rate limit on x402 routes** (protect paid endpoints from probe floods
   without throttling legit paid traffic). Add a `BaseHTTPMiddleware` placed
   BEFORE `X402PaymentMiddleware`, governing only `/x402/v1/*`, reusing the existing
   Redis `RateLimiter.is_rate_limited(key, limit=60, window=60)` (fail-open on Redis
   error). Generous limit (60/min) so a normal agent is never throttled.
   ```python
   class X402RateLimitMiddleware(BaseHTTPMiddleware):
       async def dispatch(self, request, call_next):
           if not request.url.path.startswith("/x402/v1"):
               return await call_next(request)
           ip = request.client.host if request.client else "127.0.0.1"
           try:
               limited = await RateLimiter.is_rate_limited(f"ratelimit:x402:ip:{ip}", 60, 60)
           except Exception:
               limited = False
           if limited:
               return JSONResponse(status_code=429, content={"error": "x402 gateway rate limit exceeded."})
           return await call_next(request)
   ```
3. **Free `/x402/v1/status` monitoring endpoint** (no payment). Return gateway
   health, `x402_enabled`, `network`, `merchant_wallet`, `models_available`
   (from `ModelRegistryService.get_active_models`), Redis health, and a payment
   tally from Redis keys. Free because `/status` is not in `ROUTE_PRICING` → the
   paywall `call_next`s it. Useful for liveness probes + dashboards.
4. **Verify after EACH change** with a read-only POST to a paid route (expect 402),
   the `/status` (200), and `discover` (clean). Streaming (`StreamingResponse` SSE)
   was already present for `/chat/completions` + `/responses` — confirm it still
   works post-change.

### Adding a new paid route (S3/S4/S5) — THREE touches, not one
A new paid `/x402/v1/*` endpoint is NOT just a router. To be gated correctly:
1. **Route module** in `app/x402/*.py` + `app.include_router(...)` in `main.py`
   (inside the `if settings.X402_ENABLED...` try-block, BEFORE `app.add_middleware
   (X402Middleware)` so `call_next` ordering is right).
2. **`app/x402/pricing.py` → `ROUTE_PRICING`** — key it by `"POST /x402/v1/<path>"` or
   `"GET /x402/v1/<path>"`. Any new path MISSING a pricing key is served FREE (no 402) —
   a silent revenue leak, the same class as the path-param bug below.
3. **`app/middleware/x402.py` → `QUERY_ROUTES`** — ONLY for GET routes. If a paid
   GET endpoint takes query params but is absent here, the 402 challenge emits a
   `body` input (empty) instead of `queryParams`, and the paid call can't be made.
   POST routes (JSON body) don't need this set.
New GET data routes landed this way: `data/news`, `data/eth/balance`,
`data/solana/balance`, `data/defi/yields`, `data/gas` (prices $0.02/$0.001/…).

### Section-2/3/4 full-pass hardening (2026-08-06 — input validation + AI abuse + rate limit)
When asked to harden the gateway (or any FastAPI app), the proven full-pass
shape is ONE outer input-validation middleware + key-based throttle, not a
scatter of guards per route. All of the below were live-verified.

**Middleware ordering:** FastAPI/Starlette `app.add_middleware` LAST-added =
OUTERMOST= runs FIRST. To make your validation/rate-limit middleware short-
circuit BEFORE the payment middleware (X402) or rate-limiter return 402/429,
register it AFTER them (e.g. right after `CortexHeadersMiddleware`). An inner
guard never fires because the payment gate already returned 402. Verify the
ordering by sending a body-only probe: an outer form/JSON guard gives 415 (your
middleware), not 402 (payment gate).

**One middleware file (`app/middleware/validate.py`) covering ALL of Section 2:**
- 415 unless `Content-Type: application/json` **BUT exempt
  `application/x-www-form-urlencoded` and `multipart/form-data` first** —
  a blanket 415 breaks `/auth/login` (OAuth2 sends form-encoded) → every login
  415s. Exempt BEFORE the JSON check.
- 413 body > 1MB; 400 invalid JSON / nesting depth > 10 (JSON bomb, recurse
  with a `MAX_DEPTH` guard—a `RecursionError` must become 400, not 500).
- Read the body ONCE (`await request.body()`, starlette buffers it so the
  downstream X402 re-read is safe — NO `request._receive` re-injection, which
  is what breaks SSE on this stack).
- **`JSONResponse(status_code=400, content=...)`** — NOT `JSONResponse(400,
  content=...)`. The positional int binds to `content` → `TypeError: got
  multiple values for argument 'content'` → **500 on EVERY guarded request**.
  This is the #1 "my middleware 500s everything" cause; use the keyword.
- `GET /data/*` query sanitization: match with `"/data/" in path` or endswith,
  NOT `startswith("/data/")` — the real paths are `/x402/v1/data/...` and
  `startswith` misses them (params pass straight to the payment gate = 402, and
  you'll wrongly think the guard works). **Address params (`address/addr/
  pair/token`): accept ERC-55 checksummed hex `0x[0-9a-fA-F]{40}` OR a base58
  Solana address** — a validators-only-ERC-55 guard makes `/data/solana/balance`
  permanently 400 (a Solana base58 address can never be checksum-med hex). Fix
  pattern (live 2026-08-07): `re.fullmatch(r"0x[0-9a-fA-F]{40}", a)` → ERC-55
  check; else `re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", a)` → valid base58
  (no EIP-55 equivalent exists for Solana; the on-chain RPC is the real gate).

**Section-3 capability-consistent AI payloads (in the same chat path):** gate
on the DB model row, not the user's claim. Load active models once per ~60s
into a cache via `ModelRegistryService` + `AsyncSessionLocal` (field is
`context_length`, NOT `context_window`; `get_models()` is a DEAD import — use
the async session query). Then: 400 if `tools` > 64, if the model lacks the
`tool_calling` capability but the request sends `tools`, if `max_tokens` or
`max_completion_tokens` > `context_length`, and if a token estimate (~4
chars/token) of `messages` exceeds the window. Tools/capability gating
prevents tool-schema flooding (provider prompt-slot abuse).

**Section 4 rate limit:** a transparent AI gateway's only real brute force /
DoS gap is usually a form-based login with NO limiter (every one else is
already covered: `/x402/v1` IP throttle, legacy `/v1/*` key-based limits, '§1'
nonce dedup). Fix = a per-IP throttle in the login handler itself reusing the
existing `RateLimiter.is_rate_limited(f"ratelimit:login:ip:{ip}", 10, 60)`,
fail-open on Redis error, 429 on exceed. Do NOT add a global IP limiter for
paths that already carry key-based limits.

Failure mode to expect when you deploy a guard middleware: it can break a
legit route you didn't think about (the form-login 415). Always probe BOTH the
guarded-fail case (bad input → expected 4xx) AND a happy-path pass-through
(valid → 402 payment-gate or 200) after every restart, then commit per section.

### ⚠️ Starlette `request.state` is a State, NOT a dict (500s everything)
In a `BaseHTTPMiddleware` (or anywhere reading `request.state`), do NOT call
`request.state.get("key")` — `.get` does not exist on a Starlette `State` and
that AttributeError surfaces as `500 Internal Server Error` on **every** route
the middleware wraps (including `/health`). Use
`getattr(request.state, "key", default)`. Also prefer `request.state` over
contextvars for per-request data (Starlette clears request-scoped state per
request; a contextvar in a task-group can leak across requests).
Real pattern: my S3/S6 headers middleware reads
`getattr(request.state, "request_id", None) or req headers["x-correlation-id"]`.

### ⚠️ PATH-PARAM PRICING PITFALL (silent paywall bypass — real bug, Phase B)
The x402 middleware matches pricing by **request path verbatim** against
`_PATH_PRICING` keys. A FastAPI **path param does NOT match** at runtime, so a
route like `GET /x402/v1/data/dex/pairs/{chain}` (keyed the same in
`ROUTE_PRICING`) is hit as `/data/dex/pairs/ethereum` → **lookup MISS** →
`price_str is None` → middleware `call_next`s → **served free, no 402, proxy
fires**. Silent revenue leak. **Fix: NEVER use path params on x402-gated
routes — pass all dynamic values as QUERY params** (`?chain=ethereum&pair=0x…`),
and key pricing by the constant path. Full pattern + ERC-20/`eth_call` selector
+ keyless upstream list: `references/data-marketplace.md`.

### Favicon / x402scan merchant branding — TWO distinct problems
There are two unrelated "logo" issues; fix the right one. Full detail +
Poncho page notes: **`references/x402scan-merchant-branding.md`**.

**(a) Discovery-audit `FAVICON_MISSING` warning** — `@agentcash/discovery check`
complains when `/favicon.ico` isn't a real image. Fix: serve the logo via
`FileResponse("static/favicon.jpg", media_type="image/jpeg")`. Verify with a
**cache-bust** `curl ".../favicon.ico?cb=1"` (expect `200 image/jpeg ~47KB`), not a
bare `curl` (stale Cloudflare `image/svg+xml` until TTL). Also check
`http://127.0.0.1:8000` to bypass Cloudflare.

**(b) x402scan server-page shows a letter avatar ("C"), not your logo** — this is
what the user actually notices ("the favicon still looks the same"). **Verified
mechanism (x402scan source):**
- The server card reads `origin.favicon` (`header/index.tsx`: `src={origin.favicon}`,
  fallback = `<Server>` icon). When `favicon` is null, x402scan renders a
  **generated LETTER AVATAR** from the title — that's the "C". So avatar = `favicon` field, NOT `og:image` (og:image feeds `ogImages`, separate).
- `register-origin.ts` calls `scrapeOriginData(origin)` at **registration time**,
  which fetches the origin's **HTML homepage** and parses `<link rel="icon">`
  (regex `rel=["']...icon...["']`) + `og:image`. `scrapeFavicon` (from
  `@agentcash/discovery`) probes `COMMON_FAVICON_PATHS = ["/favicon.ico",
  "/favicon.png", "/favicon.svg"]` and resolves `<link rel="icon" href>` candidates.
  For `api.` subdomains it falls back to the **base domain** (`cortexcloud.org`) if `api.` lacks data.

**⚠️ RE-REGISTRATION DOES NOT RELIABLY REFRESH A CACHED FAVICON (proven this session).**
We added the homepage + re-registered, but x402scan STILL showed "C". Proof the
scraper itself works: running `@agentcash/discovery`'s own
`scrapeFavicon('https://api.cortexcloud.org')` returns
`https://api.cortexcloud.org/favicon.ico`. So the root cause is a **stale
`favicon:null` cached in x402scan's DB from the FIRST registration** — `upsertOrigin`
does not overwrite it on re-register, and there is **NO public deregister/refresh
endpoint** (the API exposes no DELETE/remove-origin mutation; `developer.ts` only
has read-only `preview` + `batchTest`). **Correct action + expectation:**
1. DO add the HTML homepage at `/` served on both `api.` and base domain with
   `<link rel="icon" href="/favicon.ico">` + `<meta property="og:image"
   content="https://api.cortexcloud.org/favicon.ico">` (verify: `curl / | grep -iE
   "og:image|rel=.icon"`). Serve via `HTMLResponse(CORTEXCLOUD_HOME_HTML)`.
2. Re-register — NECESSARY but NOT SUFFICIENT; it may eventually refresh on a later
   re-register or periodic re-scrape.
3. If it persists, the logo is correctly deployed + scrapeable; this is a
   x402scan-side display-cache issue, NOT a gateway bug. Only x402scan support can
   force a re-scrape. **Tell the user this clearly** rather than re-churning code.
Serve via `HTMLResponse(CORTEXCLOUD_HOME_HTML)` from `main.py`.

**(c) x402scan server NAME/TITLE comes from the HTML homepage `<title>`, NOT
`openapi.json` `info.title`.** When the listing showed
"CortexCloud API -- Agent-Native AI & Data Gateway" we first checked
`info.title` (already "CortexCloud API") — but x402scan renders the card title from
the origin's served HTML `<title>` element. Fix: edit the `CORTEXCLOUD_HOME_HTML`
constant's `<title>` in `main.py` (e.g. `<title>CortexCloud API</title>`), restart,
and verify with `curl https://api.cortexcloud.org/ | grep -i '<title>'`. x402scan
re-scrapes the title on its next crawl/re-register. `info.title` and `og:title` do
NOT drive the card title.

### Home-page route pitfall (the fix for (b))
When adding the `/` HTML route, define the `CORTEXCLOUD_HOME_HTML` constant
**before** `app = FastAPI(...)` — referencing it inside `home()` without defining
it first throws `NameError` at request time (500 on `/`). Verify with
`curl https://api.cortexcloud.org/ | grep -iE "og:image|rel=.icon"` after restart.

### Poncho merchant page
Driven by `info.contact.email` in `openapi.json` (set to
`ampwerajonathan50@gmail.com`) — that verifies ownership + edits the card. The page
exists once registered + email set, but its live fields (Transactions / Volume /
unique buyers) populate ONLY after real on-chain USDC payments hit the merchant
wallet (`0xE816...2365`). No payments -> "0 / $0.00". First real payment =
`references/e2e-payment-test.md`.

### `pct mount` write-race
When editing CT 105 files via `pct mount` + base64 + python, do the
read-modify-write **and** the verification (`grep -c` / `json.load`) in the
**SAME** `pct mount … pct unmount` shell command — a verify in a separate mount
session can show a discarded overlay (an injection once printed "injected:True"
but didn't persist). See `references/data-marketplace.md`.

## Phase B — Data Marketplace (the differentiator vs jarvisclaw.ai)

Extend the gateway beyond AI inference with **x402-gated proxy endpoints backed
by KEYLESS upstreams** (no caller API key, no new secrets, pure margin). This
is what separates CortexCloud from a bare AI gateway. Proven pattern:

- **Market data** (CoinGecko + DEXScreener, keyless): `data/prices`,
  `data/coins/search`, `data/dex/search`, `data/dex/pairs` — each $0.001.
- **On-chain Base** (public RPC, keyless): `data/base/balance`,
  `data/base/token-balance` (USDC/WETH/DAI/USDT + any addr), `data/base/nonce`
  — each $0.001. Use `https://mainnet.base.org` (llamarpc was 521/down); add
  `base-rpc.publicnode.com` + `1rpc.io/base` as fallbacks.
- Each adds: a route module → `ROUTE_PRICING` key (QUERY params only!) →
  `app.include_router` in `main.py` → a path in `openapi.json` → re-run
  `discover` (route count +1, 0 warnings).
- Full route inventory + reusable ERC-20 `balanceOf` selector + exact upstream
  URLs: **`references/data-marketplace.md`**.

## S3/S6 extension surface — audit, usage, receipts, ECDSA response signing (landed 2026-08-03)

Beyond the paid routes, the gateway ships trust/auditability endpoints. Built
live on the Python gateway; reusable pattern:
- **`GET /x402/v1/pubkey`** (free) — serves the response-signing server public key
  as PEM. Backs `app/x402/trust.py`: lazy-generates an **ECDSA P-256** keypair once
  (`SECP256R1`, persisted to a PEM file, `chmod 600`), exposes `get_pubkey_pem()`,
  `sign_payload(payload)` → base64 DER signature, `verify_payload(...)`.
- **`GET /x402/v1/usage?address=0x…`** and **`GET /x402/v1/receipts?address=…`**
  (both free) — the x402 payment middleware `RPUSH`es one append-only JSON receipt
  per settled payment to `x402:rx:<sha256(payer)>` (Redis LIST, `LTRIM` to cap ~2000;
  fail-open on Redis error). `/usage` aggregates call count + total atomic spent
  (+ `.usdc` human), `/receipts` paginates. Reading the SAME key as the write is the
  lazy-correct design: no separate store.
- **Response signing middleware** (`app/middleware/headers.py`,
  `CortexHeadersMiddleware(BaseHTTPMiddleware)`) emits `X-Cortex-Request-Id`,
  `X-Cortex-Provider` (from `request.state.upstream_provider`), `X-Cortex-Latency-Ms`,
  and `X-Cortex-Signature` = `ECDSA_sha256(request_id + body)`. Add it AFTER
  `X402Middleware` (`add_middleware` order) so it wraps the paid path. Verify the
  signature end-to-end by re-signing the returned body with `get_pubkey_pem()`.

### ⚠️ ECDSA verify gotchas (cost a debugging loop)
- Cryptography must run **inside the CT** (`/opt/cortexcloud-venv/bin/python`) — the
  host interpreter has no `cryptography`, and a stray `/tmp/http*.py` can shadow
  stdlib `http`. Don't hand-verify with `urllib.request` locally.
- The signing middleware reads `request_id` via `getattr(request.state,
  "request_id", None) or headers["x-correlation-id"]`. A **free/un-routed response
  (e.g. the 402 challenge) has empty `request_id`, so the signature binds only the
  body and recomputing it needs `rid=b"" + body`. Routed 200s are signed with the
  real `request_id` — send an explicit `x-correlation-id` header on your test call
  so the verification script can reconstruct the exact payload.

## Verification (authoritative)

- **Discovery audit:** `npx -y @agentcash/discovery@latest discover
  "https://api.cortexcloud.org"` → 0 warnings, routes listed with `paid [x402]`.
- **Live 402 body validation:** run x402scan's own `validatePaymentRequiredDetailed`
  on the live 402 (exact recipe + issue table in `mlops/x402-pay-per-call-gateway`
  → `references/x402scan-v2-validator.md`). A clean body prints
  `valid: true | version: 2 | errorCount: 0`.
- **End-to-end paid-path test (MANDATORY before "production-ready"):** a safe,
  recoverable harness that generates an ephemeral buyer wallet on the CT, has the
  user fund it with a few dollars of USDC, runs the x402 client to pay + get a 200,
  then sweeps the leftover back to the merchant wallet and wipes the key. The
  `discover`-clean audit is NECESSARY but NOT SUFFICIENT — the e2e test is what
  catches real interop bugs (PAYMENT-SIGNATURE vs X-PAYMENT header, CDP
  facilitator auth). **The harness in `references/e2e-payment-test.md` was REWRITTEN
  this session with the verified-correct manual 402->pay->retry (do NOT use
  `x402_httpx_transport`; use `x402HTTPClient` + `get_payment_required_response` +
  `create_payment_payload` + `encode_payment_signature_header`).** This is also what
  populates the x402scan merchant page with a live transaction.
- x402scan's registry READ endpoints (`/api/x402/resources`, `/registry/origin`)
  are themselves x402-PAID — you cannot read your registration back without paying;
  the `discover`/`check` CLI is the verification path instead.

## References

- `references/payment-verify-inprocess.md` — S1 hardening: in-process EIP-712
  `TransferAuthorization` verification module + eth_account sign/recover self-check.
- `references/x402-schema.md` — exact verified V1/V2 field tables, facilitator
  payload shape, and the 402-response checklist.
- `references/cortexcloud-layout.md` — confirmed container/file map, env vars,
  provider wiring, and the Phase-0 fixes already applied.
- `references/x402scan-openapi.md` — x402scan registration flow (SIWX web-form,
  OpenAPI-first discovery) + a working `openapi.json` template with the `x402`
  operation extensions and `info.contact.email` ownership field.
- `references/data-marketplace.md` — Phase B data-marketplace + on-chain Base
  extension: keyless upstreams (CoinGecko/DEXScreener/public RPC), the
  PATH-PARAM pricing pitfall, ERC-20 balanceOf selector, favicon cache-bust,
  and the `pct mount` write-race gotcha.
- `references/mcp-server-build.md` — building the client-side MCP server
  (`@cortexcloud/mcp`, stdio) AND hosting it remotely over **Streamable HTTP**
  (`createServer()` factory, per-session fresh McpServer — `connect()` one-shot,
  pin the `sessionIdGenerator` UUID before `new`, the `mcp-session-id` HEADER,
  esbuild MUST be `--format=cjs` unminified or tweetnacl throws `Dynamic require
  of "crypto"`), + the cloudflared `path: /mcp*` ingress that exposes it
  publicly. Verified live 2026-08-07.
- `references/mcp-registry-submission.md` — getting the hosted server LISTED: official
  Registry API (`/v0/validate`, `/v0/auth/github-at`, `/v0/publish`) + server.json
  schema (required `$schema`, desc ≤100 chars, `Repository` = `{source,id,url}` with
  NO `type`), the `mcp-publisher login github` device-flow handoff, and the 4-registry
  map (Official → auto PulseMCP → Smithery URL → mcp.so/Glama repo). Live `valid:true`
  verified 2026-08-07.
- `references/web3-7x-quirks.md` — web3.py 7.x API gotchas (cdp-sdk pulls it in):
  no `async with`, keyword `address=`/`abi=`, ERC-20 balanceOf + ETH balance
  recipes, public Base RPC, and the gasless EIP-3009 sweep trick.
- `references/competitor-gap-analysis.md` — how to benchmark CortexCloud against a
  rival x402 gateway (BlockRun snapshot, 2026-07-12) using `@agentcash/discovery` on
  the competitor's origin + reading their site via browser.
- `references/origin-quantum-quafu.md` — Origin Quantum / Wukong integration
  (Quafu cloud, `quafu-runtime` SDK: Account token, upload_program → run →
  job.result; hybrid classical/quantum runtime programs) — basis for the
  Optimization pivot's quantum + QAOA/hybrid adapters. Verified 2026-08-08.

## CortexCloud Optimization pivot — quantum adapter groundwork (planned 2026-08-08)
The user approved an inspection+plan phase for repositioning the repo as
"optimization infrastructure for AI agents" (classical/hybrid/quantum solvers
behind one x402 API: `/v1/estimate` free, `/v1/optimize` paid, `/v1/jobs/{id}`
+ `/v1/backends` + `/v1/capabilities` free). Verified facts for that build:
- **Keep the x402 middleware machinery intact** (verify/settle/proof cache/
  payverify); the change is ROUTE_PRICING keys + INPUT_SCHEMAS, not the payment
  flow. New paid routes = the 3-touch rule above (router → pricing dict →
  openapi.json).
- **Redis removal is viable:** jobs→PG, nonce dedup→PG unique constraint
  (must survive restarts — security), proof cache + rate limiter→in-process
  (`ponytail:` single-worker ceiling). Live prod DB is lightly used (10 model
  rows, 1 user/key, 0 usage_logs) — old auth/billing tables can be left in
  place, not dropped.
- **Origin Quantum = Quafu SDK (token auth, upload_program → run → poll)** —
  full verified mechanism + adapter design: `references/origin-quantum-quafu.md`.
- Node MCP server source (`cortex_*` tools live there) = local
  `/home/hermes/cortexcloud-mcp` (src/index.ts) matching the CT bundle.

## TS gateway owning the x402 handshake (cross-stack split — decided 2026-07-16)
A TypeScript/Fastify rewrite of the gateway can own the **HTTP 402 handshake +
`/v1/settle` + audit log** while leaving **on-chain settlement** in the legacy
Python gateway. Pattern proven when building `~/cortexcloud-gateway`:
- `verifyPayment(txHash, payer, amountUsd, projectId)` is a **pluggable hook** —
  default logs + writes a `Payment` row (`status:"settled"`) but does NOT chain-
  confirm. Wire real `viem`/CDP confirmation here when this service owns settlement.
- `MERCHANT_WALLET` env default = `0xE816eA741a0084748DC9f2BEeB86Be6705862365`
  (the real CortexCloud wallet) so the 402 payload is correct even before on-chain
  verify lands.
- Mark the ceiling with `// ponytail: no on-chain settle here; verifyPayment logs +
  returns ok. Wire a real base RPC/viem check when this gateway owns settlement.`
This lets a TS MVP serve real 402s immediately without re-implementing CDP
facilitator auth (the #1 settlement blocker, above). Build the Fastify/Prisma
shell with `node-api-gateway`; keep this skill for the protocol/settlement layer.

## Live payment VERIFIED (2026-07-12)
The full loop ran end-to-end on **mainnet** and settled real USDC:
402 challenge (with `extra` EIP-712 domain) -> buyer signed EIP-3009 ->
CDP `/verify`+`/settle` (per-request JWT auth) -> request routed to Groq ->
200 + completion. Merchant wallet `0xE816...2365` balance rose on-chain
(confirmed via balanceOf on `mainnet.base.org`). The three things that were
WRONG before this worked: (1) facilitator auth was a static Bearer — must be
`cdp.x402.x402.create_cdp_auth_headers` JWTs; (2) `accepts[]` lacked `extra`
EIP-712 domain; (3) middleware imported `cdp.x402` instead of `cdp.x402.x402`.
All three fixed + verified. See `references/e2e-payment-test.md`.
- The exact v2 402 body shape + headless `validatePaymentRequiredDetailed` recipe +
  the 405 method-agnostic fix live in `mlops/x402-pay-per-call-gateway` →
  `references/x402scan-v2-validator.md` (single source of truth for the validator).
- **Upstream provider gotchas (Alchemy/CoinGecko) for paid Data routes** live in
  `references/alchemy-integration.md`: correct host (`*.g.alchemy.com` not
  `*.gateway.`), Bearer-header auth (not `?apiKey=`), and the JSON-RPC `/v2` vs
  REST `/nft/v3` split — every Data endpoint 401/400 I hit traced to one of these.
