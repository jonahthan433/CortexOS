# cortexcloud-growth

# CortexCloud Lead Generation

Find and warm up potential buyers for CortexCloud's pay-per-call QUBO/Ising optimization API (classical $0.05, hybrid $0.10, quantum $1.503 — USDC on Base via x402, no API keys).

## Core Pipeline (3 steps)

### Step 1 — Reddit Intent Signals

Search Reddit for users with active unsolved optimization problems.

**Primary method (free, works):**
```
web_search query="site:reddit.com <topic> problem OR solver OR recommend OR \"looking for\""
```

**Topics to rotate:**
- `QUBO optimization`
- `quadratic optimization python`
- `portfolio optimization API`
- `quantum computing optimization`
- `vehicle routing problem solver`

**Fallback (if IP not blocked):** Reddit JSON API — `https://www.reddit.com/search.json?q=<terms>&limit=10&sort=relevance&t=year`. Datacenter IPs get 403; residential IPs work.

**Do NOT use:**
- `stableenrich.dev/api/reddit/search` — returned empty in all tests (Aug 2026)
- `npx agentcash@latest fetch https://stableenrich.dev/...` — timed out (Aug 2026)

**For each result, extract:** username, subreddit, post title, score, permalink, and whether they describe an active unsolved problem.

### Step 2 — Lead Discovery

Find decision-makers and companies in the x402 / agent-payments / optimization space.

**Method:**
```
web_search query="\"AI agent\" OR x402 OR \"pay per call\" optimization API company OR startup"
web_search query="site:reddit.com r/optimization OR r/quant OR r/algotrading solver recommendation"
```

**Target roles:** AI Engineer, ML Engineer, Quantitative Analyst, Operations Research Engineer.
**Target signals:** Companies with "AI", "agent", "quant", or "logistics" in name; companies already using x402 (Apify, Nevermined, AWS Bedrock AgentCore).

**Do NOT use:** `stable-deepline.dev/api/contacts/by-role` — non-functional (Aug 2026).

### Step 3 — Outreach Drafting

Draft personalized cold outreach. **All drafts must be approved by Jonathan before sending.**

**Verified claims only — from the live API:**
- Pay-per-call, no API keys, USDC on Base via x402
- Prices: classical $0.05, hybrid $0.10, quantum $1.503 (from /v1/examples)
- Endpoints: /v1/optimize, /v1/estimate (free), /v1/examples, /v1/backends, /v1/capabilities
- Agent discovery: .well-known/agentsearch.txt, llms.txt, MCP endpoint
- Docs: https://api.cortexcloud.org/docs
- Proof link: https://api.cortexcloud.org/v1/examples

**NEVER claim:** customers, revenue, partnerships, benchmarks, quantum advantage, performance comparisons — none are verifiable.

**Do NOT use:** `x402.agentutility.ai/outreach-pack` — non-functional (Aug 2026).

## X Posting Rules (@cortexcloud1, corrected 2026-08-11)

### Voice
- Human, conversational. Sound like an engineer who built something useful. Not marketing.
- Never start with "Introducing", "We're excited", or "CortexCloud offers".
- One idea per post. Short sentences. Study Vercel/Railway for tone reference.

### Format
- 2 posts/day: 09:00 (technical), 19:00 (build-in-public) EAT
- **No links in post body.** Put `api.cortexcloud.org` in a reply tweet.
- ASCII only — no emoji, no hashtags (emoji/long = 403 on this account).
- **No prices in post body** unless genuinely relevant to the story.

### Content pillars
P1 Product demos, P2 Optimization examples, P3 Technical insights, P4 Verified milestones, P5 Finance, P6 Quantum (honest), P7 Build-in-public.
P1–P4: auto-eligible. P5–P7 + financial/partnership claims: **Jonathan sign-off required.**

### Good posts (examples that survived)
- "the exact solver is boring in the best way. n ≤ 18, always the right answer, no drama."
- "spent the afternoon reading about x402 settlement failures. most of them boil down to the agent not checking the price before calling."

### Bad posts (these were deleted)
- "CortexCloud offers pay-per-call QUBO/Ising optimization with classical, hybrid, and quantum backends."
- Any post with a URL in the main body instead of a reply.

