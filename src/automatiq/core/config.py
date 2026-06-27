"""
AutomatiQ — Global Configuration

Single source of truth for all paths, model identifiers, and tunables.
Import this module from anywhere in the project:

    from automatiq import config
    workspace = config.WORKSPACE_DIR

Priority chain:  CLI flag  >  ~/.automatiq/config.toml  >  hardcoded default
"""

import tomllib
from pathlib import Path

from dotenv import load_dotenv

VERSION = "0.2.2a1"

# ── Persistent user-level directory (~/.automatiq/) ──────────────────────────
# Stores binaries, logs, history, and user preferences across sessions.
HOME_DIR = Path.home() / ".automatiq"
BIN_DIR = HOME_DIR / "bin"
LOGS_DIR = HOME_DIR / "logs"
HISTORY_DIR = HOME_DIR / "history"
CONFIG_FILE = HOME_DIR / "config.toml"

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
BLOCKLIST_SOURCES = {
    "stevenblack": "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
    "adaway": "https://raw.githubusercontent.com/AdAway/adaway.github.io/master/hosts.txt",
}

# ── Agent tunables ───────────────────────────────────────────────────────────
MAX_AGENT_STEPS = 100
SANDBOX_TIMEOUT_SECONDS = 60

# ── Banner ───────────────────────────────────────────────────────────────────
BANNER_ENABLED = True
BANNER_SPEED = 1.0

VERBOSE = False


# ── Default config.toml content ─────────────────────────────────────────────

_DEFAULT_CONFIG_TOML = """\
# AutomatiQ user configuration
#
# Values here override the built-in defaults.
# CLI flags (--model, --max-steps, etc.) override everything.

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
"""


# ── TOML loader ─────────────────────────────────────────────────────────────


def _load_config_toml():
    """Read ~/.automatiq/config.toml and apply values to module globals.

    Creates the file with commented defaults on first run.
    Silently skips if the file is missing or unparseable.
    """
    global AGENT_MODEL, RECORDER_AI_MODEL, API_BASE
    global RECORDER_PROXY_ENABLED, RECORDER_PROXY_SERVER, RECORDER_PROXY_PROVIDER
    global MAX_AGENT_STEPS, SANDBOX_TIMEOUT_SECONDS
    global FPS, SEGMENT_PAD_SECONDS, MERGE_GAP_THRESHOLD_SECONDS, MAX_FRAMES_PER_PROMPT
    global BANNER_ENABLED, BANNER_SPEED
    global OUTPUT_DIR, WORKSPACE_DIR, BLOCKLIST_DIR, BLOCKLIST_DB

    if not CONFIG_FILE.exists():
        try:
            HOME_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(_DEFAULT_CONFIG_TOML, encoding="utf-8")
        except OSError:
            pass
        return

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


_load_config_toml()


def ensure_system_dirs():
    for d in (HOME_DIR, BIN_DIR, LOGS_DIR, HISTORY_DIR):
        d.mkdir(parents=True, exist_ok=True)


def ensure_output_dirs():
    ensure_system_dirs()
    for d in (OUTPUT_DIR, WORKSPACE_DIR, BLOCKLIST_DIR):
        d.mkdir(parents=True, exist_ok=True)
