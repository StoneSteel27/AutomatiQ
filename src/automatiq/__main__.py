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


def _preload():
    global _preload_error
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

        if cmd in ("agent", "run", ""):
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
    if getattr(args, "verbose", False):
        config.VERBOSE = True


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
    t.add_row("run <url>", "Record a session then immediately launch the agent")
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
    t3.add_row("  recorder_proxy", "Recording browser proxy (HTTP/SOCKS) and dynamic providers")
    t3.add_row("  banner", "Startup animation toggle and speed")
    t3.add_row("  output", "Root directory for all generated output")
    console.print(t3)
    console.print()

    rule("OPTIONS", style="cyan")
    t4 = Table(show_header=False, box=None, collapse_padding=True)
    t4.add_column(style="bold", min_width=24)
    t4.add_column()
    t4.add_row("--target PATH", "Path to a specific session folder to run the agent on")
    t4.add_row("--model MODEL", f"LiteLLM model string for the agent (default: {config.AGENT_MODEL})")
    t4.add_row("--recorder-model MODEL", f"Vision model for video-clip analysis (default: {config.RECORDER_AI_MODEL})")
    t4.add_row("--base-url URL", "Custom OpenAI-compatible API endpoint")
    t4.add_row("--max-steps N", f"Maximum agent loop iterations (default: {config.MAX_AGENT_STEPS})")
    t4.add_row("--sandbox-timeout SEC", f"Seconds per IPython cell (default: {config.SANDBOX_TIMEOUT_SECONDS})")
    t4.add_row("--output-dir PATH", "Root directory for all output (default: ./output)")
    t4.add_row("--proxy URL", "Route the recording browser through a proxy (record/run only)")
    t4.add_row("--no-proxy", "Force a direct connection, overriding config (record/run only)")
    t4.add_row("--no-banner", "Skip the startup animation")
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

    if config.BANNER_ENABLED and cmd in ("record", "agent", "run"):
        _banner_done.clear()  # gate: buffer any preload logs during animation
        show_startup(
            version=config.VERSION,
            model=banner_model,
            recorder_model=config.RECORDER_AI_MODEL,
            speed=config.BANNER_SPEED,
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
    _add_common_flags(p_record, include_recorder_model=True)
    _add_proxy_flags(p_record)
    p_record.set_defaults(func=cmd_record)

    p_agent = subparsers.add_parser("agent", add_help=False)
    _add_common_flags(p_agent)
    p_agent.add_argument("--target", type=str, default=None, help="Path to the session folder to agentic run on")
    p_agent.set_defaults(func=cmd_agent)

    p_run = subparsers.add_parser("run", add_help=False)
    p_run.add_argument("url", nargs="?", default="about:blank")
    p_run.add_argument("--name", type=str, default=None, help="Name of the session folder")
    _add_common_flags(p_run, include_recorder_model=True)
    _add_proxy_flags(p_run)
    p_run.set_defaults(func=cmd_run)

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

    args.func(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
