"""OUTPUT_DIR default: the server's working directory, not ~/.automatiq.

importlib.reload mutates the shared config module in place, so the reload
runs inside a monkeypatch.context() block and one final reload after the
block restores env-consistent values for the rest of the suite.
"""

import importlib
from pathlib import Path

import automatiq.core.config as config


def test_output_dir_defaults_to_server_cwd(monkeypatch):
    with monkeypatch.context() as m:
        m.delenv("AUTOMATIQ_OUTPUT_DIR", raising=False)
        reloaded = importlib.reload(config)
        # OUTPUT_DIR is a Path (unchanged shape); compare its string form.
        assert str(reloaded.OUTPUT_DIR) == str(Path.cwd() / "automatiq_sessions")
    # Restore state for other tests: the context exited, so the environment is
    # ambient again; one final reload rebuilds the module from the real env.
    final = importlib.reload(config)
    assert str(final.OUTPUT_DIR)  # non-empty
