"""Tests for the recorder proxy resolver and BrowserAgent proxy wiring.

``_resolve_proxy()`` is a pure function with four precedence branches:
``--no-proxy`` > ``--proxy`` > dynamic provider > static server.
``BrowserAgent(proxy=...)`` stores the proxy URL for the browser launch.
No Chrome, no network — config globals are patched per-test.
"""

from unittest.mock import MagicMock, patch

from automatiq.core import config
from automatiq.core.recorder import _resolve_proxy
from automatiq.core.recorder.browser_agent import BrowserAgent


class TestResolveProxyNoProxy:
    """``--no-proxy`` always wins, regardless of other settings."""

    def test_no_proxy_returns_none(self):
        assert _resolve_proxy(no_proxy=True) is None

    def test_no_proxy_overrides_explicit_proxy(self):
        assert _resolve_proxy(proxy="http://override:3128", no_proxy=True) is None

    def test_no_proxy_overrides_enabled_config(self):
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_SERVER", "http://static:3128"),
        ):
            assert _resolve_proxy(no_proxy=True) is None


class TestResolveProxyExplicitOverride:
    """``--proxy URL`` takes precedence over config (provider and static)."""

    def test_explicit_proxy_returned_directly(self):
        assert _resolve_proxy(proxy="http://override:3128") == "http://override:3128"

    def test_explicit_proxy_ignores_enabled_config(self):
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_SERVER", "http://static:3128"),
            patch.object(config, "RECORDER_PROXY_PROVIDER", "myprov:rotate"),
        ):
            assert _resolve_proxy(proxy="socks5://override:1080") == "socks5://override:1080"


class TestResolveProxyDisabled:
    """When proxying is disabled and no override given, returns None."""

    def test_disabled_returns_none(self):
        with patch.object(config, "RECORDER_PROXY_ENABLED", False):
            assert _resolve_proxy() is None

    def test_disabled_ignores_static_server(self):
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", False),
            patch.object(config, "RECORDER_PROXY_SERVER", "http://static:3128"),
        ):
            assert _resolve_proxy() is None


class TestResolveProxyStaticServer:
    """Enabled with no provider returns the static server URL."""

    def test_static_server_returned(self):
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_SERVER", "http://static:3128"),
            patch.object(config, "RECORDER_PROXY_PROVIDER", None),
        ):
            assert _resolve_proxy() == "http://static:3128"

    def test_static_server_none_when_unset(self):
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_SERVER", None),
            patch.object(config, "RECORDER_PROXY_PROVIDER", None),
        ):
            assert _resolve_proxy() is None


class TestResolveProxyProvider:
    """Dynamic provider is called and its return value used."""

    def test_provider_returns_url(self):
        mock_module = MagicMock()
        mock_module.rotate.return_value = "http://dynamic:3128"
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_PROVIDER", "myprov:rotate"),
            patch("automatiq.core.recorder.importlib.import_module", return_value=mock_module),
        ):
            assert _resolve_proxy() == "http://dynamic:3128"

    def test_provider_takes_precedence_over_static(self):
        mock_module = MagicMock()
        mock_module.rotate.return_value = "http://dynamic:3128"
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_SERVER", "http://static:3128"),
            patch.object(config, "RECORDER_PROXY_PROVIDER", "myprov:rotate"),
            patch("automatiq.core.recorder.importlib.import_module", return_value=mock_module),
        ):
            assert _resolve_proxy() == "http://dynamic:3128"


class TestResolveProxyProviderFallback:
    """When the provider fails or returns empty, falls back to static server."""

    def test_provider_raises_falls_back_to_static(self):
        mock_module = MagicMock()
        mock_module.rotate.side_effect = RuntimeError("rotation API down")
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_SERVER", "http://static:3128"),
            patch.object(config, "RECORDER_PROXY_PROVIDER", "myprov:rotate"),
            patch("automatiq.core.recorder.importlib.import_module", return_value=mock_module),
        ):
            assert _resolve_proxy() == "http://static:3128"

    def test_provider_returns_none_falls_back_to_static(self):
        mock_module = MagicMock()
        mock_module.rotate.return_value = None
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_SERVER", "http://static:3128"),
            patch.object(config, "RECORDER_PROXY_PROVIDER", "myprov:rotate"),
            patch("automatiq.core.recorder.importlib.import_module", return_value=mock_module),
        ):
            assert _resolve_proxy() == "http://static:3128"

    def test_provider_returns_empty_string_falls_back_to_static(self):
        mock_module = MagicMock()
        mock_module.rotate.return_value = ""
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_SERVER", "http://static:3128"),
            patch.object(config, "RECORDER_PROXY_PROVIDER", "myprov:rotate"),
            patch("automatiq.core.recorder.importlib.import_module", return_value=mock_module),
        ):
            assert _resolve_proxy() == "http://static:3128"

    def test_invalid_provider_format_falls_back_to_static(self):
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_SERVER", "http://static:3128"),
            patch.object(config, "RECORDER_PROXY_PROVIDER", "no_colon_here"),
        ):
            assert _resolve_proxy() == "http://static:3128"

    def test_provider_module_not_found_falls_back_to_static(self):
        with (
            patch.object(config, "RECORDER_PROXY_ENABLED", True),
            patch.object(config, "RECORDER_PROXY_SERVER", "http://static:3128"),
            patch.object(config, "RECORDER_PROXY_PROVIDER", "nonexistent_module:rotate"),
            patch(
                "automatiq.core.recorder.importlib.import_module",
                side_effect=ModuleNotFoundError("No module named 'nonexistent_module'"),
            ),
        ):
            assert _resolve_proxy() == "http://static:3128"


class TestBrowserAgentProxy:
    """BrowserAgent stores the proxy URL for browser launch."""

    def test_proxy_stored_on_init(self):
        agent = BrowserAgent(blocklist=None, proxy="http://test:3128")
        assert agent.proxy == "http://test:3128"

    def test_default_proxy_is_none(self):
        agent = BrowserAgent(blocklist=None)
        assert agent.proxy is None
