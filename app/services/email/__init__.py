"""Outbound mail. Production cannot use the fake or console sender."""

from app.services.email.base import EmailDeliveryError, EmailMessage, EmailSender
from app.services.email.factory import get_email_sender

__all__ = ["EmailDeliveryError", "EmailMessage", "EmailSender", "get_email_sender"]
