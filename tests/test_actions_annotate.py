"""merge_and_annotate_actions: the no-actions compile path must not be silent.

compile.actions is imported for real with its heavy dependencies stubbed in
sys.modules (ai_analyzer needs litellm/imageio_ffmpeg; the recorder needs a
no-op ActionVideoRecorder) - the test_workspace_readme pattern.
"""

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest

from automatiq.core import events

_NO_ACTIONS_TEXT = "No user actions captured - nothing to annotate with AI."


class _RecordingSignal:
    """Stands in for a blinker signal, recording emitted texts."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def send(self, sender, **kw) -> None:
        self.texts.append(str(kw.get("text", "")))


@pytest.fixture
def actions_mod(monkeypatch):
    """The real compile.actions module with heavy deps stubbed (no leak)."""
    name = "automatiq.core.recorder.compile.actions"
    installed: dict[str, types.ModuleType] = {}
    saved: dict[str, types.ModuleType | None] = {}
    if name not in sys.modules:
        litellm_stub = types.ModuleType("litellm")
        litellm_stub.APIConnectionError = type("APIConnectionError", (Exception,), {})
        litellm_stub.NotFoundError = type("NotFoundError", (Exception,), {})
        litellm_stub.AuthenticationError = type("AuthenticationError", (Exception,), {})
        litellm_stub.PermissionDeniedError = type("PermissionDeniedError", (Exception,), {})
        litellm_stub.completion = MagicMock()
        exceptions_stub = types.ModuleType("litellm.exceptions")
        exceptions_stub.InternalServerError = type("InternalServerError", (Exception,), {})
        imageio_stub = types.ModuleType("imageio_ffmpeg")
        imageio_stub.get_ffmpeg_exe = MagicMock()
        recorder_stub = types.ModuleType("automatiq.core.recorder.video_recorder")

        class FakeActionVideoRecorder:
            def __init__(self, fps=None):
                self.fps = fps

            def split_video(self, full_video_path, clip_path, clip_start, clip_end):
                return True

        recorder_stub.ActionVideoRecorder = FakeActionVideoRecorder
        installed = {
            "litellm": litellm_stub,
            "litellm.exceptions": exceptions_stub,
            "imageio_ffmpeg": imageio_stub,
            "automatiq.core.recorder.video_recorder": recorder_stub,
        }
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
    leaked = [key for key in ("litellm", "imageio_ffmpeg", "mss", "zendriver", "magika") if key in sys.modules]
    assert leaked == [], f"heavy modules leaked into sys.modules: {leaked}"


def test_no_actions_emits_info_event(monkeypatch, actions_mod, tmp_path):
    signal = _RecordingSignal()
    monkeypatch.setattr(events, "log_info", signal)
    actions_mod.merge_and_annotate_actions(
        [],
        str(tmp_path / "full.mp4"),  # missing video: irrelevant for empty actions
        0.0,
        str(tmp_path / "clips"),
    )
    assert signal.texts == [_NO_ACTIONS_TEXT]


def test_missing_video_with_actions_emits_nothing(monkeypatch, actions_mod, tmp_path):
    signal = _RecordingSignal()
    monkeypatch.setattr(events, "log_info", signal)
    actions_mod.merge_and_annotate_actions(
        [{"timestamp_unix": 2.0, "type": "click"}],
        str(tmp_path / "missing.mp4"),  # actions exist but the video is gone
        1.0,
        str(tmp_path / "clips"),
    )
    assert signal.texts == []  # that path has no story - stays silent
