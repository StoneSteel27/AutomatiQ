"""Tests for browser_manager — Brave download, caching, and resolution."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from automatiq.core import config
from automatiq.core.browser_manager import (
    _ASSET_MAP,
    _VERSIONS_URL,
    _flatten_single_nested_dir,
    _latest_for_channel,
    _pick_asset,
    _strip_sha256,
    _write_manifest,
    ensure_brave,
    find_brave_executable,
    resolve_browser_for_recording,
)

# ── synthetic test data ─────────────────────────────────────────────────────


def _synthetic_versions(channel: str = "release", tag: str = "v1.92.134") -> list[dict]:
    """Return a list mimicking ``brave-versions.json`` entries."""
    return [
        {
            "tag": tag,
            "name": tag.lstrip("v"),
            "channel": channel,
            "published": "2025-12-01T12:00:00Z",
            "dependencies": {"chrome": "150.0.7871.63"},
            "github": {
                "release_id": 12345,
                "assets": [
                    {
                        "name": "brave-v1.92.134-win32-x64.zip",
                        "download_url": "https://github.com/brave/brave-browser/releases/download/v1.92.134/brave-v1.92.134-win32-x64.zip",
                    },
                    {
                        "name": "brave-v1.92.134-win32-x64.zip.sha256",
                        "download_url": "https://github.com/brave/brave-browser/releases/download/v1.92.134/brave-v1.92.134-win32-x64.zip.sha256",
                    },
                    {
                        "name": "brave-browser-1.92.134-linux-amd64.zip",
                        "download_url": "https://github.com/brave/brave-browser/releases/download/v1.92.134/brave-browser-1.92.134-linux-amd64.zip",
                    },
                    {
                        "name": "brave-browser-1.92.134-linux-amd64.zip.sha256",
                        "download_url": "https://github.com/brave/brave-browser/releases/download/v1.92.134/brave-browser-1.92.134-linux-amd64.zip.sha256",
                    },
                    {
                        "name": "Brave-Browser-x64.dmg",
                        "download_url": "https://github.com/brave/brave-browser/releases/download/v1.92.134/Brave-Browser-x64.dmg",
                    },
                    {
                        "name": "Brave-Browser-x64.dmg.sha256",
                        "download_url": "https://github.com/brave/brave-browser/releases/download/v1.92.134/Brave-Browser-x64.dmg.sha256",
                    },
                    {
                        "name": "Brave-Browser-arm64.dmg",
                        "download_url": "https://github.com/brave/brave-browser/releases/download/v1.92.134/Brave-Browser-arm64.dmg",
                    },
                    {
                        "name": "Brave-Browser-arm64.dmg.sha256",
                        "download_url": "https://github.com/brave/brave-browser/releases/download/v1.92.134/Brave-Browser-arm64.dmg.sha256",
                    },
                    {
                        "name": "brave-browser-1.92.134-linux-arm64.zip",
                        "download_url": "https://github.com/brave/brave-browser/releases/download/v1.92.134/brave-browser-1.92.134-linux-arm64.zip",
                    },
                    {
                        "name": "brave-browser-1.92.134-linux-arm64.zip.sha256",
                        "download_url": "https://github.com/brave/brave-browser/releases/download/v1.92.134/brave-browser-1.92.134-linux-arm64.zip.sha256",
                    },
                ],
            },
        },
    ]


def _make_fake_brave_zip(exe_rel_path: str, content: bytes | None = None) -> bytes:
    """Create an in-memory zip that, when extracted, contains *exe_rel_path*."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(exe_rel_path, content or b"fake-brave-binary")
    return buf.getvalue()


