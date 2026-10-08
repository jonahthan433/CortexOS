# CortexOS vault contract

- `raw/`: user-authored captures. Treat as read-only source unless asked to edit.
- `wiki/`: organized durable knowledge; update `_master-index.md` for new articles.
- `output/`: generated reports, briefs, outlines, and other deliverables.
- `requests/<request-id>.md`: request records with lifecycle updates.
- `receipts/YYYY/MM/`: skill execution, approval decision, and confirmation records; `receipts/promotions/` holds automation review history.
- `metrics/`: user-supplied metrics and dated snapshots.
- `automations.toml`: explicit schedules; only skills with approved promotion records may run.

Use plain Markdown and descriptive filenames. Do not create a graph database or invent user facts. Sending, spending, publishing, and other external side effects require fresh human approval for every run, even after promotion.
