"""Vision-model preflight and skip/warning blocks shared by recording and re-annotation."""

import os

from automatiq.core import config

# Vision-LLM preflight: which env var litellm reads per litellm provider
# prefix. These vars are never READ as a key source (provider env vars set in
# the host environment are ignored); they are only WRITTEN to plumb the
# recorder_api_key from config.toml into the env litellm reads. Custom
# OpenAI-compatible endpoints (API_BASE) need no key. Azure is deliberately
# NOT here: it needs several env vars (AZURE_API_KEY/BASE/VERSION), which
# litellm.validate_environment lists via the unknown-provider branch.
_PROVIDER_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
}


# Vision-key state notes (verbatim single lines surfaced in tool results).
_VISION_TOML_NOTE = (
    "~/.automatiq/config.toml could not be read or parsed as TOML - vision "
    "annotation OFF and file settings ignored. Fix the file or its permissions, then record again."
)
_VISION_PLACEHOLDER_NOTE = (
    "~/.automatiq/config.toml still contains a placeholder recorder_api_key - "
    "replace it with a real key under [models] to enable vision annotation."
)
_VISION_OFF_NOTE = (
    "Vision annotation is OFF - clips will not be AI-analyzed. Enable: paste your "
    "key into recorder_api_key under [models] in ~/.automatiq/config.toml - takes "
    "effect on the next recording, no restart needed."
)

# Values agents tend to paste verbatim from instruction snippets instead of a
# real key; compared stripped + lowercased.
_PLACEHOLDER_API_KEYS = {
    "..",
    "...",
    "your_api_key",
    "your-api-key",
    "paste_your_api_key_here",
    "paste-your-api-key-here",
    "paste_your_gemini_api_key_here",
    "paste-your-gemini-api-key-here",
    "apikey",
    "xxx",
}

# Terminal vision-summary states (state, never questions; never the key value).
_VISION_SKIPPED_DETAIL = "no key - set recorder_api_key in ~/.automatiq/config.toml"
_VISION_VIDEO_DISABLED_DETAIL = "video disabled (include_video=false)"
_VISION_AUTH_REASON = "key rejected - check recorder_api_key in ~/.automatiq/config.toml"
_VISION_ABORT_REASON = "vision analysis aborted after first failure (see session log)"

# vision_state["skip_reason"] values (machine-readable, set by the runtime).
_SKIP_NO_KEY = "no_key"
_SKIP_VIDEO_DISABLED = "video_disabled"


def _provider_of(model_str: str) -> str:
    """litellm provider prefix of a model string ("openai/gpt-4o" -> "openai")."""
    return model_str.split("/", 1)[0].lower() if "/" in model_str else model_str.lower()


