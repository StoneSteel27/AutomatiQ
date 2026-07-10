"""
CLI entry point for AutomatiQ.

Usage:
    python -m automatiq record <url>   # Record a browser session
    python -m automatiq agent          # Run the agent on an existing workspace
    python -m automatiq run <url>      # Record, then launch the agent
"""

import argparse
import logging
import multiprocessing
import sys
import threading

from .cli.console import error, info, rule

# ---------------------------------------------------------------------------
# Banner gate — suppresses RichHandler output while the startup Live block
# is active so that preload-thread logs don't bleed above the animation.
# ---------------------------------------------------------------------------
_banner_done = threading.Event()
_banner_done.set()  # default: not animating, allow output freely


class _GatedRichHandler(logging.Handler):
    """Wraps a RichHandler but buffers records while the banner is live."""

    def __init__(self, inner: logging.Handler):
        super().__init__()
        self._inner = inner
        self._buf: list[logging.LogRecord] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if _banner_done.is_set():
            # Banner finished — flush any buffered records first, then emit.
            with self._lock:
                buf, self._buf = self._buf, []
            for r in buf:
                self._inner.emit(r)
            self._inner.emit(record)
        else:
            with self._lock:
                self._buf.append(record)

    def flush_buffer(self) -> None:
        """Call once after banner finishes to drain any held records."""
        with self._lock:
            buf, self._buf = self._buf, []
        for r in buf:
            self._inner.emit(r)


# ---------------------------------------------------------------------------
# Background preload — runs concurrently with the startup banner so that
# heavy modules are already imported and directories exist by the time the
# animation finishes.
#
# We peek at sys.argv before argparse runs so we can preload only what the
# chosen sub-command actually needs:
#   agent          → litellm, IPython, yaml
#   record / run   → zendriver, mss, numpy, imageio_ffmpeg  (+ agent deps for run)
# ---------------------------------------------------------------------------

_preload_error = None  # captured if preload raises unexpectedly
_preloaded_sessions = None  # pre-scanned history sessions for resume (during banner)


def _peek_command() -> str:
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            return arg
    return ""


def _peek_model() -> str | None:
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--model" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1]
    return None


def _peek_base_url() -> str | None:
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--base-url" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--base-url="):
            return arg.split("=", 1)[1]
    return None


def _peek_output_dir() -> str | None:
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--output-dir" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--output-dir="):
            return arg.split("=", 1)[1]
    return None


def _peek_session_name() -> str | None:
    """Quick scan of cwd for latest recording session name (for banner display)."""
    from pathlib import Path

    cwd = Path.cwd()
    latest, latest_mtime = None, 0.0
    for d in cwd.iterdir():
        meta = d / "session_metadata.json"
        if d.is_dir() and meta.exists():
            m = meta.stat().st_mtime
            if m > latest_mtime:
                latest_mtime, latest = m, d.name
    return latest


def _peek_resume_name() -> str | None:
    """Peek at sys.argv for the positional name argument after 'resume'."""
    argv = sys.argv[1:]
    found_resume = False
    for arg in argv:
        if arg == "resume":
            found_resume = True
            continue
        if found_resume and not arg.startswith("-"):
            return arg
    return None


