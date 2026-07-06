"""Shared fixtures and CDP event factory functions for recorder tests.

The event factories build *real* zendriver CDP dataclasses with sensible defaults
so tests only specify the fields they care about. Everything downstream — handler
serialization, file I/O, compile phase — is real and asserted against.
"""

import base64
import json
import os

import pytest
from zendriver.cdp.network import (
    AssociatedCookie,
    ConnectTiming,
    Cookie,
    CookiePriority,
    CookieSourceScheme,
    DataReceived,
    Headers,
    Initiator,
    IPAddressSpace,
    LoaderId,
    LoadingFailed,
    LoadingFinished,
    MonotonicTime,
    Request,
    RequestId,
    RequestWillBeSent,
    RequestWillBeSentExtraInfo,
    ResourcePriority,
    ResourceType,
    Response,
    ResponseReceived,
    ResponseReceivedExtraInfo,
    TimeSinceEpoch,
    WebSocketClosed,
    WebSocketCreated,
    WebSocketFrame,
    WebSocketFrameReceived,
    WebSocketFrameSent,
    WebSocketHandshakeResponseReceived,
    WebSocketRequest,
    WebSocketResponse,
    WebSocketWillSendHandshakeRequest,
)
from zendriver.cdp.security import SecurityState

from automatiq.core import config
from automatiq.core.recorder.browser_agent import BrowserAgent

# ── Helper functions ──────────────────────────────────────────────────────────


def read_jsonl(path):
    """Read a JSONL file and return a list of parsed dicts."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_jsonl(path, records):
    """Write a list of dicts as JSONL lines."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


# ── Cookie factories ──────────────────────────────────────────────────────────


def make_cookie(
    name="session_id",
    value="abc123",
    domain="example.com",
    path="/",
    size=10,
    http_only=True,
    secure=True,
    session=False,
):
    return Cookie(
        name=name,
        value=value,
        domain=domain,
        path=path,
        size=size,
        http_only=http_only,
        secure=secure,
        session=session,
        priority=CookiePriority.MEDIUM,
        same_party=False,
        source_scheme=CookieSourceScheme.SECURE,
        source_port=443,
    )


def make_associated_cookie(cookie=None, blocked_reasons=None):
    if cookie is None:
        cookie = make_cookie()
    if blocked_reasons is None:
        blocked_reasons = []
    return AssociatedCookie(cookie=cookie, blocked_reasons=blocked_reasons)


# ── HTTP event factories ──────────────────────────────────────────────────────


def make_request_event(
    request_id="REQ_001",
    url="https://example.com/api/data",
    method="GET",
    headers=None,
    post_data=None,
    resource_type=ResourceType.XHR,
    timestamp=1000.0,
    wall_time=1700000000.0,
    redirect_response=None,
    document_url=None,
    loader_id="LOADER_001",
):
    if headers is None:
        headers = {"Accept": "application/json"}
    if document_url is None:
        document_url = url

    req = Request(
        url=url,
        method=method,
        headers=Headers(headers),
        initial_priority=ResourcePriority.MEDIUM,
        referrer_policy="strict-origin-when-cross-origin",
        post_data=post_data,
    )

    return RequestWillBeSent(
        request_id=RequestId(request_id),
        loader_id=LoaderId(loader_id),
        document_url=document_url,
        request=req,
        timestamp=MonotonicTime(timestamp),
        wall_time=TimeSinceEpoch(wall_time),
        initiator=Initiator(type_="other"),
        redirect_has_extra_info=False,
        redirect_response=redirect_response,
        type_=resource_type,
        frame_id=None,
        has_user_gesture=None,
    )


def make_response_event(
    request_id="REQ_001",
    status=200,
    headers=None,
    mime_type="application/json",
    charset="utf-8",
    timestamp=1001.0,
    loader_id="LOADER_001",
    resource_type=ResourceType.XHR,
    url="https://example.com/api/data",
):
    if headers is None:
        headers = {"Content-Type": "application/json"}

    resp = Response(
        url=url,
        status=status,
        status_text="OK" if 200 <= status < 300 else "Error",
        headers=Headers(headers),
        mime_type=mime_type,
        charset=charset,
        connection_reused=False,
        connection_id=1.0,
        encoded_data_length=100.0,
        security_state=SecurityState.SECURE,
    )

    return ResponseReceived(
        request_id=RequestId(request_id),
        loader_id=LoaderId(loader_id),
        timestamp=MonotonicTime(timestamp),
        type_=resource_type,
        response=resp,
        has_extra_info=False,
        frame_id=None,
    )


def make_data_received_event(
    request_id="REQ_001",
    data=None,
    timestamp=1000.5,
    data_length=None,
    encoded_data_length=None,
):
    if data is None:
        data = base64.b64encode(b'{"key": "value"}').decode("ascii")
    if data_length is None:
        data_length = len(base64.b64decode(data))
    if encoded_data_length is None:
        encoded_data_length = data_length

    return DataReceived(
        request_id=RequestId(request_id),
        timestamp=MonotonicTime(timestamp),
        data_length=data_length,
        encoded_data_length=encoded_data_length,
        data=data,
    )


def make_loading_finished_event(
    request_id="REQ_001",
    timestamp=1002.0,
    encoded_data_length=100.0,
):
    return LoadingFinished(
        request_id=RequestId(request_id),
        timestamp=MonotonicTime(timestamp),
        encoded_data_length=encoded_data_length,
    )


