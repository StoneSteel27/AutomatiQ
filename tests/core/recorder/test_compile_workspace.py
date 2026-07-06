"""End-to-end tests for compile_workspace — the full compile pipeline.

Real CDP events are fed to real handler functions on a real BrowserAgent (no
browser). The agent's _cleanup_and_build_report closes files and writes
metadata.json. Then compile_workspace assembles the full session_dump tree.

AI annotation and video slicing are bypassed (video_start_unix=0 causes
merge_and_annotate_actions to early-return), so no LLM or ffmpeg is needed.
"""

import asyncio
import json
import os
from unittest.mock import MagicMock

from automatiq.cli import console as cli_console
from automatiq.core.recorder.compile.workspace import compile_workspace, verify_timeline_files

from .conftest import (
    make_action_payload,
    make_data_received_event,
    make_loading_finished_event,
    make_request_event,
    make_response_event,
    make_ws_closed_event,
    make_ws_created_event,
    make_ws_frame_received_event,
    make_ws_frame_sent_event,
    make_ws_handshake_request_event,
    make_ws_handshake_response_event,
)

SESSION_ID = "test_session"


def feed_full_session(agent):
    """Feed a complete multi-protocol session through the real handlers."""

    async def feed():
        # HTTP request
        await agent.request_handler_for_tab(make_request_event(), SESSION_ID)
        await agent.data_received_handler_for_tab(make_data_received_event(), SESSION_ID)
        await agent.response_handler_for_tab(make_response_event(), SESSION_ID)
        await agent.loading_finished_handler_for_tab(make_loading_finished_event(), SESSION_ID)

        # WebSocket connection
        await agent.websocket_created_handler_for_tab(make_ws_created_event(), SESSION_ID)
        await agent.websocket_handshake_request_handler_for_tab(make_ws_handshake_request_event(), SESSION_ID)
        await agent.websocket_handshake_response_handler_for_tab(make_ws_handshake_response_event(), SESSION_ID)
        await agent.websocket_frame_sent_handler_for_tab(make_ws_frame_sent_event(), SESSION_ID)
        await agent.websocket_frame_received_handler_for_tab(make_ws_frame_received_event(), SESSION_ID)
        await agent.websocket_closed_handler_for_tab(make_ws_closed_event(), SESSION_ID)

        # User action
        agent._process_action(make_action_payload("click", text="Submit"))

    asyncio.run(feed())


