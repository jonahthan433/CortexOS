# troubleshooting

# Troubleshooting Guide

Diagnostic and repair workflows for common Linux/Omarchy system issues.

## When This Skill MUST Be Used

Always invoke for end-user requests involving:

- **Systemd services**: service fails to start, `No such file or directory` in journal, ExecStart points to a deleted path
- **Audio**: no sound from HDMI monitor, wrong output device, HDMI profile unavailable
- **Browser**: `browser_exec` fails with `chrome-not-running`, browser harness can't find a running browser
- **WiFi/LAN**: KDE Connect / LAN devices not discovering each other, broadcast ping fails
- **WiFi performance**: wild latency, stuck on 2.4GHz, high retries, reg domain issues
- **Python imports**: `ImportError` in a running gateway/service from a deleted venv or pipx installation

## Systemd Service Path Failure (Deleted venv/pipx)

### Diagnosis

When a systemd user service points to a Python or binary path that no longer exists (e.g., a deleted pipx/mise installation), the service fails on restart but the old process may keep running from stale memory.

```bash
# Check service status
systemctl --user status <service>.service
journalctl --user -u <service>.service -n 50 --no-pager

# Verify the ExecStart path exists
systemctl cat <service>.service | grep ExecStart

# Check the actual running process
ls -la /proc/<PID>/exe
cat /proc/<PID>/environ | tr '\0' '\n' | grep VIRTUAL_ENV
```

### Fix

```bash
# 1. Find a working alternative venv/python
which python3
ls /home/$USER/.hermes/hermes-agent/venv/bin/python

# 2. Edit the service file
nano /home/$USER/.config/systemd/user/<service>.service

# 3. Kill the old stale process
kill <PID>

# 4. Reload and restart
systemctl --user daemon-reload
systemctl --user restart <service>.service
```

**Key pitfall:** The old process may still be running and serving traffic from stale memory.

## Audio Routing (PipeWire + ALSA)

### Switching Audio Output to HDMI

```bash
aplay -l         # shows HDMI devices
pactl list cards short  # card number
pactl list cards  # look for output:hdmi-stereo
pactl set-card-profile <card-num> output:hdmi-stereo
pactl list short sinks  # verify
```

**Pitfall:** HDMI profiles only appear when the display is actively connected.

## Browser Remote Debugging (CDP)

The browser harness looks for a running browser by checking:
1. `BU_CDP_URL` or `BU_CDP_WS` env vars
2. `DevToolsActivePort` file in browser profile dirs
3. Probing ports 9222/9223 on localhost

**Config fix:**
```yaml
# ~/.hermes/config.yaml
browser:
  cdp_url: http://127.0.0.1:9222
  engine: auto
```

**Browser startup:**
```bash
pkill -f "brave|chrome|chromium"
brave --headless --no-sandbox --remote-debugging-port=9222 &
```

## WiFi Performance (Timeout, Latency, Packet Loss)

### Diagnosis

When WiFi feels like "timeout" or shows massive latency swings, start with signal quality and band selection, not driver blame:

```bash
ip link show                          # Interface state (DORMANT = low power)
iw dev <iface> info                   # Band, channel, width, TX power
iw dev <iface> station dump           # Signal, bitrate, retries, inactive time
iw phy phy0 channels                  # Which 5GHz channels are available
ping -c 10 -W 2 8.8.8.8              # Sustained test reveals jitter, not just loss
lsmod | grep iwlwifi                  # Driver loaded
for f in /sys/module/iwlwifi/parameters/*; do echo "$f: $(cat $f)"; done
iw reg get                             # Reg domain (critical for 5GHz + power)
```

### Common Culprits and Fixes

| Symptom | Root Cause | Fix |
|---|---|---|
| Stuck on 2.4GHz, high retries, low bitrate | Roaming/band-steering keeps client on crowded band | Manually connect to 5GHz SSID or disable band steering on router |
| Reg domain `00: DFS-UNSET` (self-managed) | Kernel restricts 5GHz, low TX power | `sudo iw reg set US` (or appropriate country) |
| Wild latency (30ms–1000ms swings), 0% loss | Weak signal + driver retry storm | Move closer to AP; check signal |
| High tx retries (>5% of packets) | Co-channel interference or marginal signal | Change router channel; disable BT coex if BT unused |
| Bluetooth active + BT coex active | WiFi/BT shared antenna, coex overhead | `echo options iwlwifi bt_coex_active=N > /etc/modprobe.d/iwlwifi.conf` |

