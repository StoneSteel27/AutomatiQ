"""Agent loop — the core interactive session where the LLM investigates a
recorded browser session and produces a standalone automation/extraction script."""  # noqa: E501

import json
import logging
import os
import queue
import re
import sys
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import litellm
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from . import config, events
from .cancel_standard import (
    CancelRequestedException,
    CancelToken,
    StopRequestedException,
    StopToken,
    run_cancellable,
)
from .guardrails import check_duplicate_thought, check_final_script_bounce, check_repeated_execution
from .history import (
    compress_history,
    extract_recording_name,
    init_history_dir,
    load_session_messages,
    load_session_metadata,
    save_compressed_snapshot,
    save_session_snapshot,
)
from .ipython_sandbox import AgentSandbox
from .llm import call_llm_streaming, extract_message
from .prompts import MODE_INJECTIONS, SYSTEM_PROMPT
from .tools import AGENT_TOOLS, validate_tool_args

logger = logging.getLogger(__name__)
litellm.suppress_debug_info = True
litellm.drop_params = True

_preloaded_sandbox = None

# -----------------
# CONSTANTS
# -----------------

MAX_FINAL_SCRIPT_BOUNCES = 1
MAX_VALIDATION_RETRIES = 3
MAX_LLM_RETRIES = 5
BASE_BACKOFF = 10

# Maps litellm provider prefix → the environment variable the user must set.
_PROVIDER_KEY_MAP = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "cohere": "COHERE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "azure": "AZURE_API_KEY",
}

# -----------------
# LIFECYCLE & HELPERS
# -----------------


def _meta_get(meta: dict | None, key: str, default=0):
    """Safely retrieve a value from optional session metadata."""
    return meta.get(key, default) if meta else default


def _interruptible_sleep(seconds: int, cancel_token) -> bool:
    """Sleep for *seconds*, checking cancel_token each second.

    Returns True if cancelled before the full duration elapsed.
    """
    for _ in range(seconds):
        if cancel_token.is_cancelled():
            return True
        time.sleep(1)
    return False


@events.preload_start.connect
def handle_preload_start(sender, **kwargs):
    global _preloaded_sandbox
    if _preloaded_sandbox is None:
        workspace = str(config.WORKSPACE_DIR)
        os.makedirs(workspace, exist_ok=True)
        _preloaded_sandbox = AgentSandbox(
            working_dir=workspace,
            timeout_seconds=config.SANDBOX_TIMEOUT_SECONDS,
            bin_path=str(config.BIN_DIR),
        )


# -----------------
# AGENT LOOP
# -----------------


def find_latest_session_dir(target: str | None = None) -> Path | None:
    if target:
        p = Path(target)
        if p.exists() and (p / "session_metadata.json").exists():
            return p
        return None

    # Scan current directory
    cwd = Path.cwd()
    valid_sessions = []
    for d in cwd.iterdir():
        if d.is_dir():
            meta = d / "session_metadata.json"
            if meta.exists():
                try:
                    with open(meta) as f:
                        data = json.load(f)
                        if data.get("status") == "completed":
                            valid_sessions.append((d, meta.stat().st_mtime))
                except Exception:
                    pass

    if not valid_sessions:
        return None

    # Return the one with the latest modification time
    valid_sessions.sort(key=lambda x: x[1], reverse=True)
    return valid_sessions[0][0]


