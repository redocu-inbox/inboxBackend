"""Exercise mail actions and the real sync worker with mocked provider HTTP."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from threading import Event
from urllib.error import URLError
from uuid import UUID

import pytest
from sqlmodel import Session

from app.db import engine
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.services.google.oauth import GMAIL_MODIFY_SCOPE, GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE
from app.services.inbox import folder_sync, mail_actions
from app.worker import mail_tasks
from tests.test_frontend_connect import _add_account, _add_message, _user_id
from tests.test_mailbox_reliability import GRAPH_ITEM, _gmail_item

_SHARED_ID = "same+message"
_GRAPH_URL = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/same%2Bmessage"
_GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/same%2Bmessage"


@pytest.fixture
def inbox(client, register_user):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    accounts = {}
    messages = {}
    for provider, email, scope in (
        ("microsoft", "ana@outlook.com", "Mail.ReadWrite"),
        ("google", "ana@gmail.com", GMAIL_MODIFY_SCOPE),
    ):
        account_id = _add_account(
            user_id, email=email, provider=provider, access_token=f"{provider}-token"
        )
        accounts[provider] = account_id
        messages[provider] = _add_message(
            user_id,
            account_id,
            graph_id=_SHARED_ID,
            received_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            priority_score=None,
            is_read=False,
        )
        with Session(engine) as session:
            account = session.get(ConnectedAccount, UUID(account_id))
            account.scopes = scope
            account.refresh_token = f"{provider}-refresh"
            account.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            session.commit()
    return headers, accounts, messages


@pytest.fixture
def provider_requests(monkeypatch):
    requests = []

    def capture(request, *, timeout):
        payload = json.loads(request.data) if request.data is not None else None
        requests.append(
            (request.get_method(), request.full_url, request.get_header("Authorization"), payload)
        )
        new_id = "moved-" + payload["destinationId"] if payload and "destinationId" in payload else None
        return BytesIO(json.dumps({"id": new_id}).encode())

    monkeypatch.setattr(mail_actions, "urlopen", capture)
    monkeypatch.setattr(mail_tasks, "_ensure_graph_subscription", lambda session, account_id: None)
    return requests


def _run_sync(account_id: str) -> None:
    with Session(engine) as session:
        previous = session.get(ConnectedAccount, UUID(account_id)).last_sync_at
    asyncio.run(mail_tasks.sync_mailbox_delta({"job_id": "mail-action-test"}, account_id))
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        assert account.sync_status == "idle"
        assert account.last_sync_at is not None
        assert previous is None or account.last_sync_at > previous


@pytest.mark.parametrize("provider", ["microsoft", "google"])
def test_explicit_read_state_survives_sync(client, inbox, monkeypatch, provider_requests, provider):
    headers, accounts, messages = inbox
    message_path = f"/messages/{messages[provider]}"

    for is_read in (True, False):
        response = client.patch(
            f"{message_path}/read-state", headers=headers, json={"is_read": is_read}
        )
        assert response.status_code == 204
        assert client.get(message_path, headers=headers).json()["is_read"] is is_read

        if provider == "microsoft":
            assert provider_requests[-1] == (
                "PATCH", _GRAPH_URL, "Bearer microsoft-token", {"isRead": is_read}
            )
            updated = {**GRAPH_ITEM, "id": _SHARED_ID, "isRead": is_read}
            monkeypatch.setattr(
                mail_tasks, "_graph_get", lambda url, token: {"value": [updated]}
            )
        else:
            label_key = "removeLabelIds" if is_read else "addLabelIds"
            assert provider_requests[-1] == (
                "POST", f"{_GMAIL_URL}/modify", "Bearer google-token", {label_key: ["UNREAD"]}
            )
            updated = _gmail_item(message_id=_SHARED_ID, unread=not is_read)
            monkeypatch.setattr(
                mail_tasks, "collect_gmail_messages", lambda token: [updated]
            )
        _run_sync(accounts[provider])
        assert client.get(message_path, headers=headers).json()["is_read"] is is_read

    other = "google" if provider == "microsoft" else "microsoft"
    assert client.get(f"/messages/{messages[other]}", headers=headers).json()["is_read"] is False


@pytest.mark.parametrize(
    ("provider", "action", "url", "payload"),
    [
        ("microsoft", "archive", f"{_GRAPH_URL}/move", {"destinationId": "archive"}),
        ("microsoft", "trash", f"{_GRAPH_URL}/move", {"destinationId": "deleteditems"}),
        ("google", "archive", f"{_GMAIL_URL}/modify", {"removeLabelIds": ["INBOX"]}),
        ("google", "trash", f"{_GMAIL_URL}/trash", None),
    ],
    ids=["outlook-archive", "outlook-trash", "gmail-archive", "gmail-trash"],
)
def test_moved_message_stays_out_of_inbox_after_sync(
    client, inbox, monkeypatch, provider_requests, provider, action, url, payload
):
    headers, accounts, messages = inbox
    message_id = messages[provider]
    other_id = next(item_id for item_id in messages.values() if item_id != message_id)

    response = client.post(f"/messages/{message_id}/actions/{action}", headers=headers)
    assert response.status_code == 204
    assert provider_requests == [("POST", url, f"Bearer {provider}-token", payload)]
    before_sync = client.get("/messages", headers=headers)
    assert before_sync.headers["X-Total-Count"] == "1"
    assert [item["id"] for item in before_sync.json()] == [other_id]

    if provider == "microsoft":
        delta = {"value": [
            {**GRAPH_ITEM, "id": _SHARED_ID},
            {"id": _SHARED_ID, "@removed": {"reason": "deleted"}},
        ]}
        monkeypatch.setattr(mail_tasks, "_graph_get", lambda url, token: delta)
    else:
        stale_item = _gmail_item(message_id=_SHARED_ID, unread=True)
        monkeypatch.setattr(
            mail_tasks, "collect_gmail_messages", lambda token: [stale_item]
        )
    _run_sync(accounts[provider])

    after_sync = client.get("/messages", headers=headers)
    assert after_sync.headers["X-Total-Count"] == "1"
    assert [item["id"] for item in after_sync.json()] == [other_id]
    with Session(engine) as session:
        message = session.get(Message, UUID(message_id))
        assert message.inbox_removed_at is not None
        assert message.subject == "Hello"

    destination = "archive" if action == "archive" else "deleted"
    listed = client.get(f"/messages?folder={destination}", headers=headers).json()
    assert [(item["id"], item["folder"], item["account_id"]) for item in listed] == [
        (message_id, destination, accounts[provider]),
    ]
    body = {"message_ids": [message_id], "folder": "inbox", "expected_folder": destination}
    response = client.patch("/messages/folder", headers=headers, json=body)
    assert response.json() == {"updated": [{"id": message_id, "folder": "inbox"}], "failed": []}
    after_restore = client.get(f"/messages/{message_id}", headers=headers).json()
    assert after_restore["folder"] == "inbox"
    if provider == "microsoft":
        old_folder = "archive" if action == "archive" else "deleteditems"
        assert provider_requests[-1] == (
            "POST", f"https://graph.microsoft.com/v1.0/me/messages/moved-{old_folder}/move",
            "Bearer microsoft-token", {"destinationId": "inbox"},
        )
    else:
        assert provider_requests[-1] == ("POST", f"{_GMAIL_URL}/modify", "Bearer google-token", {"addLabelIds": ["INBOX"]})
        if action == "trash":
            assert provider_requests[-2] == ("POST", f"{_GMAIL_URL}/untrash", "Bearer google-token", None)
    count = len(provider_requests)
    assert client.patch("/messages/folder", headers=headers, json=body).json()["failed"][0]["id"] == message_id
    assert len(provider_requests) == count


def test_batch_read_updates_owned_mail_on_both_providers_and_reports_failures(client, register_user, inbox, provider_requests):
    headers, _, messages = inbox
    other = register_user("foreign@example.com")
    other_id = _user_id(client, other)
    external_account = _add_account(other_id)
    external_message = _add_message(other_id, external_account, graph_id="foreign", received_at=datetime.now(timezone.utc), priority_score=None)
    own = list(messages.values())

    response = client.patch("/messages/read-state", headers=headers, json={"message_ids": [own[0], external_message, own[1]], "is_read": True})
    assert response.status_code == 200
    assert response.json() == {
        "updated": [{"id": id, "is_read": True} for id in own],
        "failed": [{"id": external_message, "detail": "Message not found."}],
    }
    assert [item[2] for item in provider_requests] == ["Bearer microsoft-token", "Bearer google-token"]
    duplicate = client.patch("/messages/read-state", headers=headers, json={"message_ids": [own[0]] * 2, "is_read": False})
    assert duplicate.status_code == 422
    assert len(provider_requests) == 2


def test_simultaneous_archive_requests_move_the_outlook_message_once(client, inbox, monkeypatch):
    headers, _, messages = inbox
    provider_called = Event()
    finish = Event()
    calls = []

    def move(request, *, timeout):
        calls.append(request.full_url)
        provider_called.set()
        assert finish.wait(5)
        return BytesIO(b'{"id":"archived-id"}')

    def archive():
        return client.patch("/messages/folder", headers=headers, json={
            "message_ids": [messages["microsoft"]], "folder": "archive",
        })

    monkeypatch.setattr(mail_actions, "urlopen", move)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(archive)
        try:
            assert provider_called.wait(5)
            second = pool.submit(archive)
        finally:
            finish.set()
        assert first.result(timeout=5).json() == second.result(timeout=5).json()
    assert calls == [f"{_GRAPH_URL}/move"]


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_sync_reconciles_a_known_message_moved_at_the_provider(client, inbox, monkeypatch, provider):
    headers, accounts, messages = inbox

    def respond(request, *, timeout):
        if provider == "google":
            data = {"id": _SHARED_ID, "labelIds": ["TRASH", "UNREAD"]}
        elif request.full_url.endswith("/$batch"):
            requests = json.loads(request.data)["requests"]
            assert requests[-1]["headers"] == {"Prefer": 'IdType="ImmutableId"'}
            data = {"responses": [
                *({"id": name, "status": 200, "body": {"id": f"folder-{name}"}}
                  for name in ("inbox", "archive", "deleteditems")),
                {"id": "0", "status": 200, "body": {"parentFolderId": "folder-archive", "isRead": True}},
            ]}
        else:
            payload = json.loads(request.data)
            result = "stable-id" if payload["targetIdType"] == "restImmutableEntryId" else "rest-archive-id"
            data = {"value": [{"sourceId": payload["inputIds"][0], "targetId": result}]}
        return BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(mail_tasks, "read_provider_folders", folder_sync.read_provider_folders)
    monkeypatch.setattr(folder_sync, "urlopen", respond)
    monkeypatch.setattr(mail_tasks, "_ensure_graph_subscription", lambda *_: None)
    monkeypatch.setattr(mail_tasks, "collect_gmail_messages", lambda _: [])
    monkeypatch.setattr(mail_tasks, "_graph_get", lambda *_: {"value": [{"id": _SHARED_ID, "@removed": {"reason": "deleted"}}]})
    _run_sync(accounts[provider])
    detail = client.get(f"/messages/{messages[provider]}", headers=headers).json()
    assert detail["folder"] == ("deleted" if provider == "google" else "archive")
    with Session(engine) as session:
        row = session.get(Message, UUID(messages[provider]))
        assert row.graph_id == (_SHARED_ID if provider == "google" else "rest-archive-id")


def test_sync_started_before_read_action_discards_its_snapshot(client, inbox, monkeypatch, provider_requests):
    headers, accounts, messages = inbox
    message_path = f"/messages/{messages['google']}"
    outcomes = []

    def fetch_before_action(token):
        stale_item = _gmail_item(message_id=_SHARED_ID, unread=True)
        response = client.patch(
            f"{message_path}/read-state", headers=headers, json={"is_read": True}
        )
        outcomes.append(response.status_code)
        return [stale_item]

    monkeypatch.setattr(mail_tasks, "collect_gmail_messages", fetch_before_action)
    asyncio.run(mail_tasks.sync_mailbox_delta({"job_id": "stale-read"}, accounts["google"]))

    assert outcomes == [204]
    assert len(provider_requests) == 1
    assert client.get(message_path, headers=headers).json()["is_read"] is True
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(accounts["google"]))
        assert account.sync_status == "idle"
        assert account.last_sync_at is None


def test_actions_require_ownership_and_write_scope(client, register_user, inbox, provider_requests):
    headers, accounts, messages = inbox
    message_path = f"/messages/{messages['google']}"
    foreign = register_user("bob@example.com")
    assert client.post(f"{message_path}/actions/trash", headers=foreign).status_code == 404
    invalid = client.patch(f"{message_path}/read-state", headers=headers, json={"is_read": "false"})
    assert invalid.status_code == 422

    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(accounts["google"]))
        account.scopes = f"{GMAIL_READONLY_SCOPE} {GMAIL_SEND_SCOPE}"
        session.commit()

    needs_consent = client.patch(f"{message_path}/read-state", headers=headers, json={"is_read": True})
    assert needs_consent.status_code == 409
    assert "Reconnect" in needs_consent.json()["detail"]
    assert provider_requests == []


def test_gmail_action_uses_the_refreshed_token(client, inbox, monkeypatch, provider_requests):
    headers, accounts, messages = inbox
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(accounts["google"]))
        account.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.commit()

    def refresh(token):
        assert token == "google-refresh"
        return {"access_token": "new-google-token", "expires_in": 3600, "scope": GMAIL_MODIFY_SCOPE}

    monkeypatch.setattr("app.services.google.tokens.exchange_refresh_token", refresh)
    response = client.post(f"/messages/{messages['google']}/actions/trash", headers=headers)
    assert response.status_code == 204
    assert provider_requests == [("POST", f"{_GMAIL_URL}/trash", "Bearer new-google-token", None)]


def test_failed_provider_write_leaves_message_in_inbox(client, inbox, monkeypatch):
    headers, accounts, messages = inbox
    message_id = messages["google"]

    def fail_request(request, *, timeout):
        raise URLError("offline")

    monkeypatch.setattr(mail_actions, "urlopen", fail_request)
    response = client.post(f"/messages/{message_id}/actions/archive", headers=headers)
    assert response.status_code == 502
    assert {item["id"] for item in client.get("/messages", headers=headers).json()} == set(messages.values())
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(accounts["google"]))
        assert account.sync_generation == 0
        assert session.get(Message, UUID(message_id)).inbox_removed_at is None
