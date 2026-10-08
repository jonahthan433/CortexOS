# CortexOS
Custom Agentic OS

CortexOS is a local-first personal AI operating system built around portable skills, one local Bridge, and an Obsidian-compatible Markdown vault.

## Architecture

```text
Obsidian plugin ─┐
                 ├── Local Bridge ── Router ── Tier 1 rules / Tier 2 fast model / Tier 3 agent CLI
Web HUD ─────────┘        │                                  │
                          └────────── Obsidian vault ─────────┘
```

- **Two front doors:** the web HUD and Obsidian plugin are clients of one Bridge API. They do not each implement their own agent logic.
- **Bridge:** owns skill discovery, request receipts, routing, approval state, and backend selection. It binds to loopback by default.
- **Skill backbone:** skills are portable Markdown instructions plus a small `skill.json` manifest. `done by hand twice → capture a skill`; after five successful runs, review the skill for automation. Automation never bypasses the approval policy.
- **Router:** tier 1 handles explicit saved-data/rule lookups; tier 2 is an optional OpenAI-compatible fast model; tier 3 runs an installed Claude Code or Codex CLI. Conservative keyword/intent rules route new requests to tier 3. An optional Jev-compatible classifier can be added behind the router interface.
- **Memory:** all durable data is plain Markdown under the selected vault. `raw/` is the user capture area, `wiki/` contains organized notes and `_master-index.md`, and `output/` holds generated deliverables. Request logs and skill receipts are kept separately for predictable indexing.
- **Safety:** sending, spending, and publishing skills declare `approval_required`; the bridge refuses to execute them until a human approves. The scaffold does not ship connectors that could perform these actions.
- **Local-first voice:** faster-whisper STT and Kokoro TTS run behind replaceable local adapters. Both interfaces record and speak through the Bridge, with browser/OS speech offered only as a labeled fallback.

## Project structure

```text
CortexOS/
  apps/hud/                    Web HUD, local voice UI, terminal, panel layout
  apps/obsidian-plugin/        Vault command, voice, terminal, automation review
  cortexos/                    API, router, providers, voice, scheduler, vault
  skills/<domain>/<skill>/     Reusable skill instructions and manifest
  vault-template/              Bootstrap vault structure, metrics forms, schedules
  config/                      Example configuration
  docs/                         Setup, architecture, skill authoring
```

## Quick start

Requires Python 3.11+.

```bash
cd CortexOS
python -m venv .venv
. .venv/bin/activate
pip install -e .
cp config/cortexos.example.toml config/cortexos.toml
python -m cortexos init-vault ./my-vault
python -m cortexos serve
```

The Bridge defaults to `http://127.0.0.1:8765`. Configure your vault path and provider in `config/cortexos.toml`. To run tier 3, install and authenticate either `claude` or `codex`, then select the provider in configuration or with `--backend`.

See [docs/setup.md](docs/setup.md), [docs/architecture.md](docs/architecture.md), and [docs/skills.md](docs/skills.md).

## Status

Implemented: skill catalog and manifests, starter skills across requested domains, conservative three-tier router, tier 1 commands and metric lookup, optional OpenAI-compatible tier 2, Claude Code/Codex CLI tier 3, local HTTP API, human approval endpoint, vault bootstrap, request/receipt logging, CLI, local web HUD, Obsidian desktop plugin, local Whisper/Kokoro voice for both interfaces, xterm.js terminal sessions, live/cancellable output for headless Tier 3 requests, five-confirmation skill promotion review, approved-skill background scheduling, blank real-metric entry forms, and a local HUD panel layout editor.

Install the `voice` extra and `espeak-ng` to enable local Whisper/Kokoro. Build the xterm.js bundles in `apps/hud` and `apps/obsidian-plugin` for interactive sessions. The browser/OS speech fallback is manual and explicitly labeled because it may not remain on-device. Live social/usage figures still require user-entered data or configured connectors; CortexOS does not invent them. See [docs/setup.md](docs/setup.md) for schedules, promotion review, metric templates, and panel layout customization.
