# hermes-desktop-launch

# Hermes Desktop launch repair

## Trigger
User reports the Hermes Desktop GUI won't start. Symptom strings:
- `Missing X server or $DISPLAY` + `The platform failed to initialize. Exiting.`
- `xinit: unable to run server "/usr/bin/X": No such file or directory`
- Electron/Chromium Ozone platform init failure on Linux.

Or: user reports `hermes update` keeps rebuilding/compiling the desktop app
on a server that has no GUI and never runs `hermes desktop` — see the
headless section below instead of the display fixes.

## Root cause (read first — the launcher is NOT broken)
`hermes_cli/main.py` is fine. It inherits the user's `DISPLAY`/`WAYLAND_DISPLAY`
and appends `desktop.electron_flags` to the Electron launch command
(main.py ~8073). The failure is **environmental**: Electron's default ozone
backend is **X11**, which needs an X display server. On a **Wayland session**
(Fedora KDE 6 Plasma defaults to Wayland) there is no X11 `:0` — so
`DISPLAY=:0` is inert (nothing listens there) and Electron dies.
`startx` fails when **Xorg isn't installed** (`/usr/bin/X` missing).

The `chrome-sandbox ✗` line printed before launch is **non-fatal** — the app
still launches. Do NOT chase it.

## Diagnose (run on the user's machine — the agent cannot reach the laptop)
```bash
echo "type=$XDG_SESSION_TYPE DISPLAY=$DISPLAY WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
ls -l $XDG_RUNTIME_DIR/wayland-* 2>/dev/null
which Xorg Xvfb 2>/dev/null
```
- `XDG_SESSION_TYPE=wayland` + a `wayland-*` socket → Fix A.
- `XDG_SESSION_TYPE=tty` (no socket) → user is at a text console; start the GUI:
  `sudo systemctl start sddm` then log into Plasma, then `hermes desktop`.
- `which Xorg` empty → Xorg missing; install it or use Fix B (Xvfb).

## Fix A — Wayland (most common on Fedora KDE)
Persistent, via the existing config knob (no launcher patch needed):
```bash
hermes config set desktop.electron_flags '["--ozone-platform=wayland"]'
hermes desktop
```
One-shot env equivalent:
```bash
export ELECTRON_OZONE_PLATFORM_HINT=wayland
hermes desktop
```

## Fix B — headless / no real display (VNC, screenshot, CI)
```bash
sudo dnf install -y xorg-x11-server-Xvfb        # Fedora
Xvfb :99 -screen 0 1280x800x24 & sleep 2
DISPLAY=:99 hermes desktop
```
Debian/Ubuntu: `sudo apt-get install -y xvfb`.

## Notes
- `desktop.electron_flags` default is `[]` (config_defaults.py ~3568); a single
  string is also accepted and shell-split. Other useful values:
  `["--ozone-platform=x11"]` on headless/VM X11 hosts, or GPU workaround flags.
- `desktop.disable_gpu` (`"auto"`/`true`/`false`) bridges to
  `HERMES_DESKTOP_DISABLE_GPU`; `"auto"` already disables GPU only on detected
  remote displays (SSH/VNC/RDP). Rarely the cause of a launch failure.
- The agent runs in a remote container (CT103) and cannot reach the user's
  laptop; these commands are for the user to paste locally. Ask the user for
  diagnostic output rather than guessing.

## Pitfalls
- Setting `DISPLAY=:0` without an X server only hides the real problem.
- Don't reinstall/rebuild the desktop app for a display error — the build is
  fine; the launch env is the issue.
- Don't treat `chrome-sandbox ✗` as the blocker; it is non-fatal.

See references/wayland-x11-fix.md for the verbatim error log and the
main.py flag-append excerpt used to confirm root cause.

## Headless server: stop `hermes update` from rebuilding Desktop

### Root cause
`hermes_cli/update_cmd_deps.py::_rebuild_desktop_after_update` treats Desktop
as "installed" — and therefore rebuilds it on every `hermes update` — if
**any** of these exist, even on a box that has never run a GUI:
- a packaged Electron executable, or `apps/desktop/dist/index.html`
  (`_desktop_app_present`)
- `$HERMES_HOME/desktop-build-stamp.json` (survives on purpose so a swap
  that lost artifacts mid-update still gets rebuilt — but it also means a
  stray stamp from one manual/accidental build keeps triggering rebuilds
  forever after)

On a headless server this is pure waste: an Electron/npm build burns
minutes and hundreds of MB (`node_modules` + `release/` alone commonly
300-350MB each) every update, for an app that will never be launched.

### Fix — delete the artifacts once
```bash
rm -f ~/.hermes/desktop-build-stamp.json
rm -rf <repo>/apps/desktop/dist <repo>/apps/desktop/release <repo>/apps/desktop/node_modules
```
With all three signals gone, the next `hermes update` skips the Desktop
rebuild stage entirely. No config flag exists to opt out while artifacts are
present — the presence check IS the trigger, so removal is the only lever.

### Pitfalls
- A single `hermes update` run that started **while** any artifact still
  existed captures `had_desktop_app_before_update=True` for that run and will
  rebuild one more time even after you clean up mid-run — clean up, then let
  the *next* update run confirm it stays skipped, don't judge from the run
  you cleaned up during.
- Don't chase this as a code bug — the check is intentional (protects users
  who lost Desktop artifacts mid-swap). The fix on a headless box is deleting
  the leftover build, not patching the updater.
