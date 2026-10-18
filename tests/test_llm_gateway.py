"""Provider registry, model gateway, adapters, assignments, and FastModel migration.

All HTTP is mocked with httpx.MockTransport: these tests prove request shapes,
capability validation, error classification, fallback policy, streaming,
cancellation, masking, and scrubbing. They never call a real provider.
"""
from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest

from cortexos.config import Settings
from cortexos.llm import Assignment, ModelGateway, is_local_url
from cortexos.providers import ProviderError

SECRET = "sk-test-secret-value-1234567890"


def run(coro):
    return asyncio.run(coro)


def make_settings(tmp_path, providers=None, fast_model_enabled=False):
    return Settings(
        root=tmp_path,
        llm_providers=providers or {},
        fast_model_enabled=fast_model_enabled,
        fast_model_base_url="http://127.0.0.1:11434/v1",
        fast_model_name="qwen-test",
        fast_model_api_key="local",
    )


def make_gateway(tmp_path, handler, providers=None, fast_model_enabled=False):
    settings = make_settings(tmp_path, providers=providers, fast_model_enabled=fast_model_enabled)
    return ModelGateway(settings, transport=httpx.MockTransport(handler),
                        assignment_path=tmp_path / "llm_assignments.json")


def openai_response(text="hello", usage=True):
    payload = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 7, "completion_tokens": 3}
    return httpx.Response(200, json=payload)


def test_is_local_url_detects_private_and_public():
    assert is_local_url("http://127.0.0.1:11434/v1")
    assert is_local_url("http://localhost:11434/v1")
    assert is_local_url("http://192.168.1.9:8000/v1")
    assert is_local_url("http://10.0.0.5/v1")
    assert not is_local_url("https://api.openai.com/v1")
    assert not is_local_url("https://openrouter.ai/api/v1")


def test_registry_masks_keys(tmp_path):
    gateway = make_gateway(tmp_path, lambda request: openai_response(), providers={
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test/v1",
                 "api_key": SECRET, "default_model": "acme-1"},
        "ollama": {"kind": "ollama", "default_model": "qwen"},
    })
    status = gateway.providers_status()
    dumped = json.dumps(status)
    assert SECRET not in dumped
    acme = next(item for item in status if item["id"] == "acme")
    assert acme["has_key"] is True and acme["key_source"] == "config"
    assert "api_key" not in acme
    ollama = next(item for item in status if item["id"] == "ollama")
    assert ollama["local"] is True and ollama["has_key"] is False
    assert any(item["id"] == "fast-model" for item in status)


def test_env_key_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_KEY", SECRET)
    gateway = make_gateway(tmp_path, lambda request: openai_response(), providers={
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test/v1",
                 "api_key_env": "ACME_KEY", "default_model": "acme-1"},
    })
    acme = next(item for item in gateway.providers_status() if item["id"] == "acme")
    assert acme["has_key"] is True
    assert acme["key_source"] == "env:ACME_KEY"
    assert SECRET not in json.dumps(acme)


def test_validate_assignment_issues(tmp_path):
    gateway = make_gateway(tmp_path, lambda request: openai_response(), providers={
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test/v1",
                 "api_key": SECRET, "default_model": "acme-1", "models": ["acme-1", "acme-2"]},
        "acme-nokey": {"kind": "openai_compatible", "base_url": "https://api.nokey.test/v1",
                       "default_model": "acme-1"},
        "local": {"kind": "ollama", "default_model": "qwen"},
    })
    assert gateway.validate_assignment("agent-a", {"provider": "acme", "model": "acme-1"}) == []
    assert any("unknown provider" in issue for issue in gateway.validate_assignment("a", {"provider": "nope"}))
    assert any("not in provider" in issue for issue in gateway.validate_assignment("a", {"provider": "acme", "model": "acme-9"}))
    assert any("no API key" in issue for issue in gateway.validate_assignment("a", {"provider": "acme-nokey"}))
    unknown = gateway.validate_assignment("a", {"provider": "nope", "requirements": ["vision"]})
    assert any("unknown provider" in issue for issue in unknown)


