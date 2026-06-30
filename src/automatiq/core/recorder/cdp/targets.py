"""CDP target/tab management and JS-binding dispatch — a mixin for BrowserAgent."""

import asyncio
import json
import logging
import time

from zendriver import cdp

from .. import events

logger = logging.getLogger(__name__)


class _TargetManager:
    """Tab/iframe attach, JS-binding dispatch, and handler wiring via `self`."""

    async def binding_handler_for_tab(self, event: cdp.runtime.BindingCalled, session_id: str):
        if event.name == "sendActionToPython":
            try:
                payload = json.loads(event.payload)
                action_type = payload.get("type")
                is_iframe = payload.get("is_iframe", False)

                payload["timestamp_iso"] = self.ts_converter.current_iso8601()
                payload["timestamp_unix"] = time.time()
                payload["execution_context_id"] = event.execution_context_id
                payload["_session_id"] = session_id

                # We drop script_loaded logs to reduce console spam, but we DO NOT drop
                # iframe actions like 'click' or 'keypress'. We keep them.
                if action_type == "script_loaded":
                    # Only log the main tab init once, or optionally drop it entirely.
                    if not is_iframe:
                        events.log_info.send(
                            "recorder", text="[ACTION] script_loaded: Telemetry script initialized (Main Tab)"
                        )
                    return

                # Stream to disk instead of memory
                self._actions_file.write(json.dumps(payload) + "\n")
                self._actions_file.flush()
                self._actions_count += 1

                tag = " (IFRAME)" if is_iframe else ""

                if action_type == "keypress":
                    events.log_info.send("recorder", text=f"[ACTION] keypress{tag}: {payload.get('key')}")
                elif action_type == "click":
                    events.log_info.send("recorder", text=f"[ACTION] click{tag}: {payload.get('text', '')[:50]}")
                else:
                    fallback_val = payload.get("value", payload.get("newUrl", payload.get("text", "")))
                    events.log_info.send("recorder", text=f"[ACTION] {action_type}{tag}: {fallback_val[:50]}")
            except Exception as e:
                events.log_error.send("recorder", text=f"Binding handler failed: {e}")
                events.log_traceback.send("recorder")

    def _attach_handlers_to_tab(self, tab_session, session_id, is_iframe=False):
        async def on_binding(e):
            await self.binding_handler_for_tab(e, session_id)

        tab_session.add_handler(cdp.runtime.BindingCalled, on_binding)

        if not is_iframe:

            async def on_request(e):
                await self.request_handler_for_tab(e, session_id)

            async def on_data(e):
                await self.data_received_handler_for_tab(e, session_id)

            async def on_response(e):
                await self.response_handler_for_tab(e, session_id)

            async def on_finished(e):
                await self.loading_finished_handler_for_tab(e, session_id)

            async def on_failed(e):
                await self.loading_failed_handler_for_tab(e, session_id)

            async def on_req_extra(e):
                await self.req_extra_info_for_tab(e, session_id)

            async def on_res_extra(e):
                await self.res_extra_info_for_tab(e, session_id)

            tab_session.add_handler(cdp.network.RequestWillBeSent, on_request)
            tab_session.add_handler(cdp.network.DataReceived, on_data)
            tab_session.add_handler(cdp.network.ResponseReceived, on_response)
            tab_session.add_handler(cdp.network.LoadingFinished, on_finished)
            tab_session.add_handler(cdp.network.LoadingFailed, on_failed)
            tab_session.add_handler(cdp.network.RequestWillBeSentExtraInfo, on_req_extra)
            tab_session.add_handler(cdp.network.ResponseReceivedExtraInfo, on_res_extra)

            async def on_ws_created(e):
                await self.websocket_created_handler_for_tab(e, session_id)

            async def on_ws_sent(e):
                await self.websocket_frame_sent_handler_for_tab(e, session_id)

            async def on_ws_received(e):
                await self.websocket_frame_received_handler_for_tab(e, session_id)

            async def on_ws_closed(e):
                await self.websocket_closed_handler_for_tab(e, session_id)

            tab_session.add_handler(cdp.network.WebSocketCreated, on_ws_created)
            tab_session.add_handler(cdp.network.WebSocketFrameSent, on_ws_sent)
            tab_session.add_handler(cdp.network.WebSocketFrameReceived, on_ws_received)
            tab_session.add_handler(cdp.network.WebSocketClosed, on_ws_closed)

            async def on_ws_handshake_req(e):
                await self.websocket_handshake_request_handler_for_tab(e, session_id)

            async def on_ws_handshake_res(e):
                await self.websocket_handshake_response_handler_for_tab(e, session_id)

            tab_session.add_handler(cdp.network.WebSocketWillSendHandshakeRequest, on_ws_handshake_req)
            tab_session.add_handler(cdp.network.WebSocketHandshakeResponseReceived, on_ws_handshake_res)

    async def target_created_handler(self, event: cdp.target.AttachedToTarget):
        target_info = event.target_info

        if target_info.type_ == "page":
            events.log_info.send("recorder", text=f"New Tab/Window Opened: {target_info.url}")

            # Low-latency rapid polling for the newly created tab session object.
            # Avoids a fixed 500ms blind window where critical network events might be lost.
            tab_session = None
            for _ in range(100):  # max 1.0s wait
                for t in self.browser.targets:
                    if (
                        getattr(t, "session_id", None) == event.session_id
                        or getattr(t, "target_id", None) == target_info.target_id
                    ):
                        tab_session = t
                        break
                if tab_session:
                    break
                await asyncio.sleep(0.01)

            if not tab_session:
                events.log_warn.send("recorder", text=f"Could not resolve Tab object for session {event.session_id}")
                return

            self.tabs[event.session_id] = {"tab": tab_session, "type": "page", "url": target_info.url}
            events.log_debug.send("recorder", text=f"Successfully bound CDP to new tab: {target_info.target_id}")

            try:
                # Prioritize network domain to catch immediate websocket handshakes and HTTP requests
                await tab_session.send(
                    cdp.network.enable(
                        max_resource_buffer_size=100 * 1024 * 1024, max_total_buffer_size=1000 * 1024 * 1024
                    )
                )
                await tab_session.send(cdp.page.enable())
                await tab_session.send(cdp.page.set_bypass_csp(enabled=True))

                await tab_session.send(cdp.runtime.enable())
                await tab_session.send(cdp.runtime.add_binding(name="sendActionToPython"))

                self._attach_handlers_to_tab(tab_session, event.session_id, is_iframe=False)

                await tab_session.send(
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.telemetry_script, run_immediately=True)
                )
                await tab_session.send(
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.visuals_script, run_immediately=True)
                )

                await tab_session.send(cdp.runtime.evaluate(expression=self.telemetry_script))
                await tab_session.send(cdp.runtime.evaluate(expression=self.visuals_script))

            except Exception as exc:
                events.log_error.send("recorder", text=f"Failed to init CDP on new tab {target_info.target_id}: {exc}")
                events.log_traceback.send("recorder")

        elif target_info.type_ == "iframe":
            # We ONLY want JS actions from iframes (clicks inside Stripe gateways, etc.)
            # We explicitly do NOT track network requests for iframes because they are
            # already captured by the main 'page' network domain.

            if self.blocklist and self.blocklist.is_blocked_url(target_info.url):
                return

            tab_session = None
            for _ in range(100):  # max 1.0s wait
                for t in self.browser.targets:
                    if (
                        getattr(t, "session_id", None) == event.session_id
                        or getattr(t, "target_id", None) == target_info.target_id
                    ):
                        tab_session = t
                        break
                if tab_session:
                    break
                await asyncio.sleep(0.01)

            if not tab_session:
                return

            self.tabs[event.session_id] = {"tab": tab_session, "type": "iframe", "url": target_info.url}

            try:
                # Enable Runtime so we can add the binding to receive JS messages
                await tab_session.send(cdp.runtime.enable())
                await tab_session.send(cdp.runtime.add_binding(name="sendActionToPython"))

                self._attach_handlers_to_tab(tab_session, event.session_id, is_iframe=True)

                # Enable Page domain just enough to inject the script
                await tab_session.send(cdp.page.enable())
                await tab_session.send(
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.telemetry_script, run_immediately=True)
                )

                # Evaluate immediately in case the iframe is already loaded
                await tab_session.send(cdp.runtime.evaluate(expression=self.telemetry_script))
            except Exception as e:
                # Iframes get destroyed quickly. We log to see if it's the real crash source.
                events.log_error.send("recorder", text=f"IFrame CDP initialization failed: {e}")
                events.log_traceback.send("recorder")
