# quant-trading-experiment

# Quant Trading Experiment

Operate a 12-room, 20-agent quantitative research organization with live demo trading on BingX VST.

## Architecture (stable)

- **CT106 `cortexquant`** = `192.168.1.19`. FastAPI :8080.
- **12 Rooms**: QUANT_RESEARCH, WEIRD_EDGE, MATH_SIGNAL, STRATEGY_ENG, REGIME_DETECTION, BACKTEST, OPTIMIZATION, SELECTION, RED_TEAM, TRADING_JOURNAL, AI_COACH, RISK_COMMITTEE.
- **Signal Flow**: Telegram → Telethon MTProto → Parse → 12-Step Validation → Rooms + Demo → BingX VST.
- **LIVE_TRADING=false** hard default. Demo uses `DEMO_TRADING=true`.

## Critical startup pattern

`await server.serve()` blocks forever. Demo initialization MUST be a background task:

```python
# CORRECT
asyncio.create_task(start_demo_experiment())
await server.serve()

# WRONG — never reached
await start_demo_experiment()
await server.serve()
```

## BingX API quirks

- **Demo balance key is `VST`, not `USDT`**: `balance.get("VST", {}).get("total", 100000)`
- **Perpetual swap symbols**: `AVAX/USDT:USDT` not `AVAX/USDT`. Use `_get_swap_symbol()`.
- **Position side required**: `params={'positionSide': 'LONG', 'leverage': 3}`
- **Sandbox**: `exchange.set_sandbox_mode(True)` — VST only, no real funds.

## Schema dependencies

`tg_parsed_signals.raw_message_id` FK → `tg_raw_messages.id`. Insert raw first, commit, then parsed.

## Signal pipeline liveness — ingestion ≠ parsing

A green systemd unit and a growing `tg_raw_messages` table prove the Telethon
client works. They do NOT prove any signal is being extracted. Before opening any
"why is no trade happening" investigation, check the parse outcome distribution
rather than the process state:

```sql
SELECT ingestion_status, count(*) FROM tg_raw_messages GROUP BY 1;
SELECT date_trunc('day', created_at) AS d, count(*) FROM tg_parsed_signals GROUP BY 1 ORDER BY 1 DESC;
```

A healthy ingester can sit at thousands of rows while the entire signal path
downstream is dead — this system ran for days with raw messages arriving every
~10 minutes and zero parsed signals.

**The upstream channel changes its message format and the parser silently stops
matching.** When `PARSE_ERROR` dominates, do NOT assume the parser is broken and
start rewriting regexes. First classify the raw corpus by shape and by day
(recipe: `references/signal-parser-maintenance.md`) and answer the prior
question: *does the feed currently contain anything parseable at all?* The feed
had degraded to re-posted disclaimer boilerplate every ~10 min plus a
funding-outlier digest — neither is a tradable signal — and the last real scout
list was 8 days old. The correct output there is "no valid signal exists, clock
NOT started", not a rewritten parser and not a stale-signal trade.

State the consequence plainly when the feed is empty:
- Signal max age is 300 s; anything older is `stale_signal` by design.
- Standing rule: never trade historical signals as if live.
- Therefore the 7-day clock cannot start until a fresh parseable signal arrives.

## Risk limits

| Limit | Value |
|---|---|
| Max position | 2% portfolio |
| Max exposure | 15% |
| Max concurrent | 3 |
| Leverage | 3x |
| Stop loss | 2% |
| Take profit | 4% |
| Signal age | 5 min |

## Safety Constraints

- **LIVE_TRADING=false** is a HARD DEFAULT. Never enable without explicit written approval.
- **Never claim profitability from a 7-day sample.** Demo results are not statistically significant.
- **Never promise or imply guaranteed wins / guaranteed returns, on this system or any trading system.** If asked to "guarantee constant wins" or similar, say plainly that no system guarantees returns on real markets (if one did, arbitrage would erase the edge) and redirect to the real target: positive expected value with survivable risk of ruin, validated only after enough trades exist to measure win rate / EV per trade / max drawdown. This is a correctness rule, not a hedge — do not soften it into a caveat and then answer the guarantee question anyway.
- **Never auto-scale position sizes.** Fixed conservative limits only.
- **Never trade historical signals as if live.** Only process signals received after experiment start.
- **Demo credentials stay in server `.env` only.** Never commit or log secrets.
- **Training completion is not model approval, AND training must persist.**
A model is usable only when explicit validation gates pass; otherwise record it
`observation_only` and let the strategy engine fall back to `ckp_baseline`.
Gates that must ALL hold on the validation split: directional accuracy > 0.52,
Spearman IC > 0.02, R² ≥ 0, and MAE no worse than the no-model (`train`-mean)
baseline. Persist the approved/observation-only decision AND the failing gate
values into `models/ml_metadata.json` — "trained" alone is not a record.

