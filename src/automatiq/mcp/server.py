"""automatiq recorder MCP server.

FastMCP app exposing AutomatiQ's browser-session recorder as a small
tool surface over stdio. All heavy imports (zendriver, mss,
litellm, magika, imageio-ffmpeg) are deferred to the session worker thread;
server startup touches only light modules so initialize + tools/list beat
every harness's 30s cold-start budget.

Interaction contract (shaped by Codex/opencode/oh-my-pi harness research):
- Tools never block for long: start_recording returns instantly, reads are
  sub-millisecond, and the ONLY slow tool is wait_for_completion which caps
  itself well below every harness's tool timeout.
- Every result is compact JSON emitted as BOTH structuredContent and pretty
  text (oh-my-pi ignores structuredContent entirely).
- Deep artifacts are plain files in the session's output folder; a README.md
  written at compile time documents structure and schemas.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import textwrap
import threading
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from pydantic import Field

from automatiq.core import config
from automatiq.core.config import VERSION
from automatiq.mcp.annotation import (
    AnnotationJob,
    analyzable_clips,
    find_session_dir,
    get_annotation_registry,
    latest_session_dir,
)
from automatiq.mcp.logging_setup import _configure_logging, _wire_event_logging
from automatiq.mcp.runtime import SessionRegistry, _ensure_telemetry_started
from automatiq.mcp.vision import vision_preflight

logger = logging.getLogger("automatiq.mcp")

# -- Shared registry (single source of truth for tools) -----------------------

_REGISTRY: SessionRegistry | None = None

# server_started is emitted once per process, at the first lifespan startup.
_server_started_emitted = False


def _get_registry() -> SessionRegistry:
    """The process-wide session registry, created lazily on first tool use."""
    global _REGISTRY
    if _REGISTRY is None:
        from automatiq.core.config import OUTPUT_DIR, ensure_system_dirs

        ensure_system_dirs()
        _REGISTRY = SessionRegistry(output_root=str(OUTPUT_DIR))
    return _REGISTRY


# -- Heavy-import warm-up (burn the module-import gap before the first session) --
#
# The session worker's first act is importing the heavy recorder stack
# (zendriver, numpy, mss, imageio-ffmpeg) - ~30-40s cold, paid AFTER
# start_recording has already returned, so the browser window lags the tool
# call. Warming those imports here moves that burn to server startup, where it
# overlaps the human's conversation time instead of the first launch.

HEAVY_MODULES: tuple[str, ...] = (
    "automatiq.core.recorder.browser_agent",
    "automatiq.core.recorder.video_recorder",
)


def _warm_imports(modules: Sequence[str] = HEAVY_MODULES) -> list[str]:
    """Import *modules* so the first session hits sys.modules instead of disk.

    Sleeps 1s first to let the MCP handshake finish. One failing module is
    logged and skipped - the worker's own lazy import at session time remains
    the fallback (a warmed module is a cache hit; an unwarmed one is exactly
    today's behavior, never worse).
    """
    warmed: list[str] = []
    time.sleep(1.0)  # yield the interpreter to the MCP handshake before the big imports
    for name in modules:
        try:
            t0 = time.perf_counter()
            importlib.import_module(name)
            logger.debug("[WARM] imported %s (%.1fs)", name, time.perf_counter() - t0)
            warmed.append(name)
        except Exception as exc:
            logger.warning("[WARM] import failed for %s: %s; will import on first session", name, exc)
    return warmed


def _start_import_warmup() -> threading.Thread:
    """Start the daemon thread that pre-imports the heavy recorder stack."""
    thread = threading.Thread(target=_warm_imports, name="automatiq-import-warmup", daemon=True)
    thread.start()
    return thread


# -- Lifespan ------------------------------------------------------------------


def _emit_server_started() -> None:
    """Emit the server_started telemetry event, once per process.

    Booleans and the recorder model string ONLY - never keys, endpoints, or
    proxy URLs. Fail-open: telemetry must never break server startup.
    """
    global _server_started_emitted
    if _server_started_emitted:
        return
    _server_started_emitted = True
    try:
        _ensure_telemetry_started()
        from automatiq.core.telemetry import ServerStartedProps, client

        proxy_configured = bool(
            getattr(config, "RECORDER_PROXY_ENABLED", False)
            or getattr(config, "RECORDER_PROXY_SERVER", None)
            or getattr(config, "RECORDER_PROXY_PROVIDER", None)
        )
        try:
            vision_key_present = bool(vision_preflight().get("configured"))
        except Exception:
            vision_key_present = False
        client.track_server_started(
            ServerStartedProps(
                model=str(config.RECORDER_AI_MODEL or ""),
                video_default=True,  # include_video=True is the tool's default
                proxy_configured=proxy_configured,
                vision_key_present=vision_key_present,
            )
        )
    except Exception:
        pass


@asynccontextmanager
async def _app_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Wire the recorder event bus + status log, and stop sessions on shutdown."""
    from automatiq.mcp.runtime import ParentWatchdog
    from automatiq.mcp.status_log import connect_status_log

    registry = _get_registry()
    _wire_event_logging()
    connect_status_log()

    # Telemetry: start the client (idempotent) and report the startup config.
    _emit_server_started()

    # Orphan-proofing (POSIX-effective): end sessions + exit if the host dies.
    watchdog = ParentWatchdog(registry)
    watchdog.start()
    logger.info("automatiq %s ready (output_root=%s)", VERSION, registry.output_root)
    try:
        yield {"registry": registry}
    finally:
        watchdog.stop()
        # Short grace only: on EOF shutdown the host is gone; a multi-minute
        # compile can't be saved, so don't stall server exit for it.
        await asyncio.to_thread(registry.stop_all, 3.0)


app = FastMCP(
    name="automatiq",
    version=VERSION,
    instructions=textwrap.dedent(
        """
        Automatiq MCP makes reverse-engineering static and dynamic websites easy.
        It helps AI agents build scripts that replay a website's behaviour without a browser.
        This is achieved by recording all HTTP requests and user interactions, and compiling
        them into a workspace of folders - an explorable artifact of the whole session that
        shows the exact sequence of user actions, correlated with the requests the browser
        sent, so you can understand the intended behaviour of the website in the user's workflow.

        Workflow:
        1. start_recording: a visible browser opens for the user to browse their target
        website(s) as usual, while Automatiq records all user interactions and HTTP requests.
        2. wait_for_completion(session_id): polls until state is terminal
        (completed|failed|stopped) and returns the full status report as the end result.
        3. Read README.md at readme_path FIRST - it documents the layout and schema of every
        artifact (timeline.json, SUMMARY.json, per-transaction folders, websocket frames,
        video clips).
        4. Stuck on what the user did? annotate_user_interactions(session_id) re-runs the AI
        analysis on the recorded dump - optionally with a focus question about the user's flow
        and aim - and is polled with the same tools.

        Output lands under automatiq_sessions/ in the server's working directory. get_status()
        lists past sessions.

        Vision analysis (on by default): the user's workflow and actions performed in the
        browser are analyzed by an AI model to provide additional context and insights. The
        AI model labels each action, identifies the elements interacted with, and provides an
        annotated timeline of the user's intent.

        Vision analysis requires a vision-capable model key: set recorder_api_key under
        [models] in ~/.automatiq/config.toml (auto-created) - the config file is the only
        key source; provider env vars are never consulted. Without a key, recording is
        unaffected - you keep all raw captures, only the AI
        annotation layer is skipped (backfill it later with annotate_user_interactions once
        a key is configured).
        """
    ).strip(),
    lifespan=_app_lifespan,
)


# -- Tool-call telemetry middleware --------------------------------------------


def _track_tool_called(tool: str, duration_ms: int, ok: bool, error_class: str | None) -> None:
    """Queue a tool_called telemetry event. Fail-open, never blocks the call."""
    try:
        from automatiq.core.telemetry import client

        client.track_tool_called(tool=tool, duration_ms=duration_ms, ok=ok, error_class=error_class)
    except Exception:
        pass


class _ToolTelemetryMiddleware(Middleware):
    """Emit one ``tool_called`` event per MCP tool invocation (telemetry v2).

    FastMCP's public ``on_call_tool`` hook wraps every tools/call - all five
    tools, read-only ones included - without touching the tool functions, so
    the tool surface (signatures, docstrings, schemas) stays byte-identical.
    Emission happens AFTER the call completes or fails; on failure the
    exception class name is reported and the exception is re-raised unchanged.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next) -> Any:
        tool = str(getattr(context.message, "name", "unknown"))
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            _track_tool_called(
                tool=tool,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                ok=False,
                error_class=type(exc).__name__,
            )
            raise
        _track_tool_called(
            tool=tool,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            ok=True,
            error_class=None,
        )
        return result


app.add_middleware(_ToolTelemetryMiddleware())


# -- Helpers -------------------------------------------------------------------


def _result(payload: dict[str, Any], *, is_error: bool = False) -> ToolResult:
    """Compact dual-format result: structuredContent + pretty text (JSON)."""
    text = json.dumps(payload, indent=2, default=str)
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=is_error,
    )


def _resolve_session(session_id: str | None) -> tuple[Any | None, dict[str, Any] | None]:
    """Resolve a session by id (or the latest one); returns (session, error)."""
    reg = _get_registry()
    session = reg.get(session_id) if session_id else reg.latest()
    if session is None:
        err = {
            "error": f"unknown session_id '{session_id}'" if session_id else "no sessions exist yet",
            "hint": "call get_status() to list sessions, or start_recording() to create one",
        }
        return None, err
    return session, None


def _annotation_fallback_status(session_id: str | None) -> dict[str, Any] | None:
    """Status for a session known only on disk + in the annotation registry.

    Covers sessions recorded by a previous server process: the dump lives on
    disk and an annotate_user_interactions job carries the live state that
    get_status / wait_for_completion poll.
    """
    jobs = get_annotation_registry()
    job = jobs.get(session_id) if session_id else jobs.latest()
    if job is None:
        return None
    return {
        "session_id": job.session_id,
        "state": "completed",  # recorded earlier; only the dump lives on
        "source": "disk (recorded before this server started)",
        "output_dir": str(job.session_dir),
        "readme_path": str(job.session_dir / "README.md"),
        "annotation": job.snapshot(),
    }


# -- Tools ---------------------------------------------------------------------


@app.tool(
    name="start_recording",
    description=textwrap.dedent(
        """
        Launches a browser for the user to use, and starts recording their interactions, HTTP
        requests, and a video of the whole session. It then compiles all the information into
        a multi-folder artifact that provides a complete view into the website's internals,
        its logic and workflow.

        USE THIS TOOL WHEN:
        - The user wants to create automations or scraping scripts for static/dynamic/SPA
        websites.
        - You want to understand a website's complete internals and workings.
        - You need to understand an undocumented or internal API of an unknown website: real
        URLs, headers, payload schemas, pagination, websocket protocols.
        - Requests work in the browser but fail in code: compare the recorded ground truth
        against your reproduction (headers, body shape, ordering).

        A browser window opens for the user, while this tool returns immediately (never
        blocks) with a `session_id`.

        Recording ends automatically when the user closes the last browser window, and the
        data is compiled into a research artifact.

        Poll for the session's completion with `wait_for_completion(session_id)` until
        terminal, then read the README.md at readme_path - it documents every artifact's
        schema.

        Note: bodies are stored verbatim, including credentials - treat the output folder as
        secret.
        """
    ).strip(),
)
async def start_recording(
    url: Annotated[
        str,
        Field(
            description=(
                "First page the recording browser opens (http(s):// or about:blank) - typically "
                "the site whose flow you need to understand."
            )
        ),
    ],
    session_name: Annotated[
        str | None,
        Field(
            description=("Folder name for this session's dump; auto-derived from the most-visited domain when omitted.")
        ),
    ] = None,
    proxy: Annotated[
        str | None,
        Field(
            description=(
                "HTTP/SOCKS proxy URL for the recording browser only, e.g. http://host:3128 "
                "(corporate proxy, or a debugging proxy like mitmproxy)."
            )
        ),
    ] = None,
    include_video: Annotated[
        bool,
        Field(
            description=textwrap.dedent(
                """
                true (default, recommended): capture screen video AND get AI analysis - at compile
                time a vision model watches the video and labels every action (which element, what
                was typed, intent), cuts a short clip per action, and writes summaries; pair the
                labels with the timeline to see which step fired which requests. Needs a
                vision-capable key configured once (recorder_api_key in ~/.automatiq/config.toml
                - the only key source; provider env vars are never consulted); without one,
                recording is unaffected - only the AI layer is skipped.
                """
            ).strip(),
        ),
    ] = True,
) -> ToolResult:
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        return _result({"error": "url must be a non-empty string of <=2048 chars"}, is_error=True)
    if proxy is not None and "://" not in proxy:
        return _result({"error": "proxy must be a URL like http://host:port or socks5://host:1080"}, is_error=True)
    reg = _get_registry()
    # Resolve the vision preflight ONCE here and hand it to the session: the
    # resolution has side effects (config-file key plumbing), so the response
    # block, the worker seeding, and every status() poll must share one dict.
    vision_block = vision_preflight() if include_video else None
    session = reg.create(
        url=url.strip(),
        session_name=session_name,
        proxy=proxy,
        include_video=bool(include_video),
        vision_preflight_result=vision_block,
    )
    snap = session.status()
    payload = {
        "session_id": snap["session_id"],
        "session_name": snap["session_name"],
        "state": snap["state"],
        "output_root": snap["output_root"],
        "include_video": snap["include_video"],
        "note": (
            "Recording started in background; browser window is opening. It ends when the user closes "
            "the last browser window. Poll wait_for_completion(session_id). "
            "The dump stores all traffic bodies verbatim (cookies, credentials included) "
            "- treat the folder as sensitive."
        ),
    }
    if include_video:
        payload["vision"] = vision_block
    return _result(payload)


@app.tool(
    name="stop_recording",
    description=textwrap.dedent(
        """
        End a running recording early (alternative to the user closing the browser window). Capture
        stops within ~1 second; compilation then continues on its own. Afterwards poll
        wait_for_completion until state is terminal, then read the dump's README.md.
        """
    ).strip(),
)
async def stop_recording(
    session_id: Annotated[str, Field(description="Session to stop (see get_status())")],
) -> ToolResult:
    reg = _get_registry()
    session = reg.get(session_id)
    if session is None:
        return _result({"error": f"unknown session_id '{session_id}'"}, is_error=True)
    already_terminal = session.is_terminal
    session.stop()
    return _result(
        {
            "session_id": session.id,
            "state": session.state,
            "stop_requested": True,
            "note": "Stop signalled; capture ends within ~1s."
            if not already_terminal
            else "Session was already finished.",
            "next": "Poll wait_for_completion until state is terminal; compilation still runs afterwards.",
        }
    )


@app.tool(
    name="get_status",
    description=textwrap.dedent(
        """
        Inspect recording sessions. Without session_id: compact newest-first list of all known
        sessions (use to recover context after a restart). With session_id: that session's full
        status - state, capture counters, and once compiled the output_dir + readme_path of its
        dump. Returns instantly, never blocks. State values:
        created|initializing|recording|compiling|completed|failed|stopped ('initializing' covers
        first-run downloads/browser launch). Single-session status also carries an 'annotation'
        block while/after annotate_user_interactions runs on it.
        """
    ).strip(),
    annotations={"readOnlyHint": True},
)
async def get_status(
    session_id: Annotated[str | None, Field(description="Omit to list all sessions")] = None,
) -> ToolResult:
    reg = _get_registry()
    if session_id is None:
        reg.prune_terminal(keep_last=10)
        rows = reg.list_statuses(include_capture=False)[:20]
        return _result({"count": len(rows), "sessions": rows})
    session, err = _resolve_session(session_id)
    if err is not None:
        fallback = _annotation_fallback_status(session_id)
        if fallback is not None:
            return _result(fallback)
        return _result(err, is_error=True)
    snap = session.status()
    job = get_annotation_registry().get(session.id)
    if job is not None:
        snap["annotation"] = job.snapshot()
    return _result(snap)


@app.tool(
    name="wait_for_completion",
    description=textwrap.dedent(
        """
        Wait for a recording to finish and tell you where the dump is. CALL IN A LOOP after
        start_recording: each call blocks at most 25s (designed to fit any client's tool-timeout
        budget) and returns the current status; stop calling when result.completed is true. On
        completion the status carries output_dir + readme_path - read that README.md first: it
        documents the layout and schema of every artifact. The recording itself ends when the user
        closes the browser's last window (or stop_recording). Also waits for an
        annotate_user_interactions job running on the session - its result arrives in
        status.annotation.
        """
    ).strip(),
    annotations={"readOnlyHint": True},
)
async def wait_for_completion(
    session_id: Annotated[str | None, Field(description="Omit to use the most recent session")] = None,
    timeout_s: Annotated[float, Field(description="Max seconds to block this single call")] = 15.0,
) -> ToolResult:
    session, err = _resolve_session(session_id)
    if err is not None:
        fallback = _annotation_fallback_status(session_id)
        if fallback is None:
            return _result(err, is_error=True)
        jobs = get_annotation_registry()
        job = jobs.get(session_id) if session_id else jobs.latest()
        clamped = min(max(float(timeout_s or 15.0), 1.0), 25.0)
        await asyncio.to_thread(job.wait, clamped)
        fresh = _annotation_fallback_status(session_id)
        fresh["_wait"] = {"timeout_s": clamped, "reached_terminal": not job.is_running}
        return _result(fresh)
    clamped = min(max(float(timeout_s or 15.0), 1.0), 25.0)
    await asyncio.to_thread(session.wait, clamped)
    job = get_annotation_registry().get(session.id)
    if job is not None and job.is_running:
        await asyncio.to_thread(job.wait, clamped)
    snap = session.status()
    if job is not None:
        snap["annotation"] = job.snapshot()
    snap["_wait"] = {
        "timeout_s": clamped,
        "reached_terminal": bool(session.is_terminal and (job is None or not job.is_running)),
    }
    if session.is_terminal and snap.get("output_dir"):
        snap["note"] = (
            "Session folder ready. Read README.md at readme_path first - "
            "it documents the structure and schemas of all artifacts. "
            "The dump contains unredacted bodies (cookies, credentials) - treat it as sensitive."
        )
    return _result(snap)


@app.tool(
    name="annotate_user_interactions",
    description=textwrap.dedent(
        """
        Re-run AI vision analysis on an already-recorded session's dump, separately from the
        recording. It re-analyzes every action clip with a vision model, refreshes the ai_*
        annotations in timeline.json and the session_flow in SUMMARY.json (originals are backed
        up under annotations_backup/; captured data is never modified), and can additionally
        answer a focus question with a session-level narrative of the user's flow and aim,
        written to session_dump/focused_analysis.md.

        USE THIS TOOL WHEN:
        - The dump's ai_macro_summary fields are missing or say "Error: Could not analyze clip."
        - The session README says vision annotation was skipped (no key) or failed.
        - The analysis misread a complex site and you are stuck: you cannot tell what the user
        did or what they were trying to achieve, and cannot proceed.

        Returns immediately; poll with get_status(session_id) / wait_for_completion(session_id):
        progress lines start with [ANNOTATE], the final result (including the narrative) arrives
        in status.annotation.

        Needs a vision-capable key (recorder_api_key under [models] in
        ~/.automatiq/config.toml - the only key source; provider env vars are
        never consulted); always uses the configured recorder model.
        """
    ).strip(),
)
async def annotate_user_interactions(
    session_id: Annotated[
        str | None,
        Field(description="Session to re-analyze (see get_status()); omit to use the most recent recording"),
    ] = None,
    focus: Annotated[
        str | None,
        Field(
            description=(
                "Optional question about the user's flow and aim, answered with a narrative "
                "grounded in the clips - e.g. 'what was the user trying to achieve on the results page?'"
            ),
        ),
    ] = None,
) -> ToolResult:
    if focus is not None and len(focus) > 2000:
        return _result({"error": "focus must be a string of <=2000 chars"}, is_error=True)

    reg = _get_registry()
    jobs = get_annotation_registry()

    # Resolve the target: a live registry session (any folder name) first,
    # then the disk scan (default-named recording dirs, survives restarts).
    session = reg.get(session_id) if session_id else reg.latest()
    session_dir = None
    sid: str | None = None
    if session is not None:
        if not session.is_terminal:
            return _result(
                {
                    "session_id": session.id,
                    "error": "session is still recording/compiling",
                    "hint": "poll wait_for_completion until the state is terminal, then annotate",
                },
                is_error=True,
            )
        sid = session.id
        session_dir = session._resolved_output_dir() or find_session_dir(session.output_root, sid)
    elif session_id:
        sid = session_id
        existing = jobs.get(sid)
        if existing is not None and existing.is_running:
            return _result(
                {
                    "session_id": sid,
                    "error": "an annotation job is already running for this session",
                    "hint": f"poll get_status('{sid}') - the result arrives in status.annotation",
                },
                is_error=True,
            )
        session_dir = find_session_dir(reg.output_root, session_id) or find_session_dir(config.OUTPUT_DIR, session_id)
    else:
        latest_dir = latest_session_dir(reg.output_root) or latest_session_dir(config.OUTPUT_DIR)
        if latest_dir is not None:
            session_dir = latest_dir
            sid = latest_dir.name.removeprefix("recording_")

    if session_dir is None:
        return _result(
            {
                "error": f"no recorded session found for '{session_id or 'the latest one'}'",
                "hint": (
                    "call get_status() to list this server's sessions; only default-named "
                    "recording_* folders are discoverable after a server restart"
                ),
            },
            is_error=True,
        )

    existing = jobs.get(sid)
    if existing is not None and existing.is_running:
        return _result(
            {
                "session_id": sid,
                "error": "an annotation job is already running for this session",
                "hint": f"poll get_status('{sid}') - the result arrives in status.annotation",
            },
            is_error=True,
        )

    try:
        n_clips = len(analyzable_clips(session_dir))
    except Exception as exc:
        return _result({"session_id": sid, "error": f"cannot read the session dump: {exc}"}, is_error=True)
    if n_clips == 0:
        return _result(
            {
                "session_id": sid,
                "error": "session has no video clips to analyze",
                "hint": (
                    "the recording had include_video=false or captured no user actions - "
                    "re-record with video to enable AI analysis"
                ),
            },
            is_error=True,
        )

    # Warn-and-continue sibling of start_recording's vision block: without a
    # usable model there is nothing to fall back to, so surface the exact
    # preflight warning and how to fix it.
    vision_block = vision_preflight()
    if not vision_block["configured"]:
        return _result(
            {
                "session_id": sid,
                "error": "vision model not configured - annotation cannot run",
                "vision": vision_block,
                "hint": vision_block.get("warning"),
            },
            is_error=True,
        )

    job = AnnotationJob(session_id=sid, session_dir=session_dir, focus=focus, model=vision_block["model"])
    jobs.register(job)
    job.start()
    return _result(
        {
            "session_id": sid,
            "output_dir": str(session_dir),
            "clips_to_analyze": n_clips,
            "annotation": job.snapshot(),
            "note": (
                "Re-annotation running in background. Poll wait_for_completion(session_id) or "
                "get_status(session_id): progress lines start with [ANNOTATE]; the final result "
                "(and the focus narrative) arrives in status.annotation."
            ),
        }
    )


# -- Entry point ---------------------------------------------------------------


def main() -> None:
    """Configure stderr/file logging, then serve the MCP protocol over stdio."""
    _configure_logging()
    # Warm-up lives here (the console-script entry), NOT at module import:
    # pytest imports this module and must never spawn threads, while the real
    # server overlaps the heavy-import cost with the human's conversation time.
    _start_import_warmup()
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
