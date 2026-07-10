"""Anonymous usage-volume telemetry for AutomatiQ.

Design principles (Zero-Identity model):
- No persistent identifiers.  ``run_id`` is a random UUID generated per
  process and kept only in memory.
- No URLs, code, file paths, or user input in any payload.
- All payloads are validated by Pydantic models before dispatch.
- Dispatch runs on a daemon thread with a bounded queue so that telemetry
  never blocks the CLI or agent loop.
- If the endpoint is unreachable, the payload is silently dropped.
- Periodic heartbeats (every ``HEARTBEAT_INTERVAL`` seconds) ensure data
  survives terminal closure.
"""

from __future__ import annotations

import datetime
import logging
import os
import platform
import queue
import re
import threading
import traceback
import uuid
from typing import Any, Literal

import requests
from pydantic import BaseModel, Field

from automatiq.core import config

log = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 3  # seconds — fail open if endpoint is slow/down
_FLUSH_TIMEOUT = 1.5  # seconds — max wait on stop()
HEARTBEAT_INTERVAL = 300  # seconds — periodic snapshot cadence

# ── Sanitisation helpers ─────────────────────────────────────────────────────

_REDACT_PATTERNS = [
    re.compile(r"[A-Za-z]:\\[^\s'\"<>]+"),  # Windows paths
    re.compile(r"/(?:home|Users|usr|var|tmp|opt|etc|root)[^\s'\"<>]*"),  # Unix paths
    re.compile(r"https?://[^\s'\"<>]+"),  # URLs
    re.compile(r"sk-[a-zA-Z0-9_-]{10,}"),  # OpenAI-style API keys
    re.compile(r"Bearer\s+[a-zA-Z0-9._-]+"),  # Bearer tokens
    re.compile(r"token=[^\s&'\"<>]+"),  # Token query params
]


def _sanitize_message(msg: str, max_len: int = 200) -> str:
    """Strip file paths, URLs, and tokens from *msg*; truncate to *max_len*."""
    for pattern in _REDACT_PATTERNS:
        msg = pattern.sub("<redacted>", msg)
    if len(msg) > max_len:
        msg = msg[:max_len] + "..."
    return msg


