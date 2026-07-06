"""Tests for CDP handler functions — the first line of processing.

Synthetic CDP events (real zendriver dataclasses) are fed to the REAL handler
functions on a BrowserAgent with no browser launched. The handlers write JSONL
to disk exactly as they would during a live recording. We read it back and assert
correctness.

Browser-dependent code paths (get_response_body, stream_resource_content) are
skipped because self.tabs is empty — every such path is ``if tab_session:``-guarded.
Body capture works via the streaming fallback path (data_received → bodies/{rid}.bin).
"""

import asyncio
import json
import os

from zendriver.cdp.network import Headers, ResourceType, Response
from zendriver.cdp.security import SecurityState

from .conftest import (
    make_action_payload,
    make_associated_cookie,
    make_cookie,
    make_data_received_event,
    make_loading_failed_event,
    make_loading_finished_event,
    make_req_extra_info_event,
    make_request_event,
    make_res_extra_info_event,
    make_response_event,
    make_ws_closed_event,
    make_ws_created_event,
    make_ws_frame_received_event,
    make_ws_frame_sent_event,
    make_ws_handshake_request_event,
    make_ws_handshake_response_event,
    read_jsonl,
)

SESSION_ID = "test_session"


# ── HTTP handler tests ────────────────────────────────────────────────────────


