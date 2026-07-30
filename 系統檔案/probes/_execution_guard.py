"""Shared safety gate for development probes that log in to the broker."""

from __future__ import annotations

import sys


def require_explicit_broker_login_confirmation() -> bool:
    """Require an interactive, exact ``YES`` before a probe may continue."""
    try:
        interactive = bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        interactive = False

    if not interactive:
        print(
            "WARNING: This development probe performs a real Sinopac broker "
            "login. Non-interactive execution was aborted; no login occurred.",
            file=sys.stderr,
        )
        return False

    print(
        "WARNING: This is a development probe. Continuing performs a real "
        "Sinopac broker login."
    )
    try:
        answer = input("Type exactly YES to continue: ")
    except (EOFError, KeyboardInterrupt):
        print(
            "\nConfirmation was not received. Aborted without broker login.",
            file=sys.stderr,
        )
        return False

    if answer != "YES":
        print(
            "Exact YES was not entered. Aborted without broker login.",
            file=sys.stderr,
        )
        return False
    return True
