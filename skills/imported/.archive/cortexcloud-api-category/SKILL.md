# cortexcloud-api-category

# Build a new CortexCloud API category

Use when adding a new endpoint group behind x402/MPP on CortexCloud
(e.g. the ML and Automation tiers already built this way). Reuse, don't
rebuild: x402/MPP settlement, `X402RateLimitMiddleware`,
`InputValidationMiddleware`, `ROUTE_PRICING`/`ROUTE_DESCRIPTIONS`/
`FREE_ROUTES`, Bazaar `_TOOLS`, OpenAPI regen, `BaseProvider`/pricing,
feature flags.

## Recipe (minimal diff)
1. `app/core/config.py`: master `X_ENABLED` (default **False**) + per-endpoint
   gates `X_TRANSFORM_ENABLED` etc. (default True) + any secrets.
2. `app/x402/pricing.py`: add `X_FLOOR` dict + `x_price_usd(ep)` using
   `pegged_price(0.0, floor=...)`; register ROUTE_PRICING / ROUTE_DESCRIPTIONS
   / FREE_ROUTES entries (incl. a free `/v1/x/estimate`).
3. `app/middleware/x402.py`: add `elif path.startswith("/v1/x/")` branch that
   (a) returns **503 before the 402 challenge** if disabled (see pitfall),
   (b) sets `request.state.category="x"` + `provider_cost_usd`.
4. `app/api/x.py`: router + routes; `_disabled(ep)` returns 503 when master or
   per-endpoint flag off; free `/estimate` must NOT gate on master flag.
5. `app/x402/bazaar.py`: add `_TOOLS` entries (paid + free).
6. `app/main.py`: `include_router` (one line, in the additive block).
7. `tests/test_x.py` + `scripts/x_paid_smoke.py` (reuse SDK `sign_payment`).
8. Branch from `main` (NOT a sibling feature branch — keep others frozen).
   `X_ENABLED=false`. Do NOT merge/deploy without explicit approval.

## Pitfalls (learned the hard way)
- **MIDDLEWARE PRE-402 GATE (charge-then-fail trap).** The x402 middleware
  runs OUTER to your route. If you gate only in the route (`_disabled()`),
  the middleware already emitted a **402 payment challenge** → the client
  pays, then hits your 503. Gate in the middleware branch BEFORE
  `usd_to_usdc_atomic(price_str)` / challenge build:
  `if not settings.X_ENABLED: return JSONResponse(503, {...})`.
  See `references/middleware-gate.md` for the exact snippet.
- **Free `/estimate` must bypass the master gate.** If it calls `_disabled()`
  it 503s when the category is disabled, hiding pricing. Gate only paid routes.
- **JSONB import**: `from sqlalchemy.dialects.postgresql import JSONB`
  (NOT `from sqlalchemy import JSONB` — ImportError on SQLAlchemy 2.x).
- **Self-hosted pricing**: provider cost ~$0 → `pegged_price(0.0, floor=X)`
  returns exactly X (max(X, 0*1.35+0.0015)). Use per-endpoint floors.
- **Provider-funding blocker, don't fake green.** If an endpoint can't be
  validated because the upstream needs credits (OpenRouter 0 credits → paid
  model 402; free models throttled 429/502; fal TOP_UP-locked; Replicate
  needs credit — a saved card is NOT credit), **report the blocker honestly**.
  Do NOT keep trying free providers or hack code to force a green smoke.
  Self-hosted endpoints (no external provider) validate immediately via paid
  smoke. This is a standing user preference, not optional.
- **5xx after x402 settlement charges the customer** — there is no refund
  mechanism in the platform. Keep provider calls behind payment; fail fast on
  invalid input. This is a platform-wide limitation, not a per-category fix.

## Reuse pointers
- Paid smoke: `sdk/python/cortexcloud/signing.py: sign_payment(challenge, key)`
  → resubmit with `payment-signature` header. Same pattern as Data/ML smoke.
- HMAC webhook signatures: `hmac.new(secret, body, sha256).hexdigest()`,
  header `X-Cortex-Signature: sha256=<sig>`.
- SSRF guard (for egress endpoints): block private/loopback/link-local/
  metadata (169.254.169.254) IPs, re-check resolved IPs (DNS-rebinding),
  validate redirect targets through the same guard; cap body + timeout.
- Postgres for durable state (e.g. scheduled jobs): define an ORM model in
  `app/models/`, register it in `app/models/__init__.py`, create the table
  with `AsyncEngine.begin() -> conn.run_sync(Model.__table__.create,
  checkfirst=True)` (not `AsyncEngine.run_sync` — that API doesn't exist).
