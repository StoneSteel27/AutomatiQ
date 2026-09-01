"""AutomatiQ recorder MCP — configuration.

Resolution chain for every tunable: ``AUTOMATIQ_*`` environment variable >
``~/.automatiq/config.toml`` > hardcoded default. The config file is
auto-created from a commented template on first run and schema-migrated on
load (missing keys/sections are appended; the original is backed up as
``config.toml.bak``), so upgrades never lose user edits.

Persistent user-level state lives under ``~/.automatiq/`` (config, browser
binaries, managed browsers, blocklist). Per-session output directories
are created beneath ``OUTPUT_DIR`` (default ``<cwd>/automatiq_sessions``)
by the MCP runtime, which always passes an explicit session root to the
compile pipeline.
"""

import logging
import os
import re
import tomllib
from pathlib import Path

VERSION = "0.4.0"

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── Persistent user-level directory (~/.automatiq/) ──────────────────────────
# Stores browser binaries and the shared blocklist across sessions.
HOME_DIR = Path(os.environ.get("AUTOMATIQ_HOME", str(Path.home() / ".automatiq")))
BIN_DIR = HOME_DIR / "bin"
BROWSERS_DIR = HOME_DIR / "browsers"
LOGS_DIR = HOME_DIR / "logs"
CONFIG_FILE = HOME_DIR / "config.toml"

# ── Logging ──────────────────────────────────────────────────────────────────
# Min level for the stderr console; the per-session file under LOGS_DIR
# always records DEBUG+ (two-tier logging: console minimized, file verbose).
LOG_LEVEL = os.environ.get("AUTOMATIQ_LOG_LEVEL", "INFO").upper()

# ── Output root ──────────────────────────────────────────────────────────────
# Per-session directories (<name>/workspace/session_dump/...) are created
# beneath this root at compile time.
OUTPUT_DIR = Path(os.environ.get("AUTOMATIQ_OUTPUT_DIR", str(Path.cwd() / "automatiq_sessions")))

# ── Blocklist (persistent, shared across sessions) ───────────────────────────
# Source list format: "name1=url1,name2=url2" — empty disables the blocklist.
BLOCKLIST_SOURCES: dict[str, str] = {}
for _pair in filter(None, (p.strip() for p in os.environ.get("AUTOMATIQ_BLOCKLIST_SOURCES", "").split(","))):
    _name, _sep, _url = _pair.partition("=")
    if _sep and _name and _url:
        BLOCKLIST_SOURCES[_name] = _url
BLOCKLIST_DIR = HOME_DIR / "blocklist"
BLOCKLIST_DB = HOME_DIR / "blocklist.db"

# ── Models ───────────────────────────────────────────────────────────────────
RECORDER_AI_MODEL = os.environ.get("AUTOMATIQ_RECORDER_MODEL", "gemini/gemini-3.1-flash-lite")
# Custom OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, ...). When set,
# litellm routes requests there; combine with a "openai/<model>" model string.
API_BASE = os.environ.get("AUTOMATIQ_API_BASE") or None

# ── Recorder proxy ───────────────────────────────────────────────────────────
# Routes only the recording browser's egress — LLM API calls and blocklist
# downloads are unaffected. Provider is a "module:callable" returning a proxy
# URL at launch time (rotating proxies); it takes precedence over SERVER.
RECORDER_PROXY_ENABLED = _env_flag("AUTOMATIQ_RECORDER_PROXY_ENABLED")
RECORDER_PROXY_SERVER = os.environ.get("AUTOMATIQ_RECORDER_PROXY_SERVER") or None
RECORDER_PROXY_PROVIDER = os.environ.get("AUTOMATIQ_RECORDER_PROXY_PROVIDER") or None

# ── Recording tunables ───────────────────────────────────────────────────────
FPS = int(os.environ.get("AUTOMATIQ_FPS", "3"))
SEGMENT_PAD_SECONDS = float(os.environ.get("AUTOMATIQ_SEGMENT_PAD", "2"))
MERGE_GAP_THRESHOLD_SECONDS = float(os.environ.get("AUTOMATIQ_MERGE_GAP", "1.5"))
MAX_FRAMES_PER_PROMPT = int(os.environ.get("AUTOMATIQ_MAX_FRAMES_PER_PROMPT", "8"))

