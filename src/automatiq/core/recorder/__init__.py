"""Recorder sub-package — captures a full browser session (network + video + actions)
and compiles it into a structured workspace dump for the agent.
Usage: from automatiq.core.recorder import run_recording; run_recording("https://example.com")
"""

import asyncio
import importlib
import logging
import os
import tempfile
import urllib.request

from .. import config, events
from ..cancel_standard import CancelToken, StopToken
from .blocklist_db import BlocklistDB
from .browser_agent import BrowserAgent
from .compile.workspace import compile_workspace
from .video_recorder import ActionVideoRecorder

logger = logging.getLogger(__name__)


def _init_blocklist() -> BlocklistDB:
    """Create (or open) the persistent blocklist DB and download any missing source files."""
    db = BlocklistDB(db_path=str(config.BLOCKLIST_DB))

    for name, url in config.BLOCKLIST_SOURCES.items():
        hosts_file = config.BLOCKLIST_DIR / f"{name}.txt"

        if not hosts_file.exists():
            events.log_info.send("recorder", text=f"Downloading blocklist '{name}' ...")
            try:
                urllib.request.urlretrieve(url, str(hosts_file))
                events.log_info.send("recorder", text=f"Saved {hosts_file.name}")
            except Exception as exc:
                events.log_warn.send("recorder", text=f"Failed to download blocklist '{name}': {exc}")
                continue

        db.load_file(str(hosts_file), source_name=name, source_url=url)

    return db


def _resolve_proxy(proxy: str | None = None, no_proxy: bool = False) -> str | None:
    """Resolve the browser proxy URL.

    Precedence: explicit --no-proxy > explicit --proxy > dynamic provider > static server.
    Returns None when proxying is disabled or resolution fails (recording then
    proceeds on a direct connection).
    """
    if no_proxy:
        return None

    if proxy:
        return proxy

    if not config.RECORDER_PROXY_ENABLED:
        return None

    if config.RECORDER_PROXY_PROVIDER:
        module_path, _, attr = config.RECORDER_PROXY_PROVIDER.partition(":")
        if not module_path or not attr:
            events.log_warn.send(
                "recorder",
                text=f"Invalid proxy provider '{config.RECORDER_PROXY_PROVIDER}' (expected 'module:callable')",
            )
            return config.RECORDER_PROXY_SERVER
        try:
            module = importlib.import_module(module_path)
            proxy_url = getattr(module, attr)()
            if proxy_url:
                return proxy_url
            events.log_warn.send("recorder", text=f"Proxy provider {config.RECORDER_PROXY_PROVIDER} returned no URL")
        except Exception as exc:
            events.log_error.send("recorder", text=f"Proxy provider {config.RECORDER_PROXY_PROVIDER} failed: {exc}")
            events.log_traceback.send("recorder")
        return config.RECORDER_PROXY_SERVER

    return config.RECORDER_PROXY_SERVER


def run_recording(
    url: str = "about:blank",
    session_name: str | None = None,
    cancel_token: CancelToken = None,
    stop_token: StopToken = None,
    skip_callback=None,
    proxy: str | None = None,
    no_proxy: bool = False,
) -> bool:
    """Run the full recording pipeline: browser -> video -> compile workspace.

    1. Launches Chrome with CDP instrumentation and screen capture.
    2. User browses freely; Ctrl+C stops the session.
    3. Compiles the captured data into output/workspace/session_dump/.
    """

    if not config.WORKSPACE_DIR.exists():
        config.ensure_output_dirs()

    temp_video_path = os.path.join(tempfile.gettempdir(), "automatiq_full_record.mp4")

    blocklist = _init_blocklist()
    proxy = _resolve_proxy(proxy=proxy, no_proxy=no_proxy)

    _video_recorder = ActionVideoRecorder(fps=config.FPS, output_path=temp_video_path)
    _browser_agent = BrowserAgent(blocklist=blocklist, proxy=proxy)

    events.log_info.send("recorder", text="[RULE] STARTING RECORDER")
    events.log_info.send("recorder", text=f"Target URL : {url}")
    events.log_info.send("recorder", text=f"AI Model   : {config.RECORDER_AI_MODEL}")
    events.log_info.send("recorder", text=f"Blocklist  : {blocklist.total_enabled_domains()} domains loaded")
    events.log_info.send("recorder", text=f"Proxy      : {proxy if proxy else 'direct (no proxy)'}")

    temp_data_dir = None
    success = False

    try:
        _video_recorder.start()

        # We start the browser agent in the background loop but we need to hold
        # the main thread to show the spinner. asyncio.run() blocks.
        from ...cli.console import console

        with console.status(
            "[green]Recording Active 🔴[/green] Press [blue]Ctrl+C[/blue] to stop and save recording\n",
            spinner="earth",
        ):
            temp_data_dir = asyncio.run(_browser_agent.run_session(url=url, stop_token=stop_token))

    except KeyboardInterrupt:
        events.log_warn.send("recorder", text="KeyboardInterrupt caught in run_recording.")
        if stop_token:
            stop_token.stop()
    except Exception as exc:
        events.log_error.send("recorder", text=f"Recording session failed: {exc}")
        events.log_traceback.send("recorder")
    finally:
        video_start_unix = None
        try:
            video_start_unix = _video_recorder.stop()
        except Exception as exc:
            events.log_error.send("recorder", text=f"Failed to stop video recorder: {exc}")
            events.log_traceback.send("recorder")

        if stop_token:
            stop_token.reset()

        if cancel_token is None:
            cancel_token = CancelToken()

        def ask_user_to_skip(remaining: int) -> bool:
            if skip_callback:
                return skip_callback(remaining, cancel_token)
            cancel_token.reset()
            return True

        blocked = _browser_agent.stats["blocked_by_blocklist"] if _browser_agent else 0
        if blocked:
            events.log_info.send("recorder", text=f"Blocklist filtered {blocked} ad/tracker request(s)")

        try:
            blocklist.close()
        except Exception as exc:
            events.log_warn.send("recorder", text=f"Failed to close blocklist DB: {exc}")
            events.log_traceback.send("recorder")

        if temp_data_dir and video_start_unix:
            try:
                final_video_path, success = compile_workspace(
                    session_name=session_name,
                    temp_data_dir=temp_data_dir,
                    full_video_path=temp_video_path,
                    video_start_unix=video_start_unix,
                    on_skip_requested=ask_user_to_skip,
                    cancel_token=cancel_token,
                    stop_token=stop_token,
                )
            except Exception as exc:
                events.log_error.send("recorder", text=f"Workspace compilation raised unexpectedly: {exc}")
                events.log_traceback.send("recorder")
                success = False

            if success and final_video_path:
                events.log_info.send("recorder", text=f"Full recording saved to {final_video_path}")
        else:
            events.log_warn.send("recorder", text="Session data or video timestamp missing. Skipping compilation.")

    return success
