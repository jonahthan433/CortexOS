# Setup

## Requirements

- Python 3.11 or newer.
- Optional: Claude Code (`claude`) or Codex CLI (`codex`), installed and authenticated for tier 3.
- Optional: a local OpenAI-compatible server such as Ollama for tier 2.
- Obsidian is optional; any folder can serve as the vault.
- For local voice, install `espeak-ng`; the Python voice extra provides faster-whisper and Kokoro.

## Install

```bash
cd CortexOS
python -m venv .venv
. .venv/bin/activate
pip install -e .
cp config/cortexos.example.toml config/cortexos.toml
```

### Install local voice (Whisper + Kokoro)

On Debian/Ubuntu, install Kokoro's phonemizer dependency, then install CortexOS's optional local voice engines:

```bash
sudo apt-get update
sudo apt-get install espeak-ng
pip install -e ".[voice]"
```

On the first request, faster-whisper downloads the configured Whisper model (`base.en` by default) and Kokoro loads its voice assets. To fetch/warm both models before using the HUD:

```bash
python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8')"
python -c "from kokoro import KPipeline; KPipeline(lang_code='a')"
```

Set `[voice].stt_model`, `stt_device`, `stt_compute_type`, `tts_language`, and `tts_voice` in `config/cortexos.toml` to change models, hardware, language, and voice. CPU/int8 is the default. CUDA acceleration requires matching CUDA/cuDNN libraries for CTranslate2; see the [faster-whisper GPU notes](https://github.com/SYSTRAN/faster-whisper#gpu).

Edit `config/cortexos.toml`: set `bridge.vault` to your Obsidian vault path, select `codex` or `claude`, and adjust executable names/arguments if needed. Do not put API secrets into the vault. For a local model, set `fast_model_enabled = true` and use the server's local endpoint.

Bootstrap an existing or new vault:

```bash
python -m cortexos init-vault /path/to/YourVault
```

Start the Bridge:

```bash
python -m cortexos serve
```

The API listens at `http://127.0.0.1:8765` by default. Available endpoints include `GET /api/status`, `GET /api/skills`, `POST /api/requests`, `POST /api/requests/{id}/approve`, and `POST /api/sessions`. FastAPI's local OpenAPI page is at `/docs`.

The web HUD is served at `http://127.0.0.1:8765/` by the same process.

## Build the HUD terminal

The HUD bundles xterm.js locally so terminal code is not fetched from a CDN:

```bash
cd apps/hud
npm install
npm run build
cd ../..
```

Interactive PTY sessions require Linux or macOS plus the selected CLI installed and authenticated. They run with the configured vault as their working directory. The HUD can send keystrokes, resize a live Claude Code/Codex TUI, and interrupt or end the session. Tier 3 skill requests run headlessly; their live stdout/stderr appears in the task stream and can be interrupted there.

## Confirmations, promotions, and schedules

After a skill finishes, mark the run successful only when it delivered the expected result. CortexOS stores that confirmation under `receipts/`. The Bridge accepts one confirmation per completed run, validates the run receipt and skill ID, and enables promotion review after five successful confirmations. Use the HUD card's promotion button or Obsidian's **Review skill automation promotions** command. Promotion approval is a separate human decision saved in `receipts/promotions/`; it does not remove a skill's per-run send, spend, or publish approval gate.

The local scheduler reads `vault/automations.toml` every 30 seconds. Bootstrap creates a comments-only file with no jobs enabled. To add a job, use this format only after promoting that skill:

```toml
[[jobs]]
id = "weekly-review"
skill_id = "productivity-weekly-review"
enabled = true
interval_minutes = 10080
prompt = "Prepare my weekly review from the latest notes."
```

Only approved skills run; request submission still enforces the normal action approval policy. Keep schedules local and disable a job with `enabled = false`.

Bootstrap also creates blank `metrics/audience.md` and `metrics/usage.md` entry forms. They contain no sample figures. Fill in actual values and sources; unknown fields remain blank. The HUD reads metric files directly from the configured vault and refreshes them with the system data.

## Customize the HUD

Choose **Customize panels** to hide panels or move them up and down. The layout is saved in browser local storage on this device, independently of vault data. New panel modules can be added by giving their wrapper a unique `data-panel` key.

## Obsidian plugin

Build the desktop plugin bundle:

```bash
cd apps/obsidian-plugin
npm install
npm run build
```

Copy `main.js`, generated `main.css` (rename it to `styles.css`), and `manifest.json` into `<vault>/.obsidian/plugins/cortexos-bridge/`, then enable CortexOS Bridge in Obsidian's Community plugins settings. Set the Bridge URL and backend in plugin settings. The Bridge must be running on the same computer. The plugin writes a convenience copy of its response into `output/CortexOS/` in the active vault. It streams Tier 3 output, can interrupt a run, and can open an interactive PTY-backed CLI session from its command modal.

Use a skill directly:

```bash
python -m cortexos ask "Prepare a weekly review from my notes" --skill productivity-weekly-review
```

## Safety and scope

The starter router and approval API are local tools, not an authentication system for network exposure. Keep `host` at `127.0.0.1`; do not expose the Bridge to a LAN or public internet without adding authentication and origin controls. Skills that send, spend, or publish should require approval. Provider CLIs may have broad local permissions; use their own sandbox/permissions settings for your environment.

## Voice

The HUD and Obsidian plugin record audio locally in the app, post it to the loopback Bridge, transcribe with faster-whisper, submit the transcript through the standard CortexOS router, and can play the result using local Kokoro-generated WAV. Audio is processed in memory and is not written into the vault. Endpoints: `GET /api/voice/status`, `POST /api/voice/turn` (base64 encoded audio JSON), and `POST /api/voice/speak` (JSON text; WAV response).

Browser/OS speech is a manually selected last resort after local STT or TTS fails. It may use a non-local service. The UI labels this before invoking it; local voice is always attempted first.
