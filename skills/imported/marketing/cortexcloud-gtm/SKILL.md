# cortexcloud-gtm

# CortexCloud GTM — distribution, not infrastructure

## When to Use
- "Submit CortexCloud to x402/API/MCP directories", "get us listed on <dir>".
- "Track external paid calls / reach 100 external calls", "set up GTM metrics".
- Any distribution/discovery task for CortexCloud that is NOT building a feature
  (feature work → cortexcloud-api-dev).

Context: CortexCloud is production. The user's standing GTM rule: **no new
features, no Automation Tier 2, no ML merge, no pricing/arch changes** during
GTM. Freeze is active. This skill is about *getting external agents to pay*,
measured strictly.

## The milestone that matters
- **Target: 0 → 100 genuine EXTERNAL paid API calls.** Internal/test-buyer
  calls (the `/root/bzcheck/test_buyer.key` payer, address
  `0xCed8a9ff73427302cD0F0F95892EbfC2Ac83374A`) are EXCLUDED by filter.
- Track (read-only, never modify prod data): unique external payers, external
  paid calls, revenue, endpoint usage, repeat payers, acquisition source,
  failure rate, margin. See `scripts/gtm_metrics.py` pattern in the repo
  (`CortexCloudAPI/scripts/gtm_metrics.py`): filter `lower(payer) NOT IN
  (<internal set>)` — the internal address is MIXED-case in the DB, so compare
  `lower(payer)`, not the literal.

## Free directory / MCP-registry submission — NO BROWSER NEEDED
No Chrome/display in this runtime; `computer_use` + `browser_exec` are
unavailable. Reverse-engineer each directory's submission endpoint instead of
clicking a form:
1. `curl` the `/submit` page; grep for the JS chunk (`/_next/static/chunks/...`
   for Next.js) and the API call (`grep -oiE 'v1/submit.{0,400}'`).
2. Find the POST path + payload keys (e.g. x402-list `/api/v1/submit` wants
   `url` not `service_url`, endpoints as a JSON array of `/v1/...` paths;
   agent-tools `/api/v1/submit` requires `contact`).
3. `curl -X POST` it directly. Lead with `api.cortexcloud.org` and "agent-native
   x402 API platform" (NOT bare "CortexCloud" — collides with Palo Alto
   Networks Cortex Cloud).

### Directory status (verified this session)
- **x402-index** (GitHub): open an issue on `x402-index/x402-discovery-index`
  (free, ~24h verification). Account: existing GitHub.
- **x402-list** `/api/v1/submit`: works (HTTP 201, pending). 3 endpoints probed.
- **agent-tools.cloud**: often already auto-indexed via our live
  `/.well-known/bazaar` + `x402.json` + `/mcp` (passive crawl — confirm via
  their slug search).
- **402radar** `/api/radar-submit`: service degraded (503) — retry later.
- **x402scout**: SUSPENDED (503) — skip.
- **MCP registries (Smithery / mcp.so / Glama / awesome-mcp-servers)**: require
  authenticated logins; mcp.so unreachable from this IP. Account creation is a
  separate approval — hold unless the user approves.
- **Passive win**: all discovery surfaces (`/.well-known/x402.json`,
  `/.well-known/bazaar`, `/llms.txt`, `/openapi.json`, `/mcp`) return 200 →
  crawler-based directories pick us up automatically.

## AgentCash registration/indexing (PRIMARY GTM mechanism)

AgentCash is the highest-priority GTM channel. The correct flow:

### 0. Install AgentCash CLI (local install — global fails with EACCES)
```bash
mkdir -p ~/local-npm && cd ~/local-npm && npm init -y && npm install agentcash
# Run via: ~/local-npm/node_modules/.bin/agentcash <cmd>
```
Global install (`npm install -g agentcash`) fails with `EACCES: permission denied` on shared hosts. `npx agentcash@latest` works but re-downloads every time.

### 1. Verify discovery
```bash
~/local-npm/node_modules/.bin/agentcash discover https://api.cortexcloud.org
# Expect: found=true, trustTier=origin_hosted, ~45 endpoints
```

### 2. Register/index (do NOT use `register` — it hangs indefinitely)
```bash
~/local-npm/node_modules/.bin/agentcash add https://api.cortexcloud.org
# Installs skill to ~/.hermes/skills/<name> and links to Agent
# Record submission timestamp after success
```

### 3. Validate 402 challenge flow
```bash
curl -s -X POST https://api.cortexcloud.org/v1/optimize -H 'content-type: application/json' -d '{"problem":{"n":4,"data":{"linear":[1,-2,3,-4],"quadratic":{"0,1":-1.5}},"problem_type":"qubo"}}' -w '\nHTTP:%{http_code}'
# Expect: HTTP 402 with x402 challenge, USDC on Base, correct atomic amount
```

### 4. Discovery surface verification
```bash
for path in /.well-known/x402.json /.well-known/bazaar /llms.txt /openapi.json /health /v1/capabilities; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "https://api.cortexcloud.org$path")
  echo "$code $path"
done
# All should be 200
```

### 5. Already indexed
`api.cortexcloud.org` (verified 2026-09-20 14:20 UTC).

## X / community activity (gated)
- `x_search` (xAI/Grok) was CREDIT-BLOCKED this session — cannot run batched
  X search or post. Free alternative: web search for high-intent communities
  (r/x402, x402 Discord/Coinbase-Cloudflare Foundation, DEV.to x402 thread,
  2s.io learn/x402, x402 tutorials). Posting to Reddit/Discord/Dev.to needs
  accounts → approval.
- Rule: no mass-reply/follow/DM, no ads, no paid placement. Preserve any X API
  budget. Draft replies in `docs/x_engagement_drafts.md`, hold for approval.

## Pitfalls
- **Stale deploy ≠ directory problem.** If a submitted page shows old content,
  it's usually prod git divergence (see cortexcloud-api-dev "Durable drift
  guard"), not the directory. Fix the drift, not the submission.
- **Name collision.** "CortexCloud" is generic + collides with Palo Alto.
  Always position as "CortexCloud — agent-native x402 APIs at api.cortexcloud.org".
- **Don't count internal.** The metrics filter must exclude the test-buyer
  payer or the milestone reads falsely satisfied.
- **Bash quoting in nested SSH.** Commands like `grep -E "MAX_|DEMO_" /opt/.env`
  through nested `ssh pct exec -- bash -c "..."` hit EOF errors from unmatched
  quotes. Work around by piping the script via `cat > /tmp/x.py` from the
  local host, then `pct exec -- /venv/bin/python /tmp/x.py`.
- **`agentcash register` hangs — never use it.** `agentcash add <url>` is the
  correct index/register command. `register` blocks indefinitely on the
  interactive prompt even with `-y`.

## References
- `references/directory-submissions.md` — per-directory payloads + live status
  (mirror of `CortexCloudAPI/docs/discovery_submissions.md`).
