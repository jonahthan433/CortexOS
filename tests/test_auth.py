"""Dashboard sign-in tests: password store, signed sessions, and throttling."""
from __future__ import annotations

from pathlib import Path

import pytest

from cortexos.auth import (AuthError, AuthStore, LoginThrottle, MIN_PASSWORD_LENGTH,
                           SESSION_COOKIE, SESSION_TTL_SECONDS)


def make_store(tmp_path: Path) -> AuthStore:
    return AuthStore(tmp_path / "config" / "auth.json")


def test_set_verify_and_clear_password(tmp_path):
    store = make_store(tmp_path)
    assert store.has_password() is False
    with pytest.raises(AuthError):
        store.set_password("x" * (MIN_PASSWORD_LENGTH - 1))
    store.set_password("correct horse battery")
    assert store.has_password() is True
    assert store.verify_password("correct horse battery") is True
    assert store.verify_password("wrong horse battery") is False
    assert store.verify_password("") is False
    status = store.status()
    assert status["has_password"] is True and status["updated_at"] > 0
    auth_file = tmp_path / "config" / "auth.json"
    assert auth_file.stat().st_mode & 0o777 == 0o600
    assert auth_file.parent.stat().st_mode & 0o777 == 0o700
    store.clear()
    assert store.has_password() is False
    assert store.verify_password("correct horse battery") is False


def test_password_file_never_stores_plaintext(tmp_path):
    store = make_store(tmp_path)
    store.set_password("unique-password-for-test")
    raw = (tmp_path / "config" / "auth.json").read_text(encoding="utf-8")
    assert "unique-password-for-test" not in raw


def test_sessions_roundtrip_tamper_and_expiry(tmp_path):
    store = make_store(tmp_path)
    store.set_password("correct horse battery")
    token = store.issue_session()
    assert token and store.verify_session(token) is True
    assert store.verify_session(None) is False
    assert store.verify_session("not-a-session") is False
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert store.verify_session(tampered) is False
    expired = store.issue_session(ttl=-5)
    assert expired and store.verify_session(expired) is False


def test_changing_the_password_signs_sessions_out(tmp_path):
    store = make_store(tmp_path)
    store.set_password("correct horse battery")
    token = store.issue_session()
    assert store.verify_session(token) is True
    store.set_password("another long password")
    assert store.verify_session(token) is False


def test_sessions_absent_before_password_is_set(tmp_path):
    store = make_store(tmp_path)
    assert store.issue_session() is None
    assert store.verify_session("anything.at.all") is False


def test_store_picks_up_external_rewrites(tmp_path):
    store = make_store(tmp_path)
    store.set_password("correct horse battery")
    rewriter = AuthStore(tmp_path / "config" / "auth.json")
    rewriter.set_password("changed elsewhere 123")
    assert store.has_password() is True
    assert store.verify_password("changed elsewhere 123") is True
    assert store.verify_password("correct horse battery") is False


def test_login_throttle_windows_and_resets():
    throttle = LoginThrottle(max_failures=3, window_seconds=60)
    key = "192.0.2.10"
    assert throttle.retry_after(key) == 0
    for _ in range(3):
        throttle.record_failure(key)
    assert throttle.retry_after(key) > 0
    assert throttle.retry_after("192.0.2.11") == 0
    throttle.reset(key)
    assert throttle.retry_after(key) == 0


def test_session_constants():
    assert SESSION_COOKIE == "cortexos_session"
    assert SESSION_TTL_SECONDS == 7 * 24 * 3600
