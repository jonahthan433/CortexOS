from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Settings:
    root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    vault: Path = Path("vault")
    skills: Path = Path("skills")
    backend: str = "codex"
    fast_model_enabled: bool = False
    fast_model_base_url: str = "http://127.0.0.1:11434/v1"
    fast_model_name: str = "qwen2.5:3b"
    fast_model_api_key: str = "local"
    providers: dict[str, dict[str, Any]] | None = None
    voice_enabled: bool = True
    voice_stt_model: str = "base.en"
    voice_stt_device: str = "cpu"
    voice_stt_compute_type: str = "int8"
    voice_tts_language: str = "a"
    voice_tts_name: str = "af_heart"
    voice_browser_fallback: bool = True
    bridge_token: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        config_path = Path(path or os.environ.get("CORTEXOS_CONFIG", "config/cortexos.toml"))
        config_path = config_path.expanduser().resolve()
        data: dict[str, Any] = {}
        if config_path.exists():
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        base = config_path.parent.parent if config_path.parent.name == "config" else Path.cwd()
        token_path = config_path.parent / "cortexos.token"
        token = os.environ.get("CORTEXOS_TOKEN", "")
        if not token:
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except OSError:
                token = secrets.token_urlsafe(32)
                token_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    token = token_path.read_text(encoding="utf-8").strip()
                else:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(token + "\n")
        if token_path.exists():
            try:
                token_path.chmod(0o600)
            except OSError:
                pass
        bridge = data.get("bridge", {})
        router = data.get("router", {})
        voice = data.get("voice", {})
        return cls(
            root=base,
            host=bridge.get("host", "127.0.0.1"),
            port=int(bridge.get("port", 8765)),
            vault=(base / bridge.get("vault", "./vault")).resolve(),
            skills=(base / bridge.get("skills", "./skills")).resolve(),
            backend=bridge.get("backend", "codex"),
            fast_model_enabled=bool(router.get("fast_model_enabled", False)),
            fast_model_base_url=router.get("fast_model_base_url", "http://127.0.0.1:11434/v1"),
            fast_model_name=router.get("fast_model_name", "qwen2.5:3b"),
            fast_model_api_key=router.get("fast_model_api_key", "local"),
            providers=data.get("providers", {}),
            voice_enabled=bool(voice.get("enabled", True)),
            voice_stt_model=voice.get("stt_model", "base.en"),
            voice_stt_device=voice.get("stt_device", "cpu"),
            voice_stt_compute_type=voice.get("stt_compute_type", "int8"),
            voice_tts_language=voice.get("tts_language", "a"),
            voice_tts_name=voice.get("tts_voice", "af_heart"),
            voice_browser_fallback=bool(voice.get("browser_fallback", True)),
            bridge_token=token,
        )
