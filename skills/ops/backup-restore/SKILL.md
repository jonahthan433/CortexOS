# Backup and restore

Create or manage timestamped vault backups.

Rules:
- "backup" or "create a backup" makes a new timestamped copy of state.json, automations.toml, receipts, requests, and metrics under `vault/.backups/`, keeping the 10 most recent.
- "list backups" shows existing backups with timestamps.
- Restoring overwrites current vault state from a backup. Restore always requires explicit human approval because it can overwrite receipts and state.
- Never delete a backup the user did not ask to delete.
- Report the backup path after creation.
