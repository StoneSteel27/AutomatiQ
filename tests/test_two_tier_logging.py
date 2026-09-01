"""Two-tier logging: verbose DEBUG+ session file, minimized console/status.

Isolation: every test rewires against a temp LOGS_DIR (monkeypatched on
automatiq.core.config — both wiring functions read config at call time) and
resets the idempotency flags first. The stderr assertions go through capsys,
which the `wired` fixture requests so the fresh StreamHandler binds to the
capture stream. Teardown disconnects the test's receivers, closes every
session-logger handler, and clears the status ring so later tests are clean.
"""

import logging

import pytest

import automatiq.mcp.logging_setup as logging_setup
import automatiq.mcp.status_log as status_log
from automatiq.core import events as ev
from automatiq.mcp.runtime import RecordingSession, SessionRegistry

_SESSION_LOGGER_NAME = logging_setup._SESSION_LOGGER_NAME


@pytest.fixture
def wired(tmp_path, monkeypatch, capsys):
    """Fresh two-tier wiring against tmp_path; capsys bound before handlers."""
    monkeypatch.setattr("automatiq.core.config.LOGS_DIR", tmp_path)
    monkeypatch.setattr(logging_setup, "_STDERR_WIRED", False)
    monkeypatch.setattr(logging_setup, "_SESSION_FILE_WIRED", False)
    monkeypatch.setattr(logging_setup, "_EVENTS_WIRED", False)
    monkeypatch.setattr(status_log, "_STATUS_LOG_WIRED", False)
    status_log._STATUS_LOG.clear()

    root = logging.getLogger()
    root_level_before = root.level
    logging_setup._configure_logging()
    logging_setup._wire_event_logging()
    status_log.connect_status_log()
    yield tmp_path

    # Teardown: drop receivers, close+clear the session logger, empty the ring,
    # remove the stderr handler added here and restore the root level.
    for name, receiver in list(logging_setup._EVENT_RECEIVERS.items()):
        try:
            getattr(ev, name).disconnect(receiver)
        except Exception:
            pass
    for signal, receiver in list(status_log._STATUS_LOG_RECEIVERS):
        try:
            signal.disconnect(receiver)
        except Exception:
            pass
    session_logger = logging.getLogger(_SESSION_LOGGER_NAME)
    for handler in list(session_logger.handlers):
        try:
            handler.close()
        finally:
            session_logger.removeHandler(handler)
    bridge_logger = logging.getLogger(logging_setup._BRIDGE_LOGGER_NAME)
    for handler in list(bridge_logger.handlers):
        try:
            handler.close()
        finally:
            bridge_logger.removeHandler(handler)
    for handler in [h for h in root.handlers if isinstance(h, logging_setup._StderrStreamHandler)]:
        root.removeHandler(handler)
    root.setLevel(root_level_before)
    status_log._STATUS_LOG.clear()


def test_file_tier_verbose_and_console_minimized(wired, capsys):
    ev.log_info.send("recorder", text="tier-info-text")
    ev.log_debug.send("recorder", text="tier-debug-text")
    try:
        raise ValueError("tier-traceback-boom")
    except ValueError:
        ev.log_traceback.send("recorder")
    ev.log_warn.send("recorder", text="tier-warn-text")

    logs = sorted(wired.glob("session_*.log"))
    assert len(logs) == 1, f"expected exactly one session file, got {logs}"
    content = logs[0].read_text(encoding="utf-8")
    assert "tier-info-text" in content
    assert "tier-debug-text" in content  # DEBUG lands in the file
    assert "tier-warn-text" in content
    assert "Traceback (most recent call last)" in content  # exc_info rendered
    assert "ValueError: tier-traceback-boom" in content

    err = capsys.readouterr().err
    assert "tier-info-text" not in err  # INFO is file-only
    assert "tier-debug-text" not in err  # DEBUG is file-only
    assert "tier-warn-text" in err  # WARN reaches stderr too
    assert "Traceback (most recent call last)" not in err  # traceback is file-only