**Critical: the `train()` method must call `save()` before returning.** A
`train()` that fits the model in memory but never writes `ml_predictor.pkl` +
`ml_metadata.json` to `MODEL_DIR` loses the trained artefact on every cycle —
the next `load()` finds nothing. After patching `train()`, verify with:
`ls -la /opt/cortexquant/models/ml_predictor.pkl` after a training run. A
trained-but-never-saved model is indistinguishable from an untrained one at
load time; this is the bug that made the pipeline's ML always
observation-only even after the worker trained successfully.
- **Chronological splits only, pooled by timestamp — never per-symbol, never
shuffled.** Splitting each symbol independently lets one symbol's later bars land
in another symbol's training window. The label is the forward return
(`close.pct_change().shift(-1)`), so the final bar per symbol must be dropped
after shifting.
- **Train on the exchange you actually execute on.** The legacy trainer pulled
OHLCV from Binance while demo execution runs on BingX; symbol sets and available
history differ, so the metrics described instruments that were never traded.
BingX sandbox `fetch_ohlcv(..., since=<45d ago>)` returns about 1000 bars
regardless of how far back you ask, so record the ACTUAL period covered rather
than the requested lookback.

## Pitfalls

- main.py blocking: demo must be create_task, not await.
- BingX VST balance, not USDT.
- Swap symbols need `:USDT` suffix.
- FK constraints: raw before parsed.
- Separate-process tests see callback=None — test via server API.
- **`re.match()` anchors at position 0 — real channel lines are indented, so a
  pattern without a leading `\s*` matches none of them.** Test every parser regex
  against the ACTUAL corpus (`SELECT raw_text FROM tg_raw_messages`), never
  against an idealized example typed from memory; the scout lines arrive as
  `" 1. ALCH-USDT | ..."` with a leading space.
- **When a parser has two alternate line patterns, do not assume identical group
  numbering.** `match.group(3)` on a 2-group fallback pattern raises
  `IndexError: no such group`. Normalize both patterns into the same
  `(symbol, data)` tuple in one place instead of indexing groups inline.
- **A crash guarded by a condition that never currently holds is still a live
  bug.** `tg_service._process_message` iterates `for p in parsed:` while the
  variable is `parsed_results` — a `NameError` that never fires only because
  `parse()` currently returns `None` for every message. The moment a real signal
  arrives it raises, rolls back the transaction, and loses BOTH the raw and the
  parsed row. When auditing an ingestion path, trace the branch that runs when
  the happy path finally succeeds, not just the branch executing today.
- **Noise markers beat clever regexes for messages that must never trade.** A
  disclaimer/disclaimer-repost or a funding-outlier digest mentions LONG/SHORT
  and carries `SYMBOL-USDT | RSI15=...` lines, so a naive symbol matcher happily
  emits fake signals from it. Reject on substring markers (`"About these scanner
  lists"`, `"Funding Outliers"`, `"not guaranteed trade calls"`) before line
  parsing, and assert those rejections in the parser's self-check.
- **Scope discipline**: When asked to fix startup, locate the real running server's main.py first. Do not keep building new modules — inspect the deployed code, find the entrypoint, fix the actual blocking call.
- **No systemd unit means the server is exactly one dropped SSH session from
  being dead with no error.** Launching `main.py` by hand (even with
  `setsid`/`nohup`) survives only as long as something keeps the process
  supervised. When an audit or a "why isn't it running" check finds the log
  stopped mid-line with no crash/OOM entry, that absence of a unit IS the root
  cause — write `/etc/systemd/system/cortexquant.service` with
  `ExecStart=/opt/cortexquant/start.sh` and `Restart=on-failure`, don't just
  relaunch by hand again.