def make_loading_failed_event(
    request_id="REQ_001",
    timestamp=1002.0,
    error_text="net::ERR_FAILED",
    resource_type=ResourceType.XHR,
):
    return LoadingFailed(
        request_id=RequestId(request_id),
        timestamp=MonotonicTime(timestamp),
        type_=resource_type,
        error_text=error_text,
        canceled=None,
        blocked_reason=None,
        cors_error_status=None,
    )


def make_req_extra_info_event(
    request_id="REQ_001",
    associated_cookies=None,
    headers=None,
):
    if associated_cookies is None:
        associated_cookies = []
    if headers is None:
        headers = {}

    return RequestWillBeSentExtraInfo(
        request_id=RequestId(request_id),
        associated_cookies=associated_cookies,
        headers=Headers(headers),
        connect_timing=ConnectTiming(request_time=0.0),
        client_security_state=None,
        site_has_cookie_in_other_partition=None,
    )


def make_res_extra_info_event(
    request_id="REQ_001",
    headers=None,
    blocked_cookies=None,
    exempted_cookies=None,
    status_code=200,
):
    if headers is None:
        headers = {}
    if blocked_cookies is None:
        blocked_cookies = []

    return ResponseReceivedExtraInfo(
        request_id=RequestId(request_id),
        blocked_cookies=blocked_cookies,
        headers=Headers(headers),
        resource_ip_address_space=IPAddressSpace.PUBLIC,
        status_code=status_code,
        headers_text=None,
        cookie_partition_key=None,
        cookie_partition_key_opaque=None,
        exempted_cookies=exempted_cookies,
    )


# ── WebSocket event factories ─────────────────────────────────────────────────


def make_ws_created_event(
    request_id="WS_001",
    url="wss://example.com/socket",
):
    return WebSocketCreated(
        request_id=RequestId(request_id),
        url=url,
        initiator=None,
    )


def make_ws_handshake_request_event(
    request_id="WS_001",
    headers=None,
    timestamp=1000.0,
    wall_time=1700000000.0,
):
    if headers is None:
        headers = {"Upgrade": "websocket", "Connection": "Upgrade", "Host": "example.com"}

    return WebSocketWillSendHandshakeRequest(
        request_id=RequestId(request_id),
        timestamp=MonotonicTime(timestamp),
        wall_time=TimeSinceEpoch(wall_time),
        request=WebSocketRequest(headers=Headers(headers)),
    )


def make_ws_handshake_response_event(
    request_id="WS_001",
    status=101,
    headers=None,
    timestamp=1001.0,
):
    if headers is None:
        headers = {"Upgrade": "websocket", "Connection": "Upgrade"}

    return WebSocketHandshakeResponseReceived(
        request_id=RequestId(request_id),
        timestamp=MonotonicTime(timestamp),
        response=WebSocketResponse(
            status=status,
            status_text="Switching Protocols" if status == 101 else "Error",
            headers=Headers(headers),
        ),
    )


def make_ws_frame_sent_event(
    request_id="WS_001",
    opcode=1.0,
    payload_data="hello",
    timestamp=1002.0,
):
    return WebSocketFrameSent(
        request_id=RequestId(request_id),
        timestamp=MonotonicTime(timestamp),
        response=WebSocketFrame(opcode=opcode, mask=False, payload_data=payload_data),
    )


def make_ws_frame_received_event(
    request_id="WS_001",
    opcode=1.0,
    payload_data="world",
    timestamp=1003.0,
):
    return WebSocketFrameReceived(
        request_id=RequestId(request_id),
        timestamp=MonotonicTime(timestamp),
        response=WebSocketFrame(opcode=opcode, mask=False, payload_data=payload_data),
    )


def make_ws_closed_event(
    request_id="WS_001",
    timestamp=1004.0,
):
    return WebSocketClosed(
        request_id=RequestId(request_id),
        timestamp=MonotonicTime(timestamp),
    )


# ── Action payload factory ────────────────────────────────────────────────────


def make_action_payload(action_type="click", **fields):
    """Build a telemetry action payload dict as the extension would send it.

    Mirrors what telemetry.js POSTs to the ActionServer (before Python stamps
    timestamps). Tests pass it directly to ``agent._process_action``.
    """
    payload = {"type": action_type}
    payload.update(fields)
    return payload


# ── Pytest fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def agent():
    """A BrowserAgent instance with no browser launched.

    All file handles, state dicts, and ts_converter are ready in __init__.
    self.browser/self.tab are None — every browser-dependent code path is
    ``if tab_session:``-guarded, so handlers work without Chrome.
    """
    a = BrowserAgent(blocklist=None)
    yield a

    # Teardown: close file handles if still open, cleanup temp dirs
    for f in (a._actions_file, a._requests_file, a._ws_connections_file, a._ws_frames_file):
        try:
            if not f.closed:
                f.close()
        except Exception:
            pass
    for td in (a._profile_dir, a._data_dir):
        try:
            td.cleanup()
        except Exception:
            pass


@pytest.fixture
def workspace_config(tmp_path, monkeypatch):
    """Monkeypatch config.OUTPUT_DIR and config.WORKSPACE_DIR to tmp_path subdirs.

    compile_workspace converts these to str() and creates subdirectories
    (session_dump/, clips/, requests/) as needed.
    """
    output_dir = tmp_path / "output"
    workspace_dir = output_dir / "workspace"
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(config, "WORKSPACE_DIR", workspace_dir)

    return {
        "output_dir": str(output_dir),
        "workspace_dir": str(workspace_dir),
        "session_dump_dir": str(workspace_dir / "session_dump"),
    }
