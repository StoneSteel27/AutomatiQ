"""Session runtime for the automatiq recorder MCP server.

Owns everything the thin MCP tools in ``server.py`` talk to:

- ``RecordingSession`` - the state machine and worker thread that drive the
  capture pipeline (``BrowserAgent.run_session``) and the compile pipeline
  (``compile_workspace``) on a private asyncio loop.
- ``SessionRegistry`` - id-keyed registry guarded by a lock.
- ``ParentWatchdog`` - ends all sessions when the MCP host disappears
  (effective on POSIX; see class notes for the Windows caveat).

Stop-token fan-in (whichever fires first wins):

1. the ``stop_recording`` tool,
2. stdin-EOF shutdown (FastMCP lifespan calls ``registry.stop_all``),
3. the user closing the browser's last window / browser process death
   (detected inside ``BrowserAgent.run_session``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from automatiq.core import config, events
from automatiq.core.cancel_standard import CancelToken, StopToken
from automatiq.core.recorder import (
    _check_macos_screen_permission,
    _init_blocklist,
    _resolve_proxy,
)
from automatiq.mcp.status_log import _STATUS_LOG
from automatiq.mcp.vision import _SKIP_NO_KEY, _SKIP_VIDEO_DISABLED, _vision_summary_block, vision_preflight

logger = logging.getLogger(__name__)

# -- Telemetry start guard ----------------------------------------------------

_telemetry_start_lock = threading.Lock()
_telemetry_started_once = False


def _ensure_telemetry_started() -> None:
    """Start the process-wide telemetry client exactly once.

    The ``client`` singleton is used by every RecordingSession, but nothing
    in product code called ``TelemetryClient.start()``, so ``_enabled``
    stayed False and every track() call silently no-oped. This wires the
    start into the first recording; ``start()`` itself respects
    ``config.TELEMETRY_ENABLED`` and never spawns a second worker, and the
    lock + flag here keep repeated recordings from re-invoking it.
    Fail-open: any error is swallowed - telemetry must never break a
    recording.
    """
    global _telemetry_started_once
    if _telemetry_started_once:
        return
    with _telemetry_start_lock:
        if _telemetry_started_once:
            return
        try:
            from automatiq.core.telemetry import client

            client.start(command="record")
        except Exception:
            pass
        finally:
            _telemetry_started_once = True


# -- Session states -----------------------------------------------------------
STATE_CREATED = "created"
STATE_INITIALIZING = "initializing"
STATE_RECORDING = "recording"
STATE_COMPILING = "compiling"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_STOPPED = "stopped"

_TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED, STATE_STOPPED})


def new_session_id() -> str:
    """Timestamp-prefixed unique id, e.g. ``20260827_141530_a1b2c3``."""
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


class RecordingSession:
    """One capture+compile run, executed on its own worker thread."""

    def __init__(
        self,
        url: str,
        session_name: str | None = None,
        proxy: str | None = None,
        include_video: bool = True,
        output_root: str | None = None,
        vision_preflight_result: dict | None = None,
    ) -> None:
        self.id = new_session_id()
        self.url = url
        self.session_name = session_name or f"recording_{self.id}"
        self.proxy = proxy
        self.include_video = include_video
        self.output_root = Path(output_root) if output_root else config.OUTPUT_DIR

        # Stop-token fan-in point: browser-close detection and stop_all() and
        # the stop tool all land here.
        self.stop_token = StopToken()
        self.cancel_token = CancelToken()

        self.created_at = time.time()
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.state = STATE_CREATED
        self.error: str | None = None
        self._phase: str = "starting"  # pre-launch phase, surfaced via status()

        # Filled in by the worker as phases complete.
        self.final_video_path: str | None = None
        self.capture_stats: dict = {}
        self._browser_desc: str | None = None  # descriptor from browser resolution
        self._vision_summary: dict | None = None  # terminal vision block (status)
        self._vision_pf: dict | None = vision_preflight_result  # resolved-once vision preflight

        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._agent = None  # BrowserAgent (live stats) while recording

    # -- Lifecycle API (called from the asyncio event-loop side) -------------

    def start(self) -> None:
        """Spawn the worker thread and move the session to ``initializing``.

        Raises RuntimeError if the session was already started.
        """
        if self._thread is not None:
            raise RuntimeError(f"session {self.id} already started")
        self._set_state(STATE_INITIALIZING)
        self._thread = threading.Thread(target=self._run, name=f"automatiq-session-{self.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Request a graceful end of capture. Compilation still completes."""
        self.stop_token.stop()

    def wait(self, timeout_s: float = 20.0) -> bool:
        """Block up to timeout_s; True iff the session reached a terminal state."""
        return self._done.wait(timeout_s)

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    @property
    def stop_requested(self) -> bool:
        return self.stop_token.is_stopped()

    # -- Status snapshot -------------------------------------------------------

    def _vision_preflight_snapshot(self) -> dict:
        """Resolve vision preflight once per session and reuse it.

        vision_preflight() has side effects (config-file key plumbing via
        os.environ.setdefault); a second resolution inside the same session
        would see the first call's plumbing and could resolve to a different
        source/model. Freshness across sessions is preserved - each new
        RecordingSession resolves anew.
        """
        if self._vision_pf is None:
            self._vision_pf = vision_preflight()
        return self._vision_pf

    def status(self) -> dict:
        """Compact (<50KB) JSON-safe snapshot for get_status/wait_for_completion."""
        with self._lock:
            snap: dict = {
                "session_id": self.id,
                "session_name": self.session_name,
                "url": self.url,
                "state": self.state,
                "phase": self._phase,
                "error": self.error,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "include_video": self.include_video,
                "proxy_enabled": bool(self.proxy) or config.RECORDER_PROXY_ENABLED,
                "output_root": str(self.output_root),
                "final_video_path": self.final_video_path,
                "stop_requested": self.stop_requested,
                "vision_configured": self._vision_preflight_snapshot()["configured"],
            }
            if self._browser_desc is not None:
                snap["browser"] = self._browser_desc
            recent = list(_STATUS_LOG)[-15:]  # last 15, newest last
            if recent:
                snap["recent_log"] = [{"t": ts, "level": level, "text": text} for ts, level, text in recent]
            if self._vision_summary is not None:
                snap["vision"] = self._vision_summary
            if self.started_at:
                ref = self.ended_at or time.time()
                snap["duration_seconds"] = round(ref - self.started_at, 1)

        agent = self._agent
        if agent is not None:
            snap["capture"] = dict(getattr(agent, "stats", {}))
            snap["capture"]["actions_captured"] = getattr(agent, "_actions_count", 0)
            snap["browser_closed_by_user"] = bool(getattr(agent, "browser_closed_by_user", False))
            snap["session_crashed"] = bool(getattr(agent, "session_crashed", False))
        elif self.capture_stats:
            snap["capture"] = dict(self.capture_stats)

        out_dir = self._resolved_output_dir()
        if out_dir is not None:
            snap["output_dir"] = str(out_dir)
            snap["readme_path"] = str(out_dir / "README.md")
        return snap

    def _resolved_output_dir(self) -> Path | None:
        """The actual session dir created by compile (may have a numeric suffix)."""
        with self._lock:
            base = self.session_name
            root = self.output_root
        direct = root / base
        if direct.exists():
            return direct
        idx = 1
        while True:
            candidate = root / f"{base}_{idx:02d}"
            if not candidate.exists():
                return None if idx == 1 else direct
            direct = candidate
            idx += 1

    # -- Worker ---------------------------------------------------------------

    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._lock:
            self.state = state
            if error is not None:
                self.error = error

    def _set_phase(self, phase: str) -> None:
        """Record the current pre-launch phase (surfaced as status()['phase'])."""
        with self._lock:
            self._phase = phase

    def _set_error(self, message: str) -> None:
        """Record *message* as the session error (lock-guarded)."""
        with self._lock:
            self.error = message

    def _run(self) -> None:
        """Full pipeline: blocklist -> proxy -> browser resolution -> video+agent capture -> compile.

        Ported from the CLI product's run_recording(): the Rich spinner, the
        fixed shared temp-video path, and CWD-relative output are gone; the
        private asyncio loop satisfies zendriver's single-loop requirement.
        """
        self.started_at = time.time()
        record_start = time.monotonic()
        temp_video_path = os.path.join(tempfile.gettempdir(), f"automatiq_{self.id}.mp4")

        final_video_path: str | None = None
        output_dir: str | None = None
        video_start_unix: float | None = None
        temp_data_dir: str | None = None
        error_source: str | None = None
        proxy_used: str | None = None
        browser_resolution: tuple[str, Path | None, str] | None = None
        agent = None
        blocklist = None
        video_recorder = None
        vision_state = self._seed_vision_state()

        # Phase markers: each [INIT] line reaches status().recent_log, so a
        # client polling get_status sees pre-launch progress (module imports,
        # blocklist download, browser resolution) instead of an empty log.
        self._set_phase("loading")
        events.log_info.send("recorder", text=f"[INIT] session={self.id} loading recorder modules")

        try:
            from automatiq.core.recorder.browser_agent import BrowserAgent
            from automatiq.core.recorder.video_recorder import ActionVideoRecorder

            self.output_root.mkdir(parents=True, exist_ok=True)
            self._set_phase("blocklist")
            events.log_info.send("recorder", text="[INIT] initializing blocklist")
            step_started = time.monotonic()
            blocklist = _init_blocklist()
            events.log_info.send(
                "recorder",
                text=(
                    f"[INIT] blocklist ready ({blocklist.total_enabled_domains()} domains, "
                    f"{time.monotonic() - step_started:.1f}s)"
                ),
            )
            proxy_used = _resolve_proxy(proxy=self.proxy)

            # Managed-browser resolution (may download a portable Brave build,
            # ~300 MB) runs BEFORE anything heavy: a failed download must not
            # leave a half-started video capture or agent behind.
            self._set_phase("browser_setup")
            events.log_info.send("recorder", text="[INIT] resolving browser")
            step_started = time.monotonic()
            browser_resolution = self._resolve_browser()
            if browser_resolution is None:
                error_source = "browser_setup"
                return
            events.log_info.send(
                "recorder",
                text=f"[INIT] browser ready: {self._browser_desc} ({time.monotonic() - step_started:.1f}s)",
            )

            video_recorder = ActionVideoRecorder(fps=config.FPS, output_path=temp_video_path)
            agent = BrowserAgent(blocklist=blocklist, proxy=proxy_used)
            self._agent = agent
            _check_macos_screen_permission()
            events.log_info.send(
                "recorder",
                text=(
                    f"[RULE] STARTING RECORDER session={self.id} "
                    f"(blocklist={blocklist.total_enabled_domains()} domains, "
                    f"proxy={'on' if proxy_used else 'direct'})"
                ),
            )
            self._telemetry_started(proxy_used, browser_resolution)
            if self.include_video:
                video_recorder.start()
            self._set_state(STATE_RECORDING)
            self._set_phase("recording")

            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                temp_data_dir = loop.run_until_complete(
                    agent.run_session(url=self.url, stop_token=self.stop_token, browser_resolution=browser_resolution)
                )
            finally:
                asyncio.set_event_loop(None)
                loop.close()
        except Exception as exc:
            error_source = "browser_crash"
            self._set_error(str(exc))
            events.log_error.send("recorder", text=f"Recording session {self.id} failed: {exc}")
            events.log_traceback.send("recorder")

        finally:
            self._agent_stats_snapshot(agent)
            self._set_state(STATE_COMPILING)
            self._set_phase("compiling")

            video_start_unix, video_error = self._finish_capture(video_recorder, blocklist, agent)
            if video_error is not None:
                error_source = video_error

            # A compile that raised unexpectedly marks the session error itself;
            # surface that as the phase's error source for telemetry.
            error_before_compile = self.error
            final_video_path, output_dir, success = self._compile_dump(
                temp_data_dir, temp_video_path, video_start_unix, vision_state
            )
            if not success and self.error != error_before_compile:
                error_source = "compilation_error"

            if temp_data_dir:
                self._vision_summary = _vision_summary_block(vision_state)

            with self._lock:
                self.final_video_path = final_video_path
                self.capture_stats = dict(getattr(agent, "stats", {})) if agent else self.capture_stats

            self._telemetry_ended(
                record_start=record_start,
                proxy_used=proxy_used,
                success=bool(success),
                error_source=error_source,
                agent=agent,
                browser_resolution=browser_resolution,
            )
            self._terminal_outcome(success, error_source)

    def _seed_vision_state(self) -> dict:
        """Vision-key state at session start (env tier or ~/.automatiq/config.toml).

        The resolved model (from config at import time) is threaded through to
        the compile pipeline so the analyzer calls exactly the reported model;
        include_video=false sessions skip with the video-disabled reason.
        """
        preflight = self._vision_preflight_snapshot()
        vision_state: dict = {
            "configured": bool(preflight["configured"] and self.include_video),
            "model": preflight["model"],
        }
        if not self.include_video:
            vision_state["skip_reason"] = _SKIP_VIDEO_DISABLED
        elif not preflight["configured"]:
            vision_state["skip_reason"] = _SKIP_NO_KEY
        return vision_state

    def _resolve_browser(self) -> tuple[str, Path | None, str] | None:
        """Managed-browser resolution, before anything heavy starts.

        The lazy import sits alongside the other heavy imports;
        browser_manager itself only pulls stdlib + config. On failure this
        marks the session failed (error, events, STATE_FAILED) and returns
        None - the caller returns early. On success the descriptor is stashed
        for status().
        """
        try:
            from automatiq.core.browser_manager import resolve_browser_for_recording

            browser_resolution = resolve_browser_for_recording(
                no_auto_download=False, prompt_callback=None, progress_callback=None
            )
        except Exception as exc:
            message = f"browser setup failed: {exc}"
            self._set_error(message)
            events.log_error.send("recorder", text=f"Recording session {self.id} failed: {message}")
            events.log_traceback.send("recorder")
            self._set_state(STATE_FAILED, error=message)
            return None
        self._browser_desc = browser_resolution[2]
        return browser_resolution

    def _finish_capture(self, video_recorder, blocklist, agent) -> tuple[float | None, str | None]:
        """Stop the recorder, reset the stop token, close the blocklist DB.

        Returns (video_start_unix, error_source) where error_source is
        "video_error" when the recorder failed to stop, else None.
        """
        video_start_unix: float | None = None
        error_source: str | None = None
        try:
            if video_recorder is not None:
                video_start_unix = video_recorder.stop()
        except Exception as exc:
            error_source = "video_error"
            events.log_error.send("recorder", text=f"Failed to stop video recorder: {exc}")
            events.log_traceback.send("recorder")

        if self.stop_token.is_stopped():
            # Stop ends capture; compilation still runs.
            self.stop_token.reset()

        if blocklist is not None:
            blocked = getattr(agent, "stats", {}).get("blocked_by_blocklist", 0) if agent else 0
            if blocked:
                events.log_info.send("recorder", text=f"Blocklist filtered {blocked} ad/tracker request(s)")
            try:
                blocklist.close()
            except Exception as exc:
                events.log_warn.send("recorder", text=f"Failed to close blocklist DB: {exc}")
                events.log_traceback.send("recorder")
        return video_start_unix, error_source

    def _compile_dump(
        self,
        temp_data_dir: str | None,
        temp_video_path: str,
        video_start_unix: float | None,
        vision_state: dict,
    ) -> tuple[str | None, str | None, bool]:
        """Compile the captured stream into the session dump (best-effort).

        Sessions without captured data skip compilation with a warning.
        Returns (final_video_path, output_dir, success); an unexpected compile
        failure is logged and recorded via _set_error.
        """
        if not temp_data_dir or (video_start_unix is None and self.include_video):
            events.log_warn.send("recorder", text="Session data missing - skipping compilation.")
            return None, None, False
        try:
            from automatiq.core.recorder.compile.workspace import compile_workspace

            final_video_path, output_dir, success = compile_workspace(
                session_name=self.session_name,
                temp_data_dir=temp_data_dir,
                full_video_path=temp_video_path,
                video_start_unix=float(video_start_unix or 0.0),
                output_root=str(self.output_root),
                on_skip_requested=lambda remaining: True,
                cancel_token=self.cancel_token,
                stop_token=self.stop_token,
                vision_state=vision_state,
            )
            return final_video_path, output_dir, success
        except Exception as exc:
            self._set_error(str(exc))
            events.log_error.send("recorder", text=f"Workspace compilation raised unexpectedly: {exc}")
            events.log_traceback.send("recorder")
            return None, None, False

    def _terminal_outcome(self, success: bool, error_source: str | None) -> None:
        """Set the final state and release waiters.

        Precedence: success -> completed; a stop request that arrived during
        compilation -> stopped; an already-failed session keeps its state;
        anything else -> failed (recorded error, phase error source, or a
        generic fallback).
        """
        self.ended_at = time.time()
        if success:
            self._set_state(STATE_COMPLETED)
        elif self.stop_token.is_stopped():
            self._set_state(STATE_STOPPED)
        elif self.state == STATE_FAILED:
            pass
        else:
            self._set_state(STATE_FAILED, error=self.error or error_source or "unknown failure")
        self._done.set()

    def _agent_stats_snapshot(self, agent) -> None:
        if agent is None:
            return
        try:
            with self._lock:
                self.capture_stats = dict(agent.stats)
        except Exception as exc:
            events.log_debug.send("recorder", text=f"agent stats snapshot failed: {exc}")

    # -- Telemetry (fire-and-forget, mirrors the CLI product's two events) ----

    def _telemetry_started(self, proxy_used: str | None, browser_resolution: tuple | None = None) -> None:
        try:
            _ensure_telemetry_started()
            from automatiq.core.telemetry import RecordingStartedProps, client

            # Mirror the CLI product: report the resolution verb
            # ("browser_executable_path" / "browser"), falling back to
            # "brave" only when no resolution happened (failed setup).
            browser = str(browser_resolution[0]) if browser_resolution else "brave"
            client.track_recording_started(
                RecordingStartedProps(
                    browser=browser,
                    proxy_enabled=proxy_used is not None,
                    blocklist_enabled=bool(config.BLOCKLIST_SOURCES),
                )
            )
        except Exception:
            pass

    def _telemetry_ended(
        self,
        record_start: float,
        proxy_used: str | None,
        success: bool,
        error_source: str | None,
        agent,
        browser_resolution: tuple | None = None,
    ) -> None:
        try:
            from automatiq.core.telemetry import RecordingEndedProps, client

            stats = getattr(agent, "stats", {}) if agent else {}
            crash_reason = error_source
            if crash_reason is None and getattr(agent, "session_crashed", False):
                crash_reason = "browser_crash"
            browser = str(browser_resolution[0]) if browser_resolution else "brave"
            client.track_recording_ended(
                RecordingEndedProps(
                    duration_seconds=round(time.monotonic() - record_start, 1),
                    total_http_requests=int(stats.get("total_requests", 0)),
                    total_ws_connections=int(stats.get("ws_connections", 0)),
                    total_ws_frames=int(stats.get("ws_frames_sent", 0) + stats.get("ws_frames_received", 0)),
                    browser_used=browser,
                    proxy_enabled=proxy_used is not None,
                    has_ai_analysis=success,
                    crash_reason=crash_reason,
                )
            )
        except Exception:
            pass