**Two config classes can both define risk limits — grep the import in the
file that places orders, not the first `Settings`-looking file.**
  `src/utils/config.py` carries a generic `Settings` class and
  `src/exchange/demo_trading_service.py` carries its own `BingXDemoConfig`.
  If both are live in the same process (not dead code), confirm which one each
  consumer actually imports via `grep 'from.*import.*config\|demo_trader.config'`
  in every file that touches risk/exposure/concurrency logic — including the
  risk engine, the pipeline, and any worker that calls risk methods.
  **When two live config classes disagree, fix by making the risk engine accept
  an optional `config` parameter** (defaulting to the generic one, overridable by
  the demo config) rather than deleting one class. That keeps both consumers
  wired through a single authoritative check while preserving each consumer's
  own limits. See `references/risk-config-consolidation.md` for the exact pattern.
  Dead-code config classes (imported nowhere) should be removed, not patched.
- **A defined settings key is not the same as an enforced control — grep every
  reference before crediting a security setting as active.** `API_SECRET_KEY`
  in `Settings` had zero consumers until an `@app.middleware("http")` bearer-auth
  check was added in `api.py` gating every POST/PUT/PATCH/DELETE (GETs stay open
  for dashboards). Return `JSONResponse(status_code=401, ...)` from that
  middleware, never `raise HTTPException` — middleware exceptions bypass
  FastAPI's handlers and become a 500 instead (see `python-fastapi-hardening`).
  Verify all three cases with curl: no token → 401, wrong token → 401, real
  token from `.env` → 200. `CORSMiddleware(allow_origins=["*"])` still means
  any LAN host can send the request; the bearer check is what actually gates it.
- **The demo kill switch (`demo_trader.kill_switch`, flipped via
  `POST /api/demo/kill-switch`) has no deactivate endpoint** — unlike the
  separate generic `/api/kill-switch/{activate,deactivate}` pair in
  `risk.py`/`api.py`. Once tripped it only resets by restarting the service
  (`systemctl restart cortexquant`), which re-runs `start_demo_experiment()`
  and starts a *new* experiment run. Never POST that endpoint during testing
  without immediately restarting the service afterward, or the demo trader
  stays halted with no code-level way to resume it.
- **A room method existing with the right name is not the same as it computing
  anything real — grep for its actual callers and body before crediting a
  feature as implemented.** `BacktestRoom.run_backtest()` in `rooms.py` returns
  a hardcoded all-zero dict (sharpe/win_rate/etc all 0) and never calls the real
  `src/backtest/backtester.py::Backtester` class that already does point-in-time
  simulation with fees/slippage/Sharpe/drawdown. Before proposing to "add
  backtesting" or "wire up X", check whether the real engine already exists and
  is just unwired — that's a smaller diff than writing a new one, and the
  correct fix is calling the existing class, not building another.
- **Red Team and Risk Committee are already hard promotion gates** —
  `ChiefQuantDirector.promote_research()` calls `_check_veto()` before allowing
  a transition to `PRODUCTION_CANDIDATE`/`LIVE_ELIGIBLE`, and either room's
  `VETO` message flips the experiment to `REJECTED` and blocks promotion. If
  asked to "make Red Team/Risk Committee a hard gate", verify this isn't
  already true before writing new veto-enforcement code.
- **`ResearchTask` rows get created by every room (`create_task`,
  `delegate_to_room`) but nothing ever consumes the PENDING queue.** There is
  no worker loop that picks up pending tasks, runs them, and marks them
  RUNNING/COMPLETED/FAILED outside of tests — `ChiefQuantDirector.prioritize_queue()`
  only reads the queue, nothing drains it. Any "make the rooms actually run
  autonomously" ask needs a task-consumer loop as the actual missing piece,
  not more room/agent scaffolding (which already exists for all 12 rooms/24 agents).
- **Supervision house style is `asyncio.create_task` loops inside the FastAPI
  process, not Celery** — `celery==5.4.0` sits in `requirements.txt` unused.
  Adding Celery for background workers means a broker + beat + separate worker
  process to supervise themselves — more moving parts than one more
  `asyncio.create_task` loop next to the existing `demo_lifecycle.py` /
  `_monitor_experiment()` pattern. Stay with asyncio tasks + a persisted
  heartbeat/status table unless throughput genuinely outgrows a single process.
