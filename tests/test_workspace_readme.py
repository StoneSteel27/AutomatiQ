"""_write_readme + _README_TEMPLATE best-effort contract.

The compile package imports ai_analyzer (litellm, imageio_ffmpeg),
video_recorder (mss, imageio_ffmpeg) and serializers (magika) at module
level. To keep the suite's no-heavy-imports guarantee, the workspace module
is imported lazily inside each test with exactly those heavy entry points
stubbed in sys.modules; the stubs are removed right after the import.
_write_readme itself only needs os + events + the template constant, so the
real code paths are fully exercised.
"""

import importlib
import sys
from types import ModuleType
from unittest.mock import MagicMock

_HEAVY_MODULES = ("zendriver", "mss", "litellm", "magika", "imageio_ffmpeg")
_WORKSPACE = "automatiq.core.recorder.compile.workspace"

# The module-level names the compile package pulls in from heavy modules.
_STUB_SPECS = {
    "magika": ["Magika"],
    "automatiq.core.recorder.ai_analyzer": ["VideoActionAnalyzer"],
    "automatiq.core.recorder.video_recorder": ["ActionVideoRecorder"],
}


def _load_workspace():
    """Import the real workspace module without loading any heavy dependency."""
    if _WORKSPACE not in sys.modules:
        stubs = {}
        for mod_name, attrs in _STUB_SPECS.items():
            mod = ModuleType(mod_name)
            for attr in attrs:
                setattr(mod, attr, MagicMock())
            sys.modules[mod_name] = mod
            stubs[mod_name] = mod
        try:
            importlib.import_module(_WORKSPACE)
        finally:
            for mod_name in stubs:
                sys.modules.pop(mod_name, None)
    workspace = sys.modules[_WORKSPACE]
    leaked = [name for name in _HEAVY_MODULES if name in sys.modules]
    assert leaked == [], f"heavy modules leaked during workspace import: {leaked}"
    return workspace


def test_write_readme_content_and_size(tmp_path):
    workspace = _load_workspace()
    workspace._write_readme(str(tmp_path))
    readme = tmp_path / "README.md"
    assert readme.exists()
    assert readme.stat().st_size > 1500
    text = readme.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# AutomatiQ session dump"
    for token in ("timeline.json", "SUMMARY.json", "transaction.json"):
        assert token in text
    # Default vision line: sessions compiled without vision state report skipped,
    # rendered with EXACTLY ONE "AI vision annotation: " prefix.
    assert "AI vision annotation: skipped (no key - set recorder_api_key in ~/.automatiq/config.toml)" in text
    assert text.count("AI vision annotation: ") == 1


def test_write_readme_vision_line_override(tmp_path):
    workspace = _load_workspace()
    workspace._write_readme(str(tmp_path), vision_line="AI vision annotation: enabled (model gemini/x, 3/3 clips)")
    text = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "AI vision annotation: enabled (model gemini/x, 3/3 clips)" in text
    assert text.count("AI vision annotation: ") == 1
    assert "skipped (no key" not in text


def test_write_readme_documents_dump_structure(tmp_path):
    """The README documents the analysis workflow, video guidance, frame
    naming convention, ai_* fields, and keeps the sensitive-data block."""
    workspace = _load_workspace()
    workspace._write_readme(str(tmp_path))
    text = (tmp_path / "README.md").read_text(encoding="utf-8")

    # New analysis-workflow + video sections present.
    assert "## How to analyze this dump" in text
    assert "## Using the video (recommended)" in text

    # Frame-naming convention documented (components + opcode suffixes).
    assert "{seq:05d}_{direction}_{delta_ms}ms" in text
    for suffix in ("_ping", "_pong", "_close", "_continuation"):
        assert suffix in text
    # Direction tokens are client/server (sent/received would be wrong:
    # cdp/websockets.py streams WebSocketFrameSent as "client" and
    # WebSocketFrameReceived as "server" into the filename).
    assert "- `direction` - `client` (sent by the browser) or `server` (received from the server)" in text
    assert "00000_client_0ms.txt" in text  # layout-tree example frame, _client_
    assert "00001_server_12ms.json" in text  # layout-tree example frame, _server_

    # ai_* annotation fields documented on user_action.
    for field in (
        "ai_macro_summary",
        "ai_elements_interacted",
        "ai_action_success",
        "ai_video_file",
        "video_start_sec",
        "video_end_sec",
    ):
        assert field in text

    # Sensitive-data block intact (whitespace-tolerant phrase checks).
    flat = " ".join(text.split())
    assert "## Sensitive data" in text
    assert "full-fidelity recording" in flat
    assert "stored verbatim - nothing is redacted" in flat
    assert "Treat the whole folder as a secret" in flat

    # The placeholder is fully replaced on render.
    assert "{vision_annotation}" not in text

    # Layout tree lists the four artifact groups + the full recording.
    for artifact in ("full_record.mp4", "|- clips/", "|- requests/", "'- websockets/"):
        assert artifact in text