class TestCompileWorkspaceFullPipeline:
    def test_full_multi_protocol_session(self, agent, workspace_config):
        """HTTP + WebSocket + action → complete session_dump tree with all artifacts."""
        feed_full_session(agent)
        temp_data_dir = asyncio.run(agent._cleanup_and_build_report())

        video_path, success = compile_workspace(
            session_name="test",
            temp_data_dir=temp_data_dir,
            full_video_path=os.path.join(temp_data_dir, "nonexistent.mp4"),
            video_start_unix=0,
        )

        assert success is True

        session_dump = workspace_config["session_dump_dir"]

        # ── Directory structure ────────────────────────────────────────────
        assert os.path.isdir(session_dump)
        assert os.path.isdir(os.path.join(session_dump, "clips"))
        assert os.path.isdir(os.path.join(session_dump, "requests"))
        assert os.path.isdir(os.path.join(session_dump, "websockets"))

        # ── timeline.json ──────────────────────────────────────────────────
        with open(os.path.join(session_dump, "timeline.json")) as f:
            timeline = json.load(f)

        event_types = [e["event_type"] for e in timeline]
        assert "user_action" in event_types
        assert "network_request" in event_types
        assert "websocket_created" in event_types
        assert "websocket_closed" in event_types

        # Timeline is sorted by timestamp
        timestamps = [e["timestamp"] for e in timeline]
        assert timestamps == sorted(timestamps)

        # ── SUMMARY.json ───────────────────────────────────────────────────
        with open(os.path.join(session_dump, "SUMMARY.json")) as f:
            summary = json.load(f)

        assert summary["statistics"]["total_requests"] == 1
        assert summary["statistics"]["total_actions"] == 1
        assert summary["statistics"]["methods"]["GET"] == 1
        assert summary["statistics"]["domains"]["example.com"] == 1
        assert summary["statistics"]["status_codes"]["200"] == 1
        assert summary["statistics"]["websockets"] is not None
        assert summary["statistics"]["websockets"]["connections"] == 1
        assert summary["statistics"]["websockets"]["frames"] == 2

        # ── session_metadata.json ──────────────────────────────────────────
        with open(os.path.join(workspace_config["output_dir"], "session_metadata.json")) as f:
            metadata = json.load(f)

        assert metadata["status"] == "completed"
        assert metadata["files_verified"] is True

        # ── Transaction folders exist ──────────────────────────────────────
        request_folders = os.listdir(os.path.join(session_dump, "requests"))
        assert len(request_folders) == 1
        assert os.path.exists(os.path.join(session_dump, "requests", request_folders[0], "transaction.json"))

        ws_folders = os.listdir(os.path.join(session_dump, "websockets"))
        assert len(ws_folders) == 1
        assert os.path.exists(os.path.join(session_dump, "websockets", ws_folders[0], "transaction.json"))

        # ── temp_data_dir cleaned up ───────────────────────────────────────
        assert not os.path.exists(temp_data_dir)

    def test_actions_only_session(self, agent, workspace_config):
        """Session with only user actions (no requests, no WS) produces a valid workspace."""

        agent._process_action(make_action_payload("click", text="Login"))
        agent._process_action(make_action_payload("keypress", key="Enter"))

        temp_data_dir = asyncio.run(agent._cleanup_and_build_report())

        _, success = compile_workspace(
            session_name="test",
            temp_data_dir=temp_data_dir,
            full_video_path="nonexistent.mp4",
            video_start_unix=0,
        )

        assert success is True

        session_dump = workspace_config["session_dump_dir"]

        with open(os.path.join(session_dump, "timeline.json")) as f:
            timeline = json.load(f)

        # Only user_action events
        event_types = [e["event_type"] for e in timeline]
        assert all(et == "user_action" for et in event_types)
        assert len(timeline) == 2

        with open(os.path.join(session_dump, "SUMMARY.json")) as f:
            summary = json.load(f)

        assert summary["statistics"]["total_requests"] == 0
        assert summary["statistics"]["total_actions"] == 2
        assert summary["statistics"]["websockets"]["connections"] == 0

        # requests/ dir exists but is empty
        request_folders = os.listdir(os.path.join(session_dump, "requests"))
        assert len(request_folders) == 0

        # websockets/ dir exists (ws_connections.jsonl is always created by agent)
        # but should contain no connection folders
        ws_folders = os.listdir(os.path.join(session_dump, "websockets"))
        assert len(ws_folders) == 0

    def test_requests_only_no_ws(self, agent, workspace_config):
        """Session with HTTP requests but no WebSocket connections."""

        async def feed():
            await agent.request_handler_for_tab(make_request_event(), SESSION_ID)
            await agent.response_handler_for_tab(make_response_event(), SESSION_ID)
            await agent.loading_finished_handler_for_tab(make_loading_finished_event(), SESSION_ID)

        asyncio.run(feed())
        temp_data_dir = asyncio.run(agent._cleanup_and_build_report())

        _, success = compile_workspace(
            session_name="test",
            temp_data_dir=temp_data_dir,
            full_video_path="nonexistent.mp4",
            video_start_unix=0,
        )

        assert success is True

        session_dump = workspace_config["session_dump_dir"]

        with open(os.path.join(session_dump, "timeline.json")) as f:
            timeline = json.load(f)

        event_types = [e["event_type"] for e in timeline]
        assert "network_request" in event_types
        assert "websocket_created" not in event_types

        with open(os.path.join(session_dump, "SUMMARY.json")) as f:
            summary = json.load(f)

        assert summary["statistics"]["total_requests"] == 1
        assert summary["statistics"]["websockets"]["connections"] == 0


