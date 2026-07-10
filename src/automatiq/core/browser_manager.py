"""
Managed Brave browser downloader.

Fetches a portable Brave build from Brave's official versions JSON
(https://versions.brave.com/latest/brave-versions.json), verifies its SHA256
checksum, and extracts it into ``~/.automatiq/browsers/brave/<version-tag>`` so
that ``zendriver.Config(browser_executable_path=...)`` can launch it directly
without requiring a system install.

Brave is preferred for recording because it ships built-in anti-fingerprinting
and anti-tracking protections that reduce detection signals versus a default
Chromium / Chrome profile — keeping AutomatiQ's recorder stealthier against
anti-bot defenses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

from . import config
from .bin_manager import _detect_platform, _download_file, _make_executable

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_VERSIONS_URL = "https://versions.brave.com/latest/brave-versions.json"

_CHANNELS = ("release", "beta", "nightly")

# (os_name, arch) -> (archive-name template, executable path *relative to the
# extracted archive's top-level dir*).
#
# Brave names their macOS DMG assets without a version number in the filename
# (the version lives in the GitHub release URL path, not the asset name), while
# the Windows/Linux zips embed the version. We template the version string into
# whichever templates need it via ``{ver}`` at lookup time.
#
# macOS uses DMG (not ZIP) because Brave's darwin .zip assets are built with a
# standard Linux `zip` invocation that fails to preserve symlinks (no `-y`
# flag), corrupting the .app bundle's framework version symlinks and code
# signature. DMGs are produced by the same macOS packaging toolchain that
# signs the app, preserving symlinks, codesign, and extended attributes.
_ASSET_MAP: dict[tuple[str, str], tuple[str, str]] = {
    ("windows", "amd64"): ("brave-v{ver}-win32-x64.zip", "brave.exe"),
    ("windows", "arm64"): ("brave-v{ver}-win32-arm64.zip", "brave.exe"),
    ("windows", "ia32"): ("brave-v{ver}-win32-ia32.zip", "brave.exe"),
    ("linux", "amd64"): ("brave-browser-{ver}-linux-amd64.zip", "brave-browser"),
    ("linux", "arm64"): ("brave-browser-{ver}-linux-arm64.zip", "brave-browser"),
    ("darwin", "amd64"): ("Brave-Browser-x64.dmg", "Brave Browser.app/Contents/MacOS/Brave Browser"),
    ("darwin", "arm64"): ("Brave-Browser-arm64.dmg", "Brave Browser.app/Contents/MacOS/Brave Browser"),
}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _brave_cache_root() -> Path:
    """Root directory under which cached Brave versions live."""
    return config.BROWSERS_DIR / "brave"


def _version_dir(tag: str) -> Path:
    """The extraction directory for a specific Brave version tag."""
    return _brave_cache_root() / tag


def _strip_sha256(text: str) -> str:
    """Parse a Brave ``.sha256`` file. Format: ``<hexdigest>  <filename>``."""
    # Some Brave sha files put just the digest on the first line; others use
    # the standard ``<digest>  <name>`` form. Take the first whitespace token.
    return text.strip().split()[0].strip().lower()


def _fetch_versions() -> list[dict]:
    """Fetch the Brave versions JSON and return a list of version entries.

    Each entry is the raw dict from ``brave-versions.json`` (keys: ``tag``,
    ``name``, ``channel``, ``published``, ``github.assets``, ``dependencies``).
    Returns an empty list on network failure (caller decides how to react).
    """
    import urllib.request

    req = urllib.request.Request(_VERSIONS_URL, headers={"User-Agent": "AutomatiQ/browser-manager"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    # The JSON is a top-level mapping { "<tag>": { ...version... }, ... }.
    if isinstance(data, dict):
        return list(data.values())
    if isinstance(data, list):
        return data
    return []


def _latest_for_channel(versions: list[dict], channel: str) -> dict:
    """Return the newest version entry for the given channel.

    Sorted by the ``published`` ISO-8601 timestamp descending.
    """
    if channel not in _CHANNELS:
        raise ValueError(f"channel must be one of {_CHANNELS!r}, got {channel!r}")
    matching = [v for v in versions if v.get("channel") == channel]
    if not matching:
        raise RuntimeError(f"No Brave release found for channel {channel!r}.")
    matching.sort(key=lambda v: v.get("published", ""), reverse=True)
    return matching[0]


def _pick_asset(
    version_entry: dict,
    os_name: str,
    arch: str,
) -> tuple[str, str, str]:
    """Return ``(archive_url, sha256_url, exe_relative_path)`` for the given platform.

    Raises ``RuntimeError`` with a clear message if the platform is unsupported
    or the matching asset isn't in this release.
    """
    ver_tag = version_entry.get("tag") or version_entry.get("name")
    if not ver_tag:
        raise RuntimeError("Brave version entry has no tag/name.")
    # Strip a leading "v" if present — the asset names already include the
    # "v" in some templates (e.g. brave-vX.Y.Z) and don't in others.
    ver_for_template = ver_tag[1:] if ver_tag.startswith("v") else ver_tag

    key = (os_name, arch)
    if key not in _ASSET_MAP:
        raise RuntimeError(
            f"No Brave portable build available for {os_name}/{arch}. "
            "Please install Brave manually or use a different browser."
        )
    asset_template, exe_rel = _ASSET_MAP[key]
    asset_name = asset_template.format(ver=ver_for_template)

    assets = version_entry.get("github", {}).get("assets", []) or []
    archive_url = None
    sha256_url = None
    for asset in assets:
        name = asset.get("name", "")
        if name == asset_name:
            archive_url = asset.get("download_url")
        elif name == f"{asset_name}.sha256":
            sha256_url = asset.get("download_url")
    if not archive_url:
        raise RuntimeError(
            f"Brave release {ver_tag} has no asset named {asset_name!r}. "
            "This platform may not be packaged for this version."
        )
    if not sha256_url:
        raise RuntimeError(
            f"Brave release {ver_tag} has no SHA256 file for {asset_name!r}. "
            "Refusing to download without a verifiable checksum."
        )
    return archive_url, sha256_url, exe_rel


def _download_text(url: str, label: str | None = None, retries: int = 3) -> str:
    """Download a small text file (e.g. a .sha256 manifest) and return its content."""
    import tempfile

    display = label or url.rsplit("/", 1)[-1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
        tmp_path = Path(tmp.name)
    try:
        _download_file(url, tmp_path, label=display, retries=retries)
        return tmp_path.read_text(encoding="utf-8", errors="replace")
    finally:
        tmp_path.unlink(missing_ok=True)


def _verify_sha256(zip_path: Path, sha256_url: str) -> bool:
    """Download the companion .sha256 file and verify the zip against it."""
    expected = _strip_sha256(_download_text(sha256_url, label="sha256"))
    h = hashlib.sha256()
    with open(zip_path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest().lower()
    return actual == expected


def _extract_brave(archive_path: Path, dest_dir: Path) -> None:
    """Extract the Brave archive into *dest_dir*. Creates the directory.

    On macOS the archive is a DMG. We attach it with ``hdiutil`` at a private
    mount point and copy the ``.app`` bundle out with ``shutil.copytree`` using
    ``symlinks=True`` so that Unix symlinks inside the framework (e.g.
    ``Versions/Current -> 150.1.92.134``) are preserved as symlinks rather
    than being dereferenced into regular files. This preserves the bundle's
    code signature too — without it Chromium's internal fork/relaunch step
    crashes with ``exec: multi-threaded process forked``.

    On Windows/Linux the archive is a ZIP and we fall back to ``zipfile``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    if archive_path.suffix == ".dmg" and sys.platform == "darwin" and shutil.which("hdiutil") is not None:
        mount_point = tempfile.mkdtemp(prefix="automatiq_brave_dmg_")
        try:
            subprocess.run(
                ["hdiutil", "attach", "-nobrowse", "-mountpoint", mount_point, str(archive_path)],
                check=True,
                capture_output=True,
            )
            app_found: Path | None = None
            for item in Path(mount_point).iterdir():
                if item.suffix == ".app" and item.is_dir():
                    app_found = item
                    break
            if app_found is None:
                raise RuntimeError(
                    f"No .app bundle found inside DMG {archive_path.name}. "
                    "The DMG layout may have changed — please report this."
                )
            # ``symlinks=True`` is critical: it preserves the framework's
            # ``Versions/Current`` and other internal symlinks as real links
            # rather than dereferencing them into regular files.
            shutil.copytree(app_found, dest_dir / app_found.name, symlinks=True)
        finally:
            # ``eject`` is more forceful than ``detach`` when something still
            # holds a file descriptor — fall back to detach if eject fails.
            subprocess.run(
                ["hdiutil", "detach", mount_point, "-force"],
                capture_output=True,
            )
            shutil.rmtree(mount_point, ignore_errors=True)
    else:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest_dir)


