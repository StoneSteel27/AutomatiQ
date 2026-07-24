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

    # Connection-level errors that should NOT be retried (DNS, network down, etc.)
    _FATAL_EXC_TYPES = (litellm.APIConnectionError, litellm.NotFoundError, InternalServerError)

    def __init__(self):
        self.model = config.RECORDER_AI_MODEL
        self.max_frames = config.MAX_FRAMES_PER_PROMPT
        self.history: list[str] = []
        self._ai_disabled: bool = False

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

    def _is_fatal(self, exc: Exception) -> bool:
        """Return True for network-level errors that will never succeed on retry."""
        # Check the exception itself and its __cause__ chain
        current: BaseException | None = exc
        while current is not None:
            if isinstance(current, self._FATAL_EXC_TYPES):
                return True
            current = current.__cause__
        return False

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
            return error_resp

        base64_frames = self._get_base64_frames(video_path, duration_sec, cancel_check=cancel_check)
        if not base64_frames:
            error_resp["macro_summary"] = "Error: Could not extract frames."
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

            for attempt in range(1, 4):  # Max 3 attempts
                try:
                    response = litellm.completion(**kwargs)
                    raw_text = (getattr(response.choices[0].message, "content", None) or "").strip()

                    if raw_text.startswith("```"):
                        lines = raw_text.splitlines()
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].startswith("```"):
                            lines = lines[:-1]
                        raw_text = "\n".join(lines).strip()

                    analysis = VideoActionAnalysis.model_validate_json(raw_text)

                    self.history.append(analysis.macro_summary)
                    return analysis.model_dump()
                except Exception as ve:
                    if attempt < 3:
                        events.log_warn.send(
                            "recorder", text=f"AI response validation failed (Attempt {attempt}/3): {ve}. Retrying..."
                        )
                        kwargs["messages"].append(
                            {"role": "assistant", "content": raw_text if "raw_text" in locals() else ""}
                        )
                        kwargs["messages"].append(
                            {
                                "role": "user",
                                "content": f"Failed validation: {str(ve)}. Output valid JSON matching the schema.",
                            }
                        )
                    else:
                        raise ve

        except Exception as e:
            reason = self._extract_root_cause(e)

            if self._is_fatal(e):
                self._ai_disabled = True
                events.log_error.send("recorder", text=f"LLM unreachable: {reason}")
                events.log_warn.send("recorder", text="Skipping AI analysis for remaining segments.")
            else:
                events.log_error.send("recorder", text=f"AI analysis failed: {reason}")

            events.log_traceback.send("recorder")
            return error_resp

    def generate_session_name(self, session_flow: list[dict], fallback_name: str) -> str:
        """Generates a concise folder name based on the recorded session flow summaries."""
        if self._ai_disabled or not session_flow:
            return fallback_name

        try:
            summaries = [item["summary"] for item in session_flow if "summary" in item]
            if not summaries:
                return fallback_name

            prompt = (
                "You are an AI assistant. I will provide a list of actions a user performed in a web browser.\n"
                "Your task is to generate a short, descriptive folder name (max 3-4 words, using hyphens instead "
                "of spaces, lowercased) that represents the overall goal or outcome of this session.\n"
                "Do NOT include words like 'session', 'recording', 'test', 'video', or 'clip'.\n\n"
                "Examples:\n"
                "- login-to-github\n"
                "- create-new-repo\n"
                "- purchase-shoes-amazon\n\n"
                "Here are the actions:\n" + "\n".join(f"- {s}" for s in summaries)
            )

            kwargs = dict(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
            )
            if config.API_BASE:
                kwargs["api_base"] = config.API_BASE
                kwargs["api_key"] = os.environ.get("OPENAI_API_KEY") or "not-required"

            for attempt in range(1, 4):
                try:
                    response = litellm.completion(**kwargs)

                    # Robust extraction of content
                    content = None
                    if hasattr(response, "choices") and len(response.choices) > 0:
                        content = getattr(response.choices[0].message, "content", None)

                    if not isinstance(content, str) or not content.strip():
                        if attempt < 3:
                            events.log_warn.send(
                                "recorder", text=f"Empty AI response for session name (Attempt {attempt}/3). Retrying..."
                            )
                            continue
                        events.log_warn.send(
                            "recorder", text="Empty or non-string response from AI for session name, using fallback."
                        )
                        return fallback_name

                    raw_text = content.strip().lower()

                    import re

                    clean_name = re.sub(r"[^\w\-]", "-", raw_text)
                    clean_name = re.sub(r"-+", "-", clean_name).strip("-")
                    return clean_name[:50] or fallback_name
                except Exception as e:
                    if self._is_fatal(e):
                        events.log_warn.send(
                            "recorder", text=f"Fatal LLM error during session naming: {self._extract_root_cause(e)}"
                        )
                        return fallback_name
                    if attempt < 3:
                        events.log_warn.send(
                            "recorder", text=f"AI session naming failed (Attempt {attempt}/3): {e}. Retrying..."
                        )
                        continue
                    events.log_warn.send("recorder", text=f"Could not generate AI session name, using fallback: {e}")
                    events.log_traceback.send("recorder")
                    return fallback_name

            return fallback_name
        except Exception as e:
            events.log_warn.send("recorder", text=f"Unexpected error in AI session naming: {e}")
            events.log_traceback.send("recorder")
            return fallback_name
