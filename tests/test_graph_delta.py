"""Phase 11: Outlook delta sync, subscriptions, and the Graph webhook."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.main import app
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.services.graph.client import INBOX_DELTA_URL
from app.services.graph.subscriptions import GraphSubscriptionError
from app.worker.mail_tasks import renew_graph_subscriptions, sync_mailbox_delta
from tests.test_frontend_connect import _add_message, _user_id
from tests.test_mailbox_reliability import GRAPH_ITEM, _future, _gmail_item

_STORED = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=stored"
_PAGE_TWO = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$skiptoken=page2"


def _outlook(user_id: str, *, subscription_id: str | None = None, expires_in_hours: int | None = None) -> str:
    expires = None
    if expires_in_hours is not None:
        expires = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
    with Session(engine) as session:
        account = ConnectedAccount(
            user_id=UUID(user_id),
            provider="microsoft",
            provider_account_id=f"oid-{user_id[:8]}",
            email="ana@outlook.com",
            access_token="access",
            refresh_token="refresh",
            token_expires_at=_future(),
            sync_generation=1,
            sync_status="pending",
            graph_subscription_id=subscription_id,
            graph_subscription_expires_at=expires,
        )
        session.add(account)
        session.commit()
        return str(account.id)


def _gmail(user_id: str) -> str:
    with Session(engine) as session:
        account = ConnectedAccount(
            user_id=UUID(user_id),
            provider="google",
            provider_account_id=f"sub-{user_id[:8]}",
            email="ana@gmail.com",
            access_token="google-access",
            refresh_token="google-refresh",
            token_expires_at=_future(),
            sync_generation=1,
            sync_status="pending",
        )
        session.add(account)
        session.commit()
        return str(account.id)


def _run(account_or_subscription: str, generation: int | None = 1) -> None:
    asyncio.run(sync_mailbox_delta({"job_id": "job-1"}, account_or_subscription, generation))


def test_delta_link_is_stored_and_reused(client, register_user, monkeypatch):
    headers = register_user("delta@example.com")
    account_id = _outlook(_user_id(client, headers))
    calls: list[str] = []

    def graph_get(url, token):
        assert token == "access"
        calls.append(url)
        if url == INBOX_DELTA_URL:
            return {
                "value": [GRAPH_ITEM],
                "@odata.nextLink": _PAGE_TWO,
                "@odata.deltaLink": _STORED,
            }
        return {"value": []}

    monkeypatch.setattr("app.worker.mail_tasks._graph_get", graph_get)
    monkeypatch.setattr(
        "app.worker.mail_tasks.create_graph_subscription",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subscription created")),
    )
    _run(account_id)
    assert calls == [INBOX_DELTA_URL, _PAGE_TWO]
    with Session(engine) as session:
        assert session.get(ConnectedAccount, UUID(account_id)).delta_link == _STORED
    assert client.get("/messages", headers=headers).json()[0]["subject"] == "Invoice 12"

    calls.clear()
    _run(account_id)
    assert calls == [_STORED]


def test_removed_item_is_deleted_and_page_21_is_not_requested(client, register_user, monkeypatch, caplog):
    headers = register_user("removed@example.com")
    user_id = _user_id(client, headers)
    account_id = _outlook(user_id)
    kept = _add_message(
        user_id,
        account_id,
        graph_id="graph-1",
        received_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        priority_score=None,
    )
    gone = _add_message(
        user_id,
        account_id,
        graph_id="gone",
        received_at=datetime(2024, 6, 2, tzinfo=timezone.utc),
        priority_score=None,
    )
    monkeypatch.setattr(
        "app.worker.mail_tasks._graph_get",
        lambda url, token: {
            "value": [
                GRAPH_ITEM,
                {"id": "gone", "@removed": {"reason": "deleted"}},
            ],
            "@odata.deltaLink": _STORED,
        },
    )
    _run(account_id)
    with Session(engine) as session:
        assert session.get(Message, UUID(kept)) is not None
        assert session.get(Message, UUID(gone)) is None

    calls: list[str] = []

    def paged(url, token):
        calls.append(url)
        return {
            "value": [],
            "@odata.nextLink": f"https://graph.microsoft.com/v1.0/page-{len(calls)}",
        }

    monkeypatch.setattr("app.worker.mail_tasks._graph_get", paged)
    for name in ("app", "app.worker", "app.worker.mail_tasks"):
        logging.getLogger(name).disabled = False
    worker_log = logging.getLogger("app.worker.mail_tasks")
    worker_log.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING)
    try:
        _run(account_id)
    finally:
        worker_log.removeHandler(caplog.handler)
    assert len(calls) == 20
    assert "https://graph.microsoft.com/v1.0/page-20" not in calls
    assert f"sync_mailbox_delta truncated account={account_id} pages=20" in caplog.text
    with Session(engine) as session:
        assert session.get(ConnectedAccount, UUID(account_id)).delta_link == _STORED


def test_empty_webhook_settings_still_import_outlook_and_gmail(client, register_user, monkeypatch):
    headers = register_user("both@example.com")
    user_id = _user_id(client, headers)
    outlook_id = _outlook(user_id)
    gmail_id = _gmail(user_id)
    with Session(engine) as session:
        for account_id in (outlook_id, gmail_id):
            account = session.get(ConnectedAccount, UUID(account_id))
            account.sync_status = "idle"
            account.sync_generation = 0
            session.add(account)
        session.commit()
    monkeypatch.setattr(get_settings(), "graph_webhook_url", "")
    monkeypatch.setattr(get_settings(), "graph_webhook_client_state", "")
    monkeypatch.setattr(
        "app.worker.mail_tasks.create_graph_subscription",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subscription created")),
    )
    monkeypatch.setattr(
        "app.worker.mail_tasks._graph_get",
        lambda url, token: {"value": [GRAPH_ITEM], "@odata.deltaLink": _STORED},
    )
    monkeypatch.setattr(
        "app.worker.mail_tasks.collect_gmail_messages",
        lambda token: [_gmail_item(message_id="gmail-1", unread=True)],
    )

    queued = client.post("/messages/sync", headers=headers)
    assert queued.status_code == 202
    assert queued.json()["enqueued"] is True
    _run(outlook_id)
    _run(gmail_id)
    providers = {item["provider"] for item in client.get("/messages", headers=headers).json()}
    assert providers == {"microsoft", "google"}
    with Session(engine) as session:
        outlook = session.get(ConnectedAccount, UUID(outlook_id))
        gmail = session.get(ConnectedAccount, UUID(gmail_id))
        assert outlook.graph_subscription_id is None
        assert gmail.graph_subscription_id is None
        assert gmail.delta_link is None


def test_webhook_enqueues_only_a_matching_client_state(client, register_user, monkeypatch):
    headers = register_user("hook@example.com")
    user_id = _user_id(client, headers)
    account_id = _outlook(user_id, subscription_id="sub-9")
    monkeypatch.setattr(get_settings(), "graph_webhook_client_state", "expected-state")
    body = {
        "subscriptionId": "sub-9",
        "clientState": "other-state",
        "changeType": "created",
        "resource": "me/mailFolders('Inbox')/messages",
    }
    missed = client.post("/webhooks/graph", json={"value": [body]})
    assert missed.status_code == 202
    app.state.arq.enqueue_job.assert_not_awaited()

    matched = client.post(
        "/webhooks/graph",
        json={"value": [{**body, "clientState": "expected-state"}]},
    )
    assert matched.status_code == 202
    app.state.arq.enqueue_job.assert_awaited_once_with("sync_mailbox_delta", "sub-9")

    monkeypatch.setattr(
        "app.worker.mail_tasks._graph_get",
        lambda url, token: {"value": [GRAPH_ITEM], "@odata.deltaLink": _STORED},
    )
    asyncio.run(sync_mailbox_delta({"job_id": "hook"}, "sub-9"))
    assert client.get("/messages", headers=headers).json()[0]["provider"] == "microsoft"
    with Session(engine) as session:
        assert session.get(ConnectedAccount, UUID(account_id)).graph_subscription_id == "sub-9"


def test_renew_recreates_on_404_and_waits_once_on_429(client, register_user, monkeypatch):
    headers = register_user("renew@example.com")
    account_id = _outlook(_user_id(client, headers), subscription_id="sub-old", expires_in_hours=1)
    monkeypatch.setattr(get_settings(), "graph_webhook_url", "https://example.test/webhooks/graph")
    monkeypatch.setattr(get_settings(), "graph_webhook_client_state", "c" * 16)
    slept: list[int] = []
    monkeypatch.setattr("app.worker.mail_tasks.time.sleep", slept.append)

    def missing(access_token, subscription_id):
        assert access_token == "access"
        assert subscription_id == "sub-old"
        raise GraphSubscriptionError("missing", status=404)

    expiry = datetime(2026, 12, 1, tzinfo=timezone.utc)

    def created(access_token, *, notification_url, client_state):
        assert notification_url == "https://example.test/webhooks/graph"
        assert client_state == "c" * 16
        return "sub-new", expiry

    monkeypatch.setattr("app.worker.mail_tasks.renew_graph_subscription", missing)
    monkeypatch.setattr("app.worker.mail_tasks.create_graph_subscription", created)
    asyncio.run(renew_graph_subscriptions({"job_id": "renew-1"}))
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        assert account.graph_subscription_id == "sub-new"
        assert account.graph_subscription_expires_at == expiry
    assert slept == []

    def limited(access_token, subscription_id):
        raise GraphSubscriptionError("rate_limited", status=429, retry_after="2")

    monkeypatch.setattr("app.worker.mail_tasks.renew_graph_subscription", limited)
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        account.graph_subscription_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        session.add(account)
        session.commit()
    with pytest.raises(GraphSubscriptionError):
        asyncio.run(renew_graph_subscriptions({"job_id": "renew-2"}))
    assert slept == [2]
    assert client.get("/users/me", headers=headers).status_code == 200
