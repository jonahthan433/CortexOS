# System health check

Report the live health of CortexOS components.

Rules:
- Read the health snapshot from the Bridge (`/api/health`) or the ops module.
- Report bridge uptime, vault pending approvals, skill count, and voice readiness.
- Never invent numbers. If a component reports an error, say so plainly.
- This is a read-only report. It never changes system state.
