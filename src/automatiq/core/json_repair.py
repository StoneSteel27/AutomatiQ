"""
JSON repair pipeline for malformed tool arguments.

Attempts to fix common LLM mistakes (markdown fences, trailing commas, single quotes,
unescaped newlines) before failing with a parse error.

Based on patterns from OMP's validation pipeline.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

# Markdown code fence pattern for JSON
JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)

# Trailing comma before closing brace/bracket
TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")

# Single quotes instead of double quotes (simple cases)
SINGLE_QUOTE_RE = re.compile(r"'([^']*)'")


def strip_json_fences(text: str) -> tuple[str, bool]:
    """
    Strip markdown code fences from JSON.

    Example:
        ```json
        {"key": "value"}
        ```
    becomes:
        {"key": "value"}

    Returns (stripped_text, changed).
    """
    if not isinstance(text, str):
        return text, False

    match = JSON_FENCE_RE.match(text.strip())
    if match:
        return match.group(1).strip(), True
    return text, False


def fix_trailing_commas(text: str) -> tuple[str, bool]:
    """
    Remove trailing commas before closing braces/brackets.

    Example:
        {"a": 1,} -> {"a": 1}
        [1, 2,] -> [1, 2]

    Returns (fixed_text, changed).
    """
    if not isinstance(text, str):
        return text, False

    fixed = TRAILING_COMMA_RE.sub(r"\1", text)
    return fixed, fixed != text


def fix_single_quotes(text: str) -> tuple[str, bool]:
    """
    Convert single quotes to double quotes (simple cases only).

    Example:
        {'key': 'value'} -> {"key": "value"}

    WARNING: This is a simple heuristic that may break for strings containing
    apostrophes or nested quotes. Only use as a last-resort repair.

    Returns (fixed_text, changed).
    """
    if not isinstance(text, str):
        return text, False

    # Only attempt if the text looks like it has single-quoted keys/values
    if "'" not in text:
        return text, False

    # Simple replacement: 'key' -> "key", 'value' -> "value"
    # This is risky but handles the most common LLM mistake
    fixed = SINGLE_QUOTE_RE.sub(r'"\1"', text)
    return fixed, fixed != text


def fix_unescaped_newlines(text: str) -> tuple[str, bool]:
    """
    Escape unescaped newlines inside JSON strings.

    Example:
        {"key": "line1
        line2"} -> {"key": "line1\\nline2"}

    Returns (fixed_text, changed).
    """
    if not isinstance(text, str):
        return text, False

    # This is complex — we need to find string literals and escape newlines within them
    # For now, use a simple heuristic: replace literal newlines with \\n
    # This may break if the JSON is already valid, so we check first
    try:
        json.loads(text)
        # Already valid, no repair needed
        return text, False
    except (json.JSONDecodeError, ValueError):
        pass

    # Attempt repair: replace literal newlines with escaped newlines
    # This is a simple heuristic that may not handle all cases
    fixed = text.replace("\n", "\\n").replace("\r", "\\r")
    return fixed, fixed != text


def extract_leading_container(text: str) -> tuple[str, bool]:
    """
    Extract the leading JSON container if there's trailing junk.

    Example:
        {"a": 1}}} -> {"a": 1}
        [1, 2]]] -> [1, 2]

    Returns (extracted_text, changed).
    """
    if not isinstance(text, str):
        return text, False

    text = text.strip()
    if not text:
        return text, False

    # Try to parse as-is first
    try:
        json.loads(text)
        return text, False
    except (json.JSONDecodeError, ValueError):
        pass

    # Find the matching closing bracket/brace
    if not (text.startswith("{") or text.startswith("[")):
        return text, False

    # Walk through the string, tracking bracket depth
    depth = 0
    in_string = False
    escape_next = False

    for i, char in enumerate(text):
        if escape_next:
            escape_next = False
            continue

        if char == "\\":
            escape_next = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                # Found the matching close
                extracted = text[: i + 1]
                # Verify it's valid JSON
                try:
                    json.loads(extracted)
                    return extracted, True
                except (json.JSONDecodeError, ValueError):
                    return text, False

    return text, False


def repair_json(raw_text: str) -> tuple[str, list[str]]:
    """
    Attempt to repair malformed JSON.

    Applies a series of fixes in order:
    1. Strip markdown fences
    2. Fix trailing commas
    3. Fix single quotes
    4. Fix unescaped newlines
    5. Extract leading container

    Returns (repaired_text, list_of_fixes_applied).

    If no repair succeeds, returns the original text with an empty fixes list.
    The caller should attempt json.loads() and handle the error.
    """
    if not isinstance(raw_text, str):
        return raw_text, []

    fixes: list[str] = []
    current = raw_text

    # Step 1: Strip markdown fences
    stripped, changed = strip_json_fences(current)
    if changed:
        current = stripped
        fixes.append("stripped markdown fence")

    # Step 2: Fix trailing commas
    fixed_commas, changed = fix_trailing_commas(current)
    if changed:
        current = fixed_commas
        fixes.append("removed trailing commas")

    # Step 3: Fix single quotes
    fixed_quotes, changed = fix_single_quotes(current)
    if changed:
        current = fixed_quotes
        fixes.append("converted single quotes to double quotes")

    # Step 4: Fix unescaped newlines
    fixed_newlines, changed = fix_unescaped_newlines(current)
    if changed:
        current = fixed_newlines
        fixes.append("escaped unescaped newlines")

    # Step 5: Extract leading container
    extracted, changed = extract_leading_container(current)
    if changed:
        current = extracted
        fixes.append("extracted leading JSON container")

    # Verify the repaired JSON is valid
    try:
        json.loads(current)
        return current, fixes
    except (json.JSONDecodeError, ValueError):
        # Repair failed, return original text
        return raw_text, []
