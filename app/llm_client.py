"""
Thin wrapper around the AsyncOpenAI client.

Reads base_url, api_key, and default model from settings so any
OpenAI-compatible endpoint (Azure OpenAI, Groq, Ollama, OpenRouter, etc.)
can be used by changing env vars only.
"""

from openai import AsyncOpenAI

from app.settings import get_settings


def get_client() -> AsyncOpenAI:
    """Return a configured AsyncOpenAI client instance."""
    settings = get_settings()
    kwargs = dict(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    if settings.openai_api_version:
        kwargs["default_query"] = {"api-version": settings.openai_api_version}
    return AsyncOpenAI(**kwargs)
