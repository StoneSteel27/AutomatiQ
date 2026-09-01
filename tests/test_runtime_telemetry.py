"""Runtime telemetry wiring: the shared client must be started exactly once."""

import threading

import pytest

from automatiq.core import telemetry
from automatiq.mcp import runtime


@pytest.fixture()
def _reset_guard():
    """Reset the module-level start guard around each test."""
    saved = (runtime._telemetry_started_once, runtime._telemetry_start_lock)
    runtime._telemetry_started_once = False
    runtime._telemetry_start_lock = threading.Lock()
    yield
    runtime._telemetry_started_once, runtime._telemetry_start_lock = saved


class _FakeClient:
    def __init__(self) -> None:
        self.start_calls: list[str] = []

    def start(self, command: str = "unknown") -> None:
        self.start_calls.append(command)


def test_ensure_telemetry_started_runs_exactly_once(_reset_guard, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(telemetry, "client", fake)

    runtime._ensure_telemetry_started()
    runtime._ensure_telemetry_started()
    runtime._ensure_telemetry_started()

    assert fake.start_calls == ["record"]


def test_recording_session_start_triggers_telemetry_once(_reset_guard, monkeypatch, tmp_output_root):
    """_telemetry_started() must start the client before tracking, once per process."""
    fake = _FakeClient()
    monkeypatch.setattr(telemetry, "client", fake)
    tracked: list[tuple[str, dict]] = []

    class _Props:
        def __init__(self, **kwargs) -> None:
            self.data = kwargs

        def model_dump(self) -> dict:
            return dict(self.data)

    monkeypatch.setattr(telemetry, "RecordingStartedProps", _Props)
    fake.track_recording_started = lambda props: tracked.append(("recording_started", props.model_dump()))
    monkeypatch.setattr(runtime.config, "BLOCKLIST_SOURCES", [])

    session = runtime.RecordingSession(url="https://example.com", output_root=tmp_output_root)
    session._telemetry_started(proxy_used=None, browser_resolution=("browser", None, "brave"))
    session._telemetry_started(proxy_used=None, browser_resolution=("browser", None, "brave"))

    # Started exactly once (guard), and tracking happens after start.
    assert fake.start_calls == ["record"]
    assert len(tracked) == 2


def test_ensure_telemetry_started_fails_open(_reset_guard, monkeypatch):
    """An exploding client.start() must never raise out of the runtime."""

    class _Broken:
        def start(self, command: str = "unknown") -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(telemetry, "client", _Broken())

    runtime._ensure_telemetry_started()  # must not raise
