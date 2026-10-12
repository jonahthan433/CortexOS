# quant-hq-operations-console

# Quant Lab HQ Operations Console

Cinematic command-center for monitoring a 12-room / 24-agent quantitative research organization. All visual state comes from real PostgreSQL data via SSE — never simulated.

## Architecture

### Backend (CT106, /opt/cortexquant)
| File | Purpose |
|---|---|
| `src/quant_hq/rooms_schema.py` | SQLAlchemy models: ResearchAgent, ResearchMessage, ResearchExperiment, ResearchAuditLog, ResearchTask, ResearchPromotion, RoomType, PipelineStage |
| `src/quant_hq/rooms.py` | Room classes, ChiefQuantDirector, MessageBus, MemoryStore, QuantHQOrchestrator singleton |
| `src/quant_hq/events.py` | SSE router with `_get_full_state()` |
| `src/quant_hq/orchestrator.py` | Orchestration helpers |
| `src/quant_hq/schema.py` | Canonical enums (ResearchStage, AgentRole, TaskStatus) |
| `src/quant_hq/alpha_pipeline.py` | 12-step alpha extraction: demeaning, variance, normalization, truncation, cross-sectional demean, expected return, orthogonalization, weighting, combination |
| `src/quant_hq/monitor.py` | DecayDetector (rolling Sharpe + sustained-period flagging), HMRegimeDetector (2-state Gaussian HMM + forward algorithm), PermissionGraph (room write/read permissions), MonitoringService (orchestration + feedback loop) |
| `src/ingestion/tg_schema.py` | Raw message storage, parsed signal storage, backfill checkpoints, metrics |
| `src/ingestion/tg_parser.py` | v2.0.0 — parses CKPscanner Scout Opportunity lists (symbol, direction, RSI15/4H, TSI15/4H, divergence, funding) |
| `src/ingestion/tg_service.py` | Telethon MTProto ingester with pagination + real-time listener |
| `src/ingestion/tg_publisher.py` | Routes parsed signals → creates ALTCOIN_SIGNAL_RECEIVED events → MessageBus → 10 rooms |
| `src/api/tg_routes.py` | Telegram ingestion endpoints (health, signals, stats, reparse) |
| `src/api/monitoring_routes.py` | Monitoring endpoints (decay, regime, DSR, permissions) |
| `src/api/alpha_routes.py` | 12-step validation and combination endpoints |
| `main.py` | Entry point; starts scheduler, ingester, publisher, API |

### Frontend (`/console`)
- Canvas facility map: Chief Quant Director center, 12 rooms in a circle
- Agent dots on rings inside each room
- Data packets animate along conduits when messages flow
- SSE-driven: no polling, no fake animation

## 12-Step Alpha Extraction Pipeline

Every signal must pass through all 12 steps before any further action. From the RuujSs "Unified Framework for Alpha Extraction and Signal Combination":

| Step | Math | What |
|---|---|---|
| 1 | `X = R - mean(R)` | Linear demeaning (drift removal) |
| 2 | `σ² = mean(X²)` | Variance estimation |
| 3 | `Y = X / σ` | Return normalization (unit variance) |
| 4 | `Y[:M-1]` | Time-series truncation |
| 5 | `Λ = Y - mean(Y across N)` | Cross-sectional demeaning |
| 6 | `Λ[:M-1]` | Final trimming |
| 7 | `E = mean(R[-d:])` | Expected return (forward-looking) |
| 8 | `E_norm = E / σ` | Return normalization |
| 9 | `E_norm = β·Λ + ε` | Residual extraction (orthogonalize) |
| 10 | `w = ε / σ` | Signal weighting (inverse vol) |
| 11 | `Σ\|w\| = 1` | Weight normalization (gross exposure = 1) |
| 12 | `Combined = Σ w(i)·S_i` | Combined signal |

Validation runs in `AlphaExtractionPipeline.validate_signal()` → returns `(passed, results, output)`. Combine N signals via `combine_signals()`.

## Monitoring System

### DecayDetector
Rolling Sharpe with sustained-period flagging. A single bad window is variance. N consecutive bad windows is a signal. From the RuujSs alpha engine article:

