"""ParentWatchdog: clean-stop path and orphaned-parent path.

ParentWatchdog.run() polls ``os.getppid()`` against the pid captured in
``__init__``; on mismatch it calls ``registry.stop_all(join_timeout=10.0)``
and then ``os._exit(1)``. The orphan test fakes the mismatch by monkeypatching
``os.getppid`` (the parent-alive check the real code uses) and additionally
neutralizes ``os._exit`` so the watchdog cannot kill the pytest process.
"""

import os
import time

from automatiq.mcp.runtime import ParentWatchdog


class _SpyRegistry:
    def __init__(self):
        self.calls: list[float | None] = []

    def stop_all(self, join_timeout=None):
        self.calls.append(join_timeout)


def test_stop_on_fresh_watchdog_without_orphan():
    spy = _SpyRegistry()
    watchdog = ParentWatchdog(spy, interval=0.05)
    watchdog.start()
    watchdog.stop()
    watchdog.join(timeout=2.0)
    assert not watchdog.is_alive()
    # Parent is alive (ppid unchanged) -> stop_all must never have been called.
    assert spy.calls == []


def test_orphaned_parent_triggers_stop_all(monkeypatch):
    spy = _SpyRegistry()
    watchdog = ParentWatchdog(spy, interval=0.05)  # captures the real ppid here
    # Fake an orphan: make the polled check return a different parent pid.
    monkeypatch.setattr(os, "getppid", lambda: watchdog._parent_pid + 424242)
    # run() ends with os._exit(1); neutralize it so pytest survives.
    monkeypatch.setattr(os, "_exit", lambda code: None)

    watchdog.start()
    deadline = time.monotonic() + 2.0
    while not spy.calls and time.monotonic() < deadline:
        time.sleep(0.01)

    assert spy.calls, "watchdog did not call registry.stop_all within 2s"
    assert spy.calls[0] == 10.0  # the join_timeout the source passes

    watchdog.stop()
    watchdog.join(timeout=2.0)
    assert not watchdog.is_alive()
