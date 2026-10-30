"""Build the configured LLM provider from application settings."""

from app.config import get_settings
from app.services.llm.base import LLMClient
from app.services.llm.stub import StubLLM

def get_llm() -> LLMClient:
    """Return the configured provider. Only `stub` is implemented."""
    provider = get_settings().llm_provider
    if provider == "stub":
        return StubLLM()
    raise NotImplementedError(
        f"LLM provider {provider!r} is not implemented yet. Use 'stub'."
    )
