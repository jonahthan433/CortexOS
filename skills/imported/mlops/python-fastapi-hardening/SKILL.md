# python-fastapi-hardening

# FastAPI/Starlette Security Hardening

Run a hardening pass **section by section**, live-verify each guard with curl
against the running service, and confirm what changed after each section. Never
break existing endpoint contracts. Audit before implementing.

## Starlette middleware gotchas (all hit live)

- **`JSONResponse(415, content=...)` positionally crashes** — the first
  positional arg binds to `content`, so passing an int + a `content=` kwarg
  raises `TypeError: JSONResponse.__init__() got multiple values for 'content'`
  → every guarded request 500s. Always write `JSONResponse(status_code=415,
  content={...})`. One regex fix across the file:
  `/JSONResponse\((\d{3}), content=/JSONResponse(status_code=\1, content=/`.
- **`MutableHeaders` has no `.pop()`** — popping a header from a Starlette
  response raises `AttributeError: 'MutableHeaders' object has no attribute
  'pop'`. Use `try: del resp.headers[k] except (KeyError, AttributeError)`.
- **`server: uvicorn` is added at send-time by uvicorn — middleware can't
  remove it.** Strip identity with the uvicorn CLI flag `--no-server-header`
  in the systemd/exec line, not in the middleware.
- **Middleware ordering: last `add_middleware()` call runs FIRST (outermost).**
  To short-circuit malformed bodies before the payment gateway/ratelimiter sees
  them, register the input-validation middleware LAST. Verify with a curl that
  should 415/400 BEFORE the 402 gate.
- **Body is safe to re-read, never `_receive`.** Starlette buffers the request
  body, so an outer middleware can `await request.body()` and the inner x402
  routing re-reads it fine. Re-injecting `request._receive` in a
  `BaseHTTPMiddleware` kills SSE (empty 200). Use `request.state` for
  cross-middleware data, not contextvars.