def _make_nested_brave_zip(inner_dir: str, exe_rel_path: str) -> bytes:
    """Create an in-memory zip with a nested directory that *contains* exe_rel_path."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{inner_dir}/{exe_rel_path}", b"fake-brave-binary")
    return buf.getvalue()


# ── fixture helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def clean_browsers_dir(tmp_path, monkeypatch):
    """Point BROWSERS_DIR to a temp for the test.

    Also stubs ``shutil.which`` to return ``None`` so that
    ``find_brave_executable`` (called by ``ensure_brave``) never scans the
    host ``PATH`` for a system-installed Brave. This avoids two problems:

      1. On Windows / VMs the full PATH scan is dominated by slow directory
         enumeration across shared drives, multi-hundred-entry PATHs, etc.
         Repeating it across ~30 tests added tens of seconds of wall time.
      2. If the developer happens to have Brave installed system-wide, the
         cache-miss branch we are trying to exercise would be silently
         bypassed and several tests would assert on a path they never built.

    Behaviour in GitHub Actions is unchanged: CI runners do not have Brave
    on their PATH, so ``shutil.which`` already returns ``None`` there — this
    mock just makes that explicit and constant across all developer hosts.
    """
    monkeypatch.setattr(config, "BROWSERS_DIR", tmp_path)
    monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSERS_DIR", tmp_path)
    monkeypatch.setattr(config, "BROWSER_EXECUTABLE_PATH", None)
    monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_EXECUTABLE_PATH", None)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    yield tmp_path
    # Restore after test
    monkeypatch.setattr(config, "BROWSERS_DIR", Path(config.HOME_DIR) / "browsers")
    monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSERS_DIR", Path(config.HOME_DIR) / "browsers")


@pytest.fixture
def mock_fetch(monkeypatch):
    """Replace _fetch_versions with a test stub."""
    captured_urls = []

    def _stub():
        captured_urls.append(_VERSIONS_URL)
        return _synthetic_versions()

    monkeypatch.setattr(
        "automatiq.core.browser_manager._fetch_versions",
        _stub,
    )
    return captured_urls


@pytest.fixture
def mock_download(monkeypatch, tmp_path):
    """Replace _download_file so it writes a file that looks like a valid zip.

    Returns a dict keyed by url -> (dest_path, sha256_of_content).
    """
    downloads = {}

    def _stub(url: str, dest: Path, label: str | None = None, progress_callback=None, retries: int = 3):
        tag = "v1.92.134"
        # Determine what we're fetching so we can create a realistic fake.
        if "/brave-versions.json" in url:
            # This would be a text download; write raw text.
            dest.write_text(json.dumps({tag: _synthetic_versions()[0]}), encoding="utf-8")
            downloads[url] = (dest, "")
            return
        if url.endswith(".sha256"):
            # The caller wants a checksum file.
            # Find the matching zip download we already captured.
            zip_url = url.replace(".sha256", "")
            if zip_url in downloads:
                sha = hashlib.sha256()
                sha_dest = downloads[zip_url][0]
                sha.update(sha_dest.read_bytes())
                digest = sha.hexdigest().strip()
                dest.write_text(f"{digest}  {Path(zip_url).name}\n", encoding="utf-8")
            else:
                dest.write_text("deadbeef  test.zip\n", encoding="utf-8")
        else:
            # This is a zip download — write a fake zip with the right exe.
            # Determine the target platform from the URL so the fake zip
            # contains the correct executable path for that platform.
            if "win32" in url:
                exe_rel = "brave.exe"
            elif "linux" in url:
                exe_rel = "brave-browser"
            elif "darwin" in url or "Brave-Browser" in url:
                exe_rel = "Brave Browser.app/Contents/MacOS/Brave Browser"
            else:
                exe_rel = None
            if exe_rel:
                dest.write_bytes(_make_fake_brave_zip(exe_rel))
        downloads[url] = (dest, "")

    monkeypatch.setattr(
        "automatiq.core.browser_manager._download_file",
        _stub,
    )
    return downloads


# ── unit tests ──────────────────────────────────────────────────────────────


class TestAssetMapping:
    """Cross-platform asset name mapping."""

    @pytest.mark.parametrize(
        "os_name, arch, expected_archive, expected_exe",
        [
            ("windows", "amd64", "brave-v{ver}-win32-x64.zip", "brave.exe"),
            ("windows", "arm64", "brave-v{ver}-win32-arm64.zip", "brave.exe"),
            ("windows", "ia32", "brave-v{ver}-win32-ia32.zip", "brave.exe"),
            ("linux", "amd64", "brave-browser-{ver}-linux-amd64.zip", "brave-browser"),
            ("linux", "arm64", "brave-browser-{ver}-linux-arm64.zip", "brave-browser"),
            ("darwin", "amd64", "Brave-Browser-x64.dmg", "Brave Browser.app/Contents/MacOS/Brave Browser"),
            ("darwin", "arm64", "Brave-Browser-arm64.dmg", "Brave Browser.app/Contents/MacOS/Brave Browser"),
        ],
    )
    def test_asset_map_has_all_entries(self, os_name, arch, expected_archive, expected_exe):
        assert (os_name, arch) in _ASSET_MAP
        template, exe = _ASSET_MAP[(os_name, arch)]
        assert template == expected_archive
        assert exe == expected_exe

    def test_pick_asset_windows_x64(self):
        versions = _synthetic_versions()
        zip_url, sha_url, exe_rel = _pick_asset(versions[0], "windows", "amd64")
        assert "win32-x64.zip" in zip_url
        assert "win32-x64.zip.sha256" in sha_url
        assert exe_rel == "brave.exe"

    def test_pick_asset_linux_amd64(self):
        versions = _synthetic_versions()
        zip_url, sha_url, exe_rel = _pick_asset(versions[0], "linux", "amd64")
        assert "linux-amd64.zip" in zip_url
        assert "linux-amd64.zip.sha256" in sha_url
        assert exe_rel == "brave-browser"

    def test_pick_asset_darwin_x64(self):
        versions = _synthetic_versions()
        zip_url, sha_url, exe_rel = _pick_asset(versions[0], "darwin", "amd64")
        assert "Brave-Browser-x64.dmg" in zip_url
        assert exe_rel == "Brave Browser.app/Contents/MacOS/Brave Browser"

    def test_pick_asset_missing_asset_raises(self):
        """A version entry without the matching asset should raise."""
        entry = _synthetic_versions()[0]
        entry["github"]["assets"] = []  # no assets
        with pytest.raises(RuntimeError, match="has no asset"):
            _pick_asset(entry, "windows", "amd64")

    def test_pick_asset_missing_sha256_raises(self):
        """A version entry with the zip but no sha256 companion should raise."""
        entry = _synthetic_versions()[0]
        entry["github"]["assets"] = [
            {"name": "brave-v1.92.134-win32-x64.zip", "download_url": "http://a"},
        ]
        with pytest.raises(RuntimeError, match="no SHA256"):
            _pick_asset(entry, "windows", "amd64")


class TestChecksumHelpers:
    """SHA256 parsing and verification."""

    def test_strip_sha256_standard_format(self):
        text = "abc123def456  brave-v1.92.134-win32-x64.zip\n"
        assert _strip_sha256(text) == "abc123def456"

    def test_strip_sha256_digest_only(self):
        text = "abc123def456\n"
        assert _strip_sha256(text) == "abc123def456"

    def test_strip_sha256_extra_spaces(self):
        text = "  abc123def456  \n"
        assert _strip_sha256(text) == "abc123def456"


class TestVersionResolution:
    """Channel filtering / latest-version selection."""

    def test_latest_for_channel_release(self):
        entries = [
            {"channel": "release", "published": "2025-06-01T00:00:00Z", "tag": "v1.0"},
            {"channel": "release", "published": "2025-07-01T00:00:00Z", "tag": "v2.0"},
            {"channel": "beta", "published": "2025-08-01T00:00:00Z", "tag": "v3.0"},
        ]
        latest = _latest_for_channel(entries, "release")
        assert latest["tag"] == "v2.0"

    def test_latest_for_channel_beta(self):
        entries = [
            {"channel": "release", "published": "2025-07-01T00:00:00Z", "tag": "v2.0"},
            {"channel": "beta", "published": "2025-08-01T00:00:00Z", "tag": "v3.0"},
        ]
        latest = _latest_for_channel(entries, "beta")
        assert latest["tag"] == "v3.0"

    def test_channel_not_found_raises(self):
        with pytest.raises(RuntimeError, match="No Brave release found"):
            _latest_for_channel([], "release")

    def test_invalid_channel_raises(self):
        with pytest.raises(ValueError, match="channel must be one of"):
            _latest_for_channel([], "garbage")


class TestFindBraveExecutable:
    """Resolution precedence: explicit path > cache > PATH > None."""

    def test_browser_executable_path_set_and_exists(self, tmp_path, monkeypatch):
        exe = tmp_path / "custom-brave.exe"
        exe.write_text("brave")
        monkeypatch.setattr(config, "BROWSER_EXECUTABLE_PATH", str(exe))
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_EXECUTABLE_PATH", str(exe))
        result = find_brave_executable()
        assert result == exe

    def test_browser_executable_path_set_but_missing_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "BROWSERS_DIR", tmp_path)
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSERS_DIR", tmp_path)
        monkeypatch.setattr(config, "BROWSER_EXECUTABLE_PATH", "/nonexistent/brave.exe")
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_EXECUTABLE_PATH", "/nonexistent/brave.exe")
        monkeypatch.setattr(sys, "platform", "win32")
        # No cache, no PATH — should return None (after logging a warning).
        with patch("shutil.which", return_value=None):
            result = find_brave_executable()
        assert result is None

    def test_cache_returns_newest_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "BROWSERS_DIR", tmp_path)
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSERS_DIR", tmp_path)
        monkeypatch.setattr(config, "BROWSER_EXECUTABLE_PATH", None)
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_EXECUTABLE_PATH", None)

        from automatiq.core.browser_manager import _ASSET_MAP, _detect_platform

        os_name, arch = _detect_platform()
        exe_rel = _ASSET_MAP[(os_name, arch)][1]

        # Create two version dirs — the one with larger mtime should win.
        v1 = tmp_path / "brave" / "v1.0.0"
        v1_exe = v1 / exe_rel
        v1_exe.parent.mkdir(parents=True, exist_ok=True)
        v1_exe.write_text("old")

        v2 = tmp_path / "brave" / "v2.0.0"
        v2_exe = v2 / exe_rel
        v2_exe.parent.mkdir(parents=True, exist_ok=True)
        v2_exe.write_text("new")

        with patch("shutil.which", return_value=None):
            result = find_brave_executable()
        assert result is not None
        # Should return the newer one (by mtime — v2 was created after v1).
        assert "v2.0.0" in str(result) or result == v2_exe

    def test_cache_filters_by_channel_via_manifest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "BROWSERS_DIR", tmp_path)
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSERS_DIR", tmp_path)
        monkeypatch.setattr(config, "BROWSER_EXECUTABLE_PATH", None)
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_EXECUTABLE_PATH", None)

        from automatiq.core.browser_manager import _ASSET_MAP, _detect_platform

        os_name, arch = _detect_platform()
        exe_rel = _ASSET_MAP[(os_name, arch)][1]

        v_dir = tmp_path / "brave" / "v1.0.0"
        v_exe = v_dir / exe_rel
        v_exe.parent.mkdir(parents=True, exist_ok=True)
        v_exe.write_text("brave")
        (v_dir / ".automatiq-manifest.json").write_text(
            json.dumps({"channel": "beta", "published": "2025-01-01T00:00:00Z", "tag": "v1.0.0"}),
            encoding="utf-8",
        )

        with patch("shutil.which", return_value=None):
            # Asking for release should NOT find the beta-only cache.
            result = find_brave_executable(channel="release")
            assert result is None

            # Asking for beta SHOULD find it.
            result = find_brave_executable(channel="beta")
            assert result is not None
            assert "v1.0.0" in str(result)

    def test_shutil_which_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "BROWSERS_DIR", tmp_path)
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSERS_DIR", tmp_path)
        monkeypatch.setattr(config, "BROWSER_EXECUTABLE_PATH", None)
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_EXECUTABLE_PATH", None)
        with patch("shutil.which", return_value="/usr/bin/brave"):
            result = find_brave_executable()
            assert result == Path("/usr/bin/brave")


class TestFlattenNestedDir:
    """ZIP extraction nesting clean-up."""

    def test_flatten_flat_layout(self, tmp_path):
        """When the exe is at version_dir/<exe_rel>, nothing moves."""
        version_dir = tmp_path / "v1.0.0"
        version_dir.mkdir()
        (version_dir / "brave.exe").write_text("hello")
        _flatten_single_nested_dir(version_dir, "brave.exe")
        assert (version_dir / "brave.exe").exists()

    def test_flatten_nested_layout(self, tmp_path):
        """When the exe is at version_dir/inner/<exe_rel>, we lift it up."""
        version_dir = tmp_path / "v1.0.0"
        version_dir.mkdir()
        inner = version_dir / "brave-browser-1.92.134"
        inner.mkdir(parents=True)
        (inner / "brave-browser").write_text("nested-brave")
        _flatten_single_nested_dir(version_dir, "brave-browser")
        assert (version_dir / "brave-browser").exists()
        assert not inner.exists()  # should have been removed


class TestManifestIO:
    """Writing and reading the .automatiq-manifest.json file."""

    def test_write_manifest(self, tmp_path):
        version_dir = tmp_path / "v1.0.0"
        version_dir.mkdir()
        entry = _synthetic_versions()[0]
        _write_manifest(version_dir, entry)
        manifest = json.loads((version_dir / ".automatiq-manifest.json").read_text(encoding="utf-8"))
        assert manifest["tag"] == "v1.92.134"
        assert manifest["channel"] == "release"
        assert manifest["chrome"] == "150.0.7871.63"


class TestEnsureBrave:
    """Full end-to-end with mocked fetch + download + extract."""

    def test_download_creates_cached_executable(self, clean_browsers_dir, mock_fetch, mock_download, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        with patch("automatiq.core.browser_manager._detect_platform", return_value=("windows", "amd64")):
            path = ensure_brave(channel="release")
        assert path.exists()
        assert path.name == "brave.exe"
        assert "brave" in str(path.parent)
        # Manifest should have been written.
        manifest = path.parent / ".automatiq-manifest.json"
        assert manifest.exists()

    def test_ensure_brave_is_idempotent(self, clean_browsers_dir, mock_fetch, mock_download, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        with patch("automatiq.core.browser_manager._detect_platform", return_value=("windows", "amd64")):
            path1 = ensure_brave(channel="release")
            # Force _fetch_versions to raise on the second call to prove it does
            # NOT fetch again.
            monkeypatch.setattr(
                "automatiq.core.browser_manager._fetch_versions",
                lambda: (_ for _ in ()).throw(RuntimeError("network should not be called")),
            )
            path2 = ensure_brave(channel="release")
        assert path1 == path2

    def test_force_re_downloads(self, clean_browsers_dir, mock_fetch, mock_download, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        with patch("automatiq.core.browser_manager._detect_platform", return_value=("windows", "amd64")):
            ensure_brave(channel="release")
        # force=True should re-download even though it's cached.
        with patch("automatiq.core.browser_manager._detect_platform", return_value=("windows", "amd64")):
            path = ensure_brave(channel="release", force=True)
        assert path.exists()

    def test_linux_binary_path(self, clean_browsers_dir, mock_fetch, mock_download, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("automatiq.core.browser_manager._detect_platform", return_value=("linux", "amd64")):
            path = ensure_brave(channel="release")
        assert path.name == "brave-browser"

    def test_darwin_binary_path(self, clean_browsers_dir, mock_fetch, mock_download, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch("automatiq.core.browser_manager._detect_platform", return_value=("darwin", "amd64")):
            path = ensure_brave(channel="release")
        assert path.name == "Brave Browser"
        assert "Brave Browser.app" in str(path)

    def test_unsupported_platform_raises(self, clean_browsers_dir, mock_fetch, monkeypatch):
        with patch("automatiq.core.browser_manager._detect_platform", return_value=("freebsd", "amd64")):
            with pytest.raises(RuntimeError, match="No Brave portable build"):
                ensure_brave(channel="release")

    def test_fetch_failure_raises(self, clean_browsers_dir, monkeypatch):
        monkeypatch.setattr(
            "automatiq.core.browser_manager._fetch_versions",
            lambda: [],
        )
        with pytest.raises(RuntimeError, match="Could not fetch"):
            ensure_brave(channel="release")

    def test_checksum_mismatch_raises(self, clean_browsers_dir, mock_fetch, monkeypatch):
        """When the sha256 doesn't match, we should raise and clean up staging."""
        monkeypatch.setattr(sys, "platform", "win32")

        def _bad_download(url: str, dest: Path, label: str | None = None, progress_callback=None, retries: int = 3):
            if url.endswith(".sha256"):
                dest.write_text(
                    "0000000000000000000000000000000000000000000000000000000000000000  test.zip\n",
                    encoding="utf-8",
                )
            else:
                dest.write_bytes(_make_fake_brave_zip("brave.exe"))

        monkeypatch.setattr("automatiq.core.browser_manager._download_file", _bad_download)
        with patch("automatiq.core.browser_manager._detect_platform", return_value=("windows", "amd64")):
            with pytest.raises(RuntimeError, match="checksum mismatch"):
                ensure_brave(channel="release")

    def test_version_dir_nesting_is_flattened(self, clean_browsers_dir, mock_fetch, monkeypatch):
        """Brave Linux zip extracts to a nested dir; we flatten it."""
        monkeypatch.setattr(sys, "platform", "linux")

        def _nested_download(url: str, dest: Path, label: str | None = None, progress_callback=None, retries: int = 3):
            if url.endswith(".sha256"):
                # Pre-compute the sha256 by writing the zip first
                zip_dest = clean_browsers_dir / "browser" / ".staging" / "v1.92.134" / "fake.zip"
                zip_dest.parent.mkdir(parents=True, exist_ok=True)
                zip_dest.write_bytes(_make_nested_brave_zip("brave-browser-1.92.134", "brave-browser"))
                sha = hashlib.sha256(zip_dest.read_bytes()).hexdigest()
                dest.write_text(f"{sha}  fake.zip\n", encoding="utf-8")
                # Now move the zip to the right dest.
                dest.parent.mkdir(parents=True, exist_ok=True)
                zip_dest.replace(Path(str(dest).replace(".sha256", "")))
            else:
                dest.write_bytes(_make_nested_brave_zip("brave-browser-1.92.134", "brave-browser"))

        monkeypatch.setattr("automatiq.core.browser_manager._download_file", _nested_download)
        with patch("automatiq.core.browser_manager._detect_platform", return_value=("linux", "amd64")):
            path = ensure_brave(channel="release")
        assert path.exists()
        assert path.name == "brave-browser"
        # The inner dir should have been flattened.
        assert not (path.parent / "brave-browser-1.92.134").exists()


