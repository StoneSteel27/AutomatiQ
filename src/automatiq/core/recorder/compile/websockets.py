"""WebSocket stream compilation — merges lifecycle events and writes per-frame files."""

import json
import logging
import os
from datetime import datetime
from urllib.parse import urlparse

from ... import events
from .serializers import (
    detect_content_type,
    get_header_val,
    make_serializable,
    sanitize_filename,
    save_content,
)

logger = logging.getLogger(__name__)


def _new_connection_state(url: str = "", created_iso: str | None = None) -> dict:
    """Fresh per-connection state dict (never share one between request ids)."""
    return {
        "url": url,
        "request_headers": {},
        "response_headers": {},
        "response_status": None,
        "created_iso": created_iso,
        "closed_iso": None,
        "folder_name": None,
    }


def _parse_ts(value) -> float:
    """ISO-8601 string -> unix timestamp seconds; 0.0 when absent/unparseable."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return 0.0


# Opcode suffix mapping for WebSocket frame filenames.
# Data frames (1=text, 2=binary) get no suffix; all others are tagged.
_OPCODE_SUFFIXES = {
    0: "continuation",
    8: "close",
    9: "ping",
    10: "pong",
}


def _opcode_suffix(opcode: int) -> str:
    """Returns the filename suffix for a given opcode, or empty string for data frames."""
    if opcode in (1, 2):
        return ""
    if opcode in _OPCODE_SUFFIXES:
        return f"_{_OPCODE_SUFFIXES[opcode]}"
    return f"_opcode{opcode}"


def _opcode_fallback_ext(opcode: int) -> str:
    """Returns the fallback file extension when Magika is unavailable or fails."""
    if opcode in (1, 8):
        return "txt"
    return "bin"


def process_websocket_streams(connections_file: str, frames_file: str, ws_output_dir: str) -> tuple[list[dict], dict]:
    """Processes WebSocket stream files into per-connection folders with Magika-tagged frame files.

    Returns (timeline_events, ws_stats).
    """
    timeline_events = []
    ws_stats = {"connections": 0, "frames": 0, "skipped": 0}

    if not os.path.exists(connections_file):
        return timeline_events, ws_stats

    # Phase 1: Read connection lifecycle events and merge into per-connection state.
    # Each lifecycle event (created, handshake_request, handshake_response, closed) is
    # streamed separately by the CDP websocket handlers. We merge them here by request_id.
    connections = {}  # request_id -> {url, request_headers, response_headers, ...}

    with open(connections_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                rid = record.get("request_id")
                if not rid:
                    continue
                event_type = record.get("event")

                if event_type == "created":
                    connections[rid] = _new_connection_state(
                        url=record.get("url", ""), created_iso=record.get("created_iso")
                    )
                elif event_type == "handshake_request":
                    if rid not in connections:
                        connections[rid] = _new_connection_state()
                    connections[rid]["request_headers"] = record.get("request_headers", {})
                    if not connections[rid]["created_iso"]:
                        connections[rid]["created_iso"] = record.get("created_iso")
                elif event_type == "handshake_response":
                    if rid not in connections:
                        connections[rid] = _new_connection_state()
                    connections[rid]["response_headers"] = record.get("response_headers", {})
                    connections[rid]["response_status"] = record.get("response_status")
                    if not connections[rid]["created_iso"]:
                        connections[rid]["created_iso"] = record.get("created_iso")
                elif event_type == "closed":
                    if rid in connections:
                        connections[rid]["closed_iso"] = record.get("closed_iso")
            except Exception as e:
                events.log_error.send("recorder", text=f"Failed to parse WS connection record: {e}")
                events.log_traceback.send("recorder")

    # Reconstruct URL from Host header for connections that missed WebSocketCreated.
    # Browsers block ws:// on HTTPS pages (mixed content), so wss:// is the safe default.
    for conn in connections.values():
        if not conn["url"] and conn["request_headers"]:
            host = get_header_val(conn["request_headers"], "host")
            if host:
                conn["url"] = f"wss://{host}"

    # Create folders and transaction.json for each connection
    folder_paths = {}  # request_id -> ws_root path
    for rid, conn in connections.items():
        parsed_url = urlparse(conn["url"])
        domain = parsed_url.netloc or "unknown"
        folder_name = f"ws_{sanitize_filename(domain)}_{rid}"
        ws_root = os.path.join(ws_output_dir, folder_name)
        os.makedirs(ws_root, exist_ok=True)
        conn["folder_name"] = folder_name
        folder_paths[rid] = ws_root
        ws_stats["connections"] += 1

        transaction_data = {
            "url": conn["url"],
            "request_headers": conn["request_headers"],
            "response_headers": conn["response_headers"],
            "response_status": conn["response_status"],
            "created_iso": conn["created_iso"],
            "closed_iso": conn["closed_iso"],
        }
        with open(os.path.join(ws_root, "transaction.json"), "w") as f:
            json.dump(make_serializable(transaction_data), f, indent=2)

        # Add websocket_created to timeline
        created_unix = _parse_ts(conn["created_iso"])

        timeline_events.append(
            {
                "timestamp": created_unix or 0,
                "timestamp_iso": conn["created_iso"],
                "event_type": "websocket_created",
                "url": conn["url"],
                "folder": f"websockets/{folder_name}",
            }
        )

        # Add websocket_closed to timeline if we have a close event
        if conn["closed_iso"]:
            closed_unix = _parse_ts(conn["closed_iso"])

            timeline_events.append(
                {
                    "timestamp": closed_unix or 0,
                    "timestamp_iso": conn["closed_iso"],
                    "event_type": "websocket_closed",
                    "url": conn["url"],
                    "folder": f"websockets/{folder_name}",
                }
            )

    # Phase 2: Read frame events and write each frame to disk with Magika-tagged extension
    if os.path.exists(frames_file):
        with open(frames_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                    rid = frame.get("request_id")
                    if rid not in folder_paths:
                        ws_stats["skipped"] += 1
                        continue

                    ws_root = folder_paths[rid]
                    seq = frame.get("seq", 0)
                    direction = frame.get("direction", "unknown")
                    delta_ms = frame.get("delta_ms", 0)
                    opcode = frame.get("opcode", 1)
                    payload = frame.get("payload", "")
                    is_base64 = frame.get("is_base64", False)

                    # Run Magika universally (text and binary)
                    detection = detect_content_type(payload, is_base64=is_base64)
                    if detection and detection.get("extension"):
                        ext = detection["extension"]
                    else:
                        ext = _opcode_fallback_ext(opcode)

                    suffix = _opcode_suffix(opcode)
                    filename = f"{seq:05d}_{direction}_{delta_ms}ms{suffix}.{ext}"
                    filepath = os.path.join(ws_root, filename)
                    save_content(filepath, payload, is_base64=is_base64)
                    ws_stats["frames"] += 1

                except Exception as e:
                    events.log_error.send("recorder", text=f"Failed to process WS frame: {e}")
                    events.log_traceback.send("recorder")

    return timeline_events, ws_stats
