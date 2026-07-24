"""
Silent coercion pipeline for tool arguments.

Fixes common LLM mistakes (markdown fences, enum case, whitespace, double-JSON)
before validation, so trivial errors don't waste an LLM turn.

Based on patterns from:
- OMP: Multi-stage normalization + iterative coercion
- OpenCode: Schema-driven validation
- Codex: Strict parsing (no repair)

We adopt OMP's approach: silent repair before validation.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

# Fields that contain code/scripts — markdown fences should be stripped
CODE_FIELDS = {"ipython_script", "final_python_script", "code", "script"}

# Fields that are enums — case-insensitive matching should be applied
ENUM_FIELDS = {"target_mode"}

# Identifier-like fields — whitespace should be trimmed (following OMP's pattern)
# We do NOT trim content-carrying fields like "description", "context"
IDENTIFIER_FIELDS = {"path", "file", "file_path", "filepath", "url", "uri", "title", "label"}

# Markdown code fence pattern: ```python, ```bash, ```json, etc.
MARKDOWN_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?(.*?)\n?```$", re.DOTALL)

# Valid enum values for target_mode
VALID_MODES = {"reading", "testing", "building"}


def strip_markdown_fences(value: str) -> tuple[str, bool]:
    """
    Strip markdown code fences from a string.

    Example:
        ```python
        import os
        ```
    becomes:
        import os

    Returns (stripped_value, changed).
    """
    if not isinstance(value, str):
        return value, False

    match = MARKDOWN_FENCE_RE.match(value.strip())
    if match:
        return match.group(1).strip(), True
    return value, False


def normalize_enum_case(value: str, valid_values: set[str]) -> tuple[str, bool]:
    """
    Normalize enum values to match valid options (case-insensitive).

    Example:
        "Reading" -> "reading"
        "TESTING" -> "testing"

    Returns (normalized_value, changed).
    """
    if not isinstance(value, str):
        return value, False

    # Try exact match first
    if value in valid_values:
        return value, False

    # Try case-insensitive match
    lower = value.lower().strip()
    if lower in valid_values:
        return lower, True

    return value, False


def trim_whitespace(value: str) -> tuple[str, bool]:
    """
    Strip leading/trailing whitespace from a string.

    Returns (trimmed_value, changed).
    """
    if not isinstance(value, str):
        return value, False

    trimmed = value.strip()
    return trimmed, trimmed != value


def unwrap_double_json(value: str, max_depth: int = 3) -> tuple[object, bool]:
    """
    Parse stringified JSON into native objects (up to max_depth levels).

    Example:
        '{"key": "val"}' -> {"key": "val"}
        '"{\\"key\\": \\"val\\"}"' -> {"key": "val"}  (double-encoded)

    Returns (parsed_value, changed).
    """
    if not isinstance(value, str):
        return value, False

    current = value
    changed = False

    for _ in range(max_depth):
        stripped = current.strip()
        if not stripped:
            break

        # Check if it looks like JSON (starts with { or [ or ")
        if not (stripped.startswith(("{", "[", '"'))):
            break

        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            break

        # If we parsed a string, it might be double-encoded — continue unwrapping
        if isinstance(parsed, str):
            current = parsed
            changed = True
            continue

        # Successfully parsed a non-string value
        return parsed, True

    return current, changed


def coerce_tool_args(tool_name: str, args: dict) -> tuple[dict, list[str]]:
    """
    Apply all coercion transforms to tool arguments.

    Returns (coerced_args, list_of_fixes_applied).

    The fixes list contains human-readable descriptions of what was fixed,
    e.g., ["stripped markdown fence from ipython_script", "normalized target_mode case"].
    """
    if not isinstance(args, dict):
        return args, []

    fixes: list[str] = []
    coerced = dict(args)  # shallow copy

    # Tool-specific coercion
    if tool_name == "execute_ipython":
        # Strip markdown fences from ipython_script
        if "ipython_script" in coerced:
            value, changed = strip_markdown_fences(coerced["ipython_script"])
            if changed:
                coerced["ipython_script"] = value
                fixes.append("stripped markdown fence from ipython_script")

    elif tool_name == "final_submit":
        # Strip markdown fences from final_python_script
        if "final_python_script" in coerced:
            value, changed = strip_markdown_fences(coerced["final_python_script"])
            if changed:
                coerced["final_python_script"] = value
                fixes.append("stripped markdown fence from final_python_script")

    elif tool_name == "switch_mode":
        # Normalize target_mode enum case
        if "target_mode" in coerced:
            value, changed = normalize_enum_case(coerced["target_mode"], VALID_MODES)
            if changed:
                coerced["target_mode"] = value
                fixes.append(f"normalized target_mode case: '{args['target_mode']}' -> '{value}'")

    # Generic: trim whitespace from identifier fields
    for field in IDENTIFIER_FIELDS:
        if field in coerced and isinstance(coerced[field], str):
            value, changed = trim_whitespace(coerced[field])
            if changed:
                coerced[field] = value
                fixes.append(f"trimmed whitespace from {field}")

    # Generic: unwrap double-JSON for all string fields (except code/enum fields)
    for key, value in list(coerced.items()):
        if isinstance(value, str) and key not in CODE_FIELDS and key not in ENUM_FIELDS:
            # Skip if it's a simple string (no JSON-like structure)
            if value.strip().startswith(("{", "[", '"')):
                parsed, changed = unwrap_double_json(value)
                if changed:
                    coerced[key] = parsed
                    fixes.append(f"unwrapped double-JSON from {key}")

    return coerced, fixes
