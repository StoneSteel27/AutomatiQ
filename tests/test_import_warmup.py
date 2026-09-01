"""Import warm-up: _warm_imports semantics + the run entry starts it exactly once.

Kept light on purpose: the warm-up thread only ever exists in the real server
entry (server.main()), never at module import, so importing this module in
pytest spawns nothing. The success/failure tests warm a stdlib module and a
bogus name only - no heavy recorder stack is pulled into the test process
(test_server_smoke.test_no_heavy_imports depends on that staying true).
"""

from automatiq.mcp import server


def test_warm_imports_success():
    assert server._warm_imports(["json"]) == ["json"]


def test_warm_imports_swallows_errors():
    # A bogus module must not raise; it is simply skipped (falls back to the
    # session worker's own lazy import at session time).
    assert server._warm_imports(["definitely.not.a.real.module"]) == []


def test_main_starts_warmup_once(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(server, "_start_import_warmup", lambda: calls.append(1))
    # app.run() would serve stdio forever; stub it so main() returns immediately.
    monkeypatch.setattr(server.app, "run", lambda *args, **kwargs: None)
    server.main()
    assert calls == [1]
