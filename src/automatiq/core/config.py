"""
AutomatiQ — Global Configuration

Single source of truth for all paths, model identifiers, and tunables.
Import this module from anywhere in the project:

    from automatiq import config
    workspace = config.WORKSPACE_DIR

Priority chain:  CLI flag  >  ~/.automatiq/config.toml  >  hardcoded default
"""

import json
import re
import tomllib
from pathlib import Path

from dotenv import load_dotenv

VERSION = "0.3.2"

# ── Persistent user-level directory (~/.automatiq/) ──────────────────────────
# Stores binaries, logs, history, and user preferences across sessions.
HOME_DIR = Path.home() / ".automatiq"
BIN_DIR = HOME_DIR / "bin"
BROWSERS_DIR = HOME_DIR / "browsers"
LOGS_DIR = HOME_DIR / "logs"
HISTORY_DIR = HOME_DIR / "history"
CONFIG_FILE = HOME_DIR / "config.toml"
STATE_FILE = HOME_DIR / "state.json"


def load_state() -> dict:
    """Load persistent state from state.json."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    """Save persistent state to state.json."""
    try:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Per-project paths (CWD-relative) ────────────────────────────────────────
# .env is loaded from whichever directory the user runs `automatiq` in.
load_dotenv(Path.cwd() / ".env")

OUTPUT_DIR = Path.cwd() / "output"
WORKSPACE_DIR = OUTPUT_DIR / "workspace"

BLOCKLIST_DIR = OUTPUT_DIR / "blocklist"
BLOCKLIST_DB = OUTPUT_DIR / "blocklist.db"

# ── Models ───────────────────────────────────────────────────────────────────
AGENT_MODEL = "gemini/gemini-3.5-flash"
RECORDER_AI_MODEL = "gemini/gemini-3.1-flash-lite"

# Custom OpenAI-compatible endpoint (e.g. Ollama, LM Studio, vLLM).
# When set, litellm sends requests to this URL instead of the default provider.
# Use with --model openai/<model-name> (the openai/ prefix is required by litellm).
API_BASE = None

# ── Recorder proxy ───────────────────────────────────────────────────────────
# Route the recording browser through an HTTP/SOCKS proxy.
#   RECORDER_PROXY_SERVER   — static proxy URL, e.g. "http://user:pass@host:3128"
#                             or "socks5://host:1080".
#   RECORDER_PROXY_PROVIDER — optional "module:callable" that returns a proxy URL at
#                             launch time (for rotating/dynamic proxies). When set it
#                             takes precedence over RECORDER_PROXY_SERVER.
# Proxying is only applied when RECORDER_PROXY_ENABLED is true.
# Note: this only routes the recording browser's egress — LLM API calls,
# blocklist downloads, and agent tool HTTP are unaffected.
RECORDER_PROXY_ENABLED = False
RECORDER_PROXY_SERVER = None
RECORDER_PROXY_PROVIDER = None

# ── Recording tunables ───────────────────────────────────────────────────────
FPS = 3
SEGMENT_PAD_SECONDS = 2
MERGE_GAP_THRESHOLD_SECONDS = 1.5
MAX_FRAMES_PER_PROMPT = 8

# ── Blocklist sources ────────────────────────────────────────────────────────
BLOCKLIST_SOURCES = {}

# ── Browser ──────────────────────────────────────────────────────────────────
BROWSER_TYPE = "brave"  # "chrome", "brave", or "auto" — passed to zendriver Config
# Brave release channel to use when downloading a managed portable copy.
# Options: "release", "beta", "nightly".
BROWSER_CHANNEL = "release"
# Optional explicit path to a browser executable. When set, this overrides both
# zendriver's autodetect and the managed BROWSERS_DIR cache.
BROWSER_EXECUTABLE_PATH = None

# ── Agent tunables ───────────────────────────────────────────────────────────
MAX_AGENT_STEPS = 100
SANDBOX_TIMEOUT_SECONDS = 60

# ── Banner ───────────────────────────────────────────────────────────────────
BANNER_ENABLED = True
BANNER_SPEED = 1.0

VERBOSE = False

# ── Telemetry ────────────────────────────────────────────────────────────────
# Anonymous usage-volume telemetry.  Collects OS, version, command, session
# duration, token counts, and error types — never URLs, code, or persistent IDs.
TELEMETRY_ENABLED = True
TELEMETRY_ENDPOINT = "https://api.automatiq.run/v1/telemetry"

# Set to True only on the very first run (config.toml did not exist yet).
FIRST_RUN = False

# Set to True if the telemetry warning notice should be displayed at startup.
SHOW_TELEMETRY_NOTICE = False


# ── Default config.toml content ─────────────────────────────────────────────

_DEFAULT_CONFIG_TOML = """\
# AutomatiQ user configuration
#
# Values here override the built-in defaults.
# CLI flags (--model, --max-steps, etc.) override everything.