def _remove_macos_quarantine(exe_path: Path) -> None:
    """Remove Apple Gatekeeper quarantine from a downloaded Brave .app bundle.

    When macOS downloads an archive from the internet, every file inside inherits
    a ``com.apple.quarantine`` extended attribute. Without clearing it the user
    either gets a "cannot be opened because the developer cannot be verified"
    alert on first launch, or (starting in macOS 15 Sequoia) the browser is
    blocked entirely until manually approved in System Settings.

    We locate the ``.app`` bundle from the executable path and recursively strip
    all extended attributes (``xattr -cr``), which is the standard recipe for
    un-notarized browser downloads.
    """
    if sys.platform != "darwin":
        return

    app_bundle = exe_path
    while app_bundle.parent != app_bundle:
        if app_bundle.suffix == ".app" and app_bundle.is_dir():
            break
        app_bundle = app_bundle.parent
    else:
        return

    try:
        subprocess.run(
            ["xattr", "-cr", str(app_bundle)],
            capture_output=True,
            check=True,
            timeout=15,
        )
        logger.info(f"Cleared macOS quarantine on {app_bundle}")
    except subprocess.CalledProcessError as exc:
        logger.warning(
            f"Could not clear quarantine on {app_bundle} (stderr: {exc.stderr.decode().strip()!r}). "
            "You may need to right-click > Open the browser once, or run "
            f"'xattr -cr {app_bundle}' manually."
        )
    except FileNotFoundError:
        logger.debug("xattr not found — skipping quarantine removal (not macOS?)")
    except Exception:
        logger.debug("Unexpected error during quarantine removal — continuing anyway.")


