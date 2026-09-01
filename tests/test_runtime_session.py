"""RecordingSession state machine — driven without ever calling .start().

No worker thread is spawned anywhere in this file: the session is exercised
purely through its snapshot/wait/terminal-state API and private helpers.
"""

from pathlib import Path

from automatiq.mcp.runtime import STATE_COMPLETED, RecordingSession


def _session(tmp_output_root: str, name: str = "smoke") -> RecordingSession:
    return RecordingSession(
        url="https://example.com",
        session_name=name,
        proxy=None,
        include_video=False,
        output_root=tmp_output_root,
    )


def test_initial_status_snapshot(tmp_output_root):
    session = _session(tmp_output_root)
    snap = session.status()
    assert snap["session_id"] == session.id
    assert snap["state"] == "created"
    assert isinstance(snap["vision_configured"], bool)
    # No session dir exists yet -> no resolved output paths in the snapshot.
    assert "output_dir" not in snap
    assert "readme_path" not in snap


def test_status_paths_once_session_dir_exists(tmp_output_root):
    session = _session(tmp_output_root)
    expected_dir = Path(tmp_output_root) / session.session_name
    expected_dir.mkdir()
    snap = session.status()
    assert snap["output_dir"] == str(expected_dir)
    assert snap["readme_path"] == str(expected_dir / "README.md")


def test_wait_times_out_for_never_started_session(tmp_output_root):
    session = _session(tmp_output_root)
    assert session.wait(0.05) is False
    assert session.is_terminal is False


def test_terminal_state_releases_wait(tmp_output_root):
    session = _session(tmp_output_root)
    session._set_state(STATE_COMPLETED)
    session._done.set()
    assert session.is_terminal is True
    assert session.wait(0) is True


def test_resolved_output_dir_collision_suffixes(tmp_output_root):
    root = Path(tmp_output_root)

    # Nothing on disk yet -> unresolved.
    assert _session(tmp_output_root, name="collide")._resolved_output_dir() is None

    # Base dir exists -> it wins: the base-existence check runs before the
    # suffix scan, so an existing <name> beats any <name>_NN siblings.
    (root / "collide").mkdir()
    assert _session(tmp_output_root, name="collide")._resolved_output_dir() == root / "collide"

    # Base missing but suffixed dirs exist -> the latest contiguous suffix
    # (the scan walks _01, _02, ... and returns the last one that exists).
    (root / "collide").rmdir()
    (root / "collide_01").mkdir()
    (root / "collide_02").mkdir()
    assert _session(tmp_output_root, name="collide")._resolved_output_dir() == root / "collide_02"
