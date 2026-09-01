"""Anonymous usage-volume telemetry for AutomatiQ.

Design principles (Zero-Identity model):
- No persistent identifiers.  ``run_id`` is a random UUID generated per
  process and kept only in memory.
- No URLs, code, file paths, or user input in any payload.
- All payloads are validated by Pydantic models before dispatch.
- Dispatch runs on a daemon thread with a bounded queue so that telemetry
  never blocks the recording pipeline.
- If the endpoint is unreachable, the payload is silently dropped.
"""

from __future__ import annotations

import datetime
import platform
import queue
import threading
import uuid
from typing import Any

import requests
from pydantic import BaseModel, Field

from automatiq.core import config

_REQUEST_TIMEOUT = 3  # seconds - fail open if endpoint is slow/down
_FLUSH_TIMEOUT = 1.5  # seconds - max wait on stop()

# -- Pydantic payload schemas ------------------------------------------------


class TelemetryEnv(BaseModel):
    os: str
    python_version: str
    automatiq_version: str


class TelemetryPayload(BaseModel):
    run_id: str
    timestamp: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())
    event: str
    env: TelemetryEnv
    properties: dict[str, Any]


# -- Property schemas (per event type) ---------------------------------------


class RecordingStartedProps(BaseModel):
    browser: str
    proxy_enabled: bool
    blocklist_enabled: bool


class RecordingEndedProps(BaseModel):
    duration_seconds: float
    total_http_requests: int
    total_ws_connections: int
    total_ws_frames: int
    browser_used: str
    proxy_enabled: bool
    has_ai_analysis: bool
    crash_reason: str | None  # "browser_crash" | "compilation_error" | "video_error" | "proxy_error" | None


class ServerStartedProps(BaseModel):
    model: str  # recorder model string only - never keys or endpoints
    video_default: bool
    proxy_configured: bool
    vision_key_present: bool


class ToolCalledProps(BaseModel):
    tool: str
    duration_ms: int
    ok: bool
    error_class: str | None  # exception class name only - never messages


class AnnotateRunProps(BaseModel):
    duration_ms: int
    ok: bool


# -- TelemetryClient ---------------------------------------------------------


class TelemetryClient:
    """Non-blocking anonymous telemetry dispatcher.

    A single daemon thread drains a ``queue.Queue`` and POSTs each event as
    JSON to ``config.TELEMETRY_ENDPOINT``.  If telemetry is disabled or the
    request fails, the event is silently dropped - the caller never blocks.
    """

    def __init__(self) -> None:
        self._run_id: str = uuid.uuid4().hex
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._enabled: bool = False
        self._active_command: str = "unknown"
        self._env: TelemetryEnv = TelemetryEnv(
            os=platform.system(),
            python_version=platform.python_version(),
            automatiq_version=config.VERSION,
        )

    # -- lifecycle -------------------------------------------------------

    def start(self, command: str = "unknown") -> None:
        """Start the background worker if telemetry is enabled."""
        self._active_command = command
        self._enabled = bool(getattr(config, "TELEMETRY_ENABLED", False))
        if not self._enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, name="automatiq-telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the worker to stop and briefly wait for pending events."""
        if not self._enabled or self._thread is None:
            return
        self._queue.put(None)  # sentinel
        self._thread.join(timeout=_FLUSH_TIMEOUT)

    @property
    def run_id(self) -> str:
        """Random per-process UUID (stable for the lifetime of the process)."""
        return self._run_id

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def active_command(self) -> str:
        return self._active_command

    # -- public API -------------------------------------------------------

    def track(self, event: str, properties: dict[str, Any]) -> None:
        """Queue an event for asynchronous dispatch (non-blocking)."""
        if not self._enabled:
            return
        payload = {
            "run_id": self._run_id,
            "event": event,
            "env": self._env.model_dump(),
            "properties": properties,
        }
        self._queue.put(payload)

    def track_recording_started(self, props: RecordingStartedProps) -> None:
        self.track("recording_started", props.model_dump())

    def track_recording_ended(self, props: RecordingEndedProps) -> None:
        self.track("recording_ended", props.model_dump())

    def track_server_started(self, props: ServerStartedProps) -> None:
        self.track("server_started", props.model_dump())

    def track_tool_called(self, tool: str, duration_ms: int, ok: bool, error_class: str | None = None) -> None:
        self.track(
            "tool_called",
            ToolCalledProps(tool=tool, duration_ms=int(duration_ms), ok=bool(ok), error_class=error_class).model_dump(),
        )

    def track_annotate_run(self, duration_ms: int, ok: bool) -> None:
        self.track("annotate_run", AnnotateRunProps(duration_ms=int(duration_ms), ok=bool(ok)).model_dump())

    # -- internals --------------------------------------------------------

    def _worker(self) -> None:
        """Drain the queue and POST events; drop silently on failure."""
        endpoint = getattr(config, "TELEMETRY_ENDPOINT", None)
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                return
            self._send(endpoint, item)

    def _send(self, endpoint: str | None, payload: dict[str, Any]) -> None:
        if not endpoint:
            return
        try:
            validated = TelemetryPayload(**payload)
            requests.post(
                endpoint,
                json=validated.model_dump(),
                headers={"Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception:
            pass


# -- Module-level singleton --------------------------------------------------

client = TelemetryClient()
