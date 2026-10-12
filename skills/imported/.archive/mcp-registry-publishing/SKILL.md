# mcp-registry-publishing

# MCP Registry Publishing

How to get an MCP server listed across the 5 major registries (verified 2026 against primary sources: live API endpoints, official docs, submit pages).

## Compare: submission mechanism by registry
| Registry | Submission endpoint | Accepts hosted remote URL? |
|---|---|---|
| **Official MCP Registry** | `mcp-publisher` CLI; raw API `POST https://registry.modelcontextprotocol.io/v0/publish` (+ `/v0/validate` dry-run) | ✅ Native `remotes` Streamable-HTTP entry |
| **Smithery** | Web form `https://smithery.ai/new` (enter public HTTPS URL); CLI `smithery mcp publish "<url>" -n @org/server` | ✅ Primary path now — "bring your own hosting", Gateway proxies upstream |
| **PulseMCP** | Web form `POST /submit` at `pulsemcp.com/submit` (URL uses GitHub repo URL or standalone site) | ✅ Accepts URL directly; also auto-ingests from Official Registry |
| **mcp.so** | Web form `/submit` — GitHub repo URL (required) + project name | ❌ Submit GitHub repo; remote endpoint indexed from repo/README |
| **Glama** | Web form on `/mcp/servers` → "Add Server" — GitHub repo URL + name + desc | ⚠️ Entry = open-source GitHub repo; remote endpoints listed once in repo |
| **apis.guru** | `apis.guru/add-api` — the form VALIDATES client-side then composes a **GitHub ISSUE** in `APIs-guru/openapi-directory` (`labels=add API`); the maintainers/bot add the API from the issue. No separate PR step. | ✅ Any valid OpenAPI 3.x JSON at a stable URL |

## Recommended order for a hosted Streamable-HTTP server
1. **Official MCP Registry first** — publish a `remotes` entry; this auto-ingests you to **PulseMCP**.
2. **Smithery** via `smithery.ai/new` (direct URL; gateway proxies).
3. Submit the **public GitHub repo** to mcp.so and Glama, with the hosted URL declared in the README/repo so the remote endpoint gets indexed.

## Core rules
- Official Registry is **not** the legacy GitHub-Pages `registry.json` PR flow — it's a live service (`github.com/modelcontextprotocol/registry`) published via the `mcp-publisher` CLI or direct HTTP API. `remotes` entries are JSON per `server.schema.json` (2025-12-11). Registry is preview; breaking changes/data resets possible.
- **Auth == namespace shape (Official Registry).** GitHub OAuth device flow → name must be `io.github.<user|org>/*`; DNS TXT auth (`v=MCPv1; k=ed25519|ecdsap384; p=<pubkey>`) on apex → reverse-DNS `com.example/*`. GitHub org namespace needs Org **Owner** (or PAT with `read:org`); PAT for CI via `/v0/auth/github-at`.
- **Smithery scan pitfall:** its metadata scan getting 403 fails the publish. Don't block automated scans (Cloudflare Bot-Fight Mode); return **401** (not 403) for unauthenticated to trigger OAuth. Auth-wall fallback: static card at `/.well-known/mcp/server-card.json`.
- Pulsemcp.com and other Cloudflare-protected sites block default curl and headless browser; retry with a browser UA (`-A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124"`) — usually passes.

## Pitfalls learned in production (2026-08)
- **Never `mcp-publisher init` over a crafted server.json.** `init` regenerates the template (`com.example/tmp`) — publishing that yields 403 "You have permission to publish: io.github.<you>/*. Attempting: com.example/tmp". Run `publish server.json` from the project dir; edit the committed file, don't re-init.
- **`/v0/validate` is self-documenting.** 422 errors name each missing field verbatim (e.g. "expected required property $schema", "expected length <= 100" for description). Iterate against it until `{"valid":true}` — zero auth needed.
- **Repository schema gotcha:** `repository` is `{source, url, id}` — no `type` key. `id` = `gh api repos/<owner>/<repo> --jq '.id'`. `name` must be `io.github.<username>/<server>` (slash inside the name).
- **Read-back requires percent-encoding:** `GET /v0/servers/<name>/versions` 404s unless the `/` in `io.github.<user>/<server>` is encoded: `io.github.<user>%2F<server>`.
- **GitHub PAT exchange is flaky:** `/v0/auth/github-at` with a `ghp_` token succeeded once, then 401'd on every retry (cooldown/one-shot). The reliable path is interactive `mcp-publisher login github` (device flow, user's browser) — plan for it.
- **Smithery scan pre-flight:** before submitting, probe the URL with the scan UA so Cloudflare/bot-fight doesn't fail the publish. Re-runnable probe: `scripts/smithery-scan-probe.py <url>` (expect HTTP 200 + valid `initialize` result).

