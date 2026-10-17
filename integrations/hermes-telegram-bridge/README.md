# Hermes ⇄ CortexOS Telegram bridge

Makes the existing Hermes Telegram bot a first-class CortexOS front door:
Telegram commands become durable queued tasks, run through the normal agent
pipeline, and results/approvals are reported back to the same chat.

No new bot, no second poller, no new credentials. The Hermes gateway keeps
sole ownership of Telegram polling; this integration only adds (a) a gateway
plugin that claims its own trigger commands and (b) a loopback ingress
endpoint on CortexOS plus a notifier for task transitions.

## Architecture

```
Telegram ──▶ hermes-gateway (only poller)
               │  group-0 plugin handler (first match wins)
               │  POST https://127.0.0.1:8765/api/telegram/inbound
               │      header X-Cortex-Telegram-Bridge: <scoped token>
               ▼
           CortexOS ingress (allowlist + rate limit + dedupe)
               │  origin="telegram", dedupe_key="tg:<chat>:<message>"
               ▼
           task queue ──▶ dispatch gate (pause/resume) ──▶ agent/skill run
               │
               ▼
           TelegramNotifier ──▶ `hermes send --to telegram:<chat>`
                                (approval / completed / failed / cancelled / rejected)
```

- **Ingress token**: `/etc/cortexos/telegram-bridge.token` (0640 root:hermes),
  separate from the main bearer token, checked only on `/api/telegram/inbound`.
- **Allowlist**: same source as Hermes core — `TELEGRAM_ALLOWED_USERS`
  (profile-scoped env, then process env, then `$HERMES_HOME/.env` fallback).
  Fail-closed: empty allowlist ⇒ nothing is accepted.
- **Dedupe**: Hermes update receipts + CortexOS `dedupe_key` make retried
  updates collapse into one task.
- **Pause/resume**: `state/dispatch.json`; gates NEW claims only (running work
  is untouched), survives restarts.
- **Notify state**: `state/telegram-notify.json`; one send per (task, stage),
  swept every 30s and event-driven, survives restarts.

## Command namespace

| Command | Where | Notes |
| --- | --- | --- |
| `/cx help` `/cx status` `/cx agents` `/cx tasks` `/cx taskstatus <id>` `/cx assign <agent> <prompt>` `/cx task <prompt>` `/cx cancel <id>` `/cx approve <id>` `/cx reject <id>` `/cx pause` `/cx resume` | plugin → CortexOS | never collides with Hermes built-ins |
| `/task`, `/tasks`, `/agents`, `/assign`, `/taskstatus`, `/cancel`, `/reject` | plugin → CortexOS | bare aliases; free of Hermes built-ins |
| `/approve <uuid>` | plugin → CortexOS | only with a full UUID; bare `/approve` stays Hermes' own |
| everything else | Hermes core | untouched, byte-for-byte |

## Files

- `__init__.py` — the gateway plugin (`register(ctx)` + handler factory).
- `plugin.yaml` — plugin manifest.
- `test_harness.py` — offline dispatcher harness (real PTB Application, faked
  network boundaries). Run on the gateway host with the Hermes runtime:
  `CORTEXOS_SRC=/opt/cortexos/app /home/hermes/.hermes/hermes-agent/venv/bin/python test_harness.py`
- `probe_tier1.py`, `e2e_phase_a.py`, `e2e_phase_b.py` — live E2E scripts as
  actually executed against the deployed service (phase A passed checks 1–11
  and then hit its own restart-poll bug; phase B is the idempotent
  continuation that completed the restart-durability and approval checks).

CortexOS side lives in `cortexos/telegram.py` (+ `[telegram]` config section,
`POST /api/telegram/inbound`, queue dispatch gate, tests in
`tests/test_telegram.py`).

## Deploy / enable

Backend: normal CortexOS deploy (`config/cortexos.toml` gets `[telegram]
enabled = true`; `/etc/cortexos/` owned root:hermes).

Plugin:

```sh
DEST=/home/hermes/.hermes/plugins/cortexos-bridge   # install files, chown hermes
runuser -u hermes -- env HOME=/home/hermes /home/hermes/.local/bin/hermes \
    plugins enable cortexos-bridge
```

`plugins enable` nudges the running gateway over its control socket
(`reload-plugins`) which hot-rewires handlers — **no gateway restart** (a
restart would interrupt live work). Verify: `hermes plugins list` shows
`cortexos-bridge enabled`, gateway status stays active.

## Rollback

1. `runuser -u hermes -- hermes plugins disable cortexos-bridge` (or remove
   `~/.hermes/plugins/cortexos-bridge`) and re-run the reload nudge.
2. Redeploy the previous CortexOS build (or revert `cortexos/telegram.py`
   wiring); the feature is inert while `[telegram] enabled = false` and while
   no plugin is loaded.
3. Optional state cleanup: `state/dispatch.json`, `state/telegram-notify.json`.