def test_assignment_roundtrip_and_delete(tmp_path):
    gateway = make_gateway(tmp_path, lambda request: openai_response(), providers={
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test/v1",
                 "api_key": SECRET, "default_model": "acme-1"},
    })
    saved = gateway.save_assignment("writer", {"provider": "acme", "model": "acme-1", "name": "Writer"})
    assert saved["agent_id"] == "writer" and saved["model"] == "acme-1"
    assert gateway.agent_ids == ["writer"]
    # persisted and reloadable
    reloaded = ModelGateway(gateway.settings, transport=None, assignment_path=tmp_path / "llm_assignments.json")
    assert reloaded.resolve("writer") is not None
    assert reloaded.assignments["writer"].provider == "acme"
    assert reloaded.delete_assignment("writer") is True
    assert reloaded.delete_assignment("writer") is False


def test_bot_persona_leads_system_prompt_and_roundtrips(tmp_path):
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen["system"] = (body.get("messages") or [{}])[0].get("content", "")
        return openai_response("I am Researcher.")

    gateway = make_gateway(tmp_path, handler, providers={
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test/v1",
                 "api_key": SECRET, "default_model": "acme-1"},
    })
    saved = gateway.save_assignment("researcher", {
        "provider": "acme", "model": "acme-1", "name": "Researcher",
        "persona": "You are Researcher, a meticulous analyst.",
    })
    assert saved["persona"] == "You are Researcher, a meticulous analyst."
    assert saved["name"] == "Researcher"

    reloaded = ModelGateway(gateway.settings, transport=None, assignment_path=tmp_path / "llm_assignments.json")
    assert reloaded.assignments["researcher"].persona.startswith("You are Researcher")

    async def scenario():
        outcome = await gateway.generate("researcher", "who are you?")
        assert outcome.text == "I am Researcher."
    run(scenario())
    assert seen["system"].startswith("## Bot identity\nYou are Researcher, a meticulous analyst.")
    # the standard CortexOS policy survives persona injection
    assert "Do not perform external actions" in seen["system"]

    too_long = gateway.validate_assignment("researcher", {
        "provider": "acme", "model": "acme-1", "persona": "x" * 4001})
    assert any("persona" in issue for issue in too_long)


def test_local_to_cloud_fallback_requires_policy(tmp_path):
    gateway = make_gateway(tmp_path, lambda request: openai_response(), providers={
        "local": {"kind": "ollama", "default_model": "qwen"},
        "cloud": {"kind": "openai_compatible", "base_url": "https://api.acme.test/v1",
                  "api_key": SECRET, "default_model": "acme-1"},
    })
    entry = {"provider": "local", "model": "qwen",
             "fallback": [{"provider": "cloud", "model": "acme-1"}]}
    issues = gateway.validate_assignment("a", entry)
    assert any("allow_cloud_fallback" in issue for issue in issues)
    ok = gateway.validate_assignment("a", {**entry, "allow_cloud_fallback": True})
    assert ok == []


def test_openai_adapter_request_shape(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return openai_response("world")

    gateway = make_gateway(tmp_path, handler, providers={
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test/v1",
                 "api_key": SECRET, "default_model": "acme-1"},
    })
    result = run(gateway.call("acme", "acme-1", [{"role": "user", "content": "hi"}],
                              temperature=0.5, max_tokens=64, json_mode=True))
    assert captured["url"] == "https://api.acme.test/v1/chat/completions"
    assert captured["auth"] == f"Bearer {SECRET}"
    assert captured["body"]["model"] == "acme-1"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["max_tokens"] == 64
    assert result.text == "world"
    assert result.usage == {"input_tokens": 7, "output_tokens": 3}
    assert gateway.health["acme"]["status"] == "ok"


