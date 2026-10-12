# Setup

## Requirements

- Python 3.11 or newer.
- Linux with systemd user services for the boot-starting Bridge (foreground launch remains available without systemd).
- Node.js and npm to build the xterm.js terminal bundles.
- `espeak-ng` for Kokoro phonemization.
- Tier 3 primary path: the Codex CLI, installed and authenticated. Claude Code remains an optional alternate backend.
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
python -m cortexos service install
```

This installs and starts a per-user systemd service that restarts automatically and starts with your user session. Open <http://127.0.0.1:8765/> and paste the token printed by `python -m cortexos token` when prompted. The token lives in git-ignored `config/cortexos.token` with owner-only permissions. Keep it local. The Bridge is for one trusted local user; keep `host` at `127.0.0.1`.

To have the service start after boot even before you log in, enable systemd lingering once:

```bash
sudo loginctl enable-linger "$USER"
```

Without lingering, the service starts when your user session starts. To manage it later, run `cortexos service status`, `cortexos service start`, `cortexos service stop`, or `cortexos service restart`. `cortexos service uninstall` removes the user unit. For a temporary foreground run, use `python -m cortexos serve` instead of installing the unit.

Service logs are available with `journalctl --user -u cortexos.service -f`.

To start without voice, use `pip install -e .` and set `[voice].enabled = false`. The first local voice use downloads the configured Whisper and Kokoro models; model downloads need network access, subsequent inference runs locally.

Codex is the default Tier 3 agent in the example config. The Bridge is an agent front door, not tied to Codex: Claude Code is also registered, and additional command-line agents can be added under `[providers.<agent-id>]`. Install and authenticate each CLI locally. Both HUD and Obsidian offer Auto tier routing or a specific registered agent; explicitly selecting an agent sends non-Tier-1 work directly to it.

## Models and router

Tier 2 is disabled by default. To enable it, start a local OpenAI-compatible server, set `router.fast_model_enabled = true`, and configure its URL/model in `config/cortexos.toml`. In Auto mode, short factual requests can use Tier 2; substantial work uses the configured Codex default at Tier 3. Selecting a named agent bypasses Tier 2 for that request. The same fast model supplies structured JSON action-risk decisions for requests without a skill manifest.

### Register another agent

Add a provider stanza to `config/cortexos.toml`; the stanza ID is the agent ID shown in the HUD and Obsidian selector. Configure the CLI command, arguments, and interactive terminal command. Prompts are sent on stdin by default. Use `input_mode = "argument"` if the CLI expects the prompt as its final argument, or include `{prompt}` in `args` to substitute it. For example:

```toml
[providers.my-agent]
name = "My Agent"
command = "my-agent"
args = ["--prompt", "{prompt}"]
terminal_command = "my-agent"
terminal_args = []
```

Restart the Bridge after editing the config. No Python provider code changes are needed for a CLI that can accept a prompt through stdin, an argument, or the `{prompt}` placeholder.

Voice defaults use CPU/int8. Configure `[voice]` to select different models. `/api/voice/status` reports dependency readiness; the first model load can still fail if model assets or `espeak-ng` are unavailable.

## Obsidian + Codex

Use the same vault path in Obsidian and CortexOS. With the default configuration, CortexOS uses `./vault` relative to the repository. For another vault, set `bridge.vault` in `config/cortexos.toml` to its path, then initialize that exact folder:

```bash
python -m cortexos init-vault ./vault
```

Replace `./vault` with the path configured above. Bootstrap keeps existing files and creates `raw/`, `wiki/`, `output/`, `requests/`, `receipts/`, and `metrics/`, plus `AGENTS.md` and `CLAUDE.md`. Codex follows `AGENTS.md`; Claude Code can follow `CLAUDE.md`, which points to the same vault rules.

In Obsidian, choose **Open folder as vault** and select the configured vault folder. Build and install the CortexOS plugin from the repository:

```bash
cd apps/obsidian-plugin
npm install
npm run build
cd ../..
OBSIDIAN_VAULT="/absolute/path/to/your/vault"
PLUGIN_DIR="$OBSIDIAN_VAULT/.obsidian/plugins/cortexos-bridge"
mkdir -p "$PLUGIN_DIR"
cp apps/obsidian-plugin/main.js apps/obsidian-plugin/main.css apps/obsidian-plugin/manifest.json "$PLUGIN_DIR/"
```

In Obsidian, enable community plugins if prompted, then enable **CortexOS Bridge** under **Settings → Community plugins**. In its settings, keep the URL at `http://127.0.0.1:8765`, paste the value printed by `python -m cortexos token`, and select **Auto (Tier routing)** or a named agent. Auto uses Codex for Tier 3 by default. The plugin loads registered agents and skills from the Bridge and uses the selected agent for text, voice, and the interactive terminal.

The **Local REST API** community plugin is optional and separate from CortexOS. To install it, open **Settings → Community plugins → Browse**, search for “Local REST API” by coddingtonbear, install, and enable it. CortexOS does not require this plugin: its own local Bridge writes to the configured vault. Enable Local REST API only if you want its additional Obsidian API for other local tools, and follow its own settings for its separate API key. See the [plugin listing](https://community.obsidian.md/plugins/obsidian-local-rest-api).

To point Codex directly at the vault, open a terminal there before starting Codex:

```bash
cd "/absolute/path/to/your/vault"
codex
```

The vault’s `AGENTS.md` gives Codex the `raw/`, `wiki/`, `output/`, request, receipt, and metrics rules. CortexOS’s Bridge also starts its Codex provider in the configured vault folder, so HUD and Obsidian skill runs share the same memory and outputs.

### Verify both front doors

Confirm the background Bridge is live with `cortexos service status`, open the HUD at <http://127.0.0.1:8765/>, and paste the local Bridge token. In Obsidian, open the CortexOS command from the ribbon or command palette and use the same token configured in plugin settings.

1. **Skills:** run a listed skill from the HUD’s Quick launch panel. In Obsidian, choose the same skill from the skill dropdown and run it. Confirm both runs appear under the configured vault’s `requests/`, `output/`, and `receipts/` folders.
2. **Agent terminal:** choose an agent in the HUD terminal selector and in Obsidian’s agent setting, then open a terminal from each. Enter a harmless read-only request and confirm each pane shows output. Both terminals start in the configured vault; Ctrl+C interrupts the running CLI.
3. **Local voice:** choose Auto or a named agent in each interface, then record “list skills.” Confirm local Whisper sends the transcript through the Bridge and Kokoro speaks the answer. For an agent-specific check, choose Codex (or another installed agent) and dictate a harmless Tier 3 request. Use the explicit browser/OS fallback only if local voice is unavailable.

If either UI returns an authentication error, re-enter the value from `python -m cortexos token`. If the plugin cannot load skills or terminal sessions, confirm the Bridge is running and both the Bridge URL and token match. Keep the Local REST API’s own token separate; it is not the CortexOS Bridge token.

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
