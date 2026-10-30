"""The interface the rest of the app uses to call an LLM."""

from typing import Protocol

class LLMClient(Protocol):
    async def complete(self, *, system: str, user: str) -> str:
        """Return the model text."""