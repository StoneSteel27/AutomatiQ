/**
 * Automatiq Recorder — background service worker.
 *
 * Relays telemetry actions from content scripts to the Python ActionServer.
 *
 * Why a background SW relay? Content scripts share the *page's* origin for
 * network purposes, so a direct `sendBeacon`/`fetch` to http://127.0.0.1 is a
 * public-secure-origin -> private-network request that triggers Chromium's
 * Private Network Access (PNA) permission prompt. The background SW runs under
 * the extension's own origin and, with `host_permissions: ["<all_urls>"]`, is
 * exempt from PNA — so `fetch` to the loopback endpoint never prompts.
 */
importScripts("config.js");

chrome.runtime.onMessage.addListener((msg) => {
    try {
        fetch(AUTOMATIQ_ENDPOINT, {
            method: "POST",
            body: JSON.stringify(msg),
            keepalive: true,
        }).catch(() => {
            /* ActionServer unreachable — swallow to keep SW quiet */
        });
    } catch (e) {
        /* ignore */
    }
    // No `return true`: fire-and-forget so the content-script callback resolves
    // immediately and we don't pin the message channel open.
});
