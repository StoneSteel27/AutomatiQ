"""Smoke tests for the MCP tool surface via the in-memory fastmcp Client.

Everything runs through asyncio.run() (no pytest-asyncio). The lifespan does
create the real registry under ~/.automatiq (dirs only, no sessions are ever
started), and no tool call reaches reg.create(): the url/proxy guards in
start_recording return before registry access, which the empty-registry
assertions at the end of the surface test verify.

Test order matters: test_server_surface() must run before
test_no_heavy_imports() so the lazy-import assertion observes the process
state AFTER the full tool surface has been exercised. pytest runs tests in a
file in definition order; do not reorder.
"""

import asyncio
import json
import sys

import pytest
from fastmcp import Client

from automatiq.core import config
from automatiq.mcp import server

_TOOL_NAMES = {"start_recording", "stop_recording", "get_status", "wait_for_completion", "annotate_user_interactions"}
_HEAVY_MODULES = ("zendriver", "mss", "litellm", "magika", "imageio_ffmpeg")


@pytest.fixture(autouse=True)
def _no_telemetry_dispatch(monkeypatch):
    """The lifespan now starts the telemetry client (telemetry v2). Disable
    dispatch here so the real network endpoint is never hit from unit tests;
    the emission wiring itself is asserted in test_telemetry_v2.py."""
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", False)


def test_server_surface():
    async def run():
        async with Client(server.app) as client:
            # Tool surface: exactly five tools; read-only hints where declared.
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == _TOOL_NAMES
            hints = {tool.name: (tool.annotations.readOnlyHint if tool.annotations else None) for tool in tools}
            assert hints["get_status"] is True
            assert hints["wait_for_completion"] is True

            # No resources or resource templates are exposed.
            assert await client.list_resources() == []
            assert await client.list_resource_templates() == []

            # Empty-registry listing: is_error False, count 0.
            res = await client.call_tool("get_status", {})
            assert res.is_error is False
            assert res.structured_content is not None
            assert res.structured_content["count"] == 0
            # Dual-format contract: the pretty text mirrors the structured payload.
            assert json.loads(res.content[0].text) == res.structured_content

            # Unknown session id -> tool-level error result.
            res = await client.call_tool("get_status", {"session_id": "nope"}, raise_on_error=False)
            assert res.is_error is True

            # No sessions at all -> wait_for_completion errors immediately.
            res = await client.call_tool("wait_for_completion", {}, raise_on_error=False)
            assert res.is_error is True

            # annotate_user_interactions: unknown session errors before any job starts.
            res = await client.call_tool("annotate_user_interactions", {"session_id": "nope"}, raise_on_error=False)
            assert res.is_error is True

            # Invalid arguments are rejected before any session is created.
            res = await client.call_tool("start_recording", {"url": ""}, raise_on_error=False)
            assert res.is_error is True
            res = await client.call_tool(
                "start_recording", {"url": "https://example.com", "proxy": "not-a-url"}, raise_on_error=False
            )
            assert res.is_error is True

            # The url/proxy guards precede reg.create() -> registry still empty.
            res = await client.call_tool("get_status", {})
            assert res.is_error is False
            assert res.structured_content["count"] == 0

    asyncio.run(run())


def test_no_heavy_imports():
    # Depends on test_server_surface() having run first (see module docstring):
    # after a full Client session, none of the deferred heavy modules may have
    # been imported.
    leaked = [name for name in _HEAVY_MODULES if name in sys.modules]
    assert leaked == [], f"heavy modules leaked into sys.modules: {leaked}"
