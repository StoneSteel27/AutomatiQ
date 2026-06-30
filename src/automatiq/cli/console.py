"""
Centralized rich console for all AutomatiQ output.

Every module imports from here instead of using bare print() calls.
This gives us consistent styling, color-coded log levels, and
nice panels for agent output — all from a single shared Console.
"""

import logging
import os
import shutil
import signal
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

try:
    import readline  # noqa: F401
except ImportError:
    pass

try:
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings

    HAS_PT = True
except ImportError:
    HAS_PT = False

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from ..core import config, events
from ..core.cancel_standard import CancelToken, StopToken

_theme = Theme(
    {
        "info": "bold cyan",
        "debug": "dim",
        "warn": "bold yellow",
        "error": "bold red",
        "success": "bold green",
        "action": "magenta",
        "video": "blue",
        "ai": "bold magenta",
        "think": "italic",
        "exec": "bold yellow",
        "output": "white",
        "dim": "dim",
    }
)

console = Console(theme=_theme, highlight=False)

# ---------------------------------------------------------------------------
# File logger — writes timestamped entries + full tracebacks to
# output/logs/session_<timestamp>.log.  Initialized lazily by
# init_file_logger() which main.py calls after ensure_output_dirs().
# ---------------------------------------------------------------------------

_file_logger: logging.Logger | None = None


def init_file_logger(logs_dir: str) -> None:
    """Create the session log file and attach a file handler."""
    global _file_logger

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = f"{logs_dir}/session_{stamp}.log"

    _file_logger = logging.getLogger("automatiq.session")
    _file_logger.setLevel(logging.DEBUG)
    _file_logger.propagate = False

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-5s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    _file_logger.addHandler(handler)


def rename_file_logger(new_base_name: str) -> None:
    """Rename the active log file to match the provided base name."""
    global _file_logger
    if not _file_logger:
        return

    for handler in _file_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            old_path = handler.baseFilename
            handler.close()
            new_path = os.path.join(os.path.dirname(old_path), f"{new_base_name}.log")
            try:
                os.rename(old_path, new_path)
                handler.baseFilename = new_path
                handler.stream = handler._open()
            except Exception as e:
                # Fallback if rename fails
                handler.stream = handler._open()
                _file_logger.error(f"Failed to rename log file to {new_base_name}: {e}")
            break


def _log(level: int, msg: str) -> None:
    if _file_logger:
        _file_logger.log(level, msg)


