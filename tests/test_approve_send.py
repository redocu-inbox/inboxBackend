"""Approve sends a ready draft once. Provider HTTP is mocked."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from io import BytesIO
from threading import Event
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.response import addinfourl
from uuid import UUID

import pytest
from sqlmodel import Session, select

from app.db import engine
from app.models.approval import Approval
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.models.reply_draft import ReplyDraft, ReplyDraftStatus
from app.models.staged_attachment import StagedAttachment
from app.services.google.oauth import GMAIL_MODIFY_SCOPE, GMAIL_SEND_SCOPE
from app.services.mail import send as mail_send
from tests.test_frontend_connect import _add_account, _add_message, _user_id

_WHEN = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)
_SEND_SCOPE = "Mail.ReadWrite Mail.Send"
_GRAPH = "https://graph.microsoft.com/v1.0/me"
_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


def _ready_account(account_id: str, scopes: str) -> None:
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        account.scopes = scopes
        account.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
        account.access_token = "secret-access-token"
        account.refresh_token = "refresh-secret"
        session.add(account)
        session.commit()


def _draft(
    user_id: str, message_id: str, body: str = "Please send the waiver.", *, graph_id: str | None = None
) -> None:
    with Session(engine) as session:
        session.add(
            ReplyDraft(
                user_id=UUID(user_id),
                message_id=UUID(message_id),
                status=ReplyDraftStatus.ready,
                body=body,
                source="user",
                graph_draft_id=graph_id,
                to_recipients=["office@keystone.example"],
                cc_recipients=["bob@keystone.example"],
            )
        )
        session.commit()


def test_outlook_acceptance_waits_for_confirmation_without_resending(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, email="ana@outlook.com")
    _ready_account(account_id, _SEND_SCOPE)
    _add_account(user_id, email="ana@gmail.com", provider="google", access_token="other-mailbox-token")
    message_id = _add_message(
        user_id,
        account_id,
        graph_id="graph-msg-1",
        received_at=_WHEN,
        priority_score=None,
        subject="Waiver",
        from_email="luis@keystone.example",
        from_name="Luis",
    )
    _draft(user_id, message_id, graph_id="saved-draft")
    updated = client.patch(
        f"/messages/{message_id}/draft", headers=headers, json={"body": "Please send the latest waiver."}
    )
    assert updated.status_code == 200
    calls = []
    confirmation_visible = False

    def provider(request, *, timeout):
        assert request.get_header("Authorization") == "Bearer secret-access-token"
        payload = json.loads(request.data) if request.data is not None else None
        calls.append((request.get_method(), request.full_url, payload))
        if request.full_url == f"{_GRAPH}/translateExchangeIds":
            return BytesIO(b'{"value":[{"targetId":"immutable-draft"}]}')
        assert request.get_header("Prefer") == 'IdType="ImmutableId"'
        if request.full_url.endswith("/send"):
            with Session(engine) as session:
                attempt = session.exec(select(Approval)).one()
                assert attempt.provider_reference == "immutable-draft"
                assert attempt.approved_at is None
            return addinfourl(BytesIO(), {}, request.full_url, 202)
        if request.get_method() == "GET":
            if not confirmation_visible:
                raise HTTPError(request.full_url, 404, "Not indexed yet", {}, BytesIO())
            return BytesIO(b'{"isDraft":false,"sentDateTime":"2026-11-19T20:00:00Z"}')
        return BytesIO()

    monkeypatch.setattr(mail_send, "urlopen", provider)
    path = f"/messages/{message_id}/approve"
    first = client.post(path, headers=headers)
    assert first.status_code == 202
    pending = first.json()
    assert pending["send_status"] == "confirmation_pending"
    attempt_id = pending["attempt_id"]
    assert calls == [
        ("POST", f"{_GRAPH}/translateExchangeIds", {
            "inputIds": ["saved-draft"],
            "sourceIdType": "restId",
            "targetIdType": "restImmutableEntryId",
        }),
        ("PATCH", f"{_GRAPH}/messages/immutable-draft", {
            "body": {"contentType": "Text", "content": "Please send the latest waiver."},
            "toRecipients": [{"emailAddress": {"address": "office@keystone.example"}}],
            "ccRecipients": [{"emailAddress": {"address": "bob@keystone.example"}}],
            "bccRecipients": [],
        }),
        ("POST", f"{_GRAPH}/messages/immutable-draft/send", None),
    ]
    detail_path = f"/messages/{message_id}"
    status_path = f"{detail_path}/send-attempts/{attempt_id}"
    detail = client.get(detail_path, headers=headers).json()
    assert detail["pending_send_attempt_id"] == attempt_id
    assert detail["approved_at"] is None
    assert client.get("/approvals", headers=headers).json() == []

    repeated = client.post(path, headers=headers)
    assert repeated.status_code == 202
    assert repeated.json() == pending
    check = client.get(status_path, headers=headers)
    assert check.status_code == 200
    assert check.json() == {**pending, "approved_at": None}
    assert client.get("/approvals", headers=headers).json() == []

    confirmation_visible = True
    confirmed = client.get(status_path, headers=headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["send_status"] == "sent"
    assert confirmed.json()["attempt_id"] == attempt_id
    detail = client.get(detail_path, headers=headers).json()
    assert detail["pending_send_attempt_id"] is None
    assert detail["approved_at"] == confirmed.json()["approved_at"]
    assert detail["approved_at"] is not None
    assert client.post(path, headers=headers).status_code == 409
    other = register_user("bob@example.com")
    assert client.post(path, headers=other).status_code == 404
    assert calls[3:] == [
        ("GET", f"{_GRAPH}/messages/immutable-draft?$select=isDraft,sentDateTime", None),
    ] * 3
    record = client.get("/approvals", headers=headers)
    assert record.status_code == 200
    assert record.headers["X-Total-Count"] == "1"
    saved = record.json()
    assert saved[0]["message_id"] == message_id
    assert saved[0]["sent_to"] == "office@keystone.example, bob@keystone.example"
    assert saved[0]["sent_from"] == "ana@outlook.com"
    assert saved[0]["reply"] == "Please send the latest waiver."
    assert saved[0]["approved_by"]["email"] == "ana@example.com"
    assert client.get("/approvals", headers=other).json() == []


def test_forward_sends_the_composed_body_and_not_the_source_file(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, email="ana@outlook.com")
    _ready_account(account_id, _SEND_SCOPE)
    source = _add_message(
        user_id,
        account_id,
        graph_id="source-msg",
        received_at=_WHEN,
        priority_score=None,
        subject="Original",
        body_text="Secret attachment lives here.",
    )
    with Session(engine) as session:
        session.add(
            StagedAttachment(
                user_id=UUID(user_id),
                message_id=UUID(source),
                file_name="source.pdf",
                size_bytes=4,
                content_type="application/pdf",
                storage_key="attachments/source",
                created_at=_WHEN,
            )
        )
        session.commit()
    created = client.post(
        "/messages/new",
        headers=headers,
        json={
            "account_id": account_id,
            "to": ["maya@example.com"],
            "subject": "Fwd: Original",
            "body": "Please see the note.",
            "forward_of_id": source,
        },
    )
    assert created.status_code == 201
    forward_id = created.json()["id"]
    calls = []

    def provider(request, *, timeout):
        payload = json.loads(request.data) if request.data is not None else None
        calls.append((request.full_url, payload))
        return BytesIO(b'{"id":"forward-draft"}') if payload else BytesIO()

    monkeypatch.setattr(mail_send, "urlopen", provider)
    response = client.post(f"/messages/{forward_id}/approve", headers=headers)
    assert response.status_code == 202
    assert calls == [
        (f"{_GRAPH}/messages", {
            "body": {"contentType": "Text", "content": "Please see the note."},
            "toRecipients": [{"emailAddress": {"address": "maya@example.com"}}],
            "ccRecipients": [],
            "bccRecipients": [],
            "subject": "Fwd: Original",
            "attachments": [],
        }),
        (f"{_GRAPH}/messages/forward-draft/send", None),
    ]
    with Session(engine) as session:
        source_row = session.get(Message, UUID(source))
        assert source_row.approved_at is None


def test_missing_send_scope_does_not_send(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, email="ana@outlook.com")
    _ready_account(account_id, "https://graph.microsoft.com/Mail.Read")
    message_id = _add_message(
        user_id,
        account_id,
        graph_id="graph-msg-2",
        received_at=_WHEN,
        priority_score=None,
        from_email="luis@keystone.example",
    )
    _draft(user_id, message_id)

    def explode(*_args, **_kwargs):
        raise AssertionError("provider was called")

    monkeypatch.setattr(mail_send, "urlopen", explode)

    response = client.post(f"/messages/{message_id}/approve", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "Reconnect this mailbox to send mail."
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row.approved_at is None
        assert session.exec(select(Approval).where(Approval.message_id == row.id)).first() is None


@pytest.mark.parametrize("scope", [GMAIL_SEND_SCOPE, GMAIL_MODIFY_SCOPE])
def test_provider_failure_leaves_the_draft_unsent(client, register_user, monkeypatch, scope):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, email="ana@gmail.com", provider="google")
    _ready_account(account_id, f"openid email {scope}")
    message_id = _add_message(
        user_id,
        account_id,
        graph_id="gmail-1",
        received_at=_WHEN,
        priority_score=None,
        from_email="luis@keystone.example",
        conversation_id="thread-1",
    )
    _draft(user_id, message_id, body="On my way.")

    def reject(request, *, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, BytesIO())

    monkeypatch.setattr(mail_send, "urlopen", reject)
    response = client.post(f"/messages/{message_id}/approve", headers=headers)
    assert response.status_code == 502
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row.approved_at is None
        attempt = session.exec(select(Approval).where(Approval.user_id == UUID(user_id))).one()
        assert attempt.approved_at is None
        assert attempt.send_failed_at is not None
        failed_id = str(attempt.id)
    assert client.get("/approvals", headers=headers).json() == []
    failed = client.get(f"/messages/{message_id}/send-attempts/{failed_id}", headers=headers)
    assert failed.json() == {"attempt_id": failed_id, "send_status": "failed", "approved_at": None}

    monkeypatch.setattr(mail_send, "urlopen", lambda request, **kwargs: BytesIO())
    retried = client.post(f"/messages/{message_id}/approve", headers=headers)
    assert retried.status_code == 200
    with Session(engine) as session:
        attempts = session.exec(select(Approval).where(Approval.message_id == UUID(message_id))).all()
        assert len(attempts) == 2
        assert sum(attempt.approved_at is not None for attempt in attempts) == 1


def _ready_reply(client, register_user, provider):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, provider=provider)
    _ready_account(account_id, _SEND_SCOPE if provider == "microsoft" else GMAIL_MODIFY_SCOPE)
    message_id = _add_message(
        user_id, account_id, graph_id="original", received_at=_WHEN,
        priority_score=None, from_email="sender@example.com",
    )
    _draft(user_id, message_id)
    return headers, message_id


def test_simultaneous_approvals_make_one_provider_send(client, register_user, monkeypatch):
    headers, message_id = _ready_reply(client, register_user, "microsoft")
    send_entered = Event()
    response_allowed = Event()
    sends = []

    def provider(request, *, timeout):
        path = urlsplit(request.full_url).path
        if path.endswith("/createReply"):
            return BytesIO(b'{"id":"immutable-draft"}')
        if request.get_method() == "PATCH":
            assert json.loads(request.data)["body"]["content"] == "Please send the waiver."
            return BytesIO()
        if request.get_method() == "GET":
            return BytesIO(b'{"isDraft":true}')
        assert path.endswith("/immutable-draft/send")
        sends.append(path)
        send_entered.set()
        assert response_allowed.wait(5)
        return addinfourl(BytesIO(), {}, request.full_url, 202)

    monkeypatch.setattr(mail_send, "urlopen", provider)
    path = f"/messages/{message_id}/approve"
    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(client.post, path, headers=headers)
        try:
            assert send_entered.wait(5)
            second = callers.submit(client.post, path, headers=headers).result(timeout=5)
            assert second.status_code == 202
            assert second.json()["send_status"] == "confirmation_pending"
            push = client.post(f"/messages/{message_id}/draft/outlook", headers=headers)
            assert push.status_code == 409
            with Session(engine) as session:
                attempt = session.get(Approval, UUID(second.json()["attempt_id"]))
                assert attempt.message_id == UUID(message_id)
                assert attempt.approved_at is None
        finally:
            response_allowed.set()
        original = first.result(timeout=5)
        assert original.status_code == 202
        assert original.json() == second.json()

    assert len(sends) == 1
    assert client.get("/approvals", headers=headers).json() == []


@pytest.mark.parametrize("provider_name", ["microsoft", "google"])
def test_send_with_lost_response_is_confirmed_without_resending(client, register_user, monkeypatch, provider_name):
    headers, message_id = _ready_reply(client, register_user, provider_name)
    _add_account(_user_id(client, headers), provider=provider_name, email="other@example.com", access_token="wrong-mailbox-token")
    sent_reference = []
    looked_up = []
    confirmation_visible = False

    def provider(request, *, timeout):
        assert request.get_header("Authorization") == "Bearer secret-access-token"
        if request.full_url.endswith("/createReply"):
            return BytesIO(b'{"id":"immutable-draft"}')
        if request.get_method() == "PATCH":
            return BytesIO()
        if request.full_url.endswith("/send"):
            if provider_name == "google":
                data = json.loads(request.data)
                mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(data["raw"]))
                reference = mime["Message-ID"].strip("<>")
            else:
                reference = request.full_url.split("/")[-2]
            sent_reference.append(reference)
            # The provider committed the send, but its HTTP response never arrived.
            raise TimeoutError("reply lost")

        assert request.get_method() == "GET"
        if provider_name == "google":
            assert request.full_url.startswith(f"{_GMAIL}?")
            query = parse_qs(urlsplit(request.full_url).query)
            assert query["labelIds"] == ["SENT"]
            assert query["q"] == [f"rfc822msgid:{sent_reference[0]}"]
            found = {"messages": [{"id": "sent-message"}]} if confirmation_visible else {}
        else:
            assert request.full_url.startswith(f"{_GRAPH}/messages/{sent_reference[0]}?")
            assert request.get_header("Prefer") == 'IdType="ImmutableId"'
        looked_up.append(request.full_url)
        if provider_name == "microsoft":
            if not confirmation_visible:
                raise HTTPError(request.full_url, 404, "Not indexed yet", {}, BytesIO())
            found = {"isDraft": False, "sentDateTime": "2026-11-19T20:00:00Z"}
        return BytesIO(json.dumps(found).encode())

    monkeypatch.setattr(mail_send, "urlopen", provider)
    path = f"/messages/{message_id}/approve"
    pending = client.post(path, headers=headers)
    assert pending.status_code == 202
    assert pending.json()["send_status"] == "confirmation_pending"
    attempt_id = pending.json()["attempt_id"]
    status_path = f"/messages/{message_id}/send-attempts/{attempt_id}"
    detail = client.get(f"/messages/{message_id}", headers=headers).json()
    assert detail["pending_send_attempt_id"] == attempt_id
    assert detail["approved_at"] is None
    assert client.get("/approvals", headers=headers).json() == []
    with Session(engine) as session:
        attempt = session.get(Approval, UUID(attempt_id))
        assert attempt.provider_reference == sent_reference[0]
        assert attempt.approved_at is None

    unconfirmed = client.post(path, headers=headers)
    assert unconfirmed.status_code == 202
    assert unconfirmed.json() == pending.json()
    other = register_user("not-the-sender@example.com")
    assert client.get(status_path, headers=other).status_code == 404
    check = client.get(status_path, headers=headers)
    assert check.status_code == 200
    assert check.headers["Cache-Control"] == "no-store"
    assert check.json() == {"attempt_id": attempt_id, "send_status": "confirmation_pending", "approved_at": None}
    edited = client.patch(f"/messages/{message_id}/draft", headers=headers, json={"body": "Later edit"})
    assert edited.status_code == 200
    confirmation_visible = True
    confirmed = client.get(status_path, headers=headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["send_status"] == "sent"
    assert confirmed.json()["attempt_id"] == attempt_id
    detail = client.get(f"/messages/{message_id}", headers=headers).json()
    assert detail["pending_send_attempt_id"] is None
    assert detail["approved_at"] == confirmed.json()["approved_at"]
    assert client.post(path, headers=headers).status_code == 409
    assert len(looked_up) == 3
    assert len(sent_reference) == 1
    approval, = client.get("/approvals", headers=headers).json()
    assert approval["id"] == pending.json()["attempt_id"]
    assert approval["reply"] == "Please send the waiver."
