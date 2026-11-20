import asyncio
import io
import json
from urllib.error import HTTPError
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlmodel import Session

from app.config import get_settings
from app.db import engine
from app.models.account_deletion import AccountDeletion
from app.models.user import User
from app.services import stripe_cleanup
from app.worker.settings import WorkerSettings
from app.worker.tasks import purge_deleted_accounts, purge_deleted_stripe_customers

_CUSTOMER_ID = "cus_accountDeletion123"


class _StripeResponse(io.BytesIO):
    def __init__(self, url, payload):
        super().__init__(json.dumps(payload).encode())
        self.url = url

    def geturl(self):
        return self.url


def _stage_deletion(client, register_user, *, link_before_request=True):
    headers = register_user("stripe@example.com")
    user_id = UUID(client.get("/users/me", headers=headers).json()["id"])
    if not link_before_request:
        assert client.delete("/users/me", headers=headers).status_code == 202
    with Session(engine) as session:
        user = session.get(User, user_id)
        user.stripe_customer_id = _CUSTOMER_ID
        session.commit()
    if link_before_request:
        assert "stripe_customer_id" not in client.get("/users/me", headers=headers).json()
        assert client.delete("/users/me", headers=headers).status_code == 202
    return user_id


@pytest.mark.parametrize("stripe_received_delete", [False, True])
def test_stripe_cleanup_retries_after_local_purge(client, register_user, monkeypatch, stripe_received_delete):
    user_id = _stage_deletion(client, register_user)
    asyncio.run(purge_deleted_accounts({}))

    monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr("sk_test_example"))
    customer = {"id": _CUSTOMER_ID, "object": "customer"}
    customer_deleted = False
    methods = []

    def stripe_api(request, *, timeout):
        nonlocal customer_deleted
        assert request.full_url == f"https://api.stripe.com/v1/customers/{_CUSTOMER_ID}"
        assert request.get_header("Authorization") == "Bearer sk_test_example"
        assert timeout > 0
        methods.append(request.method)
        if request.method == "DELETE":
            if methods.count("DELETE") == 1:
                customer_deleted = stripe_received_delete
                raise TimeoutError
            customer_deleted = True
        payload = {**customer, "deleted": True} if customer_deleted else {
            **customer, "metadata": {"user_id": str(user_id)},
        }
        return _StripeResponse(request.full_url, payload)

    monkeypatch.setattr(stripe_cleanup, "urlopen", stripe_api)
    asyncio.run(purge_deleted_stripe_customers({}))
    with Session(engine) as session:
        assert session.get(User, user_id) is None
        pending = session.get(AccountDeletion, user_id)
        assert pending.local_purged_at is not None
        assert pending.stripe_customer_id == _CUSTOMER_ID
        assert pending.stripe_deleted_at is None

    asyncio.run(purge_deleted_stripe_customers({}))
    asyncio.run(purge_deleted_stripe_customers({}))

    with Session(engine) as session:
        completed = session.get(AccountDeletion, user_id)
        assert completed.stripe_deleted_at is not None
        assert completed.stripe_customer_id is None
    assert methods == (["GET", "DELETE", "GET"] if stripe_received_delete else ["GET", "DELETE", "GET", "DELETE"])


@pytest.mark.parametrize("outcome", ["wrong_owner", "missing_customer"])
def test_stripe_cleanup_requires_confirmation(client, register_user, monkeypatch, outcome):
    user_id = _stage_deletion(client, register_user)
    monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr("sk_test_example"))
    methods = []

    def stripe_api(request, *, timeout):
        methods.append(request.method)
        if outcome == "missing_customer":
            raise HTTPError(request.full_url, 404, "missing customer", {}, None)
        return _StripeResponse(request.full_url, {
            "id": _CUSTOMER_ID, "object": "customer", "metadata": {"user_id": str(uuid4())},
        })

    monkeypatch.setattr(stripe_cleanup, "urlopen", stripe_api)
    asyncio.run(purge_deleted_stripe_customers({}))

    with Session(engine) as session:
        pending = session.get(AccountDeletion, user_id)
        assert pending.stripe_customer_id == _CUSTOMER_ID
        assert pending.stripe_deleted_at is None
    assert methods == ["GET"]


def test_local_purge_preserves_a_backfilled_customer_without_stripe_credentials(client, register_user, monkeypatch):
    user_id = _stage_deletion(client, register_user, link_before_request=False)
    monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr(""))

    def deny_network(*_args, **_kwargs):
        pytest.fail("The worker called Stripe without credentials.")

    monkeypatch.setattr(stripe_cleanup, "urlopen", deny_network)
    asyncio.run(purge_deleted_accounts({}))
    asyncio.run(purge_deleted_stripe_customers({}))

    with Session(engine) as session:
        assert session.get(User, user_id) is None
        pending = session.get(AccountDeletion, user_id)
        assert pending.local_purged_at is not None
        assert pending.stripe_deleted_at is None
        assert pending.stripe_customer_id == _CUSTOMER_ID


def test_stripe_cleanup_is_scheduled_at_startup_and_every_minute():
    job = next(job for job in WorkerSettings.cron_jobs if job.coroutine is purge_deleted_stripe_customers)
    assert job.run_at_startup
    assert job.minute is None
    assert job.second == 0
