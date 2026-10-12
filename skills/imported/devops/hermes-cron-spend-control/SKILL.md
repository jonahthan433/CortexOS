# hermes-cron-spend-control

# Hermes Cron Spend Control

## When to use
- Cron jobs failing with `HTTP 402: ... can only afford N ... requested M tokens` (OpenRouter).
- Before any autonomous action that consumes paid API credits (standing user policy).
- Auditing which cron jobs route to paid vs free models.

## Governing rule (user standing policy, 2026-08-20)
Inspect first -> make the smallest safe implementation -> test it -> verify
credit controls and tracking -> deploy. **If any action could exceed the
credit runway, STOP and report — do not proceed.** (Four-month runway context
for this user; the principle generalizes to any paid-API agent work.)

## Inspect (read-only, no spend)
- Cron job definitions: `~/.hermes/cron/jobs.json` — key `jobs` (list or dict
  of objects). Fields: `name`, `model`, `provider`, `schedule`, `id`.
- Spend ledger: `~/.hermes/cron/usage_audit.jsonl` — one JSON per line.
  Fields: `job_id`, `model`, `total_tokens`, `completion_tokens`,
  `prompt_tokens`, `error`, `ts`. **Stores tokens, NOT USD** — no cost column
  by default.
- 402 detection: `error` field contains the substring `"402"`.
- Paid vs free heuristic: free if the `model`/`provider` string contains
  `free`, `hy3-free`, `deepseek-v4-flash-free`, or `tencent/hy3:free`.
  **Pitfall:** a pinned `None/None` usually inherits the config default
  (`model: default: hy3-free` under `agent:`) — NOT paid. A naive
  `None -> paid` flag produces false positives; confirm the config default
  before acting.

## Root cause of 402 bleed (two distinct modes — fix BOTH)
1. **Hard-pinned paid model** (e.g. `tencent/hy3`/openrouter, or `None`
   defaulting to a paid default). Repin.
2. **Runtime routing to paid despite a free pin** — the ledger shows the job
   ran `deepseek-v4-flash` / `tencent/hy3` even though it was pinned free.
   Means an earlier repin didn't take (config-drift). Re-pin AND verify via
   the ledger.

## Fix — one line per job
`hermes cron edit "<Job Name>" --model meituan/longcat-2.0:free --provider nous`
- **Pitfall:** the `cronjob` *agent tool* (update/pin actions) silently drops
  `--model`/`--provider` ("No updates provided."). Use the `hermes` **CLI**
  directly, not the tool.
- After editing, re-read `jobs.json` to confirm the pin landed — the tool/cron
  can drop model/provider on update.

### Provider-key trap (the silent fallback bug)
The provider string is **not enough** — the provider must have an
authenticated credential in `~/.hermes/auth.json` (or `*_API_KEY` env
var). Without it, the cron runtime silently falls through to the next
provider with valid creds — which is usually `openrouter`, the source
of the 402 in the first place.
- `opencode-zen` requires `OPENCODE_ZEN_API_KEY` — **not** keyless despite
  the `big-pickle` model serving without auth on the wire. Without a key,
  the pin looks right but the request still routes elsewhere.
- `nous` (Nous Portal OAuth) IS authenticated by default and is the right
  provider to pin against for free models on Hermes installs without
  paid provider keys.

### Free-model pitfall — `tencent/hy3` is PAID on Nous
The historical pin `tencent/hy3:free` (OpenRouter alias) maps to a PAID
model on the Nous inference API. Same string, different billing:
`requires available credits. Your account balance is too low`.
Verified free on Nous (2026-09-01, 200 OK):
`meituan/longcat-2.0:free`, `poolside/laguna-xs-2.1:free`,
`stepfun/step-3.7-flash:free`, `inclusionai/ling-3.0-flash-fin:free`.
- **Use `meituan/longcat-2.0:free`** as the canonical pin — works without
  cold-start, cleanest stop_reason.
- `tencent/hy3` (no `:free` suffix) is paid; `tencent/hy3:free` (OpenRouter
  alias) only works on OpenRouter and 402s on the user's free tier.

## Verify (the runnable check — non-trivial logic, leave proof behind)
1. Fire each formerly-paid job once: `hermes cron run "<Job Name>"`.
   **Run in BACKGROUND** — foreground blocks on the LLM call (the job itself
   runs an inference). Collect all fire IDs, then poll the ledger.
2. Re-read `usage_audit.jsonl`; assert every post-fix row for those jobs
   shows `model: meituan/longcat-2.0:free` and `error` is null.
   **Zero new 402s = pass.** (Reusable: `scripts/audit_cron_spend.py` prints
   per-job PIN$/USE$ flags, token totals, and total 402 count.)
3. **Ignore stale async results from pre-fix dispatches.** When you fire
   the verification batch and stale dispatches from the prior broken pin
   come back with 402, do not chase them — they were dispatched before
   the fix and the cron runtime used the old pin. Wait for the
   verification fire (the one dispatched AFTER the `cron edit` landed)
   to return and inspect only that one. Stale dispatches drain on their
   own in ~5–10s each.

## Hard cap / runway (deferred path — do NOT guess)
- To set a real USD cap or compute runway, read the live balance:
  `GET https://openrouter.ai/api/v1/credits` with the key from `config.yaml`
  (`provider: openrouter` -> `secret`).
- **Pitfall:** a 43-char non-OR secret in config returns
  `401 Missing Authentication header` (stale/wrong key). Without the correct
  key or a stated balance figure, you CANNOT compute the runway denominator —
  stop and ask the user rather than fabricating a cap.

## Skipped (not in scope unless asked)
- USD ledger column (tokens x model price): optional add for cost visibility.
- Real hard-spend cap: needs live balance; deferred per the stop-and-report rule.

## Support files
- `scripts/audit_cron_spend.py` — read-only per-job spend + paid-model audit.
- `references/provider-key-trap.md` — the provider-key silent-fallback
  bug: why a pin "looks right" but routes elsewhere, with the cross-provider
  free-model matrix and the diagnose flow.
