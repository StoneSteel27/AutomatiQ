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
    code_block,
    countdown,
    error,
    info,
    log_exception,
    output_panel,
    prompt,
    spinner,
)

logger = logging.getLogger(__name__)

# Global state for UI elements that span events
_active_spinner = None  # code-exec spinner (console.status)
_active_live = None  # LLM streaming Live region
_first_prompt = True

# Streaming state
_thought_buf: list[str] = []
_text_buf: list[str] = []
_session_start_time = 0.0
_session_tokens = 0


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
        # No newline yet — all pending
        return None, Text(buffer + "\u258d", style="dim")
    completed = buffer[: idx + 1]
    pending = buffer[idx + 1 :]
    md = Markdown(completed) if completed.strip() else None
    pt = Text(pending + "\u258d", style="dim") if pending else None
    return md, pt


def _build_stream_group():
    """Build the live render group: content + spinner + status line."""
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

    # Status line
    elapsed = int(time.monotonic() - _session_start_time) if _session_start_time else 0
    status = Text(
        f"elapsed: {elapsed}s | session: {_session_tokens:,} tokens",
        style="dim",
    )

    spin = Spinner("aesthetic", text="Thinking... (Press Esc to Stop)", style="cyan")

    return Group(content_area, spin, status)


def _build_final_group():
    """Build the final render group: clean Markdown, no spinner/status."""
    thought_full = "".join(_thought_buf)
    text_full = "".join(_text_buf)

    parts = []
    if thought_full.strip():
        parts.append(
            Panel(
                Markdown(thought_full),
                title="[think]Thinking[/think]",
                border_style="dim",
                padding=(0, 1),
            )
        )
    if text_full.strip():
        parts.append(Markdown(text_full))

    return Group(*parts) if parts else Text("")


def _stop_live():
    """Stop the active Live region if one exists."""
    global _active_live
    if _active_live is not None:
        _active_live.__exit__(None, None, None)
        _active_live = None


# ── Event handlers ──────────────────────────────────────────────────────────


@events.agent_thought_chunk.connect
def handle_agent_thought_chunk(sender, text, **kwargs):
    global _thought_buf
    _thought_buf.append(text)
    if _active_live is not None:
        _active_live.update(_build_stream_group())


@events.agent_text_chunk.connect
def handle_agent_text_chunk(sender, text, **kwargs):
    global _text_buf
    _text_buf.append(text)
    if _active_live is not None:
        _active_live.update(_build_stream_group())


@events.agent_stream_end.connect
def handle_agent_stream_end(sender, usage=None, **kwargs):
    """Stream completed normally — finalize with clean Markdown and stop Live."""
    global _active_live, _session_tokens
    if usage is not None:
        _session_tokens += getattr(usage, "completion_tokens", 0) or 0
    if _active_live is not None:
        _active_live.update(_build_final_group())
        _stop_live()


@events.tool_message.connect
def handle_tool_message(sender, text, **kwargs):
    print(text)


@events.mode_switch.connect
def handle_mode_switch(sender, mode, **kwargs):
    info(f"Switching to {mode} mode")


@events.code_exec_start.connect
def handle_code_exec_start(sender, script=None, **kwargs):
    global _active_spinner
    if script is not None:
        code_block(script)
    if _active_spinner is None:
        _active_spinner = spinner("Running...(Press Esc to Stop)")
        _active_spinner.__enter__()


@events.code_exec_output.connect
def handle_code_exec_output(sender, output, **kwargs):
    output_panel(output)


@events.code_exec_end.connect
def handle_code_exec_end(sender, **kwargs):
    global _active_spinner
    if _active_spinner:
        _active_spinner.__exit__(None, None, None)
        _active_spinner = None


@events.llm_request_start.connect
def handle_llm_request_start(sender, **kwargs):
    global _active_live, _thought_buf, _text_buf
    # Stop any previous Live (retry after error)
    if _active_live is not None:
        _active_live.update(_build_final_group())
        _stop_live()
    _thought_buf = []
    _text_buf = []
    if _active_live is None:
        from .console import console

        _active_live = Live(
            _build_stream_group(),
            console=console,
            refresh_per_second=10,
            transient=False,
        )
        _active_live.__enter__()


@events.llm_request_end.connect
def handle_llm_request_end(sender, **kwargs):
    """Safety net — stop Live if the stream didn't complete normally."""
    if _active_live is not None:
        _active_live.update(_build_final_group())
        _stop_live()


# ── CLI entry point ─────────────────────────────────────────────────────────


def run_agent_cli(cancel_token: CancelToken = None, stop_token: StopToken = None, target: str | None = None):
    global _session_start_time
    if cancel_token is None:
        cancel_token = CancelToken()
    if stop_token is None:
        stop_token = StopToken()

    _session_start_time = time.monotonic()
    input_queue = queue.Queue()

    @events.wait_start.connect
    def handle_wait_start(sender, seconds, reason, **kwargs):
        def should_abort():
            if stop_token.is_stopped():
                from ..core.cancel_standard import StopRequestedException

                raise StopRequestedException()
            return cancel_token.is_cancelled()

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
        global _active_spinner, _active_live
        if _active_spinner:
            _active_spinner.__exit__(None, None, None)
            _active_spinner = None
        if _active_live is not None:
            _stop_live()
