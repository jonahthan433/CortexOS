# telegram-user-session-reading

# Telegram User-Session Reading

Class of work: the user wants Hermes to **read or monitor the content of a
Telegram channel/chat** — to summarize it, feed it into a briefing, or build
something (e.g. a trading bot) off its posts. This is READ ingestion of a
source the user typically does NOT own.

## READ THIS FIRST: try the public scraper before ANY user-session login

If the target channel is **public** (its `https://t.me/<channel>` page loads,
HTTP 200), you do NOT need a user session at all. Telegram exposes public
channel history at `https://t.me/s/<channel>` with zero auth. Scrape that first
— it sidesteps the entire login-code nightmare below. Use
`scripts/scrape_channel.py <channel> [count]`. Only fall back to a telethon
user session if the channel is **private** and the user can see it in their app.

Quick public check: `curl -s -o /dev/null -w "%{http_code}" https://t.me/<channel>`
→ `200` means the web page exists; then confirm `https://t.me/s/<channel>`
returns post HTML (some channels disable the preview `/s/` view).

**Extracting structured fields from the scraped HTML is fiddly** — use
`html.parser` (not regex) for message text, and beware that "results table"
posts collapse to whitespace-tokens where the column order is header-row then
value-row (the day number is NOT next to the word "Day"). See
`references/parsing-public-channel-html.md` for the exact gotchas, working
regexes, and the append-only JSONL de-dupe pattern — it saves many iterations.

## ⚠️ Agent-relayed login codes are BURNED by Telegram — the user-session login usually CANNOT be completed by the agent

This is the single most important lesson. When a Telegram login code is **pasted
into any chat / shared with anyone** — including legitimately handing it to
Hermes — Telegram's anti-phishing **invalidates that code and blocks the login**.
The user receives: *"The code was entered correctly, but sign in was not
allowed, because this code was previously shared by your account."* and a
security notice about a blocked login attempt.

Consequences:
- **The file-polling `login_wait.py` trick does NOT save you.** It fixes the
  timing/EOF problems, but the code is still one the user relayed through chat,
  so Telegram still burns it. Every relayed code fails the same way.
- This is a HARD Telegram security rule, not a bug to script around. A code is
  only honored when typed **directly into the official Telegram client** on a
  device the user controls — never one echoed into a chat with the agent.

Therefore, for agent-driven setup:
1. **Prefer the public scraper** (above) — no login needed. This is the
   realistic path for "monitor / build a bot from channel X".
2. If a genuine user session is required and the channel is private, the user
   must complete the login **themselves on a real device** (e.g. Telegram
   Desktop) — the agent cannot relay the code for them.
3. Do NOT burn multiple codes trying: each failed relay triggers another
   security block and risks a flood-wait lockout on the number.

When abandoning the user-session attempt, **clean up**: delete
`~/.hermes/telegram_user.env` and any partial `~/.hermes/*.session` (they are
credentials), and pivot to the public scraper or user-forwards-posts approach.

## Counting subscribers WITHOUT admin rights (channel growth metrics)

A bot is usually NOT admin on the user's own channel (posting can still work
via a user session or `hermes send`). Consequences + working path:

- **Bot API `getChat` omits `member_count` for non-admin bots.** If the bot is
  not admin, don't chase admin rights just for a number — scrape the public
  page instead.
- **Public `https://t.me/<username>` page HTML contains the exact subscriber
  count** (in `tgme_page_extra`), zero auth:
  ```python
  html = urllib.request.urlopen(urllib.request.Request(
      f"https://t.me/{username}", headers={"User-Agent": "Mozilla/5.0"}), timeout=15
  ).read().decode("utf-8", "replace")
  subs = int(re.search(r"([\d\s,.]+)\s*subscribers", html).group(1)
             .replace(" ", "").replace(",", "").replace(".", ""))
  ```
  Returns the real count even with 4 subscribers; no login, no token.
- **Metric pattern:** append daily `{"date": "…", "subs": N}` to an append-only
  JSONL (e.g. `~/.hermes/cortexcloud-posts/metrics.json`), print today's value
  with delta vs the previous day's entry. Baseline on first run is the honest
  counter — never fabricate counts.
- Per-post VIEW counts are NOT retrievable via bot API for historical posts
  (no admin, no user session) — say so instead of inventing engagement numbers.

## The critical distinction: bot token vs user session

Hermes' Telegram integration usually runs on a **bot token**
(`TELEGRAM_BOT_TOKEN` in `~/.hermes/.env`). A bot **cannot**:
- Join / subscribe to a channel on the user's behalf (there is no
  `joinChannel` API for bots).
- Read arbitrary channels' history. A bot only sees messages in chats where a
  channel admin explicitly added it, and even then history access is limited.

So "subscribe me to @X" or "read @X for me" is **not doable with the bot**.
Say so plainly, then offer the two real options:
1. **User joins the channel themselves** (5 seconds) — sufficient if Hermes
   doesn't need to process the content.
2. **Set up a telethon USER session** — required whenever Hermes must
   programmatically read/monitor the channel's content. This logs in AS the
   user's account (api_id/api_hash), which can read anything the user can see.

