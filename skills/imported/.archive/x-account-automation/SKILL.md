# x-account-automation

# X Account Automation (xurl + OAuth)

Use when setting up or operating an X/Twitter account programmatically: auth
flows, posting, profile edits, credential storage. The `xurl` skill covers
command reference; this covers **setup/ops knowledge learned in production**.

## Auth setup (one-time, user does the secret parts)

1. X Developer Program (console.x.com) → app → **"Web App, Automated App or Bot"** (Native App breaks OAuth: `unauthorized_client`)
2. **Callback URI must be exactly `http://localhost:8080/callback`** (HTTP, not HTTPS). The console accepts `https://localhost:8080/callback` too, but xurl's authorize URL sends `http://` → mismatch → "Something went wrong" on the authorize page. This is the #1 setup failure.
3. `xurl auth apps add <name> --client-id ... --client-secret ...` (user pastes creds; never handle them yourself beyond this)
4. **Headless OAuth2** (no browser on server): run `xurl auth oauth2 --app <name> --headless <handle>` in a **PTY background process** (it reads the pasted code from stdin; a plain foreground call EOFs instantly). Send the printed URL to the user; they authorize on any device, copy the failed `localhost:8080` redirect URL from the address bar (code is in it), paste it back. `xurl auth default <name>`.
5. **Importing existing tokens directly**: `~/.xurl/auth.yml` schema (600 perms) is
   `apps: {<name>: {client_id, client_secret, redirect_uri, default_user, oauth1_token: {type: oauth1, oauth1: {access_token, token_secret, consumer_key, consumer_secret}}, oauth2_tokens: {<handle>: {type: oauth2, oauth2: {access_token, refresh_token, expiration_time}}}}, default_app: <name>}`.
   Writing this file bypasses the browser flow entirely. Never READ it back into context.
6. **Never regenerate client id/secret after wiring** — it invalidates tokens; re-do the whole flow. Stop the user from regenerating "to try again".

## New-account content filters (posting)

