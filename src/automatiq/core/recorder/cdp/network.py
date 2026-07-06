"""CDP network (HTTP) capture handlers — a mixin for BrowserAgent."""

import base64
import json
import logging
import os
import uuid
from urllib.parse import urlparse

from zendriver import cdp

from .. import events
from .helpers import merge_headers

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _NetworkHandlers:
    """HTTP request/response/cookie handlers sharing BrowserAgent state via `self`."""

    async def request_handler_for_tab(self, event: cdp.network.RequestWillBeSent, session_id: str):
        if event.wall_time:
            self.ts_converter.calibrate(event.timestamp, event.wall_time)

        # Skip the recorder's own ActionServer beacons (telemetry sendBeacon to
        # 127.0.0.1) so they don't pollute the network log or stats.
        parsed = urlparse(event.request.url)
        if parsed.hostname in _LOOPBACK_HOSTS and parsed.path == "/act":
            return

        if event.type_ in (
            cdp.network.ResourceType.DOCUMENT,
            cdp.network.ResourceType.XHR,
            cdp.network.ResourceType.FETCH,
            cdp.network.ResourceType.SCRIPT,
            cdp.network.ResourceType.WEB_SOCKET,
        ):
            self.stats["total_requests"] += 1

        if event.request_id in self.active_map:
            old_req = self.active_map[event.request_id]
            if event.redirect_response and not old_req["response_data"]:
                rd = event.redirect_response.to_json()
                old_req["response_data"] = {"status": rd["status"], "headers": rd.get("headers", {}), "body": None}
            old_req["request_state"] = "redirected"
            old_req.pop("_meta", None)

            if event.redirect_response:
                # CDP reuses the same request_id for every redirect hop and only
                # surfaces intermediate responses via RequestWillBeSent.redirect_response
                # (no separate ResponseReceived/LoadingFinished fires for them). Without
                # flushing here, the original request's method/url/post_data would be
                # overwritten by the redirect target's GET — silently dropping auth
                # POST bodies (e.g. ASP.NET WebForms logins). Persist the prior hop now
                # before the new request_obj replaces it in active_map.
                old_req["redirected"] = True
                old_req["redirected_to_url"] = event.request.url
                self._requests_file.write(json.dumps(old_req) + "\n")
                self._requests_file.flush()
                self.stats["completed"] += 1
                self.active_map.pop(event.request_id, None)

        unique_id = f"{event.request_id}_{uuid.uuid4().hex[:8]}"

        request_obj = {
            "unique_id": unique_id,
            "request_id": event.request_id,
            "timestamp_iso": self.ts_converter.to_iso8601(event.timestamp),
            "timestamp_unix": self.ts_converter.to_unix_timestamp(event.timestamp),
            "timestamp_monotonic": event.timestamp,
            "url": event.request.url,
            "method": event.request.method,
            "resource_type": str(event.type_),
            "headers": dict(event.request.headers),
            "post_data": event.request.post_data,
            "cookies_sent_details": [],
            "cookies_received_details": {},
            "response_data": None,
            "response_timing": {},
            "request_state": "pending",
            "body_fetch_error": None,
            "_session_id": session_id,
        }

        if event.request_id in self.orphan_extra_info:
            data = self.orphan_extra_info.pop(event.request_id)
            if "sent" in data:
                request_obj["cookies_sent_details"] = data["sent"]
            if "received" in data:
                request_obj["cookies_received_details"] = data["received"]
            if "raw_headers" in data:
                merge_headers(request_obj, data["raw_headers"])

        if event.request.url.startswith("data:"):
            return

        if self.blocklist and self.blocklist.is_blocked_url(event.request.url):
            self.stats["blocked_by_blocklist"] += 1
            return

        self.active_map[event.request_id] = request_obj

    async def data_received_handler_for_tab(self, event: cdp.network.DataReceived, session_id: str):
        rid = str(event.request_id)
        # We write raw chunks directly to the body file
        if event.data:
            try:
                body_path = os.path.join(self._bodies_dir, f"{rid}.bin")
                with open(body_path, "ab") as f:
                    f.write(base64.b64decode(event.data))
            except Exception as exc:
                events.log_error.send("recorder", text=f"Failed to decode/write chunk {rid}: {exc}")
                events.log_traceback.send("recorder")

    async def response_handler_for_tab(self, event: cdp.network.ResponseReceived, session_id: str):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "received"
            req["response_timing"]["received_iso"] = self.ts_converter.to_iso8601(event.timestamp)
            req["response_timing"]["received_unix"] = self.ts_converter.to_unix_timestamp(event.timestamp)

            if "timestamp_unix" in req:
                req["response_timing"]["duration_ms"] = round(
                    (req["response_timing"]["received_unix"] - req["timestamp_unix"]) * 1000, 2
                )

            resp = event.response.to_json()
            if not req["response_data"]:
                req["response_data"] = {
                    "status": resp["status"],
                    "headers": {},
                    "body": None,
                    "base64_encoded": False,
                    "charset": resp.get("charset", "utf-8") or "utf-8",
                    "mime_type": resp.get("mimeType", "unknown"),
                    "from_disk_cache": resp.get("fromDiskCache", False),
                    "from_service_worker": resp.get("fromServiceWorker", False),
                    "from_prefetch_cache": resp.get("fromPrefetchCache", False),
                }
            else:
                req["response_data"]["status"] = resp["status"]
                req["response_data"]["mime_type"] = resp.get("mimeType", "unknown")
            merge_headers(req, resp.get("headers", {}))

            rid = str(event.request_id)
            body_path = os.path.join(self._bodies_dir, f"{rid}.bin")
            # Ensure an empty file exists so we know we tracked it
            if not os.path.exists(body_path):
                open(body_path, "a").close()

            tab_session = self.tabs.get(session_id, {}).get("tab")
            if tab_session:
                try:
                    buffered = await tab_session.send(cdp.network.stream_resource_content(request_id=event.request_id))
                    if buffered:
                        with open(body_path, "ab") as f:
                            f.write(base64.b64decode(buffered))
                except Exception as e:
                    error_str = str(e)
                    if "already finished loading" not in error_str and "does not exist" not in error_str:
                        events.log_warn.send("recorder", text=f"Stream resource failed: {e}")

    async def loading_finished_handler_for_tab(self, event: cdp.network.LoadingFinished, session_id: str):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "finished"
            self.stats["completed"] += 1
            req["response_timing"]["finished_iso"] = self.ts_converter.to_iso8601(event.timestamp)
            req["response_timing"]["finished_unix"] = self.ts_converter.to_unix_timestamp(event.timestamp)

            status = req["response_data"].get("status", 0) if req["response_data"] else 0
            if 300 <= status < 400:
                self.stats["body_skip_redirect"] += 1
                req["body_fetch_error"] = f"Redirect status {status}"
            elif status in (204, 205, 304):
                self.stats["body_skip_no_content"] += 1
                req["body_fetch_error"] = f"No content status {status}"
            else:
                body_captured = False
                tab_session = self.tabs.get(session_id, {}).get("tab")
                rid = str(event.request_id)
                body_path = os.path.join(self._bodies_dir, f"{rid}.bin")

                if tab_session:
                    try:
                        result = await tab_session.send(cdp.network.get_response_body(request_id=event.request_id))
                        raw_bytes = None
                        if isinstance(result, tuple):
                            body_str, is_base64 = result
                        else:
                            body_str = result.body
                            is_base64 = result.base64_encoded

                        if is_base64:
                            raw_bytes = base64.b64decode(body_str)
                        else:
                            raw_bytes = body_str.encode("utf-8")

                        with open(body_path, "wb") as f:
                            f.write(raw_bytes)

                        req["response_data"]["body_file"] = f"bodies/{rid}.bin"
                        self.stats["body_success"] += 1
                        body_captured = True
                    except Exception as e:
                        req["body_fetch_error"] = str(e)
                        # We won't log traceback for this one as get_response_body
                        # often fails for valid reasons (cache eviction)

                if not body_captured and os.path.exists(body_path) and os.path.getsize(body_path) > 0:
                    req["response_data"]["body_file"] = f"bodies/{rid}.bin"
                    req["body_fetch_error"] = None
                    self.stats["body_from_stream"] += 1
                    body_captured = True

                if not body_captured:
                    self.stats["body_failed"] += 1

            # Stream request to disk and remove from memory
            self._requests_file.write(json.dumps(req) + "\n")
            self._requests_file.flush()
            self.active_map.pop(event.request_id, None)

    async def loading_failed_handler_for_tab(self, event: cdp.network.LoadingFailed, session_id: str):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "failed"
            req["error_text"] = event.error_text
            self.stats["failed"] += 1

            # Stream request to disk and remove from memory
            self._requests_file.write(json.dumps(req) + "\n")
            self._requests_file.flush()
            self.active_map.pop(event.request_id, None)

    async def req_extra_info_for_tab(self, event: cdp.network.RequestWillBeSentExtraInfo, session_id: str):
        cookies = [ac.to_json() for ac in event.associated_cookies]
        if event.request_id in self.active_map:
            self.active_map[event.request_id]["cookies_sent_details"] = cookies
        else:
            if event.request_id not in self.orphan_extra_info:
                self.orphan_extra_info[event.request_id] = {}
            self.orphan_extra_info[event.request_id]["sent"] = cookies

    async def res_extra_info_for_tab(self, event: cdp.network.ResponseReceivedExtraInfo, session_id: str):
        cookie_data = {
            "blocked": [c.to_json() for c in event.blocked_cookies],
            "exempted": [c.to_json() for c in (event.exempted_cookies or [])],
        }
        headers = dict(event.headers)
        if event.request_id in self.active_map:
            self.active_map[event.request_id]["cookies_received_details"] = cookie_data
            merge_headers(self.active_map[event.request_id], headers)
        else:
            if event.request_id not in self.orphan_extra_info:
                self.orphan_extra_info[event.request_id] = {}
            self.orphan_extra_info[event.request_id]["received"] = cookie_data
            self.orphan_extra_info[event.request_id]["raw_headers"] = headers
