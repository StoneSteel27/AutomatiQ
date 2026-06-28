import logging
import queue
import threading
import time

from rich.console import Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from ..core import events
from ..core.cancel_standard import CancelToken, StopToken
from ..core.main import run_agent
from .console import (
    agent_markdown,
    code_block,
    countdown,
    error,
    info,
    log_exception,
    output_panel,
    prompt,
    think,
)

logger = logging.getLogger(__name__)

# Global state
_active_live = None
_first_prompt = True

# Streaming state
_thought_buf: list[str] = []
_text_buf: list[str] = []
_total_generation_time = 0.0
_generation_start = 0.0
_session_tokens = 0

# Phase: "streaming" | "tool_exec" (Live is stopped when neither is active)
_phase = "streaming"


# ── Streaming render helpers ────────────────────────────────────────────────


def _split_md_pending(buffer: str):
    """Split buffer at last newline.

    Returns ``(markdown_renderable, pending_text)``:
    - completed lines → :class:`Markdown` (re-parsed only on newline boundary)
    - in-progress line → :class:`Text` with a cursor block char ``▍``
    """
    if not buffer:
        return None, None
    idx = buffer.rfind("\n")
    if idx == -1:
        return None, Text(buffer + "\u258d", style="dim")
    completed = buffer[: idx + 1]
    pending = buffer[idx + 1 :]
    md = Markdown(completed) if completed.strip() else None
    pt = Text(pending + "\u258d", style="dim") if pending else None
    return md, pt


def _build_stream_group():
    """Build the streaming render group: content + spinner + status line."""
    thought_full = "".join(_thought_buf)
    text_full = "".join(_text_buf)

    # Thought panel
    thought_parts = []
    if thought_full:
        md, pt = _split_md_pending(thought_full)
        if md:
            thought_parts.append(md)
        if pt:
            thought_parts.append(pt)

    thought_panel = None
    if thought_parts:
        inner = thought_parts[0] if len(thought_parts) == 1 else Group(*thought_parts)
        thought_panel = Panel(
            inner,
            title="[think]Thinking[/think]",
            border_style="dim",
            padding=(0, 1),
        )

    # Text area (below thought panel, no panel)
    text_parts = []
    if text_full:
        md, pt = _split_md_pending(text_full)
        if md:
            text_parts.append(md)
        if pt:
            text_parts.append(pt)

    content_children = [p for p in [thought_panel] if p is not None] + text_parts
    content_area = Group(*content_children) if content_children else Text("")

    # Status line — only counts time while the model is generating
    if _generation_start:
        elapsed = int(_total_generation_time + (time.monotonic() - _generation_start))
    else:
        elapsed = int(_total_generation_time)
    status = Text(
        f"elapsed: {elapsed}s | session: {_session_tokens:,} tokens",
        style="dim",
    )

    spin = Spinner("aesthetic", text="Thinking... (Press Esc to Stop)", style="cyan")

    return Group(content_area, spin, status)


def _get_renderable():
    """Return the current Live renderable based on phase.

    Called by Live's auto-refresh thread (10x/sec) under the Live's lock.
    The Live is only active during 'streaming' and 'tool_exec' phases.
    """
    if _phase == "streaming":
        return _build_stream_group()
    return Spinner("aesthetic", text="Running... (Press Esc to Stop)", style="cyan")


def _start_live():
    """Start the persistent Live region if not already running."""
    global _active_live
    if _active_live is None:
        from .console import console

        _active_live = Live(
            get_renderable=_get_renderable,
            console=console,
            refresh_per_second=10,
            transient=False,
        )
        _active_live.__enter__()


def _stop_live():
    """Stop the active Live region if one exists."""
    global _active_live
    if _active_live is not None:
        _active_live.__exit__(None, None, None)
        _active_live = None


def _flush_stream_to_console():
    """Print buffered thought/text as permanent output via console.print().

    Called when a stream ends (normally or via safety net).  The content
    is inserted above the active Live region by rich's print-during-Live
    mechanism, making it permanent in the scrollback.
    """
    global _thought_buf, _text_buf
    thought_full = "".join(_thought_buf)
    text_full = "".join(_text_buf)
    if thought_full.strip():
        think(thought_full)
    if text_full.strip():
        agent_markdown(text_full)
    _thought_buf = []
    _text_buf = []


# ── Event handlers ──────────────────────────────────────────────────────────


