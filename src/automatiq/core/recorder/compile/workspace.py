"""Workspace compilation orchestrator — assembles the final session_dump layout."""

import json
import logging
import os
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse

from ... import config, events
from ...cancel_standard import StopRequestedException
from .actions import merge_and_annotate_actions
from .network import process_network_requests
from .serializers import MAGIKA_AVAILABLE, make_serializable, sanitize_filename
from .websockets import process_websocket_streams

logger = logging.getLogger(__name__)

# Serializes compilations: the pipeline touches shared singletons (Magika
# detector, ffmpeg binaries) and heavy disk I/O; concurrent sessions must
# not interleave compile phases.
_COMPILE_LOCK = threading.Lock()

# Fidelity is the contract: passwords, cookies, auth headers, and API keys
# are stored verbatim; nothing is ever redacted, so consumers must treat
# every session folder as a secret. The README template tells MCP clients
# the same thing.
_README_TEMPLATE = """\
# AutomatiQ session dump

Compiled output of one recorded browser session. Everything here is plain
files. This README is the entry point: it documents every artifact and its
schema so you can analyze the dump without guessing - start here, then
follow the pointers.

## Sensitive data

This is a full-fidelity recording: request/response bodies, cookies,
authorization headers, and anything typed during the session (including
passwords) are stored verbatim - nothing is redacted. Treat the whole
folder as a secret: do not commit, share, or paste it anywhere, and
delete it when you are done.

## How to analyze this dump

1. Read `SUMMARY.json` first - `session_flow` is the chronological
   narrative of what the user did - then `timeline.json` end-to-end.
2. Correlate: match `user_action` events to the `network_request` /
   `websocket_*` events they triggered, via timestamps.
3. Use the `folder` field on timeline events - it points directly at
   the transaction directory; don't scan.
4. Opaque/encrypted payloads: do NOT reverse the cipher by hand.
   Search the captured JS responses (in `requests/`) for the crypto
   code (`crypto.subtle`, `CryptoJS`, `AES`, `encrypt`, `decrypt`) and
   replicate that logic instead.
5. Never hardcode ephemeral values (tokens, cookies, session IDs) -
   find the request that mints them and reproduce that flow.

## Using the video (recommended)

The video is the ground truth of what the user actually did; the JSON
artifacts are the machine's account. Using both greatly increases
reverse-engineering accuracy and aim.

- `clips/action_clip_NNN.mp4` - one short clip per action cluster; the
  `ai_video_file` field on a `user_action` event points at its clip.
  Watch the clip before reproducing an action: it shows the exact
  element interacted with, the UI state, and whether the action
  succeeded - disambiguating failed selectors, overlays, and intent.
- `full_record.mp4` - the whole session; use for flow and context
  between actions.
- Agents with vision: prefer reading the clip over trusting selectors
  alone. Agents without vision: the `ai_*` fields on `user_action`
  events are distilled from this analysis - use them.

## Vision annotation

{vision_annotation}

When annotation did not run, this same line reports skipped or failed state;
when it ran, per-clip results live in the `ai_*` fields of `user_action` events.

## Layout

    .
    |- README.md                 this file
    |- session_metadata.json     compile status ("completed" when finished)
    '- workspace/session_dump/
       |- full_record.mp4        full-screen video of the whole session
       |- timeline.json          all events merged, sorted by timestamp
       |- SUMMARY.json           aggregate digest (statistics, session_flow)
       |- crash_report.txt       only if the browser crashed (still saved)
       |- clips/                 one short clip per action cluster (video AI)
       |  '- action_clip_000.mp4
       |- requests/              one folder per network transaction
       |  '- 000_GET_example.com/
       |     |- transaction.json request/response metadata + timing
       |     |- req_payload.ext  request body (when present)
       |     '- res_body.ext     response body (when captured)
       '- websockets/            one folder per websocket connection
          '- ws_example.com_<request_id>/
             |- transaction.json handshake metadata
              |- 00000_client_0ms.txt
              '- 00001_server_12ms.json

## timeline.json

JSON array sorted by unix `timestamp`. Compact shape:
`[{timestamp, timestamp_iso, event_type, ...}]`. Elements carry
`event_type`:

- `user_action` - user input: `action` (click/input/navigate/...),
  `details` (raw recorder fields), plus AI annotation when video AI
  ran: `ai_macro_summary`, `ai_elements_interacted`,
  `ai_action_success`, `ai_video_file` (relative path to the clip),
  `video_start_sec` / `video_end_sec` (clip bounds in full_record.mp4).
- `network_request` - one HTTP exchange: `method`, `url`, `status`,
  `redirected`, `redirected_to_url`; `folder` is relative to
  session_dump/ and points at `requests/<txn>/`.
- `websocket_created` / `websocket_closed` - `url`; `folder` points at
  `websockets/<conn>/`.

## SUMMARY.json

Aggregate digest. Compact shape: `{session, session_flow, statistics}`.

- `session` - recorder metadata: recording start/end, duration,
  request counts (total/completed/failed/incomplete), `total_actions`,
  `blocked_by_blocklist`, `body_capture_stats`, `websocket_stats`, and
  crash fields when the browser crashed.
- `session_flow` - chronological narrative of what the user did, one
  `{timestamp_iso, timestamp_unix, summary}` entry per action cluster;
  present only when video AI annotation ran.
- `statistics` - `total_requests`, `total_actions`, `methods`,
  `domains`, `status_codes`, `with_auth`, `with_cookies`,
  `content_detection` (Magika detection counts, or "Magika not
  available"), `websockets` (connections/frames/skipped, or null).

## requests/<txn>/transaction.json

Per-exchange metadata. Top-level keys: `metadata`, `request`, `response`.

    {
      "metadata": {index, unique_id, method, url, status, redirected,
        redirected_to_url, timing: {request_sent_unix,
        response_received_unix, loading_finished_unix, duration_ms},
        security: {has_authorization, has_proxy_authorization,
        has_challenge}},
      "request": {headers, cookies_sent, cookies_sent_detailed,
        content_detection, has_payload},
      "response": {headers, cookies_set, cookies_set_detailed,
        content_detection, has_body, mime_mismatch}
    }

`timing` values are unix timestamps (duration_ms in milliseconds).
Bodies are stored raw next to it as `req_payload.<ext>`/`res_body.<ext>`;
the extension reflects the Magika-detected content type (fallback `.bin`).

## websockets/<conn>/

One folder per websocket connection, named `ws_<domain>_<request_id>`.
`transaction.json` carries the handshake: `url`, `request_headers`,
`response_headers`, `response_status`, `created_iso`, `closed_iso`.

Frames are one file per frame, named `{seq:05d}_{direction}_{delta_ms}ms{_opcode_suffix}.{ext}`:

- `seq` - zero-padded 5-digit sequence number, per connection.
- `direction` - `client` (sent by the browser) or `server` (received from the server).
- `delta_ms` - ms since the previous frame on this connection;
  accumulate it (anchored at `created_iso`, frame 0) to reconstruct
  absolute times.
- opcode suffix - data frames (text/binary) carry none; control frames
  are tagged `_ping`/`_pong`/`_close`/`_continuation` (unknown:
  `_opcode<N>`).
- extension - Magika-sniffed from the payload (e.g. `.json`, `.txt`);
  fallback is `.txt` for text/close frames, `.bin` otherwise.
- encoding - text payloads are stored as raw UTF-8; binary payloads
  are stored as raw bytes (decoded from their wire base64).

## session_metadata.json

    {"status": "completed", "files_verified": <bool>, "original_metadata": {...}}

## Notes

- `CRASH_REPORT_NNN.txt` in session_dump/ marks network-level capture errors
  for the matching request (NNN = request index).
- Media and binary payloads are not converted; open them as bytes.
"""