class TestHttpRequestHandlers:
    def test_full_request_lifecycle(self, agent):
        """RequestWillBeSent → DataReceived → ResponseReceived → LoadingFinished
        produces a complete record in requests.jsonl with body_file from stream."""

        async def feed():
            await agent.request_handler_for_tab(make_request_event(), SESSION_ID)
            await agent.data_received_handler_for_tab(make_data_received_event(), SESSION_ID)
            await agent.response_handler_for_tab(make_response_event(), SESSION_ID)
            await agent.loading_finished_handler_for_tab(make_loading_finished_event(), SESSION_ID)

        asyncio.run(feed())

        requests = read_jsonl(os.path.join(agent._data_dir.name, "requests.jsonl"))
        assert len(requests) == 1
        req = requests[0]

        assert req["url"] == "https://example.com/api/data"
        assert req["method"] == "GET"
        assert req["headers"]["Accept"] == "application/json"
        assert req["request_state"] == "finished"
        assert req["request_id"] == "REQ_001"
        assert "unique_id" in req and req["unique_id"].startswith("REQ_001_")

        # Response data
        assert req["response_data"] is not None
        assert req["response_data"]["status"] == 200
        assert req["response_data"]["mime_type"] == "application/json"
        assert req["response_data"]["body_file"] == "bodies/REQ_001.bin"

        # Response timing
        assert "received_unix" in req["response_timing"]
        assert "finished_unix" in req["response_timing"]
        assert "duration_ms" in req["response_timing"]

        # Stats
        assert agent.stats["total_requests"] == 1
        assert agent.stats["completed"] == 1
        assert agent.stats["body_from_stream"] == 1

        # Body file exists on disk
        body_path = os.path.join(agent._data_dir.name, req["response_data"]["body_file"])
        assert os.path.exists(body_path)
        assert os.path.getsize(body_path) > 0

    def test_loading_failed_path(self, agent):
        """LoadingFailed writes the request with state=failed and error_text."""

        async def feed():
            await agent.request_handler_for_tab(make_request_event(), SESSION_ID)
            await agent.loading_failed_handler_for_tab(
                make_loading_failed_event(error_text="net::ERR_CONNECTION_REFUSED"), SESSION_ID
            )

        asyncio.run(feed())

        requests = read_jsonl(os.path.join(agent._data_dir.name, "requests.jsonl"))
        assert len(requests) == 1
        req = requests[0]

        assert req["request_state"] == "failed"
        assert req["error_text"] == "net::ERR_CONNECTION_REFUSED"
        assert agent.stats["failed"] == 1

    def test_extra_info_populates_cookies(self, agent):
        """req_extra_info and res_extra_info populate cookie details and merge headers."""

        async def feed():
            await agent.request_handler_for_tab(make_request_event(), SESSION_ID)
            await agent.req_extra_info_for_tab(
                make_req_extra_info_event(
                    request_id="REQ_001",
                    associated_cookies=[make_associated_cookie(cookie=make_cookie(name="sid"))],
                    headers={"X-Custom-Header": "custom-value"},
                ),
                SESSION_ID,
            )
            await agent.response_handler_for_tab(make_response_event(), SESSION_ID)
            await agent.res_extra_info_for_tab(
                make_res_extra_info_event(
                    request_id="REQ_001",
                    headers={"X-Response-Extra": "yes"},
                ),
                SESSION_ID,
            )
            await agent.loading_finished_handler_for_tab(make_loading_finished_event(), SESSION_ID)

        asyncio.run(feed())

        requests = read_jsonl(os.path.join(agent._data_dir.name, "requests.jsonl"))
        assert len(requests) == 1
        req = requests[0]

        # Cookies sent details
        assert len(req["cookies_sent_details"]) == 1
        assert req["cookies_sent_details"][0]["cookie"]["name"] == "sid"

        # Cookies received details
        assert "blocked" in req["cookies_received_details"]
        assert "exempted" in req["cookies_received_details"]

        # Merged headers from extra info
        assert req["response_data"]["headers"]["X-Response-Extra"] == "yes"

    def test_redirect_response_populated(self, agent):
        """RequestWillBeSent with redirect_response on existing request updates the old
        request and creates a new one in active_map."""

        redirect_resp = Response(
            url="https://example.com/old",
            status=301,
            status_text="Moved Permanently",
            headers=Headers({"Location": "https://example.com/new"}),
            mime_type="text/plain",
            charset="utf-8",
            connection_reused=False,
            connection_id=1.0,
            encoded_data_length=50.0,
            security_state=SecurityState.SECURE,
        )

        async def feed():
            await agent.request_handler_for_tab(
                make_request_event(request_id="REQ_001", url="https://example.com/old"), SESSION_ID
            )
            await agent.request_handler_for_tab(
                make_request_event(
                    request_id="REQ_001",
                    url="https://example.com/new",
                    redirect_response=redirect_resp,
                ),
                SESSION_ID,
            )

        asyncio.run(feed())

        # New request should be in active_map with the new URL
        assert "REQ_001" in agent.active_map
        assert agent.active_map["REQ_001"]["url"] == "https://example.com/new"
        assert agent.active_map["REQ_001"]["request_state"] == "pending"

    def test_blocklist_skips_blocked_url(self, agent):
        """Blocked URLs are not added to active_map and increment blocklist stats."""

        class StubBlocklist:
            def is_blocked_url(self, url):
                return "blocked-domain.com" in url

        agent.blocklist = StubBlocklist()

        event = make_request_event(url="https://blocked-domain.com/api")
        asyncio.run(agent.request_handler_for_tab(event, SESSION_ID))

        assert "REQ_001" not in agent.active_map
        assert agent.stats["blocked_by_blocklist"] == 1

    def test_data_url_skipped(self, agent):
        """data: URLs are not tracked in active_map."""

        event = make_request_event(url="data:text/html,<h1>hello</h1>")
        asyncio.run(agent.request_handler_for_tab(event, SESSION_ID))

        assert "REQ_001" not in agent.active_map

    def test_post_data_captured(self, agent):
        """Request with post_data stores it in the request object."""

        async def feed():
            await agent.request_handler_for_tab(
                make_request_event(method="POST", post_data='{"key": "value"}'), SESSION_ID
            )
            await agent.response_handler_for_tab(make_response_event(), SESSION_ID)
            await agent.loading_finished_handler_for_tab(make_loading_finished_event(), SESSION_ID)

        asyncio.run(feed())

        requests = read_jsonl(os.path.join(agent._data_dir.name, "requests.jsonl"))
        assert len(requests) == 1
        assert requests[0]["method"] == "POST"
        assert requests[0]["post_data"] == '{"key": "value"}'

    def test_stats_incremented_per_resource_type(self, agent):
        """total_requests is incremented only for tracked resource types."""

        async def feed():
            await agent.request_handler_for_tab(
                make_request_event(resource_type=ResourceType.XHR),
                SESSION_ID,
            )

        asyncio.run(feed())
        assert agent.stats["total_requests"] == 1


# ── WebSocket handler tests ───────────────────────────────────────────────────


