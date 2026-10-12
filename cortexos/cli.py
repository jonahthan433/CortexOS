from __future__ import annotations

import argparse
import asyncio
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
    ask = sub.add_parser("ask", help="Submit a prompt through the router")
    ask.add_argument("prompt")
    ask.add_argument("skill_id", nargs="?", help="Optional skill id; omit for automatic routing")
    ask.add_argument("--backend", help="Configured agent ID; omit to use automatic tier routing")
    ask.add_argument("--skill", dest="skill_flag", help="Alternative to the positional skill id")
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
        uvicorn.run("cortexos.api:app", host=settings.host, port=settings.port, reload=False)
    elif args.command == "token":
        print(Settings.load().bridge_token)
    elif args.command == "service":
        from .service_manager import manage
        try:
            raise SystemExit(manage(args.action, Path(__file__).resolve().parent.parent))
        except RuntimeError as exc:
            print(f"CortexOS service error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    else:
        result = asyncio.run(CortexService(Settings.load()).submit(args.prompt, args.backend, args.skill_id or args.skill_flag))
        print(result.get("result") or result)