```python
class DecayDetector:
    def __init__(self, window_size=20, sustained_periods=3, decay_threshold=0.5):
        ...
    def check_decay(self, research_id, live_returns) -> Tuple[bool, Dict]:
        ...
```

### HMRegimeDetector
Two-state Gaussian HMM with forward algorithm for regime probability. Secondary correlation check catches what the model alone misses.

```python
class HMRegimeDetector:
    def __init__(self, n_states=2): ...
    def fit(self, returns) -> bool: ...
    def predict(self, returns) -> Dict: ...  # {fitted, regime, confidence, divergence}
```

### PermissionGraph
Enforces room-to-room write/read permissions at the data level. Maps each room to the set of statuses it can write and read.

```python
class PermissionGraph:
    WRITE_PERMISSIONS = {RoomType.QUANT_RESEARCH: {"CANDIDATE"}, ...}
    READ_PERMISSIONS = {RoomType.QUANT_RESEARCH: {"CANDIDATE", "APPROVED", "FLAGGED"}, ...}
    def can_write(self, room, status) -> bool: ...
    def can_read(self, room, status) -> bool: ...
```

### MonitoringService
Orchestrates decay + HMM + feedback. When decay or regime divergence is detected, sends HIGH priority messages to the research room.

```python
class MonitoringService:
    async def check_experiment(self, research_id, returns, baseline_sharpe) -> Dict: ...
    async def _alert(self, research_id, alert_type, data): ...
```

## Deflated Sharpe Ratio

Multiple-testing correction for strategy validation. Converts Sharpe into a probability that the strategy is not overfit.

```
def deflated_sharpe_ratio(sharpe, n_observations, skew=0.0, kurtosis=3.0, n_trials=1, alpha=0.05) -> Dict:
    # Returns {dsr, probability, pass, expected_max_sr, threshold, n_trials}
```

Uses `scipy.stats.norm` (Bailey & Lopez de Prado expected max SR under null).

## SSE Protocol

/api/quant-hq/events emits named SSE events:
- `state` — full state snapshot
- `audit_log` — incremental new audit events
- `heartbeat` — timestamp ping

Client uses `addEventListener` for named events. `onmessage` does NOT fire for named SSE events.

## Common pitfalls

- **Agent duplication on restart**: `initialize()` calls `register_agent()` unconditionally — add a guard to check for existing agent before insert.
- **PSQL table name mismatch**: model `__tablename__` may be singular (`research_audit_log`) while raw psql uses plural.
- **Port bind failure on restart**: `fuser -k 8080/tcp` BEFORE restarting, or get `[Errno 98] address already in use`.
- **Shell quoting kills JSON**: nested ssh/pct/bash -c with curl -d JSON mangles. Write to file, use `curl --data-binary @file`.
- **State endpoint wrapper**: `/api/quant-hq/state` returns `{state: {...}}` — console accesses `data.state`.
- **Room initialization is lazy**: only 5 of 12 rooms registered in `initialize()`. Add missing room classes + init calls + agent registrations.
- **AsyncSessionLocal**: always use async with context manager.
- **`scipy` must be in the venv, not system**. `pip install scipy` inside the CT106 venv (`/opt/cortexquant/venv`), not the host. System pip won't put it where the FastAPI app can import it.
- **`python-multipart` for FastAPI POST bodies**. FastAPI requires it for form data / JSON body parsing. `pip install python-multipart` in the venv.
- **`numpy.bool_` is not JSON-serializable**. Always wrap in `bool()` when returning from an API endpoint (e.g. `"pass": bool(probability >= threshold)`).
- **Stale `__pycache__`**. Always `find /opt/cortexquant -name __pycache__ -exec rm -rf {} +` after patching `.py` files, then restart.
- **`.env` vs `pydantic-settings`**. `settings.TELEGRAM_API_ID` reads `.env` but `os.environ.get("TELEGRAM_API_ID")` returns None until you manually load `.env` (e.g. for the Telethon ingester constructor).
- **Telegram session lock**. Kill the running server BEFORE running auth interactively, or get `sqlite3.OperationalError: database is locked`.
- **Auth codes expire in ~30s**. Run auth directly on the server (not via background polls) and type code immediately.
- **uvicorn `serve()` blocks forever — background tasks must precede it**. Any async work that must run for the lifetime of the server (e.g. a 7-day demo trading orchestrator, background reconcilers, cron-like jobs) must be started with `asyncio.create_task(...)` BEFORE `await server.serve()`. Code after `serve()` never executes.
- **Parser variable-name typo in success branch** — `for p in parsed:` instead of `for p in parsed_results:` silently breaks the main parse path. When patching parser loops, re-read the surrounding 5 lines after every edit to catch a wrong variable name — a NameError in the success branch is silent when the function returns early on `None`.
- **Scout message regex must handle leading whitespace** — CKPscanner emits lines like ` 1. ALCH-USDT | ...` (2 spaces before number). A pattern `r"(\d+)\.\s+"` matches zero lines. Always include `\s*` before numeric tokens in signal-line regexes.
- **MAX_POSITION_SIZE_PCT caps risk target** — `position_size = (capital * risk_pct) / |entry - sl|` is then `min(position_size, max_pos)`. If `MAX_POSITION_SIZE_PCT` is too tight (e.g. 5%), the position cap binds and actual risk falls well below `PER_TRADE_RISK_PCT`. Set cap high enough (e.g. 50%) that it doesn't bind for realistic stops.
- **ML model can have negative R²** — a trained GradientBoosting/RandomForest on financial OHLCV can score R² < 0 (worse than predicting the mean). The validation gate must check `val_r2 > 0` before enabling trades; a saved model file is not proof of predictive power.
- **Fresh CKPscanner signals arrive in batches** — one Scout message contains 20+ symbols, all with identical timestamps. Deduplicate by symbol (keep highest-confidence) and process as a single signal batch, not 20+ independent trades.
- **CT services have no systemd/supervisorctl** — CT106 runs processes via a custom launcher (`main.py`). After code changes, the reliable restart is `fuser -k 8080/tcp && PYTHONPATH=/opt/cortexquant nohup python main.py > /tmp/cq.log 2>&1 & disown`, not `systemctl restart`.