### Account constraints
- @cortexcloud1, xurl app 'cortexcloud', ~/.xurl/auth.yml (0600)
- Replies/quotes blocked (403). Strategy: original posts + reposts only.
- Bio: manual update only (xurl can't PATCH v2 profile; v1.1 update_profile failed).
- Deletion: `xurl delete <tweet_id>` works reliably via v2 API.

## Telegram Channel Operations

- Channel: -1003885217326, t.me/cortexcloudonline. Broad AI/tech hub, NOT API-only.
- 3 posts/day (09/14/19 EAT), sent via `hermes send --to telegram:-1003885217326 --file <path>`
- Log: ~/.hermes/cortexcloud-posts/posts.jsonl
- Subs: scrape t.me/cortexcloudonline (public page, no auth), script ~/.hermes/scripts/cc_subs.py
- Views: not retrievable (bot is NOT channel admin). Never fabricate.

### Supporting crons
| Job ID | Schedule | Purpose |
|--------|----------|---------|
| 32486de1b6bd | 07:00 daily | Subs, posts, experiments, next action |
| c485e5862ffc | Sun 18:00 | Pace-to-100K, experiment verdicts |
| 99f8bc0243ca | 10:00 daily | Cross-promo candidates (list only) |
| be1c44f4ac84 | 13:00 daily | X monitor (read-only, drafts to file) |

## CT105 Testing Workflow

CortexCloud API public endpoint returns 403 (Cloudflare). Test via localhost inside CT105:

```bash
# Push + run a test script
scp -i ~/.ssh/hermes_ninyi /tmp/test.py root@192.168.1.11:/tmp/
ssh -i ~/.ssh/hermes_ninyi root@192.168.1.11 \
  "pct push 105 /tmp/test.py /tmp/test.py && pct exec 105 -- python3 /tmp/test.py"

# Quick one-liner estimate
ssh -i ~/.ssh/hermes_ninyi root@192.168.1.11 \
  "pct exec 105 -- curl -s http://127.0.0.1:8000/v1/estimate -X POST \
   -H 'Content-Type: application/json' -d '{\"problem_type\":\"qubo\",\"n\":6,\"data\":{\"linear\":[-1,1],\"quadratic\":{\"0,1\":0.5}}}'"
```

### Deploy changes to CT105
1. scp modified file to ninyi:/tmp/
2. `pct push 105 /tmp/file.py /opt/CortexCloudAPI/<path>/file.py`
3. `pct exec 105 -- python3 -m py_compile <path>` (verify syntax)
4. `pct exec 105 -- systemctl restart cortexcloud.service`
5. `pct exec 105 -- curl -s http://127.0.0.1:8000/health` (confirm alive)

### Git (PAT-authed inside CT105)
```bash
ssh ... "pct exec 105 -- bash -c 'cd /opt/CortexCloudAPI && git add -A && git commit -m \"msg\" && git push'"
```

## Router Pitfall (2026-08-11)

`app/solvers/quantum/router.py` sort key used `_pref` (aws_braket provider preference) before `_cost`,
causing AWS Braket quantum backends ($1.50) to outrank simulated annealing ($0.05) when benchmark
evidence existed. The `_pref` field was designed for intra-quantum ranking but applied across all modes.
**Fix:** Removed `c["_pref"],` from the sort tuple. Cost is primary sort after brute-force-when-n≤18.

## Reddit OAuth Setup (for posting replies)

See `references/reddit-oauth-setup.md` for the step-by-step guide to create a Reddit script app and wire up OAuth for headless posting. Status: pending Jonathan creating the app and sharing client ID/secret.

## Pitfalls

- **Reddit blocks datacenter IPs** — web_search is the reliable path; direct API only works from residential IPs
- **The specified x402 endpoints (stableenrich.dev, stable-deepline.dev, x402.agentutility.ai) are non-functional** — don't waste time retrying them; use web_search and manual drafting
- **Brave Search rate-limits** — space web_search calls 3-5s apart; retry 429s after 30s
- **X account has no pricing in bio/posts** per Jonathan's directive — outreach emails OK to include pricing but X posts do not
- **All outreach is approval-gated** — drafts only until Jonathan says "send"

## Report Format

Compile findings into `~/.hermes/sales/leads-YYYY-MM-DD.md`:
- Step 1 table: username, subreddit, post link, intent signal
- Step 2 list: company, relevance, signal source
- Step 3: full email drafts (ready to copy-paste after approval)
- Top 3 leads ranked by buying intent

## External Payment Monitoring

Track unique external payers (excluding test wallet `0xCed8a9ff73427302cD0F0F95892EbfC2Ac83374A`). Query:
```bash
ssh pve "pct exec 105 -- docker exec cortexcloud-db psql -U postgres -d cortexcloud -t -c \"SELECT DISTINCT payer FROM x402_payments WHERE payer != '0xCed8a9ff73427302cD0F0F95892EbfC2Ac83374A';\""
```

Monitor: unique external payers, total external calls, endpoints used, payment failures, repeat usage. Daily cron recommended at 07:00 EAT.

## Directory Submission Status

| Directory | Requirement | Status |
|---|---|---|
| x402-list.com | Valid HTTP 402 on probed endpoints | ✅ Listed |
| MCP Registry | GitHub PR to modelcontextprotocol/registry | ✅ Listed |
| awesome-mcp-servers | PR to punkpeye/awesome-mcp-servers | ✅ Listed |
| x402-index | Open issue on x402-index/x402-discovery-index | ✅ Issue #42 opened (2026-09-07) |
| Glama | Add `glama.json` to repo root | ✅ Added 2026-09-10 (auto-indexes ~24h) |
| 402.ad | $0.10 x402 submission + Cloudflare WAF | ❌ WAF blocks headless; manual browser needed |
| x402.direct | Search is $0.001 x402 paid; not in free browse | ❌ Not listed |
| Smithery | URL publishing at smithery.ai/new | ❌ Requires sign-in; no public API |
| agentcash.dev | No public submission API | ❌ |
| mcpfind.org | Web-only, no public API | ❌ |
| mcp.directory | Web-only, no public API | ❌ |

### Glama Submission Recipe
```bash
# Create glama.json in repo root
cat > glama.json << 'EOF'
{"maintainers": ["jonahthan433"]}
EOF

git add glama.json
git commit -m "Add glama.json for Glama registry indexing"
git push
# Auto-indexes within ~24h, appears at glama.ai/mcp/servers
```

### x402-index submission format
```bash
gh issue create --repo x402-index/x402-discovery-index \
  --title "List: CortexCloud — agent-native pay-per-call platform" \
  --body "## Endpoint URL
- https://api.cortexcloud.org

## x402 payment address
0x5a0353bc9c75b893a9b5735d3e79f1bd988ea143 (USDC on Base, eip155:8453)

## Description
Agent-native pay-per-call API platform — AI, Research, Data, Optimization, Automation.

## Categories
- AI, Research, Data, Optimization, Automation

## Pricing
- Free: /v1/capabilities, /v1/backends, /v1/estimate
- Paid: from $0.004 to $1.503"
```

See `references/x402-submission-format.md` for full details.

### Smithery (requires sign-in — no automated path)
1. Go to smithery.ai/new
2. Enter server URL: https://api.cortexcloud.org/mcp
3. Complete publishing flow (requires account + OAuth)
4. Server becomes discoverable via Smithery marketplace

## x402 Exact Payment Flow (Testing)

The middleware supports two payment paths:
- **x402 exact** (Base mainnet, chain 8453, EIP-712 TransferWithAuthorization) — buyer signs only, no gas needed
- **MPP/Tempo** (chain 4217/42431, Tempo USDC) — requires Tempo ETH for gas, NOT free to test

**Disable MPP to force x402 exact:** Set `MPP_ENABLED=false` in `/etc/cortexcloud/cortexcloud.env` and restart cortexcloud.service.

**Challenge location:** POST unsigned → HTTP 402 → `payment-required` header (base64-encoded challenge). NOT `www-authenticate`.

**Full flow with code examples:** See `references/x402-payment-flow.md`

**Test wallet (DO NOT expose key):**
- Address: 0xCed8a9ff73427302cD0F0F95892EbfC2Ac83374A
- Key file: `/root/bzcheck/test_buyer.key` (mode 600, root-only)
- Balance: ~$1.74 USDC on Base mainnet (enough for $0.05 classical optimize)

## Product Development Gate

**Do NOT build new product features unless a real external user reveals a concrete, repeated need.** The current phase is distribution, not feature development. Tier 2, new APIs, new categories, and new features are frozen until external users demonstrate demand. When in doubt, ask: "Would an external user pay for this?" If the answer is not a clear yes from observed behavior, skip it.

## Pitfalls (new)

- **Cloudflare WAF blocks headless requests** — External `curl`/`python` to `api.cortexcloud.org` returns 403 without proper `User-Agent` and `Accept` headers. Always send `User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36` and `Accept: application/json`. For submissions to other x402 sites (402.ad), Cloudflare WAF may block even with headers — use browser or manual submission.
- **MPP/Tempo requires gas tokens** — The MPP payment path uses Tempo chain (4217 mainnet, 42431 testnet) which requires Tempo ETH for gas. The test wallet has 0 Tempo ETH. Use x402 exact path for testing by setting `MPP_ENABLED=false`.
- **Challenge is in payment-required header, not www-authenticate** — x402 v2 spec delivers the challenge via base64-encoded `payment-required` response header. Parsing `www-authenticate` is for v1/MPP. Decode the header with base64, fix padding if needed.
- **Do not retry paid submissions blindly** — 402.ad submission consumes the $0.10 even if Cloudflare WAF blocks. Manual browser submission preferred over headless retry.
- **Smithery has no public API** — Submission requires browser sign-in at smithery.ai/new. Cannot be automated via API.
- **Glama auto-indexes from GitHub** — Adding `glama.json` to repo root is sufficient; no manual submission needed. Appears at glama.ai/mcp/servers within ~24h.
- **x402-index uses GitHub issues** — Submit via `gh issue create` with structured body (endpoint URL, payment address, description, categories, pricing). See submission format above.
- **Ponytail mode** — User wants concise, code-first responses. "Code first. Then at most three short lines: what was skipped, when to add it." No essays, no feature tours, no design notes.
