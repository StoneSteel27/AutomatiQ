"""BrowserAgent — lifecycle, CDP session boot, and cleanup.

Network/HTTP, WebSocket, and target/tab handler logic live in the
``cdp`` sub-package as mixins and are composed here via multiple inheritance.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from datetime import UTC, datetime

import zendriver as zd
from zendriver import cdp

from .. import config, events
from .action_server import ActionServer
from .blocklist_db import BlocklistDB
from .cdp.helpers import TimestampConverter
from .cdp.network import _NetworkHandlers
from .cdp.targets import _TargetManager
from .cdp.websockets import _WebsocketHandlers

logger = logging.getLogger(__name__)


BROWSER_UX_FLAGS = [
    # Hide browser-maintenance UI
    "--disable-brave-update",
    "--disable-features=OutdatedBuildDetector",
    "--no-default-browser-check",
    "--no-first-run",
    "--no-service-autorun",
    # Avoid launch/relaunch disruption
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    # Keep the automation window focused on the target site
    "--disable-background-networking",
]


class BrowserAgent(_TargetManager, _NetworkHandlers, _WebsocketHandlers):
    """Manages the headless/UI browser session, CDP event handlers, and data collection."""

    def __init__(self, blocklist: BlocklistDB | None = None, proxy: str | None = None):
        self.blocklist = blocklist
        # Proxy URL (e.g. "http://host:3128", "socks5://host:1080") passed to the
        # browser via --proxy-server. None means a direct connection.
        self.proxy = proxy
        self._profile_dir = tempfile.TemporaryDirectory(prefix="automatiq_chrome_")

        # New Disk-Streaming Setup
        self._data_dir = tempfile.TemporaryDirectory(prefix="automatiq_stream_")
        self._bodies_dir = os.path.join(self._data_dir.name, "bodies")
        os.makedirs(self._bodies_dir, exist_ok=True)
        self._actions_file = open(os.path.join(self._data_dir.name, "actions.jsonl"), "a", encoding="utf-8")
        self._requests_file = open(os.path.join(self._data_dir.name, "requests.jsonl"), "a", encoding="utf-8")
        self._ws_connections_file = open(
            os.path.join(self._data_dir.name, "ws_connections.jsonl"), "a", encoding="utf-8"
        )
        self._ws_frames_file = open(os.path.join(self._data_dir.name, "ws_frames.jsonl"), "a", encoding="utf-8")
        self._actions_count = 0
        # Guards actions.jsonl writes from the ActionServer thread.
        self._actions_lock = threading.Lock()

        # Shipped Chrome extension (telemetry + visuals content scripts).
        # Copied to a writable temp dir at run_session start so config.js (the
        # ActionServer port) can be baked in before --load-extension is passed.
        self._extension_src_dir = os.path.join(os.path.dirname(__file__), "extension")
        self._extension_dir: str | None = None

        # ActionServer is started lazily in run_session (never in __init__) so
        # that constructing a BrowserAgent for tests has no thread/socket side effects.
        self._action_server: ActionServer | None = None

        self.browser = None
        self.tab = None
        self.recording_start = None

        # Crash tracking state
        self.session_crashed = False
        self.crash_timestamp = None
        self.crash_error = None

        self.ts_converter = TimestampConverter()

        self.active_map = {}
        self.orphan_extra_info = {}

        # WebSocket tracking state
        self.active_websockets = {}  # str(request_id) -> {"start_time", "sequence", "url"}

        # FIX: Central Tab Registry
        self.tabs = {}  # session_id -> {"tab": tab_session, "type": "page"|"iframe"}

        self.stats = {
            "total_requests": 0,
            "completed": 0,
            "failed": 0,
            "incomplete": 0,
            "body_success": 0,
            "body_failed": 0,
            "body_skip_no_content": 0,
            "body_skip_redirect": 0,
            "body_skip_cached": 0,
            "body_from_stream": 0,
            "blocked_by_blocklist": 0,
            "ws_connections": 0,
            "ws_frames_sent": 0,
            "ws_frames_received": 0,
            "ws_frames_skipped": 0,
            "ws_blocked_by_blocklist": 0,
        }

    def __del__(self) -> None:
        for attr in ("_actions_file", "_requests_file", "_ws_connections_file", "_ws_frames_file"):
            if hasattr(self, attr):
                f = getattr(self, attr)
                if f and not f.closed:
                    try:
                        f.close()
                    except Exception:
                        pass

    def _prepare_extension(self, port: int) -> str | None:
        """Copy the shipped extension to a writable temp dir and bake in the port.

        Returns the path to pass to --load-extension, or None on failure.
        """
        if not os.path.isdir(self._extension_src_dir):
            events.log_error.send("recorder", text=f"Extension source missing: {self._extension_src_dir}")
            return None
        ext_dir = os.path.join(self._data_dir.name, "extension")
        try:
            shutil.copytree(self._extension_src_dir, ext_dir, dirs_exist_ok=True)
        except Exception as exc:
            events.log_error.send("recorder", text=f"Failed to stage extension: {exc}")
            events.log_traceback.send("recorder")
            return None
        config_path = os.path.join(ext_dir, "config.js")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(f'var AUTOMATIQ_ENDPOINT = "http://127.0.0.1:{port}/act";\n')
        return ext_dir

    async def run_session(
        self,
        url: str,
        stop_token=None,
        browser_resolution: tuple[str, object | None, str] | None = None,
    ) -> dict:
        # Start the loopback ActionServer that receives telemetry from the
        # extension's content scripts (replaces the fingerprintable CDP binding).
        self._action_server = ActionServer(on_action=self._process_action)
        port = self._action_server.start()
        events.log_debug.send("recorder", text=f"ActionServer listening on 127.0.0.1:{port}")

        ext_dir = self._prepare_extension(port)
        if not ext_dir:
            return {}
        self._extension_dir = ext_dir

        try:
            events.log_info.send("recorder", text="Starting Zendriver Browser...")
            # Build the zendriver Config. When the CLI passed a resolved
            # browser (downloaded Brave or Chrome fallback), honour it; else
            # fall back to the legacy behaviour of letting BROWSER_TYPE drive
            # zendriver's own autodetect.
            zd_kwargs: dict = dict(user_data_dir=self._profile_dir.name, headless=False)
            if browser_resolution is not None:
                verb, value, descriptor = browser_resolution
                if verb == "browser_executable_path" and value is not None:
                    zd_kwargs["browser_executable_path"] = str(value)
                    events.log_info.send("recorder", text=f"Using {descriptor}: {value}")
                else:
                    zd_kwargs["browser"] = str(value) if value is not None else config.BROWSER_TYPE
                    events.log_info.send("recorder", text=f"Using {descriptor}")
            else:
                zd_kwargs["browser"] = config.BROWSER_TYPE
            if sys.platform == "darwin":
                zd_kwargs["sandbox"] = False
                zd_kwargs["browser_connection_timeout"] = 1.0
                zd_kwargs["browser_connection_max_tries"] = 20
            zd_config = zd.Config(**zd_kwargs)
            if sys.platform == "darwin":
                zd_config.add_argument("--use-mock-keychain")
            zd_config.add_extension(ext_dir)
            zd_config.add_argument("--disable-popup-blocking")
            zd_config.add_argument("--disable-features=ProcessPerSiteUpToMainFrameThreshold")
            for flag in BROWSER_UX_FLAGS:
                zd_config.add_argument(flag)
            if self.proxy:
                zd_config.add_argument(f"--proxy-server={self.proxy}")
                events.log_info.send("recorder", text=f"Routing browser through proxy: {self.proxy}")
            self.browser = await zd.Browser.create(config=zd_config)
            self.recording_start = datetime.now(UTC)

            # 1. Get the primary tab
            self.tab = await self.browser.get("about:blank")

            # Register it in our central tabs registry
            main_session_id = getattr(self.tab, "session_id", "main")
            self.tabs[main_session_id] = {"tab": self.tab, "type": "page", "url": "about:blank"}

            events.log_debug.send(
                "recorder",
                text="Enabling Network domain and binding handlers.",
            )

            # Enable the Debugger domain and neutralize anti-debugger tripwires
            # (eval("debugger") timing checks, e.g. bd.russismvarsha.com).
            # setSkipAllPauses(True) makes V8 skip pauses entirely — the
            # debugger; statement becomes a true no-op in ~0ms, so timing
            # tripwires (>120ms = CDP detected) fail. The Debugger.Paused
            # auto-resume handler is kept as defence-in-depth in case any
            # pause slips through despite the skip flag.
            #
            # NOTE: "other" is included in the resume reasons because eval("debugger")
            # in some contexts (e.g. injected <script> via document.write) pauses with
            # reason="other" rather than "debuggerStatement". Without resuming "other",
            # the page freezes and no interaction (clicks, typing) is possible.
            await self.tab.send(cdp.debugger.enable())
            await self.tab.send(cdp.debugger.set_skip_all_pauses(skip=True))

            async def _on_debugger_paused(event: cdp.debugger.Paused):
                if event.reason in ("debuggerStatement", "ambiguous", "assert", "other"):
                    await self.tab.send(cdp.debugger.resume())

            self.tab.add_handler(cdp.debugger.Paused, _on_debugger_paused)

            # Network domain — telemetry/visuals are injected by the
            # extension's content scripts on every page/popup/iframe natively.
            await self.tab.send(
                cdp.network.enable(max_resource_buffer_size=100 * 1024 * 1024, max_total_buffer_size=1000 * 1024 * 1024)
            )

            # 2. Bind the main tab network handlers
            self._attach_handlers_to_tab(self.tab, main_session_id, is_iframe=False)

            # 3. Enable auto-attach for ANY FUTURE tabs/windows so we can capture
            # their network traffic (script injection is handled by the extension).
            #
            # Detect new tabs/windows via TargetCreated (sent by set_discover_targets)
            # instead of auto-attach. zendriver already registers its own
            # TargetCreated handler (adds Tab to self.browser.targets) and calls
            # set_discover_targets in start(), so we just add our own handler to
            # receive the same event.
            #
            # Why not use set_auto_attach (which we previously tried):
            # Chrome requires flatten=True for browser-level auto-attach. flatten
            # creates a flattened sub-session that intercepts Debugger.Paused
            # events on the browser-level websocket — our per-tab Debugger.Paused
            # handler (on the tab's own websocket) never sees them, so
            # anti-debugger traps hang the tab.
            # set_discover_targets + TargetCreated has no sessions, no pause, no
            # flatten — the tab's websocket is the ONLY CDP session, so
            # Debugger.Paused events go there and our auto-resume catches them.
            self.browser.connection.add_handler(cdp.target.TargetCreated, self.target_created_handler)

            events.log_info.send("recorder", text=f"Navigating to {url}")
            await self.tab.send(cdp.page.navigate(url=url))

            while not (stop_token and stop_token.is_stopped()):
                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            events.log_debug.send("recorder", text="Session asyncio loop cancelled.")
            if stop_token:
                stop_token.stop()
        except KeyboardInterrupt:
            events.log_warn.send("recorder", text="Session encountered KeyboardInterrupt.")
            if stop_token:
                stop_token.stop()
        except Exception as e:
            self.session_crashed = True
            self.crash_timestamp = datetime.now(UTC).isoformat()
            self.crash_error = str(e)
            events.log_error.send("recorder", text=f"Session error: {e}")
            events.log_traceback.send("recorder")

        return await self._cleanup_and_build_report()

    async def _wait_for_pending_requests(self, timeout: float = 10.0, idle_time: float = 1.0) -> None:
        if not self.active_map:
            return
        pending = len(self.active_map)
        events.log_info.send("recorder", text=f"Waiting for {pending} pending request(s)...")

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        last_change = loop.time()
        prev_count = pending

        while self.active_map and loop.time() < deadline:
            current_count = len(self.active_map)
            if current_count != prev_count:
                last_change = loop.time()
                prev_count = current_count
            if loop.time() - last_change >= idle_time:
                break
            await asyncio.sleep(0.1)

    async def _cleanup_and_build_report(self) -> dict:
        try:
            await asyncio.wait_for(self._wait_for_pending_requests(), timeout=10.0)
        except TimeoutError:
            pass

        incomplete_count = len(self.active_map)
        if incomplete_count > 0:
            for req in self.active_map.values():
                req["request_state"] = "incomplete"
                self.stats["incomplete"] += 1
                self._requests_file.write(json.dumps(req) + "\n")
            self._requests_file.flush()
            self.active_map.clear()

        # Write synthetic "closed" events for WebSocket connections that didn't close cleanly
        if self.active_websockets:
            closed_iso = self.ts_converter.current_iso8601()
            for ws_request_id in self.active_websockets:
                closed_record = {
                    "event": "closed",
                    "request_id": str(ws_request_id),
                    "closed_iso": closed_iso,
                }
                self._ws_connections_file.write(json.dumps(closed_record) + "\n")
            self._ws_connections_file.flush()
            self.active_websockets.clear()

        # Stop the ActionServer BEFORE closing actions.jsonl so no handler races the close.
        if self._action_server is not None:
            self._action_server.stop()
            self._action_server = None

        # Close stream files
        try:
            self._actions_file.close()
            self._requests_file.close()
            self._ws_connections_file.close()
            self._ws_frames_file.close()
        except Exception as e:
            events.log_error.send("recorder", text=f"Failed to close stream files: {e}")
            events.log_traceback.send("recorder")

        if self.tab:
            self.tab.remove_handlers()

        for session_data in self.tabs.values():
            try:
                session_data["tab"].remove_handlers()
            except Exception as e:
                events.log_error.send("recorder", text=f"Cleanup error: {e}")
                events.log_traceback.send("recorder")

        try:
            if self.browser:
                await asyncio.wait_for(self.browser.stop(), timeout=5.0)
        except Exception as e:
            events.log_error.send("recorder", text=f"Browser stop cleanup failed: {e}")
            events.log_traceback.send("recorder")

        try:
            self._profile_dir.cleanup()
        except Exception as e:
            events.log_error.send("recorder", text=f"Profile dir cleanup failed: {e}")
            events.log_traceback.send("recorder")

        duration = (datetime.now(UTC) - self.recording_start).total_seconds() if self.recording_start else 0.0

        metadata = {
            "recording_started": self.recording_start.isoformat(timespec="milliseconds")
            if self.recording_start
            else None,
            "recording_ended": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "duration_seconds": round(duration, 2),
            "total_requests": self.stats["total_requests"],
            "completed_requests": self.stats["completed"],
            "failed_requests": self.stats["failed"],
            "incomplete_requests": self.stats["incomplete"],
            "total_actions": self._actions_count,
            "blocked_by_blocklist": self.stats["blocked_by_blocklist"],
            "timestamp_format": "ISO 8601 (YYYY-MM-DDTHH:MM:SS.sssZ)",
            "timezone": "UTC",
            "body_capture_stats": {
                "success": self.stats["body_success"],
                "from_stream": self.stats["body_from_stream"],
                "failed": self.stats["body_failed"],
                "skip_redirect": self.stats["body_skip_redirect"],
                "skip_no_content": self.stats["body_skip_no_content"],
                "skip_cached": self.stats["body_skip_cached"],
            },
            "websocket_stats": {
                "connections": self.stats["ws_connections"],
                "frames_sent": self.stats["ws_frames_sent"],
                "frames_received": self.stats["ws_frames_received"],
                "frames_skipped": self.stats["ws_frames_skipped"],
                "blocked_by_blocklist": self.stats["ws_blocked_by_blocklist"],
            },
            "session_crashed": self.session_crashed,
            "crash_timestamp": self.crash_timestamp,
            "crash_error": self.crash_error,
        }

        # Write metadata to the temp directory
        with open(os.path.join(self._data_dir.name, "metadata.json"), "w") as f:
            json.dump(metadata, f, indent=2)

        return self._data_dir.name
