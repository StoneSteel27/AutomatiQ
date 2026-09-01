"""config.toml mechanics: auto-create, env > file > default, schema migration,
fresh key re-read.

Reloads mutate the shared config module in place, so every reload that
changes globals is followed by a restoring reload (the ``restore_config``
fixture); env manipulation happens inside ``monkeypatch.context()`` blocks
(the suite's established pattern, see test_config_default.py).
"""

import importlib
import logging

import pytest

import automatiq.core.config as config

# ORIGINAL-CLI-shaped config (no recorder_api_key; unknown [agent] section),
# trimmed from the original install's ~/.automatiq/config.toml shape.
_ORIGINAL_SHAPED = """\
# AutomatiQ user configuration
#
# Values here override the built-in defaults.

[models]
# LiteLLM model string for the investigator agent.
agent    = "gemini/gemini-3.5-flash"

# Vision model for video-clip analysis during recording.
# Use a cheaper/faster model here to reduce cost.
recorder = "gemini/gemini-3.1-flash-lite"

# Custom OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, etc.).
# base_url = "http://localhost:11434/v1"

[agent]
# Maximum agent loop iterations before giving up.
max_steps       = 100

[recording]
# Frames per second for screen capture.
fps                     = 3

[browser]
type = "brave"

[telemetry]
enabled = true
"""

_KNOB_VARS = (
    "AUTOMATIQ_RECORDER_MODEL",
    "AUTOMATIQ_API_BASE",
    "AUTOMATIQ_FPS",
    "AUTOMATIQ_SEGMENT_PAD",
    "AUTOMATIQ_MERGE_GAP",
    "AUTOMATIQ_MAX_FRAMES_PER_PROMPT",
    "AUTOMATIQ_BROWSER_CHANNEL",
    "AUTOMATIQ_TELEMETRY",
    "AUTOMATIQ_RECORDER_PROXY_ENABLED",
    "AUTOMATIQ_RECORDER_PROXY_SERVER",
    "AUTOMATIQ_RECORDER_PROXY_PROVIDER",
)


@pytest.fixture
def restore_config():
    """Always end with a config reload under the ambient environment."""
    yield
    importlib.reload(config)


def _reload_with_home(monkeypatch, home, extra_env: dict[str, str] | None = None):
    """Reload config with AUTOMATIQ_HOME=home and the knob env vars scrubbed."""
    with monkeypatch.context() as m:
        for var in _KNOB_VARS:
            m.delenv(var, raising=False)
        m.setenv("AUTOMATIQ_HOME", str(home))
        for name, value in (extra_env or {}).items():
            m.setenv(name, value)
        importlib.reload(config)
    return config


def test_template_autocreated_on_first_import(monkeypatch, tmp_path, restore_config):
    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.CONFIG_FILE == tmp_path / "config.toml"
    assert (tmp_path / "config.toml").exists()
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == cfg._DEFAULT_CONFIG_TOML
    assert cfg.FIRST_RUN is True
    template = cfg._DEFAULT_CONFIG_TOML
    # [recorder_proxy] is file-backed again; base_url is uncommented/first-class.
    assert "[recorder_proxy]\n" in template
    assert "enabled = false" in template
    assert 'base_url = ""' in template
    assert "# base_url" not in template
    # [output] stays env-only.
    assert "[output]" not in template
    assert "AUTOMATIQ_BROWSER_EXECUTABLE_PATH" in template  # env pointer instead


def test_second_import_is_not_first_run(monkeypatch, tmp_path, restore_config):
    _reload_with_home(monkeypatch, tmp_path)
    reloaded = importlib.reload(config)
    assert reloaded.FIRST_RUN is False
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == reloaded._DEFAULT_CONFIG_TOML


def test_env_beats_file_beats_default_str_knob(monkeypatch, tmp_path, restore_config):
    (tmp_path / "config.toml").write_text('[models]\nrecorder = "openai/gpt-4o-mini"\n', encoding="utf-8")
    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.RECORDER_AI_MODEL == "openai/gpt-4o-mini"  # file beats default

    _reload_with_home(monkeypatch, tmp_path, {"AUTOMATIQ_RECORDER_MODEL": "gemini/env-model"})
    assert cfg.RECORDER_AI_MODEL == "gemini/env-model"  # env beats file

    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.RECORDER_AI_MODEL == "openai/gpt-4o-mini"  # env gone -> file again


