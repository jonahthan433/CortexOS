# Setup

## Requirements

- Python 3.11 or newer.
- Node.js and npm to build the xterm.js terminal bundles.
- `espeak-ng` for Kokoro phonemization.
- Optional Tier 3: the Claude Code CLI, Codex CLI, or both, installed and authenticated.
- Optional Tier 2 and unscoped action classification: an OpenAI-compatible fast model endpoint.
- Optional Obsidian desktop. The local web HUD works without it.

## Install and launch

From the CortexOS checkout:

```bash
sudo apt-get update
sudo apt-get install -y espeak-ng
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e "[voice]"
if [ ! -f config/cortexos.toml ]; then cp config/cortexos.example.toml config/cortexos.toml; fi
cd apps/hud && npm install && npm run build
cd ../obsidian-plugin && npm install && npm run build
cd ../..
python -m cortexos init-vault ./vault
python -m cortexos token
python -m cortexos serve
```

Open <http://127.0.0.1:8765/> and paste the token printed by `python -m cortexos token` when prompted. The token lives in git-ignored `config/cortexos.token` with owner-only permissions. Keep a copy in a password manager if needed; do not commit or share it. The Bridge is for one trusted local user only. The token is a local access barrier, not multi-user authentication or a substitute for TLS; keep `host` at `127.0.0.1` and do not expose the Bridge to a LAN or the internet.

To start without voice, use `pip install -e .` and set `[voice].enabled = false`. The first local voice use downloads the configured Whisper and Kokoro models; model downloads need network access, subsequent inference runs locally.

The example selects Codex. Install and authenticate the desired provider CLI before using Tier 3; change `bridge.backend` to `claude` for Claude Code. The interactive terminal has its own backend selector.

## Models and router

Tier 2 is disabled by default. To enable it, start a local OpenAI-compatible server, set `router.fast_model_enabled = true`, and configure its URL/model in `config/cortexos.toml`. Short factual requests can use Tier 2. Otherwise, substantial work uses the selected Tier 3 CLI. The same fast model supplies structured JSON action-risk decisions for requests without a skill manifest. If classification is disabled, unavailable, malformed, or uncertain, such Tier 3 requests wait for approval. Tier 1 rules use local data and do not need a model.

Voice defaults use CPU/int8. Configure `[voice]` to select different models. `/api/voice/status` reports dependency readiness; the first model load can still fail if model assets or `espeak-ng` are unavailable.

## Obsidian plugin

Copy the generated `main.js`, `main.css` (rename to `styles.css`), and `manifest.json` from `apps/obsidian-plugin` into `<vault>/.obsidian/plugins/cortexos-bridge/`. Enable the plugin. In settings, enter the Bridge URL and the token from `python -m cortexos token`. The Bridge writes canonical deliverables to its configured vault; the plugin also saves a convenience response copy in the active vault.

## Vault, risk declarations, and persistent state

Bootstrap preserves existing files and creates `raw/`, `wiki/`, `output/`, `requests/`, `receipts/`, and `metrics/`, along with `AGENTS.md`, `CLAUDE.md`, a comments-only `automations.toml`, and `state.json`. Promotion history and user votes are receipts. `state.json` is atomically replaced and contains pending approvals and scheduler claim times; it is local operational state, not a user metrics file.

Every `skills/**/skill.json` must declare `risk_level` (`read_only`, `local_write`, `external_action`, `financial`, or `unknown`) and `approval_required`. Invalid or missing risk metadata becomes `unknown` and requires approval. External, financial, and unknown risks always require a fresh approval for every run, including scheduled and promoted runs. `approval_required: true` also gates a skill. Promotion does not modify these fields. For requests without a matching skill, the fast-model classifier returns a structured risk decision; failures and unknown outcomes require approval. Provider prompts also prohibit external actions for unapproved runs. Review permissions granted to provider CLIs: this local application prompt is not an OS sandbox. No social, email, payment, or CRM connectors ship with this version.

PTY sessions can be reattached from the HUD while their Bridge process remains running. A Bridge restart ends the actual child process; receipts keep only backend, timestamps, byte counts, and exit code, not terminal text or commands. This avoids persisting secrets but means a terminated PTY cannot be resurrected after a Bridge restart.

## Metrics

Edit the human-readable `vault/metrics/*.toml` files. The minimal schema uses `schema_version = 1`, a `kind`, and one or more `[[metrics]]` records with `key`, `value`, `unit`, `updated`, and `source`. Values are strings so blank means unknown; enter only actual measured or user-provided values. The HUD displays blanks as an em dash. Markdown metric files from older vaults remain readable, but new files use the TOML schema.

## Promotion and scheduler

Five distinct user-confirmed successful skill runs make that skill eligible for automation review. The promotion ledger records a separate review approval. Add enabled jobs to `automations.toml` only after promotion is approved. No schedules are enabled by default. The scheduler writes a job's last-run claim before dispatch: a crash may skip a run, but a restart will not silently replay it. If a job has an external-action risk, its normal per-run approval remains required.

Use the smoke steps in [smoke-test.md](smoke-test.md). The noninteractive checks can also be run with:

```bash
source .venv/bin/activate
python scripts/smoke_noninteractive.py
```

That script uses a temporary vault, a stub provider, blank metric templates, and a scheduler dry-run. It does not call real providers, start a server, or change the configured vault. This command is provided for you to run; it has not been run as part of this code change.
