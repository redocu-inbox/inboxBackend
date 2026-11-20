"""Verify and remove one Stripe customer without retaining provider responses."""

import json
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID

_CUSTOMERS_URL = "https://api.stripe.com/v1/customers"
_TIMEOUT_SECONDS = 10


class StripeCleanupError(Exception):
    """An incomplete Stripe cleanup with a stable, non-sensitive error code."""


def ensure_stripe_customer_deleted(api_key: str, customer_id: str, user_id: UUID) -> None:
    """Check the exact customer's state and ownership before deleting it."""
    if not re.fullmatch(r"cus_[A-Za-z0-9_]+", customer_id):
        raise StripeCleanupError("invalid_customer_id")
    customer = _stripe_customer("GET", api_key, customer_id)
    if customer.get("deleted") is True:
        return

    metadata = customer.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("user_id") != str(user_id):
        raise StripeCleanupError("customer_owner_mismatch")
    if _stripe_customer("DELETE", api_key, customer_id).get("deleted") is not True:
        raise StripeCleanupError("deletion_unconfirmed")


def _stripe_customer(method: str, api_key: str, customer_id: str) -> dict:
    url = f"{_CUSTOMERS_URL}/{customer_id}"
    request = Request(url, headers={"Accept": "application/json"}, method=method)
    request.add_unredirected_header("Authorization", f"Bearer {api_key}")
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            if response.geturl() != url:
                raise StripeCleanupError("unexpected_redirect")
            payload = json.loads(response.read())
    except HTTPError as exc:
        exc.close()
        raise StripeCleanupError(f"http_{exc.code}") from None
    except OSError:
        raise StripeCleanupError("connection_failed") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise StripeCleanupError("invalid_response") from None
    if not isinstance(payload, dict) or payload.get("id") != customer_id or payload.get("object") != "customer":
        raise StripeCleanupError("unexpected_customer")
    return payload
