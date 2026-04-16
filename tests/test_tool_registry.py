"""Tests for tool_registry.py."""

import os
import textwrap
import pytest
import yaml

from app.tool_registry import ToolRegistry


SAMPLE_YAML = textwrap.dedent("""\
    tools:
      - name: lookup_order_status
        description: "Check order status"
        parameters:
          type: object
          properties:
            order_id: {type: string}
          required: [order_id]
        dify:
          endpoint: workflow
          base_url: https://api.dify.ai/v1
          api_key_env: TEST_DIFY_KEY
          input_mapping:
            order_id: order_id
          user_field: "{user_id}"
        synthetic_ack: "Checking now."
""")


@pytest.fixture
def registry_with_yaml(tmp_path):
    cfg = tmp_path / "tools.yaml"
    cfg.write_text(SAMPLE_YAML)
    r = ToolRegistry()
    os.environ["TEST_DIFY_KEY"] = "app-test-key"
    r.load(str(cfg))
    yield r
    del os.environ["TEST_DIFY_KEY"]


def test_loads_tool(registry_with_yaml):
    assert registry_with_yaml.is_dify_tool("lookup_order_status")


def test_openai_schema_shape(registry_with_yaml):
    tools = registry_with_yaml.build_openai_tools()
    assert len(tools) == 1
    schema = tools[0]
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "lookup_order_status"
    assert "parameters" in schema["function"]


def test_api_key_resolved_from_env(registry_with_yaml):
    tool = registry_with_yaml.get_tool("lookup_order_status")
    assert tool.dify_api_key == "app-test-key"


def test_input_mapping(registry_with_yaml):
    tool = registry_with_yaml.get_tool("lookup_order_status")
    result = tool.build_dify_inputs({"order_id": "ORD-999"})
    assert result == {"order_id": "ORD-999"}


def test_user_field_template(registry_with_yaml):
    tool = registry_with_yaml.get_tool("lookup_order_status")
    assert tool.format_user(user_id="alice") == "alice"


def test_missing_config_file():
    r = ToolRegistry()
    r.load("/nonexistent/path/tools.yaml")
    assert r.build_openai_tools() == []


def test_unknown_tool_returns_none(registry_with_yaml):
    assert registry_with_yaml.get_tool("nonexistent") is None
