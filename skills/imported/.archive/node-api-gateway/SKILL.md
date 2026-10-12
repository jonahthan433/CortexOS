# node-api-gateway

# Node API Gateway (TS / Fastify / Prisma)

Class-level skill for building a production-grade, deployable API gateway as a
**modular monolith** — no microservices, no premature abstraction. The hard
part is not the business logic; it's the six boot-blocking scaffolding traps
below. Hit them once, internalize, never debug them again.

## When to use
- User asks for an API server in TypeScript/Node (Fastify, Express, Nest all
  qualify, but this skill's pitfall set is Fastify-tuned).
- OpenAI-compatible `/v1/chat/completions`, provider abstraction + smart
  routing, API-key auth, rate limiting, usage metering, x402 payments.
- "Build the foundation / MVP of our platform" style specs.

## Stack defaults (proven bootable)
- Fastify 4 (`^4.29`) + `@fastify/helmet@^11`, `@fastify/cors@^9`,
  `@fastify/rate-limit@^9`. **Do NOT pull helmet v12 / ratelimit v10 / swagger
  v9 — those require Fastify 5 and throw `FST_ERR_PLUGIN_VERSION_MISMATCH`.**
- Prisma 5 + Postgres, Zod for validation, Pino for JSON logging.
- Run with `tsx` in dev (`tsx watch src/app.ts`), `tsc` for build.

## Scaffold checklist
1. `package.json` with pinned Fastify-4-compatible `@fastify/*` majors (above).
2. `tsconfig.json`: `module: NodeNext`, `moduleResolution: NodeNext`,
   `esModuleInterop: true`, `allowSyntheticDefaultImports: true`, `strict: true`.
3. `src/env.ts` — Zod-validate all env at boot (fail fast).
4. `src/db.ts` — `export const prisma = new PrismaClient()` (lazy singleton;
   health endpoint must NOT require DB so the server boots without one).
5. Plugins as small files: logging, errors, auth, ratelimit, x402.
6. Routes: `chat`, `health`. App entry wires `buildApp()`.

## THE SIX PITFALLS (all hit in one session, all avoidable)

### P1 — write_file lint reports phantom TS errors. IGNORE THEM.
The `write_file` lint harness runs `tsc` **without your tsconfig** (no
esModuleInterop, wrong `module`), so it flags `import pino from "pino"` as
`TS1259`, `import.meta`/top-level-await as errors, etc. These are FALSE under
your real config. **Authority = `npx tsc -p tsconfig.json --noEmit`.** If that
exits 0, the code is fine. Never chase the harness's phantom errors.

### P2 — NodeNext import rules.
- Relative imports MUST include the `.js` extension: `import { x } from "../db.js"`
  (not `../db`). Missing it → `TS2307 Cannot find module`.
- CJS default exports (pino, pino-http, ioredis) under NodeNext need
  `esModuleInterop`+`allowSyntheticDefaultImports` (set in tsconfig). If a lib's
  types still resolve to a namespace (e.g. `pino-http` default import fails the
  `app.register` overload), cast at the call site: `app.register(pinoHttp as any, opts)`.

### P3 — DO NOT use pino-http. Use a plain pino hook.
`app.addHook("onRequest", pinoHttp)` → `TypeError: res.on is not a function`.
But the "correct" `app.register(pinoHttp, opts)` ALSO fails under `tsx`:
pino-http's default import resolves to the *module namespace*, so registering
it as a Fastify plugin hangs with `Plugin did not start in time: 'pinoLogger'`
(AVV_ERR_PLUGIN_EXEC_TIMEOUT). The reliable fix is to skip pino-http entirely
and log via a plain hook with `pino` directly — ~12 lines, one fewer dep:
```ts
export async function registerLogger(app: FastifyInstance) {
  app.addHook("onRequest", (req, _r, d) => { (req as any).id = crypto.randomUUID(); (req as any).t0 = Date.now(); d(); });
  app.addHook("onResponse", (req, rep, d) => {
    log.info({ rid: (req as any).id, method: req.method, url: req.url, status: rep.statusCode, ms: Date.now() - (req as any).t0 });
    d();
  });
}
```
`genReqId` must import crypto (`import * as crypto from "node:crypto"`),
else `crypto.randomUUID` is undefined. Call `await registerLogger(app)` in buildApp.

### P4 — Fastify plugin version pinning.
`@fastify/helmet@12`, `@fastify/rate-limit@10`, `@fastify/swagger@9` require
Fastify 5. On Fastify 4 pin `@fastify/helmet@^11`, `@fastify/rate-limit@^9`.
Mismatch throws at boot. Pick Fastify 4 OR 5 deliberately; align all `@fastify/*`.

### P5 — Prisma relation back-fields are mandatory.
If model `User` has `apiKeys ApiKey[]`, model `ApiKey` MUST declare the
opposite relation (`user User @relation(...)`) or Prisma validation fails
(`P1012 ... missing an opposite relation field`). Remove the forward field or
add the back-relation. `prisma generate` will not run until fixed.

### P6 — tsx does NOT auto-load `.env`.
`npx tsx src/app.ts` starts with empty env → Zod throws "Required" for
`DATABASE_URL`/`OPENROUTER_API_KEY`. Fix: `npx tsx --env-file=.env src/app.ts`,
or `import "dotenv/config"`. `prisma generate` needs `DATABASE_URL` in the real
env too (`export DATABASE_URL=...` before the command, inline prefix is flaky).
**Gotcha:** writing `.env` via a shell `cat > .env <<'EOF'` heredoc inside a
command that also uses `&` backgrounding gets rejected (exit -1) and the file is
NEVER written — then `--env-file=.env` says "node: .env: not found". Create
`.env` with the `write_file` tool, never a heredoc in a backgrounded command.

### P7 — DB-down must fail CLOSED (401), never 500.
Auth resolves the key via `prisma.apiKey.findUnique` in a `preHandler`. If
Postgres is down, that throws and escapes as **500 on every protected route**
(including the bad-body 422 test). Wrap the resolve in try/catch → return null
→ 401. This also makes `/health/ready` degradation graceful. Same pattern for
any preHandler that touches the DB.

### P8 — ioredis down = unhandled error spam.
`new Redis(url, { lazyConnect: true })` still emits unhandled `ECONNREFUSED`
events when Redis is absent, spamming logs (non-fatal; rate-limit just
degrades). Silence: `redis.on("error", () => {})`.

## Verification (headless, no DB)
Boot the server, then:
```
curl -s localhost:8000/health            # 200 {status:ok}
curl -s -o /dev/null -w '%{http_code}' localhost:8000/health/ready   # 503 (DB/redis down — expected)
curl -s -o /dev/null -w '%{http_code}' -X POST localhost:8000/v1/chat/completions -H content-type:application/json -d '{"model":"x","messages":[{"role":"user","content":"hi"}]}'   # 401 (no key)
curl -s -o /dev/null -w '%{http_code}' -X POST ... -H 'x-api-key: cc_dummy' -d '{"model":"","messages":[]}'   # 422 (bad body)
```
If /health returns 500, the crypto import (P3) is the cause.
**Verification nuance:** with DB down, the bad-body test (`-H 'x-api-key: cc_dummy'`
+ `{"model":"","messages":[]}`) returns **401**, NOT 422 — auth runs before body
parse and DB-down auth fails closed (P7). The 422 path is only observable with
Postgres up and a valid key. So the headless (no-DB) smoke asserts: /health 200,
/health/ready 503, any chat without key 401, any chat with key 401 (DB down).
422/402/happy-path need `docker compose up -d db redis` + a real provider key.

## Agent discoverability (the "First Mission" surface)
The gateway's real product is being *found by agents*. Beyond the API, expose a
self-describing discovery surface so an agent needs zero human explanation:
- `GET /v1/models` — OpenAI-style model list (`{object:"list",data:[{id,context_window,...}]}`).
- `GET /capabilities` — booleans: chat, streaming, x402, modelRouting, openaiCompatible, auth scheme, `spec` URL.
- `GET /pricing` — `per_1k_tokens: {model:{input,output}}` in USD.
- `GET /version` — name, version, OpenAPI pointer.
- `GET /status` — operational status.
- `GET /.well-known/ai-plugin.json` — agent manifest: `auth`, `api.openapi` URL, `description_for_model`.
- `GET /openapi.yaml` — served as `application/yaml` (bump to `openapi: 3.1.0`).
- `GET /` — minimal HTML index linking the above + meta `well-known` link.
All discovery routes are PUBLIC: exempt them from auth + rate-limit preHandlers
(match prefix list: `/health`, `/v1/models`, `/capabilities`, `/pricing`,
`/version`, `/status`, `/.well-known`, `/`, `/openapi.yaml`). Keep the model/pricing
catalog as a static curated list (`ponytail:` upgrade to cached live provider fetch
only when dynamic availability matters). This surface is the 80% of the
discoverability mission; SDKs/SEO/MCP docs are the 20% that follow once it's live.

## Build rules (from the user's spec, enforced)
- No placeholder code, no TODOs, no fake implementations, no unrequested
  abstractions (interface-with-one-impl, factory-for-one-product).
- Mark deliberate simplifications with a `ponytail:` comment naming the ceiling
  and upgrade path (e.g. `# ponytail: streamed-token metering deferred; record
  prompt tokens only until a usage endpoint lands`).
- Auth: store only `sha256(rawKey)`; show raw key once at creation.
- x402: emit `402` + payment payload, `POST /v1/settle` with `{txHash,payer}`,
  pluggable `verifyPayment`. On-chain settlement may live in another service —
  keep `verifyPayment` swappable.

See `references/fastify-ts-pitfalls.md` for exact error transcripts → fix.
