"""
Automatic binary downloader for the IPython sandbox PATH jail.

Ensures rg, jq, gron (all platforms) and busybox (Windows only) are available.
Checks ~/.automatiq/bin first, then system PATH, then downloads with a Rich
progress display.
"""

import logging
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from . import config

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


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


# ── Busybox URLs (Windows only) ─────────────────────────────────────────────
# https://frippery.org/busybox/
#   busybox.exe     — 32-bit (works on 64-bit too), 632 334 bytes
#   busybox64.exe   — 64-bit, faster on x64, 717 824 bytes
#   busybox64u.exe  — 64-bit + Unicode, Win10 1903+ / Win11, 701 440 bytes
#   busybox64a.exe  — 64-bit ARM + Unicode, 663 040 bytes

_BUSYBOX_BASE = "https://frippery.org/files/busybox/"

_BUSYBOX_VARIANTS = {
    # (arch, has_unicode_support) → filename
    ("arm64", True): "busybox64a.exe",
    ("arm64", False): "busybox64a.exe",  # ARM only has the Unicode build
    ("amd64", True): "busybox64u.exe",  # Unicode — Win10 1903+ / Win11
    ("amd64", False): "busybox64.exe",  # 64-bit without Unicode
}
_BUSYBOX_FALLBACK = "busybox.exe"  # 32-bit, works everywhere


def _pick_busybox_url(arch: str) -> tuple[str, str]:
    """Return (download_url, local_filename) for the best busybox variant."""
    # Unicode builds need Win10 build 18362 (1903) or later.
    unicode_ok = False
    if sys.platform == "win32":
        ver = sys.getwindowsversion()
        unicode_ok = (ver.major, ver.minor, ver.build) >= (10, 0, 18362)

    filename = _BUSYBOX_VARIANTS.get((arch, unicode_ok), _BUSYBOX_FALLBACK)
    return f"{_BUSYBOX_BASE}{filename}", "busybox.exe"


# ── Tool download URLs ──────────────────────────────────────────────────────

RG_URLS = {
    (
        "windows",
        "amd64",
    ): "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-pc-windows-msvc.zip",
    (
        "linux",
        "amd64",
    ): "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz",
    (
        "linux",
        "arm64",
    ): "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-aarch64-unknown-linux-gnu.tar.gz",
    (
        "darwin",
        "amd64",
    ): "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-apple-darwin.tar.gz",
    (
        "darwin",
        "arm64",
    ): "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-aarch64-apple-darwin.tar.gz",
}

JQ_URLS = {
    ("windows", "amd64"): "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-windows-amd64.exe",
    ("linux", "amd64"): "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64",
    ("linux", "arm64"): "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-arm64",
    ("darwin", "amd64"): "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-macos-amd64",
    ("darwin", "arm64"): "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-macos-arm64",
}

GRON_URLS = {
    ("linux", "amd64"): "https://github.com/tomnomnom/gron/releases/download/v0.7.1/gron-linux-amd64-0.7.1.tgz",
    ("linux", "arm64"): "https://github.com/tomnomnom/gron/releases/download/v0.7.1/gron-linux-arm64-0.7.1.tgz",
    ("darwin", "amd64"): "https://github.com/tomnomnom/gron/releases/download/v0.7.1/gron-darwin-amd64-0.7.1.tgz",
    (
        "darwin",
        "arm64",
    ): "https://github.com/tomnomnom/gron/releases/download/v0.7.1/gron-darwin-arm64-0.7.1.tgz",
    ("windows", "amd64"): "https://github.com/tomnomnom/gron/releases/download/v0.7.1/gron-windows-amd64-0.7.1.zip",
}

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


