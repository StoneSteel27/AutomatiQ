"""Telemetry v2 (MCP-native event model) unit tests.

Covers the three new events without any heavy imports:

- tool_called: emitted by the FastMCP middleware for every tool invocation,
  including the exception path (error_class + re-raise) - the client is
  patched, never the real dispatcher.
- server_started: emitted exactly once at lifespan startup with booleans and
  the recorder model string only.
- annotate_run: fired when the background annotation JOB finishes (success
  and failure paths), not when the tool call returns.

The ai_analyzer lazy import inside AnnotationJob._run is stubbed via
sys.modules so litellm/imageio_ffmpeg never load.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from fastmcp import Client

from automatiq.core import telemetry
from automatiq.core.telemetry import ServerStartedProps, TelemetryClient
from automatiq.mcp import annotation, server


class _FakeClient:
    """Records every v2 track_* call the product code makes."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.start_calls: list[str] = []

    def start(self, command: str = "unknown") -> None:
        self.start_calls.append(command)

    def track_server_started(self, props: ServerStartedProps) -> None:
        self.events.append(("server_started", props.model_dump()))

    def track_tool_called(self, tool: str, duration_ms: int, ok: bool, error_class: str | None = None) -> None:
        self.events.append(
            ("tool_called", {"tool": tool, "duration_ms": duration_ms, "ok": ok, "error_class": error_class})
        )

    def track_annotate_run(self, duration_ms: int, ok: bool) -> None:
        self.events.append(("annotate_run", {"duration_ms": duration_ms, "ok": ok}))


@pytest.fixture()
def fake_client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(telemetry, "client", fake)
    return fake


# -- Client track_* methods ----------------------------------------------------


def test_track_v2_methods_queue_validated_payloads():
    c = TelemetryClient()
    c._enabled = True  # bypass config gating; nothing is dispatched
    c.track_server_started(
        ServerStartedProps(model="test/model", video_default=True, proxy_configured=False, vision_key_present=False)
    )
    c.track_tool_called("get_status", 12, True)
    c.track_tool_called("start_recording", 20, False, error_class="ValueError")
    c.track_annotate_run(1500, False)

    items = [c._queue.get_nowait() for _ in range(4)]
    assert [i["event"] for i in items] == ["server_started", "tool_called", "tool_called", "annotate_run"]
    assert items[0]["properties"] == {
        "model": "test/model",
        "video_default": True,
        "proxy_configured": False,
        "vision_key_present": False,
    }
    assert items[1]["properties"] == {"tool": "get_status", "duration_ms": 12, "ok": True, "error_class": None}
    assert items[2]["properties"] == {
        "tool": "start_recording",
        "duration_ms": 20,
        "ok": False,
        "error_class": "ValueError",
    }
    assert items[3]["properties"] == {"duration_ms": 1500, "ok": False}
    assert items[1]["run_id"] == items[0]["run_id"]


# -- tool_called middleware ----------------------------------------------------


class _ToolCallMsg:
    """Stand-in for mcp.types.CallToolRequestParams (only .name is read)."""

    def __init__(self, name: str) -> None:
        self.name = name


class _Ctx:
    def __init__(self, name: str) -> None:
        self.message = _ToolCallMsg(name)


def test_middleware_emits_tool_called_on_success(fake_client):
    mw = server._ToolTelemetryMiddleware()

    async def run():
        async def call_next(ctx):
            return "result"

        result = await mw.on_call_tool(_Ctx("get_status"), call_next)
        assert result == "result"

    asyncio.run(run())

    calls = [e for e in fake_client.events if e[0] == "tool_called"]
    assert len(calls) == 1
    payload = calls[0][1]
    assert payload["tool"] == "get_status"
    assert payload["ok"] is True
    assert payload["error_class"] is None
    assert isinstance(payload["duration_ms"], int)
    assert payload["duration_ms"] >= 0


def test_middleware_emits_tool_called_on_error_and_reraises(fake_client):
    mw = server._ToolTelemetryMiddleware()

    async def run():
        async def boom(ctx):
            raise RuntimeError("kaboom")

        with pytest.raises(RuntimeError, match="kaboom"):
            await mw.on_call_tool(_Ctx("start_recording"), boom)

    asyncio.run(run())

    calls = [e for e in fake_client.events if e[0] == "tool_called"]
    assert len(calls) == 1
    payload = calls[0][1]
    assert payload["tool"] == "start_recording"
    assert payload["ok"] is False
    assert payload["error_class"] == "RuntimeError"
    assert isinstance(payload["duration_ms"], int)


def test_middleware_telemetry_fails_open(monkeypatch):
    class _Broken:
        def track_tool_called(self, **kwargs):
            raise RuntimeError("telemetry exploded")

    monkeypatch.setattr(telemetry, "client", _Broken())
    mw = server._ToolTelemetryMiddleware()

    async def run():
        async def call_next(ctx):
            return "result"

        # Neither the emission nor the tool result may raise.
        return await mw.on_call_tool(_Ctx("get_status"), call_next)

    assert asyncio.run(run()) == "result"