class TestWebsocketHandlers:
    def test_full_ws_lifecycle(self, agent):
        """Created → HandshakeRequest → HandshakeResponse → FrameSent → FrameReceived → Closed
        produces 4 connection records and 2 frame records."""

        async def feed():
            await agent.websocket_created_handler_for_tab(make_ws_created_event(), SESSION_ID)
            await agent.websocket_handshake_request_handler_for_tab(make_ws_handshake_request_event(), SESSION_ID)
            await agent.websocket_handshake_response_handler_for_tab(make_ws_handshake_response_event(), SESSION_ID)
            await agent.websocket_frame_sent_handler_for_tab(make_ws_frame_sent_event(), SESSION_ID)
            await agent.websocket_frame_received_handler_for_tab(make_ws_frame_received_event(), SESSION_ID)
            await agent.websocket_closed_handler_for_tab(make_ws_closed_event(), SESSION_ID)

        asyncio.run(feed())

        connections = read_jsonl(os.path.join(agent._data_dir.name, "ws_connections.jsonl"))
        frames = read_jsonl(os.path.join(agent._data_dir.name, "ws_frames.jsonl"))

        # 4 connection lifecycle records
        assert len(connections) == 4
        events = [c["event"] for c in connections]
        assert events == ["created", "handshake_request", "handshake_response", "closed"]

        # Created record
        created = connections[0]
        assert created["request_id"] == "WS_001"
        assert created["url"] == "wss://example.com/socket"
        assert "created_iso" in created

        # Handshake request record
        hs_req = connections[1]
        assert hs_req["request_headers"]["Host"] == "example.com"

        # Handshake response record
        hs_res = connections[2]
        assert hs_res["response_status"] == 101

        # Closed record
        closed = connections[3]
        assert "closed_iso" in closed

        # 2 frame records
        assert len(frames) == 2

        sent_frame = frames[0]
        assert sent_frame["request_id"] == "WS_001"
        assert sent_frame["direction"] == "client"
        assert sent_frame["seq"] == 1
        assert sent_frame["opcode"] == 1.0
        assert sent_frame["payload"] == "hello"
        assert sent_frame["is_base64"] is False

        recv_frame = frames[1]
        assert recv_frame["direction"] == "server"
        assert recv_frame["seq"] == 2
        assert recv_frame["payload"] == "world"

        # Stats
        assert agent.stats["ws_connections"] == 1
        assert agent.stats["ws_frames_sent"] == 1
        assert agent.stats["ws_frames_received"] == 1

    def test_missed_created_fallback(self, agent):
        """If WebSocketCreated is missed, handshake_request creates a fallback stub
        so frames are still captured."""

        async def feed():
            await agent.websocket_handshake_request_handler_for_tab(make_ws_handshake_request_event(), SESSION_ID)
            await agent.websocket_frame_received_handler_for_tab(make_ws_frame_received_event(), SESSION_ID)

        asyncio.run(feed())

        # Fallback stub created
        assert "WS_001" in agent.active_websockets
        assert agent.stats["ws_connections"] == 1

        # Frame was captured
        frames = read_jsonl(os.path.join(agent._data_dir.name, "ws_frames.jsonl"))
        assert len(frames) == 1
        assert frames[0]["payload"] == "world"

    def test_unknown_rid_frame_skipped(self, agent):
        """Frames for unknown request_id are skipped and increment ws_frames_skipped."""

        event = make_ws_frame_received_event(request_id="UNKNOWN")
        asyncio.run(agent.websocket_frame_received_handler_for_tab(event, SESSION_ID))

        assert agent.stats["ws_frames_skipped"] == 1
        # No frames written
        frames_path = os.path.join(agent._data_dir.name, "ws_frames.jsonl")
        if os.path.exists(frames_path):
            assert len(read_jsonl(frames_path)) == 0

    def test_binary_frame_is_base64(self, agent):
        """Binary frames (opcode=2) have is_base64=True in the frame record."""

        async def feed():
            await agent.websocket_created_handler_for_tab(make_ws_created_event(), SESSION_ID)
            await agent.websocket_frame_received_handler_for_tab(
                make_ws_frame_received_event(opcode=2.0, payload_data="aGVsbG8="), SESSION_ID
            )

        asyncio.run(feed())

        frames = read_jsonl(os.path.join(agent._data_dir.name, "ws_frames.jsonl"))
        assert len(frames) == 1
        assert frames[0]["is_base64"] is True
        assert frames[0]["opcode"] == 2.0

    def test_ws_blocklist_skips_url(self, agent):
        """Blocked WS URLs are not tracked and increment ws_blocked_by_blocklist."""

        class StubBlocklist:
            def is_blocked_url(self, url):
                return "blocked-ws.com" in url

        agent.blocklist = StubBlocklist()

        event = make_ws_created_event(url="wss://blocked-ws.com/socket")
        asyncio.run(agent.websocket_created_handler_for_tab(event, SESSION_ID))

        assert "WS_001" not in agent.active_websockets
        assert agent.stats["ws_blocked_by_blocklist"] == 1