class TestCompileWorkspaceCrashBranch:
    def test_crash_metadata_triggers_save_crash_report(self, agent, workspace_config, monkeypatch):
        """When metadata has session_crashed=True, save_crash_report is called."""

        # Set crash state on the agent BEFORE cleanup
        agent.session_crashed = True
        agent.crash_timestamp = "2024-01-01T00:00:00.000+00:00"
        agent.crash_error = "Test crash error"

        # Stub CLI console functions
        save_calls = []

        def mock_save_crash_report(crash_timestamp=None, crash_error=None):
            save_calls.append((crash_timestamp, crash_error))

        mock_console = MagicMock()
        monkeypatch.setattr(cli_console, "save_crash_report", mock_save_crash_report)
        monkeypatch.setattr(cli_console, "console", mock_console)
        monkeypatch.setattr(cli_console, "rename_file_logger", lambda name: None)

        async def feed():
            await agent.request_handler_for_tab(make_request_event(), SESSION_ID)
            await agent.response_handler_for_tab(make_response_event(), SESSION_ID)
            await agent.loading_finished_handler_for_tab(make_loading_finished_event(), SESSION_ID)

        asyncio.run(feed())
        temp_data_dir = asyncio.run(agent._cleanup_and_build_report())

        # Verify metadata has crash info
        with open(os.path.join(temp_data_dir, "metadata.json")) as f:
            metadata = json.load(f)
        assert metadata["session_crashed"] is True
        assert metadata["crash_error"] == "Test crash error"

        _, success = compile_workspace(
            session_name="test",
            temp_data_dir=temp_data_dir,
            full_video_path="nonexistent.mp4",
            video_start_unix=0,
        )

        assert success is True
        assert len(save_calls) == 1
        assert save_calls[0] == ("2024-01-01T00:00:00.000+00:00", "Test crash error")
        assert mock_console.print.called


class TestVerifyTimelineFiles:
    def test_all_files_present(self, tmp_path):
        """verify_timeline_files returns True when all referenced files exist."""
        session_dump = str(tmp_path)
        os.makedirs(os.path.join(session_dump, "requests", "000_GET_example.com"), exist_ok=True)
        open(os.path.join(session_dump, "requests", "000_GET_example.com", "transaction.json"), "w").close()

        timeline = [
            {"event_type": "network_request", "folder": "requests/000_GET_example.com"},
        ]

        assert verify_timeline_files(session_dump, timeline) is True

    def test_missing_transaction_file(self, tmp_path):
        """verify_timeline_files returns False when transaction.json is missing."""
        session_dump = str(tmp_path)
        os.makedirs(os.path.join(session_dump, "requests", "000_GET_example.com"), exist_ok=True)
        # Don't create transaction.json

        timeline = [
            {"event_type": "network_request", "folder": "requests/000_GET_example.com"},
        ]

        assert verify_timeline_files(session_dump, timeline) is False

    def test_missing_clip_file(self, tmp_path):
        """verify_timeline_files returns False when ai_video_file is missing."""
        session_dump = str(tmp_path)

        timeline = [
            {"event_type": "user_action", "ai_video_file": "clips/action_clip_000.mp4"},
        ]

        assert verify_timeline_files(session_dump, timeline) is False

    def test_action_without_video_passes(self, tmp_path):
        """user_action events without ai_video_file don't trigger file checks."""
        session_dump = str(tmp_path)

        timeline = [
            {"event_type": "user_action"},
            {"event_type": "user_action", "ai_video_file": None},
        ]

        assert verify_timeline_files(session_dump, timeline) is True

    def test_empty_timeline(self, tmp_path):
        """Empty timeline trivially passes verification."""
        assert verify_timeline_files(str(tmp_path), []) is True
