# wifi-connectivity-troubleshooting

# WiFi Connectivity Troubleshooting

Diagnose from evidence, not guesswork. Read the actual failure mode from logs
before changing anything.

## Diagnosis Order

1. **Read the failure mode from logs** — NetworkManager journal carries the
   exact reason for every drop:

   ```bash
   journalctl -u NetworkManager --since "-14 days" --no-pager \
     | grep -iE "link timed out|ssid-not-found|Activation: failed|deauth" | tail -30
   ```

   Common reasons and their meaning:
   - `ssid-not-found` — the AP **vanished from scan** during the drop window.
     Driver-side channel loss, not the router kicking you off.
   - `ap-deauth` / deauth frames — the AP actively disassociated (interference,
     roaming policy, wrong password churn).
   - DHCP `timeout in 45 seconds` — link came back but no lease; DHCP-server
     side or lease-length problem.

2. **Check the driver for hardware/firmware errors** — if clean, driver is fine:

   ```bash
   journalctl -k --since "-14 days" | grep -i iwlwifi   # no hw_error/firmware fail = driver OK
   dmesg -T | grep -iE "iwlwifi|wlan" | tail -60
   ```

3. **Check signal quality** — marginal signal is the substrate for most drop
   bugs:

   ```bash
   iw dev <iface> station dump | grep -iE "signal|bitrate|beacon|inactive"
   iw dev <iface> link
   ```

   Thresholds: > -50 dBm excellent, -50 to -65 good, -65 to -75 marginal
   (latency spikes expected), < -75 weak (expect retries and disconnects).

4. **Rule out already-correct settings before changing them:**

   ```bash
   grep powersave /etc/NetworkManager/conf.d/*.conf    # wifi.powersave = 2 = off
   cat /sys/module/iwlwifi/parameters/bt_coex_active   # N = BT coex off
   cat /sys/module/iwlmvm/parameters/power_scheme      # 0 = active, 1/2 = sleep
   iw reg get                                            # country set (not 00)
   ```

## Recurring Drops: `ssid-not-found` on Intel iwlwifi

The signature for the LAR (Location Aware Regulatory) bug:

```
<warn> device (wlp58s0): link timed out.
<info> device (wlp58s0): state change: activated -> failed (reason 'ssid-not-found')
```

Mechanism: on the Intel 8260 at marginal 2.4GHz signal (~-72 dBm), LAR's
auto channel-roaming flushes the current channel during its internal scan and
never returns cleanly. The AP disappears from scan for 30–90s, then reappears.
Recurring, single-AP, no hardware errors — that's this bug.

Fix (highest-probability single change is `lar_disable=1`):

```bash
echo "options iwlwifi lar_disable=1" | sudo tee /etc/modprobe.d/iwlwifi-lar.conf
echo "options iwlmvm uapsd_disable=1 power_scheme=0" | sudo tee /etc/modprobe.d/iwlmvm-disable-ps.conf
# apply now via rmmod + modprobe; initramfs picks up files on next boot
sudo modprobe -r iwlmvm iwlwifi && sudo modprobe iwlwifi
nmcli device disconnect <iface> && nmcli connection up "<SSID>" ifname <iface>
```

Verify: signal should hold > -78 dBm instead of flapping into the -90s. If
drops persist, next levers: pin `802-11-wireless.bssid=<BSSID>` via nmcli, or
force 20MHz width.

## Applying modprobe changes

Write the option files under `/etc/modprobe.d/`, then either rmmod/modprobe for
an immediate effect (check which modules depend on yours — iwlmvm before
iwlwifi on the way out, reverse on the way back) or reboot to pick up the file
via initramfs. Always reconnect with nmcli after a module reload.

## Reference

See [`references/intel-8260-drop-fix.md`](references/intel-8260-drop-fix.md)
for the full session walkthrough of the `ssid-not-found` LAR fix on the Intel
8260 (Omarchy, 30 drops in 14 days, fix applied, outcome unconfirmed).

## Outcome honesty

The LAR/U-APSD fix was applied and persists across reboots, but the user never
confirmed drops stopped. Until a follow-up shows zero `link timed out` events
over a week, treat this as *applied, unverified* — if the user reports drops
continue, do not repeat the same fix; go straight to the escalation ladder
(BSSID pin → 20MHz → physical).
