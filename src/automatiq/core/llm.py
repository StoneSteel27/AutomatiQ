import json
import logging
import os

import litellm

from . import config

logger = logging.getLogger(__name__)


def extract_message(exc) -> str:
    """Pull a readable summary from an exception, stripping litellm wrapper noise."""
    import re

    def _clean(raw):
        s = str(raw)
        s = re.sub(r"^(?:[\w\.]+:\s*)+", "", s)
        s = re.sub(r"^\w+Exception\s+\w+\s*-\s*", "", s)
        json_match = re.search(r"\{.*\}", s, re.DOTALL)
        if json_match:
            try:
                body = json.loads(json_match.group())
                if "error" in body:
                    err = body["error"]
                    if isinstance(err, dict) and "message" in err:
                        return err["message"]
                    return str(err)
                if "message" in body:
                    return body["message"]
            except json.JSONDecodeError:
                pass
        return s.split("\n")[0][:300]

    return _clean(exc)


def _build_model_help(model: str, original_msg: str) -> str:
    """Build a simple error message for an invalid or unsupported model."""
    if "/" not in model:
        hint = ""
        if config.API_BASE:
            hint = (
                f"\nSince base_url is set ({config.API_BASE}), this looks like a local model. "
                f"Prefix it with 'openai/' (e.g. 'openai/{model}')."
            )
        return (
            f"Invalid model string '{model}'. Expected format: 'provider/model-name' "
            f"(e.g. 'gemini/gemini-2.5-flash').{hint}\n"
            f"Original error: {original_msg}"
        )

    return (
        f"The requested model '{model}' either does not exist, is not supported by the provider, "
        f"or there is a problem on their server side.\n"
        f"Original error: {original_msg}"
    )


def _is_model_error(exc: Exception) -> bool:
    msg = extract_message(exc).lower()
    needles = [
        "llm provider not provided",
        "unable to map your input to a model",
        "invalid model",
        "model not found",
        "unknown model",
        "unsupported model",
        "model is not supported",
    ]
    return any(n in msg for n in needles)


def call_llm_streaming(msgs: list[dict], tools: list[dict]):
    """Streaming LLM call to litellm.

    Yields tuples of ``(reasoning_delta, content_delta, tool_call_deltas, usage)``:

    - ``reasoning_delta`` — ``str | None`` fragment of reasoning/thinking content
    - ``content_delta`` — ``str | None`` fragment of response content
    - ``tool_call_deltas`` — ``list[dict] | None`` of tool-call delta dicts,
      each with keys ``index``, ``id``, ``name``, ``arguments``.  The first
      chunk for a given index carries ``id`` and ``name``; subsequent chunks
      carry only ``arguments`` JSON fragments to concatenate.
    - ``usage`` — litellm ``Usage`` object or ``None``; only present on the
      final chunk (when ``stream_options={"include_usage": True}`` is honored).
    """
    kwargs = dict(
        model=config.AGENT_MODEL,
        messages=msgs,
        tools=tools,
        tool_choice="auto",
        temperature=0.3,
        stream=True,
        stream_options={"include_usage": True},
    )
    if config.API_BASE:
        kwargs["api_base"] = config.API_BASE
        kwargs["api_key"] = os.environ.get("OPENAI_API_KEY") or "not-required"

    if litellm.supports_reasoning(model=config.AGENT_MODEL):
        kwargs["reasoning_effort"] = "high"

    try:
        response = litellm.completion(**kwargs)
        for chunk in response:
            # Usage-only chunk (choices is empty on the trailing usage chunk)
            if not chunk.choices:
                u = getattr(chunk, "usage", None)
                if u is not None:
                    yield (None, None, None, u)
                continue

            delta = chunk.choices[0].delta
            content_delta = getattr(delta, "content", None)
            reasoning_delta = getattr(delta, "reasoning_content", None)
            raw_tool_calls = getattr(delta, "tool_calls", None)
            u = getattr(chunk, "usage", None)

            tc_list: list[dict] | None = None
            if raw_tool_calls:
                tc_list = []
                for tc in raw_tool_calls:
                    func = getattr(tc, "function", None)
                    tc_list.append(
                        {
                            "index": tc.index,
                            "id": getattr(tc, "id", None),
                            "name": getattr(func, "name", None) if func else None,
                            "arguments": getattr(func, "arguments", "") if func else "",
                        }
                    )

            yield (reasoning_delta, content_delta, tc_list, u)
    except Exception as exc:
        if _is_model_error(exc):
            raise ValueError(_build_model_help(config.AGENT_MODEL, extract_message(exc))) from exc
        raise
