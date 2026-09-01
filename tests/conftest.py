"""Shared fixtures for the automatiq MCP test suite.

The suite never launches a browser and never loads the heavy runtime
modules (zendriver, mss, litellm, magika, imageio_ffmpeg); the lazy-import
guarantee itself is asserted in ``test_server_smoke.py``.

The suite also never touches the real ``~/.automatiq``: every persistent
user-level path is redirected into per-test tmp storage, and any logging
handlers a test leaves behind are closed at teardown (module-level loggers
survive across tests; open FileHandlers would leak files into later ones).
"""

import logging

import pytest

from automatiq.core import config
from automatiq.mcp.logging_setup import _BRIDGE_LOGGER_NAME, _SESSION_LOGGER_NAME


@pytest.fixture(autouse=True)
def _isolate_user_home(tmp_path, monkeypatch):
    """Sandbox every persistent user-level path (~/.automatiq/*) into tmp.

    Tests that trigger the logging wiring (directly or via ``server.main()``)
    then bind to throwaway files instead of the production log dir. LIFO
    teardown runs AFTER test-local fixtures, so closing handlers here also
    removes anything a test left attached.
    """
    home = tmp_path / "automatiq-home"
    (home / "logs").mkdir(parents=True)
    monkeypatch.setattr(config, "HOME_DIR", home)
    monkeypatch.setattr(config, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(config, "BIN_DIR", home / "bin")
    monkeypatch.setattr(config, "BROWSERS_DIR", home / "browsers")
    monkeypatch.setattr(config, "BLOCKLIST_DIR", home / "blocklist")
    monkeypatch.setattr(config, "BLOCKLIST_DB", home / "blocklist.db")
    monkeypatch.setattr(config, "CONFIG_FILE", home / "config.toml")
    yield home

    for logger_name in (_SESSION_LOGGER_NAME, _BRIDGE_LOGGER_NAME):
        session_logger = logging.getLogger(logger_name)
        for handler in list(session_logger.handlers):
            try:
                handler.close()
            finally:
                session_logger.removeHandler(handler)


@pytest.fixture
def tmp_output_root(tmp_path):
    """Empty per-test recordings root (str path, as the runtime expects)."""
    root = tmp_path / "recordings"
    root.mkdir()
    return str(root)
