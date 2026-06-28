"""Agent loop — the core interactive session where the LLM investigates a
recorded browser session and produces a standalone automation/extraction script."""  # noqa: E501

import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import litellm
from litellm.exceptions import (
    APIConnectionError,
    APIError,
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
)
from .guardrails import check_duplicate_thought, check_final_script_bounce, check_repeated_execution
from .history import compress_history, export_session_logs
from .ipython_sandbox import AgentSandbox
from .llm import call_llm_streaming, extract_message
from .prompts import MODE_INJECTIONS, SYSTEM_PROMPT
from .tools import AGENT_TOOLS, validate_tool_args

logger = logging.getLogger(__name__)
litellm.suppress_debug_info = True
litellm.drop_params = True

_preloaded_sandbox = None

# -----------------
# LIFECYCLE & HELPERS
# -----------------


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
    events.preload_end.send("core")


def run_cancellable(token: CancelToken, fn, *args, stop_token: StopToken = None, **kwargs):
    """Run *fn* in a thread, returning early if *token* is cancelled or *stop_token* is stopped."""
    result_box = [None]
    error_box = [None]
    done = threading.Event()

    def worker():
        try:
            result_box[0] = fn(*args, **kwargs)
        except Exception as exc:
            error_box[0] = exc
        finally:
            done.set()

    token.reset()
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    while not done.is_set():
        if token.is_cancelled():
            token.reset()
            raise CancelRequestedException("Cancelled via token")
        if stop_token and stop_token.is_stopped():
            raise StopRequestedException("Aborted via stop token")
        done.wait(timeout=0.15)
    if error_box[0] is not None:
        raise error_box[0]
    return result_box[0]


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


