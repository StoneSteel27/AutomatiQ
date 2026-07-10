"""Tests for sandbox last_error_info telemetry attribute."""

from automatiq.core.ipython_sandbox import AgentSandbox


def test_last_error_info_set_on_timeout(sandbox: AgentSandbox):
    """Verify last_error_info is set when a cell times out."""
    sandbox.timeout = 1  # 1 second timeout
    result = sandbox.execute("import time; time.sleep(5)")
    assert "Status: ERROR" in result
    assert sandbox.last_error_info is not None
    # On Windows, time.sleep may not respond to the soft interrupt,
    # resulting in a hard timeout instead of a soft timeout.
    assert sandbox.last_error_info["exception_class"] in ("SandboxSoftTimeout", "SandboxHardTimeout")
    assert sandbox.last_error_info["file"] == "sandbox.py"


def test_last_error_info_cleared_on_success(sandbox: AgentSandbox):
    """Verify last_error_info is None after a successful execution."""
    # First, trigger an error
    sandbox.timeout = 1
    sandbox.execute("import time; time.sleep(5)")
    assert sandbox.last_error_info is not None

    # Now run a successful cell
    result = sandbox.execute("print('ok')")
    assert "Status: Success" in result
    assert sandbox.last_error_info is None