def vision_preflight() -> dict:
    """Cheap check whether the vision LLM is usable with current config.

    Resolution chain:

    0. the model string must be ``provider/model-name`` — a bare string is
       invalid (with ``config.API_BASE`` set it is almost certainly a local
       model, so the warning hints at the ``openai/`` prefix);
    1. ``config.API_BASE`` set (custom/local endpoint) -> ``source:
       "base_url"``. A real ``recorder_api_key`` is hard-set into
       ``OPENAI_API_KEY`` (overwriting any host value) for authed endpoints;
       keyless local servers get ``OPENAI_API_KEY="not-required"`` so a stray
       host env var can never leak into the analyzer's env read;
    2. known provider + ``recorder_api_key`` under ``[models]`` in
       ``~/.automatiq/config.toml`` (read FRESH via
       :func:`config.read_recorder_api_key`) -> ``source: "config"``; the key
       is plumbed into the provider's env var (hard overwrite) for the
       remainder of the process. The value itself is never logged or returned;
    3. unknown provider: ``litellm.validate_environment(model)`` decides
       offline — no missing env vars -> ``source: "config"`` (keyless
       provider); a single missing ``*_API_KEY`` satisfied by the config key
       -> plumbed (hard overwrite) + ``source: "config"``; anything else
       missing -> not configured, with the missing variables listed;
       known providers without a config key are not configured (unparseable
       TOML / placeholder / empty notes).

    Provider env vars set in the host environment are never consulted as a
    key source; when a config key exists it overwrites the corresponding
    process env var.

    The model comes from ``config.RECORDER_AI_MODEL`` (resolved at import
    time): model edits in config.toml need a server restart, key edits do
    not. Pure config/env/file inspection - no network calls.
    """
    from automatiq.core import config

    model = config.RECORDER_AI_MODEL or ""
    provider = _provider_of(model)

    # 0. Bare model strings cannot be dispatched by litellm.
    if "/" not in model:
        warning = (
            f"Invalid model string '{model}'. Expected format: 'provider/model-name' "
            "(e.g. 'gemini/gemini-3.1-flash-lite')."
        )
        if config.API_BASE:
            warning += (
                f" Since base_url is set ({config.API_BASE}), this looks like a local model. "
                f"Prefix it with 'openai/' (e.g. 'openai/{model}')."
            )
        return {"model": model, "configured": False, "warning": warning}

    # 1. Custom/local endpoint: litellm routes to config.API_BASE; a config
    # key is hard-set for authed endpoints, keyless locals get a sentinel so
    # a stray host OPENAI_API_KEY can never leak into the analyzer's env read.
    if config.API_BASE:
        key, ok = config.read_recorder_api_key()
        stripped = (key or "").strip() if ok else ""
        if stripped and stripped.lower() not in _PLACEHOLDER_API_KEYS:
            os.environ["OPENAI_API_KEY"] = stripped
        else:
            os.environ["OPENAI_API_KEY"] = "not-required"
        return {"model": model, "configured": True, "source": "base_url"}

    key_env = _PROVIDER_KEY_ENV.get(provider)

    # 2. Known provider: recorder_api_key from config.toml (fresh re-read).
    if key_env is not None:
        key, ok = config.read_recorder_api_key()
        if not ok:
            return {"model": model, "configured": False, "warning": _VISION_TOML_NOTE}
        stripped = (key or "").strip()
        if stripped and stripped.lower() not in _PLACEHOLDER_API_KEYS:
            os.environ[key_env] = stripped
            return {"model": model, "configured": True, "source": "config"}
        if stripped:
            return {"model": model, "configured": False, "warning": _VISION_PLACEHOLDER_NOTE}
        return {"model": model, "configured": False, "warning": _VISION_OFF_NOTE}

    # 3. Unknown provider: let litellm tell us what the model needs (offline).
    try:
        from litellm import validate_environment

        result = validate_environment(model)
        missing = [str(k) for k in result.get("missing_keys", [])]
    except Exception:
        return {
            "model": model,
            "configured": False,
            "warning": (
                f"unknown provider '{provider}' in model '{model}' - set AUTOMATIQ_API_BASE "
                "for a custom endpoint or use a known provider prefix"
            ),
        }

    if not missing:
        # The provider needs no env key (OAuth-style, e.g. github_copilot).
        return {"model": model, "configured": True, "source": "config"}

    # A single missing *_API_KEY can be satisfied from config.toml.
    if len(missing) == 1 and missing[0].endswith("_API_KEY"):
        key, ok = config.read_recorder_api_key()
        stripped = (key or "").strip() if ok else ""
        if stripped and stripped.lower() not in _PLACEHOLDER_API_KEYS:
            os.environ[missing[0]] = stripped
            return {"model": model, "configured": True, "source": "config"}

    return {
        "model": model,
        "configured": False,
        "warning": (
            f"Model '{model}' requires environment variables that are not set: "
            f"{', '.join(missing)}. Set them in your environment or add "
            "recorder_api_key under [models] in ~/.automatiq/config.toml."
        ),
    }


def _vision_summary_block(vision_state: dict) -> dict:
    """Build the terminal ``vision`` status block from compile-time state.

    *vision_state* carries ``configured`` (key present at session start and
    video requested), ``model`` (the model the analysis uses, resolved at
    session start), an optional ``skip_reason``, and the analyzer counters
    filled in by the compile pipeline (``analyzed`` / ``failed`` /
    ``fatal_reason``).
    """
    if not vision_state.get("configured"):
        if vision_state.get("skip_reason") == _SKIP_VIDEO_DISABLED:
            return {"state": "skipped", "detail": _VISION_VIDEO_DISABLED_DETAIL}
        return {"state": "skipped", "detail": _VISION_SKIPPED_DETAIL}
    analyzed = int(vision_state.get("analyzed", 0))
    failed = int(vision_state.get("failed", 0))
    reason = vision_state.get("fatal_reason")
    if reason == "auth":
        return {"state": "failed", "reason": _VISION_AUTH_REASON, "analyzed": analyzed, "failed": failed}
    if reason == "other":
        return {"state": "failed", "reason": _VISION_ABORT_REASON, "analyzed": analyzed, "failed": failed}
    model = str(vision_state.get("model") or config.RECORDER_AI_MODEL)
    return {"state": "enabled", "model": model, "analyzed": analyzed, "failed": failed}
