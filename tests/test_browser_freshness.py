"""Browser freshness gate + pre-launch phase visibility.

``ensure_brave``'s cached-launch path must be network-free while the freshness
marker is fresh, bounded (5s timeout) when it does probe, and cache-fallback
on probe failure. ``RecordingSession.status()`` must expose the pre-launch
phase ("starting" before the worker runs).

Seam: ``browser_manager._brave_cache_root()`` reads ``config.BROWSERS_DIR`` at
call time, so monkeypatching that attribute redirects the whole cache (exe,
manifest, marker file) into tmp_path. browser_manager pulls only stdlib +
config + bin_manager (also stdlib + config), so no heavy-import stubs are
needed here.
"""

import json
import time

import pytest

import automatiq.core.browser_manager as browser_manager
from automatiq.core import config
from automatiq.core.bin_manager import _detect_platform
from automatiq.mcp.runtime import RecordingSession

_TAG = "v1.0.0"


@pytest.fixture
def cache_root(monkeypatch, tmp_path):
    """Redirect the Brave cache root (versions + freshness marker) to tmp."""
    root = tmp_path / "browsers"
    monkeypatch.setattr(config, "BROWSERS_DIR", root)
    return root


def _make_cached_browser(root, tag=_TAG):
    """Fabricate a cached Brave install (exe + manifest) and return the exe path."""
    os_name, arch = _detect_platform()
    exe_rel = browser_manager._ASSET_MAP[(os_name, arch)][1]
    version_dir = root / "brave" / tag
    version_dir.mkdir(parents=True, exist_ok=True)
    exe = version_dir / exe_rel
    exe.write_bytes(b"fake brave exe")
    manifest = {"tag": tag, "name": tag, "channel": "release", "published": "2026-01-01T00:00:00Z"}
    (version_dir / ".automatiq-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return exe


def _write_last_check(root, value):
    marker = root / "brave" / ".last-version-check"
    marker.write_text(str(value), encoding="utf-8")
    return marker


def test_freshness_within_window_skips_network(cache_root, monkeypatch):
    """Fresh marker -> cached exe returned with ZERO network."""
    exe = _make_cached_browser(cache_root)
    _write_last_check(cache_root, time.time())

    def no_network(*args, **kwargs):
        raise AssertionError("network hit during fresh-cache launch")

    monkeypatch.setattr(browser_manager, "_fetch_versions", no_network)
    assert browser_manager.ensure_brave() == exe


def test_freshness_stale_checks_and_touches(cache_root, monkeypatch):
    """Stale marker -> bounded probe; tag match returns cache and touches marker."""
    exe = _make_cached_browser(cache_root)
    before = time.time() - 8 * 86400
    marker = _write_last_check(cache_root, before)

    def fake_fetch(timeout=30.0):
        # The cached-launch probe must be bounded (5s), not the 30s default.
        assert timeout == browser_manager._VERSION_CHECK_TIMEOUT
        return [{"tag": _TAG, "channel": "release", "published": "2026-01-01T00:00:00Z"}]

    monkeypatch.setattr(browser_manager, "_fetch_versions", fake_fetch)
    assert browser_manager.ensure_brave() == exe
    assert float(marker.read_text(encoding="utf-8")) > before  # touched on success


def test_versions_fetch_failure_falls_back_to_cache(cache_root, monkeypatch):
    """Probe failure -> cached exe, marker untouched (retry next launch)."""
    exe = _make_cached_browser(cache_root)
    before = time.time() - 8 * 86400
    marker = _write_last_check(cache_root, before)

    def failing_fetch(timeout=30.0):
        raise OSError("network down")

    monkeypatch.setattr(browser_manager, "_fetch_versions", failing_fetch)
    assert browser_manager.ensure_brave() == exe
    assert float(marker.read_text(encoding="utf-8")) == before  # not touched


def test_status_includes_phase(tmp_output_root):
    """A session that has not started reports phase 'starting'."""
    session = RecordingSession(
        url="about:blank",
        session_name="t",
        proxy=None,
        include_video=False,
        output_root=tmp_output_root,
        vision_preflight_result={"configured": False, "model": None},
    )
    assert session.status()["phase"] == "starting"
