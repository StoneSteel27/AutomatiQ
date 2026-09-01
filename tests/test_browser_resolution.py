"""Managed-browser resolution wiring in RecordingSession._run.

``resolve_browser_for_recording`` lives in browser_manager, which pulls only
stdlib + config + bin_manager (also stdlib + config) — so patching it on its
real module costs no heavy import. Every heavy collaborator _run() touches
around the resolution point — BrowserAgent (zendriver), ActionVideoRecorder
(mss/imageio_ffmpeg), compile_workspace (litellm/magika) — is reached through
lazy imports and is intercepted here with stub modules in sys.modules (the
test_workspace_readme pattern), so _run() executes to completion in-process
without loading any heavy dependency.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

import automatiq.core.browser_manager as browser_manager
import automatiq.mcp.runtime as runtime
from automatiq.mcp.runtime import STATE_COMPLETED, STATE_FAILED, RecordingSession

_HEAVY_MODULES = ("zendriver", "mss", "litellm", "magika", "imageio_ffmpeg")
_RESOLUTION = ("browser_executable_path", "X:/brave.exe", "Brave")


@pytest.fixture
def runtime_stubs(monkeypatch, tmp_path):
    """Swap _run()'s heavy lazy imports for recording fakes; yield the record."""
    record = types.SimpleNamespace(
        agent_instances=[],
        run_session_kwargs=[],
        recorder_instances=[],
        video_started=0,
        compile_calls=[],
    )
    temp_data_dir = str(tmp_path / "session-data")

    class FakeBrowserAgent:
        def __init__(self, blocklist=None, proxy=None):
            self.stats = {}
            self.blocklist = blocklist
            self.proxy = proxy
            record.agent_instances.append(self)

        async def run_session(self, url, stop_token=None, browser_resolution=None):
            record.run_session_kwargs.append(
                {"url": url, "stop_token": stop_token, "browser_resolution": browser_resolution}
            )
            return temp_data_dir

    class FakeActionVideoRecorder:
        def __init__(self, fps=None, output_path=None):
            record.recorder_instances.append(self)

        def start(self):
            record.video_started += 1

        def stop(self):
            return None

    def fake_compile_workspace(**kwargs):
        record.compile_calls.append(kwargs)
        out_dir = tmp_path / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        return None, str(out_dir), True

    agent_mod = types.ModuleType("automatiq.core.recorder.browser_agent")
    agent_mod.BrowserAgent = FakeBrowserAgent
    recorder_mod = types.ModuleType("automatiq.core.recorder.video_recorder")
    recorder_mod.ActionVideoRecorder = FakeActionVideoRecorder
    workspace_mod = types.ModuleType("automatiq.core.recorder.compile.workspace")
    workspace_mod.compile_workspace = fake_compile_workspace

    monkeypatch.setitem(sys.modules, "automatiq.core.recorder.browser_agent", agent_mod)
    monkeypatch.setitem(sys.modules, "automatiq.core.recorder.video_recorder", recorder_mod)
    monkeypatch.setitem(sys.modules, "automatiq.core.recorder.compile.workspace", workspace_mod)
    yield record

    leaked = [name for name in _HEAVY_MODULES if name in sys.modules]
    assert leaked == [], f"heavy modules leaked into sys.modules during _run(): {leaked}"


@pytest.fixture
def light_stubs(monkeypatch):
    """No-op _run()'s light collaborators around the resolution point."""
    # The STARTING log line calls blocklist.total_enabled_domains(), so the
    # stub must return a dummy object rather than None.
    monkeypatch.setattr(runtime, "_init_blocklist", lambda: MagicMock())
    monkeypatch.setattr(runtime, "_resolve_proxy", lambda proxy=None: None)
    monkeypatch.setattr(runtime, "_check_macos_screen_permission", lambda: None)
    # Telemetry helpers would POST to the real endpoint when enabled.
    monkeypatch.setattr(RecordingSession, "_telemetry_started", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(RecordingSession, "_telemetry_ended", lambda self, **kwargs: None)


def test_run_resolves_browser_and_passes_it_to_the_agent(monkeypatch, tmp_output_root, runtime_stubs, light_stubs):
    resolve_calls: list[dict] = []

    def fake_resolve(**kwargs):
        resolve_calls.append(kwargs)
        return _RESOLUTION

    monkeypatch.setattr(browser_manager, "resolve_browser_for_recording", fake_resolve)

    session = RecordingSession(
        url="about:blank",
        session_name="t",
        proxy=None,
        include_video=False,
        output_root=tmp_output_root,
    )
    # No resolution yet -> the status snapshot must not carry a browser field.
    assert "browser" not in session.status()

    session._run()  # in-process, no worker thread

    assert resolve_calls == [{"no_auto_download": False, "prompt_callback": None, "progress_callback": None}]
    assert len(runtime_stubs.agent_instances) == 1
    assert runtime_stubs.run_session_kwargs == [
        {
            "url": "about:blank",
            "stop_token": session.stop_token,
            "browser_resolution": _RESOLUTION,
        }
    ]
    # include_video=False -> the recorder is built but never started.
    assert runtime_stubs.video_started == 0
    assert len(runtime_stubs.compile_calls) == 1  # compile reached -> success
    assert session.state == STATE_COMPLETED
    assert session.is_terminal
    assert session.status()["browser"] == "Brave"


def test_run_fails_the_session_when_browser_resolution_raises(monkeypatch, tmp_output_root, runtime_stubs, light_stubs):
    def exploding_resolve(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(browser_manager, "resolve_browser_for_recording", exploding_resolve)

    session = RecordingSession(
        url="about:blank",
        session_name="t",
        proxy=None,
        include_video=False,
        output_root=tmp_output_root,
    )
    session._run()

    assert session.state == STATE_FAILED
    assert session.error is not None
    assert "browser setup failed" in session.error
    assert "boom" in session.error
    assert runtime_stubs.agent_instances == []  # agent never constructed ...
    assert runtime_stubs.run_session_kwargs == []  # ... nor run
    assert runtime_stubs.recorder_instances == []  # video recorder never built
    assert runtime_stubs.video_started == 0  # ... nor started
    assert runtime_stubs.compile_calls == []  # nothing to compile
    assert "browser" not in session.status()
