from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shutil
import sys
from pathlib import Path

from .config import Settings
from .service import CortexService
from .vault import Vault


def main() -> None:
    parser = argparse.ArgumentParser(prog="cortexos")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-vault", help="Create CortexOS folders and index in an Obsidian vault")
    init.add_argument("path", type=Path)
    sub.add_parser("serve", help="Start the local Bridge API")
    service = sub.add_parser("service", help="Manage the always-on per-user CortexOS Bridge")
    service.add_argument("action", choices=("install", "start", "stop", "restart", "status", "uninstall"))
    sub.add_parser("token", help="Print the local Bridge token for the HUD or plugin")
    auth = sub.add_parser("auth", help="Manage dashboard password sign-in (login page)")
    auth.add_argument("action", choices=("status", "set-password", "clear"))
    auth.add_argument("--yes", action="store_true", help="Skip the 'clear' confirmation prompt")
    ask = sub.add_parser("ask", help="Submit a prompt through the router")
    ask.add_argument("prompt")
    ask.add_argument("skill_id", nargs="?", help="Optional skill id; omit for automatic routing")
    ask.add_argument("--backend", help="Configured agent ID; omit to use automatic tier routing")
    ask.add_argument("--skill", dest="skill_flag", help="Alternative to the positional skill id")
    connectors = sub.add_parser("connectors", help="Inspect or authorize connectors (OAuth setup)")
    connectors.add_argument("action", choices=("status", "authorize", "complete", "revoke"))
    connectors.add_argument("connector_id", nargs="?", help="gmail, calendar, drive, or youtube")
    connectors.add_argument("state", nargs="?", help="OAuth state printed by 'authorize'")
    connectors.add_argument("code", nargs="?", help="OAuth code copied from the redirect URL")
    args = parser.parse_args()
    if args.command == "init-vault":
        vault_path = args.path.expanduser().resolve()
        missing_instructions = {name: not (vault_path / name).exists() for name in ("AGENTS.md", "CLAUDE.md")}
        Vault(vault_path).bootstrap()
        templates = Path(__file__).resolve().parent.parent / "vault-template"
        for name, was_missing in missing_instructions.items():
            source = templates / name
            if was_missing and source.exists():
                shutil.copyfile(source, vault_path / name)
        print(f"Initialized CortexOS vault at {vault_path}")
    elif args.command == "serve":
        import uvicorn
        settings = Settings.load()
        cert = settings.root / "config" / "server.crt"
        key = settings.root / "config" / "server.key"
        ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)} if cert.exists() and key.exists() else {}
        # Bind LAN-wide when TLS is on (loopback clients still reach it); loopback otherwise.
        host = "0.0.0.0" if ssl_kwargs else settings.host
        uvicorn.run("cortexos.api:app", host=host, port=settings.port, reload=False, **ssl_kwargs)
    elif args.command == "token":
        print(Settings.load().bridge_token)
    elif args.command == "auth":
        from .auth import AuthError, AuthStore
        store = AuthStore(Settings.load().root / "config" / "auth.json")
        if args.action == "status":
            state = store.status()
            print(f"dashboard password: {'set' if state['has_password'] else 'not set'}")
            print(f"auth file: {store.path}")
        elif args.action == "set-password":
            if sys.stdin.isatty():
                first = getpass.getpass("New dashboard password: ")
                again = getpass.getpass("Repeat password: ")
                if first != again:
                    print("Passwords do not match.", file=sys.stderr)
                    raise SystemExit(1)
            else:
                first = sys.stdin.readline().rstrip("\n")
            try:
                store.set_password(first)
            except AuthError as exc:
                print(f"Could not set password: {exc}", file=sys.stderr)
                raise SystemExit(1) from exc
            print("Dashboard password updated; existing sessions were signed out.")
        else:  # clear
            if not args.yes:
                if not sys.stdin.isatty():
                    print("Refusing to clear without --yes on a non-interactive input.", file=sys.stderr)
                    raise SystemExit(2)
                answer = input("Remove the dashboard password and return to first-run setup? [y/N] ").strip().lower()
                if answer != "y":
                    print("Aborted.")
                    return
            store.clear()
            print("Dashboard password removed; the next visit asks to create one.")
    elif args.command == "service":
        from .service_manager import manage
        try:
            raise SystemExit(manage(args.action, Path(__file__).resolve().parent.parent))
        except RuntimeError as exc:
            print(f"CortexOS service error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    elif args.command == "connectors":
        from .connectors import ConnectorError, ConnectorRuntime
        runtime = ConnectorRuntime(Settings.load())
        if args.action == "status":
            print(json.dumps(runtime.status(), indent=2))
        elif not args.connector_id:
            print("A connector id is required (gmail, calendar, drive, youtube).", file=sys.stderr)
            raise SystemExit(2)
        elif args.action == "authorize":
            try:
                start = runtime.oauth_start(args.connector_id)
            except ConnectorError as exc:
                print(f"Cannot start authorization: {exc}", file=sys.stderr)
                raise SystemExit(1) from exc
            print("Open this URL, approve access, then copy the code parameter from the final redirect URL:")
            print(start["url"])
            print(f"state: {start['state']}")
            print(f"then run: cortexos connectors complete {args.connector_id} {start['state']} <code>")
        elif args.action == "complete":
            if not args.state or not args.code:
                print("Usage: cortexos connectors complete <connector_id> <state> <code>", file=sys.stderr)
                raise SystemExit(2)
            try:
                result = asyncio.run(runtime.oauth_exchange(args.connector_id, args.state, args.code))
            except ConnectorError as exc:
                print(f"Authorization failed: {exc}", file=sys.stderr)
                raise SystemExit(1) from exc
            print(json.dumps(result, indent=2))
        elif args.action == "revoke":
            try:
                print(json.dumps(runtime.revoke(args.connector_id), indent=2))
            except ConnectorError as exc:
                print(f"Revoke failed: {exc}", file=sys.stderr)
                raise SystemExit(1) from exc
    else:
        result = asyncio.run(CortexService(Settings.load()).submit(args.prompt, args.backend, args.skill_id or args.skill_flag))
        print(result.get("result") or result)
