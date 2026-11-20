"""Build the local stub or the explicitly configured Claude client."""

from app.config import get_settings
from app.services.llm.base import LLMClient
from app.services.llm.providers import AnthropicClient
from app.services.llm.stub import StubLLM

_ANTHROPIC_DEFAULT_MODEL = "claude-3-5-haiku-latest"


def get_llm() -> LLMClient:
    settings = get_settings()

    if settings.llm_provider == "anthropic" and settings.anthropic_api_key.strip():
        return AnthropicClient(
            api_key=settings.anthropic_api_key,
            model=settings.llm_model.strip() or _ANTHROPIC_DEFAULT_MODEL,
        )
    return StubLLM()
