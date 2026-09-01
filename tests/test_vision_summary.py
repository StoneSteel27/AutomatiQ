"""Terminal vision summary: status() vision block, README-adjacent state,
analyzer counters, and the start_recording vision block shape.

Everything runs without network and without heavy imports: the analyzer is
imported for real with litellm / litellm.exceptions / imageio_ffmpeg stubbed
in sys.modules (the test_workspace_readme pattern), and the completion call
is faked on the stub module the loaded module actually holds.
"""

import asyncio
import importlib
import json
import os
import sys
import types
from unittest.mock import MagicMock

import pytest
from fastmcp import Client

import automatiq.core.config as config
import automatiq.mcp.runtime as runtime
import automatiq.mcp.server as server
from automatiq.core import events
from automatiq.mcp.runtime import RecordingSession
from automatiq.mcp.vision import (
    _PROVIDER_KEY_ENV,
    _vision_summary_block,
    vision_preflight,
)

_GEMINI_MODEL = "gemini/gemini-3.1-flash-lite"
_SECRET_KEY = "sk-SUPER-SECRET-VALUE"


class _RecordingSignal:
    """Stands in for a blinker signal, recording emitted texts."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def send(self, sender, **kw) -> None:
        self.texts.append(str(kw.get("text", "")))


_SKIPPED = {"state": "skipped", "detail": "no key - set recorder_api_key in ~/.automatiq/config.toml"}
_SKIPPED_VIDEO_DISABLED = {"state": "skipped", "detail": "video disabled (include_video=false)"}
_ENABLED = {"state": "enabled", "model": _GEMINI_MODEL, "analyzed": 4, "failed": 0}
_ENABLED_OVERRIDE = {"state": "enabled", "model": "openai/gpt-4o-mini", "analyzed": 2, "failed": 0}
_AUTH = {
    "state": "failed",
    "reason": "key rejected - check recorder_api_key in ~/.automatiq/config.toml",
    "analyzed": 2,
    "failed": 3,
}
_ABORT = {
    "state": "failed",
    "reason": "vision analysis aborted after first failure (see session log)",
    "analyzed": 1,
    "failed": 2,
}

_HEAVY_MODULES = ("litellm", "imageio_ffmpeg", "zendriver", "mss", "magika")


@pytest.fixture
def scrub_provider_env():
    """Restore every provider key env var after config-file plumbing tests."""
    before = {var: os.environ.get(var) for var in _PROVIDER_KEY_ENV.values()}
    yield
    for var, old in before.items():
        if os.environ.get(var) != old:
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old


@pytest.fixture
def ai_analyzer_mod():
    """The real ai_analyzer module, loaded with heavy deps stubbed (no leak)."""
    name = "automatiq.core.recorder.ai_analyzer"
    if name in sys.modules:
        yield sys.modules[name]
        return

    litellm_stub = types.ModuleType("litellm")

    class APIConnectionError(Exception):
        pass

    class NotFoundError(Exception):
        pass

    class AuthenticationError(Exception):
        pass

    class PermissionDeniedError(Exception):
        pass

    litellm_stub.APIConnectionError = APIConnectionError
    litellm_stub.NotFoundError = NotFoundError
    litellm_stub.AuthenticationError = AuthenticationError
    litellm_stub.PermissionDeniedError = PermissionDeniedError
    litellm_stub.completion = MagicMock()

    exceptions_stub = types.ModuleType("litellm.exceptions")

    class InternalServerError(Exception):
        pass

    exceptions_stub.InternalServerError = InternalServerError

    imageio_stub = types.ModuleType("imageio_ffmpeg")
    imageio_stub.get_ffmpeg_exe = MagicMock()

    installed = {"litellm": litellm_stub, "litellm.exceptions": exceptions_stub, "imageio_ffmpeg": imageio_stub}
    saved = {key: sys.modules.get(key) for key in installed}
    sys.modules.update(installed)
    try:
        importlib.import_module(name)
    finally:
        for key, previous in saved.items():
            if previous is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = previous
    mod = sys.modules[name]
    yield mod
    leaked = [key for key in _HEAVY_MODULES if key in sys.modules]
    assert leaked == [], f"heavy modules leaked into sys.modules: {leaked}"


def test_vision_block_four_states_verbatim(monkeypatch):
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", _GEMINI_MODEL)
    assert _vision_summary_block({"configured": False}) == _SKIPPED
    assert _vision_summary_block({"configured": True, "analyzed": 4, "failed": 0}) == _ENABLED
    assert _vision_summary_block({"configured": True, "analyzed": 2, "failed": 3, "fatal_reason": "auth"}) == _AUTH
    assert _vision_summary_block({"configured": True, "analyzed": 1, "failed": 2, "fatal_reason": "other"}) == _ABORT


def test_vision_block_video_disabled_and_override_model(monkeypatch):
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", _GEMINI_MODEL)
    # include_video=false session: runtime seeds the video-disabled skip reason.
    assert _vision_summary_block({"configured": False, "skip_reason": "video_disabled"}) == _SKIPPED_VIDEO_DISABLED
    # The resolved model (threaded through vision_state) is what the summary names.
    assert (
        _vision_summary_block({"configured": True, "model": "openai/gpt-4o-mini", "analyzed": 2, "failed": 0})
        == _ENABLED_OVERRIDE
    )


def test_terminal_status_surfaces_vision_block(tmp_output_root):
    session = RecordingSession(
        url="about:blank", session_name="v", proxy=None, include_video=True, output_root=tmp_output_root
    )
    assert "vision" not in session.status()  # nothing compiled yet -> absent
    session._vision_summary = _vision_summary_block({"configured": False})
    session._set_state("completed")
    snap = session.status()
    assert snap["vision"] == _SKIPPED
    assert snap["state"] == "completed"


def test_preflight_resolved_once_per_session(monkeypatch, tmp_path, tmp_output_root):
    """Regression: repeated status() polls must not re-run the side-effectful resolution."""
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", _GEMINI_MODEL)
    monkeypatch.setattr(config, "API_BASE", None)
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    config_file.write_text('[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\n', encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    real = runtime.vision_preflight
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(runtime, "vision_preflight", counting)
    session = RecordingSession(
        url="about:blank", session_name="pf-once", proxy=None, include_video=True, output_root=tmp_output_root
    )
    for _ in range(3):
        assert session.status()["vision_configured"] is False
    assert calls["n"] == 1


def test_config_key_snapshot_survives_own_plumbing(monkeypatch, tmp_path, tmp_output_root, scrub_provider_env):
    """The cached snapshot must not be shadowed by its own config-file plumbing."""
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", _GEMINI_MODEL)
    monkeypatch.setattr(config, "API_BASE", None)
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    config_file.write_text(
        '[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\nrecorder_api_key = "k-test"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    session = RecordingSession(
        url="about:blank", session_name="pf-same", proxy=None, include_video=True, output_root=tmp_output_root
    )
    assert session.status()["vision_configured"] is True
    assert session.status()["vision_configured"] is True
    snap = session._vision_preflight_snapshot()
    assert snap["model"] == _GEMINI_MODEL
    assert snap["source"] == "config"  # plumbing must not shadow the cached snapshot
    assert os.environ["GEMINI_API_KEY"] == "k-test"  # plumbed exactly once


def test_api_key_never_appears_in_status(monkeypatch, tmp_path, tmp_output_root, scrub_provider_env):
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", _GEMINI_MODEL)
    monkeypatch.setattr(config, "API_BASE", None)
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    config_file.write_text(
        f'[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\nrecorder_api_key = "{_SECRET_KEY}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    preflight = vision_preflight()
    assert preflight["source"] == "config"
    assert _SECRET_KEY not in json.dumps(preflight)

    session = RecordingSession(
        url="about:blank", session_name="v2", proxy=None, include_video=True, output_root=tmp_output_root
    )
    session._vision_summary = _vision_summary_block({"configured": True, "analyzed": 1, "failed": 0})
    assert _SECRET_KEY not in json.dumps(session.status())


class _FakeSession:
    def __init__(self) -> None:
        self.id = "fake-vision-1"
        self.state = "created"
        self.is_terminal = False

    def status(self) -> dict:
        return {
            "session_id": self.id,
            "session_name": "fake-vision",
            "state": self.state,
            "output_root": "/tmp/fake-vision",
            "include_video": True,
        }


class _FakeRegistry:
    output_root = "/tmp/fake-vision"

    def create(self, url, session_name=None, proxy=None, include_video=True, vision_preflight_result=None):
        return _FakeSession()

    def stop_all(self, join_timeout: float = 20.0) -> None:
        pass


def _start_recording_payload() -> dict:
    async def run():
        async with Client(server.app) as client:
            res = await client.call_tool(
                "start_recording",
                {"url": "https://example.com", "session_name": "vshape", "include_video": True},
            )
            assert res.is_error is False
            return res.structured_content

    return asyncio.run(run())


def test_start_recording_vision_block_drops_warning_when_configured(monkeypatch, tmp_path, scrub_provider_env):
    monkeypatch.setattr(server, "_REGISTRY", _FakeRegistry())
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", _GEMINI_MODEL)
    monkeypatch.setattr(config, "API_BASE", None)
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    config_file.write_text(
        '[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\nrecorder_api_key = "cfg-key-shape"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    payload = _start_recording_payload()
    assert payload["vision"] == {"model": _GEMINI_MODEL, "configured": True, "source": "config"}
    assert "warning" not in payload["vision"]


def test_start_recording_vision_block_warns_when_unconfigured(monkeypatch, tmp_path, scrub_provider_env):
    monkeypatch.setattr(server, "_REGISTRY", _FakeRegistry())
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", _GEMINI_MODEL)
    monkeypatch.setattr(config, "API_BASE", None)
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    config_file.write_text('[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\n', encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    payload = _start_recording_payload()
    vision = payload["vision"]
    assert vision["configured"] is False
    assert vision["warning"] == (
        "Vision annotation is OFF - clips will not be AI-analyzed. Enable: paste your "
        "key into recorder_api_key under [models] in ~/.automatiq/config.toml - takes "
        "effect on the next recording, no restart needed."
    )


def test_analyzer_counts_successes(monkeypatch, ai_analyzer_mod):
    analyzer = ai_analyzer_mod.VideoActionAnalyzer()
    monkeypatch.setattr(analyzer, "_get_base64_frames", lambda *a, **k: ["data:image/jpeg;base64,AAA"])
    good = json.dumps({"macro_summary": "clicked the login button", "elements_interacted": [], "action_success": True})
    fake_response = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=good))])
    monkeypatch.setattr(ai_analyzer_mod.litellm, "completion", lambda **kwargs: fake_response)

    analyzer.analyze_clip("clip.mp4", 1.0)
    analyzer.analyze_clip("clip.mp4", 1.0)
    assert analyzer.clips_analyzed == 2
    assert analyzer.clips_failed == 0
    assert analyzer.fatal_reason is None


def test_analyzer_uses_override_model(monkeypatch, ai_analyzer_mod):
    # The resolved model (threaded via vision_state) must reach the litellm call.
    analyzer = ai_analyzer_mod.VideoActionAnalyzer(model="openai/gpt-4o-mini")
    assert analyzer.model == "openai/gpt-4o-mini"
    monkeypatch.setattr(analyzer, "_get_base64_frames", lambda *a, **k: ["data:image/jpeg;base64,AAA"])
    good = json.dumps({"macro_summary": "typed the query", "elements_interacted": [], "action_success": True})
    fake_response = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=good))])
    calls: list[dict] = []

    def record(**kwargs):
        calls.append(kwargs)
        return fake_response

    monkeypatch.setattr(ai_analyzer_mod.litellm, "completion", record)
    analyzer.analyze_clip("clip.mp4", 1.0)
    assert calls and calls[0]["model"] == "openai/gpt-4o-mini"
    # Default construction still reads the config model.
    assert ai_analyzer_mod.VideoActionAnalyzer().model == config.RECORDER_AI_MODEL


def test_analyzer_fatal_auth_trips_breaker_and_counts(monkeypatch, ai_analyzer_mod):
    analyzer = ai_analyzer_mod.VideoActionAnalyzer()
    monkeypatch.setattr(analyzer, "_get_base64_frames", lambda *a, **k: ["data:image/jpeg;base64,AAA"])
    calls: list[dict] = []

    def boom(**kwargs):
        calls.append(kwargs)
        raise ai_analyzer_mod.litellm.AuthenticationError("invalid api key")

    monkeypatch.setattr(ai_analyzer_mod.litellm, "completion", boom)
    resp = analyzer.analyze_clip("clip.mp4", 1.0)
    assert resp["macro_summary"] == "Error: Could not analyze clip."
    assert analyzer._ai_disabled is True
    assert analyzer.fatal_reason == "auth"
    assert analyzer.clips_analyzed == 0
    assert analyzer.clips_failed == 1
    # Auth exceptions raise on attempt 1 - no retry, no warn spam.
    assert len(calls) == 1

    analyzer.analyze_clip("clip.mp4", 1.0)  # post-trip clip: skipped, still counted failed
    assert analyzer.clips_failed == 2
    assert len(calls) == 1  # and no further LLM calls after the breaker tripped


def test_analyzer_request_failure_retries_honestly(monkeypatch, ai_analyzer_mod):
    """Non-auth request failures retry with the honest text and never append
    an empty assistant message."""
    analyzer = ai_analyzer_mod.VideoActionAnalyzer()
    monkeypatch.setattr(analyzer, "_get_base64_frames", lambda *a, **k: ["data:image/jpeg;base64,AAA"])
    calls: list[dict] = []
    warn_signal = _RecordingSignal()
    monkeypatch.setattr(events, "log_warn", warn_signal)

    def flaky(**kwargs):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        raise RuntimeError("connection reset mid-flight")

    monkeypatch.setattr(ai_analyzer_mod.litellm, "completion", flaky)
    resp = analyzer.analyze_clip("clip.mp4", 1.0)

    assert resp["macro_summary"] == "Error: Could not analyze clip."
    assert len(calls) == 3  # 3 attempts, then the final raise
    retry_warns = [t for t in warn_signal.texts if "AI request failed" in t]
    assert len(retry_warns) == 2  # attempts 1 and 2 warn; attempt 3 raises
    assert "attempt 1/3" in retry_warns[0] and "attempt 2/3" in retry_warns[1]
    assert "connection reset mid-flight" in retry_warns[0]
    # No assistant/user correction messages were appended for request failures.
    for call in calls:
        roles = [m["role"] for m in call["messages"]]
        assert roles == ["user"]
    assert analyzer.fatal_reason is None  # RuntimeError is not fatal
    assert analyzer.clips_failed == 1


def test_analyzer_validation_failure_keeps_retry_and_append(monkeypatch, ai_analyzer_mod):
    """Validation failures keep the existing warn text and append the raw text."""
    analyzer = ai_analyzer_mod.VideoActionAnalyzer()
    monkeypatch.setattr(analyzer, "_get_base64_frames", lambda *a, **k: ["data:image/jpeg;base64,AAA"])
    warn_signal = _RecordingSignal()
    monkeypatch.setattr(events, "log_warn", warn_signal)

    bad_text = "not json at all"
    good = json.dumps({"macro_summary": "clicked login", "elements_interacted": [], "action_success": True})
    responses = iter([bad_text, bad_text, good])
    calls: list[dict] = []

    def scripted(**kwargs):
        # Snapshot the message list: kwargs["messages"] is mutated in place
        # across retries, so calls must capture a point-in-time copy.
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=next(responses)))]
        )

    monkeypatch.setattr(ai_analyzer_mod.litellm, "completion", scripted)
    resp = analyzer.analyze_clip("clip.mp4", 1.0)

    assert resp["macro_summary"] == "clicked login"
    assert analyzer.clips_analyzed == 1
    assert analyzer.clips_failed == 0
    validation_warns = [t for t in warn_signal.texts if "AI response validation failed" in t]
    assert len(validation_warns) == 2
    assert "Attempt 1/3" in validation_warns[0] and "Attempt 2/3" in validation_warns[1]
    # The correction pair was appended, with the REAL raw text (never empty).
    assert len(calls[1]["messages"]) == 3
    assert calls[1]["messages"][1] == {"role": "assistant", "content": bad_text}
    assert "Failed validation" in calls[1]["messages"][2]["content"]
    assert len(calls[2]["messages"]) == 5


def test_analyzer_fatal_other_classification(monkeypatch, ai_analyzer_mod):
    analyzer = ai_analyzer_mod.VideoActionAnalyzer()
    monkeypatch.setattr(analyzer, "_get_base64_frames", lambda *a, **k: ["data:image/jpeg;base64,AAA"])
    monkeypatch.setattr(
        ai_analyzer_mod.litellm,
        "completion",
        lambda **kwargs: (_ for _ in ()).throw(ai_analyzer_mod.litellm.APIConnectionError("network down")),
    )
    analyzer.analyze_clip("clip.mp4", 1.0)
    assert analyzer.fatal_reason == "other"
    assert analyzer.clips_failed == 1
    assert analyzer.clips_analyzed == 0


def test_vision_state_flows_from_compile_contract(monkeypatch):
    """The vision_state dict contract: runtime seeds 'configured', compile fills counts."""
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", _GEMINI_MODEL)
    state = {"configured": True}
    # Simulate merge_and_annotate_actions' _sync_vision_state fill.
    state.update({"analyzed": 5, "failed": 1, "fatal_reason": None})
    block = _vision_summary_block(state)
    assert block == {"state": "enabled", "model": _GEMINI_MODEL, "analyzed": 5, "failed": 1}


@pytest.fixture
def actions_mod(ai_analyzer_mod, monkeypatch):
    """The real compile.actions module with ActionVideoRecorder stubbed out."""
    recorder_stub = types.ModuleType("automatiq.core.recorder.video_recorder")

    class FakeActionVideoRecorder:
        def __init__(self, fps=None):
            self.fps = fps

        def split_video(self, full_video_path, clip_path, clip_start, clip_end):
            return True

    recorder_stub.ActionVideoRecorder = FakeActionVideoRecorder
    monkeypatch.setitem(sys.modules, "automatiq.core.recorder.video_recorder", recorder_stub)
    name = "automatiq.core.recorder.compile.actions"
    if name not in sys.modules:
        importlib.import_module(name)
    return sys.modules[name]


def test_merge_threads_resolved_model_to_the_analyzer(monkeypatch, actions_mod, ai_analyzer_mod, tmp_path):
    # The model resolved at session start (threaded via vision_state) must be
    # the one the compile pipeline calls.
    video_file = tmp_path / "full.mp4"
    video_file.write_bytes(b"not-a-real-video-but-split-is-stubbed")
    vision_state = {"configured": True, "model": "openai/gpt-4o-mini"}

    good = json.dumps({"macro_summary": "clicked login", "elements_interacted": [], "action_success": True})
    fake_response = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=good))])
    calls: list[dict] = []

    def record(**kwargs):
        calls.append(kwargs)
        return fake_response

    monkeypatch.setattr(ai_analyzer_mod.litellm, "completion", record)
    # Frame extraction would shell out to ffmpeg; stub it at class level so the
    # fake completion is reached through the real merge_and_annotate_actions.
    monkeypatch.setattr(
        ai_analyzer_mod.VideoActionAnalyzer,
        "_get_base64_frames",
        lambda self, *a, **k: ["data:image/jpeg;base64,AAA"],
    )

    actions_mod.merge_and_annotate_actions(
        [{"timestamp_unix": 2.0, "type": "click"}],
        str(video_file),
        1.0,
        str(tmp_path / "clips"),
        vision_state=vision_state,
    )

    assert calls and calls[0]["model"] == "openai/gpt-4o-mini"
    assert vision_state["analyzed"] == 1
    assert vision_state["failed"] == 0
    assert vision_state["fatal_reason"] is None
