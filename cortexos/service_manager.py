from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


UNIT_NAME = "cortexos.service"


def _quote(value: str) -> str:
    """Quote a value for a systemd unit file, not for a shell."""
    # systemd parses quotes itself; only escape % and inner double quotes.
    value = value.replace("%", "%%").replace('"', '\"')
    return f'"{value}"'


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("systemctl")
    if not executable or platform.system() != "Linux":
        raise RuntimeError("CortexOS service commands require systemd on Linux.")
    result = subprocess.run([executable, "--user", *args], text=True, capture_output=True)
    if check and result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(details or f"systemctl --user {' '.join(args)} failed")
    return result


def _unit_text(root: Path) -> str:
    config_path = Path(os.environ.get("CORTEXOS_CONFIG", root / "config" / "cortexos.toml")).expanduser().resolve()
    paths = list(dict.fromkeys([os.environ.get("PATH", os.defpath), os.defpath]))
    path_value = os.pathsep.join(paths)
    return "\n".join([
        "[Unit]",
        "Description=CortexOS personal agent Bridge",
        "After=default.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={str(root)}",
        f"Environment={_quote('CORTEXOS_CONFIG=' + str(config_path))}",
        f"Environment={_quote('PATH=' + path_value)}",
        f"ExecStart={_quote(sys.executable)} {_quote('-c')} {_quote('from cortexos.cli import main; main()')} serve",
        "Restart=always",
        "RestartSec=3",
        "KillMode=control-group",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def manage(action: str, root: Path) -> int:
    root = root.expanduser().resolve()
    unit_path = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    if action == "install":
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(_unit_text(root), encoding="utf-8")
        unit_path.chmod(0o600)
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", UNIT_NAME)
        print(f"Installed and started {UNIT_NAME}. It will start with your user session.")
        print("For startup before login, enable user lingering with: sudo loginctl enable-linger \"$USER\"")
        return 0
    if action == "uninstall":
        _systemctl("disable", "--now", UNIT_NAME, check=False)
        unit_path.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        print(f"Removed {UNIT_NAME}.")
        return 0
    commands = {
        "start": ("start", UNIT_NAME),
        "stop": ("stop", UNIT_NAME),
        "restart": ("restart", UNIT_NAME),
        "status": ("status", "--no-pager", UNIT_NAME),
    }
    result = _systemctl(*commands[action], check=action != "status")
    output = result.stdout.strip() or result.stderr.strip()
    if output:
        print(output)
    return result.returncode
