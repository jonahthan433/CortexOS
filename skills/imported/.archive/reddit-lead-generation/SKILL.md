# reddit-lead-generation

# Reddit Lead Generation

Find and warm up potential buyers by searching Reddit for people with active unsolved problems that your product solves.

## Trigger conditions
- "find leads on Reddit"
- "Reddit growth", "Reddit outreach"
- "warm up buyers", "intent signals"
- Any task asking to search Reddit for potential customers

## The pipeline (3 steps)

### Step 1 — Intent signal search

Search Reddit for people actively asking about your problem space. Use **web_search with `site:reddit.com`** — the free Reddit JSON API is blocked on datacenter IPs (403) and no paid Reddit access API was functional as of 2026-08.

Query template (adapt to your product):
```
site:reddit.com "<your-keyword>" "looking for" OR "need help" OR "recommend" OR "any recommendations"
```

For each query, extract:
- Username (`u/...`)
- Subreddit (`r/...`)
- Post title and snippet
- Post URL
- Intent signal: are they actively looking for a solution?

Good intent signals:
- "do you have any recommendations for optimizers"
- "CVXPY output is optimally inaccurate"
- "no solver can give an optimal solution"
- "looking for open source Python code for"

Low intent (skip these):
- Tutorials, educational posts, "how does X work"
- Posts from 3+ years ago
- Posts with zero comments (likely abandoned)

### Step 2 — Lead qualification

Score each signal:
- **Direct ask** (explicitly asking for a tool/solver) → highest priority
- **Unsolved problem** (describes a broken approach) → high priority
- **Builder** (building something adjacent, potential integration) → medium
- **Educational** (learning the space, may convert later) → low

### Step 3 — Outreach drafting

For each qualified lead, draft a Reddit reply that:
- Acknowledges their specific problem (proves you read it)
- Explains how your product solves it in 1-2 sentences
- Includes ONE concrete detail (price, capability, live example)
- Links to live proof (API docs, examples endpoint) — NOT a landing page
- Sounds like a human helping, not a bot marketing

Rules:
- **Do not post from datacenter IPs** — Reddit blocks them. Draft for manual posting.
- Never spam, never mass-reply, never use templates verbatim.
- Only claim what's verifiable from your live product.
- One reply per thread maximum.

## Platform constraints (as of 2026-08)

| Path | Status |
|------|--------|
| Free Reddit JSON API | Blocked (403 from datacenter IPs) |
| stableenrich.dev (paid) | Non-functional (empty response) |
| agentcash Reddit endpoint | Non-functional (timeout) |
| Reddit OAuth (script app) | Blocked by policy gate on new app creation |
| **web_search site:reddit.com** | **Works** (rate-limited ~3 calls/minute) |
| **Manual posting** (user's device) | **Works** (highest deliverability) |

## Reference: CortexCloud outreach examples (2026-08-11)

Three real replies posted to Reddit threads:

1. **r/optimization** — user asking for solver recommendations → replied with CortexCloud as an answer (classical/hybrid/quantum, pay-per-call, live examples link)

2. **r/datascience** — user with broken CVXPY QUBO → replied with "our exact solver fixes that at $0.05" angle

3. **r/algotrading** — Riskfolio-Lib author → replied with integration/rev-share idea

All three: acknowledged the user's actual post, 3-4 sentences, link to api.cortexcloud.org/v1/examples as proof.

## Pitfalls

- The specified APIs (stableenrich.dev, stable-deepline.dev, x402.agentutility.ai) were all dead in testing — pivot to web_search immediately, don't retry.
- Reddit blocks datacenter/VPS IPs — plan for manual posting by the user.
- Rate-limit web_search calls: space them out, 3 calls/minute max.
- Reddit OAuth app creation is blocked by a policy gate that requires human acknowledgement — can't automate.