**Signal thresholds (rule of thumb):**
- > -50 dBm: excellent
- -50 to -65 dBm: good
- -65 to -75 dBm: marginal, latency spikes expected
- < -75 dBm: weak, expect retries and disconnects

**Bitrate sanity check for Intel 8260:**
- 2.4GHz: MCS 7 = 72.2 Mbit/s (should hit this easily)
- 5GHz: MCS 9 = 866.7 Mbit/s (80MHz width)
- MCS 2 on either band = something is very wrong

**Post-fix reconnect:**
```bash
nmcli device disconnect <iface>
nmcli connection up "<SSID>" ifname <iface>
```

## WiFi / Device Discovery (AP Isolation)

### Diagnosing KDE Connect Discovery Failure

```bash
ps aux | grep kdeconnect
ss -tlnp | grep kdeconnect  # TCP 1716
ss -ulnp | grep kdeconnect   # UDP 1714-1763
ping -b -c 1 192.168.1.255   # 100% loss = AP isolation
```

**Fix:** Manually add PC IP in KDE Connect app; or disable AP Isolation in router.

## Omarchy `omarchy default agent` fails with "Could not install <Name> with mise"

`omarchy default agent <name>` (Setup > Defaults > Agent in the menu) runs an
agent-specific readiness check before writing `~/.config/omarchy/defaults/agent`.
If the check fails, it opens a floating terminal and tries a `mise`-based
install; if that ALSO fails you get the generic "Could not install <Name> with
mise", even when the agent already runs fine from the CLI. Don't trust the
generic error or assume the agent itself is broken — trace the actual check:

```bash
cat /usr/share/omarchy/bin/omarchy-default-agent   # find the agent's case branch
# Some agents (currently hermes, openclaw) delegate to a dedicated
# omarchy-install-<agent>-cli instead of a bare `mise use`.
bash -x omarchy-install-<agent>-cli --check         # last predicate before exit 1 = the real blocker
```

### Hermes specifically: CLI works, but the default-agent check still fails

Root cause: Hermes Desktop (or the git-based `install.sh`) provisions a fully
working runtime under `~/.hermes/hermes-agent`, but
`omarchy-install-hermes-cli --check` additionally requires two Omarchy-specific
artifacts that neither installer writes on its own:

1. `~/.hermes/hermes-agent/.hermes-bootstrap-complete` — a JSON provenance
   marker (`schemaVersion`, `pinnedCommit`, `pinnedBranch`, `completedAt`,
   `desktopVersion`). Only Hermes Desktop's own first-run bootstrap writes this
   normally.
2. `~/.local/bin/hermes` — a wrapper script on `PATH`. The check greps this
   file **literally** for the home path string to prove it points at the
   managed install — a wrapper that writes the unexpanded `$HOME` variable
   into the file text will NOT match a literal `grep -F` check. Expand the
   path when writing the heredoc.

Fix (safe once `hermes --version` already works):

```bash
# 1. Write the bootstrap marker, pinned to the commit actually checked out
PINNED=$(git -C ~/.hermes/hermes-agent rev-parse HEAD)
cat > ~/.hermes/hermes-agent/.hermes-bootstrap-complete <<EOF
{
  "schemaVersion": 1,
  "pinnedCommit": "$PINNED",
  "pinnedBranch": "main",
  "completedAt": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "desktopVersion": "$(pacman -Q hermes-desktop 2>/dev/null | awk '{print $2}')"
}
EOF

# 2. Write the PATH wrapper with the LITERAL expanded path, not $HOME
mkdir -p ~/.local/bin
cat > ~/.local/bin/hermes <<EOF
#!/bin/bash
exec "$HOME/.hermes/hermes-agent/venv/bin/hermes" "\$@"
EOF
chmod +x ~/.local/bin/hermes

# 3. Verify, then set the default
omarchy-install-hermes-cli --check && echo OK
omarchy default agent hermes
cat ~/.config/omarchy/defaults/agent   # -> hermes
```

Do not reinstall or repair Hermes itself to fix this — the runtime is already
fine; only the two Omarchy-side check artifacts were missing.

## References

- [`references/troubleshooting-guides.md`](references/troubleshooting-guides.md) - Session-specific troubleshooting recipes
- [`references/aur-build-failures.md`](references/aur-build-failures.md) - When AUR source builds fail, try -bin variants and manual pacman install