class TestResolveBrowserForRecording:
    """CLI-facing helper: prompt/download/fallback decision tree."""

    def test_explicit_path_wins(self, tmp_path, monkeypatch):
        exe = tmp_path / "my-brave.exe"
        exe.write_text("x")
        monkeypatch.setattr(config, "BROWSER_EXECUTABLE_PATH", str(exe))
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_EXECUTABLE_PATH", str(exe))
        monkeypatch.setattr(config, "BROWSER_TYPE", "brave")
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_TYPE", "brave")
        verb, value, desc = resolve_browser_for_recording()
        assert verb == "browser_executable_path"
        assert value == exe

    def test_chrome_fallback_when_no_auto_download(self, monkeypatch):
        monkeypatch.setattr(config, "BROWSER_EXECUTABLE_PATH", None)
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_EXECUTABLE_PATH", None)
        monkeypatch.setattr(config, "BROWSER_TYPE", "chrome")
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_TYPE", "chrome")
        with patch("shutil.which", return_value=None):
            verb, value, desc = resolve_browser_for_recording(no_auto_download=True)
        assert verb == "browser"
        assert value == "chrome"

    def test_cached_brave_returns_without_prompt(self, clean_browsers_dir, monkeypatch):
        monkeypatch.setattr(config, "BROWSER_EXECUTABLE_PATH", None)
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_EXECUTABLE_PATH", None)
        monkeypatch.setattr(config, "BROWSER_TYPE", "brave")
        monkeypatch.setattr("automatiq.core.browser_manager.config.BROWSER_TYPE", "brave")
        monkeypatch.setattr(sys, "platform", "win32")

        # Create a cached version.
        v_dir = clean_browsers_dir / "brave" / "v1.92.134"
        v_dir.mkdir(parents=True)
        (v_dir / "brave.exe").write_text("brave")

        verb, value, desc = resolve_browser_for_recording(
            no_auto_download=True,
            prompt_callback=lambda _: (_ for _ in ()).throw(RuntimeError("should not be called")),
        )
        assert verb == "browser_executable_path"
        assert "brave.exe" in str(value)