Brand-new accounts get aggressive anti-spam filtering. Symptoms → cause:
- HTTP **403 "You are not permitted to perform this action"** on posts containing **emoji (🚀), arrows (→), en-dashes (–)** → X blocks non-ASCII/symbol-heavy text from new accounts. Pure ASCII passes.
- Same 403 on posts **>280 chars** (API returns 403, not 400, for over-length). On day-one accounts even **~278 chars can fail while ~180 passes** — keep well under the limit.
- Plain posts work; bare domains (`api.cortexcloud.org`) work; full `https://` URLs are **flaky on day-one accounts** (403 sometimes, pass other times — observed same session) — prefer the bare domain until the account matures.
- Repeated near-identical posts from a new account also get blocked. Verify with a tiny ASCII test post before the real one; delete tests after.
- **Replies/quotes blocked on young accounts**: `xurl reply`/`quote` → `"You can only reply to or quote posts where you are mentioned or are the author"` even when posting works. Likes/follows still work as the engagement signal until the restriction lifts (age/phone verification). Plan conversation-lane content accordingly; don't retry replies repeatedly.
- **Pinning fails**: `PUT /2/users/:id/pinned_tweets` errors on young accounts. Put the link in the profile website field instead (UTM'd).
- **Removing a stray RT**: `xurl unrepost <rt_id>` can silently no-op. Fetch the source id (`/2/tweets/<rt_id>?expansions=referenced_tweets.id` → `referenced_tweets[].type == retweeted`) then `xurl -X DELETE "/2/users/<me>/retweets/<source_id>"`.
- `xurl search` returns EMPTY results for multi-word queries on young accounts; single-word/short queries work.
- Filters relax as the account ages; phone verification speeds it up.

## 2026 X algorithm — content strategy (verified research, 2026-08)

- **Link posts get ~zero reach** (Buffer: median 0 engagement for link posts since Mar 2025; 1,700% reach gain removing the link). Post content LINK-FREE; links live in the UTM'd profile website, pinned post, or replies.
- Distribution is engagement-rate driven with ~6h visibility half-life: engineer early replies/likes, post when the audience is active.
- SimClusters topic clustering → stay in ONE topical lane (agent-native infra, etc.). Follower count barely affects per-post visibility.
- Economics align: plain post $0.015 (and better reach) vs $0.20 URL post.
- **Daily-publishing pattern that works**: cron with a self-contained prompt (topic bank, ASCII ≤185 rules, dedup against a state file), `deliver=local`, logs each post to `~/.hermes/scripts/.x_posts.jsonl` (id/text/purpose/liked ids); a weekly report cron reads the same file + UTM-sourced visitors (referrals table) + paid metrics to close the loop. Never fabricate metrics; attribute via `?utm_source=x`.

## Posting cost (pay-per-use, 2026)

Plain post $0.015 · **post with URL $0.20** · read/search $0.005. $5 ≈ 25 URL posts ≈ 3-4 months of a light channel.

## Profile editing — OAuth1 required

- `PATCH /2/users/:id` rejects OAuth2 tokens: **"You are not permitted to use OAuth2 on this endpoint"** → profile edits need **OAuth 1.0a** (Keys and tokens → OAuth 1.0 section: consumer key/secret + access token/secret).
- v1.1 endpoints work with OAuth1: `POST /1.1/account/update_profile.json` (form body), `update_profile_image.json` (base64 `image`), `update_profile_banner.json`.
- **xurl raw mode cannot sign v1.1 form-encoded bodies** (401 code 32 "Could not authenticate"). JSON bodies pass auth but are ignored by v1.1. Use the deployed helper `~/.local/bin/xprofile` (reads creds from `~/.xurl/auth.yml`, never inline; run with the hermes venv python): subcommands `profile '{"name":..., "url":...}'`, `avatar <img>`, `show`. There is NO bare `url` subcommand — set the website via `profile '{"url": "https://…?utm_source=x"}'` (UTM the profile URL for attribution). Fallback: a `requests-oauthlib` script (`OAuth1(ck, cs, at, ats)`, `requests.post(data=fields)`). v1.1 `users/show.json` reads may 403; verify via v2 `/2/users/me?user.fields=...`.

## Account audit (read-only, works on young accounts)

- Profile: `xurl "/2/users/me?user.fields=description,public_metrics,created_at,location,url,pinned_tweet_id,protected"` → bio, follower/like counts, pinned id (absent = no pin).
- Own posts: `xurl "/2/users/<id>/tweets?max_results=20&tweet.fields=created_at,public_metrics"` (max_results must be 10–100; 5 → 400 invalid param).
- **search/recent tier quirks**: some queries work (`x402`), others 401 (`QUBO OR "quantum optimization"`, `from:<user>`, or when the query has more complex operators) — Basic tier grants a subset; rotate/fall back to web research instead of retrying. Output may contain MULTIPLE JSON objects — naive `json.loads` fails with "Extra data"; parse the first object or read raw.

## Growth-strategy staging (approval-gated, verified-claims only)

When asked to build an account growth strategy: (1) audit account + verify every claim against live sources (API/docs/public results) BEFORE writing strategy; (2) position around the verified fact set only — explicitly list what is NOT claimable (private repos, no customers/revenue/benchmarks); (3) pause existing auto-posting crons during the approval window; (4) stand up a READ-ONLY monitor cron (search: query rotation, log drafts to a file, deliver=local) so conversation scanning runs but nothing publishes; (5) deliver strategy/bio/pinned-post/first-batch as a pack and gate every publish on explicit approval; after approval, only routine low-risk technical updates may auto-post. Never fabricate metrics or attribution.

## Pitfalls

- `xurl timeline` = HOME feed (accounts you follow), not the profile. List own posts via raw `/2/users/<id>/tweets`; delete only own posts (deleting others' returns 403 harmlessly, but don't bulk-delete blindly from the home feed).
- `xurl delete` on your own test posts is fine; confirm what you're deleting first.