- **`raise HTTPException(...)` inside `@app.middleware("http")` does NOT become
  the intended status code — it becomes a 500.** FastAPI's exception handlers
  wrap route handlers, not the middleware chain, so an exception raised in
  middleware falls through to the generic 500 path uncaught. An auth-check
  middleware that `raise`s 401 on a bad token will curl-test as `500` for both
  the missing-token and wrong-token cases — looks like the auth logic is
  broken when it's actually the wrong return mechanism. Fix: `return
  JSONResponse(status_code=401, content={...})` directly from the middleware,
  never `raise HTTPException` there. Verify with curl on all three cases (no
  token, wrong token, right token) before trusting the status codes.

## Input validation middleware (one pass, at the boundary)

A single `InputValidationMiddleware` (outermost) covering all body/query guards:

- POST/PUT/PATCH/DELETE → **415** unless `Content-Type: application/json`.
- **Gate by method first.** GET/HEAD/OPTIONS carry no body — guard them with
  `if request.method not in ("POST","PUT","PATCH","DELETE"): return await call_next(request)`
  before the content-type check. A blanket 415 on "not JSON" fires on HEAD/OPTIONS
  (health probes, crawler probing, x402scan) and breaks them — verified regression:
  `curl -sI /health` returned 415 until the method gate was added.
- Body > 1MB → **413**.
- Invalid JSON / nesting depth > 10 → **400** (JSON bomb).
- Chat: `messages` > 500 → 400; single message > 100KB → 400;
  unknown `model` → 400; token estimate > model `context_length` → 400;
  `tools` on a model w/o `tool_calling` capability → 400; `max_tokens` >
  `context_length` → 400.
- GET `/data`: allowlist query chars, reject shell/SQL/URL-encoded payloads
  → 400. Address params (`address`/`addr`/`pair`/`token`) must be valid
  checksummed ERC-55 (`web3.is_checksum_address`).
- **Exempt form/multipart**: OAuth2 login and multipart uploads are NOT JSON.
  `if ct in ("application/x-www-form-urlencoded",) or ct.startswith("multipart/")`
  → pass through, or you 415-break auth logins.

### Path-matching bug
`startswith("/data/")` misses `/x402/v1/data/*`. Match the suffix: `/data/`.

### DB model metadata
Match client-facing `model` against the DB registry `name` (not the UUID `id`).
Field is `context_length`, NOT `context_window`. Cache 60s; fail-empty (return
empty set so the allowlist is skipped rather than blocking the gateway).

### Live verification
A `401`/`402` means the payment gate ran FIRST (your guard didn't); a guard
claim needs 400/413/415. A `;` in a shell URL splits the command — use
URL-encoded params or a python httpx probe, never inline semicolons.

## Auth brute-force throttle

Login endpoints (OAuth2 form) usually have NO rate limit — add per-IP throttle
reusing the existing Redis `RateLimiter.is_rate_limited(key, limit, window)`:
`ratelimit:login:ip:{client_ip}`, e.g. 10/60s → **429**. Fail-open on Redis
errors (don't lock everyone out because Redis hiccuped). Verify with 13 rapid
attempts: expect 401×9 then 429×4.

## TLS / transport audit (live)

- **Min-TLS is enforced at the TLS terminator, not in the app** — probe the edge:
  `for v in tls1 tls1_1 tls1_2 tls1_3; do out=$(printf '' | timeout 12 openssl s_client -connect HOST:443 -$v 2>&1); echo "$out" | grep -q "BEGIN CERTIFICATE" && echo "$v OK" || echo "$v REJECTED"; done`
  A naive `curl --tlsv1 / --tlsv1.1` loop against the domain is unreliable (modern curl routes `--tlsv1` oddly; it "errors" ambiguously). Prefer `openssl s_client` matching the cert marker.
- **Plain HTTP (port 80) returning 200 = no auto-redirect** — Cloudflare's
  "Always Use HTTPS" is OFF. Flag it as a dashboard toggle (infra action, not
  code); `curl -sI https://host` should NOT show port-80 200-without-redirect.
  **How to verify after the user flips the toggle:**
  `curl -sI http://HOST/health` must return `301` with `redirect_url=https://HOST/health`
  (before: `200`). HTTPS itself must still be `200`. That pairing is your proof
  the toggle took effect.
- **Outbound provider cert verification**: grep the whole tree for `verify=False`, `check_hostname=False`, `ssl_unverified`, `create_default_context` — any hit is a disabled-verification leak. Ensure providers build pooled `httpx.AsyncClient`/SDK clients with default verify ON (do not pass `verify=False`).

## Dependency audit — pip-audit (Section 8)

- Deps are already exactly pinned (`==`) via `requirements.txt` — that satisfies "pin exactly"; don't add a lockfile unless required.
- `pip install pip-audit` in the venv (one-off; audit tool), then `pip-audit -r requirements.txt`. Crucial: for this class of API **`cryptography` is a live dependency (ECDSA response signing)** — if the audit flags it, the fix matters and the upgrade has blast radius.
- **After upgrading a crypto/signing library, re-verify the signing surface lives**: restart, then `curl -w %{http_code} /x402/v1/pubkey` must be 200 (proves the ECDSA path still loads), plus a `pip-audit` re-run must report clean. The test earlier showed PUBKEY=200 is the right liveness check — not just `/health`.
- Add a tiny reusable gate `scripts/dep_audit.sh` that runs `pip-audit -r requirements.txt` and exits nonzero on any vulnerability — that's the "CI fails on new vulns" item; make it re-runnable rather than a one-off.
- Don't prune "unused" deps that show 0 imports in `app/` — they're usually transitive (uvloop/watchfiles=uvicorn extras, regex=tiktoken, pluggy=pytest). Skipping pruning is the lazy correct call unless a full pip-breakage analysis says otherwise.

## Rotating a live DB/secret (Postgres) without breaking prod

The app reaches Postgres via Docker (`cortexcloud-db` container inside the LXC). To rotate and PROVE the new password works:
1. Generate `openssl rand -hex 24`; ship the value to a file on the host/container (`scp` → `pct push`) and `sed` it into `.env` — **never inline the password through ssh quoting** (nested `$NEW` through `ssh + pct exec + docker exec` layers mangles: set placeholder → `ALTER ROLE ''` cleared the password, an outage window). File-based transfer, then source/cat it at the destination.
2. Set it in the role: `psql -c "ALTER ROLE postgres PASSWORD '<the value>';"` — do not let `$NEW` come from a shell var that failed to expand.
3. Restart the app and verify with a **DB-touching request, not just `/health`**: a login that returns `401` (not a 500) proves Postgres accepted the new credentials over TCP and ran the auth query. `401` = auth-result OK; `500` = auth failure.
4. Credential hygiene: save the new secret to a chmod-600 file on the agent host, scrub all `/tmp` copies on host + container, and never ECHO/reprint the value in chat or logs. Tell the user to move it to their password manager.
5. After rotating a weak default, re-add it to the fail-fast dev-default list (it was excluded only pending rotation) so it can't regress.

## HTTP security headers

HSTS `max-age=31536000; includeSubDomains`, `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'none'`,
`Referrer-Policy: no-referrer`, `Cache-Control: no-store`, `X-Request-Id`.
Strip `Server`/`X-Powered-By`/`X-AspNet-Version`.

### HSTS `preload` (browser list) — separate from the header

The header alone (`max-age=31536000; includeSubDomains`) is NOT enough for the
browser preload list: hstspreload.org requires the literal `preload` directive
and a max-age ≥ 31536000. Add it in-repo (one string):
`Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`.
Cloudflare passes the origin's HSTS header through, so the public edge then
serves the `preload` value — verify with `curl -sI https://host/health |
grep -i strict-transport`. Then the user submits the domain at hstspreload.org
(their step, not code). Order matters: enable Always-Use-HTTPS first, verify
the 301, wait a day, only then promote to preload — preload is near-irreversible
(browsers ship it; removal takes months), so never enable it on a domain that
still serves plain HTTP.

## Secrets fail-fast (Section 7)

Pydantic-settings `BaseSettings` reads `.env` via `env_file` but does **NOT
export values into `os.environ`**. A startup fail-fast that checks
`os.environ.get("SECRET")` will wrongly report secrets "missing" that exist
only in `.env` (killed a boot). Fix: declare the secret as a real Settings
field (`VAR: Optional[str] = Field(None, env="VAR")`) and read it with
`getattr(self, var, None)` — pydantic loads it from `.env`.

Add a `model_validator(mode="after")` guarded by `if self.ENV == "production":
return self`:
- Reject known dev-placeholder defaults (`dev-jwt-...`, `dev-api-key-salt-...`,
  `postgres`) → raise `ValueError("[S7] ...")` so the process refuses to boot.
- Require non-empty for critical secrets (wallet, signing key, facilitator key
  ID+secret).
- Rotate BEFORE enabling POSTGRES_PASSWORD fail-fast, or the service won't boot
  until rotated. Temporarily exclude a secret pending rotation rather than
  keeping prod offline; re-add it once real.

### Verifying fail-fast
`env -u SECRET python -c "from app.core.config import settings"` does NOT clear
the file value — pydantic still reads `.env`. True test: copy `.env` aside,
`echo ENV=production > .env`, import (expect pydantic ValidationError), restore.
Health-200 proves all required secrets are present; a 500/`ValueError` at boot
is the guard working.

## Security audit log + alert counters (Section 9)

Events already get logged to stdout→journald, but §9 wants a **separate,
append-only security log** plus alerting. Two tiny helpers in one
`app/middleware/audit.py` module, both **fail-open** (an fs/Redis error never
breaks the request path):

```python
def audit(event, **fields):  # append one JSONL line to /var/log/cortexcloud/security.log
    # os.open(path, O_WRONLY|O_CREAT|O_APPEND, 0o600); write json; 32MB cap via
    # lseek+truncate. NEVER log secrets/bodies — coerce field values to str.

async def alert(client, bucket, window, threshold, event, **fields) -> bool:
    # Redis: INCR secalert:{bucket}; expire on first incr; when n>=threshold
    # logger.error(json.dumps(...)) then DEL so fresh windows re-alert.
```

Wire `audit(...)` at every guard/reject you already have (proof_rejected with
payer+reason+ip, rate_limited, nonce_replay, login_bruteforce, and each
validation reject kind with ip+path). Wire `alert(get_redis(), bucket, ...)`
at the money/DoS surfaces (e.g. 10 proof fails/60s = attack signal; 200
rate-limit/5min = DoS). Reuse the app's existing redis singleton
(`get_redis()`) rather than a new client — don't add a fresh dependency.

Lazy skips: the **outbound notification channel** (PagerDuty/email/webhook) is
NOT part of the lib — you emit to journald/stdout `ERROR` (`logger.error`) +
Redis counters; shipping an actual notifier is the user's infra call. Direct page
subscribers at the channel, not in the code. Also skip an audit DB table unless
analytics demands it — append JSONL first.

File perms matter: the systemd unit may run as root (no `User=`), so the log
dir/file land owned by root; `mkdir -p /var/log/cortexcloud` at bootstrap and
let the process own it.