# Default for the README's {vision_annotation} placeholder: sessions compiled
# without vision state (callers that do not pass one) report the skipped line.
# STATE ONLY - _write_readme prefixes it so the rendered line matches the
# builder's single-prefix format.
_VISION_LINE_DEFAULT = "skipped (no key - set recorder_api_key in ~/.automatiq/config.toml)"


def _vision_readme_line(vision_state: dict) -> str:
    """One-line vision-annotation state for the session README.

    Mirrors the terminal vision summary in runtime status(): enabled /
    skipped (no key) / skipped (video disabled) / failed (key rejected) /
    failed (aborted). The enabled line names the model the analysis used
    (resolved at session start). Never contains the api_key value.
    """
    if not vision_state.get("configured"):
        if vision_state.get("skip_reason") == "video_disabled":
            return "AI vision annotation: skipped (video disabled)"
        return f"AI vision annotation: {_VISION_LINE_DEFAULT}"
    reason = vision_state.get("fatal_reason")
    if reason == "auth":
        return "AI vision annotation: failed (key rejected)"
    if reason == "other":
        return "AI vision annotation: failed (aborted - see logs)"
    analyzed = int(vision_state.get("analyzed", 0))
    failed = int(vision_state.get("failed", 0))
    total = analyzed + failed
    model = vision_state.get("model") or config.RECORDER_AI_MODEL
    return f"AI vision annotation: enabled (model {model}, {analyzed}/{total} clips)"