def _preload():
    global _preload_error, _preloaded_sessions
    try:
        from .core import config

        out_dir = _peek_output_dir()
        if out_dir:
            from pathlib import Path

            config.OUTPUT_DIR = Path(out_dir)
            config.WORKSPACE_DIR = config.OUTPUT_DIR / "workspace"
            config.BLOCKLIST_DIR = config.OUTPUT_DIR / "blocklist"
            config.BLOCKLIST_DB = config.OUTPUT_DIR / "blocklist.db"

        config.ensure_system_dirs()

        cmd = _peek_command()

        _is_verbose = "--verbose" in sys.argv

        if _is_verbose:
            config.VERBOSE = True

        import logging

        from rich.logging import RichHandler

        from .cli.console import console

        level = logging.DEBUG if config.VERBOSE else logging.INFO

        _raw_handler = RichHandler(
            console=console, show_time=False, show_path=config.VERBOSE, markup=False, rich_tracebacks=True
        )
        _raw_handler.setLevel(level)
        rich_handler = _GatedRichHandler(_raw_handler)
        rich_handler.setLevel(level)

        automatiq_logger = logging.getLogger("automatiq")
        automatiq_logger.setLevel(logging.DEBUG)
        automatiq_logger.handlers.clear()
        automatiq_logger.addHandler(rich_handler)

        from .cli.console import init_file_logger

        init_file_logger(str(config.LOGS_DIR))

        if cmd in ("agent", "run", "", "resume"):
            import IPython  # noqa: F401
            import litellm  # noqa: F401
            import yaml  # noqa: F401

            litellm.suppress_debug_info = not _is_verbose

            from .core import events
            from .core.bin_manager import ensure_binaries

            ensure_binaries()
            events.preload_start.send("cli")

        if cmd in ("record", "run"):
            import imageio_ffmpeg  # noqa: F401
            import mss  # noqa: F401
            import numpy  # noqa: F401
            import zendriver  # noqa: F401

            if cmd == "record":
                import litellm  # noqa: F401

                litellm.suppress_debug_info = not _is_verbose

        if cmd == "resume":
            from .core.history import list_resumable_sessions

            global _preloaded_sessions
            _preloaded_sessions = list_resumable_sessions()

    except Exception as exc:
        _preload_error = exc


def _apply_config_overrides(args):
    from .core import config

    if getattr(args, "model", None):
        config.AGENT_MODEL = args.model
    if getattr(args, "recorder_model", None):
        config.RECORDER_AI_MODEL = args.recorder_model
    if getattr(args, "output_dir", None):
        from pathlib import Path

        config.OUTPUT_DIR = Path(args.output_dir)
        config.WORKSPACE_DIR = config.OUTPUT_DIR / "workspace"
        config.BLOCKLIST_DIR = config.OUTPUT_DIR / "blocklist"
        config.BLOCKLIST_DB = config.OUTPUT_DIR / "blocklist.db"
    if getattr(args, "max_steps", None) is not None:
        config.MAX_AGENT_STEPS = args.max_steps
    if getattr(args, "sandbox_timeout", None) is not None:
        config.SANDBOX_TIMEOUT_SECONDS = args.sandbox_timeout
    if getattr(args, "base_url", None):
        config.API_BASE = args.base_url
    if getattr(args, "no_banner", False):
        config.BANNER_ENABLED = False
    if getattr(args, "no_telemetry", False):
        config.TELEMETRY_ENABLED = False
    if getattr(args, "verbose", False):
        config.VERBOSE = True
    if getattr(args, "browser", None):
        config.BROWSER_TYPE = args.browser


