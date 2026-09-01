"""CDP target/tab management and action ingestion — a mixin for BrowserAgent."""

import asyncio
import json
import logging
import time

from zendriver import cdp

from .. import events

logger = logging.getLogger(__name__)


class _TargetManager:
    """Tab attach, network-handler wiring, and action ingestion via `self`."""

    def _process_action(self, payload: dict) -> None:
        """Ingest a telemetry action payload from the ActionServer (or tests).

        Replaces the old CDP ``Runtime.addBinding`` path so the ``Runtime``
        domain no longer needs to be enabled. Called from the ActionServer
        thread; writes are guarded by ``self._actions_lock``.
        """
        try:
            action_type = payload.get("type")
            is_iframe = payload.get("is_iframe", False)

            payload["timestamp_iso"] = self.ts_converter.current_iso8601()
            payload["timestamp_unix"] = time.time()

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
            with self._actions_lock:
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
            events.log_error.send("recorder", text=f"Action processing failed: {e}")
            events.log_traceback.send("recorder")

    def _attach_handlers_to_tab(self, tab_session, session_id, is_iframe=False):
        """Wire CDP network/WebSocket handlers for a tab session.

        Only network-domain handlers are attached — telemetry/visuals injection
        is handled by the Chrome extension's content scripts on every frame, so
        no Runtime/Page domain calls are made here (stealth).
        """
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

    async def target_created_handler(self, event: cdp.target.TargetCreated):
        """Attach CDP network capture + anti-debugger protection to a new tab/popup.

        We use ``TargetCreated`` (from ``set_discover_targets``) instead of
        ``AttachedToTarget`` (from ``set_auto_attach``).  Chrome requires
        ``flatten=True`` for browser-level auto-attach, but flatten creates a
        competing flattened sub-session that intercepts ``Debugger.Paused``
        events on the browser-level websocket — our per-tab handler (on the
        tab's own websocket) never sees them, so anti-debugger traps hang the
        tab.

        With ``TargetCreated`` + no auto-attach, the tab's own websocket is the
        ONLY CDP session for that target.  ``Debugger.Paused`` events go there
        and our auto-resume handler catches them.

          - ``Debugger.enable()`` + ``setSkipAllPauses(True)`` — makes ``debugger;``
            a ~0ms no-op so timing tripwires fail.
          - ``Debugger.Paused`` auto-resume handler — defence-in-depth for any
            pause that slips through (includes ``reason="other"`` for
            ``eval("debugger")`` via ``document.write``).
          - ``Network.enable()`` + wire all network/WS handlers.

        The small race window (scripts could run before setup completes) is not
        a problem in practice — ``chrome://newtab/`` (the default new tab) has no
        anti-debugger traps, and by the time the user navigates to a real site,
        the handler is already armed.
        """
        target_info = event.target_info

        if target_info.type_ != "page":
            return

        target_id = target_info.target_id

        # Skip the main tab (already registered in browser_agent.run_session).
        # TargetCreated fires for existing targets when set_discover_targets is
        # called, so the main tab is re-announced here.
        for existing in self.tabs.values():
            if getattr(existing.get("tab"), "target_id", None) == target_id:
                return

        events.log_info.send("recorder", text=f"New Tab/Window Opened: {target_info.url}")

        # The Tab is created by zendriver's internal TargetCreated handler.
        # Yield once so it can append to self.browser.targets before we scan.
        await asyncio.sleep(0)

        tab_session = next(
            (t for t in self.browser.targets if getattr(t, "target_id", None) == target_id),
            None,
        )

        # Fallback poll: max 1s (200 x 5ms) for slow targets like chrome://newtab/.
        for _ in range(200):
            if tab_session:
                break
            await asyncio.sleep(0.005)
            tab_session = next(
                (t for t in self.browser.targets if getattr(t, "target_id", None) == target_id),
                None,
            )

        if not tab_session:
            events.log_warn.send("recorder", text=f"Could not resolve Tab object for target {target_id}")
            return

        # Key by target_id (no session_id available from TargetCreated).
        self.tabs[str(target_id)] = {"tab": tab_session, "type": "page", "url": target_info.url}
        self._page_target_ids.add(str(target_id))
        events.log_debug.send("recorder", text=f"Successfully bound CDP to new tab: {target_id}")

        try:
            # Anti-debugger protection: enable Debugger domain and set
            # setSkipAllPauses(True) so V8 skips pauses entirely — debugger;
            # becomes a true no-op in ~0ms, so timing tripwires fail.
            # The Debugger.Paused auto-resume handler is defence-in-depth;
            # "other" is included because eval("debugger") via document.write
            # pauses with reason="other".
            await tab_session.send(cdp.debugger.enable())
            await tab_session.send(cdp.debugger.set_skip_all_pauses(skip=True))

            async def _on_debugger_paused(paused_event: cdp.debugger.Paused):
                if paused_event.reason in ("debuggerStatement", "ambiguous", "assert", "other"):
                    await tab_session.send(cdp.debugger.resume())

            tab_session.add_handler(cdp.debugger.Paused, _on_debugger_paused)

            # Network domain — catch HTTP requests and WebSocket handshakes.
            await tab_session.send(
                cdp.network.enable(max_resource_buffer_size=100 * 1024 * 1024, max_total_buffer_size=1000 * 1024 * 1024)
            )
            self._attach_handlers_to_tab(tab_session, str(target_id), is_iframe=False)

            events.log_debug.send("recorder", text=f"[DEBUGGER] New tab fully initialized: {target_id}")
        except Exception as exc:
            events.log_error.send("recorder", text=f"Failed to init CDP on new tab {target_id}: {exc}")
            events.log_traceback.send("recorder")

    async def target_destroyed_handler(self, event: cdp.target.TargetDestroyed):
        """Track page-target destruction so the session can end when the
        user closes the last browser window.

        ``Target.targetDestroyed`` carries only ``target_id`` (no
        target_info), so non-page targets are filtered by checking
        membership in ``self._page_target_ids`` — the set maintained here
        and by ``run_session`` for the main tab. When the set empties, the
        browser UI is gone and ``browser_closed_by_user`` is raised for
        the idle loop in ``browser_agent.run_session``.
        """
        tid = str(event.target_id)
        if tid not in self._page_target_ids:
            return
        self._page_target_ids.discard(tid)
        self.tabs.pop(tid, None)
        events.log_info.send("recorder", text=f"Tab/Window closed ({len(self._page_target_ids)} remaining)")
        if not self._page_target_ids:
            self.browser_closed_by_user = True
