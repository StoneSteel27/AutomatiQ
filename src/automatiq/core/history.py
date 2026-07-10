import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from . import config

logger = logging.getLogger(__name__)


# ── YAML serialization helpers ──────────────────────────────────────────────


class _SessionDumper(yaml.CSafeDumper if hasattr(yaml, "CSafeDumper") else yaml.Dumper):
    """Custom Dumper that renders multi-line strings as literal block scalars.

    Inherits from CSafeDumper (libyaml C bindings) when available for speed,
    falling back to the pure-Python Dumper otherwise.
    """


def _multiline_presenter(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_SessionDumper.add_representer(str, _multiline_presenter)


@dataclass
class SessionInfo:
    """Metadata about a resumable history session."""

    folder_name: str
    recording_name: str
    timestamp: str
    history_dir: Path
    messages_count: int
    cell_count: int

    @property
    def human_timestamp(self) -> str:
        """Return the timestamp in human-readable form (e.g. 'Jun 30, 2026 14:32')."""
        try:
            dt = datetime.strptime(self.timestamp, "%Y%m%d_%H%M%S")
            return dt.strftime("%b %d, %Y %I:%M %p")
        except (ValueError, TypeError):
            return self.timestamp

    def load_counts(self) -> None:
        """Lazily load messages_count and cell_count from the YAML file."""
        try:
            messages = load_session_messages(self.history_dir)
            self.messages_count = len(messages)
            self.cell_count = _count_cells(messages)
        except Exception:
            logger.debug(f"Could not load messages from {self.history_dir}")


def compress_history(messages: list[dict], cutoff_turn=10) -> list[dict]:
    """
    Truncates massive tool outputs, strips deep thinking blocks, and manages
    provider-specific signatures to save context window.
    """
    compressed = []
    # threshold_idx separates the recent active window from older messages
    threshold_idx = max(0, len(messages) - cutoff_turn)
    cell_counter = 0

    # 1. Determine provider from current agent configuration
    agent_model = getattr(config, "AGENT_MODEL", "").lower()
    provider = ""
    if "/" in agent_model:
        provider = agent_model.split("/")[0]

    is_gemini = provider in ("gemini", "google", "vertex_ai") or "gemini" in agent_model
    is_anthropic = provider in ("anthropic", "vertexai") or "claude" in agent_model

    # Standard Google-recommended dummy signature to bypass validation on older turns
    dummy_sig = base64.b64encode(b"skip_thought_signature_validator").decode()

    # 2. First pass (Gemini only): Map tool call IDs containing '__thought__' to their dummy-sig versions
    # for older messages so we keep the IDs perfectly matching in both assistant and tool messages.
    id_mapping = {}
    if is_gemini:
        for i, msg in enumerate(messages):
            if i < threshold_idx:
                if msg.get("role") == "assistant" and "tool_calls" in msg:
                    for tc in msg["tool_calls"]:
                        tc_id = tc.get("id")
                        if tc_id and "__thought__" in tc_id:
                            base_id = tc_id.split("__thought__")[0]
                            dummy_id = f"{base_id}__thought__{dummy_sig}"
                            id_mapping[tc_id] = dummy_id

    # 3. Second pass: Rebuild the messages list with provider-aware pruning
    for i, msg in enumerate(messages):
        role = msg.get("role")
        is_exec = False

        if role == "tool" and msg.get("name") == "execute_ipython":
            content_str = str(msg.get("content", ""))
            is_failed_val = content_str.startswith("SYSTEM: Tool Validation Error") or content_str.startswith(
                "SYSTEM: Validation failed repeatedly"
            )
            is_dup = content_str.startswith("SYSTEM: You have submitted the exact same description")
            if not is_failed_val and not is_dup:
                cell_counter += 1
                is_exec = True

        # Process assistant messages
        if role == "assistant":
            # Strip deep thinking fields, reasoning_content, and provider_specific_fields
            clean_msg = {
                "role": "assistant",
                "content": msg.get("content") or "",
            }

            # Provider-aware thinking block preservation
            if is_anthropic:
                # For Anthropic, we MUST preserve 'thinking_blocks' to avoid 400 Bad Request
                if "thinking_blocks" in msg:
                    clean_msg["thinking_blocks"] = msg["thinking_blocks"]

            # Clean tool calls if present, mapping older IDs to dummy signatures (Gemini-only)
            if "tool_calls" in msg:
                clean_tool_calls = []
                for tc in msg["tool_calls"]:
                    tc_copy = dict(tc)
                    tc_id = tc_copy.get("id")

                    if is_gemini:
                        if tc_id in id_mapping:
                            tc_copy["id"] = id_mapping[tc_id]
                        elif i < threshold_idx and tc_id and "__thought__" in tc_id:
                            base_id = tc_id.split("__thought__")[0]
                            tc_copy["id"] = f"{base_id}__thought__{dummy_sig}"

                        # Clean signature inside provider fields for older turns
                        if i < threshold_idx:
                            tc_copy.pop("provider_specific_fields", None)
                        else:
                            if "provider_specific_fields" in tc_copy:
                                tc_copy["provider_specific_fields"] = dict(tc_copy["provider_specific_fields"])
                    else:
                        # Non-Gemini models don't use thought signatures, pop metadata fields
                        tc_copy.pop("provider_specific_fields", None)

                    clean_tool_calls.append(tc_copy)
                clean_msg["tool_calls"] = clean_tool_calls

            compressed.append(clean_msg)
            continue

        # Process tool messages
        if role == "tool":
            tool_call_id = msg.get("tool_call_id")
            clean_tool_call_id = tool_call_id

            if is_gemini:
                if tool_call_id in id_mapping:
                    clean_tool_call_id = id_mapping[tool_call_id]
                elif i < threshold_idx and tool_call_id and "__thought__" in tool_call_id:
                    base_id = tool_call_id.split("__thought__")[0]
                    clean_tool_call_id = f"{base_id}__thought__{dummy_sig}"

            # If it's an older message and the output is large, truncate it
            if i < threshold_idx:
                content_str = str(msg.get("content", ""))
                if len(content_str) > 1000:
                    if msg.get("name") == "execute_ipython" and is_exec:
                        trunc_msg = f"use `%view_output Cell_{cell_counter}` to view output of this cell"
                    else:
                        trunc_msg = "<Truncated older tool output to save tokens>"

                    compressed.append(
                        {
                            "role": "tool",
                            "tool_call_id": clean_tool_call_id,
                            "name": msg.get("name"),
                            "content": trunc_msg,
                        }
                    )
                    continue

            # For recent tool messages, keep the clean ID but full content
            compressed.append(
                {
                    "role": "tool",
                    "tool_call_id": clean_tool_call_id,
                    "name": msg.get("name"),
                    "content": msg.get("content"),
                }
            )
            continue

        # For user/system messages, append as-is
        compressed.append(msg)

    return compressed


# ── Session persistence ─────────────────────────────────────────────────────


_TIMESTAMP_SUFFIX = re.compile(r"^(.+)_(\d{8}_\d{6})$")


def init_history_dir(session_name: str) -> Path:
    """Create a new timestamped history folder and return its path."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder_name = f"{session_name}_{timestamp}"
    history_dir = config.HISTORY_DIR / folder_name
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir


def save_session_snapshot(history_dir: Path, messages: list[dict], metadata: dict) -> None:
    """Write messages_full.yaml as {metadata: ..., messages: [...]}.

    Uses JSON instead of YAML for serialization speed — the full messages list
    can contain multi-MB tool outputs, and PyYAML's pure-Python string analysis
    (especially with block-scalar style) is O(n) per string. JSON is C-optimized
    and 10-100x faster for large payloads. yaml.safe_load() in the loader parses
    JSON transparently (JSON is a YAML 1.2 subset), so backward compat is preserved.
    """
    import json

    payload = {"metadata": metadata, "messages": messages}
    path = history_dir / "messages_full.yaml"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"Saved session snapshot to {path}")


def save_compressed_snapshot(history_dir: Path, messages: list[dict]) -> None:
    """Write messages_compressed.yaml as a bare list (debug artifact only)."""
    compressed = compress_history(messages, cutoff_turn=20)
    path = history_dir / "messages_compressed.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(compressed, f, Dumper=_SessionDumper, sort_keys=False, allow_unicode=True)
    logger.info(f"Saved compressed session history to {path}")


def load_session_messages(history_dir: Path) -> list[dict]:
    """Load messages from messages_full.yaml.

    Handles JSON (new format, fast), YAML dict (legacy), and bare-list (legacy) formats.
    yaml.safe_load parses JSON transparently, but we add an explicit json fallback
    for edge cases where libyaml's JSON parsing differs.
    """
    path = history_dir / "messages_full.yaml"
    messages, _ = _parse_session_file(path)
    return messages


def _parse_session_file(path: Path) -> tuple[list[dict], dict | None]:
    """Parse messages_full.yaml and return (messages, metadata).

    Handles JSON (new format), YAML dict (legacy), and bare-list (legacy) formats.
    """
    import json

    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except Exception:
        data = json.loads(raw)

    if isinstance(data, list):
        return data, None
    if isinstance(data, dict) and "messages" in data:
        return data["messages"], data.get("metadata")
    raise ValueError(f"Unexpected YAML structure in {path}: {type(data).__name__}")


def load_session_metadata(history_dir: Path) -> dict | None:
    """Load metadata from messages_full.yaml.

    Returns None for legacy sessions (bare-list format without metadata).
    """
    path = history_dir / "messages_full.yaml"
    if not path.exists():
        return None
    _, metadata = _parse_session_file(path)
    return metadata


def extract_recording_name(folder_name: str) -> str:
    """Strip the trailing _YYYYMMDD_HHMMSS timestamp suffix from a history folder name."""
    match = _TIMESTAMP_SUFFIX.match(folder_name)
    if match:
        return match.group(1)
    return folder_name


def _is_system_error_response(content: str) -> bool:
    """Return True if the tool response content is a guardrail/validation error, not real output."""
    return content.startswith(
        (
            "SYSTEM: Tool Validation Error",
            "SYSTEM: Validation failed repeatedly",
            "SYSTEM: You have submitted the exact same description",
        )
    )


def _count_cells(messages: list[dict]) -> int:
    """Count real execute_ipython tool responses (excluding validation errors and duplicates)."""
    return sum(
        1
        for msg in messages
        if msg.get("role") == "tool"
        and msg.get("name") == "execute_ipython"
        and not _is_system_error_response(str(msg.get("content", "")))
    )


def list_resumable_sessions() -> list[SessionInfo]:
    """Scan HISTORY_DIR for sessions whose recording dir exists in cwd.

    Returns sorted newest-first (by folder timestamp).
    Does NOT load YAML files — messages_count and cell_count are 0 until
    load_counts() is called explicitly.
    """
    if not config.HISTORY_DIR.exists():
        return []

    cwd = Path.cwd()
    sessions: list[SessionInfo] = []

    for d in config.HISTORY_DIR.iterdir():
        if not d.is_dir():
            continue
        messages_path = d / "messages_full.yaml"
        if not messages_path.exists():
            continue

        recording_name = extract_recording_name(d.name)
        recording_dir = cwd / recording_name
        if not (recording_dir / "session_metadata.json").exists():
            continue

        # Extract timestamp from folder name for sorting
        match = _TIMESTAMP_SUFFIX.match(d.name)
        timestamp = match.group(2) if match else ""

        sessions.append(
            SessionInfo(
                folder_name=d.name,
                recording_name=recording_name,
                timestamp=timestamp,
                history_dir=d,
                messages_count=0,
                cell_count=0,
            )
        )

    sessions.sort(key=lambda s: s.timestamp, reverse=True)
    return sessions


def find_history_dirs(name: str | None = None) -> list[Path]:
    """Find resumable history dirs matching *name* prefix.

    If *name* is None, returns all resumable sessions.
    Sorted newest-first.
    """
    sessions = list_resumable_sessions()
    if name is not None:
        sessions = [s for s in sessions if name in s.recording_name or name in s.folder_name]
    return [s.history_dir for s in sessions]


def export_session_logs(messages: list[dict], session_name: str = "unknown", metadata: dict | None = None) -> str:
    """Write session logs to ~/.automatiq/history/ and return the folder name.

    Creates a new timestamped folder, writes messages_full.yaml (with metadata)
    and messages_compressed.yaml.
    """
    history_dir = init_history_dir(session_name)
    save_session_snapshot(history_dir, messages, metadata or {})
    save_compressed_snapshot(history_dir, messages)
    return history_dir.name