def _reconstruct_exec_history(messages: list[dict]) -> list[tuple[str, str, int]]:
    """Rebuild (display_script, output, cell_num) triples from message history.

    Walks assistant tool_calls → tool responses, matching by tool_call_id.
    Filters for execute_ipython only, skipping validation errors and duplicates.
    Resolves dedup pointers ("output is the same as Cell_N...").
    """
    # Build a map: tool_call_id → tool message content
    tool_responses: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            tool_responses[msg["tool_call_id"]] = msg.get("content", "")

    # Walk assistant messages, find execute_ipython tool calls
    raw_cells: list[tuple[str, str]] = []  # (script, output) before numbering
    cell_num = 0
    for msg in messages:
        if msg.get("role") != "assistant" or "tool_calls" not in msg:
            continue
        for tc in msg["tool_calls"]:
            func = tc.get("function", {})
            if func.get("name") != "execute_ipython":
                continue
            tc_id = tc.get("id")
            if tc_id not in tool_responses:
                continue
            output = tool_responses[tc_id]
            # Skip validation errors and duplicates (not real cells)
            if (
                output.startswith("SYSTEM: Tool Validation Error")
                or output.startswith("SYSTEM: Validation failed repeatedly")
                or output.startswith("SYSTEM: You have submitted the exact same description")
            ):
                continue
            # Extract script from arguments
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                continue
            script = args.get("ipython_script", "")
            desc = args.get("description", "")
            display_script = f"# {desc}\n{script}" if desc else script
            raw_cells.append((display_script.strip(), output))

    # Resolve dedup pointers: "The output is the same as Cell_N..."
    # and strip <terminal_output> wrapper for cache storage
    resolved: list[tuple[str, str, int]] = []
    cell_outputs_by_num: dict[int, str] = {}

    for script, output in raw_cells:
        cell_num += 1
        # Check if this is a dedup pointer
        match = re.search(r"output is the same as Cell_(\d+)", output)
        if match:
            ref_cell = int(match.group(1))
            if ref_cell in cell_outputs_by_num:
                output = cell_outputs_by_num[ref_cell]
        # Strip <terminal_output> wrapper for cache
        clean_output = re.sub(r"^<terminal_output>\n?", "", output)
        clean_output = re.sub(r"\n?</terminal_output>$", "", clean_output)
        cell_outputs_by_num[cell_num] = clean_output
        resolved.append((script, clean_output, cell_num))

    return resolved