def _browser_progress_callback():
    """Return a Rich-aware progress callback for browser downloads.

    The callback prints a single updated line per chunk to avoid spamming the
    console while still showing progress. Yields ``None`` if rich isn't ready.
    """
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    from .cli.console import console

    progress = Progress(
        TextColumn("[bold blue]{task.fields[label]}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )
    task_id = None

    def _cb(downloaded: int, total: int) -> None:
        nonlocal task_id
        if task_id is None:
            if total <= 0:
                # Unknown size — just show a counter.
                task_id = progress.add_task("download", label="Brave", total=None)
            else:
                task_id = progress.add_task("download", label="Brave", total=total)
            progress.start()
        progress.update(task_id, completed=downloaded, total=total or None)

    progress.start()
    return _cb, progress


def _confirm_brave_download(question: str) -> bool:
    """Ask the user whether to download Brave. Defaults to yes on Enter."""
    from .cli.console import console, prompt_yes_no

    try:
        # Pause any active listener so keystrokes go to the prompt.
        from .cli.console import _active_listener

        if _active_listener:
            _active_listener.pause()
        try:
            import time

            time.sleep(0.1)
            console.print()
            console.print(
                "[bold yellow]Brave not found.[/bold yellow] Brave ships built-in "
                "anti-fingerprinting and anti-tracking protections that help keep "
                "the recorder stealthy against websites — fewer detection signals "
                "than a default Chrome profile, making it less likely your automation "
                "is noticed by anti-bot defenses."
            )
            console.print()
            return prompt_yes_no("Download a portable Brave copy now? (~300 MB)", default=True)
        finally:
            if _active_listener:
                _active_listener.resume()
    except Exception:
        return True


def _resolve_browser_for_recording(args) -> tuple[str, object | None, str]:
    """Decide which browser to launch for record/run, prompting if needed.

    Returns a ``(verb, value, descriptor)`` tuple suitable for
    ``BrowserAgent.run_session``.  When the CLI flags opt out of auto-download
    (``--no-auto-download-browser``), or the user answers "no" to the prompt,
    we fall straight through to whatever Chrome zendriver can find.
    """
    from .core.browser_manager import resolve_browser_for_recording

    no_auto = bool(getattr(args, "no_auto_download_browser", False))

    if no_auto:
        return resolve_browser_for_recording(
            no_auto_download=True,
            prompt_callback=None,
        )

    # Build a progress callback lazily — only used if we actually download.
    progress_holder: dict = {}

    def _prompt_then_maybe_download(question: str) -> bool:
        agreed = _confirm_brave_download(question)
        if not agreed:
            return False
        # User said yes — set up the progress callback for the download.
        cb, progress = _browser_progress_callback()
        progress_holder["cb"] = cb
        progress_holder["progress"] = progress
        return True

    try:
        return resolve_browser_for_recording(
            no_auto_download=False,
            prompt_callback=_prompt_then_maybe_download,
            progress_callback=lambda d, t: progress_holder.get("cb", lambda *_: None)(d, t),
        )
    finally:
        progress = progress_holder.get("progress")
        if progress is not None:
            progress.stop()


def cmd_setup(args):
    """`automatiq setup brave` — download a portable Brave browser."""
    from .core import config
    from .core.browser_manager import ensure_brave, find_brave_executable

    _apply_config_overrides(args)
    channel = getattr(args, "channel", None) or config.BROWSER_CHANNEL
    force = bool(getattr(args, "force", False))

    # If already cached and user didn't force, just print the path.
    if not force:
        cached = find_brave_executable(channel=channel)
        if cached is not None:
            info(f"Brave ({channel}) is already available: {cached}")
            info("Use --force to re-download.")
            return

    info(f"Downloading Brave ({channel})...")
    cb, progress = _browser_progress_callback()
    try:
        path = ensure_brave(channel=channel, progress_callback=cb, force=force)
        info(f"Brave ready: {path}")
    except Exception as exc:
        error(f"Failed to download Brave: {exc}")
        sys.exit(1)
    finally:
        progress.stop()


def cmd_record(args):
    _apply_config_overrides(args)
    from .core import config
    from .core.key_checker import check_api_keys

    check_api_keys(config.AGENT_MODEL, config.RECORDER_AI_MODEL)
    import tempfile
    from pathlib import Path

    from .cli.callbacks import get_cli_skip_callback
    from .cli.console import ask_session_name, start_cli_listeners
    from .core.cancel_standard import CancelToken, StopRequestedException, StopToken
    from .core.recorder import run_recording
    from .core.recorder.compile.serializers import sanitize_filename

    session_name = args.name if args.name is not None else ask_session_name()

    if session_name:
        output_dir_name = sanitize_filename(session_name)
        config.OUTPUT_DIR = Path.cwd() / output_dir_name
    else:
        temp_dir = tempfile.mkdtemp(prefix="automatiq_recording_")
        config.OUTPUT_DIR = Path(temp_dir)

    config.WORKSPACE_DIR = config.OUTPUT_DIR / "workspace"
    config.BLOCKLIST_DIR = config.OUTPUT_DIR / "blocklist"
    config.BLOCKLIST_DB = config.OUTPUT_DIR / "blocklist.db"
    config.ensure_output_dirs()

    cancel_token = CancelToken()
    stop_token = StopToken()

    def handle_force_quit():
        if not session_name:
            import shutil

            from .cli.console import save_crash_report

            save_crash_report()
            shutil.rmtree(config.OUTPUT_DIR, ignore_errors=True)

    monitor = start_cli_listeners(cancel_token, stop_token, on_force_quit=handle_force_quit)
    try:
        success = run_recording(
            url=args.url,
            session_name=session_name,
            cancel_token=cancel_token,
            stop_token=stop_token,
            skip_callback=get_cli_skip_callback(),
            proxy=getattr(args, "proxy", None),
            no_proxy=getattr(args, "no_proxy", False),
            browser_resolution=_resolve_browser_for_recording(args),
        )
    except KeyboardInterrupt:
        from .cli.console import warn

        warn("KeyboardInterrupt caught in __main__.")
        success = False
    except StopRequestedException:
        success = False
    finally:
        if monitor:
            monitor.clear()

    if not success and not session_name:
        # Cleanup the temp directory if recording failed/aborted and we used a temp dir
        import shutil

        shutil.rmtree(config.OUTPUT_DIR, ignore_errors=True)

    if not success:
        error("Recording failed, aborted, or produced no output.")
        sys.exit(1)
    info("Recording complete. Run 'automatiq agent' to start the agent.")


def cmd_agent(args):
    _apply_config_overrides(args)
    from .core import config
    from .core.key_checker import check_api_keys

    check_api_keys(config.AGENT_MODEL)

    from .cli.console import start_cli_listeners
    from .cli.orchestrator import run_agent_cli
    from .core.cancel_standard import CancelToken, StopToken

    cancel_token = CancelToken()
    stop_token = StopToken()

    def handle_force_quit():
        from .cli.console import save_crash_report

        save_crash_report()

    monitor = start_cli_listeners(cancel_token, stop_token, on_force_quit=handle_force_quit)
    try:
        run_agent_cli(cancel_token=cancel_token, stop_token=stop_token, target=args.target)
    finally:
        if monitor:
            monitor.clear()


def cmd_resume(args):
    _apply_config_overrides(args)
    from .core import config
    from .core.key_checker import check_api_keys

    check_api_keys(config.AGENT_MODEL)

    from .cli.console import ask_resume_session, error, info, start_cli_listeners
    from .cli.orchestrator import run_agent_cli
    from .core.cancel_standard import CancelToken, StopToken

    if args.name:
        if _preloaded_sessions is not None:
            filtered = [s for s in _preloaded_sessions if args.name in s.recording_name or args.name in s.folder_name]
            if len(filtered) == 0:
                error(f"No resumable sessions found matching '{args.name}'")
                sys.exit(1)
            history_dir = (
                filtered[0].history_dir
                if len(filtered) == 1
                else ask_resume_session(dirs=[s.history_dir for s in filtered], sessions=filtered)
            )
        else:
            from .core.history import find_history_dirs

            dirs = find_history_dirs(args.name)
            if len(dirs) == 0:
                error(f"No resumable sessions found matching '{args.name}'")
                sys.exit(1)
            history_dir = dirs[0] if len(dirs) == 1 else ask_resume_session(dirs)
    else:
        history_dir = ask_resume_session(sessions=_preloaded_sessions)

    if history_dir is None:
        info("Resume cancelled.")
        sys.exit(0)

    cancel_token = CancelToken()
    stop_token = StopToken()

    def handle_force_quit():
        from .cli.console import save_crash_report

        save_crash_report()

    monitor = start_cli_listeners(cancel_token, stop_token, on_force_quit=handle_force_quit)
    try:
        run_agent_cli(cancel_token=cancel_token, stop_token=stop_token, resume_from=str(history_dir))
    finally:
        if monitor:
            monitor.clear()


def cmd_run(args):
    _apply_config_overrides(args)
    from .core import config
    from .core.key_checker import check_api_keys

    check_api_keys(config.AGENT_MODEL, config.RECORDER_AI_MODEL)
    import tempfile
    from pathlib import Path

    from .cli.callbacks import get_cli_skip_callback
    from .cli.console import ask_session_name, start_cli_listeners
    from .cli.orchestrator import run_agent_cli
    from .core.cancel_standard import CancelToken, StopRequestedException, StopToken
    from .core.recorder import run_recording
    from .core.recorder.compile.serializers import sanitize_filename

    session_name = args.name if args.name is not None else ask_session_name()

    if session_name:
        output_dir_name = sanitize_filename(session_name)
        config.OUTPUT_DIR = Path.cwd() / output_dir_name
    else:
        temp_dir = tempfile.mkdtemp(prefix="automatiq_recording_")
        config.OUTPUT_DIR = Path(temp_dir)

    config.WORKSPACE_DIR = config.OUTPUT_DIR / "workspace"
    config.BLOCKLIST_DIR = config.OUTPUT_DIR / "blocklist"
    config.BLOCKLIST_DB = config.OUTPUT_DIR / "blocklist.db"
    config.ensure_output_dirs()

    cancel_token = CancelToken()
    stop_token = StopToken()

    def handle_force_quit():
        if not session_name:
            import shutil

            from .cli.console import save_crash_report

            save_crash_report()
            shutil.rmtree(config.OUTPUT_DIR, ignore_errors=True)

    monitor = start_cli_listeners(cancel_token, stop_token, on_force_quit=handle_force_quit)
    try:
        success = run_recording(
            url=args.url,
            session_name=session_name,
            cancel_token=cancel_token,
            stop_token=stop_token,
            skip_callback=get_cli_skip_callback(),
            proxy=getattr(args, "proxy", None),
            no_proxy=getattr(args, "no_proxy", False),
            browser_resolution=_resolve_browser_for_recording(args),
        )
    except KeyboardInterrupt:
        from .cli.console import warn

        warn("KeyboardInterrupt caught in __main__.")
        success = False
    except StopRequestedException:
        success = False
    finally:
        if monitor:
            monitor.clear()

    if not success and not session_name:
        # Cleanup the temp directory if recording failed/aborted and we used a temp dir
        import shutil

        shutil.rmtree(config.OUTPUT_DIR, ignore_errors=True)

    if not success:
        error("Recording failed or aborted. Aborting agent launch.")
        sys.exit(1)

    rule("Recording complete. Launching agent...", style="bold green")

    cancel_token = CancelToken()
    # We will pass stop_token down to the agent if we want, but for now we reset it
    stop_token = StopToken()

    def handle_agent_force_quit():
        from .cli.console import save_crash_report

        save_crash_report()

    monitor = start_cli_listeners(cancel_token, stop_token, on_force_quit=handle_agent_force_quit)
    try:
        run_agent_cli(cancel_token=cancel_token, stop_token=stop_token)
    except StopRequestedException:
        info("Agent aborted by user.")
    finally:
        if monitor:
            monitor.clear()


def cmd_feedback(args):
    """`automatiq feedback ["message"]` — send anonymous feedback to the maintainers."""
    _apply_config_overrides(args)
    from .cli.console import console, info

    message = args.message.strip() if args.message else ""
    if not message:
        console.print("[bold blue]Anonymous Feedback Box[/bold blue]")
        console.print("Your feedback helps make AutomatiQ better!")
        console.print(
            "[dim]Press [bold]Alt+Enter[/bold] (or [bold]Escape[/bold] then [bold]Enter[/bold]) to submit.[/dim]"
        )
        console.print("[dim]Press [bold]Enter[/bold] to start a new line.[/dim]\n")

        try:
            from prompt_toolkit import prompt as pt_prompt
            from prompt_toolkit.formatted_text import HTML

            message = pt_prompt(
                HTML("<ansiblue><bold>Enter feedback below:</bold></ansiblue>\n"),
                multiline=True,
            )
        except ImportError:
            console.print("[yellow]prompt_toolkit not found. Standard multiline fallback active.[/yellow]")
            console.print("[dim]Enter feedback (type Ctrl+Z or Ctrl+D on a new line to submit):[/dim]")
            lines = []
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                lines.append(line)
            message = "\n".join(lines)

        message = message.strip()
        if not message:
            error("Feedback message cannot be empty.")
            sys.exit(1)

    from .core.telemetry import client

    client.start(command="feedback")
    if not client.is_enabled:
        info("Telemetry is disabled — feedback was not sent.")
        info("Enable telemetry in ~/.automatiq/config.toml to submit feedback.")
        sys.exit(0)

    client.track_feedback(message)
    client.flush_sync(timeout=3.0)
    client.stop()
    info("Thank you! Your feedback has been sent.")


# ---------------------------------------------------------------------------
# Custom Rich help page — replaces argparse's default --help output.
# ---------------------------------------------------------------------------


def _print_rich_help():
    from rich.table import Table
    from rich.text import Text

    from .cli.console import console
    from .core import config

    console.print()
    ver = config.VERSION
    console.print(
        f"[bold]AutomatiQ[/bold] [dim]v{ver}[/dim]"
        " — Record browser sessions and reverse-engineer them"
        " into automation scripts."
    )
    console.print()

    rule("USAGE", style="cyan")
    console.print("  automatiq <command> [options]")
    console.print()

    rule("COMMANDS", style="cyan")
    t = Table(show_header=False, box=None, collapse_padding=True)
    t.add_column(style="bold", min_width=16)
    t.add_column()
    t.add_row("record <url>", "Capture a browser session (screen + network + actions)")
    t.add_row("agent", "Analyse a recorded workspace and produce an automation script (defaults to latest)")
    t.add_row("resume [name]", "Resume a previous agent session from history")
    t.add_row("run <url>", "Record a session then immediately launch the agent")
    t.add_row("setup brave", "Download a portable Brave browser (anti-fingerprinting for stealth)")
    t.add_row('feedback "msg"', "Send anonymous feedback to the maintainers")
    console.print(t)
    console.print()

    rule("KEYBOARD SHORTCUTS", style="cyan")
    t2 = Table(show_header=False, box=None, collapse_padding=True)
    t2.add_column(style="bold", min_width=16)
    t2.add_column()
    t2.add_row(Text("RECORDING", style="bold bright_cyan"), "")
    t2.add_row("  Ctrl+C", "Stop recording and save session")
    t2.add_row("", "")
    t2.add_row(Text("COMPILATION", style="bold bright_cyan"), "")
    t2.add_row("  Esc", "Skip AI analysis for remaining segments")
    t2.add_row("  y / n", "Confirm or deny the skip prompt")
    t2.add_row("  Ctrl+C", "Force-quit")
    t2.add_row("", "")
    t2.add_row(Text("AGENT", style="bold bright_cyan"), "")
    t2.add_row("  q", "Quit the agent session")
    t2.add_row("  Esc", "Cancel current LLM call or code execution")
    t2.add_row("  Ctrl+C", "Force-quit")
    console.print(t2)
    console.print()

    rule("CONFIG", style="cyan")
    console.print("  [dim]~/.automatiq/config.toml[/dim]")
    t3 = Table(show_header=False, box=None, collapse_padding=True)
    t3.add_column(style="bold", min_width=16)
    t3.add_column()
    t3.add_row("  models", "LLM model strings and custom API endpoints")
    t3.add_row("  agent", "Max iterations and sandbox timeouts")
    t3.add_row("  recording", "Capture FPS, clip padding, and merge thresholds")
    t3.add_row("  browser", "Which browser to use for recording (chrome/brave/auto)")
    t3.add_row("  recorder_proxy", "Recording browser proxy (HTTP/SOCKS) and dynamic providers")
    t3.add_row("  banner", "Startup animation toggle and speed")
    t3.add_row("  output", "Root directory for all generated output")
    t3.add_row("  telemetry", "Anonymous usage-volume metrics (opt-out)")
    console.print(t3)
    console.print()

    rule("OPTIONS", style="cyan")
    t4 = Table(show_header=False, box=None, collapse_padding=True)
    t4.add_column(style="bold", min_width=24)
    t4.add_column()
    t4.add_row("--name NAME", "Custom name for the session folder (record/run only)")
    t4.add_row("--target PATH", "Path to a specific session folder to run the agent on")
    t4.add_row("--model MODEL", f"LiteLLM model string for the agent (default: {config.AGENT_MODEL})")
    t4.add_row("--recorder-model MODEL", f"Vision model for video-clip analysis (default: {config.RECORDER_AI_MODEL})")
    t4.add_row("--base-url URL", "Custom OpenAI-compatible API endpoint")
    t4.add_row("--max-steps N", f"Maximum agent loop iterations (default: {config.MAX_AGENT_STEPS})")
    t4.add_row("--sandbox-timeout SEC", f"Seconds per IPython cell (default: {config.SANDBOX_TIMEOUT_SECONDS})")
    t4.add_row("--output-dir PATH", "Root directory for all output (default: ./output)")
    t4.add_row("--proxy URL", "Route the recording browser through a proxy (record/run only)")
    t4.add_row("--no-proxy", "Force a direct connection, overriding config (record/run only)")
    t4.add_row("--browser TYPE", "Browser to use: chrome, brave, or auto (record/run only)")
    t4.add_row("--no-auto-download-browser", "Skip the Brave download prompt; use installed Chrome (record/run)")
    t4.add_row("--no-banner", "Skip the startup animation")
    t4.add_row("--no-telemetry", "Disable anonymous usage telemetry for this run")
    t4.add_row("--verbose", "Show detailed diagnostic output")
    t4.add_row("-V, --version", "Show version")
    t4.add_row("-h, --help", "Show this help message")
    console.print(t4)
    console.print()


def main():
    # Handle --help / -h before any heavy work.
    _is_help = any(a in sys.argv for a in ("--help", "-h"))
    _is_version = any(a in sys.argv for a in ("--version", "-V"))

    if _is_version:
        from .core import config

        print(f"automatiq {config.VERSION}")
        sys.exit(0)

    if _is_help:
        _print_rich_help()
        sys.exit(0)

    # No subcommand and no flag → show help.
    if len(sys.argv) < 2:
        _print_rich_help()
        sys.exit(0)

    # Start preloading in the background before the banner begins.
    preload_thread = threading.Thread(target=_preload, daemon=True)
    preload_thread.start()

    from .cli.automatiq_banner import show_startup
    from .core import config

    cmd = _peek_command()
    banner_model = _peek_model() or config.AGENT_MODEL
    banner_base_url = _peek_base_url()
    if banner_base_url:
        config.API_BASE = banner_base_url

    if config.BANNER_ENABLED and cmd in ("record", "agent", "run", "resume", "setup"):
        _banner_done.clear()  # gate: buffer any preload logs during animation

        # Determine session name for banner (agent and resume-with-name only)
        banner_session = None
        if cmd == "agent":
            banner_session = _peek_session_name()
        elif cmd == "resume":
            banner_session = _peek_resume_name()

        show_startup(
            version=config.VERSION,
            model=banner_model,
            recorder_model=config.RECORDER_AI_MODEL,
            speed=config.BANNER_SPEED,
            session=banner_session,
        )
        _banner_done.set()  # animation done: allow log output
        # Flush any logs that arrived during the banner
        _root_logger = logging.getLogger("automatiq")
        for h in _root_logger.handlers:
            if isinstance(h, _GatedRichHandler):
                h.flush_buffer()
                break

    if preload_thread.is_alive():
        from .cli.console import spinner

        with spinner("Initializing sandbox..."):
            preload_thread.join()
    else:
        preload_thread.join()

    if _preload_error is not None:
        import socket

        _e = _preload_error
        if isinstance(_e, OSError | socket.gaierror) or (
            isinstance(_e, RuntimeError) and "Could not download" in str(_e)
        ):
            error("No internet connection (or DNS failure) — could not download sandbox binaries.")
            error("Please check your connection and re-run automatiq.")
            if "URL:" in str(_e):  # show which binary failed
                for line in str(_e).splitlines():
                    if line.strip().startswith(("URL:", "Error:")):
                        error(line.strip())
        else:
            error(f"Startup init failed: {_e}")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        prog="automatiq",
        add_help=False,
    )
    subparsers = parser.add_subparsers(dest="command")

    def _add_common_flags(p, include_recorder_model=False):
        p.add_argument("--model", metavar="MODEL")
        p.add_argument("--base-url", metavar="URL")
        if include_recorder_model:
            p.add_argument("--recorder-model", metavar="MODEL")
        p.add_argument("--max-steps", type=int, metavar="N")
        p.add_argument("--sandbox-timeout", type=int, metavar="SECONDS")
        p.add_argument("--output-dir", metavar="PATH")
        p.add_argument("--no-banner", action="store_true", default=False)
        p.add_argument("--no-telemetry", action="store_true", default=False)
        p.add_argument("--verbose", action="store_true", default=False)
        p.add_argument("-h", "--help", action="store_true", default=False, dest="help_flag")
        p.add_argument("-V", "--version", action="store_true", default=False)

    def _add_proxy_flags(p):
        p.add_argument(
            "--proxy",
            metavar="URL",
            default=None,
            help="Route the browser through a proxy, e.g. http://host:3128 or socks5://host:1080",
        )
        p.add_argument("--no-proxy", action="store_true", default=False, help="Force a direct connection")

    p_record = subparsers.add_parser("record", add_help=False)
    p_record.add_argument("url", nargs="?", default="about:blank")
    p_record.add_argument("--name", type=str, default=None, help="Name of the session folder")
    p_record.add_argument(
        "--browser",
        type=str,
        default=None,
        choices=["chrome", "brave", "auto"],
        help="Browser to use for recording",
    )
    p_record.add_argument(
        "--no-auto-download-browser",
        action="store_true",
        default=False,
        dest="no_auto_download_browser",
        help="Do not prompt to download Brave; fall back to installed Chrome",
    )
    _add_common_flags(p_record, include_recorder_model=True)
    _add_proxy_flags(p_record)
    p_record.set_defaults(func=cmd_record)

    p_agent = subparsers.add_parser("agent", add_help=False)
    _add_common_flags(p_agent)
    p_agent.add_argument("--target", type=str, default=None, help="Path to the session folder to agentic run on")
    p_agent.set_defaults(func=cmd_agent)

    p_resume = subparsers.add_parser("resume", add_help=False)
    p_resume.add_argument("name", nargs="?", default=None, help="Session name to resume (skips picker if unique match)")
    _add_common_flags(p_resume)
    p_resume.set_defaults(func=cmd_resume)

    p_run = subparsers.add_parser("run", add_help=False)
    p_run.add_argument("url", nargs="?", default="about:blank")
    p_run.add_argument("--name", type=str, default=None, help="Name of the session folder")
    p_run.add_argument(
        "--browser",
        type=str,
        default=None,
        choices=["chrome", "brave", "auto"],
        help="Browser to use for recording",
    )
    p_run.add_argument(
        "--no-auto-download-browser",
        action="store_true",
        default=False,
        dest="no_auto_download_browser",
        help="Do not prompt to download Brave; fall back to installed Chrome",
    )
    _add_common_flags(p_run, include_recorder_model=True)
    _add_proxy_flags(p_run)
    p_run.set_defaults(func=cmd_run)

    p_setup = subparsers.add_parser("setup", add_help=False)
    p_setup_sub = p_setup.add_subparsers(dest="setup_target")
    p_setup_brave = p_setup_sub.add_parser("brave", add_help=False)
    p_setup_brave.add_argument(
        "--channel",
        type=str,
        default=None,
        choices=["release", "beta", "nightly"],
        help="Brave release channel (default: release, or value from config.toml)",
    )
    p_setup_brave.add_argument("--force", action="store_true", default=False, help="Re-download even if cached")
    _add_common_flags(p_setup_brave)
    p_setup_brave.set_defaults(func=cmd_setup)
    # When `setup` is run without a sub-target, show help.
    p_setup.set_defaults(func=lambda a: (_print_rich_help(), sys.exit(0)))

    p_feedback = subparsers.add_parser("feedback", add_help=False)
    p_feedback.add_argument(
        "message",
        type=str,
        nargs="?",
        default=None,
        help="Feedback message (optional, triggers interactive multiline prompt if omitted)",
    )
    _add_common_flags(p_feedback)
    p_feedback.set_defaults(func=cmd_feedback)

    args = parser.parse_args()

    if getattr(args, "help_flag", False) or getattr(args, "version", False):
        if getattr(args, "version", False):
            print(f"automatiq {config.VERSION}")
        else:
            _print_rich_help()
        sys.exit(0)

    if not args.command:
        _print_rich_help()
        sys.exit(0)

    # ── First-run telemetry notice ────────────────────────────────────────
    if config.SHOW_TELEMETRY_NOTICE and config.TELEMETRY_ENABLED:
        from .cli.console import console

        console.print()
        console.print(
            "[bold blue]Privacy & Telemetry Notice[/bold blue]\n"
            "[dim]AutomatiQ collects anonymous usage telemetry by default to help improve stability and performance.\n"
            "We adhere to a strict Zero-Identity privacy model:[/dim]\n"
            "[dim]  • [bold]Collected[/bold]: OS, python version, active command keyword (e.g., 'record', 'run'),\n"
            "    session duration, and generic exception class names (e.g., 'TimeoutError').[/dim]\n"
            "[dim]  • [bold]NEVER Collected[/bold]: URLs, custom code, local file paths, traceback logs, full terminal\n"
            "    arguments, cookies, credentials, or persistent host/IP identifiers.[/dim]\n"
            "[dim]To opt out, run with [bold]--no-telemetry[/bold] or disable it in\n"
            "    [bold]~/.automatiq/config.toml[/bold].[/dim]"
        )
        console.print()

        try:
            state = config.load_state()
            state["telemetry_notice_shown"] = True
            config.save_state(state)
        except Exception:
            pass

    # ── Telemetry lifecycle ───────────────────────────────────────────────
    from .core.telemetry import client

    client.start(command=args.command)
    try:
        args.func(args)
    finally:
        client.stop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