class SessionRegistry:
    """Thread-safe id -> RecordingSession map. One instance per server."""

    def __init__(self, output_root: str | None = None) -> None:
        self.output_root = output_root or str(config.OUTPUT_DIR)
        self._sessions: dict[str, RecordingSession] = {}
        self._lock = threading.Lock()

    def create(
        self,
        url: str,
        session_name: str | None = None,
        proxy: str | None = None,
        include_video: bool = True,
        vision_preflight_result: dict | None = None,
    ) -> RecordingSession:
        """Create a session, register it, and start its worker thread."""
        session = RecordingSession(
            url=url,
            session_name=session_name,
            proxy=proxy,
            include_video=include_video,
            output_root=self.output_root,
            vision_preflight_result=vision_preflight_result,
        )
        with self._lock:
            self._sessions[session.id] = session
        session.start()
        events.log_info.send("runtime", text=f"Session created: {session.id}")
        return session

    def get(self, session_id: str) -> RecordingSession | None:
        """Look up a session by id; None when unknown."""
        with self._lock:
            return self._sessions.get(session_id)

    def latest(self) -> RecordingSession | None:
        """The most recently created session, or None when the registry is empty."""
        with self._lock:
            if not self._sessions:
                return None
            return max(self._sessions.values(), key=lambda s: s.created_at)

    def list_statuses(self, include_capture: bool = False) -> list[dict]:
        with self._lock:
            sessions = sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)
        rows = []
        for s in sessions:
            row = s.status()
            if not include_capture:
                row.pop("capture", None)
            row.pop("recent_log", None)  # list mode stays compact; single-session only
            rows.append(row)
        return rows

    def prune_terminal(self, keep_last: int = 10) -> int:
        """Drop terminal sessions beyond the most recent `keep_last` (memory hygiene)."""
        with self._lock:
            terminals = sorted(
                (s for s in self._sessions.values() if s.is_terminal),
                key=lambda s: s.created_at,
                reverse=True,
            )
            stale = terminals[keep_last:]
            for s in stale:
                self._sessions.pop(s.id, None)
            return len(stale)

    def stop_all(self, join_timeout: float = 20.0) -> None:
        """Fan the stop signal into every live session and wait briefly."""
        with self._lock:
            sessions = list(self._sessions.values())
        live = [s for s in sessions if not s.is_terminal]
        if not live:
            return
        events.log_info.send("runtime", text=f"Stopping {len(live)} active session(s)...")
        for s in live:
            s.stop()
        deadline = time.monotonic() + join_timeout
        for s in live:
            remaining = max(0.0, deadline - time.monotonic())
            s.wait(timeout_s=remaining)