## Dependencies (CT106 venv)

```bash
source /opt/cortexquant/venv/bin/activate
pip install numpy scipy scikit-learn hmmlearn telethon python-multipart pytest-asyncio
```

- `scipy` — for `deflated_sharpe_ratio` (norm.ppf, norm.cdf)
- `scikit-learn` — for `hmmlearn` dependency
- `hmmlearn` — Gaussian HMM for regime detection
- `telethon` — MTProto user-client for Telegram
- `python-multipart` — FastAPI POST body parsing
- `pytest-asyncio` — async test support (v0.23.8 works best; v1.4.0 has mode=STRICT issues)

## Regression tests

`tests/test_quant_hq_agents.py` — 6 tests. Run:
```bash
PYTHONPATH=/opt/cortexquant python3 tests/test_quant_hq_agents.py
```
Or: `bash scripts/regression_test.sh`

`tests/test_tg.py` — 9 parser tests + storage tests:
```bash
cd /opt/cortexquant && source venv/bin/activate && PYTHONPATH=/opt/cortexquant python3 -m pytest tests/test_tg.py -v
```

`tests/pyproject.toml`:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

## Deployment (CT106 in LXC)

```bash
# Kill old process + free port
ssh pve 'pct exec 106 -- bash -c "fuser -k 8080/tcp 2>/dev/null; sleep 2"'
# Clear stale bytecode
ssh pve 'pct exec 106 -- bash -c "find /opt/cortexquant -name __pycache__ -exec rm -rf {} + 2>/dev/null"'
# Restart
ssh pve 'pct exec 106 -- bash -c "cd /opt/cortexquant && source venv/bin/activate && PYTHONPATH=/opt/cortexquant nohup python main.py > /tmp/cq.log 2>&1 & disown"'
# Verify
ssh pve 'pct exec 106 -- bash -c "sleep 4; curl -s http://localhost:8080/health"'
ssh pve 'pct exec 106 -- bash -c "curl -s http://localhost:8080/openapi.json | python3 -c \"import sys,json; d=json.load(sys.stdin); print(len(d[\\\"paths\\\"].keys()), \\\"endpoints\\\"}\""'
```

## LIVE_TRADING=false always — research agents isolated from execution.

SSE protocol details (named events, client-side handling): see `references/sse-protocol.md`
