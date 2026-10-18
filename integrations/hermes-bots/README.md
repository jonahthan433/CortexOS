# Hermes multi-bot profiles (bots with different names)

Run several Telegram bots ("different names") without adding a second poller:
each bot is a **Hermes profile** under `~/.hermes/profiles/<name>/` with its own
config, personality, model, and **its own BotFather token**, while the one
running host gateway multiplexes all served profiles.

## Two ways to make a bot — pick by where it should live

| | Dashboard bot (CortexOS) | Telegram bot (Hermes profile) |
|---|---|---|
| Where you create it | HUD → Settings → **CREATE A BOT** | `hermes profile create <name> --clone` |
| Identity | display name + persona; one shared gateway identity | its own BotFather token, own Telegram account |
| How you use it | HUD agent selector, `/cx assign <id>` in Telegram | message the bot directly in Telegram |
| Needs | a provider (free OpenRouter models ship configured) | a BotFather token (human step) |

### Dashboard bot (verified live 2026-10-18)

Settings → CREATE A BOT asks for a name, an agent id (auto-slugged from the
name), a model-gateway provider, a model, and an optional persona. The id
becomes the task backend: it appears in the HUD agent selector and in
`/cx assign <id> <task>`. Server-side rules: ids must be safe slugs, CLI agent
ids (`hermes`, `opencode`, …) and existing bots are refused with 409, and the
persona is capped at 4000 characters. Bots are stored in
`state/llm_assignments.json` (no secrets) and run through the same approval
gate and system prompt as every other agent — the persona is prepended, the
policy text stays intact.

Live proof: a bot `researcher` (persona "You are Researcher, a meticulous
analyst…") answered `backend=researcher` with *"I am Researcher, a meticulous
analyst…"* on the free `nvidia/nemotron-3-super-120b-a12b:free` model (cost $0);
duplicate create → 409, `hermes` as id → 409, malformed id → 422.

## Telegram bot = Hermes profile


Verified mechanics (2026-10-18, `hermes` v0.22.1 on VM 103):

- `hermes profile create <name> --clone` copies `config.yaml` (model, providers,
  the free `fallback_providers` chain, `agent.reasoning_overrides`), `.env`
  (minus messaging tokens/allowlists), `SOUL.md`, and skills. It also creates a
  convenience alias `<name>` in `~/.local/bin/`.
- The clone **intentionally leaves the Telegram token behind** — a new bot can
  never accidentally share the default bot's polling identity.
- `hermes gateway list` enumerates profiles; a profile without channels or with
  the gateway stopped stays dormant and costs nothing.
- `hermes profile delete <name>` removes the profile and its gateway from the
  list cleanly.

## Create a new bot

1. **BotFather (human step, once per bot).** In Telegram, talk to
   [@BotFather](https://t.me/BotFather): `/newbot`, pick the display name and
   username, copy the token. Keep the token out of logs and chat history.

2. **Create the profile** (as the `hermes` user):

   ```sh
   hermes profile create <name> --clone
   ```

3. **Give it its own credentials and allowlist.** Edit
   `~/.hermes/profiles/<name>/.env` (mode 0600) and add:

   ```sh
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   TELEGRAM_ALLOWED_USERS=<your numeric Telegram user id>
   ```

   Do not copy another profile's token; one token per bot.

4. **Personality and model (optional).** `profiles/<name>/SOUL.md` is the
   system persona; `profiles/<name>/config.yaml` sets the model. The clone
   already inherits the free fallback chain, so a fresh profile works without
   paid credits: primary model as configured (e.g. `gpt-6-luna` while its quota
   lasts) with OpenRouter **free** models as fallback.

5. **Serve it from the host gateway** (no second process, no second poller):

   ```sh
   hermes -p <name> gateway start     # hot-serve on the running host gateway
   # or, as the create output suggests:
   hermes gateway restart             # restarts the host gateway, serves all profiles
   ```

6. **Verify**:

   ```sh
   hermes gateway list     # <name> must appear served, same host gateway
   ```

   Then message the new bot in Telegram. CortexOS sees it exactly like the
   default bot: the bridge plugin and the queue are per-profile agnostic.

## Stop or remove a bot

```sh
hermes -p <name> gateway stop     # park the profile (host stops serving it)
hermes profile delete <name>      # remove profile files entirely
```

Never use `--force` to run a profile's own gateway while the host gateway is up:
two processes polling one bot token is the one mode that breaks Telegram
downtime for that bot.

## Free models

- CortexOS decision model: `router.fast_model_name` points at the free
  OpenRouter model `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`
  (tier classification, risk gate, tier-2 answers; extracted cost $0).
- Hermes: `fallback_providers` chains free OpenRouter models
  (`nvidia/nemotron-3-super-120b-a12b:free`, then
  `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`) when the primary is
  rate-limited, with `agent.reasoning_overrides` set to `low` for both so
  fallback turns stay fast (verified 52s → 7s on a real turn).
- Errors, empty responses, and exhausted quotas fail closed: a turn either
  answers or surfaces an honest error; the approval gate is never relaxed.

## What still needs a human

- BotFather token per bot (above step 1).
- A Grok route decision: no free Grok exists on this box (xAI OAuth is
  rate-limited to 0/req, OpenRouter Grok models are paid). A "Grok-named" bot
  can run today on the free models; real Grok requires a paid key or credits.
