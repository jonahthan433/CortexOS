# omarchy-desktop-rendering

# Omarchy Desktop Rendering Troubleshooting

Diagnosing why an Omarchy (Hyprland/Wayland) desktop renders wrong: shrunk into
a small square, tiny floating windows, missing bar or background, Xwayland
fallback, or incorrect scaling.

**The `omarchy` skill covers config *authoring*.** This skill covers *why the
resulting desktop looks broken* and how to close the loop on the symptom.

## Core Principle: verify the symptom, not the config

A display bug can persist while every config file looks correct. `monitors.lua`
can be right, `hyprctl configerrors` can be clean, and the desktop can still
render wrong. **Never report "root cause fixed" on config state alone.**

When the user says a previous fix didn't work, treat the earlier diagnosis as
suspect and re-derive from the live symptom.

## Diagnostic Order

Run these in order — each rules out a layer before you touch config.

### 1. Is the panel itself correct?
```bash
export HYPRLAND_INSTANCE_SIGNATURE=<sig>   # from ls /run/user/$(id -u)/hypr/
export XDG_RUNTIME_DIR=/run/user/$(id -u)
hyprctl monitors -j
```
Native resolution, expected `scale`, `transform: 0`. If this is wrong it IS a
monitor-config bug. If it is correct, keep going — the bug is elsewhere.

### 2. Did the shell's layer surfaces attach?
```bash
hyprctl layers          # raw output, NOT -j
```
Look for `namespace: omarchy-bar` and `namespace: omarchy-background`.
Missing = the Omarchy shell (quickshell) failed to attach as a layer client.

### 3. Are there degenerate fallback windows?
```bash
hyprctl clients -j
```
Tiny floating windows (e.g. 80x80, 1x24) with empty `class` and
`title: "quickshell"` are shell surfaces that fell back to Xwayland.

### 4. What does the shell's own log say?
```bash
journalctl --user --since today _PID=$(pgrep -f 'quickshell -n -p') \
  | grep -iE 'WlrLayershell|QT_QPA_PLATFORM|WAYLAND_DISPLAY'
```
Authoritative error strings:
- `Could not create attached properties object 'qs::wayland::layershell::WlrLayershell'`
  → layer-shell attach failing; Qt is running on X11.
- `WAYLAND_DISPLAY is present but QT_QPA_PLATFORM is "xcb"`
  → same root cause, stated outright by quickshell.

### 5. Is there stacked scaling?
```bash
gsettings get org.gnome.desktop.interface text-scaling-factor
```
Must be `1.0` when Hyprland uses a fractional output scale (e.g. 1.25).
A value >1 multiplies on top of the compositor scale and shrinks Xwayland
clients into a small central region.

## Root Cause: forced X11 Qt platform

**Never force `QT_QPA_PLATFORM=xcb` in `~/.config/hypr/hyprland.lua`.**

Omarchy's default is `wayland;xcb` (`$OMARCHY_PATH/default/hypr/envs.lua`).
Forcing `xcb` makes quickshell an Xwayland client, so every layer surface —
bar, background, notifications, OSD, menu, keyboard panel — fails to attach and
falls back to tiny floating X windows centered on screen. The user sees exactly
"the desktop shrunk into a small square."

If one Qt app genuinely needs xcb (SteamVR's bundled Qt5 only ships the xcb
plugin), override it **per-app in `autostart.lua`**, never session-wide.

## Full Fix Sequence

All steps are required; skipping the env step is the usual reason a "fix"
appears not to take.

1. Remove the `QT_QPA_PLATFORM` override from `~/.config/hypr/hyprland.lua`.
2. ```bash
   hyprctl reload && hyprctl configerrors   # must be clean
   gsettings set org.gnome.desktop.interface text-scaling-factor 1.0
   ```
3. **`hyprctl reload` does not update the systemd user session env.** The stale
   value persists and every newly spawned shell inherits it:
   ```bash
   systemctl --user set-environment QT_QPA_PLATFORM=wayland
   omarchy restart shell
   ```
4. Verify live, all three must pass:
   - `hyprctl layers` shows `omarchy-bar` + `omarchy-background`
   - `hyprctl clients -j` shows no tiny quickshell windows
   - shell journal log has zero `WlrLayershell` errors

## Pitfalls

- **Parsing `hyprctl layers -j`**: the JSON nests surfaces under
  `levels -> <n> -> [ ... ]`; naive parsers report "no layers" when the bar is
  in fact attached. Read the raw `hyprctl layers` text output to be sure.
- **Session env vs config env**: `hl.env()` only affects processes spawned
  after it takes effect, and `hyprctl reload` does not refresh the systemd
  user environment. Use `systemctl --user set-environment`.
- **Backup files are evidence**: dated `monitors.lua.bak.*` files reveal what
  the config looked like shortly before a broken login. Compare them before
  assuming the current file is innocent.
- **A stale external-monitor rule** (e.g. `HDMI-A-2` at `1366x0`) can survive
  in a backup while the current file is clean — confirm which one is loaded.
- **Post-login persistence**: removing the `hl.env` line stops Hyprland from
  *setting* the variable, but if another source (uwsm, `environment.d`)
  exports it, the symptom returns on a fresh login. Verify after a real logout.

## Verification Discipline

A display fix is complete only when the exact symptom the user reported is
confirmed gone in the live session. Config correctness is necessary but not
sufficient. When a user reports "still the same after you said it was fixed",
re-open the whole diagnosis rather than re-asserting the previous conclusion.

## References

- [`references/rendering-case-log.md`](references/rendering-case-log.md) - worked
  case: shrunk-desktop root cause, exact commands, and verified output.
