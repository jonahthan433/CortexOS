# proxmox-administration

# Proxmox VE Administration

Inspect and manage Proxmox VE hosts remotely over SSH. This covers the
"check my proxmox server" class of request end to end: node identity,
resource pressure, storage pools, VM/container inventory, cluster state,
and recent errors — in one pass, not a dozen back-and-forth commands.

## When This Skill Applies

- "Check my proxmox server" / "how's the homelab node doing"
- Investigating a Proxmox host that rebooted, hung, or is under load
- Listing/auditing VMs (`qm`) or LXC containers (`pct`) on a node
- Checking storage pool usage (`local`, `local-lvm`, custom pools)
- Verifying Proxmox core services are healthy (pveproxy/pvedaemon/pvestatd)

## Known hosts

See memory for the user's saved Proxmox host/IP and SSH access details —
don't ask if it's already recorded there. Ask only if no host is known yet.

## Security Hardening

When auditing or hardening a Proxmox host, cover these in a single pass:

### 1. SSH Configuration
Check `/etc/ssh/sshd_config` and `/etc/ssh/sshd_config.d/` for:
- `PermitRootLogin` — should be `no` or `prohibit-password`
- `PasswordAuthentication` — should be `no` when keys are in use
- `MaxAuthTries` — lower to 3-5
- Drop-in files in `sshd_config.d/` (e.g. `99-hardening.conf`) override the main config

### 2. PVE Firewall
The PVE firewall (`pve-firewall` service) is **disabled by default**. Enable it with a `cluster.fw` at `/etc/pve/firewall/cluster.fw`.

