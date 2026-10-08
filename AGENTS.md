# CortexOS agent instructions

The Obsidian vault is the durable memory and deliverable store. Its required layout is:

- `raw/`: user-authored captures; do not silently rewrite these.
- `wiki/`: organized reference articles and `_master-index.md`.
- `output/`: generated reports, briefs, plans, and other deliverables.
- `requests/<request-id>.md`: Markdown records of user requests and lifecycle updates.
- `receipts/YYYY/MM/`: skill execution and user-confirmation receipts; `receipts/promotions/` holds promotion review history.
- `metrics/`: editable user-provided metrics and system aggregates.
- `automations.toml`: local scheduled job definitions. A job runs only for an approved promotion ledger entry.

Always use the vault root configured in `config/cortexos.toml`. Generated answers and skill outputs go to `output/`; request transcripts/receipts go to their respective folders. Never invent personal metrics. Sending, spending, or publishing requires an explicit human approval before the action. Skills are Markdown under `skills/` with a `skill.json` manifest.
