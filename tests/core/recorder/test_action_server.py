"""Tests for the loopback ActionServer — the transport that replaced the CDP binding.

The extension's content scripts POST telemetry actions to http://127.0.0.1:<port>/act
via navigator.sendBeacon. These tests exercise the real HTTP server with real
HTTP requests (stdlib http.client) and assert that actions land in actions.jsonl
with Python-stamped timestamps, that script_loaded is dropped, and that CORS
preflight is handled.
"""

import http.client
import json
import os

from automatiq.core.recorder.action_server import ActionServer

from .conftest import read_jsonl


def _post(agent_port, path, body: bytes | None, content_type: str = "text/plain"):
    conn = http.client.HTTPConnection("127.0.0.1", agent_port, timeout=5)
    headers = {"Content-Type": content_type, "Content-Length": str(len(body or b""))}
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _options(agent_port, path):
    conn = http.client.HTTPConnection("127.0.0.1", agent_port, timeout=5)
    conn.request("OPTIONS", path)
    resp = conn.getresponse()
    resp.read()
    headers = dict(resp.getheaders())
    conn.close()
    return resp.status, headers


class TestActionServer:
    def test_post_action_written_to_jsonl(self, agent):
        server = ActionServer(on_action=agent._process_action)
        port = server.start()
        try:
            status, _ = _post(port, "/act", json.dumps({"type": "click", "text": "Submit"}).encode())
            assert status == 204
        finally:
            server.stop()

        actions = read_jsonl(os.path.join(agent._data_dir.name, "actions.jsonl"))
        assert len(actions) == 1
        action = actions[0]
        assert action["type"] == "click"
        assert action["text"] == "Submit"
        assert "timestamp_iso" in action
        assert "timestamp_unix" in action
        assert agent._actions_count == 1

    def test_script_loaded_dropped(self, agent):
        server = ActionServer(on_action=agent._process_action)
        port = server.start()
        try:
            _post(port, "/act", json.dumps({"type": "script_loaded"}).encode())
        finally:
            server.stop()

        actions_path = os.path.join(agent._data_dir.name, "actions.jsonl")
        if os.path.exists(actions_path):
            assert len(read_jsonl(actions_path)) == 0
        assert agent._actions_count == 0

    def test_keypress_written(self, agent):
        server = ActionServer(on_action=agent._process_action)
        port = server.start()
        try:
            _post(port, "/act", json.dumps({"type": "keypress", "key": "Enter"}).encode())
        finally:
            server.stop()

        actions = read_jsonl(os.path.join(agent._data_dir.name, "actions.jsonl"))
        assert len(actions) == 1
        assert actions[0]["key"] == "Enter"

    def test_options_preflight_returns_cors(self, agent):
        server = ActionServer(on_action=agent._process_action)
        port = server.start()
        try:
            status, headers = _options(port, "/act")
        finally:
            server.stop()

        assert status == 204
        assert headers["Access-Control-Allow-Origin"] == "*"
        assert "POST" in headers["Access-Control-Allow-Methods"]

    def test_post_returns_cors_headers(self, agent):
        server = ActionServer(on_action=agent._process_action)
        port = server.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/act", body=json.dumps({"type": "click"}).encode())
            resp = conn.getresponse()
            headers = dict(resp.getheaders())
            resp.read()
            conn.close()
        finally:
            server.stop()

        assert headers["Access-Control-Allow-Origin"] == "*"

    def test_wrong_path_returns_404(self, agent):
        server = ActionServer(on_action=agent._process_action)
        port = server.start()
        try:
            status, _ = _post(port, "/other", b'{"type":"click"}')
        finally:
            server.stop()

        assert status == 404
        assert agent._actions_count == 0

    def test_invalid_json_returns_400(self, agent):
        server = ActionServer(on_action=agent._process_action)
        port = server.start()
        try:
            status, _ = _post(port, "/act", b"not-json")
        finally:
            server.stop()

        assert status == 400
        assert agent._actions_count == 0

    def test_non_object_payload_returns_400(self, agent):
        server = ActionServer(on_action=agent._process_action)
        port = server.start()
        try:
            status, _ = _post(port, "/act", b"[1,2,3]")
        finally:
            server.stop()

        assert status == 400
        assert agent._actions_count == 0

    def test_endpoint_property(self, agent):
        server = ActionServer(on_action=agent._process_action)
        assert server.port is None
        port = server.start()
        try:
            assert server.port == port
            assert server.endpoint == f"http://127.0.0.1:{port}/act"
        finally:
            server.stop()
        assert server.port is None

    def test_start_idempotent(self, agent):
        server = ActionServer(on_action=agent._process_action)
        try:
            p1 = server.start()
            p2 = server.start()
            assert p1 == p2
        finally:
            server.stop()
