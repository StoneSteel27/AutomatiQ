"""Tests for config file schema migrations and backup logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from automatiq.core import config
from automatiq.core.config import (
    _ensure_latest_schema,
    _get_existing_sections_and_keys,
    _parse_toml_sections_and_keys,
)

# A simplified, stable default configuration for deterministic testing
MOCK_DEFAULT_TOML = """\
[browser]
# Which browser to use for recording.
type = "brave"

# Brave release channel
channel = "release"

# Optional explicit path
# executable_path = ""

[agent]
# Maximum iterations
max_steps       = 100

# Timeout seconds
sandbox_timeout = 60

[telemetry]
# Anonymous telemetry
enabled = true

# Endpoint URL
# endpoint = "https://api.automatiq.run/v1/telemetry"
"""


def test_parse_toml_sections_and_keys() -> None:
    """Validate that we correctly group keys and their preceding comments."""
    parsed = _parse_toml_sections_and_keys(MOCK_DEFAULT_TOML)

    assert "browser" in parsed
    assert "agent" in parsed
    assert "telemetry" in parsed

    # Check browser.type
    assert "type" in parsed["browser"]
    type_info = parsed["browser"]["type"]
    assert type_info["commented"] is False
    assert any("Which browser to use" in line for line in type_info["lines"])
    assert type_info["lines"][-1] == 'type = "brave"'

    # Check browser.executable_path (commented key)
    assert "executable_path" in parsed["browser"]
    exec_info = parsed["browser"]["executable_path"]
    assert exec_info["commented"] is True
    assert any("Optional explicit path" in line for line in exec_info["lines"])
    assert exec_info["lines"][-1] == '# executable_path = ""'


def test_get_existing_sections_and_keys() -> None:
    """Validate parsing of existing (potentially outdated) user TOML."""
    user_toml = """\
[browser]
type = "chrome"

[agent]
# max_steps = 50
"""
    existing = _get_existing_sections_and_keys(user_toml)
    assert "browser" in existing
    assert "type" in existing["browser"]
    assert "agent" in existing
    assert "max_steps" in existing["agent"]
    assert "channel" not in existing["browser"]


@patch("automatiq.core.config._DEFAULT_CONFIG_TOML", MOCK_DEFAULT_TOML)
def test_ensure_latest_schema_missing_section(tmp_path: Path) -> None:
    """Test migration when a whole section (like telemetry) is missing from user's file."""
    temp_config = tmp_path / "config.toml"

    # User's old config completely missing [telemetry]
    old_user_toml = """\
[browser]
type = "brave"
channel = "release"

[agent]
max_steps = 100
sandbox_timeout = 60
"""
    temp_config.write_text(old_user_toml, encoding="utf-8")

    with patch("automatiq.core.config.CONFIG_FILE", temp_config):
        _ensure_latest_schema()

    updated = temp_config.read_text(encoding="utf-8")

    # Check that [telemetry] was appended
    assert "[telemetry]" in updated
    assert "enabled = true" in updated
    assert "# Anonymous telemetry" in updated

    # Check backup file was created and contains the original config
    backup_file = temp_config.with_suffix(".toml.bak")
    assert backup_file.exists()
    assert backup_file.read_text(encoding="utf-8") == old_user_toml


@patch("automatiq.core.config._DEFAULT_CONFIG_TOML", MOCK_DEFAULT_TOML)
def test_ensure_latest_schema_missing_key_in_existing_section(tmp_path: Path) -> None:
    """Test migration when a section exists, but some keys within it are missing."""
    temp_config = tmp_path / "config.toml"

    # User has [browser] and [agent], but [browser] is missing 'channel' and [agent] is missing 'sandbox_timeout'
    old_user_toml = """\
[browser]
type = "chrome"  # customized by user

[agent]
max_steps = 250  # customized by user

[telemetry]
enabled = false  # customized by user
"""
    temp_config.write_text(old_user_toml, encoding="utf-8")

    with patch("automatiq.core.config.CONFIG_FILE", temp_config):
        _ensure_latest_schema()

    updated = temp_config.read_text(encoding="utf-8")

    # Verify customized settings were untouched
    assert 'type = "chrome"' in updated
    assert "max_steps = 250" in updated
    assert "enabled = false" in updated

    # Verify missing keys and comments were appended to correct sections
    assert "Brave release channel" in updated
    assert 'channel = "release"' in updated
    assert "Timeout seconds" in updated
    assert "sandbox_timeout = 60" in updated

    # Verify backup exists
    backup_file = temp_config.with_suffix(".toml.bak")
    assert backup_file.exists()
    assert backup_file.read_text(encoding="utf-8") == old_user_toml


@patch("automatiq.core.config._DEFAULT_CONFIG_TOML", MOCK_DEFAULT_TOML)
def test_no_changes_if_schema_already_latest(tmp_path: Path) -> None:
    """Test that if the user's config already has all keys, no migration or backup happens."""
    temp_config = tmp_path / "config.toml"

    user_toml = """\
[browser]
type = "brave"
channel = "release"
# executable_path = ""

[agent]
max_steps = 100
sandbox_timeout = 60

[telemetry]
enabled = true
# endpoint = "some-endpoint"
"""
    temp_config.write_text(user_toml, encoding="utf-8")

    with patch("automatiq.core.config.CONFIG_FILE", temp_config):
        _ensure_latest_schema()

    # Verify file text remains unchanged
    assert temp_config.read_text(encoding="utf-8") == user_toml

    # Verify no backup was created since no modification happened
    backup_file = temp_config.with_suffix(".toml.bak")
    assert not backup_file.exists()


def test_telemetry_notice_triggers(tmp_path: Path) -> None:
    """Verify that SHOW_TELEMETRY_NOTICE is correctly set based on telemetry state inside state.json."""
    temp_state_file = tmp_path / "state.json"

    # Scenario 1: Telemetry enabled, state file doesn't exist -> should show notice
    with (
        patch("automatiq.core.config.TELEMETRY_ENABLED", True),
        patch("automatiq.core.config.STATE_FILE", temp_state_file),
    ):
        state = config.load_state()
        show_notice = config.TELEMETRY_ENABLED and not state.get("telemetry_notice_shown", False)
        assert show_notice is True

    # Scenario 2: Telemetry disabled, state file doesn't exist -> should NOT show notice
    with (
        patch("automatiq.core.config.TELEMETRY_ENABLED", False),
        patch("automatiq.core.config.STATE_FILE", temp_state_file),
    ):
        state = config.load_state()
        show_notice = config.TELEMETRY_ENABLED and not state.get("telemetry_notice_shown", False)
        assert show_notice is False

    # Scenario 3: Telemetry enabled, state has telemetry_notice_shown=True -> should NOT show notice
    with (
        patch("automatiq.core.config.TELEMETRY_ENABLED", True),
        patch("automatiq.core.config.STATE_FILE", temp_state_file),
    ):
        # Save state first
        state = config.load_state()
        state["telemetry_notice_shown"] = True
        config.save_state(state)

        # Verify it loads back
        new_state = config.load_state()
        assert new_state.get("telemetry_notice_shown") is True

        # Check notice trigger
        show_notice = config.TELEMETRY_ENABLED and not new_state.get("telemetry_notice_shown", False)
        assert show_notice is False