# ── Browser ──────────────────────────────────────────────────────────────────
# Brave-only recorder: no browser-type knob.
BROWSER_CHANNEL = os.environ.get("AUTOMATIQ_BROWSER_CHANNEL", "release")
BROWSER_EXECUTABLE_PATH = os.environ.get("AUTOMATIQ_BROWSER_EXECUTABLE_PATH") or None

# ── Telemetry ────────────────────────────────────────────────────────────────
# Anonymous usage-volume telemetry (OS, version, duration, counts, error
# types — never URLs, code, or persistent IDs). Opt out with 0/false.
TELEMETRY_ENABLED = _env_flag("AUTOMATIQ_TELEMETRY", default="1")
TELEMETRY_ENDPOINT = os.environ.get("AUTOMATIQ_TELEMETRY_ENDPOINT", "https://api.automatiq.run/v1/telemetry")

# ── First-run flag ───────────────────────────────────────────────────────────
# Set by the TOML loader when it creates config.toml fresh.
FIRST_RUN = False

# ── Embedded config template (schema source of truth) ────────────────────────
# Uncommented keys are the migration schema; commented-out keys are docs only.
_DEFAULT_CONFIG_TOML = """\
# AutomatiQ MCP user configuration
#
# Settings here override built-in defaults.
# AUTOMATIQ_* environment variables override this file.
# Edits apply on the next server start (recorder_api_key: next recording).
# Env-only settings: output dir (AUTOMATIQ_OUTPUT_DIR), browser path
# (AUTOMATIQ_BROWSER_EXECUTABLE_PATH).

[models]
# Vision model for video-clip analysis, as a LiteLLM string.
# Check your provider's exact model-string format at
# https://docs.litellm.ai/docs/providers
# Examples: gemini/gemini-3.1-flash-lite (default), openai/gpt-4o-mini,
# anthropic/claude-sonnet-4-20250514
recorder = "gemini/gemini-3.1-flash-lite"

# API key for the vision model's provider. Paste your key between the
# quotes - it never leaves this machine and is never logged.
# The key for whatever provider `recorder` names goes here (any litellm provider).
recorder_api_key = ""

# Custom OpenAI-compatible endpoint for local models (Ollama
# http://localhost:11434/v1, LM Studio http://localhost:1234/v1, vLLM).
# Empty string = provider default. When set, use recorder = "openai/<name>"
# (litellm requires the openai/ prefix). Local keyless servers need no key.
base_url = ""

[recording]
# Frames per second for screen capture.
fps = 3
# Seconds of padding added around each action clip.
segment_pad = 2
# Clips closer than this (seconds) are merged into one.
merge_gap_threshold = 1.5
# Maximum frames sent per vision-model prompt.
max_frames_per_prompt = 8

[browser]
# The recorder is Brave-only (built-in anti-fingerprinting keeps it stealthy).
# Brave release channel: release | beta | nightly
channel = "release"
# Explicit browser binary path: set AUTOMATIQ_BROWSER_EXECUTABLE_PATH (env) instead.

[recorder_proxy]
# Route the recording browser through an HTTP/SOCKS proxy. Only browser
# egress is proxied - LLM API calls and blocklist downloads are unaffected.
# start_recording's `proxy` tool parameter overrides this.
enabled = false
# server = "http://127.0.0.1:3128" - http://host:port or socks5://user:pass@host:port
# provider = "mymodule:callable" - optional "module:callable" returning a proxy URL at launch; overrides server.

[telemetry]
# Anonymous usage-volume telemetry (no URLs, code, or personal data).
# Set to false to opt out.
enabled = true
"""