- **For a large multi-phase build request (new worker architecture, incentive
  system, dashboard overhaul, etc.), audit the real filesystem for what already
  exists before scoping phases.** Pull the actual source tree first (rooms,
  schema, events, dashboard, backtester) via SSH/tar rather than assuming from
  memory notes, then present the phased plan against what's real vs. stubbed —
  this cuts scope by however much already exists and avoids rebuilding working code.

## Demo activation flow

The demo experiment starts via the API with the Bearer token from `.env`:
```bash
TOKEN=$(grep '^API_SECRET_KEY=' /opt/cortexquant/.env | cut -d= -f2)
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/demo/start
```
This calls `demo_trader.initialize(api_key, secret)` → loads BingX VST markets → creates an `ExperimentRun` row → sets `demo_trader.is_active = True` and `demo_trader.experiment_end` to 7 days out. **The start call must happen AFTER the server is running** (main.py calls `start_demo_experiment()` as a background task, but if `DEMO_TRADING` was off at boot, a manual restart via the API is needed).

After start, verify markets loaded and balance:
```bash
curl -s http://localhost:8080/api/demo/status | python3 -m json.tool
# Expect: "is_active": true, "experiment_end" set, "days_remaining": 6-7
```

### Reconciliation Overflow Pitfall

The startup reconciliation script (`reconcile_demo.py`) creates `DemoTrade` DB
rows for every open exchange position. If reconciliation runs when more than
`MAX_CONCURRENT_TRADES` positions are open on the exchange (e.g., 5 positions
vs 3 limit), the demo trader immediately hits `Max concurrent trades reached`
and rejects **every** fresh signal — even though some pre-existing positions are
desirable (BTC, AVAX) and others are noise (BAS, ARC, EGLD).

**Fix:** reconciliation must count existing positions against
`MAX_CONCURRENT_TRADES` and only mark the oldest N as open; close (or skip) the
rest via the BingX API before starting the experiment clock. Reconciliation
must also update `demo_trader.daily_pnl` from exchange positions so the
daily-loss guard has real data — a `daily_pnl` that stays 0 means the guard
never fires regardless of actual P&L.

**Reconciliation script:** `/opt/cortexquant/reconcile_demo.py` can be run
standalone via `pct exec 106 -- python3 /opt/cortexquant/reconcile_demo.py`
(wraps `reconcile_with_exchange()`). Useful for manual reconciliation outside
the service lifecycle.

### ML Training Reality (2026-09)

`GradientBoostingRegressor` on 365-day 1h OHLCV for BTC/ETH/SOL/AVAX yields
R² ≈ -0.04 — no predictive power. The model is saved but fails validation gates.
Use `ML_SKIP_VALIDATION=true` (systemd drop-in) to bypass the gate for demo
purposes, or improve features/model before relying on ML signals.

**ML persistence must survive restart.** After a training run, verify the model
file exists: `pct exec 106 -- ls -la /opt/cortexquant/models/ml_predictor.pkl`.
If the file is absent after a training cycle, the `train()` method is not
calling `save()` — the trained model is lost and the pipeline loads nothing.
Fix: add a `save()` call at the end of `train()` (both the mean-fallback and
ensemble/gbm success paths).

**Systemd drop-in for ML bypass:**
```bash
ssh root@192.168.1.11 'pct exec 106 -- bash -c "
  mkdir -p /etc/systemd/system/cortexquant.service.d
  echo -e \"[Service]\\nEnvironment=ML_SKIP_VALIDATION=true\" > /etc/systemd/system/cortexquant.service.d/ml-skip.conf
  systemctl daemon-reload
  systemctl restart cortexquant
"'
```

### Existing positions (2026-09-20)

| Symbol | Direction | Entry | Size | Source |
|--------|-----------|-------|------|--------|
| BTC | LONG | $77,142.90 | 0.001 | Reconciliation |
| AVAX | LONG | $7.39 | 811.91 | CKPscanner |

Two more (ARC, BAS, EGLD) exist as reconciliation artifacts and push total to 5 — above MAX_CONCURRENT_TRADES=3. Must close at least 2 to accept new signals.

