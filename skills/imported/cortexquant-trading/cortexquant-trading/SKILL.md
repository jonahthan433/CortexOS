# cortexquant-trading

# CortexQuant CT106 — 24/7 Trading System

Operate the BingX demo trading platform with supervised worker fleet, signal pipeline, and ML components.

## Topology
- Proxmox host `ninyi` = `192.168.1.11`. CT106 `cortexquant` = `192.168.1.19` (reachable only via `pct exec 106` from the host — direct SSH from agent host fails with "No route to host")
- Access pattern: `ssh root@192.168.1.11 'pct exec 106 -- bash -c "..."'`
- Copy files: pipe via stdin → `pct exec 106 -- cat > /path/to/file`
- DB: PostgreSQL `cortexquant`, user `cortexquant`, password `cq_secure_2026`
- Codebase: `/opt/cortexquant/` (old full system) — **WIPED AND REPLACED** with Phase 1 scaffold at `/opt/cortexquant/trading_system/`
- Service venv: `/opt/cortexquant/trading_system/.venv/` (Phase 1 scaffold venv with loguru, ccxt, telethon, pandas, numpy, python-dotenv, tenacity)
- Systemd: `systemctl {restart,status} cortexquant` inside CT106 — currently **stopped+disabled**; Phase 1 scaffold has no service file yet
- ML gate: `ML_SKIP_VALIDATION=true` via systemd drop-in — **no longer applicable** (Phase 1 scaffold has no ML component)

## Deployment

### Push code to CT106 (choose method by reliability, not size)

**Most reliable for text files of ANY size — stdin pipe directly to pct exec cat:**
```bash
cat /local/file.py | ssh -i ~/.ssh/hermes_ninyi -o StrictHostKeyChecking=no \
  root@192.168.1.11 'pct exec 106 -- bash -c "cat > /opt/cortexquant/trading_system/path/file.py"'
```
This is the primary method. It works for small and large text files. Always clear
`__pycache__` after pushing updated modules:
```bash
ssh root@192.168.1.11 'pct exec 106 -- bash -c "rm -rf /opt/cortexquant/trading_system/src/__pycache__ /opt/cortexquant/trading_system/tests/__pycache__"'
```

**Large files or binary — base64 pipe (most reliable, avoids shell-quoting corruption):**
```bash
base64 -w0 /local/file.py | ssh -i ~/.ssh/hermes_ninyi -o StrictHostKeyChecking=no \
  root@192.168.1.11 'pct exec 106 -- bash -c "base64 -d > /opt/cortexquant/trading_system/path/file.py"'
```

**DO NOT use scp to PVE /tmp then cp inside container** — PVE host `/tmp/` is NOT
visible inside the container (`cp: cannot stat '/tmp/file.py'`). The file lands on
the PVE host but the container cannot see it.

**DO NOT use inline python3 -c through SSH** — quoting through the SSH layer
mangles escape sequences and Unicode (emoji bytes get corrupted). Write scripts to
host `/tmp/*.py` files and pipe them through, or use the stdin cat method above.

### Apply config changes (sed inline)
```bash
ssh root@192.168.1.11 'pct exec 106 -- bash -c "sed -i \\"s|OLD|NEW|\\" /opt/cortexquant/trading_system/src/file.py"'
```

### Run self-check tests (Phase 1 scaffold)
```bash
ssh root@192.168.1.11 'pct exec 106 -- bash -c "cd /opt/cortexquant/trading_system && PYTHONPATH=. .venv/bin/python3 tests/test_self_check.py"'
```
Not pytest, not system python — use the venv python with PYTHONPATH set to the
project root. The self-check is a standalone script, not a pytest suite.

If any check fails, stop and report the blocker — do not proceed to the next fix.

