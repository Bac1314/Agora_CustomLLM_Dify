"""
YAML-driven Dify tool registry.

Loads config/tools.yaml at startup and provides:
  - build_openai_tools()  → list of OpenAI-format tool schemas to inject
  - get_tool(name)        → tool config dict or None
  - is_dify_tool(name)    → bool
"""

import logging
import os
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


class ToolDef:
    """Parsed tool definition from YAML."""

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.name: str = raw["name"]
        self.description: str = raw.get("description", "")
        self.parameters: Dict[str, Any] = raw.get("parameters", {"type": "object", "properties": {}})
        self.synthetic_ack: str = raw.get("synthetic_ack", "I'm working on that, I'll get back to you shortly.")
        self.mode: str = raw.get("mode", "async")  # "sync" | "async"

        dify = raw.get("dify", {})
        self.dify_endpoint: str = dify.get("endpoint", "workflow")  # "workflow" | "chat"
        self.dify_base_url: str = dify.get("base_url", "https://api.dify.ai/v1")
        # api_key loaded from env; never store secret in the object directly
        api_key_env: str = dify.get("api_key_env", "")
        self.dify_api_key: str = os.environ.get(api_key_env, "") if api_key_env else ""
        if api_key_env and not self.dify_api_key:
            logger.warning("Tool '%s': env var '%s' is not set — Dify calls will fail", self.name, api_key_env)
        self.input_mapping: Dict[str, str] = dify.get("input_mapping", {})
        self.user_field_template: str = dify.get("user_field", "{user_id}")

    def build_dify_inputs(self, llm_args: Dict[str, Any]) -> Dict[str, Any]:
        """Map LLM function arguments to Dify workflow inputs per input_mapping."""
        if self.input_mapping:
            return {dify_key: llm_args.get(llm_key) for llm_key, dify_key in self.input_mapping.items()}
        return llm_args

    def format_user(self, user_id: str = "", channel_name: str = "") -> str:
        return self.user_field_template.format(user_id=user_id or "unknown", channel_name=channel_name or "unknown")

    def to_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip(),
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolDef] = {}

    def load(self, config_path: str) -> None:
        """Load tools from YAML file. Safe to call multiple times (replaces previous state)."""
        try:
            with open(config_path, "r") as f:
                raw = yaml.safe_load(f) or {}
            tools_raw: List[Dict[str, Any]] = raw.get("tools", [])
            self._tools = {t["name"]: ToolDef(t) for t in tools_raw}
            logger.info("ToolRegistry: loaded %d tool(s) from %s", len(self._tools), config_path)
        except FileNotFoundError:
            logger.warning("ToolRegistry: config file not found: %s — no Dify tools registered", config_path)
            self._tools = {}
        except Exception:
            logger.exception("ToolRegistry: failed to load %s", config_path)
            self._tools = {}

    def is_dify_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def build_openai_tools(self) -> List[Dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]


# Module-level singleton; load() called in app lifespan.
registry = ToolRegistry()
