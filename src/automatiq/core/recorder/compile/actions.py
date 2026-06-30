"""Action clustering + per-cluster video slicing and AI annotation."""

import logging
import os

from ... import config, events
from ...cancel_standard import StopRequestedException
from ..ai_analyzer import VideoActionAnalyzer
from ..video_recorder import ActionVideoRecorder

logger = logging.getLogger(__name__)


def merge_and_annotate_actions(
    actions: list[dict],
    full_video_path: str,
    video_start_unix: float,
    clips_dir: str,
    on_skip_requested: callable = None,
    cancel_token=None,
    stop_token=None,
) -> list[dict]:
    if not actions or not video_start_unix or not os.path.exists(full_video_path):
        return actions

    actions.sort(key=lambda x: x.get("timestamp_unix", 0))
    merged_clips = []
    current_cluster = []

    for action in actions:
        if not current_cluster:
            current_cluster.append(action)
        else:
            last_action_time = current_cluster[-1].get("timestamp_unix", 0)
            current_action_time = action.get("timestamp_unix", 0)

            if (current_action_time - last_action_time) <= config.MERGE_GAP_THRESHOLD_SECONDS:
                current_cluster.append(action)
            else:
                merged_clips.append(current_cluster)
                current_cluster = [action]

    if current_cluster:
        merged_clips.append(current_cluster)

    recorder = ActionVideoRecorder(fps=config.FPS)
    ai_analyzer = VideoActionAnalyzer()

    # Import CancelToken standard and cancellable runner from the parent package.
    from ...cancel_standard import CancelRequestedException, run_cancellable

    events.log_info.send("recorder", text=f"Extracting {len(merged_clips)} video action segments for AI...")
    for idx, cluster in enumerate(merged_clips):
        if stop_token and stop_token.is_stopped():
            events.log_error.send("recorder", text="Compilation completely aborted by user (Ctrl+C).")
            raise StopRequestedException("Compilation completely aborted by user.")

        if cancel_token and cancel_token.is_cancelled():
            remaining = len(merged_clips) - idx
            if on_skip_requested and on_skip_requested(remaining):
                events.log_warn.send("recorder", text=f"Skipping AI analysis for remaining {remaining} segment(s).")
                break
            events.log_debug.send("recorder", text="Continuing AI analysis...")

        first_action_time_relative = cluster[0]["timestamp_unix"] - video_start_unix
        clip_start = max(0, first_action_time_relative - config.SEGMENT_PAD_SECONDS)

        last_action_time_relative = cluster[-1]["timestamp_unix"] - video_start_unix
        clip_end = last_action_time_relative + config.SEGMENT_PAD_SECONDS

        clip_filename = f"action_clip_{idx:03d}.mp4"
        clip_path = os.path.join(clips_dir, clip_filename)

        clip_ok = recorder.split_video(full_video_path, clip_path, clip_start, clip_end)

        if clip_ok:
            try:
                ai_description = run_cancellable(
                    cancel_token,
                    ai_analyzer.analyze_clip,
                    clip_path,
                    clip_end - clip_start,
                    raw_actions=cluster,
                )
            except CancelRequestedException:
                remaining = len(merged_clips) - idx
                if on_skip_requested and on_skip_requested(remaining):
                    events.log_warn.send("recorder", text=f"Skipping AI analysis for remaining {remaining} segment(s).")
                    break
                events.log_debug.send("recorder", text="Continuing AI analysis...")
                continue
            events.log_info.send(
                "recorder", text=f"[AI] Segment {idx:03d} summary: {ai_description.get('macro_summary')}"
            )

            for action in cluster:
                action["ai_macro_summary"] = ai_description.get("macro_summary")
                action["ai_elements_interacted"] = ai_description.get("elements_interacted", [])
                action["ai_action_success"] = ai_description.get("action_success")
                action["ai_video_file"] = f"clips/{clip_filename}"
                action["video_start_sec"] = round(clip_start, 2)
                action["video_end_sec"] = round(clip_end, 2)
        else:
            events.log_warn.send(
                "recorder",
                text=f"Video split failed for segment {idx:03d} ({clip_start:.1f}s-{clip_end:.1f}s) "
                f"— skipping AI annotation for {len(cluster)} action(s)",
            )

    return actions
