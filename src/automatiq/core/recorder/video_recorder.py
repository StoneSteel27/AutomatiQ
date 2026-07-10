import logging
import os
import subprocess
import sys
import threading
import time

import imageio_ffmpeg
import mss
import numpy as np

from .. import events

logger = logging.getLogger(__name__)

FFMPEG_TIMEOUT = 120  # seconds — guard against hanging FFmpeg slice operations


class ActionVideoRecorder:
    """Handles background screen recording and precise FFmpeg video slicing."""

    def __init__(self, fps: int = 10, output_path: str = "full_record.mp4"):
        self.fps = fps
        self.output_path = output_path
        self.is_recording = False
        self.video_start_unix: float | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        """Starts the screen recording in a background thread."""
        if self.is_recording:
            events.log_warn.send("recorder", text="Recording is already active.")
            return

        output_dir = os.path.dirname(self.output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        self.is_recording = True
        self.thread = threading.Thread(target=self._record_loop, daemon=True)
        self.thread.start()

        while self.video_start_unix is None and self.is_recording:
            time.sleep(0.01)

    def _record_loop(self) -> None:
        """The core recording loop executed by the background thread."""
        events.log_debug.send("recorder", text=f"Initializing video writer at {self.fps} FPS...")
        writer = None

        try:
            with mss.mss() as sct:
                if not sct.monitors or len(sct.monitors) < 2:
                    events.log_error.send("recorder", text="No monitors detected — cannot record screen.")
                    return

                monitor = sct.monitors[1]
                width, height = monitor["width"], monitor["height"]

                writer = imageio_ffmpeg.write_frames(
                    self.output_path,
                    size=(width, height),
                    fps=self.fps,
                    pix_fmt_in="bgra",
                    pix_fmt_out="yuv420p",
                    codec="libx264",
                )

                writer.send(None)

                frame_duration = 1.0 / self.fps

                # CRITICAL: Record the exact Unix timestamp immediately before the first frame
                self.video_start_unix = time.time()
                events.log_info.send("recorder", text=f"[VIDEO] Recording started at UNIX {self.video_start_unix}")

                while self.is_recording:
                    loop_start = time.time()

                    screenshot = sct.grab(monitor)
                    writer.send(np.array(screenshot).tobytes())

                    elapsed = time.time() - loop_start
                    sleep_time = frame_duration - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)

        except mss.exception.ScreenShotError as e:
            if sys.platform == "darwin" and "CGWindowListCreateImage" in str(e):
                events.log_error.send(
                    "recorder",
                    text="Screen recording permission denied — AutomatiQ needs access to capture video.\n\n"
                    "To fix this:\n"
                    "  1. Open System Settings > Privacy & Security > Screen & System Audio Recording\n"
                    "  2. Enable the toggle for your terminal app (Terminal, iTerm2, VS Code, etc.)\n"
                    "  3. Restart your terminal and run AutomatiQ again\n\n"
                    "If the toggle is already enabled, try toggling it off and back on — macOS "
                    "sometimes caches a stale permission grant.",
                )
            else:
                events.log_error.send("recorder", text=f"Screen capture failed: {e}")
            events.log_traceback.send("recorder")
        except Exception as e:
            events.log_error.send("recorder", text=f"Video recording thread failed: {e}")
            events.log_traceback.send("recorder")
        finally:
            if writer is not None:
                try:
                    writer.close()
                    events.log_info.send("recorder", text=f"[VIDEO] Recording finalized and saved to {self.output_path}")
                except Exception as e:
                    events.log_error.send("recorder", text=f"Failed to close video writer: {e}")
                    events.log_traceback.send("recorder")

    def stop(self) -> float | None:
        """Stops the recording thread and returns the exact start timestamp for alignment."""
        if not self.is_recording:
            return self.video_start_unix

        events.log_info.send("recorder", text="Halting video recording...")
        self.is_recording = False

        if self.thread:
            self.thread.join()

        return self.video_start_unix

    def split_video(self, input_file: str, output_file: str, start_time_sec: float, end_time_sec: float) -> bool:
        """
        Slices a video using FFmpeg.
        Re-encodes the tiny chunk to ensure exact timestamps and prevent 1KB empty files.
        """
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

            duration = end_time_sec - start_time_sec

            cmd = [
                ffmpeg_exe,
                "-y",
                # Put -ss BEFORE -i for fast, accurate seeking
                "-ss",
                str(start_time_sec),
                "-i",
                input_file,
                "-t",
                str(duration),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                output_file,
            ]

            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=FFMPEG_TIMEOUT,
            )

            if os.path.exists(output_file) and os.path.getsize(output_file) > 5000:
                return True
            else:
                events.log_error.send("recorder", text=f"FFmpeg produced an empty or invalid clip for {output_file}.")
                events.log_error.send("recorder", text=f"FFmpeg Error Log: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            events.log_error.send(
                "recorder", text=f"FFmpeg video split timed out after {FFMPEG_TIMEOUT}s for {output_file}"
            )
            events.log_traceback.send("recorder")
            return False
        except Exception as e:
            events.log_error.send("recorder", text=f"Unexpected error during video split: {e}")
            events.log_traceback.send("recorder")
            return False
