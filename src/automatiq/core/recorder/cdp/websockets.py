"""CDP WebSocket capture handlers — a mixin for BrowserAgent.

Each lifecycle event is streamed separately to ws_connections.jsonl.
``process_websocket_streams`` in ``../compile/websockets.py`` merges them at
compile time. This avoids race conditions where WebSocketCreated fires before
the handshake events have populated a shared stash.

CDP event ordering:
  WebSocketCreated
  -> WebSocketWillSendHandshakeRequest
  -> WebSocketHandshakeResponseReceived
  -> WebSocketFrameSent / WebSocketFrameReceived
  -> WebSocketClosed
"""

import json
import logging

from zendriver import cdp

from .. import events

logger = logging.getLogger(__name__)


def _missed_event_stub(timestamp) -> dict:
    """Fresh active_websockets stub for a connection whose WebSocketCreated event was missed."""
    return {
        "start_time": timestamp,
        "sequence": 1,
        "url": "",
    }


class _WebsocketHandlers:
    """WebSocket lifecycle/frame handlers sharing BrowserAgent state via `self`."""

    async def websocket_created_handler_for_tab(self, event: cdp.network.WebSocketCreated, session_id: str):
        try:
            rid = str(event.request_id)
            url = event.url

            if self.blocklist and self.blocklist.is_blocked_url(url):
                self.stats["ws_blocked_by_blocklist"] += 1
                return

            # Set active_websockets BEFORE file I/O so frames are captured even if write fails.
            # start_time will be updated by the handshake_request handler; if that event is
            # missed, the first frame's timestamp becomes the baseline (delta = 0ms).
            self.active_websockets[rid] = {
                "start_time": None,
                "sequence": 1,
                "url": url,
            }
            self.stats["ws_connections"] += 1

            record = {
                "event": "created",
                "request_id": rid,
                "url": url,
                "created_iso": self.ts_converter.current_iso8601(),
            }
            self._ws_connections_file.write(json.dumps(record) + "\n")
            self._ws_connections_file.flush()
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketCreated handler failed: {e}")
            events.log_traceback.send("recorder")

    async def websocket_handshake_request_handler_for_tab(
        self, event: cdp.network.WebSocketWillSendHandshakeRequest, session_id: str
    ):
        try:
            if event.wall_time:
                self.ts_converter.calibrate(event.timestamp, event.wall_time)

            rid = str(event.request_id)

            # Fallback: if WebSocketCreated was missed (Network.enable not retroactive),
            # create the active_websockets stub here so frames are still captured.
            if rid not in self.active_websockets:
                self.active_websockets[rid] = _missed_event_stub(event.timestamp)
                self.stats["ws_connections"] += 1
            else:
                # Update start_time on the active connection so frame deltas are correct
                self.active_websockets[rid]["start_time"] = event.timestamp

            record = {
                "event": "handshake_request",
                "request_id": rid,
                "request_headers": dict(event.request.headers),
                "start_time": event.timestamp,
                "created_iso": self.ts_converter.to_iso8601(event.timestamp),
            }
            self._ws_connections_file.write(json.dumps(record) + "\n")
            self._ws_connections_file.flush()
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketWillSendHandshakeRequest handler failed: {e}")
            events.log_traceback.send("recorder")

    async def websocket_handshake_response_handler_for_tab(
        self, event: cdp.network.WebSocketHandshakeResponseReceived, session_id: str
    ):
        try:
            rid = str(event.request_id)
            resp = event.response

            # Fallback: if both WebSocketCreated and handshake_request were missed,
            # create the active_websockets stub here so frames are still captured.
            if rid not in self.active_websockets:
                self.active_websockets[rid] = _missed_event_stub(event.timestamp)
                self.stats["ws_connections"] += 1

            record = {
                "event": "handshake_response",
                "request_id": rid,
                "response_headers": dict(resp.headers),
                "response_status": resp.status,
                "created_iso": self.ts_converter.to_iso8601(event.timestamp),
            }
            self._ws_connections_file.write(json.dumps(record) + "\n")
            self._ws_connections_file.flush()
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketHandshakeResponseReceived handler failed: {e}")
            events.log_traceback.send("recorder")

    async def _process_ws_frame(self, request_id, direction: str, timestamp: float, opcode: int, payload_data: str):
        rid = str(request_id)
        if rid not in self.active_websockets:
            self.stats["ws_frames_skipped"] += 1
            return

        ws_state = self.active_websockets[rid]

        # Fallback: if start_time was never set (missed handshake), use first frame as baseline
        if ws_state["start_time"] is None:
            ws_state["start_time"] = timestamp

        delta_ms = int((timestamp - ws_state["start_time"]) * 1000)
        seq = ws_state["sequence"]
        is_base64 = opcode == 2

        frame_record = {
            "request_id": rid,
            "seq": seq,
            "direction": direction,
            "delta_ms": delta_ms,
            "opcode": opcode,
            "payload": payload_data,
            "is_base64": is_base64,
        }
        self._ws_frames_file.write(json.dumps(frame_record) + "\n")
        self._ws_frames_file.flush()

        ws_state["sequence"] += 1

        if direction == "client":
            self.stats["ws_frames_sent"] += 1
        else:
            self.stats["ws_frames_received"] += 1

    async def websocket_frame_sent_handler_for_tab(self, event: cdp.network.WebSocketFrameSent, session_id: str):
        try:
            await self._process_ws_frame(
                event.request_id, "client", event.timestamp, event.response.opcode, event.response.payload_data
            )
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketFrameSent handler failed: {e}")
            events.log_traceback.send("recorder")

    async def websocket_frame_received_handler_for_tab(self, event: cdp.network.WebSocketFrameReceived, session_id: str):
        try:
            await self._process_ws_frame(
                event.request_id, "server", event.timestamp, event.response.opcode, event.response.payload_data
            )
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketFrameReceived handler failed: {e}")
            events.log_traceback.send("recorder")

    async def websocket_closed_handler_for_tab(self, event: cdp.network.WebSocketClosed, session_id: str):
        try:
            rid = str(event.request_id)

            # Don't pop from active_websockets here — late-arriving frames after close
            # are still captured with a valid sequence number. _cleanup_and_build_report
            # clears active_websockets at session end.
            record = {
                "event": "closed",
                "request_id": rid,
                "closed_iso": self.ts_converter.to_iso8601(event.timestamp),
            }
            self._ws_connections_file.write(json.dumps(record) + "\n")
            self._ws_connections_file.flush()
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketClosed handler failed: {e}")
            events.log_traceback.send("recorder")