def test_anthropic_adapter(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-api-key")
        captured["version"] = request.headers.get("anthropic-version")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "anthropic says hi"}],
                                         "usage": {"input_tokens": 5, "output_tokens": 4}})

    gateway = make_gateway(tmp_path, handler, providers={
        "claude-api": {"kind": "anthropic", "base_url": "https://api.anthropic.test",
                       "api_key": SECRET, "default_model": "claude-x"},
    })
    result = run(gateway.call("claude-api", "claude-x",
                              [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]))
    assert captured["url"] == "https://api.anthropic.test/v1/messages"
    assert captured["key"] == SECRET and captured["version"] == "2023-06-01"
    assert captured["body"]["system"] == "sys"
    assert captured["body"]["messages"][0]["content"] == "hi"
    assert result.text == "anthropic says hi"
    assert result.usage == {"input_tokens": 5, "output_tokens": 4}


def test_gemini_adapter(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-goog-api-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "gemini says hi"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}})

    gateway = make_gateway(tmp_path, handler, providers={
        "gem": {"kind": "gemini", "base_url": "https://generativelanguage.test",
                "api_key": SECRET, "default_model": "gemini-x"},
    })
    result = run(gateway.call("gem", "gemini-x",
                              [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                              json_mode=True))
    assert captured["url"] == "https://generativelanguage.test/v1beta/models/gemini-x:generateContent"
    assert captured["key"] == SECRET
    assert captured["body"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert captured["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert result.text == "gemini says hi"
    assert result.usage == {"input_tokens": 3, "output_tokens": 2}


def test_error_classification_retryable_vs_terminal(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.ok.test":
            return httpx.Response(500, text="server exploded")
        return httpx.Response(401, text=f"bad key {SECRET}")

    gateway = make_gateway(tmp_path, handler, providers={
        "ok": {"kind": "openai_compatible", "base_url": "https://api.ok.test", "api_key": SECRET, "default_model": "m"},
        "bad": {"kind": "openai_compatible", "base_url": "https://api.bad.test", "api_key": SECRET, "default_model": "m"},
    })

    async def scenario():
        with pytest.raises(ProviderError) as five:
            await gateway.call("ok", "m", [{"role": "user", "content": "boom"}], timeout_override=1)
        assert five.value.retryable is True
        assert "500" in str(five.value)

        with pytest.raises(ProviderError) as one:
            await gateway.call("bad", "m", [{"role": "user", "content": "hi"}])
        assert one.value.retryable is False
        # secrets never leak through errors
        assert SECRET not in str(one.value)
        assert "[redacted]" in str(one.value)
    run(scenario())
    assert gateway.health["ok"]["status"] == "error"
    assert gateway.health["bad"]["status"] == "error"


def test_capability_rejection_is_clear(tmp_path):
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={})

    gateway = make_gateway(tmp_path, handler, providers={
        "claude-api": {"kind": "anthropic", "base_url": "https://api.anthropic.test", "api_key": SECRET, "default_model": "claude-x"},
    })
    with pytest.raises(ProviderError) as exc:
        run(gateway.call("claude-api", "claude-x", [{"role": "user", "content": "hi"}], json_mode=True))
    assert "does not support" in str(exc.value) and "json_mode" in str(exc.value)
    assert called["n"] == 0  # rejected before any request


def test_test_provider_modes_are_honest(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})
        return openai_response("ok")

    gateway = make_gateway(tmp_path, handler, providers={
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test", "api_key": SECRET, "default_model": "m1"},
        "keyless": {"kind": "openai_compatible", "base_url": "https://api.keyless.test", "default_model": "m1"},
    })

    async def scenario():
        validate = await gateway.test_provider("acme", "validate")
        assert validate["status"] == "ok"
        connectivity = await gateway.test_provider("acme", "connectivity")
        assert connectivity["status"] == "ok" and connectivity["models"] == ["m1", "m2"]
        inference = await gateway.test_provider("acme", "inference")
        assert inference["status"] == "ok" and inference["reply"] == "ok"
        blocked = await gateway.test_provider("keyless", "inference")
        assert blocked["status"] == "blocked_missing_credentials"
        unknown = await gateway.test_provider("ghost", "validate")
        assert unknown["status"] == "error"
    run(scenario())


def test_inference_empty_reply_is_reported_honestly(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return openai_response("", usage=True)

    gateway = make_gateway(tmp_path, handler, providers={
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test", "api_key": SECRET, "default_model": "m1"},
    })
    result = run(gateway.test_provider("acme", "inference"))
    assert result["status"] == "ok"  # the model genuinely responded (tokens billed)
    assert "no text" in result["detail"]  # ...but the interface says so plainly
    assert result["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_fast_model_max_tokens_is_opt_in(tmp_path):
    from cortexos.providers import FastModel

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return openai_response("ok", usage=False)

    gateway = make_gateway(tmp_path, handler, fast_model_enabled=True)
    fast = FastModel(gateway.settings, gateway=gateway)
    assert run(fast.answer("hello")) == "ok"
    assert "max_tokens" not in captured["body"]  # default: provider decides (historical behavior)
    gateway.settings.fast_model_max_tokens = 128
    assert run(fast.answer("hello")) == "ok"
    assert captured["body"]["max_tokens"] == 128


def test_agent_generate_fallback_chain(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "primary" in str(request.url):
            return httpx.Response(503, text="down")
        return openai_response("from fallback")

    gateway = make_gateway(tmp_path, handler, providers={
        "primary": {"kind": "openai_compatible", "base_url": "https://primary.test", "api_key": SECRET, "default_model": "p1"},
        "backup": {"kind": "openai_compatible", "base_url": "https://backup.test", "api_key": SECRET, "default_model": "b1"},
    })
    gateway.save_assignment("agent-x", {"provider": "primary", "model": "p1",
                                        "fallback": [{"provider": "backup", "model": "b1"}]})

    async def scenario():
        result = await gateway.generate("agent-x", "do work", skill_instructions="skill text")
        assert result.text == "from fallback"
        assert result.provider == "backup" and result.fallback_used is True
        statuses = [attempt["status"] for attempt in result.attempts]
        assert statuses == ["error", "ok"]
    run(scenario())


def test_agent_generate_blocks_undeclared_local_to_cloud(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    gateway = make_gateway(tmp_path, handler, providers={
        "local": {"kind": "ollama", "base_url": "http://127.0.0.1:1/v1", "default_model": "qwen"},
        "cloud": {"kind": "openai_compatible", "base_url": "https://cloud.test", "api_key": SECRET, "default_model": "c1"},
    })
    gateway.assignments["agent-y"] = Assignment(
        agent_id="agent-y", provider="local", model="qwen",
        fallback=[{"provider": "cloud", "model": "c1"}], allow_cloud_fallback=False)

    async def scenario():
        with pytest.raises(ProviderError) as exc:
            await gateway.generate("agent-y", "do work")
        assert "local" in str(exc.value) or "cloud" in str(exc.value) or "failed" in str(exc.value)
    run(scenario())


def test_generate_requires_assignment(tmp_path):
    gateway = make_gateway(tmp_path, lambda request: openai_response())
    with pytest.raises(ProviderError):
        run(gateway.generate("nobody", "hi"))


def test_streaming_deltas(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body.get("stream") is True
        sse = (b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
               b'data: {"choices":[{"delta":{"content":"lo "}}]}\n\n'
               b'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
               b'data: [DONE]\n\n')
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    gateway = make_gateway(tmp_path, handler, providers={
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test", "api_key": SECRET, "default_model": "m1"},
    })

    async def scenario():
        deltas = []
        result = await gateway.call("acme", "m1", [{"role": "user", "content": "hi"}],
                                    stream=True, on_output=lambda text: deltas.append(text))
        assert result.streamed is True
        assert result.text == "Hello world"
        assert deltas == ["Hel", "lo ", "world"]
    run(scenario())


def test_interrupt_cancels_inflight_gateway_call(tmp_path):
    from cortexos.llm import GatewayCancelled

    started = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={})

    gateway = make_gateway(tmp_path, handler, providers={
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test", "api_key": SECRET, "default_model": "m1"},
        "backup": {"kind": "openai_compatible", "base_url": "https://backup.test", "api_key": SECRET, "default_model": "b1"},
    })
    gateway.assignments["agent-z"] = Assignment(
        agent_id="agent-z", provider="acme", model="m1",
        fallback=[{"provider": "backup", "model": "b1"}])

    async def scenario():
        task = asyncio.create_task(gateway.generate("agent-z", "long work", request_id="req-1"))
        while not started.is_set():
            await asyncio.sleep(0.01)
        assert gateway.interrupt("req-1") is True
        with pytest.raises(GatewayCancelled) as exc:
            await task
        assert exc.value.retryable is False
        # a human stop is not a retryable failure and must not touch fallbacks
        assert gateway.interrupt("req-1") is False
    run(scenario())


def test_fast_model_uses_gateway(tmp_path):
    from cortexos.providers import FastModel

    replies = {"content": "The answer is 42."}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("response_format"):
            replies["json"] = True
        else:
            replies["json"] = False
        if body.get("max_tokens") == 2:
            return openai_response("QUICK", usage=False)
        return openai_response("The answer is 42.", usage=False)

    gateway = make_gateway(tmp_path, handler, fast_model_enabled=True)
    fast = FastModel(gateway.settings, gateway=gateway)

    async def scenario():
        assert await fast.answer("what is life?") == "The answer is 42."
        assert await fast.classify("what is 2+2?") == "QUICK"
        # a failing fast model must fail conservative, not raise into submission
        failing = FastModel(gateway.settings, gateway=make_gateway(tmp_path, lambda request: httpx.Response(500), fast_model_enabled=True))
        assert await failing.classify("what is 2+2?") == "WORK"
        assert failing.last_error and "[redacted]" not in failing.last_error
    run(scenario())
    assert replies["json"] is False


def test_risk_classifier_retries_then_fails_closed(tmp_path):
    from cortexos.providers import FastModel

    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            # A free model can answer 200 with an empty body; the classifier
            # retries exactly once instead of gating everything as unknown.
            return openai_response("", usage=False)
        return openai_response(json.dumps({
            "risk_level": "external_action", "requires_approval": True, "reason": "sends an email",
        }), usage=False)

    gateway = make_gateway(tmp_path, flaky, fast_model_enabled=True)
    fast = FastModel(gateway.settings, gateway=gateway)

    async def scenario():
        decision = await fast.classify_action("send an email to test@example.com")
        assert decision == {"risk_level": "external_action", "requires_approval": True, "reason": "sends an email"}
        assert calls["n"] == 2  # one retry, never more

        dead = FastModel(gateway.settings, gateway=make_gateway(
            tmp_path, lambda request: openai_response("still not json"), fast_model_enabled=True))
        fallback = await dead.classify_action("anything")
        assert fallback["risk_level"] == "unknown" and fallback["requires_approval"] is True
    run(scenario())


def test_fast_model_respects_jev_decision_without_http(tmp_path):
    from cortexos.providers import FastModel

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return openai_response("X", usage=False)

    gateway = make_gateway(tmp_path, handler, fast_model_enabled=True)
    fast = FastModel(gateway.settings, gateway=gateway)

    async def fake_decide(kind, prompt, extra=None):
        return "tier2_fast" if kind == "tier" else None

    fast.jev.decide = fake_decide
    assert run(fast.classify("anything")) == "QUICK"
    assert calls["n"] == 0  # tier decision came from JEV; no model call was made
