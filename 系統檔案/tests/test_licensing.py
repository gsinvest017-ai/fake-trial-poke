"""Keyguard integration and release-enforcement policy."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import licensing
import service


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("FAKE_TRIAL_POKE_LICENCE_EMAIL", raising=False)
    monkeypatch.delenv("FAKE_TRIAL_POKE_LICENCE_ENFORCE", raising=False)


@pytest.fixture
def issuer(monkeypatch):
    from keyguard.licensekey import (
        LicenseKeyIssuer,
        encode_key_material,
        generate_keypair,
    )

    seed, public = generate_keypair()
    monkeypatch.setenv("KEYGUARD_PUBLIC_KEY", encode_key_material(public))
    monkeypatch.setattr("keyguard.licensekey.EMBEDDED_PUBLIC_KEY", "")
    return LicenseKeyIssuer(seed)


def test_application_binding_is_stable():
    assert licensing.APP_ID == "FAKE_TRIAL_POKE"
    assert licensing.ENFORCE_ENV == "FAKE_TRIAL_POKE_LICENCE_ENFORCE"
    assert licensing.EMAIL_ENV == "FAKE_TRIAL_POKE_LICENCE_EMAIL"


def test_development_runs_are_advisory(monkeypatch):
    monkeypatch.setattr("sys.frozen", False, raising=False)
    assert licensing.should_enforce() is False


def test_frozen_release_enforces(monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    assert licensing.should_enforce() is True


def test_unknown_customer_gets_bounded_trial(monkeypatch, issuer):
    monkeypatch.setenv(
        "FAKE_TRIAL_POKE_LICENCE_EMAIL",
        "trial@example.com",
    )
    state = licensing.check()
    assert state.status == "trial"
    assert state.allowed
    assert state.days_remaining is not None
    assert state.days_remaining <= 14


def test_signed_expiring_key_activates(monkeypatch, issuer):
    email = "buyer@example.com"
    monkeypatch.setenv("FAKE_TRIAL_POKE_LICENCE_EMAIL", email)
    key = issuer.issue(
        "FAKE_TRIAL_POKE",
        email,
        days=30,
        seats=1,
        plan="TRIAL_RELEASE",
    )

    ok, _ = licensing.activate(key)
    assert ok

    state = licensing.check()
    assert state.status == "active"
    assert state.allowed
    assert state.days_remaining is not None
    assert 28 <= state.days_remaining <= 30


def test_genuine_expired_key_is_blocked(monkeypatch, issuer):
    email = "expired@example.com"
    monkeypatch.setenv("FAKE_TRIAL_POKE_LICENCE_EMAIL", email)
    now = datetime.now(timezone.utc)
    key = issuer.issue(
        "FAKE_TRIAL_POKE",
        email,
        expires_utc=now + timedelta(days=1),
        seats=1,
    )
    ok, _ = licensing.activate(key)
    assert ok

    monkeypatch.setattr(
        "keyguard.licensekey._now_utc",
        lambda: now + timedelta(days=2),
    )
    monkeypatch.setenv("FAKE_TRIAL_POKE_LICENCE_ENFORCE", "1")
    state = licensing.check()
    assert state.status == "expired"
    assert state.allowed is False


def test_key_for_another_application_is_rejected(monkeypatch, issuer):
    email = "buyer@example.com"
    monkeypatch.setenv("FAKE_TRIAL_POKE_LICENCE_EMAIL", email)
    ok, message = licensing.activate(
        issuer.issue("OTHER_APP", email, days=30)
    )
    assert not ok
    assert "OTHER_APP" in message


def test_cli_exposes_licence_commands():
    args = service.parse_args(
        [
            "--activate",
            "KG1.example",
            "--licence-email",
            "buyer@example.com",
        ]
    )
    assert args.activate == "KG1.example"
    assert args.licence_email == "buyer@example.com"


def test_background_desktop_thread_does_not_install_signal_handlers(
    monkeypatch,
):
    calls: list[object] = []
    monkeypatch.setattr("signal.signal", lambda *args: calls.append(args))
    monkeypatch.setattr(
        "threading.current_thread",
        lambda: object(),
    )
    monkeypatch.setattr(
        "threading.main_thread",
        lambda: object(),
    )

    service.install_stop_handlers(object())
    assert calls == []
