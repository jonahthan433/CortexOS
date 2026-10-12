# hermes-gateway-troubleshooting

# Hermes Gateway Troubleshooting

## When to Use

User reports a messaging chat is down ("why is telegram down", "bot not replying"),
or the `hermes-gateway.service` journal shows reconnect loops, polling conflicts, or
`Fatal ... adapter error` rebuilds.

## Rule zero: one bot token, one poller

Telegram allows exactly ONE `getUpdates` poller per bot token. Two hermes gateways
holding the same token terminate each other in a loop:

```
[Telegram] Telegram polling conflict (n/5) — previous session still held open on Telegram's servers. Error: Conflict: terminated by other getUpdates request; make sure that only one bot instance is running
```

This user runs hermes on several machines (desktop + homelab LXCs). ALWAYS hunt for
duplicate instances on every machine before debugging anything else. Restarting the
local gateway alone never fixes it — the remote poller re-kills the new session.

## Diagnostic sequence

1. Local journal:
   `journalctl --user -u hermes-gateway.service -n 60 --no-pager | grep -iE "telegram|conflict"`
2. Rule out real network loss:
   `curl -s -m 10 -o /dev/null -w "%{http_code}\n" https://api.telegram.org/` (302 = reachable)
   and `ss -tnp | grep 149.154` (ESTAB = polling works)
3. Duplicates locally: `ps aux | grep "gateway run" | grep -v grep`
   (the gateway cmdline is a huge python bootstrap blob — grep server-side, don't eyeball `ps`)
4. Duplicates in LXCs on the Proxmox host:
   `ssh root@192.168.1.11 "pct list"` then per VMID:
   `ssh root@192.168.1.11 "pct exec <VMID> -- su - hermes -c 'systemctl --user is-active hermes-gateway.service'"`
   — `systemctl --user` via `pct exec` MUST run as the hermes user with a login shell;
   as root it silently returns nothing (no user session bus).

## Fix (reversible)

1. Stop + disable the non-owner gateway (the LXC one; the desktop unit owns the token):
   `ssh root@192.168.1.11 "pct exec <VMID> -- su - hermes -c 'systemctl --user stop hermes-gateway.service && systemctl --user disable hermes-gateway.service'"`
2. Sleep ~30s — Telegram needs time to release the held getUpdates session.
3. Restart the owner: `systemctl --user restart hermes-gateway.service`
4. Verify: no new `polling conflict` lines AND ESTAB sockets to 149.154.x.x:443 owned
   by the local gateway PID (`ss -tnp | grep 149.154`).

Don't delete the remote unit file — stop+disable reverses in one command.

## Pitfalls

- `Temporary failure in name resolution` / `httpx.ConnectError` lines during a
  conflict storm are noise — verify DNS separately (`getent hosts api.telegram.org`)
  before chasing them.
- `Fatal telegram adapter error (telegram_network_error) ... rebuilding the adapter`
  is a downstream symptom of the same conflict, not a second bug.
- The gateway runs multiple adapters; a Discord `401 / Improper token` in the same
  journal is a separate issue (bad/revoked token) — don't conflate.
- Conflict warnings keep appearing for up to a few minutes after the duplicate dies
  (Telegram holds the old session ~30-60s). Wait before re-verifying.

## References

- [`references/ct103-duplicate-poller.md`](references/ct103-duplicate-poller.md) —
  full 2026-10-11 diagnosis: log excerpts, exact commands, outcome.