def _write_readme(output_dir: str, vision_line: str | None = None) -> None:
    """Write the self-documenting README into the session folder.

    Best-effort: a failure here must never fail the compile. *vision_line*
    replaces the template's ``{vision_annotation}`` placeholder; when None
    the skipped default line is written.
    """
    try:
        default_line = f"AI vision annotation: {_VISION_LINE_DEFAULT}"
        text = _README_TEMPLATE.replace("{vision_annotation}", vision_line or default_line)
        with open(os.path.join(output_dir, "README.md"), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        events.log_warn.send("recorder", text=f"Could not write session README: {e}")


def verify_timeline_files(session_dump_dir: str, timeline_events: list[dict]) -> bool:
    """Verifies that all files referenced in the timeline events exist on disk."""
    missing_files = []

    for event in timeline_events:
        if event.get("event_type") in ("network_request", "websocket_created", "websocket_closed") and "folder" in event:
            # Only check that the core transaction file we created exists
            transaction_path = os.path.join(session_dump_dir, event["folder"], "transaction.json")
            if not os.path.exists(transaction_path):
                missing_files.append(transaction_path)

        elif event.get("event_type") == "user_action" and event.get("ai_video_file"):
            # Only check that the video clip we created exists
            clip_path = os.path.join(session_dump_dir, event["ai_video_file"])
            if not os.path.exists(clip_path):
                missing_files.append(clip_path)

    if missing_files:
        events.log_warn.send("recorder", text=f"Timeline verification failed. Missing files: {missing_files}")
        return False
    return True


def compile_workspace(
    session_name: str | None,
    temp_data_dir: str,
    full_video_path: str,
    video_start_unix: float,
    output_root: str | None = None,
    on_skip_requested: callable = None,
    cancel_token=None,
    stop_token=None,
    vision_state: dict | None = None,
) -> tuple[str | None, str | None, bool]:
    """Compile the recorded stream files into a session_dump workspace.

    Creates ``<output_root>/<session_name>/`` (falls back to
    ``config.OUTPUT_DIR`` when ``output_root`` is None) containing
    ``workspace/session_dump/`` plus a ``session_metadata.json`` sibling.

    *vision_state* (optional mutable dict) is filled with the AI analyzer's
    per-run outcome (``analyzed`` / ``failed`` / ``fatal_reason``) and drives
    the README's vision line; the caller seeds it with ``configured``.

    Returns ``(final_video_path, output_dir, success)``.
    """
    with _COMPILE_LOCK:
        return _compile_workspace_locked(
            session_name=session_name,
            temp_data_dir=temp_data_dir,
            full_video_path=full_video_path,
            video_start_unix=video_start_unix,
            output_root=output_root,
            on_skip_requested=on_skip_requested,
            cancel_token=cancel_token,
            stop_token=stop_token,
            vision_state=vision_state,
        )


def _compile_workspace_locked(
    session_name: str | None,
    temp_data_dir: str,
    full_video_path: str,
    video_start_unix: float,
    output_root: str | None = None,
    on_skip_requested: callable = None,
    cancel_token=None,
    stop_token=None,
    vision_state: dict | None = None,
) -> tuple[str | None, str | None, bool]:
    events.log_info.send("recorder", text="[RULE] Compiling Workspace")
    events.log_info.send("recorder", text="Extracting data, and analyzing video...")

    try:
        # Load metadata
        metadata_path = os.path.join(temp_data_dir, "metadata.json")
        metadata = {}
        if os.path.exists(metadata_path):
            with open(metadata_path, encoding="utf-8") as f:
                metadata = json.load(f)

        # Load actions
        actions = []
        actions_path = os.path.join(temp_data_dir, "actions.jsonl")
        if os.path.exists(actions_path):
            with open(actions_path, encoding="utf-8") as f:
                for line in f:
                    actions.append(json.loads(line))

        requests_path = os.path.join(temp_data_dir, "requests.jsonl")
        total_requests = metadata.get("total_requests", 0)

        timeline_events = []

        # If we used a temporary name, let's figure out a fallback based on domains
        fallback_session_name = "recording"
        if not session_name:
            domain_counts = {}
            for action in actions:
                url = action.get("newUrl") or action.get("url")
                if url:
                    domain = urlparse(url).netloc
                    if domain:
                        domain_counts[domain] = domain_counts.get(domain, 0) + 1

            if domain_counts:
                most_common = max(domain_counts, key=domain_counts.get)
                fallback_session_name = sanitize_filename(most_common)

        # Resolve the per-session output directory: <output_root>/<session_name>.
        # The name is caller-provided or derived from the most-recorded domain.
        # The CLI product's LLM session-naming + CWD rename + global config
        # repointing are intentionally NOT ported: compile stays deterministic,
        # never writes outside output_root, and never mutates config globals.
        root = Path(output_root) if output_root else config.OUTPUT_DIR
        session_dir_name = sanitize_filename(session_name or fallback_session_name)
        output_dir = str(root / session_dir_name)
        _base_output_dir = output_dir
        _idx = 1
        while os.path.exists(output_dir):
            output_dir = f"{_base_output_dir}_{_idx:02d}"
            _idx += 1

        workspace_dir = os.path.join(output_dir, "workspace")
        session_dump_dir = os.path.join(workspace_dir, "session_dump")
        clips_dir = os.path.join(session_dump_dir, "clips")
        requests_dir = os.path.join(session_dump_dir, "requests")

        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(session_dump_dir, exist_ok=True)
        os.makedirs(clips_dir, exist_ok=True)
        os.makedirs(requests_dir, exist_ok=True)

        with open(os.path.join(output_dir, "session_metadata.json"), "w") as f:
            json.dump(make_serializable({"status": "in_progress", "original_metadata": metadata}), f, indent=2)

        if actions:
            actions = merge_and_annotate_actions(
                actions,
                full_video_path,
                video_start_unix,
                clips_dir,
                on_skip_requested,
                cancel_token,
                stop_token,
                vision_state=vision_state,
            )
            for action in actions:
                timeline_events.append(
                    {
                        "timestamp": action.get("timestamp_unix", 0),
                        "timestamp_iso": action.get("timestamp_iso"),
                        "event_type": "user_action",
                        "action": action.get("type"),
                        "details": {
                            k: v
                            for k, v in action.items()
                            if k
                            not in [
                                "timestamp_unix",
                                "timestamp_iso",
                                "type",
                                "ai_macro_summary",
                                "ai_elements_interacted",
                                "ai_action_success",
                                "ai_video_file",
                                "video_start_sec",
                                "video_end_sec",
                            ]
                        },
                        "ai_macro_summary": action.get("ai_macro_summary"),
                        "ai_elements_interacted": action.get("ai_elements_interacted"),
                        "ai_action_success": action.get("ai_action_success"),
                        "ai_video_file": action.get("ai_video_file"),
                        "video_start_sec": action.get("video_start_sec"),
                        "video_end_sec": action.get("video_end_sec"),
                    }
                )

        detection_stats = {}
        network_stats = {"methods": {}, "domains": {}, "status_codes": {}, "with_auth": 0, "with_cookies": 0}
        if os.path.exists(requests_path):
            events.log_info.send(
                "recorder", text=f"Extracting {total_requests} network requests and building transactions..."
            )
            network_events, detection_stats, network_stats = process_network_requests(
                requests_path, temp_data_dir, requests_dir, session_dump_dir
            )
            timeline_events.extend(network_events)

        # Process WebSocket streams
        ws_connections_path = os.path.join(temp_data_dir, "ws_connections.jsonl")
        ws_frames_path = os.path.join(temp_data_dir, "ws_frames.jsonl")
        ws_stats = {}
        if os.path.exists(ws_connections_path):
            events.log_info.send("recorder", text="Extracting WebSocket connections and frames...")
            ws_output_dir = os.path.join(session_dump_dir, "websockets")
            os.makedirs(ws_output_dir, exist_ok=True)
            ws_timeline_events, ws_stats = process_websocket_streams(ws_connections_path, ws_frames_path, ws_output_dir)
            timeline_events.extend(ws_timeline_events)

        timeline_events.sort(key=lambda x: x["timestamp"])
        with open(os.path.join(session_dump_dir, "timeline.json"), "w") as f:
            json.dump(make_serializable(timeline_events), f, indent=2)

        session_flow = []
        seen_summaries = set()
        for action in actions:
            text = action.get("ai_macro_summary")
            if text and text not in seen_summaries:
                seen_summaries.add(text)
                session_flow.append(
                    {
                        "timestamp_iso": action.get("timestamp_iso"),
                        "timestamp_unix": action.get("timestamp_unix"),
                        "summary": text,
                    }
                )

        summary = {
            "session": metadata,
            "session_flow": session_flow,
            "statistics": {
                "total_requests": total_requests,
                "total_actions": len(actions),
                "methods": network_stats["methods"],
                "domains": network_stats["domains"],
                "status_codes": network_stats["status_codes"],
                "with_auth": network_stats["with_auth"],
                "with_cookies": network_stats["with_cookies"],
                "content_detection": detection_stats if MAGIKA_AVAILABLE else "Magika not available",
                "websockets": ws_stats if ws_stats else None,
            },
        }

        with open(os.path.join(session_dump_dir, "SUMMARY.json"), "w") as f:
            json.dump(make_serializable(summary), f, indent=2)

        # Move the video file into the output directory before verifying
        final_video_path = os.path.join(session_dump_dir, "full_record.mp4")
        if os.path.exists(full_video_path):
            shutil.move(full_video_path, final_video_path)

        # Verify files referenced in timeline exist
        files_verified = verify_timeline_files(session_dump_dir, timeline_events)

        # Update and finalize metadata
        with open(os.path.join(output_dir, "session_metadata.json"), "w") as f:
            final_meta = {"status": "completed", "files_verified": files_verified, "original_metadata": metadata}
            json.dump(make_serializable(final_meta), f, indent=2)

        # Self-documenting output: the folder's README is the entry point
        # for MCP clients reading the dump (structure + file schemas). The
        # vision line reflects the same state as the runtime status block.
        _write_readme(output_dir, vision_line=_vision_readme_line(vision_state or {}))

        # Final session directory was decided up front (no post-hoc rename,
        # no global config repointing — the MCP runtime owns output paths).
        final_output_dir = output_dir

        # Cleanup temp_data_dir
        try:
            shutil.rmtree(temp_data_dir)
        except Exception as e:
            events.log_warn.send("recorder", text=f"Could not clean up temporary data directory {temp_data_dir}: {e}")

        # --- Crash report handling ---
        if metadata.get("session_crashed"):
            crash_timestamp = metadata.get("crash_timestamp", "unknown")
            crash_error = metadata.get("crash_error", "unknown")
            try:
                crash_report = os.path.join(session_dump_dir, "crash_report.txt")
                with open(crash_report, "w", encoding="utf-8") as f:
                    f.write(
                        "AutomatiQ recorder crash report\n"
                        "===============================\n\n"
                        f"Timestamp: {crash_timestamp}\n"
                        f"Error: {crash_error}\n\n"
                        "The recording was still saved. A few actions and requests\n"
                        "may have been lost due to the abrupt termination.\n"
                    )
            except Exception as e:
                events.log_warn.send("recorder", text=f"Could not write crash report: {e}")
            events.log_warn.send(
                "recorder",
                text=f"Session crashed at {crash_timestamp}: {crash_error} — recording was still saved.",
            )

        events.log_info.send("recorder", text=f"[SUCCESS] Workspace compiled successfully at {final_output_dir}")

        return final_video_path, final_output_dir, True

    except StopRequestedException as e:
        events.log_error.send("recorder", text=str(e))
        return None, None, False
    except Exception as e:
        events.log_error.send("recorder", text=f"Workspace compilation failed: {e}")
        events.log_traceback.send("recorder")
        return None, None, False
