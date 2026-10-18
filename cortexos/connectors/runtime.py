"""Connector runtime: the single execution path for every connector operation.

Order of enforcement for each call:
1. connectors enabled              5. scope check (missing_scope is precise)
2. argument validation             6. per-operation rate limit
3. approval gate (write/external)  7. idempotency journal (dedupe, no dup sends)
4. credential status               8. timeout + bounded retries, then journal truth

Nothing executes during status/report calls; OAuth start/exchange is explicit
and server-side (tokens are never returned by any API).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time

from .base import (ConnectorApprovalRequired, ConnectorBlocked, ConnectorError,
                   ConnectorRateLimited, ConnectorValidationError, field_schema,
                   scrub_secrets, validate_args)
from .google import (GOOGLE_TOKEN_URL, GOOGLE_TOKENINFO_URL, authorization_url,
                     build_google_connectors)
from .store import ConnectorStore, IdempotencyJournal, RateLimiter


class ConnectorRuntime:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.store = ConnectorStore(settings.connectors_state_dir, settings.connectors_client_dir)
        self.journal = IdempotencyJournal(settings.connectors_state_dir / "journal.json")
        self.limiter = RateLimiter()
        self._oauth_states: dict[str, dict] = {}
        self._health: dict[str, dict] = {}
        self._connectors = {connector.spec.id: connector for connector in build_google_connectors(self.store, transport)}

    # ---------------------------------------------------------------- lookup

    def connector(self, connector_id: str):
        connector = self._connectors.get(connector_id)
        if connector is None:
            raise ConnectorValidationError(f"Unknown connector: {connector_id}")
        return connector

    def ids(self) -> list[str]:
        return sorted(self._connectors)

    # ---------------------------------------------------------------- status

    def status(self) -> dict:
        return {"enabled": self.settings.connectors_enabled,
                "state_dir": str(self.settings.connectors_state_dir),
                "client_dir": str(self.settings.connectors_client_dir),
                "connectors": [self._connector_status(connector)
                               for connector in sorted(self._connectors.values(), key=lambda item: item.spec.id)]}

    def _connector_status(self, connector) -> dict:
        spec = connector.spec
        if not self.settings.connectors_enabled:
            credential = {"status": "disabled", "detail": "Connectors are disabled in configuration"}
        else:
            credential = connector.credential_status()
        return {"id": spec.id, "name": spec.name, "version": spec.version, "description": spec.description,
                "auth": spec.auth, "scopes": list(spec.scopes), "setup": spec.setup,
                "credential": credential,
                "health": self._health.get(spec.id, {"status": "unchecked"}),
                "operations": [{"name": operation.name, "description": operation.description,
                                "side_effect": operation.side_effect, "scopes": list(operation.scopes),
                                "approval": operation.approval, "idempotency": operation.idempotency,
                                "retry_safe": operation.retry_safe, "timeout_seconds": operation.timeout_seconds,
                                "rate_limit_per_minute": operation.rate_limit_per_minute,
                                "fields": {name: field_schema(field) for name, field in operation.fields.items()}}
                               for operation in spec.operations]}

    async def probe(self, connector_id: str) -> dict:
        """Active health check: refresh if needed, then validate the token with Google."""
        connector = self.connector(connector_id)
        credential = connector.credential_status()
        if credential["status"] != "ready":
            health = {"status": credential["status"], "checked_at": time.time(),
                      "detail": credential.get("detail")}
            self._health[connector_id] = health
            return health
        try:
            token = await connector.valid_token()
            response = await connector._http("GET", GOOGLE_TOKENINFO_URL,
                                             params={"access_token": token["access_token"]}, timeout=10)
            body = response.json() if response.content else {}
            if response.status_code == 200 and isinstance(body, dict) and not body.get("error"):
                health = {"status": "ok", "checked_at": time.time(),
                          "scopes_granted": str(body.get("scope", "")).split(),
                          "expires_in": int(body.get("expires_in") or 0)}
            else:
                health = {"status": "error", "checked_at": time.time(),
                          "detail": connector._error_detail(response)}
        except ConnectorError as exc:
            health = {"status": exc.status if exc.status != "error" else "error",
                      "checked_at": time.time(), "detail": scrub_secrets(str(exc))}
        self._health[connector_id] = health
        return health

    # ------------------------------------------------------------- execution

    @staticmethod
    def summary(connector, operation, args: dict) -> str:
        """Human summary for the approval prompt; never contains credentials."""
        if connector.spec.id == "gmail" and operation.name == "messages.send":
            return (f"Send Gmail message to {', '.join(args['to'])} — subject: {args['subject']!r} — "
                    f"body preview: {args['body'][:200]!r}")
        if connector.spec.id == "calendar" and operation.name == "events.create":
            return f"Create calendar event {args['summary']!r} from {args['start']} to {args['end']}"
        if connector.spec.id == "drive" and operation.name == "files.upload":
            return f"Upload {args['name']!r} to Google Drive"
        return f"{connector.name}: {operation.name}"

    @staticmethod
    def _journal_key(connector_id: str, operation, args: dict, client_key: str | None) -> str:
        if operation.idempotency == "client_key":
            if not client_key:
                raise ConnectorValidationError(f"{connector_id}.{operation.name} requires an idempotency_key")
            return f"{connector_id}:{operation.name}:k:{client_key}"
        digest = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return f"{connector_id}:{operation.name}:h:{digest}"

    async def execute(self, connector_id: str, operation_name: str, args: dict, *, approved: bool = False,
                      idempotency_key: str | None = None) -> dict:
        connector = self.connector(connector_id)
        operation = connector.operation(operation_name)
        if not self.settings.connectors_enabled:
            raise ConnectorBlocked("disabled", "Connectors are disabled in configuration")
        cleaned = validate_args(operation, args)
        if operation.approval and not approved:
            raise ConnectorApprovalRequired(
                f"{connector_id}.{operation.name} requires per-run human approval",
                risk_level="external_action", summary=self.summary(connector, operation, cleaned))
        credential = connector.credential_status()
        if credential["status"] != "ready":
            raise ConnectorBlocked(credential["status"], credential.get("detail", ""))
        token = await connector.valid_token()
        granted = set(token.get("scopes") or [])
        missing = [scope for scope in operation.scopes if scope not in granted]
        if missing:
            raise ConnectorBlocked(
                "missing_scope",
                "Granted scopes do not include: " + ", ".join(missing)
                + ". Re-authorize with the extended scope set to enable this operation.")
        wait = self.limiter.check(f"{connector_id}:{operation.name}", operation.rate_limit_per_minute)
        if wait is not None:
            raise ConnectorRateLimited(
                f"{connector_id}.{operation.name} exceeded its rate limit of "
                f"{operation.rate_limit_per_minute}/minute", retry_after=round(wait, 1))

        journal_key = None
        if operation.side_effect != "read":
            journal_key = self._journal_key(connector_id, operation, cleaned, idempotency_key)
            existing = self.journal.lookup(journal_key)
            if existing:
                state = existing.get("status")
                if state == "completed":
                    return {"status": "deduplicated", "deduplicated": True, "connector": connector_id,
                            "operation": operation.name, "version": connector.spec.version,
                            "side_effect": operation.side_effect, "result_ref": existing.get("result_ref"),
                            "note": "This exact request was already performed; nothing was sent again."}
                if state in {"pending", "unknown"}:
                    raise ConnectorBlocked(
                        "unknown_outcome",
                        "A previous attempt with this idempotency key has an unconfirmed outcome; "
                        "inspect it before retrying")
            self.journal.begin(journal_key, connector_id, operation.name)

        started = time.time()
        attempts = 0
        outcome = None
        error: ConnectorError | None = None
        while attempts < 1 + operation.max_retries:
            attempts += 1
            try:
                outcome = await asyncio.wait_for(connector.execute(operation, cleaned, token["access_token"]),
                                                 timeout=operation.timeout_seconds)
                error = None
                break
            except asyncio.TimeoutError:
                # Never retried automatically: the remote side may have applied the call.
                error = ConnectorError(f"{connector_id}.{operation.name} timed out after "
                                       f"{operation.timeout_seconds:g}s")
                break
            except ConnectorError as exc:
                error = exc
                if not (operation.retry_safe and exc.retryable):
                    break
                await asyncio.sleep(min(0.3 * attempts, 1.5))

        if error is not None:
            if journal_key:
                known_noop = error.http_status is not None and 400 <= error.http_status < 500
                self.journal.finish(journal_key, status="failed" if known_noop else "unknown")
            raise error

        duration_ms = int((time.time() - started) * 1000)
        if journal_key:
            self.journal.finish(journal_key, status="completed", result_ref=outcome.get("result_ref"))
        return {"status": "ok", "deduplicated": False, "connector": connector_id,
                "operation": operation.name, "version": connector.spec.version,
                "side_effect": operation.side_effect, "data": outcome.get("data"),
                "result_ref": outcome.get("result_ref"),
                "meta": {"duration_ms": duration_ms, "attempts": attempts}}

    # ----------------------------------------------------------------- oauth

    def oauth_start(self, connector_id: str) -> dict:
        connector = self.connector(connector_id)
        client = self.store.client_config(connector_id)
        if not client:
            raise ConnectorBlocked("not_configured", connector.spec.setup)
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        self._oauth_states[state] = {"connector": connector_id, "at": time.time(), "verifier": verifier}
        redirect_uri = self.store.redirect_uri(connector_id)
        scopes = list(connector.spec.scopes)
        return {"connector": connector_id, "state": state, "redirect_uri": redirect_uri, "scopes": scopes,
                "url": authorization_url(client, scopes, state, redirect_uri, challenge),
                "note": ("Open the URL and approve access, then exchange the returned code (the browser redirect "
                         "may show a connection error page; copy the code parameter from its address bar). "
                         "Tokens are stored server-side and never displayed again.")}

    async def oauth_exchange(self, connector_id: str, state: str, code: str) -> dict:
        connector = self.connector(connector_id)
        pending = self._oauth_states.pop(state, None)
        if not pending or pending.get("connector") != connector_id:
            raise ConnectorBlocked("invalid_state", "Authorization state is unknown or already used; start again")
        if time.time() - float(pending.get("at", 0)) > 900:
            raise ConnectorBlocked("invalid_state", "Authorization took too long; start again")
        client = self.store.client_config(connector_id)
        if not client:
            raise ConnectorBlocked("not_configured", connector.spec.setup)
        payload = {"code": code, "client_id": client["client_id"],
                   "redirect_uri": self.store.redirect_uri(connector_id), "grant_type": "authorization_code"}
        if pending.get("verifier"):
            payload["code_verifier"] = pending["verifier"]
        if client.get("client_secret"):
            payload["client_secret"] = client["client_secret"]
        response = await connector._http("POST", GOOGLE_TOKEN_URL, data=payload, timeout=20)
        body = response.json() if response.content else {}
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise ConnectorBlocked("authorization_failed",
                                   f"Token exchange failed: {connector._error_detail(response)}")
        previous = self.store.token(connector_id) or {}
        token = {"access_token": body["access_token"],
                 "refresh_token": body.get("refresh_token") or previous.get("refresh_token"),
                 "expires_at": time.time() + float(body.get("expires_in", 3600)) - 30,
                 "scopes": str(body.get("scope", "")).split(), "obtained_at": time.time()}
        self.store.save_token(connector_id, token)
        return {"connector": connector_id, "status": "ready", "scopes": token["scopes"],
                "note": "Credentials stored server-side (mode 0600). Tokens are never returned by this API."}

    def revoke(self, connector_id: str) -> dict:
        connector = self.connector(connector_id)
        removed = self.store.delete_token(connector_id)
        self._health.pop(connector_id, None)
        return {"connector": connector_id, "status": "revoked" if removed else "no_credentials",
                "note": ("Local credentials removed; existing approvals for this connector are unaffected. "
                         "Revoke the app in your Google account if you also want server-side access removed.")}
