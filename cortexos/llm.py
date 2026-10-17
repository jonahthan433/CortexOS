"""Universal provider layer: registry, model gateway, agent assignments.

Boundaries (deliberate, see ledger):
- Providers are *inference endpoints* (openai-compatible, Anthropic, Gemini,
  Ollama). CLI coding agents (codex/claude/hermes) stay execution adapters and
  are never faked as chat endpoints.
- JEV tier routing stays deterministic and separate; it never goes through
  this gateway.
- Secrets live server-side only. API keys are read from config or environment,
  never returned by any endpoint, never logged, and scrubbed from error text.
- Changing an agent's provider does not change its system instructions,
  memory, permissions, or safety policy: the same agent_system_prompt() text
  and the same approval gate apply to every execution path.
"""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .config import Settings
from .providers import ProviderError, agent_system_prompt

PROVIDER_KINDS = ("openai_compatible", "anthropic", "gemini", "ollama")
CAPABILITY_NAMES = ("chat", "streaming", "json_mode", "tools", "vision")
REQUIRED_CAPABILITY = "chat"

# Conservative, honest per-kind defaults. Anything a kind cannot genuinely do
# stays False: unsupported calls are rejected clearly instead of failing weirdly.
KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai_compatible": {"base_url": "https://api.openai.com/v1", "capabilities": {"chat": True, "streaming": True, "json_mode": True, "tools": False, "vision": False}},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "capabilities": {"chat": True, "streaming": True, "json_mode": True, "tools": False, "vision": False}, "local": True},
    "anthropic": {"base_url": "https://api.anthropic.com", "capabilities": {"chat": True, "streaming": False, "json_mode": False, "tools": False, "vision": False}},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com", "capabilities": {"chat": True, "streaming": False, "json_mode": True, "tools": False, "vision": False}},
}

# HTTP statuses that are worth retrying without changing configuration.
RETRYABLE_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


async def emit(output, text: str) -> None:
    """Deliver streamed text to a sync or async sink."""
    result = output(text)
    if inspect.isawaitable(result):
        await result


class GatewayCancelled(ProviderError):
    """A human stop cancelled an in-flight provider call. Never retried, never
    converted into a fallback attempt."""


