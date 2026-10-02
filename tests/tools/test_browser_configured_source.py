"""Explicit source-browser routing without launching or copying a browser."""
import pytest
from tools import browser_tool as bt
from tools import browser_tool_cloud as cloud
from tools import browser_tool_lightpanda_fallback as lightpanda
from tools import browser_tool_real_profile as real_profile
from hermes_cli import browser_connect


@pytest.mark.parametrize(("configured", "expected"), [("chrome", "chrome"), ("default", "edge"), ("auto", "edge"), ("firefox", "firefox")])
def test_source_browser_is_resolved_before_any_snapshot(monkeypatch, configured, expected):
    monkeypatch.setattr(bt, "_real_profile_cdp_cache", {})
    monkeypatch.setattr(bt, "_browser_cfg", lambda key, default, *args: configured if key == "real_profile_browser" else default)
    monkeypatch.setattr(cloud, "_use_real_profile", lambda: True)
    monkeypatch.setattr(lightpanda, "_using_lightpanda_engine", lambda: False)
    monkeypatch.setattr(browser_connect, "detect_default_chromium", lambda: "edge")
    # Stop at the validation boundary; no real data-directory or process is touched.
    monkeypatch.setattr(real_profile, "_real_profile_unsupported_reason", lambda browser: "stop:" + browser)
    assert real_profile._real_profile_cdp() == (None, "stop:" + expected)
