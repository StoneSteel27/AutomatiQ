"""Two-tier logging: verbose DEBUG+ session file, minimized stderr; stdlib bridge for automatiq.* records."""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from automatiq.core import config

logger = logging.getLogger("automatiq.mcp")

# ── Logging / events wiring (two-tier; stdout is pure MCP protocol) ──────────
# Console (stderr) is gated to config.LOG_LEVEL; the per-session file records
# everything DEBUG+ from two loggers sharing ONE FileHandler:
# - "automatiq.session" receives the recorder blinker event traffic
#   (propagate=False, so it never double-prints on stderr);
# - "automatiq" bridges stdlib logging from OUR modules (bin_manager,
#   browser_manager, action_server, the ParentWatchdog, ...) into the same
#   file; it keeps propagate=True so WARN+ still reaches the stderr console,
#   where the stderr handler's own level filters DEBUG records.
# Third-party loggers stay gated by root's own level and reach neither file
# nor console at DEBUG.

_SESSION_LOGGER_NAME = "automatiq.session"
_BRIDGE_LOGGER_NAME = "automatiq"

_STDERR_WIRED = False
_SESSION_FILE_WIRED = False
_EVENTS_WIRED = False
_EVENT_RECEIVERS: dict[str, object] = {}


class _StderrStreamHandler(logging.StreamHandler):
    """StreamHandler that re-resolves ``sys.stderr`` on every emit.

    Keeps the console tier pointed at the live stderr even when a host swaps
    the stream after ``_configure_logging`` (test capture harnesses, embedders).
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def _configure_logging() -> None:
    """Stderr console at LOG_LEVEL + verbose DEBUG+ session file logger.

    Idempotent: repeat calls never stack handlers. config is read at call
    time so LOGS_DIR/LOG_LEVEL can be overridden (tests) after import.
    """
    global _STDERR_WIRED, _SESSION_FILE_WIRED

    config.ensure_system_dirs()  # LOGS_DIR must exist before the FileHandler

    root = logging.getLogger()
    if not _STDERR_WIRED:
        handler = _StderrStreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        # Propagated records are filtered per-handler (ancestor logger levels
        # are NOT re-checked), so this explicit level keeps the console tier
        # identical: automatiq DEBUG records reach this handler but are
        # dropped here unless LOG_LEVEL=DEBUG.
        handler.setLevel(config.LOG_LEVEL)
        root.addHandler(handler)
        _STDERR_WIRED = True
    root.setLevel(config.LOG_LEVEL)

    session_logger = logging.getLogger(_SESSION_LOGGER_NAME)
    session_logger.setLevel(logging.DEBUG)
    session_logger.propagate = False  # file-only: never double-prints on stderr
    if not _SESSION_FILE_WIRED:
        file_handler = logging.FileHandler(
            config.LOGS_DIR / f"session_{datetime.now():%Y%m%d_%H%M%S}.log",
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-5s  %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        session_logger.addHandler(file_handler)
        # Same handler on the "automatiq" bridge logger: stdlib logger.* calls
        # from our modules land in the session file too (DEBUG+), while still
        # propagating to root -> stderr (filtered there by LOG_LEVEL).
        bridge_logger = logging.getLogger(_BRIDGE_LOGGER_NAME)
        bridge_logger.setLevel(logging.DEBUG)
        bridge_logger.addHandler(file_handler)
        _SESSION_FILE_WIRED = True


def _wire_event_logging() -> None:
    """Route recorder blinker signals into the two-tier loggers.

    INFO/DEBUG go to "automatiq.session" (file-only, propagate=False). WARN
    and ERROR go through "automatiq.mcp" ONLY: the record propagates into the
    session file via the "automatiq" bridge handler and to stderr via root,
    one line per tier. TRACEBACK is file-only with exc_info.

    Receiver bodies swallow every exception — blinker propagates receiver
    errors into the emitting recorder thread, and logging must never kill a
    session. Idempotent via flag; stale receivers from a previous wiring are
    disconnected first.
    """
    global _EVENTS_WIRED
    from automatiq.core import events as ev

    if _EVENTS_WIRED:
        return
    _EVENTS_WIRED = True

    session_logger = logging.getLogger(_SESSION_LOGGER_NAME)

    for name, receiver in _EVENT_RECEIVERS.items():
        try:
            getattr(ev, name).disconnect(receiver)
        except Exception:
            pass
    _EVENT_RECEIVERS.clear()

    def _file_info(sender, **kw) -> None:
        try:
            session_logger.info("[recorder] %s", kw.get("text", ""))
        except Exception:
            pass

    def _file_debug(sender, **kw) -> None:
        try:
            session_logger.debug("[recorder] %s", kw.get("text", ""))
        except Exception:
            pass

    def _warn_both_tiers(sender, **kw) -> None:
        """WARN: one logger call, two tiers via propagation (file + stderr)."""
        try:
            logger.warning("[recorder] %s", kw.get("text", ""))
        except Exception:
            pass

    def _error_both_tiers(sender, **kw) -> None:
        """ERROR: one logger call, two tiers via propagation (file + stderr)."""
        try:
            logger.error("[recorder] %s", kw.get("text", ""))
        except Exception:
            pass

    def _file_traceback(sender, **kw) -> None:
        try:
            session_logger.error("[recorder] traceback:", exc_info=True)
        except Exception:
            pass

    receivers = {
        "log_debug": _file_debug,
        "log_info": _file_info,
        "log_warn": _warn_both_tiers,
        "log_error": _error_both_tiers,
        "log_traceback": _file_traceback,
    }
    for name, receiver in receivers.items():
        getattr(ev, name).connect(receiver)
        _EVENT_RECEIVERS[name] = receiver
