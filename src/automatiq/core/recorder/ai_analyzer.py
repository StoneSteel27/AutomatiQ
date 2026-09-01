import base64
import json
import logging
import os
import subprocess

import imageio_ffmpeg
import litellm
from litellm.exceptions import InternalServerError
from pydantic import BaseModel, Field

from .. import config, events

logger = logging.getLogger(__name__)


class VideoActionAnalysis(BaseModel):
    macro_summary: str = Field(
        ..., description="A 1-2 sentence description of the human intent and action performed in this video sequence."
    )
    elements_interacted: list[str] = Field(
        default_factory=list,
        description="A list of specific UI elements interacted with (e.g., ['Username Input', 'Login Button']).",
    )
    action_success: bool = Field(
        ...,
        description=(
            "True if the action appeared to succeed based on the visual aftermath, "
            "False if an error or failure is visible."
        ),
    )


class VideoActionAnalyzer:
    """Extracts frames from video clips and analyzes them using Vision AI for structured JSON output."""

    SUBPROCESS_TIMEOUT = 60  # seconds — guard against hanging ffmpeg

    # Connection/auth-level errors that should NOT be retried (DNS, network
    # down, bad credentials, etc.) - retrying these can never succeed.
    _FATAL_EXC_TYPES = (
        litellm.APIConnectionError,
        litellm.NotFoundError,
        InternalServerError,
        litellm.AuthenticationError,
        litellm.PermissionDeniedError,
    )

    # The auth/permission members of _FATAL_EXC_TYPES: a fatal failure with
    # one of these means the key itself was rejected.
    _AUTH_EXC_TYPES = (
        litellm.AuthenticationError,
        litellm.PermissionDeniedError,
    )

    def __init__(self, model: str | None = None):
        # *model* overrides config.RECORDER_AI_MODEL: the runtime threads the
        # model resolved at session start through the vision state so the
        # analyzer calls exactly that model.
        self.model = model or config.RECORDER_AI_MODEL
        self.max_frames = config.MAX_FRAMES_PER_PROMPT
        self.history: list[str] = []
        self._ai_disabled: bool = False
        # Per-run annotation outcome, surfaced through the compile pipeline
        # into the terminal vision summary (mcp/vision.py::_vision_summary_block).
        self.clips_analyzed: int = 0
        self.clips_failed: int = 0
        self.fatal_reason: str | None = None  # "auth" | "other" when the breaker trips

    def _get_base64_frames(self, video_path: str, duration_sec: float, cancel_check=None) -> list[str]:
        """Extracts evenly spaced frames using lightweight native FFmpeg.

        *cancel_check*, when provided, is called between subprocess calls; if
        it returns True the extraction is aborted early.
        """
        if not os.path.exists(video_path):
            events.log_error.send("recorder", text=f"Video file not found: {video_path}")
            return []

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        try:
            step = duration_sec / self.max_frames
            timestamps = [max(0, min(duration_sec - 0.1, step * i + (step / 2))) for i in range(self.max_frames)]

            base64_frames = []
            for t in timestamps:
                if cancel_check and cancel_check():
                    return base64_frames

                extract_cmd = [
                    ffmpeg_exe,
                    "-ss",
                    str(t),
                    "-i",
                    video_path,
                    "-vframes",
                    "1",
                    "-vf",
                    "scale=1280:-1",
                    "-q:v",
                    "2",
                    "-f",
                    "image2",
                    "-c:v",
                    "mjpeg",
                    "pipe:1",
                ]

                frame_data = subprocess.run(
                    extract_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=self.SUBPROCESS_TIMEOUT,
                )

                if frame_data.stdout and len(frame_data.stdout) > 100:
                    img_b64 = base64.b64encode(frame_data.stdout).decode("utf-8")
                    base64_frames.append(f"data:image/jpeg;base64,{img_b64}")

            return base64_frames

        except subprocess.TimeoutExpired:
            events.log_error.send(
                "recorder", text=f"FFmpeg frame extraction timed out after {self.SUBPROCESS_TIMEOUT}s for {video_path}"
            )
            events.log_traceback.send("recorder")
            return []
        except Exception as e:
            events.log_error.send("recorder", text=f"FFmpeg frame extraction failed for {video_path}: {e}")
            events.log_traceback.send("recorder")
            return []

    @staticmethod
    def _extract_root_cause(exc: Exception) -> str:
        """Pull a human-readable one-liner from an exception."""
        cause = getattr(exc, "__cause__", None) or exc
        msg = str(cause)
        # Keep it to one line
        msg = msg.replace("\n", " ").strip()
        return msg if msg else str(exc)[:200]

    def _matches(self, exc: Exception, types: tuple[type[BaseException], ...]) -> bool:
        """True when *exc* or its ``__cause__`` chain contains one of *types*."""
        current: BaseException | None = exc
        while current is not None:
            if isinstance(current, types):
                return True
            current = current.__cause__
        return False

    def _is_fatal(self, exc: Exception) -> bool:
        """Return True for network-level errors that will never succeed on retry."""
        return self._matches(exc, self._FATAL_EXC_TYPES)

    def analyze_clip(
        self, video_path: str, duration_sec: float, raw_actions: list[dict] | None = None, cancel_check=None
    ) -> dict:
        """Analyzes the clip and guarantees a structured response.

        *cancel_check*, when provided, is a callable returning True when the
        user has requested cancellation (e.g. pressed Esc).  It is forwarded to
        frame extraction so we can bail out between ffmpeg calls.
        """

        error_resp = {
            "macro_summary": "Error: Could not analyze clip.",
            "elements_interacted": [],
            "action_success": False,
        }

        if self._ai_disabled:
            self.clips_failed += 1
            return error_resp

        base64_frames = self._get_base64_frames(video_path, duration_sec, cancel_check=cancel_check)
        if not base64_frames:
            error_resp["macro_summary"] = "Error: Could not extract frames."
            self.clips_failed += 1
            return error_resp

        context_prompt = "You are a QA testing AI analyzing a screen recording.\n\n"

        if self.history:
            context_prompt += "### PREVIOUS MACRO-ACTIONS IN THIS SESSION ###\n"
            for i, past_action in enumerate(self.history):
                context_prompt += f"{i + 1}. {past_action}\n"
            context_prompt += "\n"

        if raw_actions:
            action_summaries = [
                f"[{a['type']}] on '{a.get('text', a.get('key', a.get('value', 'element')))}'" for a in raw_actions
            ]
            context_prompt += (
                f"### SYSTEM TELEMETRY FOR CURRENT CLIP ###\nSystem detected: {', '.join(action_summaries)}\n\n"
            )

        content = [{"type": "text", "text": context_prompt}]
        for b64 in base64_frames:
            content.append({"type": "image_url", "image_url": {"url": b64}})

        events.log_debug.send("recorder", text=f"Prompting Vision AI with {len(base64_frames)} frames...")

        try:
            schema_json = json.dumps(VideoActionAnalysis.model_json_schema())
            content[0]["text"] += (
                "\n\nIMPORTANT: You must respond in pure JSON format. "
                f"The JSON must exactly match this schema: {schema_json}"
            )

            kwargs = dict(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            if config.API_BASE:
                kwargs["api_base"] = config.API_BASE
                kwargs["api_key"] = os.environ.get("OPENAI_API_KEY") or "not-required"

            raw_text = ""
            for attempt in range(1, 4):  # Max 3 attempts
                try:
                    response = litellm.completion(**kwargs)
                except Exception as req_exc:
                    # The API call itself failed: no response text exists yet.
                    if self._matches(req_exc, self._AUTH_EXC_TYPES):
                        raise  # auth is never retryable - no warn spam
                    if attempt < 3:
                        reason = self._extract_root_cause(req_exc)
                        events.log_warn.send(
                            "recorder",
                            text=f"AI request failed (attempt {attempt}/3): {reason}. Retrying...",
                        )
                        continue
                    raise

                raw_text = (getattr(response.choices[0].message, "content", None) or "").strip()

                if raw_text.startswith("```"):
                    lines = raw_text.splitlines()
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    raw_text = "\n".join(lines).strip()

                try:
                    analysis = VideoActionAnalysis.model_validate_json(raw_text)
                except Exception as ve:
                    if attempt < 3:
                        events.log_warn.send(
                            "recorder", text=f"AI response validation failed (Attempt {attempt}/3): {ve}. Retrying..."
                        )
                        kwargs["messages"].append({"role": "assistant", "content": raw_text})
                        kwargs["messages"].append(
                            {
                                "role": "user",
                                "content": f"Failed validation: {str(ve)}. Output valid JSON matching the schema.",
                            }
                        )
                        continue
                    raise

                self.history.append(analysis.macro_summary)
                self.clips_analyzed += 1
                return analysis.model_dump()

        except Exception as e:
            reason = self._extract_root_cause(e)
            self.clips_failed += 1

            if self._is_fatal(e):
                self._ai_disabled = True
                self.fatal_reason = "auth" if self._matches(e, self._AUTH_EXC_TYPES) else "other"
                events.log_error.send("recorder", text=f"LLM unreachable: {reason}")
                events.log_warn.send("recorder", text="Skipping AI analysis for remaining segments.")
            else:
                events.log_error.send("recorder", text=f"AI analysis failed: {reason}")

            events.log_traceback.send("recorder")
            return error_resp