# ── Action handler tests ──────────────────────────────────────────────────────


class TestActionProcessing:
    def test_click_action_written_to_jsonl(self, agent):
        """Action payload from the extension writes to actions.jsonl."""

        agent._process_action(make_action_payload("click", text="Submit", url="https://example.com"))

        actions = read_jsonl(os.path.join(agent._data_dir.name, "actions.jsonl"))
        assert len(actions) == 1
        action = actions[0]
        assert action["type"] == "click"
        assert action["text"] == "Submit"
        assert "timestamp_iso" in action
        assert "timestamp_unix" in action
        assert agent._actions_count == 1

    def test_script_loaded_dropped(self, agent):
        """script_loaded actions are NOT written to actions.jsonl."""

        agent._process_action(make_action_payload("script_loaded"))

        actions_path = os.path.join(agent._data_dir.name, "actions.jsonl")
        if os.path.exists(actions_path):
            assert len(read_jsonl(actions_path)) == 0
        assert agent._actions_count == 0

    def test_keypress_action_written(self, agent):
        """Keypress payload writes to actions.jsonl."""

        agent._process_action(make_action_payload("keypress", key="Enter"))

        actions = read_jsonl(os.path.join(agent._data_dir.name, "actions.jsonl"))
        assert len(actions) == 1
        assert actions[0]["type"] == "keypress"
        assert actions[0]["key"] == "Enter"

    def test_iframe_action_written(self, agent):
        """Actions with is_iframe=True are still written (not dropped)."""

        agent._process_action(make_action_payload("click", text="Pay", is_iframe=True))

        actions = read_jsonl(os.path.join(agent._data_dir.name, "actions.jsonl"))
        assert len(actions) == 1
        assert actions[0]["is_iframe"] is True


# ── Cleanup test ──────────────────────────────────────────────────────────────


class TestCleanupAndReport:
    def test_metadata_written(self, agent):
        """_cleanup_and_build_report writes metadata.json with correct stats."""

        async def feed():
            await agent.request_handler_for_tab(make_request_event(), SESSION_ID)
            await agent.response_handler_for_tab(make_response_event(), SESSION_ID)
            await agent.loading_finished_handler_for_tab(make_loading_finished_event(), SESSION_ID)

        asyncio.run(feed())
        data_dir = asyncio.run(agent._cleanup_and_build_report())

        metadata_path = os.path.join(data_dir, "metadata.json")
        assert os.path.exists(metadata_path)

        with open(metadata_path) as f:
            metadata = json.load(f)

        assert metadata["total_requests"] == 1
        assert metadata["completed_requests"] == 1
        assert metadata["session_crashed"] is False
        assert metadata["crash_timestamp"] is None
        assert "websocket_stats" in metadata
        assert "body_capture_stats" in metadata

        # File handles should be closed
        assert agent._requests_file.closed
        assert agent._actions_file.closed
        assert agent._ws_connections_file.closed
        assert agent._ws_frames_file.closed

    def test_incomplete_requests_flushed(self, agent):
        """Pending requests in active_map are written to requests.jsonl as incomplete."""

        async def feed():
            await agent.request_handler_for_tab(make_request_event(), SESSION_ID)
            # Don't send loading_finished — request stays pending

        asyncio.run(feed())
        assert len(agent.active_map) == 1

        data_dir = asyncio.run(agent._cleanup_and_build_report())

        requests = read_jsonl(os.path.join(data_dir, "requests.jsonl"))
        assert len(requests) == 1
        assert requests[0]["request_state"] == "incomplete"

    def test_synthetic_ws_closed_events(self, agent):
        """_cleanup_and_build_report writes synthetic closed events for un-closed WS."""

        async def feed():
            await agent.websocket_created_handler_for_tab(make_ws_created_event(), SESSION_ID)
            # Don't send closed event — connection stays open

        asyncio.run(feed())
        assert len(agent.active_websockets) == 1

        data_dir = asyncio.run(agent._cleanup_and_build_report())

        connections = read_jsonl(os.path.join(data_dir, "ws_connections.jsonl"))
        # Should have created + synthetic closed
        events = [c["event"] for c in connections]
        assert "created" in events
        assert "closed" in events
