# x402-payment-testing

# x402 live payment smoke test

## When to use
- You added/changed a paid (x402 v2) endpoint and need to prove the FULL loop: 402 challenge → signed payment → facilitator validate+settle → upstream executes → 200 → ledger records provider cost + margin.
- Unit/integration tests with a mocked facilitator are NOT enough — the user explicitly wants a real settled payment.

## Hard constraint: resource URL MUST be https
The CDP x402 facilitator (`api.cdp.coinbase.com/platform/v2/x402/validate`) rejects any `resource.url` not matching `^https://.*$`. A localhost `http://127.0.0.1:PORT` challenge fails schema validation with `doesn't match the regular expression ^https://.*$`.

Fixes, in order of preference:
1. **Expose staging over https** (real deployment behind TLS). Then the challenge `resource.url` is the public https URL and the facilitator can fetch+validate it.
2. **Ad-hoc cloudflared tunnel** for a localhost staging server (no token needed): `cloudflared tunnel --url http://127.0.0.1:PORT` → gives `https://<random>.trycloudflare.com`. Set the server's `X402_RESOURCE_BASE` (or make the middleware derive `resource.url` from the incoming request's `scheme://host`, which it should do anyway — see Pitfalls) to that URL, and point the smoke client at the public URL. Kill the tunnel afterward (it's a public exposure).
3. Do NOT weaken payment verification to bypass this. The https requirement is an external anti-fraud constraint, not a code defect.

## Signing recipe (verified with x402 2.14.0, cdp 1.47.1)
The test-buyer key is a raw 64-hex EOA private key (NOT a CDP-managed account). Sign with `eth_account` + the x402 EVM signer:

```python
import os, httpx
from eth_account import Account
from x402.client import x402Client, x402ClientSync
from x402.http.x402_http_client import x402HTTPClientSync
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

BASE = "https://<public-or-staging-https-url>"   # see https constraint above
account = Account.from_key(open("/path/to/test_buyer.key").read().strip())
client = x402ClientSync(x402Client())
client.register("eip155:8453", ExactEvmScheme(signer=EthAccountSigner(account)))
client.register_policy(__import__("x402").prefer_network("eip155:8453"))
xhttp = x402HTTPClientSync(client)

raw = httpx.post(f"{BASE}/v1/paid/route", json=payload)
assert raw.status_code == 402
headers, _payload = xhttp.handle_402_response(dict(raw.headers), raw.content)
assert headers, "failed to build signed payment headers"
r = httpx.post(f"{BASE}/v1/paid/route", json=payload, headers=headers)
assert r.status_code == 200, r.text
```

Key API notes (these are version-sensitive — re-check with `inspect` if the package version changed):
- `x402ClientSync(x402Client())` — the sync client wraps the async `x402Client`.
- `register("eip155:8453", ExactEvmScheme(signer=EthAccountSigner(account)))` — scheme + EVM signer.
- `handle_402_response(dict(headers), content)` returns `(headers_dict, payload)` — pass `dict(raw.headers)`, NOT the httpx Response object (the client expects a `requests`-style shape, not `httpx.Response`).
- The async `x402HTTPClient` (no `Sync`) is NOT callable and has no `.request` — use the sync `x402HTTPClientSync` + manual two-step POST.

### Proven async variant (used on CortexCloud staging — settled real USDC on Base)
If the sync `EthAccountSigner` recipe breaks on your x402 version, use the async flow from `ct_paid_tests.py` (it worked where the sync adapter errored):

```python
import asyncio, httpx
from eth_account import Account
from x402 import x402Client, prefer_network
from x402.http.x402_http_client import x402HTTPClient
from x402.mechanisms.evm.exact import ExactEvmScheme

buyer = Account.from_key(open("/root/bzcheck/test_buyer.key").read().strip())
client = x402Client()
client.register("eip155:8453", ExactEvmScheme(signer=buyer))
client.register_policy(prefer_network("eip155:8453"))
xhttp = x402HTTPClient(client)

async def settle(session, method, path, json_body=None, params=None):
    kw = {"headers": {"content-type": "application/json"}}
    if json_body is not None: kw["json"] = json_body
    if params is not None: kw["params"] = params
    r1 = await session.request(method, f"{BASE}{path}", **kw)
    assert r1.status_code == 402
    pr = xhttp.get_payment_required_response(lambda n: r1.headers.get(n), r1.json())
    payload = await xhttp.create_payment_payload(pr)
    pay_headers = xhttp.encode_payment_signature_header(payload)
    kw2 = {"headers": {**kw["headers"], **pay_headers}}
    if json_body is not None: kw2["json"] = json_body
    if params is not None: kw2["params"] = params
    return await session.request(method, f"{BASE}{path}", **kw2)
# asyncio.run against a route that is LIVE + reachable at BASE (https).
```
`ExactEvmScheme(signer=buyer)` with the raw `Account` works directly — no sync/signer adapter. Prefer this if you hit sync-client errors.

## Where to find the test-buyer key
Ask the user / check the deployment repo. It is a raw hex private key the facilitator can settle (USDC on Base, eip155:8453). Never log the key or the resulting signed payload.

## Verify ledger + metrics after a successful payment
- Query the payments ledger table for the settled row: confirm `provider_cost_usd`, `margin_usd`, `category` are non-null and `margin ≈ amount_usd − provider_cost_usd`.
- Hit the internal metrics endpoint (gated by `INTERNAL_TOKEN`; 503 if unset) and confirm `successful_payments`, `cortexcloud_revenue_usd`, `cortexcloud_margin_usd`, and `revenue_by_mode` reflect the new row.

## Manual x402 EIP-712 signing (without the x402 Python SDK)
When the x402 SDK is unavailable or the sync adapter breaks, sign manually with `eth_account`:

```python
import base64, json, os, time
from eth_account import Account
from eth_account.messages import encode_typed_data

buyer = Account.from_key(open("/root/bzcheck/test_buyer.key").read().strip())

# 1. POST unsigned to get 402
# 2. Decode challenge from `payment-required` header (x402 exact) or `www-authenticate` request= field (MPP)
pr_b64 = resp.headers["payment-required"].rstrip("=")
pr_b64 += "=" * ((4 - len(pr_b64) % 4) % 4)  # fix padding
challenge = json.loads(base64.b64decode(pr_b64))
accepts0 = challenge["accepts"][0]

# 3. Build EIP-712 typed data from accepts0
domain = {
    "name": accepts0["extra"]["name"], "version": accepts0["extra"]["version"],
    "chainId": 8453, "verifyingContract": accepts0["asset"],
}
message = {
    "from": buyer.address, "to": accepts0["payTo"],
    "value": str(accepts0["amount"]), "validAfter": "0",
    "validBefore": str(int(time.time()) + 120),
    "nonce": "0x" + os.urandom(32).hex(),
}
typed_data = {
    "types": {
        "EIP712Domain": [{"name":"name","type":"string"},{"name":"version","type":"string"},
                          {"name":"chainId","type":"uint256"},{"name":"verifyingContract","type":"address"}],
        "TransferWithAuthorization": [{"name":"from","type":"address"},{"name":"to","type":"address"},
                          {"name":"value","type":"uint256"},{"name":"validAfter","type":"uint256"},
                          {"name":"validBefore","type":"uint256"},{"name":"nonce","type":"bytes32"}],
    },
    "primaryType": "TransferWithAuthorization", "domain": domain, "message": message,
}

# 4. Sign and build payment-signature payload
signed = buyer.sign_message(encode_typed_data(full_message=typed_data))
sig_hex = signed.signature.hex()
payload = {"x402Version": 2, "accepted": accepts0,
           "payload": {"authorization": message, "signature": sig_hex}}
payment_sig = base64.b64encode(json.dumps(payload).encode()).decode()

# 5. Retry with payment-signature header
```

**Pitfall: base64 padding.** The `payment-required` / `request=` base64 values often lack trailing `=` padding — `binascii.Error: Incorrect padding` is fixed by stripping then re-padding to a multiple of 4, NOT by appending a fixed number of `=`.

**Pitfall: challenge header differs by scheme.** x402 exact puts the challenge in `payment-required`; MPP/Tempo puts it in `www-authenticate request=`. Parse the right header for the active scheme.

## Pitfalls
- **Cloudflare TLS termination makes the challenge `resource.url` http, not https.** When prod sits behind Cloudflare (or any TLS-terminating reverse proxy), the incoming `request.url.scheme` is `http` (the proxy forwards plain HTTP to the backend), so the middleware emits `resource.url: http://api.cortexcloud.org/...`. The CDP facilitator requires `^https://.*$` → every paid call fails validation with `doesn't match the regular expression ^https://.*$`. **Fix in the middleware** (root cause, not a test hack): derive the scheme from `X-Forwarded-Proto` when present: `scheme = request.headers.get("x-forwarded-proto", request.url.scheme)`. This makes ALL paid endpoints (Quantum included) correctly publish an https challenge. Do NOT work around it by rewriting the challenge URL inside the smoke client — that masks the real bug and real agents would hit it too. (If you must smoke against prod before the fix lands, the workaround is to patch the challenge's `resource.url` to https in both the `payment-required` header AND the response body before calling `handle_402_response`, since the x402 client reads the header in V2 — but fix the middleware for real.)
- **Don't read cost/margin from `request.state` set by the route** — the middleware records the payment BEFORE calling the route, so `request.state` is empty at record time. Compute provider cost in the middleware from the request body, then record it.
- **SQLAlchemy `func.interval("24 hours")` is broken** — it emits `interval($3::VARCHAR)`, which Postgres rejects (`syntax error at or near "$3"`). Use `text("interval '24 hours'")` instead. (Found via a 500 on the metrics endpoint.)
- **Keep the staging server alive across tool calls.** On Proxmox `pct exec`, a foreground `uvicorn` dies when the exec session ends. Launch it as a background terminal process from the agent host (ssh + `exec uvicorn …`) so it persists; or `setsid nohup … & disown` inside the CT.
- **Multiple stale uvicorn processes on the same port** silently serve old code and cause confusing "address already in use" / wrong-config results. Before each relaunch: `pkill -9 -f "port 8001"` (or the relevant port), confirm `ss -ltnp | grep 8001` is free, then start exactly one.

## What a green smoke proves vs. what it doesn't
- Proves: payment settles, upstream executes, 200 returned, ledger/metrics correct.
- Does NOT prove the upstream provider credential is valid. If the upstream returns 401 (e.g. an expired API key) the payment STILL settles and is recorded — you'll see `validate 200, settle 200, then <upstream> 401`. That's a credential issue, not a code defect; flag it separately.
- **503 `provider_unconfigured` BEFORE settlement is a config gap, not a code bug.** If the route returns `{"error":"provider_unconfigured","detail":"... API key not configured on gateway"}`, the provider key (e.g. `ALCHEMY_API_KEY`) is empty in the deploy's config/.env — the facilitator never gets reached. This surfaces per-provider: a CoinGecko-free endpoint (no key needed) can still prove the FULL money path (402→settle→200→margin) while the keyed provider's endpoints 503. Fix by adding the real key to the deploy env; do not code around it.
- **Header spoofing cannot complete settlement on a throwaway localhost instance.** Injecting `host: api.cortexcloud.org` + `x-forwarded-proto: https` gets past the facilitator's `url_https` check, but the facilitator then runs an **`endpoint_reachable` preflight GET** on `resource.url` (the real public host). If the route is disabled/unreachable there, preflight fails `Not Found` and settlement is rejected — even though your localhost instance would have served it. So: to smoke a route, it MUST be live + reachable at the public https URL (enable it on the staging instance, or use a real tunnel). Spoofing headers alone is insufficient; don't chase that dead end.
