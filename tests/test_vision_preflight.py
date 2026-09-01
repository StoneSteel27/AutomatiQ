"""vision_preflight() — config/config.toml inspection, no network, no heavy imports.

vision_preflight() re-reads ``config.RECORDER_AI_MODEL`` / ``config.API_BASE``
at call time and the recorder_api_key FRESH via ``config.read_recorder_api_key``
(so key edits apply without a restart). HOME is faked with tmp_path - both
``config.HOME_DIR`` and ``config.CONFIG_FILE`` are patched - so the real
~/.automatiq can never leak in; provider key env vars are never read as a
key source (only scrubbed, or overwritten by key plumbing) and any
os.environ mutation from plumbing is scrubbed.
"""

import os
import sys
import types
from pathlib import Path

import pytest

from automatiq.core import config
from automatiq.mcp.vision import _PROVIDER_KEY_ENV, vision_preflight

_GEMINI_MODEL = "gemini/gemini-3.1-flash-lite"

_TOML_NOTE = (
    "~/.automatiq/config.toml could not be read or parsed as TOML - vision "
    "annotation OFF and file settings ignored. Fix the file or its permissions, then record again."
)
_PLACEHOLDER_NOTE = (
    "~/.automatiq/config.toml still contains a placeholder recorder_api_key - "
    "replace it with a real key under [models] to enable vision annotation."
)
_OFF_NOTE = (
    "Vision annotation is OFF - clips will not be AI-analyzed. Enable: paste your "
    "key into recorder_api_key under [models] in ~/.automatiq/config.toml - takes "
    "effect on the next recording, no restart needed."
)


@pytest.fixture
def scrub_provider_env():
    """Restore every provider key env var after key-plumbing tests."""
    before = {var: os.environ.get(var) for var in _PROVIDER_KEY_ENV.values()}
    yield
    for var, old in before.items():
        if os.environ.get(var) != old:
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old


def _fake_home(monkeypatch, tmp_path, config_text: str | None = None) -> Path:
    """Point HOME_DIR + CONFIG_FILE at tmp_path; optionally write config.toml."""
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", _GEMINI_MODEL)
    monkeypatch.setattr(config, "API_BASE", None)
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    if config_text is not None:
        config_file.write_text(config_text, encoding="utf-8")
    return config_file


def test_gemini_without_key_is_unconfigured(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path, '[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\n')
    out = vision_preflight()
    assert out["configured"] is False
    assert out["warning"] == _OFF_NOTE


def test_gemini_env_key_alone_is_not_configured(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path, '[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\n')
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    out = vision_preflight()
    assert out["configured"] is False
    assert out["warning"] == _OFF_NOTE
    assert os.environ["GEMINI_API_KEY"] == "fake-key-for-tests"  # env var untouched


def test_custom_api_base_configured_source_base_url(monkeypatch, tmp_path, scrub_provider_env):
    _fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "API_BASE", "http://127.0.0.1:11434/v1")
    out = vision_preflight()
    assert out["configured"] is True
    assert out["source"] == "base_url"
    assert "warning" not in out
    assert "GEMINI_API_KEY" not in os.environ  # keyless local: nothing plumbed


