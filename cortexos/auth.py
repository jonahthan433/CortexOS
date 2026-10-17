"""Dashboard sign-in: password hash + signed cookie sessions.

The bearer token stays the machine credential for scripts, the CLI, and the
Obsidian plugin; the HUD uses a login page backed by this store instead of a
pasted token. The auth file (config/auth.json) holds a scrypt password hash
and a session-signing secret, 0600, never printed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
from pathlib import Path

SESSION_COOKIE = "cortexos_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 200

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


class AuthError(ValueError):
    """Invalid password or malformed auth state."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class AuthStore:
    """Password hash + session signing secret in one 0600 JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self._cache: dict | None = None
        self._stamp: tuple[int, int] | None = None

    # ------------------------------------------------------------- storage
    def _load(self) -> dict:
        try:
            stat = self.path.stat()
        except OSError:
            self._cache, self._stamp = {}, None
            return {}
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._stamp == stamp:
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._cache, self._stamp = data, stamp
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=".auth-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._cache, self._stamp = None, None

    # ------------------------------------------------------------- password
    def status(self) -> dict:
        data = self._load()
        return {
            "has_password": bool(data.get("password_hash") and data.get("password_salt")),
            "updated_at": data.get("updated_at"),
        }

    def has_password(self) -> bool:
        return self.status()["has_password"]

    @staticmethod
    def _check(password: str) -> str:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters long")
        return password

    def set_password(self, password: str) -> None:
        value = self._check(password)
        salt = os.urandom(16)
        digest = hashlib.scrypt(value.encode("utf-8"), salt=salt, **_SCRYPT)
        data = self._load()
        data.update({
            "password_salt": _b64(salt),
            "password_hash": _b64(digest),
            "updated_at": time.time(),
            # A new signing secret signs out every existing session.
            "session_secret": _b64(os.urandom(32)),
        })
        self._save(data)

    def verify_password(self, password: str) -> bool:
        data = self._load()
        try:
            salt = _unb64(str(data["password_salt"]))
            expected = _unb64(str(data["password_hash"]))
        except (KeyError, ValueError):
            return False
        if not isinstance(password, str) or not password:
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
        return hmac.compare_digest(digest, expected)

    def clear(self) -> None:
        data = self._load()
        for key in ("password_salt", "password_hash", "session_secret"):
            data.pop(key, None)
        data["updated_at"] = time.time()
        self._save(data)

    # ------------------------------------------------------------- sessions
    def _secret(self) -> bytes | None:
        value = self._load().get("session_secret")
        if not value:
            return None
        try:
            return _unb64(str(value))
        except ValueError:
            return None

    def issue_session(self, ttl: int = SESSION_TTL_SECONDS) -> str | None:
        secret = self._secret()
        if secret is None:
            return None
        payload = json.dumps({"exp": int(time.time()) + int(ttl)}, separators=(",", ":")).encode("utf-8")
        body = _b64(payload)
        mac = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{_b64(mac)}"

    def verify_session(self, value: str | None) -> bool:
        secret = self._secret()
        if not secret or not value or "." not in value:
            return False
        body, _, mac_text = value.partition(".")
        try:
            mac = _unb64(mac_text)
            payload = json.loads(_unb64(body).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            return False
        try:
            return int(payload.get("exp", 0)) > int(time.time())
        except (TypeError, ValueError):
            return False


class LoginThrottle:
    """Bounded in-memory backoff for failed sign-ins, keyed by client address."""

    def __init__(self, max_failures: int = 5, window_seconds: int = 300):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = time.time() - self.window_seconds
        failures = [stamp for stamp in self._failures.get(key, []) if stamp > cutoff]
        if failures:
            self._failures[key] = failures
        else:
            self._failures.pop(key, None)
        return failures

    def retry_after(self, key: str) -> int:
        failures = self._recent(key)
        if len(failures) < self.max_failures:
            return 0
        return max(1, int(failures[0] + self.window_seconds - time.time()) + 1)

    def record_failure(self, key: str) -> None:
        self._recent(key)
        self._failures.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
