"""
Shared download/executable utilities for the managed browser cache.

Used by browser_manager to fetch portable Brave builds and their checksum
files from the network and to mark extracted binaries executable on POSIX.
Downloaded assets land under ~/.automatiq/browsers (the managed cache).
"""

import logging
import platform
import stat
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Platform detection ───────────────────────────────────────────────────────

_ARCH_MAP = {
    "AMD64": "amd64",
    "x86_64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def _detect_platform():
    os_name = {"win32": "windows", "linux": "linux", "darwin": "darwin"}.get(sys.platform)
    arch = _ARCH_MAP.get(platform.machine(), "amd64")
    return os_name, arch


# ── Download helpers ─────────────────────────────────────────────────────────


def _download_file(
    url: str,
    dest: Path,
    label: str | None = None,
    progress_callback: Callable[[int, int], None] = None,
    retries: int = 3,
):
    """Download *url* to *dest*, reporting progress to *progress_callback*.

    Retries up to *retries* times with backoff on transient network errors.
    Raises RuntimeError with a user-friendly message if all attempts fail.
    """
    import time

    display = label or dest.name
    last_exc: Exception | None = None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AutomatiQ/bin-manager"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(dest, "wb") as fp:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        fp.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            progress_callback(downloaded, total)
            logger.debug(f"Downloaded {display} ({dest.stat().st_size:,} bytes)")
            return  # success
        except OSError as exc:
            last_exc = exc
            dest.unlink(missing_ok=True)  # remove partial file
            if attempt < retries - 1:
                wait = 1.5 * (attempt + 1)
                logger.warning(
                    f"Download attempt {attempt + 1} failed for {display}, retrying in {wait:.0f}s... ({exc})"
                )
                time.sleep(wait)

    # All retries exhausted
    raise RuntimeError(
        f"Could not download '{display}' after {retries} attempts.\n"
        f"  This usually means no internet connection or a temporary DNS failure.\n"
        f"  Please check your connection and try again.\n"
        f"  URL: {url}\n"
        f"  Error: {last_exc}"
    ) from last_exc


def _make_executable(path: Path):
    if sys.platform != "win32":
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
