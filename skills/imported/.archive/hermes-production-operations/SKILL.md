# hermes-production-operations

# Hermes Production Operations

24/7 hardening + incident response for the Hermes gateway on this host (headless Debian, user `hermes`, NO sudo, systemd **--user**). Gateway = `hermes-gateway.service` (user unit, ExecStart `python -m hermes_cli.main gateway run`); DB = `~/.hermes/state.db` (SQLite WAL). One surface per HERMES_HOME — a second Hermes surface (desktop/dashboard GUI) writing state.db while the gateway runs is the corruption trigger seen in production.

## Inventory (already deployed — reuse, don't rebuild)
- `~/.hermes/scripts/hermes_health.py` — watchdog. Default/watch: prints anomalies ONLY (empty = healthy). `--report`: RPM, api_calls, latency, db/wal MB, telegram, errors. Cron `hermes-health-watch` (every 15m, no_agent, deliver origin, silent when clean).
- `~/.hermes/scripts/hermes_maintain.py` — daily 4:10 cron `hermes-db-maintenance`: integrity_check → FTS rebuild on failure → wal_checkpoint(TRUNCATE) → VACUUM if freelist>15% & >100MB → `hermes backup --quick -l daily` (Hermes' own snapshots → state-snapshots/, self-pruning) → stale git index.lock (>1h, no git proc) → archive debris (recovery-*, state.db.corrupt-*, state.db.malformed-* older than 1 day: tar.gz → verify → remove, NEVER delete). Log: `~/.hermes/logs/maintenance.log`.
- `~/.hermes/scripts/verify_gateway_restart.sh` — detached restart + self-verify (see safe restart).
- Unit `/home/hermes/.config/systemd/user/hermes-gateway.service`: Restart=always, **StartLimitIntervalSec=600 + StartLimitBurst=8** (was 0 = unbounded loop), RestartForceExitStatus=75 (75 = deliberate TEMPFAIL), KillMode=mixed, TimeoutStopSec=60, ExecReload=SIGUSR1.

## Quick recon (one batch)
```
systemctl --user cat hermes-gateway.service; systemctl --user show hermes-gateway -p MainPID -p NRestarts
cat ~/.hermes/gateway.pid; pgrep -f "hermes_cli.main gateway run"   # pidfile pid must == MainPID and be alive
python3 -c "import sqlite3;c=sqlite3.connect('file:/home/hermes/.hermes/state.db?mode=ro',uri=True);print(c.execute('PRAGMA integrity_check').fetchone()[0], c.execute('PRAGMA journal_mode').fetchone()[0], c.execute('PRAGMA busy_timeout').fetchone()[0])"
du -sh ~/.hermes/* | sort -rh | head   # growth = recovery-*/state.db.corrupt-*/state-snapshots/ debris
```
Facts: state.db already WAL + busy_timeout=5000 + synchronous=FULL. `messages.token_count` is **never populated** → TPM from DB impossible; derive RPM/calls/latency from gateway.log lines `response ready: platform=telegram ... time=Xs api_calls=N` (time-filter by line-prefix timestamp). `gateway-exit-diag.log` = JSONL, timestamps **UTC** (host is EAT, UTC+3) — parse with `calendar.timegm`. errors.log mixes WARNING + ERROR — count only lines containing ` ERROR `, exclude `homeassistant` (known broken DNS on homeassistant.local → permanent reconnect noise).

## Token-cost knobs (hermes config set — verify with config get)
- `agent.reasoning_effort` max→**medium** (biggest saver)
- `model.rate_limit_delay 0.3` (client-side pacing; api_max_retries=3 + 1.5x backoff cap 5s already default)
- `auxiliary.free_only true` (blocks the paid OpenRouter aux fallback lane — seen firing with gemini-3.6-flash) — **WARNING: this knob silently kills aux VISION.** With free_only=true and only a paid vision SKU available, `vision_analyze` fails `No LLM provider configured for task=vision`; repeated failures then gate the tool out of the session. If screenshots matter, set `auxiliary.free_only false` + explicit `auxiliary.vision.provider openrouter` + `auxiliary.vision.model google/gemini-3.6-flash` (verified at resolver level: `resolve_provider_client('openrouter', model=..., is_vision=True)` → client OK). Config/toolset changes apply next session; mid-session fallback = direct OpenRouter vision call (base64 → chat completions).
- `fallback_model.provider openrouter` + `fallback_model.model deepseek/deepseek-chat` (429/529/503 failover; needs OPENROUTER_API_KEY)
- `updates.pre_update_backup true`
- No native auto model-escalation (`agent.service_tier` is a stub) — cheap default + manual /model switch is the ceiling.

## Safe gateway restart (does NOT kill your own run)
Never `systemctl --user restart` from inside a gateway-run agent — the agent's own process dies mid-turn, final reply lost. Schedule detached instead (escapes the gateway cgroup, survives SIGTERM/SIGKILL):
```
systemd-run --user --on-active=120s --unit=gw-restart-verify /bin/bash ~/.hermes/scripts/verify_gateway_restart.sh
```
Verify script: restart → sleep 25 → pgrep count + MainPID + telegram getMe → sendMessage confirmation via bot token from `.env` (never print the token). Config changes (fallback_model etc.) apply on restart.

## Pitfalls
- **Unpinned agent cron + global model drift = scheduler refuses to run (guard #44585).** If the global inference config changed since a cron job was created (e.g. `provider gemini -> deepseek`, `model gemini-3.1-flash-lite -> deepseek-v4-flash`) and the job is NOT pinned, its run fails with `RuntimeError: Skipped to prevent unintended spend: global inference config drifted since this job was created ... To run on the new config, pin it explicitly`. The `cronjob` TOOL's update does NOT expose model/provider (passing them yields `No updates provided`); pin via the CLI: `hermes cron edit <job_id> --model <model> --provider <provider>` — verify with `grep -A8 '"<job_id>"' ~/.hermes/cron/jobs.json` (fields `model`/`provider`). Check `cronjob action=list` for `last_status: error` jobs and read the run output in `~/.hermes/cron/output/<job_id>/<run>.md` for the exact error before touching anything.
- **Cron agents HALLUCINATE completed side-effects (hit 2026-08-12).** A no_agent=false cron told to post to X returned success with a plausible "tweet text" and finished `status: ok` — but made ZERO tool calls (API calls: 0), posted nothing, and the claimed content was factually wrong. The final-response-is-the-deliverable pattern makes action crons self-report success without doing the work. Hardening for ANY cron whose job is a side-effect (post, send, write remote): the prompt must demand (a) the action via an actual terminal/file tool call, (b) the returned artifact ID (tweet id / message id / file path) quoted from tool output, (c) explicit `FAILED: <error>` reporting when the tool errors, and (d) a log append via file tools. Verify from the parent session: check the real system (e.g. `xurl /2/users/<id>/tweets`) — a cron's `last_status: ok` proves only that the LLM finished, not that the side-effect happened. Add domain fact-rules to action-cron prompts when the model can invent plausible-but-wrong claims (e.g. "n≤18 routes to classical; never claim quantum was chosen for a small problem").
- cronjob schedule `15m` = **one-shot**; recurring needs `every 15m`.
- no_agent watchdog cron: script must print NOTHING when healthy (empty stdout = silent delivery), anomalies only.
- FTS warning `Persisted transcript lagged live cached history ... (possible FTS write corruption)`: 1–2 events = benign async-flush lag; burst ≥3/15m together with `file is not a database` / `database disk image is malformed` = real corruption.
- FTS rebuild (only on integrity failure): `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')` and same for `messages_fts_trigram`.
- **Google OAuth dies with `invalid_grant: Token has been expired or revoked`** → re-auth via google-workspace `setup.py` (`--auth-url` → user pastes the `localhost:8080/?code=...` redirect URL → `--auth-code` → `--check-live`; `--check` alone only refreshes, it doesn't prove the client works). If re-auth passes but `--check-live` / `google_api.py` fail with `module 'googleapiclient.discovery_cache' has no attribute 'autodetect'`, the installed google-api-python-client tree is half-installed (`discovery_cache/` empty in the Google venv) — NOT a code bug, do not patch `cache_discovery=False`. Repair: `uv pip install --python /home/hermes/.hermes/google-venv/bin/python --force-reinstall google-api-python-client` (the venv is uv-managed, there is NO pip binary). Details: `references/google-oauth-reauth-and-client-repair.md`.
- `hermes config set` from scripts: export HERMES_HOME first; invoke as `~/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main config set K V`.
- Latency stats are polluted by cron/background runs — filter `platform=telegram` lines.
- Corruption debris: Hermes' recovery renames the bad db (`state.db.corrupt-*`, `state.db.malformed-*`) and writes `recovery-*/` copies — archive (compress) rather than delete; they are the only copy of pre-recovery data.

## References / scripts
- `references/corruption-incident-2026-08-08.md` — full incident timeline + artifact anatomy.
- `scripts/hermes_health.py`, `scripts/hermes_maintain.py`, `scripts/verify_gateway_restart.sh` — canonical copies to redeploy if `~/.hermes/scripts/` is lost.
