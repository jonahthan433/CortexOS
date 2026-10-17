"""Shared connector interface: declarations, validation, and error taxonomy.

Connectors declare exactly what they can do (operation, side-effect class,
scopes, approval requirement, idempotency behavior, timeouts, rate limits).
Nothing here talks to the network; the runtime in ``runtime.py`` executes
operations through these declarations under the approval gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SIDE_EFFECTS = ("read", "write", "external")
IDEMPOTENCY_MODES = ("none", "content_hash", "client_key")

_TOKEN_PATTERNS = (
    re.compile(r"ya29\.[A-Za-z0-9_\-\.]+"),
    re.compile(r"1//[A-Za-z0-9_\-\.]{10,}"),
    re.compile(r"(?i)access_token=[^\s&\"']+"),
    re.compile(r"(?i)(access_token|refresh_token|client_secret|id_token)\"?\s*[:=]\s*\"?[^\s\"&,]+"),
)


def scrub_secrets(text: object, limit: int = 300) -> str:
    """Redact token-shaped values and bound length; used for every surfaced error."""
    value = str(text)
    for pattern in _TOKEN_PATTERNS:
        value = pattern.sub("[redacted]", value)
    return value[:limit]


class ConnectorError(Exception):
    """Base connector failure. Messages must already be safe to surface."""
    status = "error"
    retryable = False
    http_status: int | None = None


class ConnectorBlocked(ConnectorError):
    """The operation cannot run in the current credential/scope/config state."""
    def __init__(self, status: str, detail: str = ""):
        super().__init__(detail or status)
        self.status = status
        self.detail = detail or status


class ConnectorValidationError(ConnectorError):
    status = "invalid_arguments"


class ConnectorRateLimited(ConnectorError):
    status = "rate_limited"
    def __init__(self, detail: str, retry_after: float):
        super().__init__(detail)
        self.retry_after = retry_after


class ConnectorApprovalRequired(ConnectorError):
    status = "approval_required"
    def __init__(self, detail: str, *, risk_level: str = "external_action", summary: str = ""):
        super().__init__(detail)
        self.risk_level = risk_level
        self.summary = summary


@dataclass(frozen=True)
class Field:
    type: str                    # "str" | "int" | "bool" | "str_list"
    required: bool = False
    max_length: int | None = None
    max_items: int | None = None
    choices: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    side_effect: str              # read | write | external
    scopes: tuple[str, ...]
    approval: bool
    idempotency: str              # none | content_hash | client_key
    fields: dict[str, Field] = field(default_factory=dict)
    rate_limit_per_minute: int = 60
    timeout_seconds: float = 20.0
    retry_safe: bool = False      # only safe (read/idempotent) calls are auto-retried
    max_retries: int = 0


@dataclass(frozen=True)
class ConnectorSpec:
    id: str
    name: str
    version: str
    description: str
    auth: str                     # "oauth2"
    scopes: tuple[str, ...]       # scopes requested by default during authorization
    operations: tuple[Operation, ...]
    setup: str                    # human-readable setup instructions


def validate_args(operation: Operation, args: dict) -> dict:
    """Validate and normalize operation arguments; rejects unknown keys."""
    if not isinstance(args, dict):
        raise ConnectorValidationError("args must be an object")
    unknown = sorted(set(args) - set(operation.fields))
    if unknown:
        raise ConnectorValidationError(f"unknown argument(s) for {operation.name}: {unknown}")
    cleaned: dict = {}
    for name, spec in operation.fields.items():
        value = args.get(name)
        if value is None:
            if spec.required:
                raise ConnectorValidationError(f"missing required argument: {name}")
            continue
        if spec.type == "str":
            if not isinstance(value, str) or not value.strip():
                raise ConnectorValidationError(f"{name} must be a non-empty string")
            value = value.strip()
            if spec.max_length and len(value) > spec.max_length:
                raise ConnectorValidationError(f"{name} exceeds {spec.max_length} characters")
        elif spec.type == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConnectorValidationError(f"{name} must be an integer")
        elif spec.type == "bool":
            if not isinstance(value, bool):
                raise ConnectorValidationError(f"{name} must be a boolean")
        elif spec.type == "str_list":
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
                raise ConnectorValidationError(f"{name} must be a non-empty list of strings")
            value = [item.strip() for item in value]
            if spec.max_items and len(value) > spec.max_items:
                raise ConnectorValidationError(f"{name} allows at most {spec.max_items} items")
        else:
            raise ConnectorValidationError(f"{name} has an unsupported field type")
        if spec.choices and value not in spec.choices:
            raise ConnectorValidationError(f"{name} must be one of: {', '.join(spec.choices)}")
        cleaned[name] = value
    return cleaned


def field_schema(spec: Field) -> dict:
    schema = {"type": spec.type, "required": spec.required}
    if spec.max_length is not None:
        schema["max_length"] = spec.max_length
    if spec.max_items is not None:
        schema["max_items"] = spec.max_items
    if spec.choices:
        schema["choices"] = list(spec.choices)
    return schema


class Connector:
    """Base connector: credential reporting plus a per-service call implementation."""

    spec: ConnectorSpec

    def __init__(self, spec: ConnectorSpec, store, transport=None):
        self.spec = spec
        self.store = store
        self.transport = transport

    def operation(self, name: str) -> Operation:
        for operation in self.spec.operations:
            if operation.name == name:
                return operation
        raise ConnectorValidationError(f"Unknown operation for {self.spec.id}: {name}")

    def credential_status(self) -> dict:
        if self.store.client_config(self.spec.id) is None:
            return {"status": "not_configured",
                    "detail": f"No OAuth client file at {self.store.client_path(self.spec.id)}; see setup notes"}
        mode = self.store.client_mode(self.spec.id)
        loose = mode is not None and (mode & 0o077) != 0
        token = self.store.token(self.spec.id)
        if not token:
            return {"status": "blocked_missing_credentials",
                    "detail": "OAuth authorization has not been completed for this connector"}
        scopes = token.get("scopes") or []
        status = {"status": "ready", "detail": "authorized", "scopes": scopes,
                  "expires_at": token.get("expires_at"), "obtained_at": token.get("obtained_at")}
        if loose:
            status["warning"] = f"client file permissions are {oct(mode)}; use chmod 600"
        return status

    async def valid_token(self) -> dict:
        raise NotImplementedError

    async def execute(self, operation: Operation, args: dict, access_token: str) -> dict:
        raise NotImplementedError
