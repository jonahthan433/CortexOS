# quantum-provider-integration

# Quantum Provider Integration (Braket / IBM Qiskit Runtime)

Use when wiring a quantum compute provider (Amazon Braket, IBM Quantum) into
an API/solver adapter: dynamic backend discovery, availability, real
workloads, cost modeling. Learned on CortexCloud production, 2026-08.

## AWS Braket (boto3 + braket-sdk)

- **`search_devices(filters=[])` does NOT return `deviceCapabilities`** — the
  field is absent in list responses. Capability-based filtering (e.g. exclude
  AHS-only devices) requires a **`get_device(deviceArn=...)` call per device**
  (cached). Filtering on `deviceCapabilities` from search_devices yields an
  empty string → silently excludes every QPU. Classic trap.
- QuEra Aquila is `deviceType=QPU` + ONLINE but **analog (AHS) only** — it
  cannot run QUBO/Ising IR; submitting fails with
  `CreateQuantumTask ... supports ['braket.ir.ahs.program']`. Gate-model
  capability check: caps JSON contains `jaqcd` / `annealing` / `openqasm`.
- Regions: Rigetti QPUs in us-west-1; IonQ/QuEra in us-east-1. Discovery must
  iterate both.
- `SearchQuantumTasks` (listing jobs) often **AccessDenied** even when
  Create/Get task works — the IAM role may only allow the run path.