# Migration schema: section -> {key -> the "key = default" line to insert}.
_SCHEMA_SECTIONS: dict[str, dict[str, str]] = {
    "models": {
        "recorder": 'recorder = "gemini/gemini-3.1-flash-lite"',
        "recorder_api_key": 'recorder_api_key = ""',
        "base_url": 'base_url = ""',
    },
    "recording": {
        "fps": "fps = 3",
        "segment_pad": "segment_pad = 2",
        "merge_gap_threshold": "merge_gap_threshold = 1.5",
        "max_frames_per_prompt": "max_frames_per_prompt = 8",
    },
    "browser": {
        "channel": 'channel = "release"',
    },
    "recorder_proxy": {
        "enabled": "enabled = false",
    },
    "telemetry": {
        "enabled": "enabled = true",
    },
}


def _file_value(section: dict, key: str, env_name: str, current, coerce):
    """``env > file > default`` resolution for one knob.

    Env var set -> keep the env-derived *current*. TOML *key* present in
    *section* -> the coerced file value. Coercion failure -> keep *current*
    (defensive: a bad value must never crash startup). Otherwise keep the
    default-derived *current*.
    """
    if env_name and os.environ.get(env_name):
        return current
    if key in section:
        try:
            return coerce(section[key])
        except (TypeError, ValueError):
            logger.warning("config.toml: ignored invalid value for key '%s' - using default", key)
            return current
    return current


def _template_section_block(section: str) -> str:
    """The full commented template text of one [section] (header to next)."""
    lines = _DEFAULT_CONFIG_TOML.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == f"[{section}]":
            start = i
            break
    if start is None:
        return f"[{section}]\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = j
            break
    return "\n".join(lines[start:end])


def _ensure_latest_schema() -> None:
    """Migrate config.toml to the current schema (best-effort, silent-fail).

    Missing sections are appended with their full commented template block;
    missing keys are inserted as ``key = default`` right after their section
    header. All existing user lines and comments are preserved; the original
    is backed up as ``config.toml.bak`` before any change. Unknown user
    sections (e.g. [agent]) are left untouched.
    """
    try:
        user_text = CONFIG_FILE.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        data = tomllib.loads(user_text)
    except Exception:
        return  # malformed: leave it alone; the loader falls back to defaults

    lines = user_text.splitlines()
    modified = False

    # Missing keys inside existing sections: insert after the section header.
    for section, keys in _SCHEMA_SECTIONS.items():
        if section not in data:
            continue
        present = data[section] if isinstance(data[section], dict) else {}
        missing = [k for k in keys if k not in present]
        if not missing:
            continue
        header_pat = re.compile(rf"^\s*\[{re.escape(section)}\]\s*(?:#.*)?$")
        for idx, line in enumerate(lines):
            if header_pat.match(line):
                insert_at = idx + 1
                for key_line in reversed([keys[k] for k in missing]):
                    lines.insert(insert_at, key_line)
                modified = True
                break

    # Missing sections: append the full commented template block at EOF.
    for section in _SCHEMA_SECTIONS:
        if section not in data:
            lines.append("")
            lines.extend(_template_section_block(section).split("\n"))
            modified = True

    if not modified:
        return

    try:
        CONFIG_FILE.with_suffix(".toml.bak").write_text(user_text, encoding="utf-8")
    except OSError as exc:
        # Never overwrite a user config without a backup.
        logger.warning("config.toml: could not write backup config.toml.bak (%s) - migration aborted", exc)
        return
    try:
        CONFIG_FILE.write_text("\n".join(lines) + ("\n" if user_text.endswith("\n") else ""), encoding="utf-8")
    except OSError:
        return
    logger.info("config.toml migrated to current schema (backup: config.toml.bak)")