def _extract_binary_from_archive(archive_path: Path, binary_name: str, dest: Path):
    archive_str = str(archive_path)

    if archive_str.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.namelist():
                if member.endswith(binary_name):
                    with zf.open(member) as src, open(dest, "wb") as dst:
                        dst.write(src.read())
                    _make_executable(dest)
                    return True

    elif archive_str.endswith(".tar.gz") or archive_str.endswith(".tgz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.name.endswith(binary_name):
                    f = tf.extractfile(member)
                    if f:
                        with open(dest, "wb") as dst:
                            dst.write(f.read())
                        _make_executable(dest)
                        return True

    logger.warning(f"Could not find {binary_name} inside {archive_path}")
    return False


def _resolve_shim(path: Path) -> Path | None:
    """Resolves Scoop shims on Windows to find the real executable."""
    if sys.platform != "win32":
        return None
    shim_file = path.with_suffix(".shim")
    if shim_file.exists():
        for line in shim_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("path="):
                real = Path(line.split("=", 1)[1].strip().strip('"'))
                if real.exists():
                    return real
    return None


def _copy_system_binary(found: str, dest: Path, test_args: list[str] | None = None) -> bool:
    """
    On Mac/Linux: Does nothing! We trust the system PATH.
    On Windows: Resolves shims and securely links the real .exe into our bin cache.
    """
    if sys.platform != "win32":
        return True

    if test_args is None:
        test_args = ["--version"]

    src = Path(found)
    resolved = _resolve_shim(src)
    if resolved:
        src = resolved

    try:
        os.symlink(src, dest)
    except OSError:
        shutil.copy(src, dest)
        shutil.copymode(src, dest)

    try:
        subprocess.run(
            [str(dest)] + test_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        dest.unlink(missing_ok=True)
        return False


# ── Per-tool ensure functions ────────────────────────────────────────────────


def _ensure_busybox(bin_dir: Path, os_name: str, arch: str, progress_callback: Callable[[int, int], None] = None):
    if os_name != "windows":
        return
    dest = bin_dir / "busybox.exe"
    if dest.exists():
        return
    found = shutil.which("busybox")
    if found and _copy_system_binary(found, dest, test_args=["--help"]):
        return
    url, _ = _pick_busybox_url(arch)
    _download_file(url, dest, label="busybox", progress_callback=progress_callback)


def _ensure_rg(bin_dir: Path, os_name: str, arch: str, progress_callback: Callable[[int, int], None] = None):
    dest = bin_dir / _exe("rg")
    if dest.exists():
        return
    found = shutil.which("rg")
    if found and _copy_system_binary(found, dest):
        return
    url = RG_URLS.get((os_name, arch))
    if not url:
        logger.warning(f"No ripgrep download available for {os_name}/{arch}")
        return
    tmp = bin_dir / os.path.basename(url)
    _download_file(url, tmp, label="ripgrep", progress_callback=progress_callback)
    _extract_binary_from_archive(tmp, _exe("rg"), dest)
    tmp.unlink(missing_ok=True)


def _ensure_jq(bin_dir: Path, os_name: str, arch: str, progress_callback: Callable[[int, int], None] = None):
    dest = bin_dir / _exe("jq")
    if dest.exists():
        return
    found = shutil.which("jq")
    if found and _copy_system_binary(found, dest):
        return
    url = JQ_URLS.get((os_name, arch))
    if not url:
        logger.warning(f"No jq download available for {os_name}/{arch}")
        return
    _download_file(url, dest, label="jq", progress_callback=progress_callback)
    _make_executable(dest)


def _ensure_gron(bin_dir: Path, os_name: str, arch: str, progress_callback: Callable[[int, int], None] = None):
    dest = bin_dir / _exe("gron")
    if dest.exists():
        return
    found = shutil.which("gron")
    if found and _copy_system_binary(found, dest):
        return
    url = GRON_URLS.get((os_name, arch))
    if not url:
        logger.warning(f"No gron download available for {os_name}/{arch}")
        return
    tmp = bin_dir / os.path.basename(url)
    _download_file(url, tmp, label="gron", progress_callback=progress_callback)
    _extract_binary_from_archive(tmp, _exe("gron"), dest)
    tmp.unlink(missing_ok=True)


# ── Public API ───────────────────────────────────────────────────────────────


def ensure_binaries(progress_callback: Callable[[int, int], None] = None, tools: list[str] | None = None) -> Path:
    """Check and download required binaries. Returns the bin directory path."""
    bin_dir = config.BIN_DIR
    bin_dir.mkdir(parents=True, exist_ok=True)

    os_name, arch = _detect_platform()

    if tools is None:
        tools = ["busybox", "rg", "jq", "gron"] if os_name == "windows" else ["rg", "jq", "gron"]

    if "busybox" in tools and os_name == "windows":
        _ensure_busybox(bin_dir, os_name, arch, progress_callback)
    if "rg" in tools:
        _ensure_rg(bin_dir, os_name, arch, progress_callback)
    if "jq" in tools:
        _ensure_jq(bin_dir, os_name, arch, progress_callback)
    if "gron" in tools:
        _ensure_gron(bin_dir, os_name, arch, progress_callback)

    if os_name == "windows" and "busybox" in tools:
        bb = bin_dir / "busybox.exe"
        if not bb.exists() and not shutil.which("busybox"):
            logger.error("busybox is required on Windows but was not found.")
            logger.error("Download manually from https://frippery.org/busybox/")
            logger.error("Place busybox.exe in: " + str(bin_dir))
            sys.exit(1)

    # Only report missing tools; stay silent when everything is fine.
    missing = []
    for tool in tools:
        if not (bin_dir / _exe(tool)).exists() and not shutil.which(tool):
            missing.append(tool)

    if missing:
        logger.warning(f"Failed to install/find: {', '.join(missing)}")
        logger.warning(f"Make sure they exist in PATH or {bin_dir}")

    return bin_dir