### Reconciliation on Startup

`demo_trader.positions` is in-memory only — service restart loses all tracked positions while BingX retains the real ones. Add reconciliation to `start_demo()` BEFORE the background task starts:

```python
positions = await demo_trader.adapter.get_positions()
for pos in positions:
    symbol = pos['symbol'].replace('/USDT', '').replace(':USDT', '')
    if symbol not in demo_trader.positions:
        demo_trader.positions[symbol] = {
            'exchange_order_id': pos['id'],
            'direction': pos['side'],
            'entry_price': pos['entryPrice'],
            'size': pos['contracts'],
        }
```

Without this, the first pipeline run after a restart sees 0 internal positions and may try to re-open existing BTC/AVAX positions.

## BingX transient: `timestamp is invalid` (code 109400)

The demo trading log repeatedly logs `BingX positions error: bingx {"code":109400,"msg":"timestamp is invalid"}`. This is **transient** — the CCXT adapter sometimes gets a clock-skewed timestamp on the first call after restart. It does NOT mean the integration is broken. Confirm via a direct CCXT test:
```bash
python3 -c "
import ccxt, time
ex = ccxt.bingx({'apiKey': '...', 'secret': '...', 'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
ex.set_sandbox_mode(True)
print('Markets:', len(ex.load_markets()))
print('Positions:', len(ex.fetch_positions()))
"
```
If markets load and positions return, the connection is healthy. The errors stop on retry. Do NOT treat a few `timestamp is invalid` log lines as a config bug.

## Supervisor guard: `LIVE_TRADING_GUARD`

`src/workers/supervisor.py` has a hard guard at module level:
```python
LIVE_TRADING_GUARD = True
```
This means `WorkerSupervisor.start_all()` **refuses to start any workers** if `LIVE_TRADING=true`, regardless of `DEMO_TRADING`. If the user says "start live trading" and you flip `LIVE_TRADING=true` in `.env`, the supervisor will dead-stop. The demo path uses `DEMO_TRADING=true` and leaves `LIVE_TRADING=false`. To actually enable real BingX trading, the supervisor guard logic must be explicitly changed (not just `.env`), and this requires explicit per-session approval.

## VST demo balance starts at ~100,000

The BingX VST sandbox account starts with **~100,000 VST** (not 0). The `fetch_balance()` returns `{'VST': {'free': 99693.0, 'used': 302.9, 'total': 99996.0}}`. Demo trades execute immediately once the pipeline is wired.

## Full Signal-to-Execution Pipeline

The pipeline wires: Telegram parsing → market snapshot → ML prediction → strategy selection → classical optimization → quantum optimization → risk check → BingX execution → decision record → post-trade analytics.

**Two execution paths must converge.** Historically this system had two parallel
paths to demo execution: (a) the orchestrator's 12-step alpha validation +
shared `FullPipeline`, and (b) the Telegram publisher's direct `FullPipeline`
call per signal. When both are active, a signal's validation depends on which
path receives it — inconsistent and untestable. The correct wiring: **the
publisher's `_safe_demo_call` must route through the orchestrator's
`process_signal`**, not create a fresh `FullPipeline`. The orchestrator owns the
shared pipeline instance; the publisher is a delivery mechanism only.

### Architecture
```
tg_publisher → orchestrator.process_signal(signal_dict)
  ├─ 12-step alpha validation (AlphaExtractionPipeline)
  ├─ shared FullPipeline (single instance, not fresh per call)
  │   ├─ idempotency check (DecisionRecord.by signal_id)
  │   ├─ market snapshot (OHLCV from BingX via demo_trader.adapter.exchange)
  │   ├─ ML prediction (MLPredictor — observation-only until trained+saved)
  │   ├─ strategy selection (StrategyEngine with deterministic fallback)
  │   ├─ classical optimization (RiskfolioAdapter)
  │   ├─ quantum optimization (QuantumSolver)
  │   ├─ risk check (RiskEngine — accepts optional config param)
  │   ├─ duplicate position check
  │   └─ BingX VST execution (demo_trader.process_signal)
  ├─ DecisionRecord persisted to database
  └─ demo_trader.reconcile_positions() updates daily_pnl from fills

/api/demo/test-signal → orchestrator.process_signal(signal_dict)  # same path
```