def test_tool_called_through_real_client_session(fake_client):
    """End-to-end over the in-memory MCP client: every tools/call is counted."""

    async def run():
        async with Client(server.app) as client:
            res = await client.call_tool("get_status", {})
            assert res.is_error is False
        return None

    asyncio.run(run())

    calls = [e for e in fake_client.events if e[0] == "tool_called"]
    assert len(calls) == 1
    assert calls[0][1]["tool"] == "get_status"
    assert calls[0][1]["ok"] is True


# -- server_started at lifespan startup ----------------------------------------


def test_server_started_emitted_once_at_lifespan(fake_client, monkeypatch):
    from automatiq.core import config
    from automatiq.mcp import runtime

    monkeypatch.setattr(server, "_server_started_emitted", False)
    monkeypatch.setattr(runtime, "_telemetry_started_once", False)
    monkeypatch.setattr(server, "vision_preflight", lambda: {"configured": True, "model": "test/model"})
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "test/model")
    monkeypatch.setattr(config, "RECORDER_PROXY_ENABLED", False)
    monkeypatch.setattr(config, "RECORDER_PROXY_SERVER", None)
    monkeypatch.setattr(config, "RECORDER_PROXY_PROVIDER", None)

    async def run():
        async with Client(server.app):
            pass
        # A second lifespan startup (e.g. an in-process reconnect) must not
        # re-emit: server_started is once per process.
        async with Client(server.app):
            pass

    asyncio.run(run())

    started = [e for e in fake_client.events if e[0] == "server_started"]
    assert len(started) == 1
    assert started[0][1] == {
        "model": "test/model",
        "video_default": True,
        "proxy_configured": False,
        "vision_key_present": True,
    }
    # The client was started (guarded, so possibly once across the process).
    assert fake_client.start_calls == ["record"]


def test_server_started_proxy_and_vision_flags(fake_client, monkeypatch):
    from automatiq.core import config

    monkeypatch.setattr(server, "_server_started_emitted", False)
    monkeypatch.setattr(server, "vision_preflight", lambda: {"configured": False, "warning": "no key"})
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "test/model")
    monkeypatch.setattr(config, "RECORDER_PROXY_ENABLED", True)

    async def run():
        async with Client(server.app):
            pass

    asyncio.run(run())

    started = [e for e in fake_client.events if e[0] == "server_started"]
    assert len(started) == 1
    assert started[0][1]["proxy_configured"] is True
    assert started[0][1]["vision_key_present"] is False


def test_server_started_fails_open(monkeypatch):
    class _Broken:
        def start(self, command: str = "unknown") -> None:
            raise RuntimeError("boom")

        def track_server_started(self, props) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(telemetry, "client", _Broken())
    monkeypatch.setattr(server, "_server_started_emitted", False)

    async def run():
        async with Client(server.app):
            pass

    # Lifespan startup must succeed despite the exploding telemetry client.
    asyncio.run(run())
    assert server._server_started_emitted is True


# -- annotate_run on background job completion ---------------------------------


@pytest.fixture()
def _stub_ai_analyzer(monkeypatch):
    """Stub the lazy heavy import inside AnnotationJob._run."""
    mod = types.ModuleType("automatiq.core.recorder.ai_analyzer")

    class _FakeAnalyzer:
        def __init__(self, model=None) -> None:
            self.model = model

    mod.VideoActionAnalyzer = _FakeAnalyzer
    monkeypatch.setitem(sys.modules, "automatiq.core.recorder.ai_analyzer", mod)


def test_annotate_run_emitted_on_job_success(fake_client, _stub_ai_analyzer, tmp_path):
    job = annotation.AnnotationJob(session_id="s-ok", session_dir=tmp_path)
    job._annotate = lambda analyzer_cls: None  # instant success stand-in
    job.state = "completed"

    job._run()  # run the worker body synchronously

    runs = [e for e in fake_client.events if e[0] == "annotate_run"]
    assert len(runs) == 1
    payload = runs[0][1]
    assert payload["ok"] is True
    assert isinstance(payload["duration_ms"], int)
    assert payload["duration_ms"] >= 0
    assert job.ended_at is not None


def test_annotate_run_emitted_on_job_failure(fake_client, _stub_ai_analyzer, tmp_path):
    job = annotation.AnnotationJob(session_id="s-bad", session_dir=tmp_path)

    def boom(analyzer_cls):
        raise RuntimeError("no clips")

    job._annotate = boom

    job._run()

    assert job.state == "failed"
    runs = [e for e in fake_client.events if e[0] == "annotate_run"]
    assert len(runs) == 1
    payload = runs[0][1]
    assert payload["ok"] is False
    assert isinstance(payload["duration_ms"], int)


def test_annotate_run_telemetry_fails_open(_stub_ai_analyzer, monkeypatch, tmp_path):
    class _Broken:
        def track_annotate_run(self, **kwargs):
            raise RuntimeError("telemetry exploded")

    monkeypatch.setattr(telemetry, "client", _Broken())

    job = annotation.AnnotationJob(session_id="s-broken", session_dir=tmp_path)
    job._annotate = lambda analyzer_cls: None
    job.state = "completed"

    job._run()  # must not raise

    assert job.state == "completed"
