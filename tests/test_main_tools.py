"""Tests for plugin LLM tool registration."""

from unittest.mock import Mock

from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.tools import (
    MemoryForgetTool,
    MemoryMemorizeTool,
    MemorySearchTool,
)
from astrbot_plugin_anamnesis.main import (
    AnamnesisPlugin,
    _parse_version,
    _version_lt,
)


def test_parse_version_accepts_source_and_prerelease_versions():
    assert _parse_version("4.25.2") == (4, 25, 2)
    assert _parse_version("v4.25.2-beta.1") == (4, 25, 2)
    assert _parse_version("not-a-version") == ()


def test_version_lt_pads_version_segments():
    assert _version_lt("4.24.1", "4.24.2") is True
    assert _version_lt("4.25", "4.25.0") is False
    assert _version_lt("4.25.2", "4.24.2") is False
    assert _version_lt("unknown", "4.24.2") is False


def test_register_llm_tools_is_idempotent():
    plugin = AnamnesisPlugin.__new__(AnamnesisPlugin)
    plugin.context = Mock()
    plugin.config_manager = ConfigManager(
        {"agent_tools": {"enable_recall_tool": True, "enable_memorize_tool": True}}
    )
    plugin.initializer = Mock()
    plugin.initializer.memory_engine = Mock()
    plugin.initializer.memory_processor = Mock()
    plugin._llm_tools_registered = False

    plugin._register_agent_tools_if_needed()
    plugin._register_agent_tools_if_needed()

    plugin.context.add_llm_tools.assert_called_once()
    tools = plugin.context.add_llm_tools.call_args.args
    tools_by_name = {tool.name: tool for tool in tools}
    assert set(tools_by_name) == {
        "anamnesis_recall_memory",
        "anamnesis_memorize_memory",
    }
    assert isinstance(tools_by_name["anamnesis_recall_memory"], MemorySearchTool)
    assert isinstance(tools_by_name["anamnesis_memorize_memory"], MemoryMemorizeTool)
    assert plugin._llm_tools_registered is True


def test_register_llm_tools_defaults_only_recall():
    plugin = AnamnesisPlugin.__new__(AnamnesisPlugin)
    plugin.context = Mock()
    plugin.config_manager = ConfigManager()
    plugin.initializer = Mock()
    plugin.initializer.memory_engine = Mock()
    plugin.initializer.memory_processor = Mock()
    plugin._llm_tools_registered = False

    plugin._register_agent_tools_if_needed()

    plugin.context.add_llm_tools.assert_called_once()
    tools = plugin.context.add_llm_tools.call_args.args
    assert [tool.name for tool in tools] == ["anamnesis_recall_memory"]
    assert isinstance(tools[0], MemorySearchTool)
    assert plugin._llm_tools_registered is True


def test_register_llm_tools_no_memory_engine():
    plugin = AnamnesisPlugin.__new__(AnamnesisPlugin)
    plugin.context = Mock()
    plugin.config_manager = ConfigManager()
    plugin.initializer = Mock()
    plugin.initializer.memory_engine = None
    plugin.initializer.memory_processor = Mock()
    plugin._llm_tools_registered = False

    plugin._register_agent_tools_if_needed()

    plugin.context.add_llm_tools.assert_not_called()
    assert plugin._llm_tools_registered is False


def test_register_llm_tools_no_memory_processor():
    plugin = AnamnesisPlugin.__new__(AnamnesisPlugin)
    plugin.context = Mock()
    plugin.config_manager = ConfigManager()
    plugin.initializer = Mock()
    plugin.initializer.memory_engine = Mock()
    plugin.initializer.memory_processor = None
    plugin._llm_tools_registered = False

    plugin._register_agent_tools_if_needed()

    plugin.context.add_llm_tools.assert_not_called()
    assert plugin._llm_tools_registered is False


def test_register_llm_tools_respects_recall_tool_disabled():
    plugin = AnamnesisPlugin.__new__(AnamnesisPlugin)
    plugin.context = Mock()
    plugin.config_manager = ConfigManager(
        {"agent_tools": {"enable_recall_tool": False, "enable_memorize_tool": True}}
    )
    plugin.initializer = Mock()
    plugin.initializer.memory_engine = Mock()
    plugin.initializer.memory_processor = Mock()
    plugin._llm_tools_registered = False

    plugin._register_agent_tools_if_needed()

    plugin.context.add_llm_tools.assert_called_once()
    tools = plugin.context.add_llm_tools.call_args.args
    assert [tool.name for tool in tools] == ["anamnesis_memorize_memory"]
    assert isinstance(tools[0], MemoryMemorizeTool)
    assert plugin._llm_tools_registered is True


def test_register_llm_tools_respects_memorize_tool_disabled():
    plugin = AnamnesisPlugin.__new__(AnamnesisPlugin)
    plugin.context = Mock()
    plugin.config_manager = ConfigManager(
        {"agent_tools": {"enable_recall_tool": True, "enable_memorize_tool": False}}
    )
    plugin.initializer = Mock()
    plugin.initializer.memory_engine = Mock()
    plugin.initializer.memory_processor = Mock()
    plugin._llm_tools_registered = False

    plugin._register_agent_tools_if_needed()

    plugin.context.add_llm_tools.assert_called_once()
    tools = plugin.context.add_llm_tools.call_args.args
    assert [tool.name for tool in tools] == ["anamnesis_recall_memory"]
    assert isinstance(tools[0], MemorySearchTool)
    assert plugin._llm_tools_registered is True


def test_register_llm_tools_respects_all_tools_disabled():
    plugin = AnamnesisPlugin.__new__(AnamnesisPlugin)
    plugin.context = Mock()
    plugin.config_manager = ConfigManager(
        {"agent_tools": {"enable_recall_tool": False, "enable_memorize_tool": False}}
    )
    plugin.initializer = Mock()
    plugin.initializer.memory_engine = Mock()
    plugin.initializer.memory_processor = Mock()
    plugin._llm_tools_registered = False

    plugin._register_agent_tools_if_needed()

    plugin.context.add_llm_tools.assert_not_called()
    assert plugin._llm_tools_registered is True


def test_register_llm_tools_forget_is_opt_in():
    plugin = AnamnesisPlugin.__new__(AnamnesisPlugin)
    plugin.context = Mock()
    plugin.config_manager = ConfigManager(
        {
            "agent_tools": {
                "enable_recall_tool": False,
                "enable_memorize_tool": False,
                "enable_forget_tool": True,
            }
        }
    )
    plugin.initializer = Mock()
    plugin.initializer.memory_engine = Mock()
    plugin.initializer.memory_processor = Mock()
    plugin.user_profile_manager = Mock()
    plugin._llm_tools_registered = False

    plugin._register_agent_tools_if_needed()

    plugin.context.add_llm_tools.assert_called_once()
    tools = plugin.context.add_llm_tools.call_args.args
    assert len(tools) == 1
    assert isinstance(tools[0], MemoryForgetTool)
    assert tools[0].user_profile_manager is plugin.user_profile_manager
