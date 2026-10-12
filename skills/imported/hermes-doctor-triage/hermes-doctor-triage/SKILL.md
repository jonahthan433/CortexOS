# hermes-doctor-triage

# Hermes Doctor Triage

## When to Use

`hermes doctor` reports issues (config drift warnings, provider key "invalid" lines,
npm vulnerability counts) that need resolving, not relaying.

Fix findings at the root cause. The report text is generated code —
read the generator, not just the message.

## Step 0: reproduce live, never trust a screenshot transcription

Vision transcriptions of terminal screenshots garble provider names, URLs, and issue
wording. Re-run `hermes doctor` yourself (timeout >= 300s; it makes network probes)
and triage the live output. Screenshot only tells you the general shape.

## Step 1: trace the finding to its generator

Doctor checks live in `~/.hermes/hermes-agent/hermes_cli/doctor_*.py` (facade
`doctor.py` re-exports; `doctor_config.py` = config drift, `doctor_tools.py` = npm
audit + toolsets, `doctor_connectivity.py` = provider key probes). `grep -rn "<issue
phrase>" hermes_cli/doctor*.py` locates the exact emitter and often the sanctioned fix
path in its docstring. Issues listed as `manual_issues` are never touched by
`hermes doctor --fix` — expect to fix them by hand.

## Legacy custom_providers drift ("the v12 list migration ran once and does not re-fire")

The v11->v12 migration (`hermes_cli/config_migrations.py::_migrate_to_12`) moved the
`custom_providers:` list into `providers:` once at the version bump; entries hand-added
afterward stay stranded and doctor nags forever. Reuse the migration itself rather than
hand-converting shapes:

1. Delete broken entries first (e.g. a hand-written duplicate whose `base_url` ends in a
   stray `\`) via `read_user_config_raw` + `atomic_config_write` from `hermes_cli.config`
   — never `yaml.dump` on a config path (CI-linted, destroys comments/ordering).
2. In a `python3` heredoc with the repo on `sys.path`: `from hermes_cli import
   config_migrations as m; m._migrate_to_12({}, quiet=True)` — it converts every list
   entry to the providers shape through the official translator and persists via the
   sanctioned migration path (keeps `key_env`, `models`, `default_model`, `transport`).
3. Back up `config.yaml` first; verify with `hermes doctor` (the nag rows disappear)
   and confirm the running session's own provider survived in `providers:`.

## "Check <KEY> in .env" false alarms

`doctor_connectivity.py` probes EVERY registered api_key ProviderProfile's `/models`
endpoint with the first env var it finds. A regional sibling (e.g. the CN endpoint)
sharing one key with the intl profile 401s by construction when the key is for the
other region — the key is fine, the probe is doomed. Verify the key directly first
(`curl -H "Authorization: Bearer $KEY" <intl>/models` -> 200). Fix without touching the
repo: drop an override user-plugin at
`~/.hermes/plugins/model-providers/<same-name>/` (bundled `__init__.py` + `plugin.yaml`
copied, `supports_health_check=False` on the doomed regional profiles — doctor then
reports "key configured" and runtime resolution is unchanged; user plugins win on name
collision, last-writer-wins). Do NOT edit the bundled plugin in the git tree —
`hermes update` clobbers it.

## "Browser tools (agent-browser) has N npm vulnerabilities"

The audit runs against the hermes-agent PROJECT_ROOT (`npm audit --workspaces=false`),
not the tool dir under `~/.hermes/tools/` (which has no lockfile — `npm audit` there
ENOLOCKs). `doctor_tools.py::_audit_one` explains the posture: versions come from the
committed lockfile, so a local `npm audit fix` never persists — the next `hermes update`
reinstalls the pinned state and the finding reappears. The fix is the lockfile bump
itself: edit the pin in `package.json` (check BOTH `dependencies` and `overrides` —
they often pin the same package), then `npm install --package-lock-only --workspaces=false
--ignore-scripts` followed by `npm install --workspaces=false --ignore-scripts`, verify
`npm audit` is clean. Tell the user this reverts on the next `hermes update` until
upstream ships the bump (check `git show origin/main:package.json | grep <pkg>` first —
if upstream already fixed it, just update).
