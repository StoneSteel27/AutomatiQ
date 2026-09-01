"""annotate_user_interactions: background re-annotation of a recorded dump.

The annotation module stays light at import (analyzer imports lazily inside
the worker thread); tests stub ``automatiq.core.recorder.ai_analyzer`` in
sys.modules with a fake analyzer and preload the REAL compile.workspace
module with its heavy deps stubbed (same trick as test_workspace_readme) so
README refresh runs the real code. The vision preflight is stubbed at the
server seam; its own resolution logic is covered by test_vision_preflight.
"""

import asyncio
import json
import sys
import time
import types
from pathlib import Path

import pytest

from automatiq.core import config
from automatiq.mcp import annotation, server
from automatiq.mcp.runtime import SessionRegistry

_SID = "20260831_174635_7893ab"
_WORKSPACE_MOD = "automatiq.core.recorder.compile.workspace"


# ── Heavy-import-free loaders ────────────────────────────────────────────────


def _load_real_workspace():
    """Import the real compile.workspace with heavy entry points stubbed."""
    if _WORKSPACE_MOD not in sys.modules:
        stubs = {}
        for mod_name, attrs in {
            "magika": ["Magika"],
            "automatiq.core.recorder.ai_analyzer": ["VideoActionAnalyzer"],
            "automatiq.core.recorder.video_recorder": ["ActionVideoRecorder"],
        }.items():
            mod = types.ModuleType(mod_name)
            for attr in attrs:
                setattr(mod, attr, lambda: None)
            sys.modules[mod_name] = mod
            stubs[mod_name] = mod
        try:
            import importlib

            importlib.import_module(_WORKSPACE_MOD)
        finally:
            for mod_name in stubs:
                sys.modules.pop(mod_name, None)
    return sys.modules[_WORKSPACE_MOD]


class _FakeAnalyzer:
    """Stands in for VideoActionAnalyzer: records the model, canned results."""

    instances: list["_FakeAnalyzer"] = []

    def __init__(self, model=None):
        self.model = model
        self.fatal_reason = None
        self.history: list[str] = []
        _FakeAnalyzer.instances.append(self)

    def analyze_clip(self, video_path, duration_sec, raw_actions=None, cancel_check=None):
        types_ = "+".join(a.get("type", "?") for a in raw_actions or [])
        summary = f"user performed {types_} at {Path(video_path).name}"
        self.history.append(summary)
        return {"macro_summary": summary, "elements_interacted": ["Search Box"], "action_success": True}


def _install_fake_analyzer(monkeypatch):
    mod = types.ModuleType("automatiq.core.recorder.ai_analyzer")
    mod.VideoActionAnalyzer = _FakeAnalyzer
    monkeypatch.setitem(sys.modules, "automatiq.core.recorder.ai_analyzer", mod)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """Fresh registries, sandboxed OUTPUT_DIR, reset fake-analyzer log."""
    _load_real_workspace()
    _FakeAnalyzer.instances = []
    server._REGISTRY = SessionRegistry(output_root=str(tmp_path / "sessions"))
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "sessions-out")
    annotation.get_annotation_registry().clear()
    monkeypatch.setattr(server, "vision_preflight", lambda: {"model": "openai/fake-vision", "configured": True})
    yield
    annotation.get_annotation_registry().clear()
    server._REGISTRY = None
    # Drop the compile modules this file cached with stubbed heavy deps:
    # later test files' loaders yield whatever is in sys.modules, and the
    # stub bindings (e.g. a fake VideoActionAnalyzer) must not leak to them.
    for mod_name in (
        _WORKSPACE_MOD,
        "automatiq.core.recorder.compile",
        "automatiq.core.recorder.compile.actions",
        "automatiq.core.recorder.compile.network",
        "automatiq.core.recorder.compile.serializers",
        "automatiq.core.recorder.compile.websockets",
    ):
        sys.modules.pop(mod_name, None)