def run_agent(
    input_queue: queue.Queue,
    cancel_token: CancelToken = None,
    stop_token: StopToken = None,
    target: str | None = None,
):
    events.agent_start.send("core")
    """Interactive agent loop. Reads from the workspace produced by the recorder."""
    if cancel_token is None:
        cancel_token = CancelToken()

    session_dump = find_latest_session_dir(target)
    if not session_dump:
        if target:
            events.log_error.send("core", text=f"No valid completed session found at {target}")
        else:
            events.log_error.send("core", text="No valid completed sessions found in the current directory.")
        events.log_info.send(
            "core", text="Run 'automatiq record <url>' first, or use 'automatiq run <url>' for one-shot."
        )
        sys.exit(1)

    workspace_dir = session_dump / "workspace"
    events.log_info.send("core", text=f"Using session at: {session_dump}")

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

    # Initial state
    current_mode = "reading"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{MODE_INJECTIONS['reading']}\n\nSession started. You are in reading mode."},
    ]

    needs_user_input = True
    awaiting_tool_complete = False
    awaiting_mode_switch = False
    scr = ""
    mode_switch_notification = ""
    final_script_bounces = 0
    MAX_FINAL_SCRIPT_BOUNCES = 1
    MAX_VALIDATION_RETRIES = 3

    exec_history: list[tuple[str, str, int]] = []
    consecutive_validation_failures = 0
    cell_counter = 0
    prev_description = ""
    MAX_LLM_RETRIES = 5
    BASE_BACKOFF = 10

    consecutive_autonomous_turns = 0
    total_steps = 0

    try:
        while True:
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
                    events.agent_text.send(
                        "core",
                        text=f"⚠️ Paused: Agent hit {config.MAX_AGENT_STEPS} consecutive turns without completing task.",
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

            if needs_user_input:
                events.prompt_request_start.send("core")
                ip = input_queue.get()
                consecutive_autonomous_turns = 0
                events.prompt_request_end.send("core")
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
                    finally:
                        events.llm_request_end.send("core")

                    events.agent_stream_end.send("core", usage=usage)

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
                            SimpleNamespace(
                                id=tool_call_accumulator[idx]["id"],
                                function=SimpleNamespace(
                                    name=tool_call_accumulator[idx]["name"],
                                    arguments=tool_call_accumulator[idx]["arguments"],
                                ),
                            )
                            for idx in sorted_indices
                        ]
                        assistant_msg["tool_calls"] = [
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
                    events.log_error.send(
                        "core",
                        text=(
                            f"Model '{config.AGENT_MODEL}' is not found or unsupported. "
                            f"Please verify your config.toml file at ~/.automatiq/config.toml \nDetails: {msg}"
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
                    wait = BASE_BACKOFF * (2 ** (attempt - 1))
                    events.log_warn.send("core", text=f"LLM call failed (attempt {attempt}/{MAX_LLM_RETRIES}): {msg}")
                    events.log_traceback.send("core")
                    if attempt < MAX_LLM_RETRIES:
                        events.log_warn.send("core", text=f"Retrying in {wait}s ...")
                        events.wait_start.send("core", seconds=wait, reason="Retrying")

                        cancelled = False
                        for _ in range(wait):
                            if cancel_token.is_cancelled():
                                cancelled = True
                                break
                            time.sleep(1)
                        if cancelled:
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
                if reasoning:
                    events.agent_thought.send("core", text=reasoning)
                if content:
                    events.agent_text.send("core", text=content)
                needs_user_input = True
                continue

            tool_call = tool_calls[0]
            tool_name = tool_call.function.name
            try:
                tool_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as exc:
                tool_args = {}
                validation_error = f"Invalid JSON arguments: {exc}"
            else:
                validation_error = validate_tool_args(tool_name, tool_args)

            if validation_error:
                consecutive_validation_failures += 1
                messages.append(assistant_msg)

                if consecutive_validation_failures >= MAX_VALIDATION_RETRIES:
                    events.log_error.send(
                        "core", text=f"Tool validation failed {MAX_VALIDATION_RETRIES} times. Bailing out."
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": f"SYSTEM: Validation failed repeatedly. Error: {validation_error}. Returning.",
                        }
                    )
                    needs_user_input = True
                    consecutive_validation_failures = 0
                else:
                    events.log_warn.send("core", text=f"Tool validation error: {validation_error}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": f"SYSTEM: Tool Validation Error: {validation_error}. Please try again.",
                        }
                    )
                continue

            consecutive_validation_failures = 0

            total_steps += 1
            events.step_start.send("core", step=total_steps, prompt_tokens=usage.prompt_tokens if usage else 0)

            # Deduplicate logic based on description
            current_description = tool_args.get("description", "").strip() if tool_name == "execute_ipython" else ""
            duplicate_warning = check_duplicate_thought(current_description, prev_description)
            if duplicate_warning:
                events.log_warn.send("core", text="Exact duplicate description detected.")
                messages.append(assistant_msg)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": duplicate_warning,
                    }
                )
                continue

            if tool_name == "execute_ipython":
                prev_description = current_description

            # Append the LLM's assistant message
            messages.append(assistant_msg)
            if reasoning:
                events.agent_thought.send("core", text=reasoning)
            if content:
                events.agent_text.send("core", text=content)

            # Process the specific tool
            if tool_name == "final_submit":
                script_content = tool_args.get("final_python_script", "")

                final_script_bounces += 1
                should_bounce, bounce_message = check_final_script_bounce(
                    current_mode, final_script_bounces, MAX_FINAL_SCRIPT_BOUNCES
                )
                if should_bounce:
                    events.log_warn.send(
                        "core",
                        text="Final script submitted outside building mode (or verification needed) — bouncing back.",
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": bounce_message,
                        }
                    )
                    continue

                events.log_info.send("core", text="Agent submitted the final script.")
                events.tool_message.send("core", text=f"\n--- FINAL SCRIPT ---\n\n{script_content}\n")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": "Final script delivered successfully to the user. Awaiting feedback.",
                    }
                )
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
                    events.log_warn.send("core", text="Blocked: exact script ran multiple times.")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": repeat_warning,
                        }
                    )
                    continue

                try:
                    events.code_exec_start.send("core", script=display_script)
                    try:
                        scr = run_cancellable(cancel_token, sandbox.execute, script_to_run, stop_token=stop_token)
                    finally:
                        events.code_exec_end.send("core")
                except CancelRequestedException:
                    sandbox.cancel()
                    events.log_info.send("core", text="Cancelled by token. Returning to prompt.")
                    events.operation_cancelled.send("core")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": "SYSTEM: Execution cancelled by user.",
                        }
                    )
                    needs_user_input = True
                    continue

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
                events.code_exec_output.send("core", output=scr)

                tool_response_content = f"<terminal_output>\n{scr}\n</terminal_output>"

                messages.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "name": tool_name, "content": tool_response_content}
                )

                awaiting_tool_complete = False

            elif tool_name == "switch_mode":
                target_mode = tool_args.get("target_mode")
                context_memo = tool_args.get("context", "")
                events.mode_switch.send("core", mode=target_mode)

                current_mode = target_mode
                mode_injection = MODE_INJECTIONS.get(target_mode, "")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": "Mode switched successfully.",
                    }
                )

                mode_switch_notification = (
                    f"{mode_injection}\n\n--- Research memo from previous mode ---\n{context_memo}"
                )
                awaiting_mode_switch = True

    except StopRequestedException:
        events.log_info.send("core", text="Agent loop stopped by user.")
        sandbox.cancel()
    except Exception as exc:
        events.log_error.send("core", text=f"Unexpected error: {exc}")
        events.log_traceback.send("core")
        sandbox.cancel()
    finally:
        try:
            folder_name = export_session_logs(messages, session_dump.name)
            try:
                from ..cli.console import rename_file_logger

                rename_file_logger(folder_name)
            except Exception as e:
                events.log_warn.send("core", text=f"Could not rename log file: {e}")
        except Exception as exc:
            events.log_error.send("core", text=f"Failed to save session logs: {exc}")
            events.log_traceback.send("core")
        sandbox.close()
