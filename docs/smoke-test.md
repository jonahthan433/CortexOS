# CortexOS v0.1 smoke checks

The automated check exercises noninteractive code in an isolated temporary vault. Run it from the repository after setup:

```bash
source .venv/bin/activate
python scripts/smoke_noninteractive.py
```

It bootstraps the vault, checks Tier 1, runs a stubbed skill five times with five feedback votes, checks promotion and scheduler eligibility without dispatch, and verifies a pending approval survives service reconstruction. It does not call an LLM or CLI, start the Bridge, alter the configured vault, or add metrics.

## Local UI checks

Start the Bridge from the repository:

```bash
source .venv/bin/activate
python -m cortexos init-vault ./vault
python -m cortexos token
python -m cortexos serve
```

Open <http://127.0.0.1:8765/> and paste the token. `python -m cortexos token` prints the same local secret when needed. To inspect API responses from another terminal:

```bash
CORTEXOS_BRIDGE_TOKEN="$(python -m cortexos token)"
curl -fsS http://127.0.0.1:8765/api/status -H "Authorization: Bearer $CORTEXOS_BRIDGE_TOKEN"
curl -fsS http://127.0.0.1:8765/api/voice/status -H "Authorization: Bearer $CORTEXOS_BRIDGE_TOKEN"
curl -fsS -X POST http://127.0.0.1:8765/api/requests \
  -H "Authorization: Bearer $CORTEXOS_BRIDGE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":"list skills"}'
curl -fsS -X POST http://127.0.0.1:8765/api/requests \
  -H "Authorization: Bearer $CORTEXOS_BRIDGE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":"metric audience"}'
```

The first request uses Tier 1. The metric request reads `vault/metrics/audience.toml` when present. Add a real sourced value there and refresh the HUD; unknown values remain blank. An unauthenticated `/api/status` request should return HTTP 401.

### Voice and Tier 2

Use the HUD microphone to say “list skills.” Confirm the local Whisper transcript appears, the Bridge returns the Tier 1 result, and Kokoro speaks it. Then submit text and click **Speak with local Kokoro**. Confirm browser/OS speech is offered only as an explicitly labeled choice after local failure. No audio file should be written to the vault.

Tier 2 requires a configured and running OpenAI-compatible fast model. Enable it in `config/cortexos.toml`, restart the Bridge, and ask a short factual question. Check its receipt reports tier 2. With Tier 2 disabled, do not expect a Tier 2 response.

### Action gate and recovery

If Tier 2 classification is configured, submit an unscoped request that asks for an outward-facing action. Otherwise any unscoped Tier 3 request is conservatively held because its risk is unknown. Confirm it appears in **Pending approvals**, persists in `vault/state.json`, and remains pending after a Bridge restart. Reject it and confirm the decision receipt. Never approve a real send, spend, or publish operation just to test the gate.

Risk comes from the matched skill's `risk_level` and `approval_required` fields, or a structured classifier result for requests without a skill. Missing/invalid skill risk metadata becomes `unknown`. Test future side-effect skills by setting `risk_level` to `external_action` or `financial` and `approval_required` to true; each run must pause even when the skill has approved promotion. The starter skills do not connect to external services.

With either installed CLI, choose its backend in the HUD and open a PTY. Confirm live output and input, interrupt a harmless task, refresh the existing-session list, and reattach while the session is running. Close the session; check `vault/receipts/` for a terminal summary containing backend, times, byte counts, and exit code. No raw terminal text is included. A Bridge restart ends PTY processes; receipts remain inspectable but cannot restore a dead process.

### Skill vote and scheduler

In the HUD, run a harmless starter skill against suitable notes or a prompt. Mark only a satisfactory result successful. Repeat until there are five successful feedback receipts for five distinct run IDs. The UI should then allow promotion review; approve the review separately. Votes and promotion decisions are in `vault/receipts/`.

For a scheduler integration check, add one enabled job for a promoted, read-only starter skill to `vault/automations.toml`. Confirm its normal request, output, and receipt are created; then immediately disable it. The scheduler records a claim before dispatch, preventing silent replay after restart. Use the automated dry-run for checking eligibility without executing the job.

### Metrics and layout

Edit `vault/metrics/audience.toml` or `usage.toml` with real values and source notes. Confirm the HUD displays each value, unit, date, and source; a blank value should show as an em dash. Reorder and hide a HUD panel, reload the page, and confirm the layout persists in that browser's local storage.

If using Obsidian, enter the same Bridge token in plugin settings. Run a command, use local voice, open the terminal, and inspect skill promotion state. The plugin and HUD share the Bridge and vault; the Obsidian PTY can be used while its modal remains open.
