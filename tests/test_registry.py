"""SessionRegistry bookkeeping with RecordingSession.start neutered.

``create()`` normally spawns the worker thread; monkeypatching ``start`` to a
no-op keeps every code path of the registry (create/get/latest/list/prune)
exercisable without threads, browsers, or heavy imports.
"""

from automatiq.mcp.runtime import STATE_COMPLETED, RecordingSession, SessionRegistry


def test_create_get_latest_and_list(tmp_output_root, monkeypatch):
    monkeypatch.setattr(RecordingSession, "start", lambda self: None)
    reg = SessionRegistry(output_root=tmp_output_root)

    session = reg.create(url="https://example.com", session_name="s1", proxy=None, include_video=False)
    assert reg.get(session.id) is session
    assert reg.latest() is session

    rows = reg.list_statuses()
    assert len(rows) == 1
    assert rows[0]["session_id"] == session.id
    assert rows[0]["state"] == "created"


def test_prune_terminal_keeps_most_recent(tmp_output_root, monkeypatch):
    monkeypatch.setattr(RecordingSession, "start", lambda self: None)
    reg = SessionRegistry(output_root=tmp_output_root)

    ids = []
    for i, created_at in enumerate((100.0, 200.0, 300.0)):
        session = reg.create(url="https://example.com", session_name=f"p{i}", proxy=None, include_video=False)
        session.created_at = created_at  # deterministic ordering for the prune math
        session._set_state(STATE_COMPLETED)
        session._done.set()
        ids.append(session.id)

    # prune_terminal drops terminal sessions beyond the most recent keep_last.
    dropped = reg.prune_terminal(keep_last=1)
    assert dropped == 2
    rows = reg.list_statuses()
    assert len(rows) == 1
    assert rows[0]["session_id"] == ids[-1]
    assert reg.get(ids[0]) is None
    assert reg.get(ids[1]) is None


def test_get_unknown_id_returns_none(tmp_output_root):
    reg = SessionRegistry(output_root=tmp_output_root)
    assert reg.get("nope") is None