def test_api_base_forwards_config_key(monkeypatch, tmp_path, scrub_provider_env):
    _fake_home(monkeypatch, tmp_path, '[models]\nrecorder = "x/y"\nrecorder_api_key = "cfg-key-9"\n')
    monkeypatch.setattr(config, "API_BASE", "http://localhost:1234/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = vision_preflight()
    assert out == {"model": _GEMINI_MODEL, "configured": True, "source": "base_url"}
    assert os.environ["OPENAI_API_KEY"] == "cfg-key-9"  # forwarded for authed endpoints


def test_api_base_placeholder_key_still_configured(monkeypatch, tmp_path, scrub_provider_env):
    _fake_home(
        monkeypatch, tmp_path, '[models]\nrecorder = "x/y"\nrecorder_api_key = "PASTE_YOUR_GEMINI_API_KEY_HERE"\n'
    )
    monkeypatch.setattr(config, "API_BASE", "http://localhost:1234/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = vision_preflight()
    assert out == {"model": _GEMINI_MODEL, "configured": True, "source": "base_url"}
    assert os.environ["OPENAI_API_KEY"] == "not-required"  # placeholder never forwarded
    assert "warning" not in out  # no toml/placeholder notes on this branch


def _stub_litellm(monkeypatch, validate):
    """Install a litellm module stub exposing only validate_environment."""
    stub = types.ModuleType("litellm")
    stub.validate_environment = validate
    monkeypatch.setitem(sys.modules, "litellm", stub)


def test_bare_model_string_is_invalid(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "gemini-2.5-flash")
    out = vision_preflight()
    assert out["configured"] is False
    assert "Invalid model string 'gemini-2.5-flash'" in out["warning"]
    assert "provider/model-name" in out["warning"]
    assert "openai/" not in out["warning"]  # no API_BASE -> no local-model hint


def test_bare_model_string_with_api_base_gets_openai_hint(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "llava")
    monkeypatch.setattr(config, "API_BASE", "http://localhost:11434/v1")
    out = vision_preflight()
    assert out["configured"] is False
    assert "Since base_url is set (http://localhost:11434/v1), this looks like a local model." in out["warning"]
    assert "openai/llava" in out["warning"]


def test_unknown_provider_litellm_failure_falls_back(monkeypatch, tmp_path):
    def boom(model, api_key=None):
        raise RuntimeError("litellm exploded")

    _stub_litellm(monkeypatch, boom)
    _fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "weird/model-x")
    out = vision_preflight()
    assert out["configured"] is False
    assert "unknown provider 'weird'" in out["warning"]


def test_unknown_provider_missing_single_api_key_uses_config_key(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path, '[models]\nrecorder = "ollama/llava"\nrecorder_api_key = "cfg-key-7"\n')
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "ollama/llava")
    _stub_litellm(monkeypatch, lambda model, api_key=None: {"missing_keys": ["OLLAMA_API_KEY"]})
    out = vision_preflight()
    assert out == {"model": "ollama/llava", "configured": True, "source": "config"}
    assert os.environ["OLLAMA_API_KEY"] == "cfg-key-7"
    os.environ.pop("OLLAMA_API_KEY", None)  # scrub plumbing


def test_unknown_provider_missing_api_key_without_config_key_is_listed(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path, '[models]\nrecorder = "ollama/llava"\n')
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "ollama/llava")
    _stub_litellm(monkeypatch, lambda model, api_key=None: {"missing_keys": ["OLLAMA_API_KEY"]})
    out = vision_preflight()
    assert out["configured"] is False
    assert "OLLAMA_API_KEY" in out["warning"]
    assert "OLLAMA_API_KEY" not in os.environ  # nothing to plumb


def test_unknown_provider_missing_nonkey_envs_listed(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path, '[models]\nrecorder = "ollama/llava"\nrecorder_api_key = "cfg-key-7"\n')
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "ollama/llava")
    _stub_litellm(monkeypatch, lambda model, api_key=None: {"missing_keys": ["OLLAMA_HOST", "NUM_CTX", "LLAMA_API_KEY"]})
    out = vision_preflight()
    assert out["configured"] is False
    assert "OLLAMA_HOST, NUM_CTX, LLAMA_API_KEY" in out["warning"]
    assert "LLAMA_API_KEY" not in os.environ  # non-key entries block the config-key path


def test_unknown_provider_needs_no_key(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "github_copilot/gpt-4o")
    _stub_litellm(monkeypatch, lambda model, api_key=None: {"missing_keys": []})
    out = vision_preflight()
    assert out == {"model": "github_copilot/gpt-4o", "configured": True, "source": "config"}


def test_azure_resolves_via_validate_environment(monkeypatch, tmp_path):
    """Azure is not in _PROVIDER_KEY_ENV: it needs several env vars, which
    litellm.validate_environment lists (never a false configured:True)."""
    _fake_home(monkeypatch, tmp_path, '[models]\nrecorder = "azure/gpt-4o"\nrecorder_api_key = "cfg-azure-key"\n')
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "azure/gpt-4o")
    _stub_litellm(
        monkeypatch,
        lambda model, api_key=None: {"missing_keys": ["AZURE_API_KEY", "AZURE_API_BASE"]},
    )
    out = vision_preflight()
    assert out["configured"] is False
    assert "AZURE_API_KEY, AZURE_API_BASE" in out["warning"]
    assert "AZURE_API_KEY" not in os.environ  # nothing plumbed for a multi-var provider


