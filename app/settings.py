import os
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Upstream LLM (OpenAI-compatible)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    openai_api_version: str = ""  # Azure OpenAI only; passed as ?api-version= query param

    # Agora (app context for session keying)
    agora_app_id: str = ""
    agora_app_certificate: str = ""

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    tools_config: str = "config/tools.yaml"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
