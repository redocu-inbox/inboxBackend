from dataclasses import dataclass


class EmailDeliveryError(Exception):
    """Delivery failed. The message must not contain the link, token, or recipient body."""


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str


class EmailSender:
    def send(self, message: EmailMessage) -> None:
        raise NotImplementedError