### DecisionRecord Schema
The `decision_records` table captures every signal's complete journey:
- `signal_id` (unique, idempotency key)
- `signal_parsed`, `market_snapshot`, `ml_prediction`, `ml_confidence`
- `strategy_selected`, `strategy_params`
- `classical_optimizer_output`, `quantum_optimizer_output`
- `risk_decision` (approved/rejected), `risk_reason`
- `order_params`, `exchange_order_id`, `fill_price`, `fill_quantity`
- `fees`, `funding`, `slippage`
- `exit_price`, `exit_reason`, `gross_pnl`, `net_pnl`
- `status` (pending, executed, closed, rejected, error)
- `error_classification`, `market_regime`

### Idempotency Pattern
Always check for existing DecisionRecord before processing. A signal that was rejected yesterday must not be re-processed on restart.

### API Routes
- `GET /api/pipeline/decisions` — list recent decisions
- `GET /api/pipeline/decisions/{signal_id}` — full decision chain
- `GET /api/pipeline/stats` — execution statistics

## User Workflow Preferences (Always-On)

These are standing rules the user explicitly stated. They apply to every session.

- **Do not increase risk limits to force a trade.** If `PER_TRADE_RISK_PCT=1.0%` and the pipeline submits 2.0%, the rejection is correct behavior. Record the mismatch, add a regression test, and stop.
- **Treat rejections as integration tests.** A `"rejected"` status is a successful pipeline execution, not a bug.
- **The demo path sizes on notional, not on risk-at-stop — do not report
  `MAX_POSITION_SIZE_PCT` as the per-trade risk.**
  `demo_trading_service.process_signal` computes
  `size = balance * MAX_POSITION_SIZE_PCT/100 / price`, then multiplies by
  `LEVERAGE_MAX`. So notional is `balance × 2% × 3 = 6%` of the account, and with a
  2% stop the actual loss at the stop is about `6% × 2% = 0.12%` of the account —
  nowhere near the configured percentage. `RiskEngine.calculate_position_size()`
  DOES implement true risk-at-stop sizing (`risk_amount / risk_per_unit`, where
  `risk_per_unit = entry − stop_loss`), but the demo execution path never calls it.
  When asked to "verify risk is X% per trade measured as loss at the stop-loss",
  compute the loss from the sizing expression actually used and state the real
  number; do not echo the config constant back as the answer.
- **Fix blockers in this order:** (1) make risk authoritative, (2) replace hardcoded Binance with BingX market resolver, (3) implement `StrategyEngine.select_strategy()`, (4) put ML in observation-only mode, (5) add synthetic e2e test mode, (6) process one real signal only when fully validated.
- **Label momentum_ma as a fallback, not an AI decision.** When ML is untrained or confidence < 0.6, the strategy MUST be labeled `"fallback"`.
- **Never treat default 0.5 confidence as a validated prediction.** Untrained ML models MUST return `{"confidence": 0.5, "observation_only": True}`.
- **Synthetic e2e test mode is mandatory before any real trade.** Implement a `test_e2e()` method that runs the full pipeline WITHOUT placing orders.
- **Never allow an LLM to directly rewrite production trading code, bypass risk controls, or promote itself.**
- **Process a real signal ONLY when ALL gates pass:** exact market exists on BingX, ML model is trained (or ckp_baseline is explicitly used), risk approves, no duplicate position/order would be created, and the signal is fresh.
- **Report exact details, not summaries.** For every pipeline test or trade, report: signal_id, symbol, direction, prediction, strategy name + reason, classical/quantum optimizer outputs, risk decision + reason, order params, exchange response, fill details, fees, and final P&L.
- **Do not optimize further until one complete real signal has traversed the entire pipeline.**

## Verify

When the signal path looks dead (`PARSE_ERROR` dominant, no recent
`tg_parsed_signals`), work through
`references/signal-parser-maintenance.md` before touching any regex — it separates
"parser broke" from "feed carries nothing", which need opposite responses.

```bash
curl http://localhost:8080/api/demo/status | python3 -m json.tool
curl -s -X POST http://localhost:8080/api/demo/test-signal | python3 -m json.tool
tail -50 /tmp/cq.log | grep -a demo\|BingX\|callback\|order
```