[browser]
# Which browser to use for recording. Options: chrome, brave, auto.
# "auto" auto-detects from installed browsers (Chrome preferred).
#
# Brave is the default because it ships built-in anti-fingerprinting and
# anti-tracking protections that help keep the recorder stealthy against
# websites. If Brave isn't found, AutomatiQ will offer to download a
# portable copy; declining falls back to whatever Chrome is installed.
type = "brave"

# Brave release channel for the managed portable download.
# Options: release, beta, nightly. Only used when type = "brave".
channel = "release"

# Optional explicit path to a browser executable. When set, this overrides
# both zendriver's autodetect and the managed ~/.automatiq/browsers cache.
# Example: "C:/Users/me/browsers/brave-v1.92.134/brave.exe"
# executable_path = ""

[models]
# LiteLLM model string for the investigator agent.
# Examples: openai/gpt-4o, anthropic/claude-sonnet-4-20250514, gemini/gemini-2.0-flash
agent    = "gemini/gemini-3.5-flash"

# Vision model for video-clip analysis during recording.
# Use a cheaper/faster model here to reduce cost.
recorder = "gemini/gemini-3.1-flash-lite"

# Custom OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, etc.).
# When set, all LLM requests are routed to this URL.
# Use with agent = "openai/<name>" (the openai/ prefix is required by litellm).
# base_url = "http://localhost:11434/v1"

[agent]
# Maximum agent loop iterations before giving up.
max_steps       = 100

# How long (seconds) a single IPython cell is allowed to run.
sandbox_timeout = 60

[recording]
# Frames per second for screen capture.
fps                     = 3

# Seconds of padding added around each action clip.
segment_pad             = 2

# Clips closer than this (seconds) are merged into one.
merge_gap_threshold     = 1.5

# Maximum frames sent per vision-model prompt.
max_frames_per_prompt   = 8

[banner]
# Set to false to disable the animated startup banner.
enabled = true

# Animation speed multiplier (2.0 = twice as fast, 0.5 = half speed).
speed   = 1.0

[output]
# Root directory for per-project output (workspace, blocklist).
# Relative paths are resolved from the directory where you run `automatiq`.
# dir = "output"

[recorder_proxy]
# Route the recording browser through an HTTP/SOCKS proxy.
# This only affects the recording browser's egress — LLM API calls,
# blocklist downloads, and agent tool HTTP are not proxied.
enabled  = false

# Static proxy URL. Examples:
#   server = "http://127.0.0.1:3128"
#   server = "http://user:pass@host:3128"
#   server = "socks5://127.0.0.1:1080"
# server = ""

# Optional dynamic provider "module:callable" returning a proxy URL at launch
# (for rotating proxies). Takes precedence over `server` when set.
#
# The callable takes no required arguments and returns a proxy URL string,
# e.g. "http://host:3128". A minimal rotating provider looks like:
#
#     # myproxies.py
#     import requests
#     def rotate() -> str:
#         requests.get("http://127.0.0.1:8000/rotate", timeout=30)
#         return "http://127.0.0.1:3128"
#
# Tip: NodeMaven (https://go.nodemaven.com/automatiqagentmd) is the project's
# preferred proxy partner — promo codes AUTOMATIQ35 (35% off Mobile/Residential)
# and AUTOMATIQ40 (40% off ISP/Static) are available for AutomatiQ users.
#
# provider = "myproxies:rotate"

[telemetry]
# Anonymous usage-volume telemetry.
# AutomatiQ collects anonymous metrics (OS, version, command used, session
# duration, token counts, error types) to help improve the tool.
# No URLs, code, personal data, or persistent identifiers are ever collected.
# Set to false to opt out.
enabled = true

