"""In-memory sender for tests and local development. It does not log the message."""

from app.services.email.base import EmailMessage


class FakeEmailSender:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)

    def clear(self) -> None:
        self.messages.clear()


FAKE_SENDER = FakeEmailSender()
