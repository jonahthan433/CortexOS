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
    fast_model_max_tokens: int = 0
    jev_enabled: bool = False
    jev_base_url: str = "https://openrouter.ai/api"
    jev_api_key: str = ""
    jev_model: str = "typesafe/jev-1.13"
    jev_min_confidence: float = 0.5
    jev_timeout_seconds: float = 10.0
    fast_model_timeout_seconds: float = 45.0
    queue_enabled: bool = True
    queue_db: Path = Path("state/queue.db")
    queue_workers: int = 2
    queue_max_attempts: int = 3
    queue_backoff_base_seconds: float = 30.0
    queue_backoff_cap_seconds: float = 900.0
    queue_task_timeout_seconds: float = 900.0
    queue_dedupe_window_seconds: float = 30.0
    queue_retention_days: float = 30.0
    queue_poll_interval_seconds: float = 0.5
    providers: dict[str, dict[str, Any]] | None = None
    llm_providers: dict[str, dict[str, Any]] | None = None
    llm_default_timeout_seconds: float = 60.0
    voice_enabled: bool = True
    voice_stt_model: str = "base.en"
    voice_stt_device: str = "cpu"
    voice_stt_compute_type: str = "int8"
    voice_tts_language: str = "a"
    voice_tts_name: str = "af_heart"
    voice_browser_fallback: bool = True
    bridge_token: str = ""
    connectors_enabled: bool = True
    connectors_state_dir: Path = Path("state/connectors")
    connectors_client_dir: Path = Path("config/connectors")
    telegram_enabled: bool = False
    telegram_allow: tuple[str, ...] = ()
    telegram_env_file: Path = Path("/home/hermes/.hermes/.env")
    telegram_token_file: Path = Path("/etc/cortexos/telegram-bridge.token")
    telegram_send_command: str = "/opt/cortexos/hermes-run"
    telegram_send_timeout_seconds: float = 60.0
    telegram_notify_retries: bool = False
    telegram_rate_per_minute: int = 20
    telegram_max_reply_chars: int = 3500

    @property
    def agent_ids(self) -> list[str]:
        configured = list((self.providers or {}).keys())
        return list(dict.fromkeys(["codex", "claude", *configured]))

    @property
    def agents(self) -> list[dict[str, str]]:
        labels = {"codex": "Codex", "claude": "Claude Code", "opencode": "OpenCode"}
        result = []
        for agent_id in self.agent_ids:
            config = (self.providers or {}).get(agent_id, {})
            result.append({"id": agent_id, "name": str(config.get("name") or labels.get(agent_id) or agent_id.replace("-", " ").title())})
        return result

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
        queue = data.get("queue", {})
        llm = data.get("llm", {})
        connectors = data.get("connectors", {})
        telegram = data.get("telegram", {})
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
            fast_model_max_tokens=int(router.get("fast_model_max_tokens", 0) or 0),
            jev_enabled=bool(router.get("jev_enabled", False)),
            jev_base_url=router.get("jev_base_url", "https://openrouter.ai/api"),
            jev_api_key=router.get("jev_api_key", ""),
            jev_model=router.get("jev_model", "typesafe/jev-1.13"),
            jev_min_confidence=float(router.get("jev_min_confidence", 0.5)),
            jev_timeout_seconds=float(router.get("jev_timeout_seconds", 10)),
            fast_model_timeout_seconds=float(router.get("fast_model_timeout_seconds", 45)),
            queue_enabled=bool(queue.get("enabled", True)),
            queue_db=(base / queue.get("db_path", "state/queue.db")).resolve(),
            queue_workers=max(1, int(queue.get("workers", 2))),
            queue_max_attempts=max(1, int(queue.get("max_attempts", 3))),
            queue_backoff_base_seconds=float(queue.get("backoff_base_seconds", 30)),
            queue_backoff_cap_seconds=float(queue.get("backoff_cap_seconds", 900)),
            queue_task_timeout_seconds=float(queue.get("task_timeout_seconds", 900)),
            queue_dedupe_window_seconds=float(queue.get("dedupe_window_seconds", 30)),
            queue_retention_days=float(queue.get("retention_days", 30)),
            queue_poll_interval_seconds=float(queue.get("poll_interval_seconds", 0.5)),
            providers=data.get("providers", {}),
            llm_providers=llm.get("providers", {}),
            llm_default_timeout_seconds=float(llm.get("default_timeout_seconds", 60)),
            voice_enabled=bool(voice.get("enabled", True)),
            voice_stt_model=voice.get("stt_model", "base.en"),
            voice_stt_device=voice.get("stt_device", "cpu"),
            voice_stt_compute_type=voice.get("stt_compute_type", "int8"),
            voice_tts_language=voice.get("tts_language", "a"),
            voice_tts_name=voice.get("tts_voice", "af_heart"),
            voice_browser_fallback=bool(voice.get("browser_fallback", True)),
            bridge_token=token,
            connectors_enabled=bool(connectors.get("enabled", True)),
            connectors_state_dir=(base / connectors.get("state_dir", "state/connectors")).resolve(),
            connectors_client_dir=(base / connectors.get("client_dir", "config/connectors")).resolve(),
            telegram_enabled=bool(telegram.get("enabled", False)),
            telegram_allow=tuple(str(item) for item in (telegram.get("allow") or []) if str(item).strip()),
            telegram_env_file=Path(base / str(telegram.get("env_file", "/home/hermes/.hermes/.env"))).resolve(),
            telegram_token_file=Path(base / str(telegram.get("token_file", "/etc/cortexos/telegram-bridge.token"))).resolve(),
            telegram_send_command=str(telegram.get("send_command") or "/opt/cortexos/hermes-run"),
            telegram_send_timeout_seconds=float(telegram.get("send_timeout_seconds", 60)),
            telegram_notify_retries=bool(telegram.get("notify_retries", False)),
            telegram_rate_per_minute=max(1, int(telegram.get("rate_per_minute", 20))),
            telegram_max_reply_chars=max(200, int(telegram.get("max_reply_chars", 3500))),
        )
