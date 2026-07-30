from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            values[key] = value
    return values


@contextlib.contextmanager
def _silence_output() -> Iterator[None]:
    """Suppress Python and native-library output so credentials cannot leak."""
    with open(os.devnull, "w", encoding="utf-8") as sink:
        saved_fds: list[tuple[int, int]] = []
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
                stream_fd = stream.fileno()
                saved_fd = os.dup(stream_fd)
                os.dup2(sink.fileno(), stream_fd)
                saved_fds.append((stream_fd, saved_fd))
            except (AttributeError, OSError, ValueError):
                continue

        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
        finally:
            for stream_fd, saved_fd in reversed(saved_fds):
                try:
                    os.dup2(saved_fd, stream_fd)
                finally:
                    os.close(saved_fd)


def _login_smoke(env_path: Path) -> bool:
    api = None
    login_ok = False
    try:
        values = _load_dotenv(env_path)
        api_key = values.get("SHIOAJI_API_KEY", "").strip()
        secret_key = (
            values.get("SHIOAJI_SECRET_KEY", "").strip()
            or values.get("SECRET_KEY", "").strip()
        )
        if not api_key or not secret_key:
            return False

        with _silence_output():
            import shioaji as sj

            api = sj.Shioaji()
            api.login(
                api_key=api_key,
                secret_key=secret_key,
                fetch_contract=False,
                subscribe_trade=False,
            )
        login_ok = True
    except BaseException:
        login_ok = False
    finally:
        if api is not None:
            try:
                with _silence_output():
                    api.logout()
            except BaseException:
                pass
    return login_ok


def main() -> int:
    login_ok = _login_smoke(ENV_FILE)
    print("LOGIN_SMOKE_OK" if login_ok else "LOGIN_SMOKE_FAIL")
    return 0 if login_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
