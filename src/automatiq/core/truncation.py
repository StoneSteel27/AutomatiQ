"""
Middle-out truncation for error messages and large payloads.

Preserves the beginning and end of text, replacing the middle with a marker.
This is useful for error messages where both the start (imports, headers) and
end (stack traces, closing brackets) are important.

Based on Codex's truncate_middle_chars() implementation.
"""


def truncate_middle(text: str, max_bytes: int = 512) -> str:
    """
    Truncate text by removing the middle portion, preserving start and end.

    If the text exceeds `max_bytes`, keeps the first and last portions
    (split evenly) and inserts a truncation marker in the middle.

    Handles UTF-8 boundaries correctly — never splits multi-byte characters.

    Example:
        truncate_middle("a" * 1000, max_bytes=100)
        -> "aaa...900 chars truncated...aaa"

    Args:
        text: The text to truncate.
        max_bytes: Maximum byte size of the output (including marker).

    Returns:
        Truncated text with marker, or original text if under limit.
    """
    if not text:
        return text

    # Check if truncation is needed
    text_bytes = len(text.encode("utf-8"))
    if text_bytes <= max_bytes:
        return text

    # Estimate marker size (will refine after extraction)
    # Use a reasonable estimate: "...XXX chars truncated..." is ~25 bytes
    marker_estimate_bytes = 25

    # Split remaining budget: half for prefix, half for suffix
    remaining_budget = max_bytes - marker_estimate_bytes
    if remaining_budget < 0:
        remaining_budget = 0
    half_budget = remaining_budget // 2

    # Extract prefix (first half_budget bytes)
    prefix = _extract_utf8_prefix(text, half_budget)

    # Extract suffix (last half_budget bytes)
    suffix = _extract_utf8_suffix(text, half_budget)

    # Calculate removed bytes
    prefix_bytes = len(prefix.encode("utf-8"))
    suffix_bytes = len(suffix.encode("utf-8"))
    removed_bytes = text_bytes - prefix_bytes - suffix_bytes

    # Build output with marker
    marker = f"...{removed_bytes} chars truncated..."
    return f"{prefix}{marker}{suffix}"


def _extract_utf8_prefix(text: str, max_bytes: int) -> str:
    """
    Extract the longest prefix that fits within max_bytes (UTF-8 safe).

    Walks character-by-character to avoid splitting multi-byte chars.
    """
    result = []
    total_bytes = 0

    for char in text:
        char_bytes = len(char.encode("utf-8"))
        if total_bytes + char_bytes > max_bytes:
            break
        result.append(char)
        total_bytes += char_bytes

    return "".join(result)


def _extract_utf8_suffix(text: str, max_bytes: int) -> str:
    """
    Extract the longest suffix that fits within max_bytes (UTF-8 safe).

    Walks character-by-character from the end to avoid splitting multi-byte chars.
    """
    result = []
    total_bytes = 0

    # Walk backwards
    for char in reversed(text):
        char_bytes = len(char.encode("utf-8"))
        if total_bytes + char_bytes > max_bytes:
            break
        result.append(char)
        total_bytes += char_bytes

    # Reverse to restore original order
    return "".join(reversed(result))