class ParentWatchdog(threading.Thread):
    """Exits the server when the MCP host process disappears.

    Polls ``os.getppid()`` against the value captured at startup:

    - POSIX: effective - orphaned children are reparented, so the ppid
      changes within ``interval`` seconds after the host dies.
    - Windows: ineffective in-process - the parent pid does not change, and
      a ``taskkill /F`` leaves no chance to run Python code anyway. Graceful
      stdin-EOF shutdown (lifespan -> ``registry.stop_all``) is the primary
      safety net there; treat this watchdog as defence-in-depth.
    """

    def __init__(self, registry: SessionRegistry, interval: float = 5.0) -> None:
        super().__init__(name="automatiq-parent-watchdog", daemon=True)
        self.registry = registry
        self.interval = interval
        self._parent_pid = os.getppid()
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        """End the watchdog loop (called on clean server shutdown)."""
        self._stop_evt.set()

    def run(self) -> None:
        """Poll os.getppid() until the MCP host disappears, then shut down."""
        while not self._stop_evt.wait(self.interval):
            current = os.getppid()
            if current != self._parent_pid:
                logger.error(
                    "MCP host process vanished (ppid %s -> %s); stopping sessions and exiting.",
                    self._parent_pid,
                    current,
                )
                try:
                    self.registry.stop_all(join_timeout=10.0)
                finally:
                    os._exit(1)
