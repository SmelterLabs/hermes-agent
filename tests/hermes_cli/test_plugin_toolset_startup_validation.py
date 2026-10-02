"""CLI startup accepts plugin and MCP names before registry discovery completes."""
import cli
from hermes_cli import plugins


def test_cli_warning_excludes_declared_plugins_and_mcp(monkeypatch):
    monkeypatch.setattr(cli, "CLI_CONFIG", {"agent": {}, "mcp_servers": {"fixture_mcp": {}}})
    monkeypatch.setattr(plugins, "get_plugin_toolset_keys_nowait", lambda: {"fixture_plugin"})
    shell = object.__new__(cli.HermesCLI)
    printed = []
    shell._console_print = printed.append
    shell._init_toolsets(["fixture_plugin", "fixture_mcp", "fixture_typo"])
    assert len(printed) == 1
    assert "fixture_typo" in printed[0]
    assert "fixture_plugin" not in printed[0]
    assert "fixture_mcp" not in printed[0]
