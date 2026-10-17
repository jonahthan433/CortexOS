"""Stage 7 connector tests: declarations, honest credential/scope status,
argument validation, approval enforcement, idempotency, retries, rate limits,
token refresh, OAuth plumbing (never initiated here), and secret hygiene —
all over a mocked HTTP transport (no network, no real credentials)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from cortexos.config import Settings
from cortexos.connectors import (ConnectorApprovalRequired, ConnectorBlocked, ConnectorError,
                                 ConnectorRateLimited, ConnectorRuntime, ConnectorValidationError,
                                 IdempotencyJournal)
from cortexos.connectors.base import Connector, ConnectorSpec, Operation
from cortexos.connectors.google import GMAIL_READ, GMAIL_SEND
from cortexos.service import CortexService
from cortexos.vault import Vault

CLIENT_SECRET = "secret-value"


def make_settings(tmp_path: Path) -> Settings:
    (tmp_path / "vault").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    return Settings(root=tmp_path, vault=tmp_path / "vault", skills=tmp_path / "skills",
                    queue_db=tmp_path / "state" / "queue.db",
                    connectors_state_dir=tmp_path / "state" / "connectors",
                    connectors_client_dir=tmp_path / "config" / "connectors")


def write_client(settings: Settings, connector_id: str = "gmail") -> Path:
    path = settings.connectors_client_dir / f"{connector_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"client_id": "client-123.apps.googleusercontent.com",
                                "client_secret": CLIENT_SECRET}), encoding="utf-8")
    path.chmod(0o600)
    return path


def write_token(runtime: ConnectorRuntime, connector_id: str = "gmail", *, scopes: list[str],
                expires_in: float = 3600, access: str = "ya29.test-token",
                refresh: str = "1//refresh-token") -> None:
    runtime.store.save_token(connector_id, {
        "access_token": access, "refresh_token": refresh,
        "expires_at": time.time() + expires_in, "scopes": list(scopes), "obtained_at": time.time()})


class MockAPI:
    def __init__(self, responses: list | None = None):
        self.calls: list[dict] = []
        self.responses: list = list(responses or [])

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"method": request.method, "url": str(request.url), "path": request.url.path,
                           "headers": dict(request.headers),
                           "body": request.content.decode() if request.content else "",
                           "params": dict(request.url.params)})
        if self.responses:
            response = self.responses.pop(0)
            if callable(response):
                return response(request)
            return response
        return httpx.Response(200, json={})


def receipts(vault_root: Path) -> list[dict]:
    return [Vault._receipt_data(path) for path in (vault_root / "receipts").glob("**/*.md")]


# ------------------------------------------------------------------ declarations


def test_status_declares_capabilities_and_honest_credentials(tmp_path):
    settings = make_settings(tmp_path)
    runtime = ConnectorRuntime(settings)
    status = runtime.status()
    assert status["enabled"] is True
    by_id = {item["id"]: item for item in status["connectors"]}
    assert set(by_id) == {"gmail", "calendar", "drive", "youtube"}
    gmail = by_id["gmail"]
    assert gmail["version"] == "1.0.0" and gmail["auth"] == "oauth2"
    assert gmail["credential"]["status"] == "not_configured"
    operations = {operation["name"]: operation for operation in gmail["operations"]}
    assert operations["messages.search"]["side_effect"] == "read"
    assert operations["messages.search"]["approval"] is False
    assert operations["messages.send"]["side_effect"] == "external"
    assert operations["messages.send"]["approval"] is True
    assert operations["messages.send"]["idempotency"] == "content_hash"
    drive = by_id["drive"]
    assert "https://www.googleapis.com/auth/drive.readonly" not in drive["scopes"]  # least privilege default
    assert "https://www.googleapis.com/auth/drive.file" in drive["scopes"]
    files_list = next(op for op in drive["operations"] if op["name"] == "files.list")
    assert files_list["scopes"] == ["https://www.googleapis.com/auth/drive.readonly"]


def test_credential_status_transitions_and_never_leaks_tokens(tmp_path):
    settings = make_settings(tmp_path)
    runtime = ConnectorRuntime(settings)
    write_client(settings)
    credential = next(item for item in runtime.status()["connectors"] if item["id"] == "gmail")["credential"]
    assert credential["status"] == "blocked_missing_credentials"
    write_token(runtime, scopes=list(GMAIL_READ))
    credential = next(item for item in runtime.status()["connectors"] if item["id"] == "gmail")["credential"]
    assert credential["status"] == "ready" and credential["scopes"] == list(GMAIL_READ)
    blob = json.dumps(runtime.status())
    assert "ya29.test-token" not in blob and "1//refresh-token" not in blob and CLIENT_SECRET not in blob


# ------------------------------------------------------------------ execution


def test_read_operation_uses_bearer_token_and_bounded_params(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    api = MockAPI([httpx.Response(200, json={"messages": [{"id": "m1", "threadId": "t1"}],
                                             "resultSizeEstimate": 1})])
    runtime = ConnectorRuntime(settings, transport=api.transport())
    write_token(runtime, scopes=list(GMAIL_READ))
    result = asyncio.run(runtime.execute("gmail", "messages.search",
                                         {"query": "from:bob newer_than:7d", "max_results": 5}))
    assert result["status"] == "ok" and result["data"]["messages"][0]["id"] == "m1"
    assert result["meta"]["attempts"] == 1
    call = api.calls[0]
    assert call["method"] == "GET" and call["path"] == "/gmail/v1/users/me/messages"
    assert call["headers"]["authorization"] == "Bearer ya29.test-token"
    assert call["params"]["q"] == "from:bob newer_than:7d" and call["params"]["maxResults"] == "5"


def test_missing_scope_blocks_without_any_http_call(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    api = MockAPI()
    runtime = ConnectorRuntime(settings, transport=api.transport())
    write_token(runtime, scopes=list(GMAIL_READ))  # gmail.send not granted
    with pytest.raises(ConnectorBlocked) as excinfo:
        asyncio.run(runtime.execute("gmail", "messages.send", {"to": ["bob@example.com"],
                                                               "subject": "Hi", "body": "Hello"},
                                    approved=True))
    assert excinfo.value.status == "missing_scope"
    assert api.calls == []


def test_write_requires_approval_and_executes_after_approval_with_receipt(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    service = CortexService(settings)
    api = MockAPI([httpx.Response(200, json={"id": "msg-9", "threadId": "th-9"})])
    service.connectors = ConnectorRuntime(settings, transport=api.transport())
    write_token(service.connectors, scopes=list(GMAIL_READ) + list(GMAIL_SEND))
    pending = asyncio.run(service.request_connector_operation(
        "gmail", "messages.send", {"to": ["bob@example.com"], "subject": "Invoice", "body": "Please review."}))
    assert pending["status"] == "awaiting_approval" and api.calls == []
    approval_id = pending["approval_id"]
    assert approval_id in service.pending and service.pending[approval_id]["kind"] == "connector"
    approved = asyncio.run(service.approve(approval_id))
    assert approved["status"] == "ok" and approved["result_ref"] == "msg-9"
    assert len(api.calls) == 1 and api.calls[0]["method"] == "POST"
    entries = receipts(settings.vault)
    operations = [item for item in entries if item.get("event") == "connector_op"]
    assert operations and operations[-1]["status"] == "completed"
    assert operations[-1]["approval"] == "approved" and operations[-1]["result_ref"] == "msg-9"
    blob = json.dumps(entries)
    assert "ya29" not in blob and "1//refresh" not in blob and CLIENT_SECRET not in blob


def test_rejecting_a_connector_approval_never_executes(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    service = CortexService(settings)
    api = MockAPI([httpx.Response(200, json={"id": "msg-9"})])
    service.connectors = ConnectorRuntime(settings, transport=api.transport())
    write_token(service.connectors, scopes=list(GMAIL_SEND))
    pending = asyncio.run(service.request_connector_operation(
        "gmail", "messages.send", {"to": ["bob@example.com"], "subject": "Hi", "body": "Hello"}))
    rejected = service.reject(pending["approval_id"])
    assert rejected["status"] == "rejected" and api.calls == []
    assert pending["approval_id"] not in service.pending


def test_validation_rejects_bad_arguments_and_unknown_targets(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    runtime = ConnectorRuntime(settings, transport=MockAPI().transport())
    write_token(runtime, scopes=list(GMAIL_READ) + list(GMAIL_SEND))
    with pytest.raises(ConnectorValidationError, match="missing required argument"):
        asyncio.run(runtime.execute("gmail", "messages.search", {}))
    with pytest.raises(ConnectorValidationError, match="unknown argument"):
        asyncio.run(runtime.execute("gmail", "messages.search", {"query": "x", "bogus": 1}))
    with pytest.raises(ConnectorValidationError, match="Unknown operation"):
        asyncio.run(runtime.execute("gmail", "nope", {}))
    with pytest.raises(ConnectorValidationError, match="Unknown connector"):
        asyncio.run(runtime.execute("nope", "x", {}))


def test_idempotency_dedupes_identical_external_requests(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    api = MockAPI([httpx.Response(200, json={"id": "msg-1"})])
    runtime = ConnectorRuntime(settings, transport=api.transport())
    write_token(runtime, scopes=list(GMAIL_SEND))
    args = {"to": ["bob@example.com"], "subject": "Invoice", "body": "Please review."}
    first = asyncio.run(runtime.execute("gmail", "messages.send", args, approved=True))
    assert first["status"] == "ok" and len(api.calls) == 1
    second = asyncio.run(runtime.execute("gmail", "messages.send", args, approved=True))
    assert second["status"] == "deduplicated" and second["deduplicated"] is True
    assert second["result_ref"] == "msg-1" and len(api.calls) == 1
    # Different content is a different key and does execute.
    third = asyncio.run(runtime.execute("gmail", "messages.send",
                                        {**args, "body": "A different message."}, approved=True))
    assert third["status"] == "ok" and len(api.calls) == 2
    journal = json.loads((settings.connectors_state_dir / "journal.json").read_text(encoding="utf-8"))
    assert (settings.connectors_state_dir / "journal.json").stat().st_mode & 0o777 == 0o600
    assert "ya29" not in json.dumps(journal) and "1//refresh" not in json.dumps(journal)


def test_unknown_outcome_blocks_silent_retry(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    attempts = []

    def timeout_handler(request):
        attempts.append(1)
        raise httpx.ConnectTimeout("simulated network failure")

    api = MockAPI([timeout_handler])
    runtime = ConnectorRuntime(settings, transport=api.transport())
    write_token(runtime, scopes=list(GMAIL_SEND))
    args = {"to": ["bob@example.com"], "subject": "Hi", "body": "Hello"}
    with pytest.raises(ConnectorError):
        asyncio.run(runtime.execute("gmail", "messages.send", args, approved=True))
    assert len(attempts) == 1  # external sends are never auto-retried
    with pytest.raises(ConnectorBlocked) as excinfo:
        asyncio.run(runtime.execute("gmail", "messages.send", args, approved=True))
    assert excinfo.value.status == "unknown_outcome"
    assert len(attempts) == 1


def test_reads_retry_on_5xx_but_writes_do_not(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    api = MockAPI([httpx.Response(500, json={"error": {"message": "backend unavailable"}}),
                   httpx.Response(200, json={"messages": []})])
    runtime = ConnectorRuntime(settings, transport=api.transport())
    write_token(runtime, scopes=list(GMAIL_READ))
    result = asyncio.run(runtime.execute("gmail", "messages.search", {"query": "x"}))
    assert result["status"] == "ok" and len(api.calls) == 2

    write_client(settings)
    api2 = MockAPI([httpx.Response(500, json={"error": {"message": "backend unavailable"}})])
    runtime2 = ConnectorRuntime(settings, transport=api2.transport())
    write_token(runtime2, scopes=list(GMAIL_SEND))
    with pytest.raises(ConnectorError):
        asyncio.run(runtime2.execute("gmail", "messages.send",
                                     {"to": ["a@b.c"], "subject": "x", "body": "y"}, approved=True))
    assert len(api2.calls) == 1  # no retry for external side effects


def test_rate_limit_is_enforced_per_operation(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings, "test")
    api = MockAPI()
    runtime = ConnectorRuntime(settings, transport=api.transport())
    write_token(runtime, "test", scopes=["scope.read"])
    spec = ConnectorSpec(id="test", name="Test", version="1.0.0", description="", auth="oauth2",
                         scopes=("scope.read",), setup="none",
                         operations=(Operation("ping", "read ping", "read", ("scope.read",), approval=False,
                                               idempotency="none", rate_limit_per_minute=2),))

    class TestConnector(Connector):
        async def valid_token(self):
            return self.store.token("test")

        async def execute(self, operation, args, access_token):
            return {"data": {"ok": True}, "result_ref": None}

    runtime._connectors["test"] = TestConnector(spec, runtime.store, None)
    for _ in range(2):
        assert asyncio.run(runtime.execute("test", "ping", {}))["status"] == "ok"
    with pytest.raises(ConnectorRateLimited) as excinfo:
        asyncio.run(runtime.execute("test", "ping", {}))
    assert excinfo.value.retry_after > 0
    assert api.calls == []  # the test connector makes no HTTP calls


def test_disabled_connectors_block_execution(tmp_path):
    settings = make_settings(tmp_path)
    settings.connectors_enabled = False
    write_client(settings)
    runtime = ConnectorRuntime(settings, transport=MockAPI().transport())
    write_token(runtime, scopes=list(GMAIL_READ))
    credential = next(item for item in runtime.status()["connectors"] if item["id"] == "gmail")["credential"]
    assert credential["status"] == "disabled"
    with pytest.raises(ConnectorBlocked) as excinfo:
        asyncio.run(runtime.execute("gmail", "messages.search", {"query": "x"}))
    assert excinfo.value.status == "disabled"


# --------------------------------------------------------------------- tokens


def test_expired_token_is_refreshed_and_persisted(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    api = MockAPI([httpx.Response(200, json={"access_token": "ya29.fresh", "expires_in": 3600,
                                             "scope": GMAIL_READ[0]}),
                   httpx.Response(200, json={"messages": []})])
    runtime = ConnectorRuntime(settings, transport=api.transport())
    write_token(runtime, scopes=list(GMAIL_READ), expires_in=-1000)
    result = asyncio.run(runtime.execute("gmail", "messages.search", {"query": "x"}))
    assert result["status"] == "ok"
    assert api.calls[0]["path"] == "/token" and api.calls[0]["method"] == "POST"
    assert api.calls[1]["headers"]["authorization"] == "Bearer ya29.fresh"
    stored = runtime.store.token("gmail")
    assert stored["access_token"] == "ya29.fresh"
    assert runtime.store.token_path("gmail").stat().st_mode & 0o777 == 0o600


def test_refresh_failure_reports_expired(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    api = MockAPI([httpx.Response(400, json={"error": "invalid_grant",
                                             "error_description": "Token has been expired or revoked."})])
    runtime = ConnectorRuntime(settings, transport=api.transport())
    write_token(runtime, scopes=list(GMAIL_READ), expires_in=-1000)
    with pytest.raises(ConnectorBlocked) as excinfo:
        asyncio.run(runtime.execute("gmail", "messages.search", {"query": "x"}))
    assert excinfo.value.status == "expired"
    assert len(api.calls) == 1


# ---------------------------------------------------------------------- oauth


def test_oauth_start_requires_client_config_and_returns_scoped_url(tmp_path):
    settings = make_settings(tmp_path)
    runtime = ConnectorRuntime(settings, transport=MockAPI().transport())
    with pytest.raises(ConnectorBlocked) as excinfo:
        runtime.oauth_start("gmail")
    assert excinfo.value.status == "not_configured"
    assert "config/connectors" in excinfo.value.detail
    write_client(settings)
    start = runtime.oauth_start("gmail")
    assert start["url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client-123.apps.googleusercontent.com" in start["url"]
    assert CLIENT_SECRET not in start["url"] and "access_type=offline" in start["url"]
    assert "gmail.readonly" in start["url"] and "gmail.send" in start["url"]
    assert start["state"] and start["redirect_uri"].startswith("http://localhost")


def test_oauth_exchange_stores_tokens_server_side_and_is_single_use(tmp_path):
    settings = make_settings(tmp_path)
    write_client(settings)
    start_scopes = ("https://www.googleapis.com/auth/gmail.readonly "
                    "https://www.googleapis.com/auth/gmail.send")
    api = MockAPI([httpx.Response(200, json={"access_token": "ya29.oauth", "refresh_token": "1//new-refresh",
                                             "expires_in": 3600, "scope": start_scopes})])
    runtime = ConnectorRuntime(settings, transport=api.transport())
    start = runtime.oauth_start("gmail")
    exchanged = asyncio.run(runtime.oauth_exchange("gmail", start["state"], "auth-code-1"))
    assert exchanged["status"] == "ready"
    assert "ya29" not in json.dumps(exchanged) and "1//" not in json.dumps(exchanged)
    stored = runtime.store.token("gmail")
    assert stored["access_token"] == "ya29.oauth" and stored["refresh_token"] == "1//new-refresh"
    assert any(scope.endswith("/gmail.send") for scope in stored["scopes"])
    credential = next(item for item in runtime.status()["connectors"] if item["id"] == "gmail")["credential"]
    assert credential["status"] == "ready"
    with pytest.raises(ConnectorBlocked) as excinfo:
        asyncio.run(runtime.oauth_exchange("gmail", start["state"], "auth-code-1"))
    assert excinfo.value.status == "invalid_state"


# -------------------------------------------------------------------- journal


def test_journal_is_bounded_and_private(tmp_path):
    journal = IdempotencyJournal(tmp_path / "journal.json", limit=3)
    for index in range(5):
        journal.begin(f"k{index}", "gmail", "messages.send")
        journal.finish(f"k{index}", status="completed", result_ref=f"ref-{index}")
    data = json.loads((tmp_path / "journal.json").read_text(encoding="utf-8"))
    assert len(data["entries"]) == 3
    assert (tmp_path / "journal.json").stat().st_mode & 0o777 == 0o600


def test_idempotency_keys_are_stable_and_client_keys_are_required():
    client_key_operation = Operation("op", "", "external", ("scope",), approval=True, idempotency="client_key")
    with pytest.raises(ConnectorValidationError, match="requires an idempotency_key"):
        ConnectorRuntime._journal_key("test", client_key_operation, {}, None)
    assert ConnectorRuntime._journal_key("test", client_key_operation, {}, "abc") == "test:op:k:abc"
    content_operation = Operation("op", "", "external", ("scope",), approval=True, idempotency="content_hash")
    first = ConnectorRuntime._journal_key("test", content_operation, {"b": 1, "a": 2}, None)
    second = ConnectorRuntime._journal_key("test", content_operation, {"a": 2, "b": 1}, None)
    assert first == second and first.startswith("test:op:h:")
