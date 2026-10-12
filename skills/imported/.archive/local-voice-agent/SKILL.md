# local-voice-agent

# Local Voice Agent

Build a voice conversation system: mic → local STT → agent brain → local TTS → speakers.
Half-duplex (mic gated while agent speaks). Private by default — run the models locally.

## Architecture (proven)
```
mic → Ears (whisper.cpp :2022 /v1/audio/transcriptions)
    → Brain (warm persistent agent session, streaming)
    → Sentence chunker
    → Mouth (Kokoro :8880 /v1/audio/speech → PCM 24k int16)
    → speakers
```
State machine: keydown→listening → keyup→thinking → first audio→speaking → done→idle.
While speaking: mic closed, transcription off, no self-hear. Press talk while speaking →
interrupt + reopen mic. Typed input races PTT via asyncio.wait(FIRST_COMPLETED).

## Gotchas (real, cost time — read references/)
- **whisper.cpp server** uses `--port` (NOT `-p`); default 8080; serves ONLY `/inference`,
  not `/v1/audio/transcriptions`. Put a thin adapter in front to expose the OpenAI route.
  See `references/whisper_kokoro.md`.
- **Kokoro** PyPI `kokoro[fastapi]` extra does NOT ship a server module in current versions.
  Write a small FastAPI wrapping `KPipeline` (returns raw PCM). See `references/whisper_kokoro.md`.
- **Brain = Hermes locally**: `hermes serve` WS gateway (:9119) needs dashboard cookie auth
  (401 headless). The proven headless path is `hermes chat -q "<text>" -Q --resume <id>`;
  session_id prints to STDERR. Capture it, reuse it → warm persistent session.
  See `references/hermes_brain.md`.
- **Audio I/O needs a real machine**: sounddevice needs PortAudio; pynput needs an X display.
  Headless servers can't capture/play audio — run the loop on the desk machine.

## Workflow rule (user correction, 2026-07-15)
Before building a visual/conversational system from a spec, ASK FIRST. Propose the
concept (or state→visual mapping) and wait for approval before writing code. The user
explicitly required this twice: "Proceed, and always ask like you just did before any
build." For the visualizer he picked a concept, then I proposed how each of the 5 states
maps to the scene and waited. Embed this in any creative/build task for this user.

## Companion: browser visualizer (reads the same voice bus)
A fullscreen Canvas visualizer can react to the voice agent via the `~/voice-line/`
status files. Server is stdlib-only, read-only, binds 127.0.0.1:8777; `/state` returns
`{state, level, alert}`. Critical **stomp-tolerance**: if `.voice_waveform` is <2s fresh,
report `speaking` regardless of `.voice_state`. See `references/voice_bus_visualizer.md`.

## Interruptibility & signal bus (root-cause fixes)
- **Blocking playback kills interrupts.** `sd.OutputStream.write(whole_sentence)` blocks the
  event loop, so `stop_playback()` from another task can't run mid-sentence. Fix: play in
  `loop.run_in_executor`, write in small chunks (e.g. 2048 samples), check a
  `threading.Event` stop flag between chunks (~40ms cancel latency). Root cause = single
  blocking call, not the interrupt logic.
- **Feed the visualizer REAL level.** Don't write `set_waveform([0.0]*64)`. Downsample the
  actually-played PCM to 64 abs-normalized floats and push it to `.voice_waveform` so the
  visualizer's speaking animation tracks the voice.

## Remote / thin-client deployment (audio ≠ brain machine)
When the agent CLI (`hermes`) lives on a server but mic/speakers are on a laptop, run the
audio loop on the laptop and reach the rest over LAN. Make endpoints env-driven:
- `HERMES_HOST` (default 127.0.0.1) → Whisper/Kokoro URLs `http://$HERMES_HOST:2022|8880`.
- `HERMES_BIN` (default "hermes") → `.split()` it so `HERMES_BIN="ssh user@host hermes"`
  runs the brain remotely. **SSH argv trap:** `create_subprocess_exec(["ssh","host","hermes","chat","-q",text,"-Q"])`
  passes each token separately; the remote sshd re-joins them and the remote shell re-splits
  on spaces, so the quotes around `text` are LOST → `hermes: error: unrecognized arguments`
  (a multi-word query splits into stray args). Fix: when prefix[0]=="ssh", build ONE remote
  string with the query shell-quoted: `remote = "hermes chat -q " + shlex.quote(text) + " -Q ..."`
  then `cmd = [*ssh_prefix, remote]`. Local (non-ssh) path keeps the argv list unchanged.
- **BIND 0.0.0.0, NOT 127.0.0.1.** Whisper adapter (:2022), Kokoro (:8880), and the
  visualizer server (:8777) all default to `127.0.0.1` — unreachable from the laptop.
  For thin-client, bind `0.0.0.0` on every service the laptop hits (uvicorn `host=`,
  `ThreadingHTTPServer(("0.0.0.0", PORT))`). Restart the actual PROCESS after the edit;
  a running daemon keeps its old bind. Identify by port, kill that PID, relaunch.
- run.sh guard: don't gate on `if [ "$HERMES_HOST" = 127.0.0.1 ]` — the env var often
  fails to propagate into the script and it wrongly boots local torch/uvicorn. PROBE the
  target instead: `if curl -s --max-time 2 http://$HERMES_HOST:2022/health; then skip
  local servers; else start them; fi`. Probing the real endpoint is robust to env drift.
- Needs keyless SSH (ed25519 → server `authorized_keys`). If server user has NO password,
  key auth is the ONLY way in; `ssh-copy-id` can't run, so append the laptop's
  `id_ed25519.pub` to the server's `~/.ssh/authorized_keys` directly. See `references/remote_client.md`.

## Pitfalls that crashed a real session
- **Duck/context-manager `__exit__` reads an attr `__init__` never set.** `Duck.__exit__`
  read `self._vol`, but `__enter__` only set it when Spotify was running → `AttributeError`
  killed the turn when Spotify was absent. Rule: init EVERY attribute referenced in
  `__exit__`/cleanup in `__init__` (`self._vol = None`), so partial-enter never crashes exit.
- **Scope: "voice agent" ≠ "browser app".** User expected to open the voice agent in a
  browser like a dashboard. The talk loop is a LOCAL python app (mic/speakers), not a web
  UI — only the visualizer is browser-served. State this split upfront: visualizer = browser
  (`http://<host>:8777`); talking = local app. Browser-mic voice is a separate, unbuilt feature.

## Remote brain via OpenLive + ACP over SSH
When the user runs OpenLive (a desktop voice app) on their laptop and wants Hermes on a
headless host to be the brain, OpenLive drives Hermes via ACP over keyless SSH. See
`references/openlive_acp_ssh.md` for the full setup: install `hermes-agent[acp]` in the
Hermes venv, generate an ed25519 key, add it to the laptop's `authorized_keys`, and point
OpenLive's Hermes agent at a project path on the host.

## Files shape
main.py (orchestrator) · ears.py (STT) · brain.py (agent) · mouth.py (TTS) ·
ptt.py (hotkey + held-state) · ducking.py (optional Spotify) · signals.py (status bus) ·
run.sh (start servers). Keep it to the files the spec needs; no extra layers.

## Verify before claiming done
Whisper health 200 · Kokoro returns PCM · brain returns a reply + reuses session_id ·
full text loop (transcript→brain→PCM) works. Real mic/speaker only testable on desk machine.