def _load_config_toml() -> None:
    """Read ~/.automatiq/config.toml and apply values to module globals.

    Creates the file from the embedded template on first run (FIRST_RUN).
    Schema-migrates missing sections/keys (original saved as .bak). Silently
    skips on OSError or TOML parse errors - startup must never crash on file
    problems; defaults already applied by normal global initialization.
    Resolution per knob: AUTOMATIQ_* env var > config.toml > default.
    """
    global RECORDER_AI_MODEL, API_BASE
    global FPS, SEGMENT_PAD_SECONDS, MERGE_GAP_THRESHOLD_SECONDS, MAX_FRAMES_PER_PROMPT
    global BROWSER_CHANNEL, TELEMETRY_ENABLED, FIRST_RUN
    global RECORDER_PROXY_ENABLED, RECORDER_PROXY_SERVER, RECORDER_PROXY_PROVIDER

    if not CONFIG_FILE.exists():
        try:
            HOME_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(_DEFAULT_CONFIG_TOML, encoding="utf-8")
            FIRST_RUN = True
        except OSError:
            pass
        return

    _ensure_latest_schema()

    try:
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return

    models = data.get("models", {}) if isinstance(data.get("models"), dict) else {}
    RECORDER_AI_MODEL = _file_value(models, "recorder", "AUTOMATIQ_RECORDER_MODEL", RECORDER_AI_MODEL, str)
    API_BASE = _file_value(models, "base_url", "AUTOMATIQ_API_BASE", API_BASE, str) or None

    recording = data.get("recording", {}) if isinstance(data.get("recording"), dict) else {}
    FPS = _file_value(recording, "fps", "AUTOMATIQ_FPS", FPS, int)
    SEGMENT_PAD_SECONDS = _file_value(recording, "segment_pad", "AUTOMATIQ_SEGMENT_PAD", SEGMENT_PAD_SECONDS, float)
    MERGE_GAP_THRESHOLD_SECONDS = _file_value(
        recording, "merge_gap_threshold", "AUTOMATIQ_MERGE_GAP", MERGE_GAP_THRESHOLD_SECONDS, float
    )
    MAX_FRAMES_PER_PROMPT = _file_value(
        recording, "max_frames_per_prompt", "AUTOMATIQ_MAX_FRAMES_PER_PROMPT", MAX_FRAMES_PER_PROMPT, int
    )

    browser = data.get("browser", {}) if isinstance(data.get("browser"), dict) else {}
    BROWSER_CHANNEL = _file_value(browser, "channel", "AUTOMATIQ_BROWSER_CHANNEL", BROWSER_CHANNEL, str)

    proxy_sec = data.get("recorder_proxy", {}) if isinstance(data.get("recorder_proxy"), dict) else {}
    RECORDER_PROXY_ENABLED = _file_value(
        proxy_sec, "enabled", "AUTOMATIQ_RECORDER_PROXY_ENABLED", RECORDER_PROXY_ENABLED, bool
    )
    RECORDER_PROXY_SERVER = (
        _file_value(proxy_sec, "server", "AUTOMATIQ_RECORDER_PROXY_SERVER", RECORDER_PROXY_SERVER, str) or None
    )
    RECORDER_PROXY_PROVIDER = (
        _file_value(proxy_sec, "provider", "AUTOMATIQ_RECORDER_PROXY_PROVIDER", RECORDER_PROXY_PROVIDER, str) or None
    )

    telemetry = data.get("telemetry", {}) if isinstance(data.get("telemetry"), dict) else {}
    TELEMETRY_ENABLED = _file_value(telemetry, "enabled", "AUTOMATIQ_TELEMETRY", TELEMETRY_ENABLED, bool)


def read_recorder_api_key() -> tuple[str | None, bool]:
    """FRESH re-parse of CONFIG_FILE; returns ``(key_value, parse_ok)``.

    Powers no-restart key edits: preflight calls this per session instead of
    relying on a cached import-time value. parse_ok is False on OSError,
    TOML errors, or a missing file. key_value is None when the key
    is absent, empty, or not a string. The value must never be logged or
    echoed.
    """
    try:
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return None, False
    models = data.get("models", {})
    if not isinstance(models, dict):
        return None, True
    key = models.get("recorder_api_key")
    if not isinstance(key, str) or not key.strip():
        return None, True
    return key, True


_load_config_toml()


def ensure_system_dirs() -> None:
    for d in (HOME_DIR, BIN_DIR, BROWSERS_DIR, LOGS_DIR, BLOCKLIST_DIR):
        d.mkdir(parents=True, exist_ok=True)
