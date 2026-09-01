"""Recorder sub-package — browser-session capture engine.

The capture pipeline (BrowserAgent + ActionServer + CDP handlers + video
recorder) and the compile pipeline (compile/) live here. Session
orchestration — lifecycle, threading, state machine, registry — lives in
``automatiq.mcp.runtime``, which drives ``BrowserAgent.run_session`` and
``compile_workspace`` directly.
"""

import importlib
import logging
import shutil
import sys
import urllib.request

from .. import config, events
from ._guidance import MACOS_PERMISSION_STEPS
from .blocklist_db import BlocklistDB

logger = logging.getLogger(__name__)


def _check_macos_screen_permission() -> None:
    """Warn on macOS if Screen Recording permission is not yet granted.

    Performs a single-frame test grab with ``mss``. If the grab fails with
    ``CGWindowListCreateImage() failed``, macOS is blocking screen capture and
    the full video recording will also produce no output.

    The warning is emitted immediately so the user can fix the permission before
    the recording wastes time. ``mss`` is imported lazily so that merely
    importing this package does not pay for it.
    """
    if sys.platform != "darwin":
        return

    import mss  # noqa: PLC0415 — lazy: heavy-ish import not needed off macOS

    try:
        with mss.mss() as sct:
            if not sct.monitors or len(sct.monitors) < 2:
                return
            sct.grab(sct.monitors[1])
    except mss.exception.ScreenShotError as exc:
        if "CGWindowListCreateImage" in str(exc):
            events.log_warn.send(
                "recorder",
                text=(
                    "⚠️  macOS Screen Recording permission is NOT enabled.\n\n"
                    "AutomatiQ needs this to capture video of your browsing session.\n"
                    "To fix:\n"
                    f"{MACOS_PERMISSION_STEPS}"
                    "  3. Restart your terminal and try again\n\n"
                    "Proceeding without video — the session will lack visual replay.\n"
                ),
            )
        else:
            events.log_debug.send("recorder", text=f"Screen permission check failed (unexpected): {exc}")
    except Exception:
        pass


def _init_blocklist() -> BlocklistDB:
    """Create (or open) the persistent blocklist DB and download any missing source files.

    Runs inside a session worker thread: first-run source downloads can be slow
    and must never delay the MCP tool call that started the session.
    """
    config.ensure_system_dirs()
    db = BlocklistDB(db_path=str(config.BLOCKLIST_DB))

    for name, url in config.BLOCKLIST_SOURCES.items():
        hosts_file = config.BLOCKLIST_DIR / f"{name}.txt"

        if not hosts_file.exists():
            events.log_info.send("recorder", text=f"Downloading blocklist '{name}' ...")
            try:
                # Bounded fetch: urlretrieve has no timeout parameter, so the
                # equivalent urlopen form caps each source at 10s (failures
                # stay non-fatal - the source is simply skipped).
                req = urllib.request.Request(url, headers={"User-Agent": "AutomatiQ/blocklist"})
                with urllib.request.urlopen(req, timeout=10) as resp, open(hosts_file, "wb") as out:
                    shutil.copyfileobj(resp, out)
                events.log_debug.send("recorder", text=f"Saved {hosts_file.name}")
            except Exception as exc:
                events.log_warn.send("recorder", text=f"Failed to download blocklist '{name}': {exc}")
                continue

        db.load_file(str(hosts_file), source_name=name, source_url=url)

    return db


def _resolve_proxy(proxy: str | None = None) -> str | None:
    """Resolve the browser proxy URL.

    Precedence: explicit tool argument > dynamic provider > static server.
    Returns None when proxying is disabled or resolution fails (recording then
    proceeds on a direct connection). The CLI's --no-proxy flag is gone; the
    MCP tool passes proxy=None to mean direct.
    """
    if proxy:
        return proxy

    if not config.RECORDER_PROXY_ENABLED:
        return None

    if config.RECORDER_PROXY_PROVIDER:
        module_path, _, attr = config.RECORDER_PROXY_PROVIDER.partition(":")
        if not module_path or not attr:
            events.log_warn.send(
                "recorder",
                text=f"Invalid proxy provider '{config.RECORDER_PROXY_PROVIDER}' (expected 'module:callable')",
            )
            return config.RECORDER_PROXY_SERVER
        try:
            module = importlib.import_module(module_path)
            proxy_url = getattr(module, attr)()
            if proxy_url:
                return proxy_url
            events.log_warn.send("recorder", text=f"Proxy provider {config.RECORDER_PROXY_PROVIDER} returned no URL")
        except Exception as exc:
            events.log_error.send("recorder", text=f"Proxy provider {config.RECORDER_PROXY_PROVIDER} failed: {exc}")
            events.log_traceback.send("recorder")
        return config.RECORDER_PROXY_SERVER

    return config.RECORDER_PROXY_SERVER
