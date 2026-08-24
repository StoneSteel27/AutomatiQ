"""Local HTTP server that receives telemetry actions from the recorder extension.

The Chrome extension's content scripts (running in an isolated world) hand user
actions to the extension's background service worker via
``chrome.runtime.sendMessage``; the worker relays each payload as a JSON
``fetch`` POST (with ``keepalive: true``) to ``http://127.0.0.1:<port>/act``.
The service-worker relay exists because Chromium's Private Network Access
would otherwise block/prompt requests from HTTPS pages to loopback. This
server parses each payload, stamps it with timestamps, and hands it to the
``BrowserAgent`` for streaming to ``actions.jsonl``.

Using a loopback HTTP endpoint (instead of a CDP ``Runtime.addBinding``) lets
the recorder avoid enabling the ``Runtime`` / ``Debugger`` CDP domains, which
are fingerprintable. Loopback is a secure context, so the relay works even on
HTTPS pages, and isolated-world content scripts are not subject to page CSP.
"""

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

logger = logging.getLogger(__name__)

_HOST = "127.0.0.1"
_PATH = "/act"
_CORS_HEADERS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "*"),
]


class _ActionServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that carries the action callback to the handler."""

    def __init__(self, server_address, RequestHandlerClass, on_action):
        super().__init__(server_address, RequestHandlerClass)
        self.on_action = on_action
        self.daemon_threads = True


class _ActionHandler(BaseHTTPRequestHandler):
    """Handles POST /act (action payload) and OPTIONS (CORS preflight)."""

    def do_OPTIONS(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(204)
        for header, value in _CORS_HEADERS:
            self.send_header(header, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != _PATH:
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            self.send_error(400, f"Invalid JSON: {exc}")
            return

        if not isinstance(payload, dict):
            self.send_error(400, "Payload must be a JSON object")
            return

        try:
            self.server.on_action(payload)
        except Exception as exc:
            logger.exception("on_action callback raised")
            self.send_error(500, str(exc))
            return

        self.send_response(204)
        for header, value in _CORS_HEADERS:
            self.send_header(header, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):  # silence default stderr logging
        pass


class ActionServer:
    """Starts a loopback HTTP server on a free port and relays actions.

    The server is started lazily via :meth:`start` (never in ``__init__``), so
    constructing a ``BrowserAgent`` for tests has no threading or socket side
    effects.
    """

    def __init__(self, on_action):
        self._on_action = on_action
        self._httpd: _ActionServer | None = None
        self._thread: Thread | None = None
        self._port: int | None = None

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def endpoint(self) -> str:
        return f"http://{_HOST}:{self._port}{_PATH}"

    def start(self) -> int:
        if self._httpd is not None:
            return self._port
        self._httpd = _ActionServer((_HOST, 0), _ActionHandler, self._on_action)
        self._port = self._httpd.server_address[1]
        self._thread = Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self._port

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                logger.debug("ActionServer shutdown error", exc_info=True)
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._httpd = None
        self._thread = None
        self._port = None
