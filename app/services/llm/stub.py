"""Deterministic LLM stand-in for local development and tests."""

import logging

logger = logging.getLogger(__name__)


class StubLLM:
    async def complete(self, *, system: str, user: str) -> str:
        """Log the prompt lengths and return a fixed string."""
        logger.info(
            "stub llm, complete (system_len=%s user_len=%s)",
            len(system),
            len(user),
        )
        return "[stub llm]"