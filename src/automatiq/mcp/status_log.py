"""Process-wide status ring: every info/warn/error event lands here so session status can show recent activity."""

from collections import deque
from datetime import datetime

# ── Process-wide activity ring (Tier 2 of two-tier logging) ──────────────────
# Ring of (iso_ts, level, text) for INFO/WARN/ERROR recorder events only.
# This is the PROCESS-WIDE activity ring, shared across sessions by design:
# every single-session status() surfaces its last 15 entries as recent_log
# (newest last). The verbose DEBUG+ copy lives in the session file logger
# (logging_setup._configure_logging).
_STATUS_LOG: deque[tuple[str, str, str]] = deque(maxlen=50)
_STATUS_LOG_WIRED = False
_STATUS_LOG_RECEIVERS: list[tuple[object, object]] = []


def connect_status_log() -> None:
    """Append recorder INFO/WARN/ERROR events to the status ring.

    Idempotent; receiver bodies swallow every exception (blinker propagates
    receiver errors into the emitting recorder thread). TRACEBACK events are
    summarized — no traceback bodies ever reach status payloads.
    """
    global _STATUS_LOG_WIRED
    from automatiq.core import events as ev

    if _STATUS_LOG_WIRED:
        return
    _STATUS_LOG_WIRED = True

    for signal, receiver in _STATUS_LOG_RECEIVERS:
        try:
            signal.disconnect(receiver)
        except Exception:
            pass
    _STATUS_LOG_RECEIVERS.clear()

    def _remember(level: str):
        def _handle(sender, **kw) -> None:
            try:
                _STATUS_LOG.append((datetime.now().isoformat(timespec="seconds"), level, str(kw.get("text", ""))))
            except Exception:
                pass

        return _handle

    def _trace(sender, **kw) -> None:
        try:
            _STATUS_LOG.append(
                (datetime.now().isoformat(timespec="seconds"), "error", "traceback logged to session log")
            )
        except Exception:
            pass

    receivers = [
        (ev.log_info, _remember("info")),
        (ev.log_warn, _remember("warn")),
        (ev.log_error, _remember("error")),
        (ev.log_traceback, _trace),
    ]
    for signal, receiver in receivers:
        signal.connect(receiver)
        _STATUS_LOG_RECEIVERS.append((signal, receiver))
