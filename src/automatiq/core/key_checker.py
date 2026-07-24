"""
API key validation for AutomatiQ.

Runs before any recorder or agent work begins to give the user a clear,
actionable error if their environment is missing keys or has invalid ones —
rather than failing deep inside the agent loop with a cryptic API error.
"""

import logging
import os
import sys

import litellm

from . import config, events

logger = logging.getLogger(__name__)


def _validate_model(model: str, api_key: str | None = None) -> bool:
    """
    Validate that the environment has what it needs to call `model`.

    Uses litellm.validate_environment() to check all required env vars are
    present. This is the only reliable cross-provider check — litellm's
    check_valid_key() is only trustworthy for OpenAI and returns false
    negatives for Gemini, Anthropic, and others.

    When *api_key* is a non-None value (e.g. ``"not-required"`` for a local
    endpoint), litellm's built-in substring filter at
    ``litellm/utils.py:filter_missing_keys`` strips any ``*_API_KEY`` entries
    from ``missing_keys``, so the check passes for keyless local servers
    without us hand-rolling per-provider logic.

    Returns True if everything looks good, False otherwise (errors already
    printed).
    """
    result = litellm.validate_environment(model, api_key=api_key)

    missing = result.get("missing_keys", [])
    if missing:
        events.log_error.send("key_checker", text=f"Model '{model}' requires environment variables that are not set:")
        for key in missing:
            events.log_info.send("key_checker", text=f"  {key}")
        events.log_info.send("key_checker", text="Set them in your .env file or export them before running.")
        return False

    return True


def check_api_keys(*models: str) -> None:
    """
    Validate API keys for one or more models.

    Prints a summary and exits with code 1 if any model fails validation.
    Call this at the start of every subcommand, after config overrides are applied.

    Usage:
        check_api_keys(config.AGENT_MODEL)
        check_api_keys(config.AGENT_MODEL, config.RECORDER_AI_MODEL)
    """
    # GitHub Copilot uses OAuth device flow — no env key to validate
    if any(m.startswith("github_copilot/") for m in models):
        events.log_info.send(
            "key_checker", text="GitHub Copilot detected — skipping key validation (uses OAuth device flow)."
        )
        return

    # When a custom base URL is set, requests go to a user-controlled endpoint
    # (Ollama, vLLM, LM Studio).  Standard provider API keys are not required.
    # We pass a dummy api_key to litellm's validate_environment so its built-in
    # substring filter clears *_API_KEY from missing_keys.  If the user DID set
    # OPENAI_API_KEY (some local servers require auth), the real key is forwarded.
    effective_api_key: str | None = None
    if config.API_BASE:
        effective_api_key = os.environ.get("OPENAI_API_KEY") or "not-required"
        events.log_info.send("key_checker", text=f"Custom endpoint ({config.API_BASE}) — key validation relaxed.")

    # Catch obviously malformed model strings before hitting litellm
    for m in models:
        if "/" not in m:
            hint = ""
            if config.API_BASE:
                hint = (
                    f"\nSince base_url is set ({config.API_BASE}), this looks like a local model. "
                    f"Prefix it with 'openai/' (e.g. 'openai/{m}')."
                )
            events.log_error.send(
                "key_checker",
                text=(
                    f"Invalid model string '{m}'. Expected format: 'provider/model-name' "
                    f"(e.g. 'gemini/gemini-2.5-flash').{hint}\n"
                    f"Check [models] agent in ~/.automatiq/config.toml."
                ),
            )
            sys.exit(1)

    failed: list[str] = []
    seen: set[str] = set()

    for model in models:
        if model in seen:
            continue
        seen.add(model)

        ok = _validate_model(model, api_key=effective_api_key)
        if not ok:
            failed.append(model)

    if failed:
        events.log_error.send("key_checker", text=f"{len(failed)} model(s) failed key validation — cannot continue.")
        for m in failed:
            events.log_info.send("key_checker", text=f"  {m}")
        sys.exit(1)

    # events.log_info.send("key_checker", text="[SUCCESS] API keys validated.")
