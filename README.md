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
- **Bridge:** owns skill discovery, request receipts, routing, approval state, and backend selection. It binds to loopback and protects `/api` with a local shared token.
- **Skill backbone:** skills are portable Markdown instructions plus a small `skill.json` manifest. `done by hand twice → capture a skill`; after five successful runs, review the skill for automation. Automation never bypasses the approval policy.
- **Router:** tier 1 handles explicit saved-data/rule lookups; tier 2 is an optional OpenAI-compatible fast model; tier 3 runs an installed Claude Code or Codex CLI. Skill manifests declare structured risk. Unscoped Tier 3 work uses a structured classifier when available and fails closed when classification is unavailable or uncertain.
- **Memory:** all durable data is plain Markdown under the selected vault. `raw/` is the user capture area, `wiki/` contains organized notes and `_master-index.md`, and `output/` holds generated deliverables. Request logs and skill receipts are kept separately for predictable indexing.
- **Safety:** every skill declares `risk_level` and `approval_required`. External, financial, unknown, or explicitly gated risks require fresh approval for every run, including promoted and scheduled runs. The scaffold does not ship connectors that could perform these actions.
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
sudo apt-get update
sudo apt-get install -y espeak-ng
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[voice]"
if [ ! -f config/cortexos.toml ]; then cp config/cortexos.example.toml config/cortexos.toml; fi
cd apps/hud && npm install && npm run build
cd ../obsidian-plugin && npm install && npm run build
cd ../..
python -m cortexos init-vault ./vault
python -m cortexos token
python -m cortexos serve
```

The Bridge defaults to `http://127.0.0.1:8765`. Paste the printed token into the HUD when prompted, or into the Obsidian plugin settings. This is a single-user local access barrier, not multi-user authentication; do not expose the Bridge beyond loopback. Configure your vault path and provider in `config/cortexos.toml`. To run Tier 3, install and authenticate `claude` or `codex`.

See [docs/setup.md](docs/setup.md), [docs/smoke-test.md](docs/smoke-test.md), [docs/architecture.md](docs/architecture.md), and [docs/skills.md](docs/skills.md).

## Status

Implemented locally (not yet release-certified): skill catalog with explicit risk metadata, structured risk classification and fail-closed fallback, persistent approval/scheduler state, token-protected Bridge APIs, request/receipt logging, local HUD and Obsidian clients, Whisper/Kokoro voice adapters, interactive/re-attachable PTY sessions with summary receipts, five-confirmation skill promotion review, approved-skill scheduling, human-editable TOML metrics, and panel layout controls. The small noninteractive smoke runner is provided but has not been run as part of this change.

See [docs/setup.md](docs/setup.md) for the complete install sequence, Bridge token setup, metric schema, and recovery limits. Run [docs/smoke-test.md](docs/smoke-test.md) for user-run checks. Live social/usage figures require real user-entered data or future connectors; CortexOS does not invent them.
