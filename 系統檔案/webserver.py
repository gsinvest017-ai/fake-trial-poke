"""Local, dependency-free HTTP server for the live auction detector UI."""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit


HOST = "127.0.0.1"
DEFAULT_PORT = 8900
UI_DIR = Path(__file__).resolve().parent / "ui"
INDEX_PATH = UI_DIR / "index.html"

StateProvider = Callable[[], Mapping[str, Any]] | Any
ControlHandler = Callable[[str, bool | None], Mapping[str, Any]]

_LOGGER = logging.getLogger(__name__)
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _get_state(provider: StateProvider) -> Mapping[str, Any]:
    """Read a fresh state snapshot from a callable or SharedState-like object."""
    if callable(provider):
        state = provider()
    elif callable(getattr(provider, "get_state", None)):
        state = provider.get_state()
    elif callable(getattr(provider, "snapshot", None)):
        state = provider.snapshot()
    else:
        raise TypeError(
            "state_provider must be callable or expose get_state()/snapshot()"
        )

    if not isinstance(state, Mapping):
        raise TypeError("state_provider must return a mapping")
    return state


def _is_within_ui(path: Path) -> bool:
    try:
        path.relative_to(UI_DIR)
    except ValueError:
        return False
    return True


class LocalHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server holding the detector's shared state provider."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        state_provider: StateProvider,
        control_handler: ControlHandler | None = None,
    ) -> None:
        self.state_provider = state_provider
        self.control_handler = control_handler
        self.serve_thread: threading.Thread | None = None
        super().__init__(server_address, DetectorRequestHandler)


class DetectorRequestHandler(BaseHTTPRequestHandler):
    """Serve the detector state API and self-contained local UI."""

    server: LocalHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = unquote(urlsplit(self.path).path)

        if path == "/api/state":
            self._serve_state()
            return
        if path in {"/", "/index.html", "/ui", "/ui/"}:
            self._serve_file(INDEX_PATH, no_store=True)
            return
        if path == "/favicon.ico":
            self._send_bytes(204, b"", "image/x-icon")
            return
        if path.startswith("/ui/"):
            requested = (UI_DIR / path.removeprefix("/ui/")).resolve()
            if _is_within_ui(requested) and requested.is_file():
                self._serve_file(requested, no_store=False)
                return

        self._send_json(
            404,
            {"error": "not_found", "message": "找不到指定的本機資源"},
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = unquote(urlsplit(self.path).path)
        if path != "/api/control":
            self._send_json(
                404,
                {"error": "not_found", "message": "找不到指定的本機資源"},
            )
            return
        self._serve_control()

    def _serve_control(self) -> None:
        handler = self.server.control_handler
        if handler is None:
            self._send_json(
                503,
                {
                    "error": "control_unavailable",
                    "message": "目前服務未啟用錄製控制",
                },
            )
            return

        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length or "")
        except ValueError:
            content_length = -1
        if content_length < 1:
            self._send_json(
                400,
                {
                    "error": "invalid_body",
                    "message": "請提供 JSON request body",
                },
            )
            return
        if content_length > 16_384:
            self._send_json(
                413,
                {
                    "error": "body_too_large",
                    "message": "request body 過大",
                },
            )
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(
                400,
                {
                    "error": "invalid_json",
                    "message": "request body 必須是有效 JSON",
                },
            )
            return
        if not isinstance(payload, dict):
            self._send_json(
                400,
                {
                    "error": "invalid_body",
                    "message": "request body 必須是 JSON object",
                },
            )
            return

        action = payload.get("action")
        if action not in {"set_auto", "stop", "start"}:
            self._send_json(
                400,
                {
                    "error": "invalid_action",
                    "message": "action 必須是 set_auto、stop 或 start",
                },
            )
            return
        enabled: bool | None = None
        if action == "set_auto":
            enabled = payload.get("enabled")
            if type(enabled) is not bool:
                self._send_json(
                    400,
                    {
                        "error": "invalid_enabled",
                        "message": "set_auto 的 enabled 必須是 boolean",
                    },
                )
                return

        try:
            state = handler(action, enabled)
            if not isinstance(state, Mapping):
                raise TypeError("control handler must return a mapping")
            self._send_json(200, state)
        except Exception as exc:
            status = int(getattr(exc, "status_code", 500))
            error = str(getattr(exc, "error_code", "control_failed"))
            if status < 400 or status > 599:
                status = 500
            if status >= 500:
                _LOGGER.exception(
                    "Recording control failed (%s)",
                    type(exc).__name__,
                )
                message = "錄製控制暫時無法完成"
            else:
                message = str(exc) or "錄製控制要求無法執行"
            self._send_json(
                status,
                {"error": error, "message": message},
            )

    def _serve_state(self) -> None:
        try:
            state = _get_state(self.server.state_provider)
            self._send_json(200, state)
        except Exception:
            _LOGGER.exception("Failed to read detector state")
            self._send_json(
                503,
                {
                    "error": "state_unavailable",
                    "message": "即時狀態暫時無法取得",
                },
            )

    def _serve_file(self, path: Path, *, no_store: bool) -> None:
        try:
            content = path.read_bytes()
        except OSError:
            self._send_json(
                404,
                {"error": "ui_not_found", "message": "找不到偵測器介面"},
            )
            return

        content_type, _ = mimetypes.guess_type(path.name)
        if content_type is None:
            content_type = "application/octet-stream"
        if content_type.startswith("text/"):
            content_type = f"{content_type}; charset=utf-8"
        self._send_bytes(
            200,
            content,
            content_type,
            cache_control="no-store" if no_store else "public, max-age=3600",
        )

    def _send_json(self, status: int, payload: Any) -> None:
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
                default=_json_default,
            ).encode("utf-8")
        except (TypeError, ValueError):
            _LOGGER.exception("Failed to serialize API response")
            status = 500
            body = json.dumps(
                {"error": "invalid_state", "message": "即時狀態格式錯誤"},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            cache_control="no-store, max-age=0",
        )

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        _LOGGER.debug("%s - %s", self.address_string(), format % args)


def _validate_local_host(host: str) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("The detector web server may only bind to localhost")


def create_server(
    state_provider: StateProvider,
    *,
    host: str = HOST,
    port: int = DEFAULT_PORT,
    control_handler: ControlHandler | None = None,
) -> LocalHTTPServer:
    """Create, but do not start, a localhost-only detector server."""
    _validate_local_host(host)
    return LocalHTTPServer(
        (host, int(port)),
        state_provider,
        control_handler,
    )


def start_server(
    state_provider: StateProvider,
    *,
    host: str = HOST,
    port: int = DEFAULT_PORT,
    control_handler: ControlHandler | None = None,
) -> LocalHTTPServer:
    """Start the detector web server in a daemon background thread."""
    server = create_server(
        state_provider,
        host=host,
        port=port,
        control_handler=control_handler,
    )
    thread = threading.Thread(
        target=server.serve_forever,
        name="auction-detector-web",
        daemon=True,
    )
    server.serve_thread = thread
    thread.start()
    return server


def stop_server(server: LocalHTTPServer) -> None:
    """Gracefully stop a server returned by :func:`start_server`."""
    server.shutdown()
    server.server_close()
    thread = server.serve_thread
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2.0)


def serve_forever(
    state_provider: StateProvider,
    *,
    host: str = HOST,
    port: int = DEFAULT_PORT,
    control_handler: ControlHandler | None = None,
) -> None:
    """Run a localhost-only detector server in the current thread."""
    server = create_server(
        state_provider,
        host=host,
        port=port,
        control_handler=control_handler,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
