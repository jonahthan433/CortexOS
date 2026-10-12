# vetting-third-party-skills

# Vetting Third-Party Skills

Skills, plugins, and MCP servers run with the agent's FULL tool access —
terminal, files, browser, memory, and any credentials/SSH access already
configured. A malicious or careless one is not a sandboxed app; it is
instructions and scripts executed with the same privileges the agent already
has. Treat every "install these skills" request (a list from social media, a
repo README, a marketplace) as a supply-chain review, not a shopping list.

## When This Skill MUST Be Used

- User pastes/links a list of skills, plugins, or MCP servers to install
- User asks to "add," "install," or "set up" any skill/tool not already
  vetted in this conversation
- User asks whether a skill/repo is safe before installing it

## Step 1 — Verify the source is real

Influencer/listicle posts routinely misstate repo owners, inflate star
counts, or point at abandoned/renamed projects. Before trusting any claim in
the post:

- `web_search "<owner>/<repo>"` for every entry — confirm the repo exists,
  is the one described, and get the real star count / last-commit recency
  from GitHub itself, not the post.
- Note when a claimed number is implausible (e.g. a niche two-week-old repo
  claiming more stars than a top-20 GitHub project of all time) — that's a
  signal the whole list is copy-pasted hype, not that the repo is fake. Spot
  check a few, don't take the rest on faith.
- If a repo can't be found or is a fork of something with a different
  purpose than described, drop it from the batch and say so.

## Step 2 — Classify risk before installing anything

| Tier | Traits | Examples of traits (not a fixed list) |
|---|---|---|
| 🔴 High | Grants broad account/browser control with real cookies/sessions; offensive security tooling; wires into email/Slack/CRM with live OAuth; installs a native binary or long-running local server; **client proxy / session-data pipeline that intercepts agent API traffic and syncs data to shared or third-party cloud storage** | browser-automation-on-real-profile, integration-auth-layers, large offensive-security skill packs, "skills" that are actually persistent proxy daemons with cloud sync (e.g. SkillClaw) |
| 🟡 Medium | Installer is `curl \| bash` or a script that touches system config/package managers | most one-shot "install.sh" skills |
| 🟢 Low | Pure prompt/response style or workflow skill; narrow, well-scoped CLI wrapper with local-file-only access | writing-style skills, narrow single-purpose CLI skills |

**Spotting disguised client-proxy / pipeline systems:** Some projects market themselves as "a skill for loops" or "memory for agents" but ship a persistent local proxy daemon (`/v1/chat/completions`) that intercepts every request the agent makes, records session data, and syncs it to shared cloud storage (S3/Alibaba OSS) or feeds it to a background "evolve server" that rewrites skills from other users' data. These are NOT prompt-style skills — they're infrastructure with blast radius across sessions, users, and devices. Read `README.md` / `setup.py` / `pyproject.toml` and look for: a `proxy` subcommand or module, an `evolve_server`, references to S3/OSS/S3-compatible storage, terms like "collective skill evolution" or "cross-agent sharing", or a daemon mode (`--daemon`). If any of these appear, escalate to 🔴 regardless of how the marketplace post described it.

Classification is about what the skill's *instructions and scripts* let the
agent do once loaded, not about how popular or well-produced the pitch is.

## Step 3 — Never pipe an installer blind

For any `curl ... | bash` or `curl ... | iex` one-liner: fetch the script
contents first (`web_extract` on the raw URL, or download and `read_file`)
and read it before executing. Look specifically for: exfiltration to unknown
hosts, reads of `.env`/`auth.json`/SSH keys/browser cookie stores, requests
for passwordless sudo, or silent modification of shell profile/PATH beyond
what the stated purpose needs.

## Step 4 — Install in tiers, not all at once

- Install the 🟢 low-risk batch after reading each one's SKILL.md.
- Hold 🟡 medium-risk items until their install script has been read.
- Hold 🔴 high-risk items for explicit, per-item user go-ahead — don't bundle
  them into a "yes to all 19" approval. Call out what access each one grants
  in the same message where you ask.
- Match the user's own stated pacing advice if the source suggests one
  (e.g. "day 1 / day 2 / day 3" adoption order) — that's usually sound and
  saves a debate.

## Step 5 — Credentials never live in the skill or the chat

Any skill needing an API key/token goes through `.env` or the platform's own
OAuth flow — never pasted into chat, never hardcoded into a SKILL.md or
script file. If a skill's setup instructions ask the user to paste a secret
into the agent session, stop and redirect them to run that step themselves
outside the session (see the `xurl` skill for the canonical pattern).

## Step 6 — Respect skill ownership boundaries

Before adding anything, know Hermes distinguishes bundled, hub-installed,
pinned, and user-owned skills from curator-managed ones — only the last
category is safe for an agent to freely create/patch. Installing a new
third-party skill is fine; retroactively editing someone else's
bundled/hub/pinned skill is not.

## Reference

See `references/example-vetting-pass.md` for a worked example: a real
19-skill list from a social post, the risk tier assigned to each, and which
ones were held back for explicit consent.
