# resuming-interrupted-sessions

# Resuming Interrupted Sessions

This user opens sessions with "can we continue from where we stopped" routinely — the
skill exists because a plain `session_search` read truncates on big sessions and
interrupted commands need side-effect checks before re-running.

## Flow
1. `session_search()` (browse) → pick the newest session whose title/preview matches.
2. `session_search(session_id=...)` to read it — but **large sessions truncate** (~100K+
   chars). Fall back to direct DB forensics (below) for the tail.
3. Find what the LAST user message asked and what the last tool run was doing. If the
   session ended with `[Command interrupted]` (exit 130), the work is usually NOT done —
   resume there.

## state.db forensics (when session_search output is truncated)
sqlite3 CLI is often absent on this host — use python3's module:
`~/.hermes/state.db`, table `messages` (columns: `id, session_id, role, content,
tool_call_id, tool_calls, tool_name, timestamp, reasoning, ...`).

- Tail of a session:
  `SELECT role, content FROM messages WHERE session_id=? AND role IN ('user','assistant') AND content != '' ORDER BY id DESC LIMIT 8`
- All user asks (thread of intent):
  `... role='user' AND content != '' ORDER BY id ASC LIMIT 12`
- Window by id: `... id BETWEEN ? AND ? ORDER BY id ASC` — reconstructs the exact
  sequence around an interruption.

Key facts learned the hard way:
- **Tool rows store OUTPUT only, not the command/args.** To reconstruct what was about
  to run, read the assistant message's `reasoning` column (holds the plan + command
  intent) and the assistant `content` before it.
- **`[Command interrupted]` = user stop, not test failure.** Before re-running, verify
  side effects: check for the expected output file, task/record creation, etc. An
  interrupted command may have never executed (missing output file = never ran) OR
  executed before the kill (orphan) — the reasoning column usually notes which was
  confirmed.
- `exit_code 130` on a terminal tool = interrupted; `2` = file not found (e.g. the
  output-file check ran on the wrong host — paths like `/root/live_qpu_out.txt` live
  INSIDE the LXC, check via `pct exec`).

## SSH reminder
Plain `ssh root@192.168.1.11` fails — the ninyi key is `~/.ssh/hermes_ninyi`:
`ssh -i ~/.ssh/hermes_ninyi -o BatchMode=yes root@192.168.1.11 "pct exec 105 -- <cmd>"`.

## Pitfalls
- Don't re-run an interrupted command blindly — check for orphan side effects first
  (a QPU task / payment / deploy may have landed before the kill).
- Interrupted background runs: `process(action='list'/'poll')` before restarting; a
  stale session may still be running.
- **Never restart the gateway that hosts your session as a step you still need to
  complete after.** `systemctl --user restart hermes-gateway` (or any service the
  agent loop runs inside) kills the in-flight run mid-turn — the final message is
  never delivered and the user sees a silent cut. If a gateway restart MUST happen
  as part of the work (e.g. verifying a config change), detach it so it fires AFTER
  your final response: `systemd-run --user --on-active=120s --unit=gw-verify /path/verify.sh`
  — the transient unit escapes the gateway's cgroup (KillMode=mixed only SIGTERMs
  the main process), so it survives the restart and confirms via its own channel
  (log file + Telegram `sendMessage` via bot token from `.env`, never a secret
  printed). The final assistant message goes out immediately; the restart fires
  later. If the run still gets cut, resume with this skill's normal flow (the
  interruption marker + reasoning column will show the scheduled restart).