# Endpoint URL. Change this only if you run your own telemetry server.
# endpoint = "https://api.automatiq.run/v1/telemetry"
"""


# ── Config schema migration ──────────────────────────────────────────────────


def _parse_toml_sections_and_keys(toml_text: str):
    """Parses a TOML string to group keys with their preceding comments by section."""
    sections = {}
    current_section = None
    accumulated_comments = []

    section_pat = re.compile(r"^\s*\[([a-zA-Z0-9_-]+)\]\s*(?:#.*)?$")
    key_val_pat = re.compile(r"^\s*(#?)\s*([a-zA-Z0-9_-]+)\s*=\s*(.*)$")

    for line in toml_text.splitlines():
        stripped = line.strip()

        m_sec = section_pat.match(stripped)
        if m_sec:
            current_section = m_sec.group(1)
            sections[current_section] = {}
            accumulated_comments = []
            continue

        if current_section is None:
            continue

        m_key = key_val_pat.match(stripped)
        if m_key:
            key_name = m_key.group(2)
            is_commented = bool(m_key.group(1))
            block_lines = accumulated_comments + [line]
            sections[current_section][key_name] = {
                "lines": block_lines,
                "commented": is_commented,
            }
            accumulated_comments = []
        elif stripped.startswith("#"):
            accumulated_comments.append(line)
        elif not stripped:
            accumulated_comments = []  # reset so comments don't span far apart

    return sections


def _get_existing_sections_and_keys(toml_text: str):
    """Scans user TOML to find active/commented keys present in each section."""
    sections = {}
    current_section = None

    section_pat = re.compile(r"^\s*\[([a-zA-Z0-9_-]+)\]\s*(?:#.*)?$")
    key_val_pat = re.compile(r"^\s*(#?)\s*([a-zA-Z0-9_-]+)\s*=\s*(.*)$")

    for line in toml_text.splitlines():
        stripped = line.strip()
        m_sec = section_pat.match(stripped)
        if m_sec:
            current_section = m_sec.group(1)
            sections[current_section] = set()
            continue

        if current_section is None:
            continue

        m_key = key_val_pat.match(stripped)
        if m_key:
            sections[current_section].add(m_key.group(2))

    return sections


def _ensure_latest_schema() -> None:
    """Intelligently merges missing keys and sections into the user's config.toml.

    Creates a .bak file of config.toml before making any changes.
    """
    try:
        user_text = CONFIG_FILE.read_text(encoding="utf-8")
    except OSError:
        return

    default_schema = _parse_toml_sections_and_keys(_DEFAULT_CONFIG_TOML)
    user_schema = _get_existing_sections_and_keys(user_text)
    user_lines = user_text.splitlines()

    section_pat = re.compile(r"^\s*\[([a-zA-Z0-9_-]+)\]\s*(?:#.*)?$")
    modified = False

    for section_name, default_keys in default_schema.items():
        # Case A: Entire section is missing
        if section_name not in user_schema:
            section_block_lines = [f"\n[{section_name}]"]
            for _key_name, key_info in default_keys.items():
                section_block_lines.extend(key_info["lines"])
            user_lines.extend(section_block_lines)
            modified = True
            continue

        # Case B: Section exists but check for missing keys
        user_keys = user_schema[section_name]
        missing_keys = [k for k in default_keys if k not in user_keys]
        if not missing_keys:
            continue

        # Find where this section starts & ends in user_lines
        start_idx = -1
        for idx, line in enumerate(user_lines):
            m = section_pat.match(line.strip())
            if m and m.group(1) == section_name:
                start_idx = idx
                break

        if start_idx == -1:
            continue

        end_idx = len(user_lines)
        for idx in range(start_idx + 1, len(user_lines)):
            m = section_pat.match(user_lines[idx].strip())
            if m:
                end_idx = idx
                break

        # Insert missing keys at the end of the section
        insert_lines = []
        for key_name in missing_keys:
            key_info = default_keys[key_name]
            insert_lines.append("")  # Spacer
            insert_lines.extend(key_info["lines"])

        user_lines[end_idx:end_idx] = insert_lines
        modified = True

    if modified:
        try:
            # Create a backup of the original config before writing updates
            backup_file = CONFIG_FILE.with_suffix(".toml.bak")
            backup_file.write_text(user_text, encoding="utf-8")
        except OSError:
            pass

        try:
            updated_text = "\n".join(user_lines) + ("\n" if user_text.endswith("\n") else "")
            CONFIG_FILE.write_text(updated_text, encoding="utf-8")
        except OSError:
            pass


# ── TOML loader ─────────────────────────────────────────────────────────────


def _load_config_toml():
    """Read ~/.automatiq/config.toml and apply values to module globals.

    Creates the file with commented defaults on first run.
    Migrates missing sections on upgrade.
    Silently skips if the file is missing or unparseable.
    """
    global AGENT_MODEL, RECORDER_AI_MODEL, API_BASE
    global RECORDER_PROXY_ENABLED, RECORDER_PROXY_SERVER, RECORDER_PROXY_PROVIDER
    global MAX_AGENT_STEPS, SANDBOX_TIMEOUT_SECONDS
    global FPS, SEGMENT_PAD_SECONDS, MERGE_GAP_THRESHOLD_SECONDS, MAX_FRAMES_PER_PROMPT
    global BANNER_ENABLED, BANNER_SPEED
    global BROWSER_TYPE, BROWSER_CHANNEL, BROWSER_EXECUTABLE_PATH
    global OUTPUT_DIR, WORKSPACE_DIR, BLOCKLIST_DIR, BLOCKLIST_DB
    global TELEMETRY_ENABLED, TELEMETRY_ENDPOINT, FIRST_RUN

    if not CONFIG_FILE.exists():
        try:
            HOME_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(_DEFAULT_CONFIG_TOML, encoding="utf-8")
            FIRST_RUN = True
        except OSError:
            pass
        return

    # Migrate: append any missing sections (e.g. [telemetry]) to old configs.
    _ensure_latest_schema()

    try:
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return

    # [models]
    models = data.get("models", {})
    if "agent" in models:
        AGENT_MODEL = str(models["agent"])
    if "recorder" in models:
        RECORDER_AI_MODEL = str(models["recorder"])
    if "base_url" in models:
        API_BASE = str(models["base_url"])

    # [recorder_proxy]
    proxy = data.get("recorder_proxy", {})
    if "enabled" in proxy:
        RECORDER_PROXY_ENABLED = bool(proxy["enabled"])
    if "server" in proxy:
        RECORDER_PROXY_SERVER = str(proxy["server"]) or None
    if "provider" in proxy:
        RECORDER_PROXY_PROVIDER = str(proxy["provider"]) or None

    # [browser]
    browser = data.get("browser", {})
    if "type" in browser:
        BROWSER_TYPE = str(browser["type"])
    if "channel" in browser:
        BROWSER_CHANNEL = str(browser["channel"])
    if "executable_path" in browser:
        BROWSER_EXECUTABLE_PATH = str(browser["executable_path"]) or None

    # [agent]
    agent = data.get("agent", {})
    if "max_steps" in agent:
        MAX_AGENT_STEPS = int(agent["max_steps"])
    if "sandbox_timeout" in agent:
        SANDBOX_TIMEOUT_SECONDS = int(agent["sandbox_timeout"])

    # [recording]
    rec = data.get("recording", {})
    if "fps" in rec:
        FPS = int(rec["fps"])
    if "segment_pad" in rec:
        SEGMENT_PAD_SECONDS = float(rec["segment_pad"])
    if "merge_gap_threshold" in rec:
        MERGE_GAP_THRESHOLD_SECONDS = float(rec["merge_gap_threshold"])
    if "max_frames_per_prompt" in rec:
        MAX_FRAMES_PER_PROMPT = int(rec["max_frames_per_prompt"])

    # [banner]
    banner = data.get("banner", {})
    if "enabled" in banner:
        BANNER_ENABLED = bool(banner["enabled"])
    if "speed" in banner:
        BANNER_SPEED = float(banner["speed"])

    # [output]
    output = data.get("output", {})
    if "dir" in output:
        OUTPUT_DIR = Path(output["dir"]).resolve()
        WORKSPACE_DIR = OUTPUT_DIR / "workspace"
        BLOCKLIST_DIR = OUTPUT_DIR / "blocklist"
        BLOCKLIST_DB = OUTPUT_DIR / "blocklist.db"

    # [telemetry]
    telemetry = data.get("telemetry", {})
    if "enabled" in telemetry:
        TELEMETRY_ENABLED = bool(telemetry["enabled"])
    if "endpoint" in telemetry:
        TELEMETRY_ENDPOINT = str(telemetry["endpoint"]) or TELEMETRY_ENDPOINT


_load_config_toml()

SHOW_TELEMETRY_NOTICE = TELEMETRY_ENABLED and not load_state().get("telemetry_notice_shown", False)


def ensure_system_dirs():
    for d in (HOME_DIR, BIN_DIR, BROWSERS_DIR, LOGS_DIR, HISTORY_DIR):
        d.mkdir(parents=True, exist_ok=True)


def ensure_output_dirs():
    ensure_system_dirs()
    for d in (OUTPUT_DIR, WORKSPACE_DIR, BLOCKLIST_DIR):
        d.mkdir(parents=True, exist_ok=True)