def is_local_url(url: str) -> bool:
    """True for loopback/private/link-local endpoints (never silently cloud-bound)."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    if host in {"localhost", ""}:
        return host == "localhost"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local") or host.endswith(".internal")
    return address.is_loopback or address.is_private or address.is_link_local


@dataclass
class ProviderSpec:
    id: str
    kind: str
    base_url: str
    api_key: str = ""
    key_source: str = "none"
    default_model: str = ""
    models: list[str] = field(default_factory=list)
    enabled: bool = True
    local: bool = False
    builtin: bool = False
    timeout_seconds: float = 60.0
    capabilities: dict[str, bool] = field(default_factory=dict)

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def supports(self, capabilities) -> list[str]:
        return [name for name in capabilities if not self.capabilities.get(name)]

    def public(self, health: dict | None = None) -> dict:
        """Masked view: safe for API responses and HUD display."""
        return {
            "id": self.id,
            "kind": self.kind,
            "base_url": self.base_url,
            "local": self.local,
            "enabled": self.enabled,
            "builtin": self.builtin,
            "default_model": self.default_model,
            "models": list(self.models),
            "capabilities": dict(self.capabilities),
            "has_key": self.has_key,
            "key_source": self.key_source if self.has_key else "none",
            "timeout_seconds": self.timeout_seconds,
            "health": health or {"status": "unknown"},
        }


def build_provider_specs(settings: Settings) -> dict[str, ProviderSpec]:
    """Registry from [llm.providers.*] plus the builtin fast-model entry
    (which keeps its historical [router] config keys working unchanged)."""
    specs: dict[str, ProviderSpec] = {}
    for provider_id, entry in (settings.llm_providers or {}).items():
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind", "openai_compatible"))
        if kind not in PROVIDER_KINDS:
            kind = "openai_compatible"
        defaults = KIND_DEFAULTS[kind]
        api_key = str(entry.get("api_key") or "")
        key_source = "config" if api_key else "none"
        env_name = entry.get("api_key_env")
        if not api_key and env_name:
            api_key = os.environ.get(str(env_name), "")
            key_source = f"env:{env_name}" if api_key else f"env:{env_name} (unset)"
        capabilities = dict(defaults["capabilities"])
        for name, value in (entry.get("capabilities") or {}).items():
            if name in CAPABILITY_NAMES:
                capabilities[name] = bool(value)
        specs[provider_id] = ProviderSpec(
            id=provider_id,
            kind=kind,
            base_url=str(entry.get("base_url") or defaults["base_url"]).rstrip("/"),
            api_key=api_key,
            key_source=key_source if api_key else ("none" if not env_name else f"env:{env_name} (unset)"),
            default_model=str(entry.get("default_model") or ""),
            models=[str(model) for model in (entry.get("models") or [])],
            enabled=bool(entry.get("enabled", True)),
            local=bool(entry.get("local", defaults.get("local", False))),
            timeout_seconds=float(entry.get("timeout_seconds", settings.llm_default_timeout_seconds)),
            capabilities=capabilities,
        )
    specs["fast-model"] = ProviderSpec(
        id="fast-model",
        kind="openai_compatible",
        base_url=settings.fast_model_base_url.rstrip("/"),
        api_key=settings.fast_model_api_key,
        key_source="config",
        default_model=settings.fast_model_name,
        models=[settings.fast_model_name] if settings.fast_model_name else [],
        enabled=settings.fast_model_enabled,
        local=is_local_url(settings.fast_model_base_url),
        builtin=True,
        timeout_seconds=settings.fast_model_timeout_seconds,
        capabilities=dict(KIND_DEFAULTS["openai_compatible"]["capabilities"]),
    )
    return specs


@dataclass
class Assignment:
    agent_id: str
    provider: str
    model: str = ""
    name: str = ""
    fallback: list[dict[str, str]] = field(default_factory=list)
    allow_cloud_fallback: bool = False
    requirements: list[str] = field(default_factory=lambda: [REQUIRED_CAPABILITY])
    updated_at: float = 0.0

    def public(self) -> dict:
        return {"agent_id": self.agent_id, "name": self.name or self.agent_id, "provider": self.provider,
                "model": self.model, "fallback": [dict(item) for item in self.fallback],
                "allow_cloud_fallback": self.allow_cloud_fallback,
                "requirements": list(self.requirements), "updated_at": self.updated_at}


class AssignmentStore:
    """Agent->provider assignments live in state/ (operational state, gitignored).

    The secrets-bearing config/cortexos.toml is never written by the API.
    """

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        assignments = data.get("assignments", {})
        return assignments if isinstance(assignments, dict) else {}

    def save(self, assignments: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"assignments": assignments}, indent=2, sort_keys=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


@dataclass
class GatewayResult:
    text: str
    provider: str
    model: str
    attempts: list[dict] = field(default_factory=list)
    usage: dict | None = None
    elapsed_ms: int = 0
    fallback_used: bool = False
    streamed: bool = False

    def meta(self) -> dict:
        return {"provider": self.provider, "model": self.model, "usage": self.usage,
                "attempts": self.attempts, "elapsed_ms": self.elapsed_ms,
                "fallback_used": self.fallback_used}


class ModelGateway:
    """One invocation path for chat-completion providers.

    Normalizes request/response shapes, capability checks, timeouts,
    retryable-vs-terminal errors, streaming deltas, cancellation, usage and
    the explicit fallback policy. No silent local->cloud fallback, ever.
    """

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 assignment_path: Path | None = None):
        self.settings = settings
        self.transport = transport
        self.providers = build_provider_specs(settings)
        self.assignments_store = AssignmentStore(assignment_path or (settings.root / "state" / "llm_assignments.json"))
        self.assignments: dict[str, Assignment] = {}
        self.health: dict[str, dict] = {}
        self.last_error: str | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._interrupted: set[str] = set()
        self._load_assignments()

    # ------------------------------------------------------------ registry
    def _load_assignments(self) -> None:
        raw = self.assignments_store.load()
        loaded: dict[str, Assignment] = {}
        for agent_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            loaded[agent_id] = Assignment(
                agent_id=agent_id,
                provider=str(entry.get("provider", "")),
                model=str(entry.get("model", "")),
                name=str(entry.get("name", "")),
                fallback=[dict(item) for item in entry.get("fallback", []) if isinstance(item, dict)],
                allow_cloud_fallback=bool(entry.get("allow_cloud_fallback", False)),
                requirements=[str(item) for item in entry.get("requirements", [REQUIRED_CAPABILITY])],
                updated_at=float(entry.get("updated_at", 0) or 0),
            )
        self.assignments = loaded

    def reload(self) -> None:
        self.providers = build_provider_specs(self.settings)
        self._load_assignments()

    @property
    def agent_ids(self) -> list[str]:
        return sorted(self.assignments)

    def resolve(self, agent_id: str) -> Assignment | None:
        return self.assignments.get(agent_id)

    def providers_status(self) -> list[dict]:
        return [spec.public(self.health.get(spec.id)) for spec in sorted(self.providers.values(), key=lambda item: item.id)]

    def agents_status(self) -> list[dict]:
        result = [assignment.public() for assignment in sorted(self.assignments.values(), key=lambda item: item.agent_id)]
        for entry in result:
            entry["issues"] = self.validate_assignment(entry["agent_id"], entry)
        return result

    # ------------------------------------------------------------ secrets
    def scrub(self, text: str) -> str:
        cleaned = str(text)
        for spec in self.providers.values():
            if spec.api_key and len(spec.api_key) >= 8:
                cleaned = cleaned.replace(spec.api_key, "[redacted]")
        return cleaned

    def _detail(self, status: int, text: str) -> str:
        return self.scrub(text)[:300] if text else ""

    def _set_health(self, provider_id: str, status: str, detail: str = "", mode: str = "") -> None:
        self.health[provider_id] = {"status": status, "detail": detail, "mode": mode,
                                    "checked_at": time.time()}
        if status == "error":
            self.last_error = f"{provider_id}: {detail}"[:200]
        else:
            self.last_error = None

    # ------------------------------------------------------- assignments
    def validate_assignment(self, agent_id: str, entry: dict) -> list[str]:
        issues: list[str] = []
        if not agent_id or len(agent_id) > 64 or not all(c.isalnum() or c in "._-" for c in agent_id):
            return [f"invalid agent id: {agent_id!r}"]
        provider_id = str(entry.get("provider", ""))
        spec = self.providers.get(provider_id)
        if spec is None:
            issues.append(f"unknown provider: {provider_id or '(missing)'}")
        else:
            if not spec.enabled:
                issues.append(f"provider '{provider_id}' is disabled")
            model = str(entry.get("model") or spec.default_model)
            if not model:
                issues.append("no model set and the provider has no default_model")
            if spec.models and model not in spec.models:
                issues.append(f"model '{model}' is not in provider '{provider_id}' models list")
            if provider_id != "fast-model" and spec.kind != "ollama" and not spec.has_key:
                issues.append(f"provider '{provider_id}' has no API key configured ({spec.key_source})")
            missing = spec.supports([str(name) for name in entry.get("requirements", [REQUIRED_CAPABILITY])])
            if missing:
                issues.append(f"provider '{provider_id}' lacks required capabilities: {', '.join(missing)}")
        for index, fallback in enumerate(entry.get("fallback", []) or []):
            if len(issues) > 8:
                break
            fallback_provider = str(fallback.get("provider", ""))
            fallback_spec = self.providers.get(fallback_provider)
            if fallback_spec is None:
                issues.append(f"fallback[{index}]: unknown provider '{fallback_provider}'")
                continue
            fallback_model = str(fallback.get("model") or fallback_spec.default_model)
            if not fallback_model:
                issues.append(f"fallback[{index}]: no model set and provider has no default_model")
            if fallback_spec.models and fallback_model and fallback_model not in fallback_spec.models:
                issues.append(f"fallback[{index}]: model '{fallback_model}' is not in provider '{fallback_provider}' models list")
            if spec is not None and spec.local and not fallback_spec.local and not bool(entry.get("allow_cloud_fallback", False)):
                issues.append(f"fallback[{index}]: local->cloud fallback requires allow_cloud_fallback=true")
        return issues

    def save_assignment(self, agent_id: str, entry: dict) -> dict:
        issues = self.validate_assignment(agent_id, entry)
        if issues:
            raise ProviderError("; ".join(issues))
        spec = self.providers[str(entry.get("provider"))]
        assignment = Assignment(
            agent_id=agent_id,
            provider=spec.id,
            model=str(entry.get("model") or spec.default_model),
            name=str(entry.get("name", "")),
            fallback=[{"provider": str(item.get("provider")), "model": str(item.get("model") or (self.providers[str(item.get("provider"))].default_model if str(item.get("provider")) in self.providers else ""))}
                      for item in (entry.get("fallback") or [])],
            allow_cloud_fallback=bool(entry.get("allow_cloud_fallback", False)),
            requirements=[str(item) for item in entry.get("requirements", [REQUIRED_CAPABILITY])],
            updated_at=time.time(),
        )
        self.assignments[agent_id] = assignment
        self.assignments_store.save({key: value.public() | {"updated_at": value.updated_at} for key, value in self.assignments.items()})
        return assignment.public()

    def delete_assignment(self, agent_id: str) -> bool:
        if agent_id not in self.assignments:
            return False
        self.assignments.pop(agent_id)
        self.assignments_store.save({key: value.public() | {"updated_at": value.updated_at} for key, value in self.assignments.items()})
        return True

    # ------------------------------------------------------------ requests
    def _client(self, timeout: float) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": timeout}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.AsyncClient(**kwargs)

    async def call(self, provider_id: str, model: str, messages: list[dict], *,
                   temperature: float = 0.2, max_tokens: int | None = None, json_mode: bool = False,
                   on_output: Callable[[str], Any] | None = None, stream: bool = False,
                   timeout_override: float | None = None) -> GatewayResult:
        """Low-level single-provider call with capability validation (no fallback)."""
        spec = self.providers.get(provider_id)
        if spec is None:
            raise ProviderError(f"Unknown provider: {provider_id}")
        if not spec.enabled:
            raise ProviderError(f"Provider '{provider_id}' is disabled")
        model = model or spec.default_model
        if not model:
            raise ProviderError(f"Provider '{provider_id}' has no model configured")
        needed = [REQUIRED_CAPABILITY] + (["json_mode"] if json_mode else []) + (["streaming"] if stream else [])
        missing = spec.supports(needed)
        if missing:
            raise ProviderError(f"Provider '{provider_id}' does not support: {', '.join(missing)}")
        stream = stream and spec.capabilities.get("streaming", False) and on_output is not None and not json_mode
        started = time.time()
        timeout = min(spec.timeout_seconds, timeout_override) if timeout_override else spec.timeout_seconds
        try:
            async with self._client(timeout) as client:
                if spec.kind in ("openai_compatible", "ollama"):
                    result = await self._openai_call(client, spec, model, messages, temperature, max_tokens,
                                                     json_mode, stream, on_output)
                elif spec.kind == "anthropic":
                    result = await self._anthropic_call(client, spec, model, messages, temperature, max_tokens)
                elif spec.kind == "gemini":
                    result = await self._gemini_call(client, spec, model, messages, temperature, max_tokens, json_mode)
                else:  # pragma: no cover - kinds are validated at load time
                    raise ProviderError(f"Provider '{provider_id}' has unsupported kind '{spec.kind}'")
        except ProviderError:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = self._detail(status, self._safe_text(exc.response))
            retryable = status in RETRYABLE_STATUSES
            self._set_health(provider_id, "error", f"HTTP {status} {detail}", "inference")
            raise ProviderError(f"{provider_id}: HTTP {status} {detail}".strip(), retryable=retryable) from exc
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout, httpx.WriteTimeout) as exc:
            self._set_health(provider_id, "error", f"{type(exc).__name__}", "inference")
            raise ProviderError(f"{provider_id}: connection failed ({type(exc).__name__})", retryable=True) from exc
        except httpx.HTTPError as exc:
            self._set_health(provider_id, "error", f"{type(exc).__name__}", "inference")
            raise ProviderError(f"{provider_id}: transport error ({type(exc).__name__})", retryable=True) from exc
        except (ValueError, TypeError, KeyError) as exc:
            self._set_health(provider_id, "error", f"invalid response ({type(exc).__name__})", "inference")
            raise ProviderError(f"{provider_id}: invalid response ({type(exc).__name__})") from exc
        self._set_health(provider_id, "ok", "", "inference")
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    @staticmethod
    def _safe_text(response: httpx.Response) -> str:
        try:
            return response.text
        except Exception:
            return ""

    def _validate_shape(self, provider_id: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ProviderError(f"{provider_id}: unexpected response (not JSON object)")

    async def _openai_call(self, client: httpx.AsyncClient, spec: ProviderSpec, model: str, messages: list[dict],
                           temperature: float, max_tokens: int | None, json_mode: bool, stream: bool,
                           on_output) -> GatewayResult:
        url = spec.base_url + "/chat/completions"
        headers = {}
        if spec.api_key:
            headers["Authorization"] = f"Bearer {spec.api_key}"
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if stream:
            payload["stream"] = True
            chunks: list[str] = []
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        parsed = json.loads(data)
                    except ValueError:
                        continue
                    delta = ((parsed.get("choices") or [{}])[0].get("delta") or {}).get("content")
                    if delta:
                        chunks.append(delta)
                        await emit(on_output, delta)
            text = "".join(chunks).strip()
            if not text:
                raise ProviderError(f"{spec.id}: streamed response contained no text", retryable=True)
            return GatewayResult(text=text, provider=spec.id, model=model, streamed=True)
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        self._validate_shape(spec.id, data)
        try:
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            raise ProviderError(f"{spec.id}: unexpected response shape ({self._detail(0, json.dumps(data)[:200])})") from exc
        usage = data.get("usage") or {}
        normalized_usage = None
        if usage:
            normalized_usage = {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}
        return GatewayResult(text=text, provider=spec.id, model=model, usage=normalized_usage)

    async def _anthropic_call(self, client: httpx.AsyncClient, spec: ProviderSpec, model: str, messages: list[dict],
                              temperature: float, max_tokens: int | None) -> GatewayResult:
        url = spec.base_url + "/v1/messages"
        system = "\n\n".join(message["content"] for message in messages if message.get("role") == "system")
        user = "\n\n".join(message["content"] for message in messages if message.get("role") != "system")
        payload = {"model": model, "system": system, "max_tokens": max_tokens or 4096, "temperature": temperature,
                   "messages": [{"role": "user", "content": user}]}
        response = await client.post(url, headers={"x-api-key": spec.api_key, "anthropic-version": "2023-06-01"}, json=payload)
        response.raise_for_status()
        data = response.json()
        self._validate_shape(spec.id, data)
        try:
            parts = [block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"]
            text = "".join(parts).strip()
        except (AttributeError, TypeError) as exc:
            raise ProviderError(f"{spec.id}: unexpected response shape") from exc
        usage = data.get("usage") or {}
        normalized = {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")} if usage else None
        return GatewayResult(text=text, provider=spec.id, model=model, usage=normalized)

    async def _gemini_call(self, client: httpx.AsyncClient, spec: ProviderSpec, model: str, messages: list[dict],
                           temperature: float, max_tokens: int | None, json_mode: bool) -> GatewayResult:
        url = f"{spec.base_url}/v1beta/models/{model}:generateContent"
        system = "\n\n".join(message["content"] for message in messages if message.get("role") == "system")
        user = "\n\n".join(message["content"] for message in messages if message.get("role") != "system")
        generation: dict[str, Any] = {"temperature": temperature}
        if max_tokens:
            generation["maxOutputTokens"] = max_tokens
        if json_mode:
            generation["responseMimeType"] = "application/json"
        payload: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": user}]}],
                                   "generationConfig": generation}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        response = await client.post(url, headers={"x-goog-api-key": spec.api_key}, json=payload)
        response.raise_for_status()
        data = response.json()
        self._validate_shape(spec.id, data)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts).strip()
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            raise ProviderError(f"{spec.id}: unexpected response shape ({self._detail(0, json.dumps(data)[:200])})") from exc
        usage = data.get("usageMetadata") or {}
        normalized = {"input_tokens": usage.get("promptTokenCount"), "output_tokens": usage.get("candidatesTokenCount")} if usage else None
        return GatewayResult(text=text, provider=spec.id, model=model, usage=normalized)

    # ------------------------------------------------------------- agent path
    def _attempt_targets(self, assignment: Assignment) -> list[dict[str, Any]]:
        primary = self.providers.get(assignment.provider)
        model = assignment.model or (primary.default_model if primary else "")
        targets = [{"provider": assignment.provider, "model": model}]
        for fallback in assignment.fallback:
            spec = self.providers.get(str(fallback.get("provider")))
            if spec is None:
                continue
            targets.append({"provider": spec.id, "model": str(fallback.get("model") or spec.default_model)})
        return targets

    async def generate(self, agent_id: str, prompt: str, *, skill_instructions: str = "", approved: bool = False,
                       request_id: str | None = None, on_output=None, json_mode: bool = False,
                       max_tokens: int | None = None) -> GatewayResult:
        assignment = self.assignments.get(agent_id)
        if assignment is None:
            raise ProviderError(f"Agent '{agent_id}' has no provider assignment")
        requirements = list(assignment.requirements or [REQUIRED_CAPABILITY])
        if json_mode and "json_mode" not in requirements:
            requirements.append("json_mode")
        system = agent_system_prompt(self.settings, skill_instructions, approved)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        targets = self._attempt_targets(assignment)
        attempts: list[dict] = []
        last_error: ProviderError | None = None
        previous_spec: ProviderSpec | None = None
        try:
            for index, target in enumerate(targets):
                spec = self.providers.get(target["provider"])
                if spec is None:
                    continue
                if index > 0 and previous_spec is not None and previous_spec.local and not spec.local and not assignment.allow_cloud_fallback:
                    attempts.append({"provider": spec.id, "status": "skipped",
                                     "detail": "local->cloud fallback not permitted by assignment policy"})
                    continue
                missing = spec.supports(requirements)
                if missing:
                    attempts.append({"provider": spec.id, "status": "rejected",
                                     "detail": f"missing capabilities: {', '.join(missing)}"})
                    last_error = ProviderError(f"Provider '{spec.id}' lacks required capabilities: {', '.join(missing)}")
                    continue
                try:
                    result = await self._call_with_interrupt(request_id, spec.id, target["model"], messages,
                                                             temperature=0.2, max_tokens=max_tokens,
                                                             json_mode=json_mode,
                                                             on_output=on_output if on_output is not None else None,
                                                             stream=True)
                    if on_output is not None and not result.streamed and result.text:
                        await emit(on_output, result.text)
                    attempts.append({"provider": spec.id, "model": result.model, "status": "ok",
                                     "usage": result.usage, "elapsed_ms": result.elapsed_ms})
                    return GatewayResult(text=result.text, provider=spec.id, model=result.model,
                                         attempts=attempts, usage=result.usage, elapsed_ms=result.elapsed_ms,
                                         fallback_used=index > 0)
                except GatewayCancelled:
                    attempts.append({"provider": spec.id, "model": target["model"], "status": "interrupted",
                                     "detail": "cancelled by human stop"})
                    raise
                except ProviderError as exc:
                    last_error = exc
                    attempts.append({"provider": spec.id, "model": target["model"], "status": "error",
                                     "retryable": exc.retryable, "detail": self.scrub(str(exc))[:200]})
                    previous_spec = spec
                    continue
            if last_error is not None:
                raise ProviderError(f"All providers failed for agent '{agent_id}'. Last: {last_error}",
                                    retryable=last_error.retryable)
            raise ProviderError(f"No usable provider for agent '{agent_id}'")
        finally:
            if request_id:
                self._running.pop(request_id, None)
                self._interrupted.discard(request_id)

    async def _call_with_interrupt(self, request_id: str | None, provider_id: str, model: str,
                                   messages: list[dict], **kwargs) -> GatewayResult:
        """Run one provider call so that a human stop cancels only *that* call.

        The attempt runs in its own task; a direct (non-queued) submit keeps its
        request task alive and reports a clear error instead of being aborted,
        while the queue keeps its own cooperative cancel semantics.
        """
        if not request_id:
            return await self.call(provider_id, model, messages, **kwargs)
        attempt = asyncio.create_task(self.call(provider_id, model, messages, **kwargs))
        self._running[request_id] = attempt
        try:
            return await attempt
        except asyncio.CancelledError:
            if request_id in self._interrupted:
                raise GatewayCancelled("Gateway call cancelled by human stop", retryable=False)
            raise
        finally:
            self._running.pop(request_id, None)

    def interrupt(self, request_id: str) -> bool:
        task = self._running.get(request_id)
        if task is None or task.done():
            return False
        self._interrupted.add(request_id)
        task.cancel()
        return True

    # ---------------------------------------------------------------- tests
    async def test_provider(self, provider_id: str, mode: str = "validate", model: str = "") -> dict:
        """Three honest levels: validate (config), connectivity (endpoint),
        inference (real call). Never reports success for a blocked test."""
        spec = self.providers.get(provider_id)
        if spec is None:
            return {"provider": provider_id, "mode": mode, "status": "error", "detail": "unknown provider"}
        if mode == "validate":
            problems = []
            if spec.kind not in PROVIDER_KINDS:
                problems.append(f"unsupported kind: {spec.kind}")
            if not spec.base_url:
                problems.append("missing base_url")
            if not spec.default_model and not model and not spec.models:
                problems.append("no model configured")
            if not spec.local and not spec.has_key:
                problems.append(f"no API key ({spec.key_source})")
            if problems:
                status, detail = "error", "; ".join(problems)
            else:
                status, detail = "ok", f"{spec.kind} config valid"
            self._set_health(provider_id, status, "" if status == "ok" else detail, "validate")
            return {"provider": provider_id, "mode": mode, "status": status, "detail": detail}
        if not spec.enabled:
            return {"provider": provider_id, "mode": mode, "status": "disabled", "detail": "provider is disabled"}
        if not spec.local and not spec.has_key:
            return {"provider": provider_id, "mode": mode, "status": "blocked_missing_credentials",
                    "detail": f"no API key configured ({spec.key_source})"}
        started = time.time()
        if mode == "connectivity":
            if spec.kind in ("openai_compatible", "ollama"):
                url = spec.base_url + "/models"
                headers = {"Authorization": f"Bearer {spec.api_key}"} if spec.api_key else {}
                try:
                    async with self._client(min(15.0, spec.timeout_seconds)) as client:
                        response = await client.get(url, headers=headers)
                        response.raise_for_status()
                        data = response.json()
                    models = [str(item.get("id")) for item in (data.get("data") or []) if isinstance(item, dict)]
                    detail = f"{len(models)} models listed"
                    self._set_health(provider_id, "ok", detail, "connectivity")
                    return {"provider": provider_id, "mode": mode, "status": "ok", "detail": detail,
                            "models": models[:50], "elapsed_ms": int((time.time() - started) * 1000)}
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    detail = f"HTTP {status} {self._detail(status, self._safe_text(exc.response))}"
                    self._set_health(provider_id, "error", detail, "connectivity")
                    return {"provider": provider_id, "mode": mode, "status": "error", "detail": detail,
                            "elapsed_ms": int((time.time() - started) * 1000)}
                except httpx.HTTPError as exc:
                    detail = f"{type(exc).__name__}"
                    self._set_health(provider_id, "error", detail, "connectivity")
                    return {"provider": provider_id, "mode": mode, "status": "error", "detail": detail,
                            "elapsed_ms": int((time.time() - started) * 1000)}
            if spec.kind == "gemini":
                url = f"{spec.base_url}/v1beta/models"
                try:
                    async with self._client(min(15.0, spec.timeout_seconds)) as client:
                        response = await client.get(url, headers={"x-goog-api-key": spec.api_key})
                        response.raise_for_status()
                        data = response.json()
                    models = [str(item.get("name", "")).split("/")[-1] for item in (data.get("models") or []) if isinstance(item, dict)]
                    detail = f"{len(models)} models listed"
                    self._set_health(provider_id, "ok", detail, "connectivity")
                    return {"provider": provider_id, "mode": mode, "status": "ok", "detail": detail,
                            "models": models[:50], "elapsed_ms": int((time.time() - started) * 1000)}
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    detail = f"HTTP {status} {self._detail(status, self._safe_text(exc.response))}"
                    self._set_health(provider_id, "error", detail, "connectivity")
                    return {"provider": provider_id, "mode": mode, "status": "error", "detail": detail,
                            "elapsed_ms": int((time.time() - started) * 1000)}
                except httpx.HTTPError as exc:
                    detail = f"{type(exc).__name__}"
                    self._set_health(provider_id, "error", detail, "connectivity")
                    return {"provider": provider_id, "mode": mode, "status": "error", "detail": detail,
                            "elapsed_ms": int((time.time() - started) * 1000)}
            return {"provider": provider_id, "mode": mode, "status": "unsupported",
                    "detail": f"{spec.kind} has no non-inference connectivity probe; use inference mode"}
        if mode == "inference":
            target_model = model or spec.default_model
            if not target_model:
                return {"provider": provider_id, "mode": mode, "status": "error", "detail": "no model configured"}
            try:
                result = await self.call(provider_id, target_model,
                                         [{"role": "user", "content": "Reply with the single word: ok"}],
                                         temperature=0, max_tokens=8)
                return {"provider": provider_id, "mode": mode, "status": "ok",
                        "detail": f"inference ok ({result.model})", "reply": result.text[:80],
                        "usage": result.usage, "elapsed_ms": result.elapsed_ms}
            except ProviderError as exc:
                return {"provider": provider_id, "mode": mode, "status": "error",
                        "detail": self.scrub(str(exc))[:300], "retryable": exc.retryable,
                        "elapsed_ms": int((time.time() - started) * 1000)}
        return {"provider": provider_id, "mode": mode, "status": "error",
                "detail": f"unknown test mode '{mode}' (use validate, connectivity, or inference)"}
