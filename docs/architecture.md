# CortexOS architecture

## Decisions

1. **One Bridge for both interfaces.** The HUD and Obsidian plugin are thin clients so routing, policies, and audit logs stay consistent. The Bridge binds to localhost as the default trust boundary.
2. **Skills are files.** A skill is readable Markdown with a JSON manifest beside it. This keeps skills editable in Obsidian and usable by Claude Code, Codex, and the CortexOS runner without vendor-specific storage.
3. **Routing begins conservatively.** Exact local commands take tier 1. Short factual questions can use tier 2 when enabled. When enabled, the configured small model also classifies ambiguous no-skill requests as `QUICK` or `WORK`; only an exact `QUICK` can select tier 2. Unmatched, substantial, or unclear requests go to tier 3. Jev or another compatible classifier can replace the OpenAI-compatible local model without changing the Bridge API.
4. **No secrets in the vault.** Provider credentials should remain in each provider's normal local credential store or environment. The example config is ignored once copied to `config/cortexos.toml`.
5. **Markdown is the persistence format.** Requests use one predictable file per ID, while outputs and receipts are date-partitioned. All remain human-readable and searchable without a knowledge graph or hosted database.
6. **Human approval is a policy boundary.** Skills that can send, spend, or publish set `approval_required`. The Bridge pauses them and only executes after an explicit approval request.
7. **Metrics are user-supplied.** `metrics/` is prepared for social, usage, finance, and other metrics. No sample number is represented as a real user value.
8. **Promotion and automation are explicit.** Only one user confirmation per verified completed run is accepted; five successes make a skill eligible for a distinct promotion review. The scheduler runs only enabled jobs for approved skills and still submits through the same approval policy.
9. **HUD customization is local and reversible.** Panel order and visibility are stored in the browser's local storage and do not alter vault content or product code.

## Request lifecycle

`POST /api/requests` → log request → match skill / choose tier → optionally pause → execute provider → write output and receipt. Tier 1 commands need no model. Tier 2 is read-only and OpenAI-compatible. Tier 3 invokes the chosen local agent CLI with a generated prompt containing the skill instructions.

## Modular layers

- `apps/hud`: locally rearrangeable/hideable panels, status, tabs, skill buttons, automation review, metrics, and terminal launch/stream view.
- `apps/obsidian-plugin`: command modal, voice, PTY terminal, and automation promotion review inside the vault, calling the Bridge.
- `cortexos/voice.py`: local Whisper STT and Kokoro TTS adapters. Voice input calls the same Bridge request service as buttons.
- `cortexos/scheduler.py`: lightweight local scheduler; no jobs run unless added and enabled in the vault, and each skill must have an approved promotion ledger.
- `cortexos/connectors`: opt-in data sources. Any write-capable connector must expose a preview/approval action.

These modules can be added without moving the skill catalog or vault contract. Interactive CLI sessions use a local POSIX PTY and WebSocket; background Tier 3 requests stream process output over a separate request WebSocket and accept an interrupt signal. Terminal commands are fixed by local config to the selected Claude/Codex executable; web clients cannot choose arbitrary commands or working directories.