**Correct format** (this is NOT YAML — it's a custom Proxmox syntax):

```ini
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
in ACCEPT -i lo
in ACCEPT -p tcp --source 192.168.1.0/24 --dport 22
in ACCEPT -p tcp --source 100.64.0.0/16 --dport 22
in ACCEPT -p tcp --source 192.168.1.0/24 --dport 8006
in ACCEPT -p tcp --source 100.64.0.0/16 --dport 8006
```

Key rules:
- Use `--source` (long form), not `-s` — the parser rejects `-s`
- Use `--dport`, not `port:` — the parser rejects `port:`
- No `-m conntrack` — established/related is handled automatically by the framework
- Rules are **one line each**: `type action [flags]`
- Valid types: `in`, `out`, `forward`, `group`
- Valid actions: `ACCEPT`, `DROP`, `REJECT`

After writing: `pve-firewall restart` then `pve-firewall status` to verify `enabled/running`.

See [`references/pve-firewall-config.md`](references/pve-firewall-config.md) for the full template.

### 3. Fail2ban
Install and configure for SSH + PVE web UI:
```bash
apt-get install -y fail2ban
```
Create `/etc/fail2ban/jail.local` with `sshd` and `pveproxy` jails. The `pveproxy` filter watches `/var/log/pveproxy/access.log` for 401/403 responses.

### 4. Unused Services
Disable services that aren't needed:
- **rpcbind** — port 111 exposed on 0.0.0.0 if active. Disable with `systemctl disable --now rpcbind rpcbind.socket` then `systemctl mask rpcbind.service rpcbind.socket`
- **NFS/Samba** — check with `systemctl is-active nfs-server smbd`

### 5. Package Updates
```bash
apt-get update && apt-get upgrade
apt list --upgradable | grep -i security
```
Pay attention to `stable-security` tagged packages (e.g. Node.js in this session).

### 6. PVE User Management
- `pveum user list` — audit existing users
- `pveum user add <userid>@<realm> --password <pw>` — create named admin account
- `pveum aclmod / -member <user> -role PVEAdmin` — assign role at root
- Avoid relying solely on `root@pam`; create a named account with TFA via the web UI

### 7. Port Exposure Audit
Run `ss -tlnp` or `netstat -tlnp` and categorize every listening port:
- **0.0.0.0 / ::** — exposed to all interfaces (needs firewall or justification)
- **127.0.0.1 / ::1** — localhost only (safe)
- **Tailscale IP / custom** — overlay network only (evaluate)

For each exposed port, ask: is there a firewall rule allowing the right sources? Is the service actually needed?

## Syncing Hermes Instances (LXC ↔ Host)

When a Proxmox host runs a Hermes LXC (e.g. CT 103) and you want to sync skills/plugins/memories to the host's Hermes:

```bash
# From the Proxmox HOST (not inside the LXC):
ssh root@<proxmox-host> "tar cz -C /tmp skills plugins memories" | tar xz -C ~/.hermes/

# Alternative using pct pull (slower, one file at a time):
ssh root@<proxmox-host> 'pct pull 103 /home/hermes/.hermes/skills/<name> /tmp/skills/<name>'
```

After extracting, clean up server-only artifacts that shouldn't live on desktop:
- `.bundled_manifest`, `.curator_*`, `.hub`, `.usage.json*`
- Timestamped directories (e.g. `2026-09-19T20-09-31Z`)

**Memories**: merge, don't replace. The server LXC's `MEMORY.md`/`USER.md` typically has richer operational context (CT IDs, project details, cron schedules). Append server entries that don't duplicate what's already on the host.

**Config**: do NOT copy `config.yaml` between instances — model providers, API keys, and environment-specific settings differ. Only merge portable pieces like personality presets.

## Standard Health-Check Command

Run everything in ONE batched SSH call — don't spend a call per metric:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=15 root@<proxmox-host> '
echo "--- Node ---"; hostnamectl 2>/dev/null | head -6
echo "--- Uptime/load ---"; uptime
echo "--- Memory ---"; free -h
echo "--- Root storage ---"; df -hT /
echo "--- Storage pools ---"; pvesm status 2>&1
echo "--- Proxmox services ---"; systemctl is-active pveproxy pvedaemon pvestatd 2>&1
echo "--- VMs ---"; qm list 2>&1
echo "--- Containers ---"; pct list 2>&1
echo "--- Cluster ---"; pvecm status 2>&1 | head -20
echo "--- Recent errors ---"; journalctl -p err -n 15 --no-pager 2>&1
' 2>&1
```

## Interpreting Output

- **`pvesm status`** is the real disk-usage signal for a Proxmox host —
  it lists every storage pool (`dir`, `lvmthin`, etc.) with a `%` column.
  Don't rely on `df /` alone; a host can have several pools (e.g. one for
  ISOs/backups, one lvmthin for VM disks) with very different fill levels.
- **`qm list`** = VMs, **`pct list`** = LXC containers. Report both — a
  host can be VM-only, container-only, or mixed. Each row's `Status`
  column is running/stopped; call out anything unexpectedly stopped.
- **`pvecm status` → "Corosync config '/etc/pve/corosync.conf' does not
  exist"** is expected and NOT an error for a standalone (non-clustered)
  node. Only worry if the user expects clustering and it's missing.
- **Cross-reference `uptime` against `journalctl -p err`.** A burst of
  `Connection timed out`, `cfs-lock ... timeout`, `pvestatd: can't lock
  file ... got timeout`, or `Broken pipe` entries that stops abruptly,
  paired with an uptime of just minutes, means the node recently
  rebooted and those errors are the pre-reboot crash cause — not a
  live, ongoing problem. Say so explicitly rather than alarming the user
  about "active" errors that already resolved via reboot.

## Pitfall: Transient SSH Flakiness Right After Reboot

Immediately after a Proxmox host reboots (or while it's still flapping),
SSH can intermittently fail with:
- `Connection timed out during banner exchange`
- `ssh: connect to host <ip> port 22: No route to host`

...even though ICMP ping succeeds and the TCP port opens on a raw socket
check. This is transient sshd/network-stack startup flakiness, not a real
reachability problem. **Retry the SSH command 2-3 times with a short delay
before concluding the host is down or unreachable** — don't report the
server as down off a single failed attempt when ping/port checks succeed.

## Starting/Stopping Guests

```bash
# LXC containers
ssh root@<host> 'pct start <vmid>'
ssh root@<host> 'pct stop <vmid>'
ssh root@<host> 'pct status <vmid>'

# VMs
ssh root@<host> 'qm start <vmid>'
ssh root@<host> 'qm stop <vmid>'
ssh root@<host> 'qm status <vmid>'
```

Always confirm with the user before starting/stopping guests unless they
explicitly asked for it in the same request — a stopped container may be
intentionally offline (maintenance, decommissioned, cost-saving).
