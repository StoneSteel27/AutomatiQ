"""BrowserAgent — lifecycle, CDP session boot, and cleanup.

Network/HTTP, WebSocket, and target/tab handler logic live in the
``cdp`` sub-package as mixins and are composed here via multiple inheritance.
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import UTC, datetime

import zendriver as zd
from zendriver import cdp

from .. import events
from .blocklist_db import BlocklistDB
from .cdp.helpers import TimestampConverter
from .cdp.network import _NetworkHandlers
from .cdp.targets import _TargetManager
from .cdp.websockets import _WebsocketHandlers

logger = logging.getLogger(__name__)


class BrowserAgent(_TargetManager, _NetworkHandlers, _WebsocketHandlers):
    """Manages the headless/UI browser session, CDP event handlers, and data collection."""

    def __init__(
        self,
        telemetry_js_path=None,
        visuals_js_path=None,
        blocklist: BlocklistDB | None = None,
        proxy: str | None = None,
    ):
        _js_dir = os.path.join(os.path.dirname(__file__), "js")
        self.telemetry_js_path = telemetry_js_path or os.path.join(_js_dir, "telemetry.js")
        self.visuals_js_path = visuals_js_path or os.path.join(_js_dir, "visuals.js")
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

        self.telemetry_script = ""
        self.visuals_script = ""

    def _load_scripts(self) -> bool:
        try:
            with open(self.telemetry_js_path, encoding="utf-8") as f:
                self.telemetry_script = f.read()
            with open(self.visuals_js_path, encoding="utf-8") as f:
                self.visuals_script = f.read()
            return True
        except FileNotFoundError as e:
            events.log_error.send("recorder", text=f"Missing JS dependencies: {e}")
            return False

    async def run_session(self, url: str, stop_token=None) -> dict:
        if not self._load_scripts():
            return {}

        try:
            events.log_info.send("recorder", text="Starting Zendriver Browser...")
            browser_args = [
                "--incognito",
                "--disable-popup-blocking",
                f"--user-data-dir={self._profile_dir.name}",
            ]
            if self.proxy:
                browser_args.append(f"--proxy-server={self.proxy}")
                events.log_info.send("recorder", text=f"Routing browser through proxy: {self.proxy}")
            self.browser = await zd.start(
                headless=False,
                browser_args=browser_args,
            )
            self.recording_start = datetime.now(UTC)

            # 1. Get the primary tab
            self.tab = await self.browser.get("about:blank")

            # Register it in our central tabs registry
            main_session_id = getattr(self.tab, "session_id", "main")
            self.tabs[main_session_id] = {"tab": self.tab, "type": "page", "url": "about:blank"}

            events.log_info.send("recorder", text="Enabling CDP domains and binding handlers...")

            # Prioritize network domain
            await self.tab.send(
                cdp.network.enable(max_resource_buffer_size=100 * 1024 * 1024, max_total_buffer_size=1000 * 1024 * 1024)
            )
            await self.tab.send(cdp.page.enable())
            await self.tab.send(cdp.page.set_bypass_csp(enabled=True))
            await self.tab.send(cdp.runtime.enable())
            await self.tab.send(cdp.runtime.add_binding(name="sendActionToPython"))

            # 2. Bind the main tab handlers EXPLICITLY using our new registry functions
            self._attach_handlers_to_tab(self.tab, main_session_id, is_iframe=False)

            # Inject scripts into the main tab
            await self.tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=self.telemetry_script, run_immediately=True)
            )
            await self.tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=self.visuals_script, run_immediately=True)
            )

            await self.tab.send(cdp.runtime.evaluate(expression=self.telemetry_script))
            await self.tab.send(cdp.runtime.evaluate(expression=self.visuals_script))

            # 3. Enable auto-attach for ANY FUTURE tabs/windows/iframes
            await self.browser.connection.send(
                cdp.target.set_auto_attach(auto_attach=True, wait_for_debugger_on_start=False, flatten=True)
            )
            self.browser.connection.add_handler(cdp.target.AttachedToTarget, self.target_created_handler)
            await self.browser.connection.send(cdp.target.set_discover_targets(discover=True))

            events.log_info.send("recorder", text=f"Navigating to {url}")
            await self.tab.send(cdp.page.navigate(url=url))

            while not (stop_token and stop_token.is_stopped()):
                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            events.log_info.send("recorder", text="Session asyncio loop cancelled.")
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