def test_configure_logging_idempotent(wired):
    logging_setup._configure_logging()  # repeat call must not stack handlers
    session_logger = logging.getLogger(_SESSION_LOGGER_NAME)
    file_handlers = [h for h in session_logger.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    assert session_logger.propagate is False
    assert session_logger.level == logging.DEBUG


def test_wire_event_logging_idempotent(wired):
    logging_setup._wire_event_logging()  # repeat call must not reconnect
    ev.log_info.send("recorder", text="idempotent-once-text")
    logs = list(wired.glob("session_*.log"))
    assert len(logs) == 1
    assert logs[0].read_text(encoding="utf-8").count("idempotent-once-text") == 1


def test_warn_event_written_once_to_session_file(wired, capsys):
    """Pass-3 regression: a warn event hits the session file exactly ONCE
    (the bridge propagation must not duplicate the direct write)."""
    ev.log_warn.send("recorder", text="dedup-warn-text")

    logs = list(wired.glob("session_*.log"))
    assert len(logs) == 1
    content = logs[0].read_text(encoding="utf-8")
    assert content.count("dedup-warn-text") == 1  # file: exactly one line
    # ...and stderr still sees it exactly once (via root, not the file handler).
    assert capsys.readouterr().err.count("dedup-warn-text") == 1


def _pin_console_gates_to_info() -> None:
    """Pin root + stderr handler to INFO regardless of ambient AUTOMATIQ_LOG_LEVEL."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in root.handlers:
        if isinstance(h, logging_setup._StderrStreamHandler):
            h.setLevel(logging.INFO)


def test_bridge_stdlib_warning_reaches_session_file(wired, capsys):
    """Fix A: stdlib logger calls from OUR modules land in the session file."""
    _pin_console_gates_to_info()
    logging.getLogger("automatiq.core.bin_manager").warning("bridge-check")

    logs = list(wired.glob("session_*.log"))
    assert len(logs) == 1
    content = logs[0].read_text(encoding="utf-8")
    assert "bridge-check" in content
    # The bridge record still reaches stderr (WARN >= LOG_LEVEL).
    assert "bridge-check" in capsys.readouterr().err


def test_bridge_automatiq_debug_file_not_stderr(wired, capsys):
    """automatiq DEBUG reaches the file; the stderr handler filters it at INFO."""
    _pin_console_gates_to_info()
    logging.getLogger("automatiq.core.bridge-test").debug("automatiq-debug-check")

    logs = list(wired.glob("session_*.log"))
    content = logs[0].read_text(encoding="utf-8")
    assert "automatiq-debug-check" in content  # file records everything DEBUG+
    assert "automatiq-debug-check" not in capsys.readouterr().err  # console unchanged


def test_third_party_debug_reaches_neither(wired, capsys):
    """Third-party loggers stay gated by root's level: no file, no stderr."""
    _pin_console_gates_to_info()
    logging.getLogger("litellm").debug("third-party-debug-check")

    # Nothing was logged at all -> with delay=True no session file even exists.
    assert list(wired.glob("session_*.log")) == []
    assert "third-party-debug-check" not in capsys.readouterr().err


def test_receiver_exceptions_swallowed(wired, monkeypatch):
    session_logger = logging.getLogger(_SESSION_LOGGER_NAME)

    def explode(*args, **kwargs):
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(session_logger, "info", explode)
    ev.log_info.send("recorder", text="sink-robustness-text")  # must not raise


def test_recent_log_in_single_session_status(wired):
    ev.log_info.send("recorder", text="status-info-text")
    ev.log_warn.send("recorder", text="status-warn-text")
    ev.log_debug.send("recorder", text="status-debug-text")  # DEBUG never surfaces
    session = RecordingSession(
        url="about:blank",
        session_name="t",
        proxy=None,
        include_video=False,
        output_root=str(wired / "rec"),
    )
    entries = session.status()["recent_log"]
    texts = [e["text"] for e in entries]
    assert "status-info-text" in texts
    assert "status-warn-text" in texts
    assert "status-debug-text" not in texts
    assert all(set(e) == {"t", "level", "text"} for e in entries)
    assert all(e["level"] in ("info", "warn", "error") for e in entries)

    for i in range(20):
        ev.log_info.send("recorder", text=f"flood-{i}")
    flooded = session.status()["recent_log"]
    assert len(flooded) == 15  # capped at the last 15
    assert flooded[-1]["text"] == "flood-19"  # newest last
    assert flooded[-1]["level"] == "info"


def test_list_mode_excludes_recent_log(monkeypatch, wired):
    ev.log_info.send("recorder", text="list-mode-info-text")
    monkeypatch.setattr(RecordingSession, "start", lambda self: None)
    registry = SessionRegistry(output_root=str(wired / "rec"))
    registry.create(url="about:blank", session_name="lm")

    rows = registry.list_statuses()
    assert rows, "expected at least one session row"
    assert all("recent_log" not in row for row in rows)

    # ...while the single-session view of the same session DOES carry it.
    single = registry.get(rows[0]["session_id"]).status()
    assert any(e["text"] == "list-mode-info-text" for e in single["recent_log"])