@events.agent_thought_chunk.connect
def handle_agent_thought_chunk(sender, text, **kwargs):
    global _thought_buf
    _thought_buf.append(text)
    if _active_live is not None:
        _active_live.refresh()


@events.agent_text_chunk.connect
def handle_agent_text_chunk(sender, text, **kwargs):
    global _text_buf
    _text_buf.append(text)
    if _active_live is not None:
        _active_live.refresh()


@events.agent_stream_end.connect
def handle_agent_stream_end(sender, usage=None, **kwargs):
    """Stream completed — flush final content to console, stop Live."""
    global _session_tokens, _total_generation_time, _generation_start
    if _generation_start:
        _total_generation_time += time.monotonic() - _generation_start
        _generation_start = 0.0
    if usage is not None:
        _session_tokens += getattr(usage, "completion_tokens", 0) or 0
    _flush_stream_to_console()
    _stop_live()


@events.tool_message.connect
def handle_tool_message(sender, text, **kwargs):
    from .console import console

    console.print(text)


@events.mode_switch.connect
def handle_mode_switch(sender, mode, **kwargs):
    info(f"Switching to {mode} mode")


@events.code_exec_start.connect
def handle_code_exec_start(sender, script=None, **kwargs):
    global _phase
    if script is not None:
        code_block(script)
    _phase = "tool_exec"
    _start_live()


@events.code_exec_output.connect
def handle_code_exec_output(sender, output, **kwargs):
    output_panel(output)


@events.code_exec_end.connect
def handle_code_exec_end(sender, **kwargs):
    _stop_live()


@events.llm_request_start.connect
def handle_llm_request_start(sender, **kwargs):
    global _thought_buf, _text_buf, _generation_start, _phase
    _thought_buf = []
    _text_buf = []
    _generation_start = time.monotonic()
    _phase = "streaming"
    _start_live()


@events.llm_request_end.connect
def handle_llm_request_end(sender, **kwargs):
    """Safety net — if stream didn't complete normally, flush and stop Live."""
    global _total_generation_time, _generation_start
    if _generation_start:
        _total_generation_time += time.monotonic() - _generation_start
        _generation_start = 0.0
    _flush_stream_to_console()
    _stop_live()


# ── CLI entry point ─────────────────────────────────────────────────────────


def run_agent_cli(cancel_token: CancelToken = None, stop_token: StopToken = None, target: str | None = None):
    if cancel_token is None:
        cancel_token = CancelToken()
    if stop_token is None:
        stop_token = StopToken()

    input_queue = queue.Queue()

    @events.wait_start.connect
    def handle_wait_start(sender, seconds, reason, **kwargs):
        def should_abort():
            if stop_token.is_stopped():
                from ..core.cancel_standard import StopRequestedException

                raise StopRequestedException()
            return cancel_token.is_cancelled()

        # Stop Live so countdown has terminal control
        _stop_live()
        cancelled = countdown(seconds, message=reason, cancel_check=should_abort)
        if cancelled:
            cancel_token.reset()
            events.operation_cancelled.send("core")

    @events.prompt_request_start.connect
    def handle_prompt_request_start(sender, **kwargs):
        global _first_prompt
        if _first_prompt:
            info("Type in q to quit | Esc to cancel processing | Ctrl+Enter for new-line")
            _first_prompt = False

        # Stop Live so prompt_toolkit has terminal control
        _stop_live()
        try:
            ip = prompt()
        except (KeyboardInterrupt, EOFError):
            ip = "q"
        input_queue.put(ip)

    def backend_worker():
        try:
            run_agent(
                input_queue=input_queue,
                cancel_token=cancel_token,
                stop_token=stop_token,
                target=target,
            )
        except Exception as exc:
            error(f"Agent loop crashed: {exc}")
            log_exception()
        finally:
            events.agent_done.send("core")

    t = threading.Thread(target=backend_worker, daemon=True)
    t.start()

    try:
        done_event = threading.Event()

        @events.agent_done.connect
        def handle_agent_done(sender, **kwargs):
            done_event.set()

        while not done_event.wait(timeout=0.1):
            if stop_token.is_stopped():
                info("Abort requested via UI StopToken (Ctrl+C). Exiting...")
                break

        done_event.wait(timeout=5.0)

    except KeyboardInterrupt:
        info("Interrupted by OS user signal (Ctrl+C). Exiting...")
        stop_token.stop()
        done_event.wait(timeout=5.0)
    finally:
        _stop_live()
