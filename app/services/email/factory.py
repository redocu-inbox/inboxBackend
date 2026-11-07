import logging

from app.config import get_settings
from app.services.email.base import EmailMessage, EmailSender
from app.services.email.fake import FAKE_SENDER
from app.services.email.resend import ResendEmailSender

logger = logging.getLogger(__name__)


class DisabledEmailSender:
    def send(self, message: EmailMessage) -> None:
        del message
        logger.info("password reset email not sent because EMAIL_PROVIDER=disabled")


def get_email_sender() -> EmailSender:
    provider = get_settings().email_provider.strip().lower()
    if provider in {"", "fake", "console"}:
        return FAKE_SENDER
    if provider == "disabled":
        return DisabledEmailSender()
    if provider == "resend":
        settings = get_settings()
        return ResendEmailSender(settings.email_api_key, settings.email_from)
    raise RuntimeError("EMAIL_PROVIDER is not supported")