def _make_session(root: Path, sid: str = _SID, with_clips: bool = True) -> Path:
    """Build a compiled session dump on disk (the user's real failure shape:
    clips sliced, ai fields carrying 'Error: Could not analyze clip.')."""
    d = root / f"recording_{sid}"
    dump = d / "workspace" / "session_dump"
    clips = dump / "clips"
    clips.mkdir(parents=True)
    (d / "session_metadata.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")

    def action(n, clip, action="input", text="hello"):
        return {
            "timestamp": 1788178600.0 + n,
            "timestamp_iso": f"2026-08-31T12:16:4{n}.000+00:00",
            "event_type": "user_action",
            "action": action,
            "details": {"text": text},
            "ai_macro_summary": "Error: Could not analyze clip.",
            "ai_elements_interacted": [],
            "ai_action_success": False,
            **({"ai_video_file": clip, "video_start_sec": 5.0, "video_end_sec": 8.5} if clip else {}),
        }

    events = [
        action(1, "clips/action_clip_000.mp4"),
        action(2, "clips/action_clip_000.mp4", action="click"),
        action(3, "clips/action_clip_001.mp4", action="click"),
        action(4, None),  # captured without a clip -> counted, never analyzed
        {"timestamp": 1788178610.0, "timestamp_iso": "x", "event_type": "network_request", "url": "https://a/"},
    ]
    if not with_clips:
        for ev in events:
            ev.pop("ai_video_file", None)
            ev.pop("video_start_sec", None)
            ev.pop("video_end_sec", None)
    (dump / "timeline.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
    (dump / "SUMMARY.json").write_text(
        json.dumps(
            {
                "session": {"total_actions": 4},
                "session_flow": [{"timestamp_iso": "x", "summary": "Error: Could not analyze clip."}],
                "statistics": {"total_actions": 4},
            }
        ),
        encoding="utf-8",
    )
    if with_clips:
        for name in ("action_clip_000.mp4", "action_clip_001.mp4"):
            (clips / name).write_bytes(b"fake-mp4")
    _load_real_workspace()._write_readme(str(d))
    return d


def _wait_done(job, timeout: float = 5.0) -> bool:
    return job.wait(timeout)


# ── Resolution helpers ───────────────────────────────────────────────────────


def test_find_session_dir_variants(tmp_path):
    root = tmp_path / "sessions"
    exact = _make_session(root)
    assert annotation.find_session_dir(root, _SID) == exact

    suffixed = root / f"recording_{_SID}_01" / "workspace" / "session_dump"
    suffixed.mkdir(parents=True)
    (suffixed / "timeline.json").write_text("[]", encoding="utf-8")
    assert annotation.find_session_dir(root, _SID) == exact  # exact wins over suffix

    assert annotation.find_session_dir(root, "missing") is None
    no_timeline = root / "recording_20260101_000000_zzzzzz"
    no_timeline.mkdir()
    assert annotation.find_session_dir(root, "20260101_000000_zzzzzz") is None


def test_latest_session_dir_picks_newest(tmp_path):
    root = tmp_path / "sessions"
    _make_session(root, sid="20260101_000000_aaaaaa")
    newer = _make_session(root, sid="20260202_000000_bbbbbb")
    time.sleep(0.05)
    (newer / "workspace" / "session_dump" / "timeline.json").touch()
    assert annotation.latest_session_dir(root) == newer
    assert annotation.latest_session_dir(root / "nothing") is None


# ── Tool: happy paths ────────────────────────────────────────────────────────


def test_annotate_refreshes_dump_and_polls_via_existing_tools(tmp_path, monkeypatch):
    _install_fake_analyzer(monkeypatch)
    root = tmp_path / "sessions"
    session_dir = _make_session(root)

    res = asyncio.run(server.annotate_user_interactions(session_id=_SID))
    assert res.is_error is False
    payload = res.structured_content
    assert payload["clips_to_analyze"] == 2
    assert payload["session_id"] == _SID
    job = annotation.get_annotation_registry().get(_SID)
    assert _wait_done(job)

    dump = session_dir / "workspace" / "session_dump"
    events = json.loads((dump / "timeline.json").read_text(encoding="utf-8"))
    user_actions = [e for e in events if e["event_type"] == "user_action"]
    assert all(not e["ai_macro_summary"].startswith("Error:") for e in user_actions[:3])
    assert user_actions[0]["ai_action_success"] is True
    assert user_actions[3]["ai_macro_summary"] == "Error: Could not analyze clip."  # no clip -> untouched
    assert events[4]["event_type"] == "network_request"  # non-actions untouched

    summary = json.loads((dump / "SUMMARY.json").read_text(encoding="utf-8"))
    flows = [f["summary"] for f in summary["session_flow"]]
    assert len(flows) == 2  # clip000's two events share one summary (deduped), clip001 distinct
    assert all("Error:" not in f for f in flows)

    backup = session_dir / "annotations_backup"
    assert (backup / "timeline.json").is_file()
    assert "Error: Could not analyze clip." in (backup / "timeline.json").read_text(encoding="utf-8")

    readme = (session_dir / "README.md").read_text(encoding="utf-8")
    assert readme.count("AI vision annotation: ") == 1
    assert "re-annotated" in readme and "openai/fake-vision" in readme

    # Disk-only session (registry has no RecordingSession): the EXISTING pollers
    # still answer via the annotation fallback.
    status = asyncio.run(server.get_status(_SID))
    assert status.is_error is False
    snap = status.structured_content
    assert snap["annotation"]["state"] == "completed"
    assert snap["annotation"]["clips"] == {"analyzed": 2, "failed": 0, "total": 2}
    assert snap["annotation"]["actions_without_clips"] == 1
    assert snap["output_dir"] == str(session_dir)

    waited = asyncio.run(server.wait_for_completion(_SID))
    assert waited.structured_content["_wait"]["reached_terminal"] is True
    assert waited.structured_content["annotation"]["state"] == "completed"


def test_annotate_with_focus_writes_narrative(tmp_path, monkeypatch):
    _install_fake_analyzer(monkeypatch)
    session_dir = _make_session(tmp_path / "sessions")

    monkeypatch.setattr(annotation, "_narrative_completion", lambda model, prompt: "The user searched for pricing.")
    res = asyncio.run(server.annotate_user_interactions(session_id=_SID, focus="what did the user search for?"))
    assert res.is_error is False
    job = annotation.get_annotation_registry().get(_SID)
    assert _wait_done(job)

    dump = session_dir / "workspace" / "session_dump"
    narrative_file = dump / "focused_analysis.md"
    text = narrative_file.read_text(encoding="utf-8")
    assert "what did the user search for?" in text
    assert "The user searched for pricing." in text

    status = asyncio.run(server.get_status(_SID))
    ann = status.structured_content["annotation"]
    assert ann["narrative"] == "The user searched for pricing."
    assert Path(ann["narrative_path"]) == narrative_file


# ── Tool: rejection paths ────────────────────────────────────────────────────


def test_annotate_unknown_session_errors(tmp_path):
    res = asyncio.run(server.annotate_user_interactions(session_id="ghost"))
    assert res.is_error is True
    assert "no recorded session found" in res.structured_content["error"]
    assert "recording_*" in res.structured_content["hint"]


def test_annotate_no_clips_errors(tmp_path):
    _make_session(tmp_path / "sessions", with_clips=False)
    res = asyncio.run(server.annotate_user_interactions(session_id=_SID))
    assert res.is_error is True
    assert "no video clips" in res.structured_content["error"]


def test_annotate_vision_unconfigured_errors(tmp_path, monkeypatch):
    _make_session(tmp_path / "sessions")
    monkeypatch.setattr(
        server, "vision_preflight", lambda: {"model": "openai/x", "configured": False, "warning": "no key"}
    )
    res = asyncio.run(server.annotate_user_interactions(session_id=_SID))
    assert res.is_error is True
    assert res.structured_content["hint"] == "no key"


def test_annotate_rejects_running_job(tmp_path):
    job = annotation.AnnotationJob(session_id=_SID, session_dir=tmp_path)
    annotation.get_annotation_registry().register(job)  # never started -> stays "running"
    res = asyncio.run(server.annotate_user_interactions(session_id=_SID))
    assert res.is_error is True
    assert "already running" in res.structured_content["error"]


def test_annotate_rejects_non_terminal_live_session(tmp_path):
    from automatiq.mcp.runtime import RecordingSession

    root = tmp_path / "sessions"
    session = RecordingSession(url="https://x", session_name=f"recording_{_SID}", output_root=str(root))
    server._REGISTRY._sessions[session.id] = session  # state: created (non-terminal)
    res = asyncio.run(server.annotate_user_interactions(session_id=session.id))
    assert res.is_error is True
    assert "still recording" in res.structured_content["error"]


def test_annotate_bad_args(tmp_path):
    res = asyncio.run(server.annotate_user_interactions(session_id=_SID, focus="x" * 2001))
    assert res.is_error is True


def test_annotate_latest_disk_session_when_no_id_given(tmp_path, monkeypatch):
    _install_fake_analyzer(monkeypatch)
    _make_session(tmp_path / "sessions")
    res = asyncio.run(server.annotate_user_interactions())
    assert res.is_error is False
    assert res.structured_content["session_id"] == _SID
    job = annotation.get_annotation_registry().get(_SID)
    assert _wait_done(job)
    assert job.snapshot()["state"] == "completed"