def test_env_beats_file_beats_default_int_knob(monkeypatch, tmp_path, restore_config):
    (tmp_path / "config.toml").write_text("[recording]\nfps = 30\n", encoding="utf-8")
    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.FPS == 30

    _reload_with_home(monkeypatch, tmp_path, {"AUTOMATIQ_FPS": "99"})
    assert cfg.FPS == 99  # env beats file

    _reload_with_home(monkeypatch, tmp_path)
    assert cfg.FPS == 30


def test_bool_knob_file_and_env(monkeypatch, tmp_path, restore_config):
    (tmp_path / "config.toml").write_text("[telemetry]\nenabled = false\n", encoding="utf-8")
    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.TELEMETRY_ENABLED is False  # file beats the true default

    _reload_with_home(monkeypatch, tmp_path, {"AUTOMATIQ_TELEMETRY": "1"})
    assert cfg.TELEMETRY_ENABLED is True  # env beats file

    _reload_with_home(monkeypatch, tmp_path)
    assert cfg.TELEMETRY_ENABLED is False


def test_base_url_file_env_default(monkeypatch, tmp_path, restore_config):
    (tmp_path / "config.toml").write_text('[models]\nbase_url = ""\n', encoding="utf-8")
    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.API_BASE is None  # empty string = provider default

    (tmp_path / "config.toml").write_text('[models]\nbase_url = "http://localhost:11434/v1"\n', encoding="utf-8")
    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.API_BASE == "http://localhost:11434/v1"

    _reload_with_home(monkeypatch, tmp_path, {"AUTOMATIQ_API_BASE": "http://localhost:1234/v1"})
    assert cfg.API_BASE == "http://localhost:1234/v1"  # env beats file

    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.API_BASE == "http://localhost:11434/v1"  # env gone -> file again


def test_recorder_proxy_file_env_default(monkeypatch, tmp_path, restore_config):
    # Section absent -> defaults unchanged.
    (tmp_path / "config.toml").write_text('[models]\nrecorder = "openai/gpt-4o-mini"\n', encoding="utf-8")
    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.RECORDER_PROXY_ENABLED is False
    assert cfg.RECORDER_PROXY_SERVER is None
    assert cfg.RECORDER_PROXY_PROVIDER is None

    # File values applied.
    (tmp_path / "config.toml").write_text(
        '[recorder_proxy]\nenabled = true\nserver = "http://127.0.0.1:3128"\nprovider = "mymodule:rotate"\n',
        encoding="utf-8",
    )
    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.RECORDER_PROXY_ENABLED is True
    assert cfg.RECORDER_PROXY_SERVER == "http://127.0.0.1:3128"
    assert cfg.RECORDER_PROXY_PROVIDER == "mymodule:rotate"

    # Env overrides file.
    _reload_with_home(
        monkeypatch,
        tmp_path,
        {"AUTOMATIQ_RECORDER_PROXY_SERVER": "socks5://127.0.0.1:1080", "AUTOMATIQ_RECORDER_PROXY_ENABLED": "0"},
    )
    assert cfg.RECORDER_PROXY_ENABLED is False
    assert cfg.RECORDER_PROXY_SERVER == "socks5://127.0.0.1:1080"
    assert cfg.RECORDER_PROXY_PROVIDER == "mymodule:rotate"  # untouched by env