def _extract_error_location(exc: BaseException) -> tuple[int, str]:
    """Return ``(line, file_basename)`` for the last automatiq-package traceback frame.

    Falls back to the last frame overall (or ``(0, "unknown")``) when no
    automatiq frame is present.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    last_automatiq = None
    for frame in frames:
        if "/automatiq/" in frame.filename.replace("\\", "/"):
            last_automatiq = frame
    if last_automatiq is not None:
        return last_automatiq.lineno, os.path.basename(last_automatiq.filename)
    if frames:
        return frames[-1].lineno, os.path.basename(frames[-1].filename)
    return 0, "unknown"


# ── Pydantic payload schemas ────────────────────────────────────────────────


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


# ── Property schemas (per event type) ───────────────────────────────────────


class AgentErrorProps(BaseModel):
    """A single error captured during an agent session."""

    exception_class: str
    message: str
    line: int
    file: str
    phase: str
    step: int
    cell: int


class AgentStartedProps(BaseModel):
    model: str
    session_type: Literal["fresh", "resume"]
    proxy_enabled: bool
    max_steps: int


class RecordingStartedProps(BaseModel):
    browser: str
    proxy_enabled: bool
    blocklist_enabled: bool


class AgentHeartbeatProps(BaseModel):
    step: int
    cell: int
    duration_seconds: float
    current_mode: str
    current_phase: str
    final_scripts_submitted: int
    guardrails: dict[str, int]
    errors: list[AgentErrorProps]


class ModeSwitchedProps(BaseModel):
    from_mode: str
    to_mode: str
    step: int
    cell: int


class FinalScriptSubmittedProps(BaseModel):
    step: int
    cell: int
    duration_seconds: float
    total_tokens: int


class AgentSessionEndedProps(BaseModel):
    outcome: Literal["success", "abandoned_by_user", "step_limit_reached", "crash"]
    model: str
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    steps_taken: int
    cells_executed: int
    duration_seconds: float
    proxy_enabled: bool
    guardrails: dict[str, int] = Field(
        default_factory=lambda: {
            "duplicate_thought": 0,
            "repeated_execution": 0,
            "final_script_bounce": 0,
            "step_limit": 0,
            "validation_bailout": 0,
        }
    )
    errors: list[AgentErrorProps] = Field(default_factory=list)
    crash_step: int | None = None
    crash_cell: int | None = None
    crash_phase: str | None = None


class RecordingEndedProps(BaseModel):
    duration_seconds: float
    total_http_requests: int
    total_ws_connections: int
    total_ws_frames: int
    browser_used: str
    proxy_enabled: bool
    has_ai_analysis: bool
    crash_reason: str | None  # "browser_crash" | "compilation_error" | "video_error" | "proxy_error" | None


class SystemCrashProps(BaseModel):
    crash_type: Literal["force_quit", "unhandled_exception"]
    exception_class: str | None = None
    message: str | None = None
    line: int | None = None
    file: str | None = None
    module: str | None = None
    active_command: str
    step: int | None = None
    cell: int | None = None
    phase: str | None = None


class UserFeedbackProps(BaseModel):
    message: str


# ── Error factory helpers ────────────────────────────────────────────────────


def make_error_props(
    exception_class: str,
    message: str,
    line: int,
    file: str,
    phase: str,
    step: int,
    cell: int,
) -> AgentErrorProps:
    """Build an :class:`AgentErrorProps` with sanitised message."""
    return AgentErrorProps(
        exception_class=exception_class,
        message=_sanitize_message(message),
        line=line,
        file=file,
        phase=phase,
        step=step,
        cell=cell,
    )


def make_error_from_exc(
    exc: BaseException,
    phase: str,
    step: int,
    cell: int,
) -> AgentErrorProps:
    """Build an :class:`AgentErrorProps` from a real exception, extracting source location."""
    line, file = _extract_error_location(exc)
    return make_error_props(
        exception_class=type(exc).__name__,
        message=str(exc),
        line=line,
        file=file,
        phase=phase,
        step=step,
        cell=cell,
    )


# ── TelemetryClient ─────────────────────────────────────────────────────────


class TelemetryClient:
    """Non-blocking anonymous telemetry dispatcher.

    A single daemon thread drains a ``queue.Queue`` and POSTs each event as
    JSON to ``config.TELEMETRY_ENDPOINT``.  If telemetry is disabled or the
    request fails, the event is silently dropped — the caller never blocks.
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

    # ── lifecycle ───────────────────────────────────────────────────────

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
        return self._run_id

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def active_command(self) -> str:
        return self._active_command

    # ── public API ───────────────────────────────────────────────────────

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

    def track_agent_started(self, props: AgentStartedProps) -> None:
        self.track("agent_started", props.model_dump())

    def track_agent_heartbeat(self, props: AgentHeartbeatProps) -> None:
        self.track("agent_heartbeat", props.model_dump())

    def track_mode_switched(self, props: ModeSwitchedProps) -> None:
        self.track("mode_switched", props.model_dump())

    def track_final_script_submitted(self, props: FinalScriptSubmittedProps) -> None:
        self.track("final_script_submitted", props.model_dump())

    def track_agent_session_ended(self, props: AgentSessionEndedProps) -> None:
        self.track("agent_session_ended", props.model_dump())

    def track_recording_started(self, props: RecordingStartedProps) -> None:
        self.track("recording_started", props.model_dump())

    def track_recording_ended(self, props: RecordingEndedProps) -> None:
        self.track("recording_ended", props.model_dump())

    def track_system_crash(self, props: SystemCrashProps) -> None:
        self.track("system_crash", props.model_dump())

    def track_feedback(self, message: str) -> None:
        props = UserFeedbackProps(message=message)
        self.track("user_feedback", props.model_dump())

    def flush_sync(self, timeout: float = _FLUSH_TIMEOUT) -> None:
        """Block until the queue is drained or *timeout* elapses.

        Used by the ``feedback`` command which needs to confirm delivery
        before the process exits.
        """
        if not self._enabled or self._thread is None:
            return
        deadline = threading.Event()
        self._queue.put(("__flush__", deadline))
        deadline.wait(timeout=timeout)

    # ── internals ────────────────────────────────────────────────────────

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
            if isinstance(item, tuple) and item[0] == "__flush__":
                item[1].set()  # type: ignore[index]
                continue
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


# ── Module-level singleton ──────────────────────────────────────────────────

client = TelemetryClient()