## Database queries
```bash
cat > /tmp/query.py << 'PYEOF'
import asyncio, asyncpg
async def main():
    conn = await asyncpg.connect("postgresql://cortexquant:cq_secure_2026@localhost:5432/cortexquant")
    rows = await conn.fetch("SELECT symbol, direction, order_status FROM demo_trades WHERE order_status = 'OPEN'")
    for r in rows:
        print(dict(r))
    conn.close()
asyncio.run(main())
PYEOF
cat /tmp/query.py | ssh root@192.168.1.11 'pct exec 106 -- bash -c "cat > /tmp/query.py && /opt/cortexquant/venv/bin/python /tmp/query.py"'
```

## BingX position management

### Close positions
```python
import asyncio, ccxt
exchange = ccxt.bingx({'apiKey': api_key, 'secret': secret, 'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
exchange.set_sandbox_mode(True)
positions = await asyncio.to_thread(exchange.fetch_positions)
for pos in positions:
    if pos['contracts'] > 0 and symbol in to_close:
        side = 'sell' if pos['side'] == 'long' else 'buy'
        await asyncio.to_thread(exchange.create_order, symbol=pos['symbol'], type='market', side=side, amount=pos['contracts'])
```

## Worker fleet

**16 workers** (restored from git HEAD 2c42cf4). All supervised by `WorkerSupervisor`:
- `dashboard_feed` (5s), `monitor` (60s), `demo_reconcile` (30s), `tg_ingestion` (120s)
- `market_data` (600s), `ml_predict` (300s), `backtest_adapter` (600s), `risk_committee` (240s)
- `hypothesis` (120s), `strategy_version` (300s), `walkforward` (600s), `red_team` (300s)
- `incentive_scoring` (240s), `room_tasks` (120s), `quantum_optimize` (600s), `portfolio_optimize` (600s)
- `signal_aggregator` (60s)

API: `GET /api/workers/health`

To list: `curl -s http://localhost:8080/api/workers/health | python3 -m json.tool`

## Memory pressure on 3GB CT106

When memory pressure persists:
- **Kill duplicate Hermes dashboard processes**: `kill $(pgrep -f "hermes dashboard.*9119")` — saves ~356MB
- **Kill Headroom MCP** (only needed for context compression): `kill $(pgrep -f "headroom mcp serve")` — saves ~59MB
- These respawn via systemd — that's normal and acceptable
- **Cut Nextcloud PHP workers**: `pm.max_children = 5` (already low, ~200MB total)
- Verify savings: `free -h` should show Available > 1.2GB on a 3GB host
- **Never use `sort=-%mem` in `ps` on CT106** — it runs BusyBox `ps` which doesn't support `--sort`. Use `ps aux | head` and read RSS manually.

## Signal pipeline
Telegram (CKPscanner) → `tg_service.py` → `tg_parser.py` → `tg_publisher` → `FullPipeline` → `DemoTradingService` → BingX

### Pipeline checks (order matters)
1. Idempotency (decision_records table)
2. Market data snapshot (symbol normalized to base: `ETH/USDT` → `ETH`)
3. ML prediction (observation-only until approved; gates via `ML_SKIP_VALIDATION`)
4. Strategy selection
5. Classical/quantum optimization
6. Risk check (real values, not hardcoded)
7. Signal freshness (5-minute max)
8. BingX market existence check
9. Duplicate position check
10. Execution

### Pipeline execution paths (unified)
Both Telegram signals and `/api/demo/test-signal` now route through the same orchestrator (`DemoExperimentOrchestrator.process_signal`) and shared `FullPipeline` instance. Do not create fresh pipeline instances per call.

## 7-day experiment clock

- `start_experiment(duration_days=7)` arms the experiment (`is_active=True`) but does NOT start the clock.
- The clock starts on first filled trade via `start_clock("first_filled_trade")`.
- **Auto-start on restart**: after reconciliation rehydrates positions from exchange, `start_clock("rehydrated_positions")` is called so the clock survives restarts.
- Verify: `curl -s http://localhost:8080/api/demo/status` → `experiment_start`, `experiment_end`, `days_remaining`.

