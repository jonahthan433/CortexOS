# cortexcloud-formulate

# CortexCloud Formulate

Domain-to-QUBO adapter library for the CortexCloud optimization API. One function per problem domain — takes real-world objects (returns, distances, shifts, blocks) and emits QUBO matrices that can be sent directly to `POST https://api.cortexcloud.org/v1/optimize`.

**Repo:** https://github.com/jonahthan433/cortexcloud-formulate
**Install:** Copy `cortexcloud_formulate.py` + `pip install numpy`. Single file, no framework dependencies.

## Domains Covered

| Function | Problem | Input |
|----------|---------|-------|
| `portfolio()` | Cardinality-constrained Markowitz | Returns, covariance, K |
| `tsp()` | Travelling salesman (time-indexed) | Distance matrix |
| `assignment()` | Task/worker or SKU/warehouse | Cost matrix |
| `scheduling()` | Single-machine scheduling | Processing times, deadlines |
| `nurse_scheduling()` | Nurse-to-shift preference allocation | Preference matrix |
| `mining_cutoff()` | Open-pit block extraction | Grades, precedence edges |
| `unit_commitment()` | Generator dispatch | Profits matrix, startup costs |
| `maxcut()` | Graph MAX-CUT | Adjacency matrix |
| `crop_allocation()` | Field crop allocation | Yields, adjacency edges |

## Usage Pattern

```python
from cortexcloud_formulate import portfolio, solve
import numpy as np

returns = np.array([0.05, -0.02, 0.03, -0.04, 0.01, 0.02])
cov = np.diag([0.01, 0.02, 0.015, 0.025, 0.01, 0.02])
problem = portfolio(returns, cov, cardinality=3)

result = solve(problem)
print(result["recommendation"]["solver_id"])   # "brute-force"
print(result["recommendation"]["cortexcloud_price_usd"])  # 0.05
```

## Key Design Decisions

- Only QUBO encoding, no solver logic. CortexCloud handles solver selection, routing, and execution.
- Constraint penalties auto-scaled proportionally to objective values.
- n = variables² for time-indexed formulations (TSP, scheduling). Keep small for exact solvers.
- All formulations minimise — profits are negated, costs stay positive.

## Testing

```bash
cd cortexcloud-formulate && source .venv/bin/activate
python -c "from cortexcloud_formulate import *; print('ok')"
python tests/demo.py
```

## Venv

Local `.venv` (Python 3.12, numpy 2.5.2) via `uv venv && uv pip install numpy`.

## See Also

- `cortexcloud-platform` skill — API architecture, deploy, router bugs
- `references/benchmarks-trial-endpoints.md` in cortexcloud-platform
