# x402-directory-submission

# Submit an x402 / agent-native API to discovery directories

## When to use
A pay-per-call API (x402, MPP, or an MCP tool server) is live and needs to be
discoverable by autonomous agents. You're asked to get it listed on free
agent/API directories. No paid placement, no ads.

## Prep (do this first)
- Position every listing with the canonical **domain + one-line tagline**, e.g.
  `"<Name> — agent-native x402 APIs at <domain>"`. If the brand collides with
  a bigger product, ALWAYS lead with the domain, never the bare name (recorded
  case: "CortexCloud" collides with Palo Alto Networks' Cortex Cloud — searches
  are dominated by them; the domain `api.cortexcloud.org` is the differentiator).
- Have ready: `url`, `description`, `category`, `endpoints` (as `/v1/path`
  ARRAY, NOT full URLs), `settler_address` / payTo, `contact` email.
- Ensure the service EXPOSES crawlable discovery so directories auto-index you
  passively: `/.well-known/x402.json`, `/.well-known/bazaar`,
  `/.well-known/agentsearch.txt`, `/llms.txt`, `/openapi.json`, `/mcp`.
  (agent-tools.cloud confirmed it auto-listed a service from its live bazaar.)

## Submit — the runtime often has NO browser/Chrome (DISPLAY unset)
Do NOT stall on "no browser available." Most directory `/submit` pages are
SPAs whose form POSTs to a backend API. Reverse-engineer and `curl` it:
1. `curl` the `/submit` page; extract its JS chunks.
   - Next.js: grep `/_next/static/chunks/app/submit/page-*.js` for
     `v1/submit`, `radar-submit`, `fetch("/api/...`, `body:JSON.stringify({`.
   - Astro/plain: grep the HTML for `action="/api/..."` and `<input name=>`.
2. Read the payload keys from that chunk (the `JSON.stringify({...})` object).
3. `curl -X POST <api> -H 'content-type: application/json' -d '{...}'`.
4. Iterate on 400/422 — the error body lists exactly which fields are missing
   or invalid. Fix and resend. (Example: x402-list's API wants key `url`, not
   `service_url`; `endpoints` must be a JSON array of `/v1/...` paths.)

## Known directory map (current as of 2026-08; verify before each submit)
See `references/directory-endpoints.md` for exact payload quirks. Summary:
- **x402-index** (GitHub): open an issue (free, 24h verification). No browser.
- **x402-list.com**: `POST /api/v1/submit` → 201. Wants `url` (not
  `service_url`), `website_url`, `email`, `endpoints` as `/v1/path` array.
- **agent-tools.cloud**: `POST /api/v1/submit` → also auto-indexes from
  `/.well-known/bazaar`. Requires `contact`. May already list you.
- **402radar.io**: `POST /api/radar-submit`. Was returning 503 (degraded) —
  retry; if still down, record and skip.
- **x402scout.com**: SUSPENDED (503). Skip.
- **MCP registries** (Smithery, mcp.so, Glama, awesome-mcp-servers): require an
  authenticated account login. Not submittable without login; mcp.so may also be
  unreachable from some IPs. Account creation needs separate approval (standing
  guard: ask before creating accounts).

## Full-platform vs optimization-only positioning (CortexCloud-specific lesson, generalizes)
When the API spans multiple categories (Automation/Data/Research/AI/Opt/Quantum),
NEVER lead listings with one category. Audit every surface for stale single-category
framing BEFORE submitting, and reposition to the full platform with the easiest/
cheapest categories first (Acquisition priority: Automation/Data/Research; Opt/Quantum
= differentiator). Do NOT market a category that is not production-ready (e.g. ML
preview) — qualify it as "preview only" or omit. Fix doc/repo surfaces AND live API
strings (e.g. `/llms.txt` built from `discovery.py`) — the latter is app code, gets
approval first.

## Pitfalls (observed)
- **Agoragentic "Agent Trap Shield"**: POST/PATCH to `/api/capabilities` is scanned
  for instruction-like/imperative external content and returns `403 agent_trap_blocked`
  if the description uses second-person/imperative phrasing ("Solve...", "Pay per
  call..."). Rewrite the description as DECLARATIVE FACTS ("CortexCloud is an
  API platform. Categories include..."). The shield is stricter on PATCH (mutating an
  existing listing) than POST. Declarative copy passes.
- **Field-name mismatches**: directories invent their own schema. Always read the
  OpenAPI (`/openapi.json`) to get exact required fields + names before guessing.
  Agoragentic wants `endpoint_url` (not `endpoint`), `price_per_unit` (not `price`),
  required `name/description/category/price_per_unit`. PayanAgent wants `walletAddress`
  (required, = payTo) and `priceCents` (integer, not `price`/`price:0.05`).
- **Name collisions**: "CortexCloud" was already taken on Agoragentic (409) — register
  a unique handle (e.g. "CortexCloud API") while keeping the display name as specified.

## Guardrails
- No paid listings, subscriptions, or promoted placement — ever, without
  explicit approval.
- Record every submission URL/status. Stop and report when the free,
  browser-less submissions are exhausted; do not fabricate results.
- This is GTM/discovery only. Do not touch production code, pricing, or
  architecture to satisfy a listing.

## References
- `references/directory-endpoints.md` — exact endpoints, payload field maps,
  and per-directory quirks discovered while submitting CortexCloud.