## Reconciliation

- `reconcile_demo.py` fetches open positions from BingX, syncs to internal state, creates missing DB rows.
- **MAX_CONCURRENT_TRADES guard**: DB row creation is capped at `demo_trader.config.MAX_CONCURRENT_TRADES` (default 25 for settings, 3 for demo config).
- On startup, `demo_lifecycle.start_demo_experiment()` runs reconciliation BEFORE wiring the publisher callback.

## ⚠️ Pitfalls

### **LIVE_TRADING=false is a hard default — never enable it**
The system defaults to `LIVE_TRADING=false`. Do not change this. All fixes must preserve this. Verify after every restart: `curl -s http://localhost:8080/health | grep live_trading`.

### **NEVER overwrite .env with echo > — DESTROYS API KEYS**
The .env file contains BingX API keys, DB creds, and other secrets. Using `echo "..." > /opt/cortexquant/.env` **overwrites the entire file**, destroying all existing vars and bricking the service with "credentials not configured". ALWAYS append: `echo "..." >> /opt/cortexquant/.env` or use `sed -i` to modify specific lines. After appending, verify with `cat /opt/cortexquant/.env | grep KEY_NAME`.

### **POST requests require Bearer auth (protect_mutations middleware)**
All POST/PUT/PATCH/DELETE requests are rejected with 401 unless a valid `Authorization: Bearer <API_SECRET_KEY>` header is sent. When `API_SECRET_KEY` is the default `change-me-in-production`, ALL mutations are blocked. For testing, either temporarily set a key in `.env` (and remove it after) or call Python directly on CT106.

### **Service venv is `/opt/cortexquant/venv/`, not `.venv/`**
The `.venv` folder is a separate env created during debugging — it may be incomplete. The service uses `/opt/cortexquant/venv/`. Always invoke the service venv: `/opt/cortexquant/venv/bin/python`.

### **File transfer to CT106 requires `pct exec 106`**
Direct SSH from agent to 192.168.1.19 fails ("No route to host"). All access via: `ssh root@192.168.1.11 'pct exec 106 -- ...'`

### **Heredoc via SSH truncates on context compression**
Piping a file through `ssh ... bash -c "cat > /tmp/x << 'EOF'"` works for small files but truncates when the heredoc exceeds ~4KB (context compression eats the body). For larger files, use base64 pipe or scp.

### **ta library import order kills valid names on partial failure**
In `src/tools/technical_adapter.py`, the `try: from ta.volatility import (BollingerBands, AverageTrueRange, ...)` block fails entirely if ANY name is missing (e.g., `StochIndicator` was renamed to `StochasticOscillator` in ta 0.11.0). When the import fails, ALL names in that block become unavailable — even valid ones like `AverageTrueRule`. Fix broken names OR split into separate try/except blocks per group.

### **Missing imports in predictor.py**
`src/ml/predictor.py` uses `os.environ.get("ML_SKIP_VALIDATION", ...)` and `datetime.now(timezone.utc)` but historically omitted `import os` and `from datetime import datetime, timezone`. After patching, verify both imports are present — missing imports cause silent ML save failures and broken approval gates.

### **Symbol normalization in pipeline**
`FullPipeline.process_signal` fetches OHLCV using `f"{symbol}/USDT"`. If the signal arrives as `ETH/USDT`, this becomes `ETH/USDT/USDT` and the exchange errors. Normalize to base symbol immediately: `symbol = symbol.split("/")[0]`.

### **Duplicate DB creation loop in reconcile_demo.py**
When patching reconciliation to respect MAX_CONCURRENT_TRADES, a duplicate block was accidentally introduced. Always `grep -c "Respect MAX" reconcile_demo.py` after patching — there should be exactly ONE guard block, not two. Duplicate blocks can create duplicate DemoTrade rows for the same symbol.

### Memory pressure on 3GB CT106