def run_agent(
    input_queue: queue.Queue,
    cancel_token: CancelToken = None,
    stop_token: StopToken = None,
    target: str | None = None,
    resume_from: str | None = None,
):
    """Interactive agent loop. Reads from the workspace produced by the recorder."""
    if cancel_token is None:
        cancel_token = CancelToken()

    history_dir: Path | None = None
    saved_meta: dict | None = None

    # ── Resume path: load messages + state from disk ────────────────────────
    if resume_from:
        history_dir = Path(resume_from)
        if not (history_dir / "messages_full.yaml").exists():
            events.log_error.send("core", text=f"No session history found at {history_dir}")
            events.log_info.send("core", text="Use 'automatiq resume' to list available sessions.")
            sys.exit(1)

        messages = load_session_messages(history_dir)
        saved_meta = load_session_metadata(history_dir)

        # Restore critical state from metadata (defaults for legacy sessions)
        current_mode = _meta_get(saved_meta, "current_mode", "reading")

        # Reconstruct exec_history from messages
        exec_history = _reconstruct_exec_history(messages)

        # cell_counter must be at least len(exec_history) so new cells don't
        # collide with restored Cell_1..Cell_N in output_cache
        cell_counter = max(_meta_get(saved_meta, "cell_counter"), len(exec_history))

        # Derive recording session dir from history folder name
        recording_name = extract_recording_name(history_dir.name)
        session_dump = find_latest_session_dir(str(Path.cwd() / recording_name))
        if not session_dump:
            events.log_error.send(
                "core",
                text=f"Recording directory '{recording_name}' not found in current directory.",
            )
            events.log_info.send(
                "core",
                text=f"Make sure you're in the same directory where the recording folder '{recording_name}' exists.",
            )
            sys.exit(1)

        # Preserve session start time from metadata
        session_started = _meta_get(saved_meta, "session_started", None)
    else:
        # ── Fresh start path ─────────────────────────────────────────────────
        session_dump = find_latest_session_dir(target)
        if not session_dump:
            if target:
                events.log_error.send("core", text=f"No valid completed session found at {target}")
            else:
                events.log_error.send("core", text="No valid completed sessions found in the current directory.")
            events.log_info.send(
                "core",
                text="Run 'automatiq record <url>' first, or use 'automatiq run <url>' for one-shot.",
            )
            sys.exit(1)

        current_mode = "reading"
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{MODE_INJECTIONS['reading']}\n\nSession started. You are in reading mode."},
        ]
        exec_history = []
        cell_counter = 0
        session_started = None

    workspace_dir = session_dump / "workspace"

    global _preloaded_sandbox
    if _preloaded_sandbox:
        sandbox = _preloaded_sandbox
        # If preloaded sandbox was set to WORKSPACE_DIR, but our session is elsewhere:
        # We need to recreate it if the paths don't match exactly.
        if Path(sandbox.working_dir).absolute() != workspace_dir.absolute():
            sandbox.close()
            sandbox = AgentSandbox(
                working_dir=str(workspace_dir),
                timeout_seconds=config.SANDBOX_TIMEOUT_SECONDS,
                bin_path=str(config.BIN_DIR),
            )
        _preloaded_sandbox = None
    else:
        sandbox = AgentSandbox(
            working_dir=str(workspace_dir),
            timeout_seconds=config.SANDBOX_TIMEOUT_SECONDS,
            bin_path=str(config.BIN_DIR),
        )

    # ── Resume: populate sandbox caches + inject system message ─────────────
    if resume_from and exec_history:
        for script, output, cell_num in exec_history:
            sandbox.output_cache[f"Cell_{cell_num}"] = output
            sandbox.history.append(script)
        sandbox.cell_counter = cell_counter
        messages.append(
            {
                "role": "user",
                "content": (
                    "SYSTEM: Session resumed from checkpoint. "
                    f"{len(exec_history)} previous cell outputs available via %view_output. "
                    "Kernel state is fresh — previous variables/imports are NOT available. "
                    "Unless really necessary, use %restore to rebuild variables; otherwise proceed as normal."
                ),
            }
        )

    # ── Session metadata accumulator ────────────────────────────────────────
    _session_meta: dict = {
        "model": config.AGENT_MODEL,
        "llm_calls": _meta_get(saved_meta, "llm_calls"),
        "cells_executed": _meta_get(saved_meta, "cells_executed"),
        "prompt_tokens": _meta_get(saved_meta, "prompt_tokens"),
        "completion_tokens": _meta_get(saved_meta, "completion_tokens"),
        "total_tokens": _meta_get(saved_meta, "total_tokens"),
        "session_started": session_started or datetime.now().isoformat(timespec="seconds"),
        "current_mode": current_mode,
        "cell_counter": cell_counter,
    }

    # ── Initial loop state ──────────────────────────────────────────────────
    needs_user_input = True
    awaiting_tool_complete = False
    awaiting_mode_switch = False
    scr = ""
    mode_switch_notification = ""
    final_script_bounces = 0
    consecutive_validation_failures = 0
    prev_description = ""
    consecutive_autonomous_turns = 0

    # ── Telemetry tracking ─────────────────────────────────────────────────
    try:
        from .telemetry import HEARTBEAT_INTERVAL as _hb_interval
    except Exception:
        _hb_interval = 300

    _session_outcome = "abandoned_by_user"
    _guardrail_counts: dict[str, int] = {
        "duplicate_thought": 0,
        "repeated_execution": 0,
        "final_script_bounce": 0,
        "step_limit": 0,
        "validation_bailout": 0,
    }
    _session_errors: list = []
    _total_steps = 0
    _current_phase = "init"
    _last_heartbeat_time = time.monotonic()
    _final_scripts_submitted = 0
    _step_limit_reached = False
    _crash_step: int | None = None
    _crash_cell: int | None = None
    _crash_phase: str | None = None

    # ── Fire agent_started telemetry ──────────────────────────────────────
    with suppress(Exception):
        from .telemetry import AgentStartedProps, client

        client.track_agent_started(
            AgentStartedProps(
                model=config.AGENT_MODEL,
                session_type="resume" if resume_from else "fresh",
                proxy_enabled=bool(config.RECORDER_PROXY_ENABLED),
                max_steps=config.MAX_AGENT_STEPS,
            )
        )

    # ── Error-tracking helpers (closures over _session_errors / counters) ──
    def _track_exc(exc: BaseException) -> None:
        """Append a real exception to _session_errors for telemetry."""
        try:
            from .telemetry import make_error_from_exc

            _session_errors.append(
                make_error_from_exc(exc, _current_phase, _total_steps, int(_session_meta.get("cells_executed", 0)))
            )
        except Exception:
            pass

    def _track_synthetic_err(exception_class: str, message: str, line: int, file: str) -> None:
        """Append a synthetic error (no exception object) to _session_errors."""
        try:
            from .telemetry import make_error_props

            _session_errors.append(
                make_error_props(
                    exception_class,
                    message,
                    line,
                    file,
                    _current_phase,
                    _total_steps,
                    int(_session_meta.get("cells_executed", 0)),
                )
            )
        except Exception:
            pass

    try:
        while True:
            # ── Periodic heartbeat telemetry ───────────────────────────────
            if time.monotonic() - _last_heartbeat_time >= _hb_interval:
                _last_heartbeat_time = time.monotonic()
                with suppress(Exception):
                    from .telemetry import AgentHeartbeatProps, client

                    started_dt = datetime.fromisoformat(_session_meta["session_started"])
                    _hb_duration = (datetime.now() - started_dt).total_seconds()
                    client.track_agent_heartbeat(
                        AgentHeartbeatProps(
                            step=_total_steps,
                            cell=int(_session_meta.get("cells_executed", 0)),
                            duration_seconds=_hb_duration,
                            current_mode=current_mode,
                            current_phase=_current_phase,
                            final_scripts_submitted=_final_scripts_submitted,
                            guardrails=dict(_guardrail_counts),
                            errors=list(_session_errors),
                        )
                    )

            if stop_token and stop_token.is_stopped():
                raise StopRequestedException("Aborted via stop token")

            if sandbox.cancel_result is not None:
                cr = sandbox._cancel_result
                sandbox._cancel_result = None
                if cr == "lost":
                    messages.append(
                        {
                            "role": "user",
                            "content": "SYSTEM: Execution cancelled by user — process was force-killed. State lost. "
                            "Run %restore to recover previous variables.",
                        }
                    )
                elif cr == "preserved":
                    messages.append(
                        {
                            "role": "user",
                            "content": "SYSTEM: Execution interrupted by user. State preserved — variables are intact.",
                        }
                    )
                awaiting_tool_complete = False
                awaiting_mode_switch = False
                needs_user_input = True
                continue

            if not needs_user_input:
                if consecutive_autonomous_turns >= config.MAX_AGENT_STEPS:
                    _guardrail_counts["step_limit"] += 1
                    _step_limit_reached = True
                    events.log_warn.send(
                        "core",
                        text=f"Paused: Agent hit {config.MAX_AGENT_STEPS} consecutive turns without completing task.",
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "SYSTEM GUARDRAIL: You have reached the maximum consecutive turn limit. "
                                "Execution has been paused to wait for user guidance. "
                                "Please review the context and wait for the user's instructions."
                            ),
                        }
                    )
                    needs_user_input = True
                    continue
                consecutive_autonomous_turns += 1
                _total_steps += 1

            if needs_user_input:
                events.prompt_request_start.send("core")
                ip = input_queue.get()
                consecutive_autonomous_turns = 0
                if ip.strip().lower() == "q":
                    events.log_info.send("core", text="User requested exit.")
                    break
                messages.append({"role": "user", "content": ip})
                needs_user_input = False

            elif awaiting_tool_complete:
                awaiting_tool_complete = False

            elif awaiting_mode_switch:
                messages.append({"role": "user", "content": mode_switch_notification})
                awaiting_mode_switch = False

            # Compress history to save tokens
            compiled_messages = compress_history(messages, cutoff_turn=20)

            assistant_msg = None
            tool_calls = None
            reasoning = None
            content = ""
            usage = None
            aborted = False
            _current_phase = "llm_call"
            for attempt in range(1, MAX_LLM_RETRIES + 1):
                try:
                    events.llm_request_start.send("core")
                    try:
                        stream = call_llm_streaming(compiled_messages, AGENT_TOOLS)
                        thought_buf: list[str] = []
                        text_buf: list[str] = []
                        tool_call_accumulator: dict[int, dict] = {}
                        for chunk in stream:
                            if cancel_token.is_cancelled():
                                cancel_token.reset()
                                raise CancelRequestedException("Cancelled via token during streaming")
                            if stop_token and stop_token.is_stopped():
                                raise StopRequestedException("Aborted via stop token during streaming")

                            reasoning_delta, content_delta, tool_call_deltas, chunk_usage = chunk

                            if reasoning_delta:
                                thought_buf.append(reasoning_delta)
                                events.agent_thought_chunk.send("core", text=reasoning_delta)
                            if content_delta:
                                text_buf.append(content_delta)
                                events.agent_text_chunk.send("core", text=content_delta)
                            if tool_call_deltas:
                                for tc in tool_call_deltas:
                                    idx = tc["index"]
                                    if idx not in tool_call_accumulator:
                                        tool_call_accumulator[idx] = {"id": None, "name": None, "arguments": ""}
                                    if tc["id"]:
                                        tool_call_accumulator[idx]["id"] = tc["id"]
                                    if tc["name"]:
                                        tool_call_accumulator[idx]["name"] = tc["name"]
                                    if tc["arguments"]:
                                        tool_call_accumulator[idx]["arguments"] += tc["arguments"]
                            if chunk_usage:
                                usage = chunk_usage

                        events.agent_stream_end.send("core", usage=usage)
                    finally:
                        events.llm_request_end.send("core")

                    # Track token usage in session metadata
                    if usage:
                        _session_meta["llm_calls"] += 1
                        _session_meta["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
                        _session_meta["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
                        _session_meta["total_tokens"] += getattr(usage, "total_tokens", 0) or 0

                    reasoning = "".join(thought_buf) or None
                    content = "".join(text_buf)

                    assistant_msg = {"role": "assistant"}
                    if content:
                        assistant_msg["content"] = content
                    if reasoning:
                        assistant_msg["reasoning_content"] = reasoning

                    if tool_call_accumulator:
                        sorted_indices = sorted(tool_call_accumulator)
                        tool_calls = [
                            {
                                "id": tool_call_accumulator[idx]["id"],
                                "type": "function",
                                "function": {
                                    "name": tool_call_accumulator[idx]["name"],
                                    "arguments": tool_call_accumulator[idx]["arguments"],
                                },
                            }
                            for idx in sorted_indices
                        ]
                        assistant_msg["tool_calls"] = tool_calls
                    else:
                        tool_calls = None

                    break
                except CancelRequestedException:
                    events.log_info.send("core", text="Cancelled by token. Returning to prompt.")
                    events.operation_cancelled.send("core")
                    aborted = True
                    break
                except NotFoundError as exc:
                    msg = extract_message(exc)
                    _track_exc(exc)
                    events.log_error.send(
                        "core",
                        text=(
                            f"Model '{config.AGENT_MODEL}' is not found or unsupported. "
                            f"Please verify your config.toml file at ~/.automatiq/config.toml \nDetails: {msg}"
                        ),
                    )
                    aborted = True
                    break
                except InternalServerError as exc:
                    msg = extract_message(exc)
                    _track_exc(exc)
                    if config.API_BASE:
                        events.log_error.send(
                            "core",
                            text=(
                                f"Unexpected credentials error with local endpoint ({config.API_BASE}). "
                                f"This should have been handled automatically — please report this bug.\n"
                                f"Details: {msg}"
                            ),
                        )
                    else:
                        provider = config.AGENT_MODEL.split("/")[0] if "/" in config.AGENT_MODEL else "provider"
                        key_name = _PROVIDER_KEY_MAP.get(provider, f"{provider.upper().replace('-', '_')}_API_KEY")
                        events.log_error.send(
                            "core",
                            text=(
                                f"Missing API credentials for '{config.AGENT_MODEL}'.\n"
                                f"Set {key_name} in your environment or ~/.automatiq/config.toml.\n"
                                f"Details: {msg}"
                            ),
                        )
                    aborted = True
                    break
                except (
                    RateLimitError,
                    ServiceUnavailableError,
                    APIConnectionError,
                    Timeout,
                    APIError,
                ) as exc:
                    msg = extract_message(exc)
                    _track_exc(exc)
                    wait = BASE_BACKOFF * (2 ** (attempt - 1))
                    events.log_warn.send("core", text=f"LLM call failed (attempt {attempt}/{MAX_LLM_RETRIES}): {msg}")
                    events.log_traceback.send("core")
                    if attempt < MAX_LLM_RETRIES:
                        events.log_warn.send("core", text=f"Retrying in {wait}s ...")
                        events.wait_start.send("core", seconds=wait, reason="Retrying")
                        if _interruptible_sleep(wait, cancel_token):
                            cancel_token.reset()
                            events.log_info.send("core", text="Cancelled by token. Returning to prompt.")
                            events.operation_cancelled.send("core")
                            aborted = True
                            break
                    else:
                        events.log_error.send("core", text="Max retries exceeded. Returning to prompt.")
                        aborted = True
                        break

            if aborted or assistant_msg is None:
                needs_user_input = True
                awaiting_tool_complete = False
                awaiting_mode_switch = False
                continue

            if not tool_calls:
                messages.append(assistant_msg)
                needs_user_input = True
                continue

            # Append the assistant message ONCE before processing tool calls
            messages.append(assistant_msg)

            # Process ALL tool calls (parallel execution support)
            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                _current_phase = "tool_execution"
                validation_error = None

                # Phase 4: Attempt JSON repair before failing
                raw_args = tool_call["function"]["arguments"]
                try:
                    tool_args = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    # Try to repair the JSON
                    from .json_repair import repair_json

                    repaired, json_fixes = repair_json(raw_args)
                    if json_fixes:
                        try:
                            tool_args = json.loads(repaired)
                            events.log_info.send(
                                "core",
                                text=f"Applied {len(json_fixes)} JSON repair(s): {', '.join(json_fixes)}",
                            )
                        except json.JSONDecodeError:
                            # Repair failed, use original error
                            tool_args = {}
                            validation_error = f"Invalid JSON arguments: {exc}"
                            _track_exc(exc)
                    else:
                        tool_args = {}
                        validation_error = f"Invalid JSON arguments: {exc}"
                        _track_exc(exc)
                else:
                    # Phase 1: Apply silent coercion before validation
                    from .coercion import coerce_tool_args

                    tool_args, coercion_fixes = coerce_tool_args(tool_name, tool_args)
                    if coercion_fixes:
                        fixes_str = ", ".join(coercion_fixes)
                        events.log_info.send(
                            "core",
                            text=f"Applied {len(coercion_fixes)} silent fix(es) to {tool_name}: {fixes_str}",
                        )

                    validation_error = validate_tool_args(tool_name, tool_args)

                if validation_error:
                    consecutive_validation_failures += 1
                    _track_synthetic_err("ToolValidationError", validation_error, 560, "main.py")

                    # Truncate large error messages to prevent context bloat
                    from .truncation import truncate_middle

                    truncated_error = truncate_middle(validation_error, max_bytes=1024)

                    if consecutive_validation_failures >= MAX_VALIDATION_RETRIES:
                        _guardrail_counts["validation_bailout"] += 1
                        events.log_error.send(
                            "core", text=f"Tool validation failed {MAX_VALIDATION_RETRIES} times. Bailing out."
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "name": tool_name,
                                "content": f"SYSTEM: Validation failed repeatedly. Error: {truncated_error}. Returning.",
                            }
                        )
                        needs_user_input = True
                        consecutive_validation_failures = 0
                    else:
                        events.log_warn.send("core", text=f"Tool validation error: {truncated_error}")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "name": tool_name,
                                "content": f"SYSTEM: Tool Validation Error: {truncated_error}. Please fix and retry.",
                            }
                        )
                    continue

                consecutive_validation_failures = 0

                # Deduplicate logic based on description
                current_description = tool_args.get("description", "").strip() if tool_name == "execute_ipython" else ""
                duplicate_warning = check_duplicate_thought(current_description, prev_description)
                if duplicate_warning:
                    _guardrail_counts["duplicate_thought"] += 1
                    events.log_warn.send("core", text="Exact duplicate description detected.")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": tool_name,
                            "content": duplicate_warning,
                        }
                    )
                    continue

                if tool_name == "execute_ipython":
                    prev_description = current_description

                # Process the specific tool
                if tool_name == "final_submit":
                    script_content = tool_args.get("final_python_script", "")

                    final_script_bounces += 1
                    should_bounce, bounce_message = check_final_script_bounce(
                        current_mode, final_script_bounces, MAX_FINAL_SCRIPT_BOUNCES
                    )
                    if should_bounce:
                        _guardrail_counts["final_script_bounce"] += 1
                        events.log_warn.send(
                            "core",
                            text=(
                                "Final script submitted outside building mode (or verification needed) — bouncing back."
                            ),
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "name": tool_name,
                                "content": bounce_message,
                            }
                        )
                        continue

                    events.log_info.send("core", text="Agent submitted the final script.")
                    _session_outcome = "success"
                    _final_scripts_submitted += 1
                    with suppress(Exception):
                        from .telemetry import FinalScriptSubmittedProps, client

                        started_dt = datetime.fromisoformat(_session_meta["session_started"])
                        _fs_duration = (datetime.now() - started_dt).total_seconds()
                        client.track_final_script_submitted(
                            FinalScriptSubmittedProps(
                                step=_total_steps,
                                cell=int(_session_meta.get("cells_executed", 0)),
                                duration_seconds=_fs_duration,
                                total_tokens=int(_session_meta.get("total_tokens", 0)),
                            )
                        )
                    events.tool_message.send("core", text=f"\n--- FINAL SCRIPT ---\n\n{script_content}\n")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": tool_name,
                            "content": "Final script delivered successfully to the user. Awaiting feedback.",
                        }
                    )
                    _session_meta["current_mode"] = current_mode
                    if history_dir is None:
                        history_dir = init_history_dir(session_dump.name)
                    save_session_snapshot(history_dir, messages, _session_meta)
                    needs_user_input = True
                    continue

                elif tool_name == "execute_ipython":
                    script_to_run = tool_args.get("ipython_script", "")
                    desc = tool_args.get("description", "")

                    display_script = f"# {desc}\n{script_to_run}" if desc else script_to_run

                    cell_counter += 1
                    current_cell = cell_counter

                    is_repeated, repeat_warning = check_repeated_execution(display_script, exec_history)
                    if is_repeated:
                        _guardrail_counts["repeated_execution"] += 1
                        events.log_warn.send("core", text="Blocked: exact script ran multiple times.")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "name": tool_name,
                                "content": repeat_warning,
                            }
                        )
                        continue

                    try:
                        events.code_exec_start.send("core", script=display_script)
                        _current_phase = "sandbox_execution"
                        if script_to_run.strip() == "%restore":
                            sandbox.progress_callback = lambda cur, tot: events.restore_progress.send(
                                "core", current=cur, total=tot
                            )
                        else:
                            sandbox.progress_callback = None
                        try:
                            scr = run_cancellable(cancel_token, sandbox.execute, script_to_run, stop_token=stop_token)
                        finally:
                            sandbox.progress_callback = None
                            events.code_exec_end.send("core")
                    except CancelRequestedException:
                        sandbox.cancel()
                        events.log_info.send("core", text="Cancelled by token. Returning to prompt.")
                        events.operation_cancelled.send("core")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "name": tool_name,
                                "content": "SYSTEM: Execution cancelled by user.",
                            }
                        )
                        needs_user_input = True
                        continue

                    # ── Check sandbox-level errors for telemetry ──────────────────
                    if sandbox.last_error_info:
                        _track_synthetic_err(
                            sandbox.last_error_info["exception_class"],
                            sandbox.last_error_info["message"],
                            sandbox.last_error_info["line"],
                            sandbox.last_error_info["file"],
                        )

                    output_match_cell = None
                    for _prev_script, prev_output, prev_cell in exec_history:
                        if prev_output and scr == prev_output and len(scr) > 100:
                            output_match_cell = prev_cell
                            break

                    if output_match_cell is not None:
                        scr = (
                            f"The output is the same as Cell_{output_match_cell}. "
                            f"Use %view_output Cell_{output_match_cell} if you need to review it."
                        )
                    exec_history.append((display_script.strip(), scr, current_cell))
                    _session_meta["cells_executed"] += 1
                    _session_meta["cell_counter"] = cell_counter
                    _session_meta["current_mode"] = current_mode
                    if history_dir is None:
                        history_dir = init_history_dir(session_dump.name)
                    save_session_snapshot(history_dir, messages, _session_meta)
                    events.code_exec_output.send("core", output=scr)

                    tool_response_content = f"<terminal_output>\n{scr}\n</terminal_output>"

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": tool_name,
                            "content": tool_response_content,
                        }
                    )

                    awaiting_tool_complete = False

                elif tool_name == "switch_mode":
                    target_mode = tool_args.get("target_mode")
                    context_memo = tool_args.get("context", "")
                    events.mode_switch.send("core", mode=target_mode)

                    _prev_mode = current_mode
                    current_mode = target_mode

                    with suppress(Exception):
                        from .telemetry import ModeSwitchedProps, client

                        client.track_mode_switched(
                            ModeSwitchedProps(
                                from_mode=_prev_mode,
                                to_mode=target_mode,
                                step=_total_steps,
                                cell=int(_session_meta.get("cells_executed", 0)),
                            )
                        )

                    mode_injection = MODE_INJECTIONS.get(target_mode, "")

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": tool_name,
                            "content": "Mode switched successfully.",
                        }
                    )

                    mode_switch_notification = (
                        f"{mode_injection}\n\n--- Research memo from previous mode ---\n{context_memo}"
                    )
                    awaiting_mode_switch = True
                    _session_meta["current_mode"] = current_mode
                    if history_dir is None:
                        history_dir = init_history_dir(session_dump.name)
                    save_session_snapshot(history_dir, messages, _session_meta)

    except StopRequestedException:
        events.log_info.send("core", text="Agent loop stopped by user.")
        sandbox.cancel()
    except Exception as exc:
        _session_outcome = "crash"
        _crash_step = _total_steps
        _crash_cell = int(_session_meta.get("cells_executed", 0))
        _crash_phase = _current_phase
        _track_exc(exc)
        events.log_error.send("core", text=f"Unexpected error: {exc}")
        events.log_traceback.send("core")
        sandbox.cancel()
        with suppress(Exception):
            from .telemetry import SystemCrashProps, _extract_error_location, _sanitize_message, client

            _crash_line, _crash_file = _extract_error_location(exc)
            client.track_system_crash(
                SystemCrashProps(
                    crash_type="unhandled_exception",
                    exception_class=type(exc).__name__,
                    message=_sanitize_message(str(exc)),
                    line=_crash_line,
                    file=_crash_file,
                    module=type(exc).__module__,
                    active_command=client.active_command,
                    step=_crash_step,
                    cell=_crash_cell,
                    phase=_crash_phase,
                )
            )
    finally:
        try:
            _session_meta["session_ended"] = datetime.now().isoformat(timespec="seconds")
            try:
                started_dt = datetime.fromisoformat(_session_meta["session_started"])
                _session_meta["duration_seconds"] = int((datetime.now() - started_dt).total_seconds())
            except (ValueError, TypeError):
                _session_meta["duration_seconds"] = 0

            if history_dir is None:
                history_dir = init_history_dir(session_dump.name)
            save_session_snapshot(history_dir, messages, _session_meta)
            save_compressed_snapshot(history_dir, messages)

            recording_name = extract_recording_name(history_dir.name)
            new_dir = history_dir.parent / f"{recording_name}_{datetime.now():%Y%m%d_%H%M%S}"
            if new_dir != history_dir and not new_dir.exists():
                history_dir.rename(new_dir)
                history_dir = new_dir

            from ..cli.console import rename_file_logger

            rename_file_logger(history_dir.name)
        except Exception as exc:
            events.log_error.send("core", text=f"Failed to save session logs: {exc}")
            events.log_traceback.send("core")

        # ── Fire anonymous telemetry event ────────────────────────────────
        with suppress(Exception):
            from .telemetry import AgentSessionEndedProps, client

            _final_outcome = _session_outcome
            if _step_limit_reached and _final_outcome == "abandoned_by_user":
                _final_outcome = "step_limit_reached"

            client.track_agent_session_ended(
                AgentSessionEndedProps(
                    outcome=_final_outcome,
                    model=config.AGENT_MODEL,
                    total_tokens=int(_session_meta.get("total_tokens", 0)),
                    prompt_tokens=int(_session_meta.get("prompt_tokens", 0)),
                    completion_tokens=int(_session_meta.get("completion_tokens", 0)),
                    steps_taken=_total_steps,
                    cells_executed=int(_session_meta.get("cells_executed", 0)),
                    duration_seconds=float(_session_meta.get("duration_seconds", 0)),
                    proxy_enabled=bool(config.RECORDER_PROXY_ENABLED),
                    guardrails=dict(_guardrail_counts),
                    errors=list(_session_errors),
                    crash_step=_crash_step,
                    crash_cell=_crash_cell,
                    crash_phase=_crash_phase,
                )
            )

        sandbox.close()