def test_readme_template_line_budget():
    """The template stays a dense but bounded single string (120-160 lines)."""
    workspace = _load_workspace()
    line_count = len(workspace._README_TEMPLATE.splitlines())
    assert 120 <= line_count <= 160


def test_write_readme_render_smoke(tmp_path):
    """Smoke: _write_readme renders a well-formed markdown README that
    parses into a stable section structure with no leftover placeholder."""
    workspace = _load_workspace()
    workspace._write_readme(str(tmp_path), vision_line="AI vision annotation: enabled (model gemini/x, 3/3 clips)")
    text = (tmp_path / "README.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# AutomatiQ session dump"
    headings = [line for line in lines if line.startswith("## ")]
    assert headings[0] == "## Sensitive data"
    assert len(headings) == len(set(headings))  # no duplicate sections
    assert headings == [
        "## Sensitive data",
        "## How to analyze this dump",
        "## Using the video (recommended)",
        "## Vision annotation",
        "## Layout",
        "## timeline.json",
        "## SUMMARY.json",
        "## requests/<txn>/transaction.json",
        "## websockets/<conn>/",
        "## session_metadata.json",
        "## Notes",
    ]
    # Indented artifact tree parses as an indented block under ## Layout.
    assert "    '- workspace/session_dump/" in text


def test_vision_readme_line_states():
    workspace = _load_workspace()
    # Video disabled: the skip line states the real reason, not "no key".
    assert (
        workspace._vision_readme_line({"configured": False, "skip_reason": "video_disabled"})
        == "AI vision annotation: skipped (video disabled)"
    )
    # No key: names the config.toml slot.
    assert (
        workspace._vision_readme_line({"configured": False})
        == "AI vision annotation: skipped (no key - set recorder_api_key in ~/.automatiq/config.toml)"
    )
    # Enabled: names the model actually used (resolved at session start).
    assert (
        workspace._vision_readme_line({"configured": True, "model": "openai/gpt-4o-mini", "analyzed": 2, "failed": 1})
        == "AI vision annotation: enabled (model openai/gpt-4o-mini, 2/3 clips)"
    )
    assert (
        workspace._vision_readme_line({"configured": True, "analyzed": 1, "failed": 0, "fatal_reason": "auth"})
        == "AI vision annotation: failed (key rejected)"
    )
    assert (
        workspace._vision_readme_line({"configured": True, "analyzed": 0, "failed": 2, "fatal_reason": "other"})
        == "AI vision annotation: failed (aborted - see logs)"
    )
    # Every state string carries exactly one prefix.
    for state in (
        {"configured": False, "skip_reason": "video_disabled"},
        {"configured": False},
        {"configured": True, "model": "openai/gpt-4o-mini", "analyzed": 2, "failed": 1},
        {"configured": True, "analyzed": 1, "failed": 0, "fatal_reason": "auth"},
        {"configured": True, "analyzed": 0, "failed": 2, "fatal_reason": "other"},
    ):
        assert workspace._vision_readme_line(state).count("AI vision annotation: ") == 1


def test_write_readme_is_best_effort(tmp_path):
    workspace = _load_workspace()
    # A file where a directory is needed: open() must fail inside
    # _write_readme and be swallowed (best-effort, never raises).
    (tmp_path / "blocker").write_text("not a directory", encoding="utf-8")
    blocked = tmp_path / "blocker" / "sub"
    workspace._write_readme(str(blocked))
    assert not (blocked / "README.md").exists()
