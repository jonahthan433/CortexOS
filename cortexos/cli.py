from __future__ import annotations

import argparse
import asyncio
import shutil
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
    sub.add_parser("token", help="Print the local Bridge token for the HUD or plugin")
    ask = sub.add_parser("ask", help="Submit a prompt through the router")
    ask.add_argument("prompt")
    ask.add_argument("--backend", choices=("codex", "claude"))
    ask.add_argument("--skill")
    args = parser.parse_args()
    if args.command == "init-vault":
        Vault(args.path.expanduser().resolve()).bootstrap()
        source = Path(__file__).resolve().parent.parent / "AGENTS.md"
        if source.exists() and not (args.path / "AGENTS.md").exists():
            shutil.copyfile(source, args.path / "AGENTS.md")
        print(f"Initialized CortexOS vault at {args.path.expanduser().resolve()}")
    elif args.command == "serve":
        import uvicorn
        settings = Settings.load()
        uvicorn.run("cortexos.api:app", host=settings.host, port=settings.port, reload=False)
    elif args.command == "token":
        print(Settings.load().bridge_token)
    else:
        result = asyncio.run(CortexService(Settings.load()).submit(args.prompt, args.backend, args.skill))
        print(result.get("result") or result)