def save_crash_report(crash_timestamp: str | None = None, crash_error: str | None = None) -> str | None:
    """Copy the session log to ./automatiq_crash_report.log in the current working directory.

    Optionally appends crash timestamp and error information at the end of the file.
    Returns the destination path on success, or None on failure.
    """
    log_path = None
    for handler in _file_logger.handlers if _file_logger else []:
        if isinstance(handler, logging.FileHandler):
            log_path = handler.baseFilename
            handler.flush()
            break

    if not log_path or not os.path.exists(log_path):
        print("\n[ERROR] No session log found to save crash report.")
        return None

    dest_log = os.path.join(os.getcwd(), "automatiq_crash_report.log")
    try:
        shutil.copy2(log_path, dest_log)

        if crash_timestamp or crash_error:
            with open(dest_log, "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 60 + "\n")
                f.write("AUTOMATIQ CRASH REPORT\n")
                f.write("=" * 60 + "\n\n")
                if crash_timestamp:
                    f.write(f"Crash Timestamp : {crash_timestamp}\n")
                if crash_error:
                    f.write(f"Error           : {crash_error}\n")
                f.write("\n" + "-" * 60 + "\n")
                f.write(
                    "NOTE: A crash occurred during the active recording session.\n"
                    "The recording was still saved, but a few actions and\n"
                    "requests may have been lost due to the abrupt termination.\n"
                )
                f.write("-" * 60 + "\n")

        print(f"\n[INFO] Crash report log saved to {dest_log}")
        return dest_log
    except Exception as e:
        print(f"\n[ERROR] Failed to save crash report log: {e}")
        return None


def info(msg: str) -> None:
    first_line = str(msg).splitlines()[0] if str(msg).splitlines() else str(msg)
    console.print(f"[info]\\[INFO][/info] {escape(first_line)}")
    _log(logging.INFO, msg)


def debug(msg: str) -> None:
    if config.VERBOSE:
        first_line = str(msg).splitlines()[0] if str(msg).splitlines() else str(msg)
        console.print(f"[debug]\\[DEBUG][/debug] {escape(first_line)}")
    _log(logging.DEBUG, msg)


def warn(msg: str) -> None:
    first_line = str(msg).splitlines()[0] if str(msg).splitlines() else str(msg)
    console.print(f"[warn]\\[WARN][/warn] {escape(first_line)}")
    _log(logging.WARNING, msg)


def error(msg: str) -> None:
    first_line = str(msg).splitlines()[0] if str(msg).splitlines() else str(msg)
    console.print(f"[error]\\[ERROR][/error] {escape(first_line)}")
    _log(logging.ERROR, msg)


def action(msg: str) -> None:
    console.print(f"[action]\\[ACTION][/action] {escape(msg)}")
    _log(logging.INFO, f"[ACTION] {msg}")


def video(msg: str) -> None:
    console.print(f"[video]\\[VIDEO][/video] {escape(msg)}")
    _log(logging.INFO, f"[VIDEO] {msg}")


def ai(msg: str) -> None:
    console.print(f"[ai]\\[AI][/ai] {escape(msg)}")
    _log(logging.INFO, f"[AI] {msg}")


def think(text: str) -> None:
    quoted = Panel(Markdown(text), title="[think]Thinking[/think]", border_style="dim", padding=(0, 1))
    console.print(quoted)


def agent_markdown(text: str) -> None:
    """Print standard agent text output as free-floating Markdown."""
    console.print(Markdown(text))


def countdown(seconds: int, message: str = "Retrying", cancel_check=None) -> bool:
    """Live countdown on a single line. Returns True if cancelled via *cancel_check*."""
    with Live(
        Text(f"{message} in {seconds}s ...", style="dim"), console=console, refresh_per_second=2, transient=True
    ) as live:
        for remaining in range(seconds, 0, -1):
            if cancel_check and cancel_check():
                return True
            live.update(Text(f"{message} in {remaining}s ...", style="dim"))
            time.sleep(1)
    return False


def code_block(code: str, lang: str = "python") -> None:
    syntax = Syntax(code, lang, theme="monokai", line_numbers=True, word_wrap=True)
    console.print(Panel(syntax, title="[exec]EXEC[/exec]", border_style="yellow", padding=(0, 0)))


def output_panel(text: str) -> None:
    console.print(Panel(escape(text), title="[output]OUTPUT[/output]", border_style="dim", padding=(0, 1)))


def rule(title: str = "", style: str = "dim") -> None:
    console.print(Rule(title=title, style=style))


def log_exception() -> None:
    """Plain traceback to the log file only — nothing on terminal."""
    if _file_logger:
        _file_logger.error(traceback.format_exc())


def spinner(message: str = "Working..."):
    """Returns a rich Status context manager for use with `with` blocks."""
    return console.status(f"[dim]{message}[/dim]", spinner="aesthetic", spinner_style="cyan")


_active_listener = None


class CLIListener:
    """Wrapper around threading.Event to cleanly stop/pause the listener thread and restore terminal."""

    def __init__(
        self,
        active_event: threading.Event,
        thread: threading.Thread,
        paused_event: threading.Event,
        paused_ack_event: threading.Event,
    ):
        self._active_event = active_event
        self._thread = thread
        self._paused_event = paused_event
        self._paused_ack_event = paused_ack_event

    def pause(self) -> None:
        if self._thread and self._thread.is_alive():
            self._paused_event.set()
            self._paused_ack_event.wait(timeout=1.0)

    def resume(self) -> None:
        self._paused_event.clear()

    def clear(self) -> None:
        global _active_listener
        if _active_listener is self:
            _active_listener = None
        self._active_event.clear()
        self._paused_event.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def is_set(self) -> bool:
        return self._active_event.is_set()

    def __bool__(self) -> bool:
        return True


def get_prompt_toolkit_bindings():
    kb = KeyBindings()

    # Alt+Enter (escape, enter) or Ctrl+Enter/Ctrl+J (c-j) inserts a newline
    @kb.add("escape", "enter")
    @kb.add("c-j")
    def _(event):
        event.current_buffer.insert_text("\n")

    # Normal Enter submits the entire buffer
    @kb.add("enter")
    def _(event):
        event.current_buffer.validate_and_handle()

    return kb


def prompt_continuation(width, line_number, is_soft_wrap):
    return "." * (width - 1) + " "


def prompt() -> str:
    global _active_listener
    if _active_listener:
        _active_listener.pause()
    try:
        # Give asynchronous Rich spinners time to fully exit and clear lines
        time.sleep(0.1)
        if HAS_PT:
            return pt_prompt(
                HTML("<ansigreen><bold>&gt;&gt;&gt; </bold></ansigreen>"),
                multiline=True,
                prompt_continuation=prompt_continuation,
                key_bindings=get_prompt_toolkit_bindings(),
            )
        else:
            if os.name != "nt":
                return input("\x01\033[1;32m\x02>>> \x01\033[0m\x02")
            else:
                return console.input("[bold green]>>> [/bold green]")
    finally:
        if _active_listener:
            _active_listener.resume()


def ask_session_name() -> str:
    global _active_listener
    if _active_listener:
        _active_listener.pause()
    try:
        # Give asynchronous Rich spinners time to fully exit and clear lines
        time.sleep(0.1)
        if HAS_PT:
            return pt_prompt(
                HTML(
                    "<ansicyan><bold>Enter a name for this recording session "
                    "(leave blank to auto-detect domain): </bold></ansicyan>"
                )
            ).strip()
        else:
            if os.name != "nt":
                p = (
                    "\x01\033[1;36m\x02Enter a name for this recording session "
                    "(leave blank to auto-detect domain): \x01\033[0m\x02"
                )
                return input(p).strip()
            else:
                msg = (
                    "[bold cyan]Enter a name for this recording session "
                    "(leave blank to auto-detect domain): [/bold cyan]"
                )
                return console.input(msg).strip()
    finally:
        if _active_listener:
            _active_listener.resume()


def ask_resume_session(dirs: list | None = None, sessions: list | None = None) -> Path | None:
    """Interactively pick a session to resume.

    Always shows a numbered picker of available sessions.
    Enter selects the first (latest) session, or type a number/index.
    If *dirs* is provided, only those sessions are shown.
    If *sessions* is provided, use them instead of scanning HISTORY_DIR
    (avoids re-scanning when the preload thread already did it).
    Returns the selected history_dir Path, or None if cancelled.
    """
    from ..core.history import list_resumable_sessions

    global _active_listener

    def _pause():
        if _active_listener:
            _active_listener.pause()

    def _resume():
        if _active_listener:
            _active_listener.resume()

    def _do_prompt(message: str) -> str:
        _pause()
        try:
            time.sleep(0.1)
            if HAS_PT:
                return pt_prompt(HTML(f"<ansicyan><bold>{message}</bold></ansicyan>")).strip()
            elif os.name != "nt":
                return input(f"\x01\033[1;36m\x02{message}\x01\033[0m\x02").strip()
            else:
                return console.input(f"[bold cyan]{message}[/bold cyan]").strip()
        finally:
            _resume()

    # ── Gather sessions to display ──────────────────────────────────────────
    all_sessions = sessions if sessions is not None else list_resumable_sessions()
    if not all_sessions:
        error("No resumable sessions found.")
        return None

    if dirs is not None:
        sessions = [s for s in all_sessions if s.history_dir in dirs]
        if not sessions:
            error("No matching sessions found.")
            return None
    else:
        sessions = all_sessions

    # ── Show numbered picker ─────────────────────────────────────────────────
    console.print()
    t = Table(show_header=False, box=None, collapse_padding=True, padding=(0, 1))
    t.add_column(style="bold cyan", width=5)
    t.add_column(style="bold", min_width=20)
    t.add_column(style="dim")
    for i, s in enumerate(sessions, 1):
        marker = " (latest)" if i == 1 else ""
        t.add_row(f"{i}.", f"{s.recording_name}{marker}", s.human_timestamp)
    console.print(t)
    console.print()

    choice = _do_prompt("Select session [1]: ")
    if not choice:
        idx = 1
    else:
        try:
            idx = int(choice)
        except ValueError:
            # Try matching by name text
            filtered = [s for s in sessions if choice in s.recording_name or choice in s.folder_name]
            if len(filtered) == 1:
                return filtered[0].history_dir
            if not filtered:
                error(f"No sessions matching '{choice}'.")
                return None
            # Multiple name matches — show sub-picker
            sessions = filtered
            console.print()
            t2 = Table(show_header=False, box=None, collapse_padding=True, padding=(0, 1))
            t2.add_column(style="bold cyan", width=5)
            t2.add_column(style="bold", min_width=20)
            t2.add_column(style="dim")
            for i, s in enumerate(sessions, 1):
                t2.add_row(f"{i}.", s.recording_name, s.human_timestamp)
            console.print(t2)
            console.print()
            choice = _do_prompt("Select [1]: ")
            if not choice:
                idx = 1
            else:
                try:
                    idx = int(choice)
                except ValueError:
                    error(f"Invalid selection: {choice}")
                    return None

    if 1 <= idx <= len(sessions):
        return sessions[idx - 1].history_dir
    error(f"Selection out of range: {idx}")
    return None


def start_cli_listeners(cancel_token: CancelToken, stop_token: StopToken, on_force_quit=None) -> CLIListener | None:
    if not sys.stdin.isatty():
        return None

    active = threading.Event()
    active.set()
    paused = threading.Event()
    paused_ack = threading.Event()

    # Catch raw OS SIGINT to prevent KeyboardInterrupt from crashing the main thread on the first press
    try:

        def sigint_handler(signum, frame):
            if stop_token.is_stopped():
                # Second press: Force a hard exit
                print("\n[ERROR] Force quit requested (Double Ctrl+C). Shutting down immediately.")
                if on_force_quit:
                    try:
                        on_force_quit()
                    except Exception:
                        pass
                os._exit(1)
            else:
                stop_token.stop()

        signal.signal(signal.SIGINT, sigint_handler)
    except (ValueError, OSError):
        pass  # Fails gracefully if not running in the main thread

    def _listen():
        try:
            from prompt_toolkit.input import create_input
            from prompt_toolkit.keys import Keys

            inp = create_input()
        except Exception:
            return  # Fallback gracefully if prompt_toolkit fails to initialize

        while active.is_set():
            if paused.is_set():
                paused_ack.set()
                while paused.is_set() and active.is_set():
                    time.sleep(0.05)
                if active.is_set():
                    paused_ack.clear()

            if not active.is_set():
                break

            try:
                # Enter raw mode only while actively listening
                with inp.raw_mode():
                    while active.is_set() and not paused.is_set():
                        keys = inp.read_keys()
                        for k in keys:
                            if k.key == Keys.Escape:
                                cancel_token.cancel()
                            elif k.key == Keys.ControlC:
                                if stop_token.is_stopped():
                                    print("\n[ERROR] Force quit requested (Double Ctrl+C). Shutting down immediately.")
                                    if on_force_quit:
                                        try:
                                            on_force_quit()
                                        except Exception:
                                            pass
                                    os._exit(1)
                                else:
                                    stop_token.stop()
                        time.sleep(0.05)
            except Exception:
                time.sleep(0.05)

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    global _active_listener
    _active_listener = CLIListener(active, t, paused, paused_ack)
    return _active_listener


# ---------------------------------------------------------------------------
# Global Event Listeners for UI updates
# ---------------------------------------------------------------------------


@events.log_info.connect
def handle_log_info(sender, text, **kwargs):
    if text.startswith("[ACTION]"):
        action(text.replace("[ACTION]", "", 1).strip())
    elif text.startswith("[VIDEO]"):
        video(text.replace("[VIDEO]", "", 1).strip())
    elif text.startswith("[AI]"):
        ai(text.replace("[AI]", "", 1).strip())
    else:
        info(text)


@events.log_debug.connect
def handle_log_debug(sender, text, **kwargs):
    debug(text)


@events.log_warn.connect
def handle_log_warn(sender, text, **kwargs):
    warn(text)


@events.log_error.connect
def handle_log_error(sender, text, **kwargs):
    error(text)


@events.log_traceback.connect
def handle_log_traceback(sender, **kwargs):
    log_exception()