def _verify_macos_signature(app_bundle: Path) -> bool:
    """Verify that the ``.app`` bundle has a valid code signature.

    Returns ``True`` if ``codesign --verify --deep --strict`` passes. An invalid
    signature indicates symlink corruption during extraction (Python's zipfile
    on macOS writes symlink target paths as literal file content, which breaks
    framework versioned symlinks and invalidates the bundle signature).
    """
    if sys.platform != "darwin":
        return True
    try:
        result = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app_bundle)],
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode().strip()
            logger.warning(f"Brave code signature invalid — likely symlink corruption during extraction: {stderr}")
            return False
        return True
    except FileNotFoundError:
        logger.debug("codesign not found — skipping signature verification (not macOS?)")
        return True


# ── Public API ───────────────────────────────────────────────────────────────


def find_brave_executable(channel: str | None = None) -> Path | None:
    """Return a usable Brave executable path, or ``None`` if none is available.

    Resolution order (first hit wins):
      1. ``config.BROWSER_EXECUTABLE_PATH`` if set and the file exists.
      2. The newest cached version under ``~/.automatiq/browsers/brave/`` whose
         executable file is present. When *channel* is provided, only version
         dirs whose manifest's ``channel`` matches are considered.
      3. ``shutil.which("brave")`` / ``shutil.which("brave-browser")`` (PATH
         install).
      4. None.
    """
    # 1. Explicit override.
    if config.BROWSER_EXECUTABLE_PATH:
        path = Path(config.BROWSER_EXECUTABLE_PATH)
        if path.exists():
            return path
        logger.warning(f"BROWSER_EXECUTABLE_PATH={path!r} set but file does not exist; ignoring.")

    # 2. Cached managed copy.
    os_name, arch = _detect_platform()
    exe_rel = _ASSET_MAP.get((os_name, arch), (None, None))[1]
    cache_root = _brave_cache_root()
    if cache_root.is_dir() and exe_rel:
        # Look for a <manifest.json> in each version dir to filter by channel.
        # Fall back to "no manifest" (just an executable presence check) so old
        # downloads still work.
        candidates: list[tuple[str, str, Path]] = []  # (published, channel, exe)
        for sub in cache_root.iterdir():
            if not sub.is_dir():
                continue
            exe = sub / exe_rel
            if not exe.exists():
                continue
            manifest_path = sub / ".automatiq-manifest.json"
            entry_channel: str | None = None
            entry_published: str = ""
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    entry_channel = manifest.get("channel")
                    entry_published = manifest.get("published", "")
                except Exception:
                    pass
            else:
                # No manifest: assume it's a valid download. Use the dir mtime
                # for ordering so newest wins.
                entry_published = str(sub.stat().st_mtime)
            if channel is not None and entry_channel is not None and entry_channel != channel:
                continue
            candidates.append((entry_published, entry_channel or "", exe))
        if candidates:
            candidates.sort(reverse=True)  # newest first
            return candidates[0][2]

    # 3. PATH-installed Brave.
    for name in ("brave", "brave-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)

    return None


def _write_manifest(version_dir: Path, version_entry: dict) -> None:
    """Write a small ``.automatiq-manifest.json`` next to the extracted browser.

    Stores the channel + published timestamp so future ``find_brave_executable``
    calls can filter or sort by either field without re-fetching the JSON.
    """
    manifest = {
        "tag": version_entry.get("tag"),
        "name": version_entry.get("name"),
        "channel": version_entry.get("channel"),
        "published": version_entry.get("published", ""),
        "chrome": (version_entry.get("dependencies") or {}).get("chrome"),
    }
    (version_dir / ".automatiq-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def ensure_brave(
    channel: str = "release",
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    force: bool = False,
) -> Path:
    """Ensure a usable Brave executable is available; download one if needed.

    Steps:
      1. If a cached copy of the *latest* version for *channel* exists and
         *force* is False, return it.
      2. Fetch the Brave versions JSON, resolve the latest version for *channel*.
      3. Download the platform-matching zip + its ``.sha256`` to a staging dir.
      4. Verify the checksum. On mismatch, raise ``RuntimeError``.
      5. Extract into ``~/.automatiq/browsers/brave/<version-tag>/``.
      6. Mark the binary executable on POSIX (defensive; the zip usually sets
         the bit already on Linux/macOS).
      7. Write a tiny ``.automatiq-manifest.json`` next to it.
      8. Delete the staging zip + checksum and return the executable path.

    Raises ``RuntimeError`` with a user-friendly message on network failure,
    unsupported platform, or checksum mismatch.
    """
    config.ensure_system_dirs()

    os_name, arch = _detect_platform()

    # Try the cheap path first: do we already have the latest for this channel?
    if not force:
        cached = find_brave_executable(channel=channel)
        if cached is not None:
            # If we have a cached version, check whether it's the latest for
            # this channel. We only do a network lookup here when we have a
            # cached copy; on failure, log and return the cached one.
            try:
                versions = _fetch_versions()
                latest = _latest_for_channel(versions, channel)
                latest_tag = latest.get("tag")
                if latest_tag:
                    # If our cache's manifest tag != latest, fall through to
                    # download.
                    manifest_path = cached.parent / ".automatiq-manifest.json"
                    if manifest_path.exists():
                        try:
                            cached_tag = json.loads(manifest_path.read_text(encoding="utf-8")).get("tag")
                        except Exception:
                            cached_tag = None
                        if cached_tag == latest_tag:
                            return cached
                    # No manifest or stale → fall through and re-download.
            except Exception as exc:
                logger.debug(f"Couldn't reach Brave versions JSON ({exc}); using cached browser.")
                return cached

    # Heavy path: fetch + download + verify + extract.
    versions = _fetch_versions()
    if not versions:
        raise RuntimeError(
            "Could not fetch the Brave versions list.\n"
            f"  URL: {_VERSIONS_URL}\n"
            "  Check your internet connection and try again, or install Brave manually."
        )
    latest = _latest_for_channel(versions, channel)
    tag = latest.get("tag") or latest.get("name")
    if not tag:
        raise RuntimeError("Brave versions JSON returned an entry without a tag/name.")
    archive_url, sha256_url, exe_rel = _pick_asset(latest, os_name, arch)

    version_dir = _version_dir(tag)
    exe_path = version_dir / exe_rel
    if exe_path.exists() and not force:
        _write_manifest(version_dir, latest)
        return exe_path

    # Stage the download under a hidden subdir so we can clean up atomically.
    staging = _brave_cache_root() / ".staging" / tag
    staging.mkdir(parents=True, exist_ok=True)
    archive_path = staging / archive_url.rsplit("/", 1)[-1]
    sha_path = staging / f"{archive_path.name}.sha256"

    try:
        _download_file(archive_url, archive_path, label=f"Brave {tag} ({channel})", progress_callback=progress_callback)
        _download_file(sha256_url, sha_path, label=f"Brave {tag} sha256")
        expected = _strip_sha256(sha_path.read_text(encoding="utf-8", errors="replace"))
        h = hashlib.sha256()
        with open(archive_path, "rb") as fp:
            for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                h.update(chunk)
        actual = h.hexdigest().lower()
        if actual != expected:
            raise RuntimeError(
                f"Brave download checksum mismatch (expected {expected}, got {actual})."
                " The download may be corrupt or tampered with — refusing to use it."
            )

        # Extract.
        if version_dir.exists():
            shutil.rmtree(version_dir, ignore_errors=True)
        _extract_brave(archive_path, version_dir)

        # Defensive chmod on POSIX (the zip usually preserves Unix perms, but
        # some Windows-zipped Unix builds drop the exec bit).
        if sys.platform != "win32" and exe_path.exists():
            _make_executable(exe_path)
        elif sys.platform == "win32":
            # On Windows we just need the .exe to exist; no chmod equivalent.
            pass

        # The Linux/macOS zip contains a top-level dir like "brave-browser-1.92.134/"
        # while Windows zips put files directly under the archive root. Detect a
        # single nested top-level dir and lift its contents up so the final
        # layout is always ``version_dir/<exe_rel>``.
        _flatten_single_nested_dir(version_dir, exe_rel)

        _remove_macos_quarantine(exe_path)

        if sys.platform == "darwin":
            app_bundle = exe_path
            while app_bundle.parent != app_bundle:
                if app_bundle.suffix == ".app" and app_bundle.is_dir():
                    break
                app_bundle = app_bundle.parent
            if not _verify_macos_signature(app_bundle):
                shutil.rmtree(version_dir, ignore_errors=True)
                raise RuntimeError(
                    f"Extracted Brave at {version_dir} has an invalid code signature. "
                    "This usually means the framework layout or code signature was corrupted. "
                    "Delete the cached copy and try again:\n"
                    f"  rm -rf {version_dir}\n"
                    "Then re-run AutomatiQ — it will re-download and mount/extract the DMG correctly."
                )

        _write_manifest(version_dir, latest)
        return exe_path
    finally:
        # Clean up staging (keep around only on success for debugging).
        shutil.rmtree(staging, ignore_errors=True)


def _flatten_single_nested_dir(version_dir: Path, exe_rel: str) -> None:
    """If the extraction put everything under one nested subdir, lift it up.

    Brave's Linux zip extracts to ``brave-browser-<ver>/...``; Windows zips also
    nest under a single top dir. We want the executable at
    ``version_dir/<exe_rel>`` regardless of the zip's internal layout.
    """
    expected = version_dir / exe_rel
    if expected.exists():
        return
    # Look for an immediate child whose own <exe_rel> exists.
    for child in version_dir.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        nested_exe = child / exe_rel
        if nested_exe.exists():
            # Move all of child's contents up into version_dir.
            for item in child.iterdir():
                target = version_dir / item.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
                shutil.move(str(item), str(target))
            child.rmdir()  # now empty
            return


# ── Convenience for the CLI ──────────────────────────────────────────────────


def resolve_browser_for_recording(
    *,
    no_auto_download: bool = False,
    prompt_callback: Callable[[str], bool] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[str, Path | None, str]:
    """Decide how to launch the recording browser.

    Returns a ``(verb, path_or_none, descriptor)`` tuple suitable for passing
    into ``zendriver.Config``:

      - ``("browser_executable_path", <Path>, "Brave <tag>")`` — launch the
        managed-path browser at *path*.
      - ``("browser", "chrome", "Chrome")`` — fall back to zendriver's Chrome
        autodetect.
      - ``("browser", "brave", "Brave (system)")`` — fall back to installed
        Brave via zendriver's Brave autodetect. (Used only when
        ``config.BROWSER_TYPE=="brave"`` but no managed copy is available and
        the user explicitly wants Brave via PATH — currently unreachable, since
        ``find_brave_executable`` already covers PATH.)

    Args:
        no_auto_download: when True, never prompt or download; fall straight
            through to whatever system browser is available.
        prompt_callback: invoked with a ``question`` string when a download is
            being proposed; its return value is truthy to proceed, falsy to
            skip. When None, the download proceeds without prompting (used by
            the ``setup`` command).
        progress_callback: forwarded to :func:`ensure_brave` for Rich progress.
    """
    if config.BROWSER_EXECUTABLE_PATH:
        path = Path(config.BROWSER_EXECUTABLE_PATH)
        if path.exists():
            return "browser_executable_path", path, f"browser at {path}"

    if config.BROWSER_TYPE not in ("brave", "auto", "chrome"):
        # Unrecognised type — let zendriver pick Chrome.
        return "browser", "chrome", "Chrome (default)"

    if config.BROWSER_TYPE == "chrome":
        return "browser", "chrome", "Chrome"

    # auto / brave → prefer Brave.
    found = find_brave_executable(channel=config.BROWSER_CHANNEL)
    if found is not None:
        return "browser_executable_path", found, "Brave"

    if no_auto_download:
        # Fall through to installed Chrome via zendriver's autodetect.
        return "browser", "chrome", "Chrome (Brave not configured)"

    # Offer to download.
    question = (
        "Brave not found. Brave ships built-in anti-fingerprinting and "
        "anti-tracking protections that help keep the recorder stealthy "
        "against websites — fewer detection signals than a default Chrome "
        "profile, making it less likely your automation is noticed by anti-bot "
        "defenses.\n\nDownload a portable Brave copy now? (~300 MB) [Y/n] "
    )
    if prompt_callback is not None:
        try:
            should_download = bool(prompt_callback(question))
        except Exception:
            should_download = False
    else:
        should_download = True

    if not should_download:
        return "browser", "chrome", "Chrome (Brave download skipped)"

    brave_path = ensure_brave(channel=config.BROWSER_CHANNEL, progress_callback=progress_callback)
    return "browser_executable_path", brave_path, "Brave"