- Provider cost per task ≈ task fee + per-shot (Rigetti Cepheus ~$0.30 +
  $0.000425/shot at 1024 shots ≈ $0.74; observed bills vary — verify from
  real runs, don't trust list prices).

## IBM Quantum (qiskit-ibm-runtime ≥0.4x)

- **Channel renamed**: `QiskitRuntimeService(channel="ibm_quantum_platform", token=...)`.
  The old `"ibm_quantum"` channel name raises
  `ValueError 'channel' can only be 'ibm_cloud', or 'ibm_quantum_platform'`.
- **SamplerV2 signature changed**: `SamplerV2(mode=backend)` — passing
  `backend=` raises `unexpected keyword argument 'backend'`.
- **No auto-transpile since 2024-03-04**: circuits that don't match the
  target raise `The instruction h on qubits (0,) is not supported by the
  target system`. Always `transpile(circ, backend=backend, optimization_level=1)`
  before `sampler.run`.
- **Heron RZZ constraint**: `The instruction rzz is supported only for angles
  in the range [0, pi/2]`. Decompose: `RZZ(θ) = CX(i,j)·RZ(θ,j)·CX(i,j)`
  (exact for any angle).
- Discovery: `service.backends(simulator=False, operational=True)` → filter
  real-time QPUs; sort by `num_qubits` desc. Warning "Instance was not set"
  → pass `instance="open-instance"` (open plan).
- `service.jobs(limit=N)` lists submitted jobs; `str(job.status())` returns a
  plain string ("QUEUED"/"DONE"/...), not an enum.
- **Open-plan economics**: free QPU quota is *execution* time (10 min/28 days);
  queue wait is free but can be **20-60+ min** (free tier = lowest priority).
  `job.result(timeout=...)` is a client-side wait — generous timeout (≥1200s)
  or async polling required. Client timeouts leave jobs queued server-side;
  cancel them (`job.cancel()`) to protect quota.

## Worker wedge (silent job hang) — the #1 operational trap

Symptom: a job stays `status: "running"` forever with **zero worker log
lines** after creation; `GET /v1/jobs/{id}` keeps returning 200/running;
classical & hybrid jobs unaffected. Correlates with *fresh* provider
discovery (works in a long-lived process, wedges after a restart once the
discovery TTL expires).

Root cause: the job worker's `pick_solver` → router → `availability()` →
provider SDK discovery call runs synchronously, and **QiskitRuntimeService /
boto3 calls have no effective timeout** — a stalled provider API (IBM
saturated, AWS throttled) blocks the worker forever. The status was already
committed as `running`, so the job looks alive but never progresses.

Diagnosis: `journalctl -u <svc>` around job creation — the SDK's network
lines appear (`qiskit_runtime_service._discover_account`, instance
resolution) and then nothing.

Fix (root cause, single point): **bound every provider discovery call** —
run it in a thread with a hard timeout:
```python
def _list_backends(service):
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(service.backends, simulator=False, operational=True)
        try:
            backends = fut.result(timeout=15.0)
        except _cf.TimeoutError:
            ex.shutdown(wait=False)   # do NOT `with`-join a hung thread
            raise TimeoutError("provider API discovery timed out")
    return [{"name": b.name, "num_qubits": int(getattr(b, "num_qubits", 0) or 0),
             "simulator": bool(getattr(b, "simulator", False))} for b in backends]
```
Never use the ThreadPoolExecutor as a context manager around the submit
(`__exit__` joins the thread and hangs again) — call `shutdown(wait=False)`
in the timeout path. Cache failures too (TTL), so a flaky provider degrades
to `unavailable`, not to wedged workers. See
`references/wedge-and-verification.md` for the full diagnosis transcript.

## Routing vs provider cost — "primary provider" is a ranking problem

Cost-based router ranking (`_cost = provider_cost + runtime_s × LATENCY_TAX`)
silently makes the **cheapest provider win every auto/quantum selection**.
A $0.00-cost fallback (IBM open plan) therefore *replaces* the intended
primary (Braket) even when the fallback's queue is unusable. Three honest
levers, no special-casing:

- **Provider-family preference (the one that actually shipped)**: add a
  preference tier to the sort key — `_pref = 0 if solver.provider ==
  "aws_braket" else 1` — and sort `(c["_pref"], c["_cost"])`. The primary
  FAMILY wins whenever any of its members is available; the fallback is
  used only when no primary-family backend exists. Cost parity within a
  family is preserved (cheapest member still wins).
- give the fallback a **realistic runtime estimate** reflecting actual queue
  latency (IBM open plan ≈ 1200s, not 1s) — the latency-inclusive sort then
  prefers the fast primary naturally; AND
- keep the fallback's provider cost ≤ the margin guard's ceiling
  (`provider_cost ≤ effective_price / MARKUP`) so it stays sellable as a
  genuine fallback when the primary is down.

Note: the runtime-estimate lever alone is NOT sufficient when the fallback's
honest cost is $0.00 and the latency tax is tiny (1e-4 × 1200s = $0.12 still
beats the primary's $0.75+). The family-preference sort key is the reliable
mechanism.

## Test isolation & live verification

- **Real tokens in `.env` leak into tests**: once a live provider adapter
  exists, router/availability tests hit the real API (slow, flaky, and IBM's
  queue state can change assertions). Add an autouse fixture resetting the
  registry's module-global solver singletons
  (`registry._origin_wukong = registry._braket_backends = registry._ibm_backend = None`),
  plus a per-test `no_ibm`-style fixture that nulls the provider token.
- Adapter solve tests: mock the SDK seam (`_new_service`) AND `qiskit.transpile`
  (fake backends lack `.target`; transpile runs real pass managers).
- Live paid verification: gate real QPU execution behind a flag
  (`QUANTUM_LIVE_EXECUTION=true` in the unit env) — **never flip the gate off
  mid-job**: the worker picks the flag up later and fails an already-paid,
  already-submitted run. Flag on → paid request → poll to completion → flag
  off → verify ledger. Wallet-balance checks via public RPCs need a browser
  User-Agent header or most public endpoints 403.

## Cost model pattern

Provider cost per adapter (`price_usd`), charged price =
`max(list_price, provider_cost × MARKUP)`; margin guards must compare against
the **effective (charged)** price, not the list price. IBM open plan → cost
$0.00 → tier sells at list price with full margin (but see "Routing vs
provider cost" above — set cost ≥ primary's for ranking parity if the primary
must win selection).
