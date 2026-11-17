"""Approve sends a ready draft once. Provider HTTP is mocked."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlmodel import Session, select

from app.db import engine
from app.models.approval import Approval
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.models.reply_draft import ReplyDraft, ReplyDraftStatus
from app.models.staged_attachment import StagedAttachment
from app.services.mail.send import MailSendError
from tests.test_frontend_connect import _add_account, _add_message, _user_id

_WHEN = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)
_SEND_SCOPE = "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.Send"


def _ready_account(account_id: str, scopes: str) -> None:
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        account.scopes = scopes
        account.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
        account.access_token = "secret-access-token"
        account.refresh_token = "refresh-secret"
        session.add(account)
        session.commit()


def _draft(user_id: str, message_id: str, body: str = "Please send the waiver.") -> None:
    with Session(engine) as session:
        session.add(
            ReplyDraft(
                user_id=UUID(user_id),
                message_id=UUID(message_id),
                status=ReplyDraftStatus.ready,
                body=body,
                source="user",
                to_recipients=["office@keystone.example"],
                cc_recipients=["bob@keystone.example"],
            )
        )
        session.commit()


def test_approve_sends_the_edited_recipients_once(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, email="ana@outlook.com")
    _ready_account(account_id, _SEND_SCOPE)
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
    _draft(user_id, message_id)
    calls = []

    def fake_reply(access_token, graph_message_id, *, body, to, cc, files):
        assert access_token == "secret-access-token"
        calls.append(
            {
                "kind": "reply",
                "graph_id": graph_message_id,
                "body": body,
                "to": to,
                "cc": cc,
                "files": [name for name, _content, _type in files],
            }
        )

    def explode(*_args, **_kwargs):
        raise AssertionError("new mail was sent for a reply")

    monkeypatch.setattr("app.services.inbox.approve.mail_send.send_outlook_reply", fake_reply)
    monkeypatch.setattr("app.services.inbox.approve.mail_send.send_outlook_new", explode)
    monkeypatch.setattr("app.services.inbox.approve.mail_send.send_gmail", explode)

    first = client.post(f"/messages/{message_id}/approve", headers=headers)
    assert first.status_code == 200
    assert first.json()["approved_at"] is not None
    assert calls == [
        {
            "kind": "reply",
            "graph_id": "graph-msg-1",
            "body": "Please send the waiver.",
            "to": ["office@keystone.example"],
            "cc": ["bob@keystone.example"],
            "files": [],
        }
    ]
    second = client.post(f"/messages/{message_id}/approve", headers=headers)
    assert second.status_code == 409
    assert calls == [
        {
            "kind": "reply",
            "graph_id": "graph-msg-1",
            "body": "Please send the waiver.",
            "to": ["office@keystone.example"],
            "cc": ["bob@keystone.example"],
            "files": [],
        }
    ]
    other = register_user("bob@example.com")
    assert client.post(f"/messages/{message_id}/approve", headers=other).status_code == 404
    record = client.get("/approvals", headers=headers)
    assert record.status_code == 200
    assert record.headers["X-Total-Count"] == "1"
    saved = record.json()
    assert saved[0]["message_id"] == message_id
    assert saved[0]["sent_to"] == "office@keystone.example, bob@keystone.example"
    assert saved[0]["sent_from"] == "ana@outlook.com"
    assert saved[0]["reply"] == "Please send the waiver."
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

    def fake_new(access_token, *, subject, body, to, cc, files):
        calls.append({"subject": subject, "body": body, "to": to, "cc": cc, "files": files})

    monkeypatch.setattr("app.services.inbox.approve.mail_send.send_outlook_new", fake_new)
    monkeypatch.setattr(
        "app.services.inbox.approve.mail_send.send_outlook_reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("reply")),
    )

    response = client.post(f"/messages/{forward_id}/approve", headers=headers)
    assert response.status_code == 200
    assert calls == [
        {
            "subject": "Fwd: Original",
            "body": "Please see the note.",
            "to": ["maya@example.com"],
            "cc": [],
            "files": [],
        }
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

    monkeypatch.setattr("app.services.inbox.approve.mail_send.send_outlook_reply", explode)
    monkeypatch.setattr("app.services.inbox.approve.mail_send.send_outlook_new", explode)
    monkeypatch.setattr("app.services.inbox.approve.mail_send.send_gmail", explode)

    response = client.post(f"/messages/{message_id}/approve", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "Reconnect this mailbox to send mail."
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row.approved_at is None
        assert session.exec(select(Approval).where(Approval.message_id == row.id)).first() is None


def test_provider_failure_leaves_the_draft_unsent(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, email="ana@gmail.com", provider="google")
    _ready_account(account_id, "openid email https://www.googleapis.com/auth/gmail.send")
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

    def fail(*_args, **_kwargs):
        raise MailSendError("gmail_unavailable")

    monkeypatch.setattr("app.services.inbox.approve.mail_send.send_gmail", fail)
    response = client.post(f"/messages/{message_id}/approve", headers=headers)
    assert response.status_code == 502
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row.approved_at is None
        assert session.exec(select(Approval).where(Approval.user_id == UUID(user_id))).first() is None