def test_openai_provider_key_mapping(monkeypatch, tmp_path, scrub_provider_env):
    _fake_home(monkeypatch, tmp_path, '[models]\nrecorder = "openai/gpt-4o"\n')
    monkeypatch.setattr(config, "RECORDER_AI_MODEL", "openai/gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-for-tests")
    out = vision_preflight()
    assert out["configured"] is False  # an env var alone never configures vision
    assert out["warning"] == _OFF_NOTE
    assert os.environ["OPENAI_API_KEY"] == "sk-fake-for-tests"  # untouched without a config key
    # With a config key present, it overwrites the same-named env var.
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[models]\nrecorder = "openai/gpt-4o"\nrecorder_api_key = "cfg-openai-key"\n', encoding="utf-8"
    )
    out = vision_preflight()
    assert out["configured"] is True
    assert out["source"] == "config"
    assert os.environ["OPENAI_API_KEY"] == "cfg-openai-key"  # overwritten


def test_config_key_overrides_env(monkeypatch, tmp_path, scrub_provider_env):
    config_file = _fake_home(
        monkeypatch, tmp_path, '[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\nrecorder_api_key = "cfg-key"\n'
    )
    monkeypatch.setenv("GEMINI_API_KEY", "env-key-value")
    out = vision_preflight()
    assert out == {"model": _GEMINI_MODEL, "configured": True, "source": "config"}
    assert os.environ["GEMINI_API_KEY"] == "cfg-key"  # overwritten by the config key
    assert config_file.read_text(encoding="utf-8").count("cfg-key") == 1  # file untouched


def test_config_key_configures_and_plumbs_provider_env(monkeypatch, tmp_path, scrub_provider_env):
    _fake_home(
        monkeypatch,
        tmp_path,
        '[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\nrecorder_api_key = "cfg-secret-value"\n',
    )
    out = vision_preflight()
    assert out == {"model": _GEMINI_MODEL, "configured": True, "source": "config"}
    assert os.environ["GEMINI_API_KEY"] == "cfg-secret-value"  # plumbed for litellm
    assert "cfg-secret-value" not in str(out)  # the value is never echoed


def test_config_key_empty_or_blank_is_off(monkeypatch, tmp_path, scrub_provider_env):
    _fake_home(monkeypatch, tmp_path, '[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\nrecorder_api_key = ""\n')
    assert vision_preflight()["warning"] == _OFF_NOTE  # empty
    cfg = tmp_path / "config.toml"
    cfg.write_text('[models]\nrecorder = "x/y"\nrecorder_api_key = "   "\n', encoding="utf-8")
    assert vision_preflight()["warning"] == _OFF_NOTE  # blank


def test_placeholder_keys_get_their_own_note(monkeypatch, tmp_path, scrub_provider_env):
    for placeholder in ("PASTE_YOUR_GEMINI_API_KEY_HERE", "...", "Your-API-Key"):
        _fake_home(
            monkeypatch,
            tmp_path,
            f'[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\nrecorder_api_key = "{placeholder}"\n',
        )
        out = vision_preflight()
        assert out["configured"] is False, placeholder
        assert out["warning"] == _PLACEHOLDER_NOTE, placeholder
        assert "GEMINI_API_KEY" not in os.environ, placeholder  # nothing plumbed


def test_malformed_toml_note(monkeypatch, tmp_path, scrub_provider_env):
    _fake_home(monkeypatch, tmp_path, "not [valid toml")
    out = vision_preflight()
    assert out["configured"] is False
    assert out["warning"] == _TOML_NOTE


def test_key_is_reread_fresh_no_restart(monkeypatch, tmp_path, scrub_provider_env):
    config_file = _fake_home(monkeypatch, tmp_path, '[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\n')
    assert vision_preflight()["configured"] is False  # no key yet
    config_file.write_text(
        '[models]\nrecorder = "gemini/gemini-3.1-flash-lite"\nrecorder_api_key = "late-key-123"\n',
        encoding="utf-8",
    )
    out = vision_preflight()  # same process, same session file - no restart
    assert out["configured"] is True
    assert out["source"] == "config"
    assert os.environ["GEMINI_API_KEY"] == "late-key-123"