## Updating an existing listing (hit 2026-08: stale server on Smithery)
- **Re-publish to the SAME `-n` name = in-place upsert.** `smithery mcp publish <url> -n <org>/<server>` over an existing name updates the entry — no duplicate, no delete. A URL-based entry proxies your LIVE endpoint, so future changes show up with zero re-publishing (prefer it over repo-based deploys for that reason).
- **Verify the old endpoint is actually dead before blaming the listing:** `curl -X POST <old-base>/mcp` with MCP headers → 404 = old server gone, listing stale; 200 = still live (metadata-only refresh).
- **Hosted-proxy health probe:** Smithery-proxied servers answer at `https://<server>--<org>.run.tools/`. A `401 {"error":"invalid_token",...}` is HEALTHY — deployed and auth-gated as designed (the platform injects the token). 404 = not deployed. Don't read 401 as failure.
- **Crawler self-check before submitting:** mcp.so/glama crawl public servers — check `https://mcp.so/search?q=<name>` / `https://glama.ai/mcp/servers?q=<name>` first. TRAP: the page echoes your query, so `grep -c <name>` hits are NOT index hits — read the result-count text ("No servers match …" / "0 results").
- **apis.guru logo:** renders `x-logo.url` from the spec — no `x-logo` = logo-less listing. Add `x-logo: {url: <public png>}` + `info.x-apisguru-categories: [<cat>]` to the spec generator, re-serve, then submit the URL (Glama/mcp.so read `x-logo` too when they index the spec).
- **apis.guru add-api = a GitHub issue; submit it directly with `gh`** (2026-08, reverse-engineered from the form's JS). Form's client-side checks: URL path must not be `/` (root → "specify a machine-readable API definition location, not a website root URL"), must not end `.html`, no localhost; it probes via `https://cors.redoc.ly/<url>` and rejects `text/html` content-type. Submission = redirect to `github.com/APIs-guru/openapi-directory/issues/new?labels=add%20API&title=Add "<name>" API&body=<body>` where body lines are `**Key**: value` — keys `Format, Official, Url, Name, Category, Logo`. Skip the form entirely:
  `gh issue create --repo APIs-guru/openapi-directory --title 'Add "CortexCloud" API' --label 'add API' --body $'**Format**: openapi\n**Official**: true\n**Url**: https://api.cortexcloud.org/openapi.json\n**Name**: CortexCloud\n**Category**: machine_learning\n**Logo**: https://api.cortexcloud.org/cortex-logo.png'`
- **apis.guru validator gotchas (each caused a rejection before it was found):** (1) spec MUST have an absolute `servers[].url` — FastAPI's default openapi.json has NO `servers`; inject `spec["servers"]=[{"url":"https://api.cortexcloud.org"}]` in the spec generator. (2) `info.x-apisguru-categories` values must be from their valid set — `"optimization"` is INVALID and rejected; use `"ai-ml"` (or another listed category). Both belong in `update_openapi_v2.py` so the served spec stays directory-ready.

## Official Registry: end-to-end publish flow (verified 2026-08, CortexCloud `io.github.jonahthan433/cortexcloud`)
- **npm package is a hard prerequisite.** The registry hosts metadata only — publish the package first. Ownership verification = `mcpName` in package.json (`"mcpName": "io.github.<user>/<server>"` — MUST match the `io.github.<user>/` prefix for GitHub auth) plus a `repository` field. Scope ownership: the npm account publishing MUST own the scope — E404 `registry.npmjs.org` on publish = logged into the wrong account (fix: `npm whoami` / `npm login`).
- **Description limit bites TWICE:** the registry rejects `body.description` > 100 chars on `publish` — and the npm package description is checked too. Keep BOTH `server.json.description` and `package.json.description` ≤ 100 chars (verified: 78/88 chars pass).
- **`mcp-publisher publish` reads the LOCAL `server.json`.** After fixing descriptions in the repo, `git pull` on the publishing machine before re-publishing. Never re-run `init` (it regenerates a `com.example/tmp` template → 403).
- **JWT expiry:** `mcp-publisher publish` fails 401 "invalid or expired Registry JWT" mid-session; fix is `mcp-publisher login github` (device flow, fresh JWT). DNS timeouts during login = transient, just retry the login.
- **Read-back verification (working endpoint):** `GET https://registry.modelcontextprotocol.io/v0.1/servers?search=<name>` — substring search, returns ALL published versions (old ones stay with `_meta.io.modelcontextprotocol.registry/official.isLatest: false`). Per-server GET paths (`/v0.1/servers/<name>` etc.) 404; `/docs` → `openapi.yaml` is the API reference.
- Old versions of your server remain listed as non-latest — harmless; no action needed.

## awesome-mcp-servers (punkpeye) — add a server via PR (verified 2026-08)
- Flow: `gh repo clone punkpeye/awesome-mcp-servers` → `gh repo fork --remote=true` (if the fork remote isn't added, `git remote add fork https://github.com/<you>/awesome-mcp-servers.git`) → branch → edit README → `git config user.name/user.email` (needed: commits fail with "Author identity unknown"; use `<gh-user>@users.noreply.github.com`) → `git push -u fork <branch>` → `gh pr create --repo punkpeye/awesome-mcp-servers --head <you>:<branch> --title ... --body ...`.
- Entry format (one line in README): `- [owner/repo](https://github.com/owner/repo) <emoji-tags> - <description with install/endpoint hints>`. Emoji legend in README: 📇 TypeScript, 🐍 Python, ☁️ remote/cloud-hosted, 🏠 self-hosted, 🎖️ official. Optional glama badge `[![name MCP server](https://glama.ai/mcp/servers/OWNER/REPO/badges/score.svg)](...)`.
- Placement: category section (e.g. `### 🧮 Data Science Tools`), roughly alphabetical by owner within section. Remote streamable-http servers: include `☁️` and state the endpoint URL in the description.

## Support
- `references/2025-2026-mechanism.md` — exact endpoints, required server.json fields, auth flows, per-registry pitfalls.