Flag the power/scope tradeoff: a user session acts as the user's account. Scope
your scripts to **read-only** (iter_messages) for monitoring use cases, and
store credentials root-only.

## Setup workflow (user session)

1. **User gets API credentials from my.telegram.org** (only they can — tied to
   their account, not the bot): https://my.telegram.org → API development tools
   → create an app → copy `api_id` (number) and `api_hash` (string).

2. **Store creds root-only** (per user rule: ask before storing sensitive
   data). Write to e.g. `~/.hermes/telegram_user.env`, then `chmod 600`:
   ```
   TELEGRAM_API_ID=<id>
   TELEGRAM_API_HASH=<hash>
   ```

3. **Install telethon + python-dotenv into a venv.** On Debian, `python3 -m
   venv` may produce a venv WITHOUT pip — use `uv`:
   ```bash
   uv pip install --python ~/.hermes/google-venv/bin/python telethon python-dotenv
   ```
   (Reuse an existing venv, e.g. the google-venv, or make a dedicated one.)

4. **One-time login creates the session file.** This needs the user's phone
   number + the login code Telegram sends them. Do NOT use interactive
   `input()`/PTY — it fails with EOF and the code expires during the relay.
   Use the file-polling login (`scripts/login_wait.py`): run it in the
   background, tell the user to check Telegram, then write their code to
   `/tmp/tg_code` the instant they send it. See the login pitfalls below.

5. **Read-only reader** uses `client.iter_messages(target, limit=N)` where
   `target` is `@username` or a `https://t.me/...` URL. The session persists
   after first login; subsequent reads are non-interactive.

Ready-to-use starter files:
- `scripts/scrape_channel.py` — PUBLIC channel scraper (no auth). Try this
  FIRST; uses `t.me/s/<channel>` + stdlib html.parser. Avoids the login-code
  problem entirely.
- `scripts/read_channel.py` — read-only recent-message fetcher.
- `scripts/login_wait.py` — RECOMMENDED session creator: no PTY/stdin, polls
  `/tmp/tg_code` for the code (and `/tmp/tg_pw` for a 2FA password). Set phone
  via `TG_PHONE` env.
- `scripts/login.py` — legacy interactive creator; prone to the EOF/expiry
  pitfalls below. Prefer `login_wait.py`.

## Pitfalls

- **Promising a bot can subscribe/read a channel.** It cannot. Check
  `~/.hermes/.env` for `TELEGRAM_BOT_TOKEN` vs user-session creds first, and set
  expectations before attempting anything.
- **Interactive login EOF — PTY is NOT reliable.** telethon's default
  `input('Please enter your phone...')` throws `EOFError: EOF when reading a
  line` via heredoc/non-PTY exec. But `pty=true` did NOT fix it either in
  practice — the terminal's PTY did not feed typed stdin to `input()`, so it
  still hit EOF. **Do not rely on PTY interactive input.** Use the
  file-polling pattern instead (see `scripts/login_wait.py`): the script calls
  `send_code_request`, captures `phone_code_hash`, then polls a temp file (e.g.
  `/tmp/tg_code`) for up to ~120s; the agent writes the code to that file the
  instant the user sends it, and sign-in happens in the SAME process with the
  fresh hash. Zero typing gap, zero PTY dependency.
- **Login codes expire fast AND each request invalidates the previous.** The
  #1 login failure here was `PhoneCodeExpiredError` / `PhoneCodeInvalidError`
  from a multi-turn relay: agent requests code in turn 1, user reads SMS and
  replies in turn 3, agent signs in in turn 5 — by then the ~1-2 min window has
  passed. Worse, every extra `send_code_request` you fire generates a NEW code
  and INVALIDATES the prior one, so probing "did the code send?" repeatedly
  burns codes. Rules: (a) request the code and sign in within ONE process
  (send_code_request → sign_in with that run's `phone_code_hash`); (b) never
  call `send_code_request` again "to check" — it kills the code the user is
  about to give you; (c) use the file-polling script so the sign-in fires the
  moment the code arrives, not a turn later.
- **`sign_in` needs `phone_code_hash`.** A one-shot script that only does
  `sign_in(phone, code)` fails with `ValueError: You also need to provide a
  phone_code_hash`. Capture the hash from the `send_code_request` return value
  in the same run and pass it: `sign_in(phone, code, phone_code_hash=r.phone_code_hash)`.
- **2FA fallback.** If the account has two-step verification, `sign_in` after
  the code raises a password error — catch it and call
  `sign_in(password=<pw>)`. Have the login script accept an optional password
  arg so you don't need a second round-trip.
- **Missing python-dotenv.** If the login/read script uses `load_dotenv`,
  install `python-dotenv` in the SAME interpreter. `No module named 'dotenv'`
  means it landed in a different env — install with
  `uv pip install --python <venv>/bin/python python-dotenv`.
- **Session file is a credential.** `~/.hermes/*.session` grants account
  access — keep it root-only, never commit or move it off-host.