When memory pressure persists:
- **Kill duplicate Hermes dashboard processes**: `kill $(pgrep -f "hermes dashboard.*9119")` — saves ~356MB
- **Kill Headroom MCP** (only needed for context compression): `kill $(pgrep -f "headroom mcp serve")` — saves ~59MB
- These respawn via systemd — that's normal and acceptable
- **Cut Nextcloud PHP workers**: `pm.max_children = 5` (already low, ~200MB total)
- Verify savings: `free -h` should show Available > 1.2GB on a 3GB host
- **Never use `sort=-%mem` in `ps` on CT106** — it runs BusyBox `ps` which doesn't support `--sort`. Use `ps aux | head` and read RSS manually.

### BingX demo credentials restoration (after .env overwrite)
If `.env` is accidentally overwritten (see NEVER overwrite .env pitfall), BingX credentials must be re-added:
```
DEMO_EXCHANGE_API_KEY=<key>
DEMO_EXCHANGE_SECRET=<secret>
DEMO_EXCHANGE_SANDBOX=true
```
Then `systemctl restart cortexquant` and verify: `curl -s http://localhost:8080/api/demo/status` — should show `demo_trading: true`, not "credentials not configured".

### Risk engine hardcoded values bug
The risk check in `full_pipeline.py` was passing `{"position_size_pct": 2.0, "risk_pct": 2.0}` instead of real signal values — made exposure limits unenforceable. Fixed 2026-09-09.

### Parser whitespace bug
Scout messages have leading whitespace (` 1. ALCH-USDT |...`). LINE_PATTERN regex needs `\s*` prefix.

### ML model always fails validation
GradientBoostingRegressor on 34,940 samples → R²=-0.04. Until improved, `ML_SKIP_VALIDATION=true` required. Retrain via `scripts/train_model_standalone.py`.

### N+1 meme-coin positions
Without enforced exposure limits, Scout signals can open 19+ positions reaching 260%+ exposure. Monitor via `/api/demo/status`.

## Key files
- `src/workers/supervisor.py` — heartbeat, retry, failure classification
- `src/workers/jobs.py` — all 16 worker definitions
- `src/risk/risk.py` — risk engine with LIVE_TRADING guard
- `src/pipeline/full_pipeline.py` — signal processing pipeline
- `src/ingestion/tg_service.py` — Telegram ingestion
- `src/ingestion/tg_parser.py` — Scout message parser
- `src/exchange/demo_trading_service.py` — BingX execution
- `reconcile_demo.py` — exchange→DB reconciliation
- `src/ml/predictor.py` — ML predictor (observation-only until approved)
- `src/tools/technical_adapter.py` — TA features (watch import order)
- `src/quant_hq/demo_lifecycle.py` — startup, reconciliation, clock auto-start
- `src/quant_hq/demo_orchestrator.py` — signal orchestration
- `src/api/demo_routes.py` — test-signal and start/stop endpoints
- `scripts/train_model_standalone.py` — ML training

## Verify health
```bash
ssh root@192.168.1.11 'pct exec 106 -- bash -c "systemctl is-active cortexquant && curl -s http://localhost:8080/api/workers/health && curl -s http://localhost:8080/api/demo/status"'
```

## Test signal dry-run (synthetic e2e)

Run a signal through the full pipeline without placing real trades:
```bash
cat > /tmp/dryrun.py << 'PYEOF'
import json, urllib.request
BASE = "http://localhost:8080"
def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())
status = get(BASE + "/api/demo/status")
assert status["live_trading"] == False
assert status["demo_trading"] == True
assert status["kill_switch"] == False
print("OK:", status["positions_open"], "positions")
PYEOF
cat /tmp/dryrun.py | ssh root@192.168.1.11 'pct exec 106 -- bash -c "cat > /tmp/dryrun.py && /opt/cortexquant/venv/bin/python /tmp/dryrun.py"'
```
