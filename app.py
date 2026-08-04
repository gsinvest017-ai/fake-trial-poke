"""Licensed desktop entry point for the fake-trial-poke release build."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path


APP_FOLDER_NAME = "fake-trial-poke"


def _frozen_runtime_dir() -> Path:
    """Where a packaged build keeps the data it writes.

    Windows keeps it beside the executable, which is where every installed
    copy already has its `data\\`, `log\\` and `.env` -- moving it would strand
    those. A macOS `.app` cannot do the same: `sys.executable` sits inside
    `Fake Trial Poke.app/Contents/MacOS`, and writing there means the recording
    lives inside the bundle, breaks the code signature, and is thrown away by
    the next drag-and-drop upgrade. Application Support is the writable place
    macOS actually offers.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_FOLDER_NAME
    if sys.platform.startswith("linux"):
        base = os.environ.get("XDG_DATA_HOME") or str(
            Path.home() / ".local" / "share"
        )
        return Path(base) / APP_FOLDER_NAME
    return Path(sys.executable).resolve().parent


if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    RUNTIME_DIR = _frozen_runtime_dir()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
else:
    BUNDLE_DIR = Path(__file__).resolve().parent / "系統檔案"
    RUNTIME_DIR = BUNDLE_DIR

if str(BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(BUNDLE_DIR))

import control_state
import licensing
import recorder
import scanner
import service
import telegram_notify
import webserver


LOG = logging.getLogger(__name__)
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8900
WINDOW_TITLE = "假試撮盤前監控"


def _copy_first_run_file(relative_path: str) -> None:
    source = BUNDLE_DIR / relative_path
    destination = RUNTIME_DIR / relative_path
    if destination.exists() or not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _configure_runtime_paths() -> None:
    """Keep mutable data beside the installed executable, not in _MEIPASS."""
    data_dir = RUNTIME_DIR / "data"
    history_dir = data_dir / "history"

    service.BASE_DIR = RUNTIME_DIR
    service.DATA_DIR = data_dir
    service.HISTORY_DIR = history_dir
    service.HOLIDAYS_PATH = data_dir / "holidays.txt"

    control_state.BASE_DIR = RUNTIME_DIR
    control_state.CONTROL_PATH = data_dir / "control.json"

    recorder.BASE_DIR = RUNTIME_DIR
    recorder.DATA_DIR = data_dir
    recorder.LOG_DIR = RUNTIME_DIR / "log"

    scanner.BASE_DIR = RUNTIME_DIR
    scanner.DATA_DIR = data_dir

    telegram_notify.BASE_DIR = RUNTIME_DIR
    telegram_notify.DEFAULT_ENV_PATH = RUNTIME_DIR / ".env"

    webserver.UI_DIR = BUNDLE_DIR / "ui"
    webserver.INDEX_PATH = webserver.UI_DIR / "index.html"

    _copy_first_run_file("data/holidays.txt")
    _copy_first_run_file(".env.example")


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((SERVER_HOST, port)) != 0


def _find_free_port(preferred: int) -> int:
    if _port_is_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((SERVER_HOST, 0))
        return int(sock.getsockname()[1])


def _wait_for_server(port: int, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((SERVER_HOST, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_server(port: int) -> None:
    try:
        exit_code = service.main(
            ["--host", SERVER_HOST, "--port", str(port)]
        )
    except BaseException:
        LOG.exception("background service stopped unexpectedly")
        return
    if exit_code:
        LOG.error("background service exited with code %s", exit_code)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=SERVER_PORT)
    parser.add_argument("--activate", metavar="KEY")
    parser.add_argument("--licence-email", metavar="EMAIL")
    parser.add_argument("--licence-status", action="store_true")
    parser.add_argument("--machine-id", action="store_true")
    return parser


def _ui_shell_html() -> str:
    """The application's own markup, for the lock screen to sit in front of.

    Read straight off disk rather than fetched from the service, because the
    service is precisely what must not be running: the shell is there so the
    customer recognises their application, not so it works.
    """
    try:
        return webserver.INDEX_PATH.read_text(encoding="utf-8")
    except OSError:
        LOG.warning("could not read the UI shell for the lock screen")
        return ""


def _refuse_if_unlicensed() -> None:
    """Stop here unless the licence allows the application to run.

    The refusal is shown *inside a window that looks like this application*,
    with the real UI inert behind it, instead of a small dialog on an empty
    desktop -- a bare dialog reads as a launcher that crashed rather than as a
    licence that lapsed.

    Nothing below this point runs: no port, no Shioaji login, no background
    scan. That ordering is the whole guarantee. Starting the service first and
    covering it with a window would leave the real thing running behind
    something anyone can close.
    """
    # Before deciding, not after: the vendor may have renewed already, and
    # refusing somebody for a licence that exists -- and then telling them to
    # run a command to install the thing they were sent -- is the whole reason
    # this step exists. Only ever extends, and a failure here changes nothing.
    note = licensing.refresh_licence(url=licensing.STATUS_URL)
    if note:
        LOG.warning("%s", note)

    # check() already folds in soft mode -- it returns allowed=True whenever
    # should_enforce() is false -- so a source checkout still runs freely.
    state = licensing.check()
    if state.allowed:
        return

    LOG.error("licence refused: %s (%s)", state.message, state.status)
    for folder in licensing.licence_inboxes():
        LOG.error("a renewal saved into %s is picked up on the next launch",
                  folder)
    if licensing.refuse_in_window(
            state,
            title=WINDOW_TITLE,
            width=1440,
            height=900,
            background="#07060a",
            behind_html=_ui_shell_html()):
        raise SystemExit(2)

    # No pywebview, or the window could not open: fall back to the tkinter
    # window / MessageBox, which exits on its own.
    licensing.exit_with_message(state)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s -- %(message)s",
        datefmt="%H:%M:%S",
    )
    _configure_runtime_paths()

    # Every branch below is a one-shot command whose whole output is its answer,
    # and this is a windowed build: nothing printed reaches the operator until a
    # console is attached. `report_cli` does that, and falls back to a dialog.
    if args.machine_id:
        licensing.attach_parent_console()
        print(licensing.machine_id())
        return 0
    if args.activate:
        ok, message = licensing.activate(
            args.activate,
            args.licence_email or "",
        )
        return licensing.report_cli(ok, message)
    if args.licence_status:
        licensing.attach_parent_console()
        print(
            json.dumps(
                licensing.check().to_dict(),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    _refuse_if_unlicensed()

    port = _find_free_port(args.port)
    threading.Thread(
        target=_start_server,
        args=(port,),
        daemon=True,
        name="fake-trial-poke-service",
    ).start()
    if not _wait_for_server(port):
        LOG.error("service did not become ready within 45 seconds")
        return 1

    try:
        import webview
    except ImportError as exc:
        # Report what actually failed, not a guess. On the first real macOS
        # build `webview` itself was bundled correctly and the missing piece
        # was its dependency `bottle` -- and "pywebview is missing from this
        # build" sent the search in exactly the wrong direction. The import
        # error already names the culprit; print it.
        LOG.error("could not import pywebview: %s", exc)
        return 1

    webview.create_window(
        WINDOW_TITLE,
        f"http://{SERVER_HOST}:{port}/",
        width=1440,
        height=900,
        min_size=(960, 640),
        resizable=True,
        background_color="#07060a",
    )
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
