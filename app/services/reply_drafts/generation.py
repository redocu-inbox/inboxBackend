"""Prompt and body for one local reply.

The stub provider stores a fixed sentence. That sentence is not the
`[stub llm]` marker `StubLLM.complete` returns. A configured provider stores
the model text. Callers pass strings copied out of the database session so
the model call does not keep that session open.
"""

from app.services.graph.html_text import MAX_PLAIN_TEXT_CHARS

STUB_REPLY_SENTENCE = "Thanks for your message. I will review it and reply shortly."


def build_reply_prompts(
    *,
    subject: str | None,
    body_text: str | None,
    body_preview: str | None,
) -> tuple[str, str]:
    """System and user prompts from already-loaded message text."""
    system = (
        "Draft a short reply to this email. Use only facts from the email. "
        "Return the reply text and nothing else."
    )
    body = body_text if body_text else body_preview
    user = f"Subject: {subject or ''}\n\n{body or ''}"
    return system, user


def final_reply_body(provider: str, model_text: str) -> str:
    """The text to store. `stub` ignores the model marker and uses the fixed sentence."""
    if provider.strip().lower() == "stub":
        return STUB_REPLY_SENTENCE
    if not isinstance(model_text, str):
        raise ValueError("empty model text")
    text = model_text.strip()
    if not text or len(text) > MAX_PLAIN_TEXT_CHARS:
        raise ValueError("empty model text")
    return text
