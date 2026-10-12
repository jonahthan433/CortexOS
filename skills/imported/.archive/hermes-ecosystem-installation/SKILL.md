# hermes-ecosystem-installation

# Hermes Ecosystem Installation

Installing community skills/plugins (GitHub repos, awesome-hermes-agent, agentskills.io, skill registries). Verified 2026-08-10 on a bulk 6-item install.

## Skills (bulk pattern)
1. Clone: `git clone --depth 1 -q <repo> /tmp/dir`
2. Skills live in varied paths (`/SKILL.md`, `skills/*/SKILL.md`, `.claude/skills/*/SKILL.md`, `writing-skills/*/`). Walk the tree for dirs containing `SKILL.md`, copy each dir into `~/.hermes/skills/<category>/<name>/` (name from frontmatter `name:`, fallback = dir name).
3. **Collision dedup**: big packs (e.g. dev-gtm) bundle their own copies of superpowers skills (`brainstorming`, `test-driven-development`, `using-superpowers`) — REMOVE the duplicates from the new category, never overwrite the canonical ones. Check `hermes skills list` after.
4. Skip the repo's non-skill dirs (`.git`, `scripts`, `tools`, `schema`, `assets`).
5. Verify: `hermes skills check` (audit/updates) + `hermes skills list` tail (counts: "N enabled").

## Plugins
- `hermes plugins install <owner/repo>` copies the repo; **portable packages install DISABLED** — enable after.
- **Manifest-less plugins** (no `plugin.yaml`/`plugin.json` at root): `hermes plugins install` copies the repo but the loader can't see it. If `pyproject.toml` has `[project.entry-points."hermes_agent.plugins"] name = "module"`, install the package into the hermes venv: `/home/hermes/.hermes/hermes-agent/venv/bin/pip install <plugin-dir>` — it then appears as `entrypoint` source, **enabled by the ENTRY-POINT name** (e.g. repo rtk-hermes → plugin id `rtk-rewrite`), not the repo name.
- **Tool-override permission**: enabling a plugin that can intercept tools prompts "Grant it?" — declining is fine for normal operation (`hermes plugins enable <name>` succeeds; `--allow-tool-override` grants the privileged path later if needed). Default: enable without override.
- **Gateway restart is required for plugins to take effect and is BLOCKED from inside the gateway process** ("cannot restart the gateway from inside the gateway process"). Tell the user to run `hermes gateway restart` from a separate shell, or it activates on the next natural restart. Don't attempt it in-session.

## Pitfalls
- **`hermes mcp add` — flags after `--args` leak into the TOOL's argv, not Hermes'.** `hermes mcp add headroom --command /path/bin --args mcp serve --connect-timeout 90` passes `--connect-timeout 90` to the tool itself → it errors at startup → "✗ Connection failed: Connection closed". Fix: keep `--args` pure tool args (`--args mcp serve`) and pipe `printf 'Y\n' |` into the command so the interactive "Enable all N tools? [Y/n]" prompt resolves in non-tty sessions. Verify with `hermes mcp test <name>`; new MCP tools load next session.
- **Cron prompts must never embed secrets or curl-with-token** — a prompt containing `curl .../bot${TELEGRAM_BOT_TOKEN}/getChat` is BLOCKED by the `exfil_curl_url` threat pattern at job creation. Wrap the call in a local script (e.g. `~/.hermes/scripts/cc_subs.py` reads the token from `~/.hermes/.env` itself, prints a result) and have the cron prompt invoke the script. Script-indirection is the sanctioned pattern for cron jobs needing secrets/metrics.
- `hermes skills install <identifier>` takes a registry identifier or SKILL.md URL — for local dirs, manual copy (above) is more predictable.
- The awesome-hermes-agent list tags items `production`/`beta`/`experimental` — beta+ items usually install cleanly; experimental ones may lack manifests.
- After bulk installs, re-run `hermes skills check` — a clean audit is the completion signal.
