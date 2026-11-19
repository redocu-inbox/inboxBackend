"""Build the configured LLM provider from application settings.

The hosted model is Claude. `stub`, an unknown provider, and `anthropic`
with an empty key stay on `StubLLM`. A non-empty Anthropic key returns
`AnthropicClient`. `llm_model` is used when it is set. An `openai` value
with `OPENAI_API_KEY` still constructs the old client; that path is not
the backup provider.
"""

from app.config import get_settings
from app.services.llm.base import LLMClient
from app.services.llm.providers import AnthropicClient, OpenAIClient
from app.services.llm.stub import StubLLM

_OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
_ANTHROPIC_DEFAULT_MODEL = "claude-3-5-haiku-latest"


def get_llm() -> LLMClient:
    """Return the configured provider. A missing key stays on the stub."""
    settings = get_settings()
    provider = settings.llm_provider.strip().lower()
    model = settings.llm_model.strip()
    if provider == "openai" and settings.openai_api_key.strip():
        return OpenAIClient(api_key=settings.openai_api_key, model=model or _OPENAI_DEFAULT_MODEL)
    if provider == "anthropic" and settings.anthropic_api_key.strip():
        return AnthropicClient(
            api_key=settings.anthropic_api_key,
            model=model or _ANTHROPIC_DEFAULT_MODEL,
        )
    return StubLLM()
