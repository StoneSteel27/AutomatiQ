"""On-demand AI re-annotation of recorded sessions.

``annotate_user_interactions`` (server.py) re-runs the vision analyzer over
an existing session dump's clips in a background thread:

- per-clip results refresh the ``ai_*`` fields on ``user_action`` events in
  ``timeline.json`` and rebuild ``SUMMARY.json``'s ``session_flow``;
- the README's vision line is rewritten to reflect the re-run;
- originals are backed up once under ``annotations_backup/`` (captured data
  is never masked: raw telemetry fields are never touched);
- an optional ``focus`` question gets a session-level narrative (one extra
  LLM call grounded in the clip summaries + raw telemetry), written to
  ``session_dump/focused_analysis.md`` and returned in the job snapshot.

Progress is polled through the EXISTING get_status / wait_for_completion
tools: running jobs register here, and the server merges
``status["annotation"]`` snapshots into their results. This module stays
light at import time (the analyzer / litellm load lazily inside the worker
thread) so the test suite's no-heavy-imports guarantee holds.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from automatiq.core import events

# Error-summaries the analyzer produces instead of raising (ai_analyzer's
# error_resp). A clip whose macro_summary starts with this counts as failed.
_ANALYSIS_ERROR_PREFIX = "Error:"

# Session-dump layout constants (mirror compile/workspace.py).
_DUMP_REL = Path("workspace") / "session_dump"
_TIMELINE_REL = _DUMP_REL / "timeline.json"
_SUMMARY_REL = _DUMP_REL / "SUMMARY.json"
_CLIPS_REL = _DUMP_REL / "clips"


def _timeline_path(session_dir: Path) -> Path:
    return session_dir / _TIMELINE_REL


def find_session_dir(output_root: str | Path, session_id: str | None) -> Path | None:
    """Locate a recorded session's folder on disk by its session id.

    Only default-named dirs carry the id (``recording_<id>`` plus compile's
    ``_NN`` collision suffixes); custom session_name folders are resolvable
    only while their RecordingSession lives in the in-memory registry.
    Returns None unless the dir contains a compiled timeline.json.
    """
    if not session_id:
        return None
    root = Path(output_root)
    if not root.is_dir():
        return None
    candidates = sorted(root.glob(f"recording_{session_id}")) + sorted(root.glob(f"recording_{session_id}_*"))
    for candidate in candidates:
        if _timeline_path(candidate).is_file():
            return candidate
    return None


def latest_session_dir(output_root: str | Path) -> Path | None:
    """The most recently modified recorded session under *output_root*."""
    root = Path(output_root)
    if not root.is_dir():
        return None
    candidates = [d for d in root.glob("recording_*") if _timeline_path(d).is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def _load_timeline(session_dir: Path) -> list[dict]:
    path = _timeline_path(session_dir)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"timeline.json is not a JSON array: {path}")
    return data


def analyzable_clips(session_dir: Path, events: list[dict] | None = None) -> list[str]:
    """Distinct existing clip files referenced by user_action events, in order."""
    if events is None:
        events = _load_timeline(session_dir)
    dump_dir = session_dir / _DUMP_REL
    clips: list[str] = []
    for ev in events:
        if ev.get("event_type") != "user_action":
            continue
        clip = ev.get("ai_video_file")
        if clip and clip not in clips and (dump_dir / clip).is_file():
            clips.append(clip)
    return clips


def _raw_action(ev: dict) -> dict:
    """Rebuild the recorder-action shape analyze_clip expects from a timeline event."""
    raw = {"type": ev.get("action") or "action"}
    details = ev.get("details")
    if isinstance(details, dict):
        raw.update(details)
    raw["timestamp_unix"] = ev.get("timestamp")
    raw["timestamp_iso"] = ev.get("timestamp_iso")
    return raw


def _narrative_completion(model: str, prompt: str) -> str:
    """One text-only LLM call for the focus narrative; raises on failure.

    Module-level so tests can stub it without importing litellm.
    """
    import litellm

    response = litellm.completion(model=model, messages=[{"role": "user", "content": prompt}], max_tokens=1200)
    return (getattr(response.choices[0].message, "content", "") or "").strip()


class AnnotationJob:
    """One background re-annotation run over a session dump on disk."""

    def __init__(self, session_id: str, session_dir: Path, focus: str | None = None, model: str | None = None):
        self.session_id = session_id
        self.session_dir = Path(session_dir)
        self.focus = (focus or "").strip() or None
        self.model = model
        self.state = "running"  # running | completed | failed
        self.error: str | None = None
        self.analyzed = 0
        self.failed = 0
        self.total_clips = 0
        self.actions_without_clips = 0
        self.narrative: str | None = None
        self.narrative_path: Path | None = None
        self.started_at = time.time()
        self.ended_at: float | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    # -- Lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError(f"annotation job for {self.session_id} already started")
        self._thread = threading.Thread(target=self._run, name=f"automatiq-annotate-{self.session_id}", daemon=True)
        self._thread.start()

    def wait(self, timeout_s: float = 20.0) -> bool:
        """Block up to timeout_s; True iff the job reached a terminal state."""
        return self._done.wait(timeout_s)

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    def snapshot(self) -> dict:
        """JSON-safe progress/result snapshot for get_status / wait_for_completion."""
        with self._lock:
            snap = {
                "state": self.state,
                "session_id": self.session_id,
                "output_dir": str(self.session_dir),
                "model": self.model,
                "focus": self.focus,
                "clips": {"analyzed": self.analyzed, "failed": self.failed, "total": self.total_clips},
                "actions_without_clips": self.actions_without_clips,
                "error": self.error,
                "started_at": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds"),
                "ended_at": (
                    datetime.fromtimestamp(self.ended_at).isoformat(timespec="seconds")
                    if self.ended_at is not None
                    else None
                ),
            }
            if self.narrative is not None:
                snap["narrative"] = self.narrative
            if self.narrative_path is not None:
                snap["narrative_path"] = str(self.narrative_path)
            return snap

    # -- Worker ---------------------------------------------------------------

    def _run(self) -> None:
        try:
            # Lazy heavy imports (ai_analyzer pulls litellm + imageio_ffmpeg):
            # this thread only runs from the real server's tool, never at
            # module import, so the test-suite no-heavy-imports guarantee holds.
            from automatiq.core.recorder.ai_analyzer import VideoActionAnalyzer

            self._annotate(VideoActionAnalyzer)
        except Exception as exc:
            events.log_error.send("recorder", text=f"[ANNOTATE] session {self.session_id} failed: {exc}")
            events.log_traceback.send("recorder")
            with self._lock:
                self.state = "failed"
                self.error = str(exc)[:500]
        finally:
            with self._lock:
                self.ended_at = time.time()
            self._emit_telemetry_done()
            self._done.set()

    def _emit_telemetry_done(self) -> None:
        """Emit annotate_run telemetry once the job finishes (fail-open).

        Fires on background completion - not when the tool call returns - with
        the analysis job's wall-clock duration and the terminal outcome.
        """
        try:
            with self._lock:
                ended = self.ended_at
            duration_ms = max(0, int((ended - self.started_at) * 1000))
            ok = self.state == "completed"
            from automatiq.core.telemetry import client

            client.track_annotate_run(duration_ms=duration_ms, ok=ok)
        except Exception:
            pass

    def _annotate(self, analyzer_cls) -> None:
        session_id = self.session_id
        events.log_info.send(
            "recorder",
            text=(f"[ANNOTATE] re-annotating session {session_id} from {self.session_dir.name} (model {self.model})"),
        )

        timeline_events = _load_timeline(self.session_dir)

        # Cluster user_action events by shared clip, preserving timeline order.
        clusters: list[tuple[str, list[int]]] = []
        for idx, ev in enumerate(timeline_events):
            if ev.get("event_type") != "user_action":
                continue
            clip = ev.get("ai_video_file")
            if not clip:
                with self._lock:
                    self.actions_without_clips += 1
                continue
            if clusters and clusters[-1][0] == clip:
                clusters[-1][1].append(idx)
            else:
                clusters.append((clip, [idx]))

        dump_dir = self.session_dir / _DUMP_REL
        clusters = [(clip, idxs) for clip, idxs in clusters if (dump_dir / clip).is_file()]
        with self._lock:
            self.total_clips = len(clusters)
        if not clusters:
            raise RuntimeError(
                "no video clips to analyze - the session has no user_action events with an "
                "existing clip (include_video was off, no actions were captured, or clips were deleted)"
            )

        analyzer = analyzer_cls(model=self.model)
        for clip, idxs in clusters:
            sample = timeline_events[idxs[0]]
            start = sample.get("video_start_sec")
            end = sample.get("video_end_sec")
            has_bounds = isinstance(start, (int | float)) and isinstance(end, (int | float))
            duration = max(0.5, end - start) if has_bounds else 3.0
            result = analyzer.analyze_clip(
                str(dump_dir / clip), duration, raw_actions=[_raw_action(timeline_events[i]) for i in idxs]
            )
            summary = str(result.get("macro_summary", ""))
            with self._lock:
                if summary.startswith(_ANALYSIS_ERROR_PREFIX):
                    self.failed += 1
                else:
                    self.analyzed += 1
            for i in idxs:
                timeline_events[i]["ai_macro_summary"] = summary
                timeline_events[i]["ai_elements_interacted"] = result.get("elements_interacted", [])
                timeline_events[i]["ai_action_success"] = result.get("action_success")
            events.log_info.send("recorder", text=f"[ANNOTATE] {clip}: {summary}")

        fatal_reason = getattr(analyzer, "fatal_reason", None)
        if self.analyzed == 0:
            if fatal_reason == "auth":
                reason = "vision model rejected the key - check recorder_api_key in ~/.automatiq/config.toml"
            elif fatal_reason:
                reason = "vision model unreachable - see the session log"
            else:
                reason = "all clips failed to analyze - see the session log"
            with self._lock:
                self.state = "failed"
                self.error = reason
            events.log_error.send("recorder", text=f"[ANNOTATE] aborted: {reason}")
            return

        self._persist(timeline_events)
        if self.focus:
            self._write_narrative(timeline_events)
        with self._lock:
            self.state = "completed"
        events.log_info.send(
            "recorder",
            text=f"[ANNOTATE] done: {self.analyzed}/{self.total_clips} clips analyzed for session {session_id}",
        )

    # -- Persistence (captured data is never modified; annotations regenerate) --

    def _backup_originals(self, dump_dir: Path) -> Path:
        """One-time backup of the pre-annotation derived artifacts."""
        backup_dir = self.session_dir / "annotations_backup"
        if not backup_dir.exists():
            backup_dir.mkdir()
            for rel, src in (
                ("timeline.json", dump_dir / "timeline.json"),
                ("SUMMARY.json", dump_dir / "SUMMARY.json"),
                ("README.md", self.session_dir / "README.md"),
            ):
                if src.is_file():
                    try:
                        shutil.copy2(src, backup_dir / rel)
                    except Exception as exc:
                        events.log_warn.send("recorder", text=f"[ANNOTATE] backup of {rel} failed: {exc}")
        return backup_dir

    def _persist(self, timeline_events: list[dict]) -> None:
        dump_dir = self.session_dir / _DUMP_REL
        self._backup_originals(dump_dir)

        with open(dump_dir / "timeline.json", "w", encoding="utf-8") as f:
            json.dump(timeline_events, f, indent=2, default=str)

        # Rebuild session_flow from the refreshed annotations (mirrors compile).
        summary_path = dump_dir / "SUMMARY.json"
        if summary_path.is_file():
            with open(summary_path, encoding="utf-8") as f:
                summary = json.load(f)
            session_flow = []
            seen: set[str] = set()
            for ev in timeline_events:
                if ev.get("event_type") != "user_action":
                    continue
                text = ev.get("ai_macro_summary")
                if text and text not in seen and not str(text).startswith(_ANALYSIS_ERROR_PREFIX):
                    seen.add(text)
                    session_flow.append(
                        {
                            "timestamp_iso": ev.get("timestamp_iso"),
                            "timestamp_unix": ev.get("timestamp"),
                            "summary": text,
                        }
                    )
            summary["session_flow"] = session_flow
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, default=str)

        # README vision line reflects the re-run (single prefix, like compile).
        try:
            from automatiq.core.recorder.compile.workspace import _write_readme

            total = max(1, self.total_clips)
            vision_line = (
                f"AI vision annotation: re-annotated {datetime.now():%Y-%m-%d %H:%M} "
                f"(model {self.model}, {self.analyzed}/{total} clips)"
            )
            _write_readme(str(self.session_dir), vision_line=vision_line)
        except Exception as exc:
            events.log_warn.send("recorder", text=f"[ANNOTATE] README refresh failed: {exc}")

    def _write_narrative(self, timeline_events: list[dict]) -> None:
        """Answer the focus question with one LLM call over the refreshed annotations."""
        dump_dir = self.session_dir / _DUMP_REL
        annotated = [
            ev
            for ev in timeline_events
            if ev.get("event_type") == "user_action"
            and ev.get("ai_macro_summary")
            and not str(ev.get("ai_macro_summary")).startswith(_ANALYSIS_ERROR_PREFIX)
        ]
        if not annotated:
            events.log_warn.send("recorder", text="[ANNOTATE] no successful clip summaries - skipping focus narrative")
            return

        timeline_lines = []
        telemetry_lines = []
        for ev in annotated:
            ts = ev.get("timestamp_iso") or ""
            elements = ", ".join(ev.get("ai_elements_interacted") or []) or "n/a"
            timeline_lines.append(f"- [{ts}] {ev['ai_macro_summary']} (elements: {elements})")
            raw = _raw_action(ev)
            target = raw.get("text", raw.get("key", raw.get("value", "")))
            telemetry_lines.append(f"[{ts}] {raw.get('type')}" + (f" '{target}'" if target else ""))

        prompt = (
            "You are analyzing a recorded browser session to answer one specific question "
            "about what the user did and what they were trying to achieve.\n\n"
            f"### QUESTION ###\n{self.focus}\n\n"
            "### AI-ANNOTATED ACTION TIMELINE ###\n" + "\n".join(timeline_lines) + "\n\n"
            "### RAW INPUT TELEMETRY ###\n" + "\n".join(telemetry_lines) + "\n\n"
            "Write a concise narrative (max ~400 words): the user's overall aim and flow, "
            "the step-by-step path with timestamps, and a direct answer to the question. "
            "Point out where the flow succeeded, where it stalled or failed, and what the "
            "user's next likely intent was at the end."
        )

        answer = None
        for attempt in (1, 2):
            try:
                answer = _narrative_completion(self.model or "", prompt)
                if answer:
                    break
            except Exception as exc:
                events.log_warn.send("recorder", text=f"[ANNOTATE] narrative call failed (attempt {attempt}/2): {exc}")
        if not answer:
            events.log_warn.send("recorder", text="[ANNOTATE] focus narrative could not be generated")
            return

        narrative_path = dump_dir / "focused_analysis.md"
        backup_dir = self._backup_originals(dump_dir)
        if narrative_path.is_file():
            try:
                shutil.copy2(narrative_path, backup_dir / "focused_analysis_prev.md")
            except Exception:
                pass
        header = (
            f"# Focused analysis\n\n"
            f"- session: {self.session_id}\n"
            f"- question: {self.focus}\n"
            f"- model: {self.model}\n"
            f"- generated: {datetime.now().isoformat(timespec='seconds')}\n\n---\n\n"
        )
        narrative_path.write_text(header + answer + "\n", encoding="utf-8")
        with self._lock:
            self.narrative = answer
            self.narrative_path = narrative_path
        events.log_info.send("recorder", text="[ANNOTATE] focus narrative written to session_dump/focused_analysis.md")


class AnnotationJobRegistry:
    """Thread-safe id -> AnnotationJob map. One instance per server."""

    def __init__(self) -> None:
        self._jobs: dict[str, AnnotationJob] = {}
        self._lock = threading.Lock()

    def register(self, job: AnnotationJob) -> None:
        with self._lock:
            self._jobs[job.session_id] = job

    def get(self, session_id: str | None) -> AnnotationJob | None:
        if not session_id:
            return None
        with self._lock:
            return self._jobs.get(session_id)

    def latest(self) -> AnnotationJob | None:
        with self._lock:
            if not self._jobs:
                return None
            return max(self._jobs.values(), key=lambda j: j.started_at)

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()


_REGISTRY: AnnotationJobRegistry | None = None


def get_annotation_registry() -> AnnotationJobRegistry:
    """The process-wide annotation-job registry, created lazily."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = AnnotationJobRegistry()
    return _REGISTRY
