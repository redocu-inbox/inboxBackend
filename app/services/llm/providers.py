"""Thin OpenAI and Anthropic clients.

`complete` matches `LLMClient`. The key stays in a header. Errors log a status
or exception type and do not include the prompt, the key, or the response body.
"""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


class OpenAIClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self.model = model

    async def complete(self, *, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        body = _post_json(
            _OPENAI_URL,
            payload,
            {"Authorization": f"Bearer {self._api_key}"},
            provider="openai",
        )
        return _openai_text(body)


class AnthropicClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self.model = model

    async def complete(self, *, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        body = _post_json(
            _ANTHROPIC_URL,
            payload,
            {"x-api-key": self._api_key, "anthropic-version": _ANTHROPIC_VERSION},
            provider="anthropic",
        )
        return _anthropic_text(body)


def _post_json(url: str, payload: dict, headers: dict[str, str], *, provider: str) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except HTTPError as exc:
        exc.read()
        logger.warning("llm request failed provider=%s status=%s", provider, exc.code)
        raise RuntimeError("llm_unavailable") from None
    except (URLError, TimeoutError) as exc:
        logger.warning("llm request failed provider=%s error_type=%s", provider, type(exc).__name__)
        raise RuntimeError("llm_unavailable") from None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("llm request failed provider=%s error_type=JSONDecodeError", provider)
        raise RuntimeError("llm_unavailable") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("llm_unavailable")
    return parsed


def _openai_text(payload: dict) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("llm_unavailable")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("llm_unavailable")
    return content


def _anthropic_text(payload: dict) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        raise RuntimeError("llm_unavailable")
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return text
    raise RuntimeError("llm_unavailable")