def test_bad_file_values_are_defensive(monkeypatch, tmp_path, restore_config, caplog):
    (tmp_path / "config.toml").write_text('[recording]\nfps = "three"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="automatiq.core.config"):
        cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.FPS == 3  # coercion failure -> default kept, no crash
    assert any(
        "ignored invalid value for key 'fps'" in r.message and r.levelno == logging.WARNING for r in caplog.records
    )


def test_migration_aborts_when_backup_write_fails(monkeypatch, tmp_path, caplog):
    user_text = '[models]\nrecorder = "openai/gpt-4o-mini"\n'  # missing recorder_api_key
    real_file = tmp_path / "config.toml"
    real_file.write_text(user_text, encoding="utf-8")

    class _FailingBackup:
        """CONFIG_FILE stand-in whose .bak write always fails."""

        def read_text(self, encoding=None):
            return real_file.read_text(encoding="utf-8")

        def with_suffix(self, suffix):
            class _Boom:
                def write_text(self, data, encoding=None):
                    raise OSError("disk full")

            return _Boom()

        def write_text(self, data, encoding=None):
            raise AssertionError("migrated file written despite backup failure")

    monkeypatch.setattr(config, "CONFIG_FILE", _FailingBackup())
    with caplog.at_level(logging.WARNING, logger="automatiq.core.config"):
        config._ensure_latest_schema()

    assert real_file.read_text(encoding="utf-8") == user_text  # NOT overwritten
    assert any("migration aborted" in r.message for r in caplog.records)


def test_migration_inserts_recorder_api_key_and_writes_bak(monkeypatch, tmp_path, restore_config):
    (tmp_path / "config.toml").write_text(_ORIGINAL_SHAPED, encoding="utf-8")
    cfg = _reload_with_home(monkeypatch, tmp_path)

    migrated = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert cfg.FIRST_RUN is False
    # Missing [models] keys were inserted right after the section header:
    # recorder_api_key first, then base_url (insertion is reversed-schema-order).
    lines = migrated.splitlines()
    header_idx = lines.index("[models]")
    assert lines[header_idx + 1] == 'recorder_api_key = ""'
    assert lines[header_idx + 2] == 'base_url = ""'
    # The missing [recorder_proxy] section was appended with its template block.
    assert "[recorder_proxy]" in migrated
    assert "enabled = false" in migrated
    # User content preserved verbatim.
    assert 'agent    = "gemini/gemini-3.5-flash"' in migrated
    assert 'recorder = "gemini/gemini-3.1-flash-lite"' in migrated
    assert "max_steps       = 100" in migrated
    assert "[agent]" in migrated
    assert "# Vision model for video-clip analysis during recording." in migrated
    # .bak holds the pre-migration content.
    bak = tmp_path / "config.toml.bak"
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == _ORIGINAL_SHAPED
    # The migrated file still parses and the empty key slot reads back as None.
    assert cfg.read_recorder_api_key() == (None, True)


def test_migration_appends_missing_sections_with_comments(monkeypatch, tmp_path, restore_config):
    # A file carrying [models]/[recording]/[browser] but missing [telemetry]
    # and [recorder_proxy]: the loader re-appends both sections with their
    # commented template blocks, and inserts bare `key = default` lines for
    # keys missing from EXISTING sections. [output] is never injected
    # (env-only knob).
    user_text = (
        '[models]\nrecorder = "openai/gpt-4o-mini"\n\n'
        "[recording]\n# my fps note\nfps = 30\n\n"
        '[browser]\ntype = "brave"\n'
    )
    (tmp_path / "config.toml").write_text(user_text, encoding="utf-8")
    cfg = _reload_with_home(monkeypatch, tmp_path)
    migrated = (tmp_path / "config.toml").read_text(encoding="utf-8")
    for section_text in ("[models]", "[recording]", "[browser]", "[recorder_proxy]", "[telemetry]"):
        assert section_text in migrated
    assert "[output]" not in migrated
    # The appended blocks carry their commented template text.
    assert "# Anonymous usage-volume telemetry (no URLs, code, or personal data)." in migrated
    assert "# Route the recording browser through an HTTP/SOCKS proxy." in migrated
    # Missing keys in existing sections were inserted as bare lines...
    assert 'base_url = ""' in migrated
    assert 'recorder_api_key = ""' in migrated
    assert "segment_pad = 2" in migrated
    # ...and the user's own lines/comments survived verbatim.
    assert "# my fps note" in migrated
    assert "fps = 30" in migrated
    assert 'recorder = "openai/gpt-4o-mini"' in migrated
    # Appending/inserting is a modification -> the original is backed up.
    bak = tmp_path / "config.toml.bak"
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == user_text
    assert cfg.TELEMETRY_ENABLED is True
    assert cfg.RECORDER_PROXY_ENABLED is False
    assert cfg.FPS == 30  # user values untouched by the migration


def test_malformed_file_falls_back_to_defaults(monkeypatch, tmp_path, restore_config):
    (tmp_path / "config.toml").write_text("this is not [ valid toml", encoding="utf-8")
    cfg = _reload_with_home(monkeypatch, tmp_path)
    assert cfg.FPS == 3  # defaults, no crash
    assert cfg.RECORDER_AI_MODEL == "gemini/gemini-3.1-flash-lite"
    assert cfg.FIRST_RUN is False


def test_read_recorder_api_key_is_fresh(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_file)

    cfg_file.write_text('[models]\nrecorder_api_key = "k-test-123"\n', encoding="utf-8")
    assert config.read_recorder_api_key() == ("k-test-123", True)

    cfg_file.write_text('[models]\nrecorder_api_key = ""\n', encoding="utf-8")
    assert config.read_recorder_api_key() == (None, True)

    cfg_file.write_text("[models]\nrecorder_api_key = 123\n", encoding="utf-8")
    assert config.read_recorder_api_key() == (None, True)  # non-string -> None

    cfg_file.unlink()
    assert config.read_recorder_api_key() == (None, False)  # missing file

    cfg_file.write_text("not [ valid toml", encoding="utf-8")
    assert config.read_recorder_api_key() == (None, False)  # parse error
