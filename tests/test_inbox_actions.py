"""Conversation history, Reply all, forwards, and files from the computer.

None of these routes send mail.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlmodel import Session

from app.db import engine
from app.services.inbox import history as history_module
from tests.test_frontend_connect import _add_account, _add_message, _user_id

_WHEN = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def _mailbox(client, register_user, email="ana@example.com"):
    headers = register_user(email)
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, email="ana@outlook.com")
    return headers, user_id, account_id


def _message(user_id, account_id, graph_id, *, hours=0, **columns):
    return _add_message(
        user_id,
        account_id,
        graph_id=graph_id,
        received_at=_WHEN + timedelta(hours=hours),
        priority_score=None,
        **columns,
    )


def test_graph_and_gmail_keep_the_thread_and_recipients():
    from app.worker.mail_tasks import graph_message_fields
    from app.services.google.messages import gmail_message_fields

    fields = graph_message_fields(
        {
            "id": "g1",
            "receivedDateTime": "2024-06-01T12:00:00Z",
            "conversationId": "thread-1",
            "from": {"emailAddress": {"name": "Luis", "address": "luis@keystone.example"}},
            "toRecipients": [
                {"emailAddress": {"name": "Ana", "address": "ana@outlook.com"}},
                {"emailAddress": {"address": "office@keystone.example"}},
            ],
            "ccRecipients": [{"emailAddress": {"address": "bob@keystone.example"}}],
            "replyTo": [{"emailAddress": {"name": "Luis", "address": "replies@keystone.example"}}],
        }
    )
    assert fields["conversation_id"] == "thread-1"
    assert fields["to_recipients"] == ["Ana <ana@outlook.com>", "office@keystone.example"]
    assert fields["cc_recipients"] == ["bob@keystone.example"]
    assert fields["reply_to"] == ["Luis <replies@keystone.example>"]
    assert fields["origin"] == "provider"
    assert fields["direction"] == "incoming"

    item = {
        "id": "gm1",
        "threadId": "gmail-thread",
        "internalDate": "1717243200000",
        "labelIds": ["INBOX"],
        "snippet": "Hello",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Luis <luis@keystone.example>"},
                {"name": "To", "value": "Ana <ana@outlook.com>, office@keystone.example"},
                {"name": "Reply-To", "value": "replies@keystone.example"},
            ],
            "body": {"data": "SGVsbG8"},
        },
    }
    gmail = gmail_message_fields(item)
    assert gmail["conversation_id"] == "gmail-thread"
    assert gmail["to_recipients"] == ["Ana <ana@outlook.com>", "office@keystone.example"]
    assert gmail["reply_to"] == ["replies@keystone.example"]


def test_conversation_pages_sent_mail_and_hides_a_local_draft(client, register_user, monkeypatch):
    headers, user_id, account_id = _mailbox(client, register_user)
    monkeypatch.setattr(history_module, "PAGE_SIZE", 2)
    first = _message(
        user_id,
        account_id,
        "a",
        hours=0,
        conversation_id="thread-9",
        subject="First",
        body_text="Start",
        direction="incoming",
        origin="provider",
    )
    sent = _message(
        user_id,
        account_id,
        "b",
        hours=1,
        conversation_id="thread-9",
        subject="Re: First",
        body_text="Sent reply",
        direction="outgoing",
        origin="provider",
        from_email="ana@outlook.com",
    )
    latest = _message(
        user_id,
        account_id,
        "c",
        hours=2,
        conversation_id="thread-9",
        subject="Re: First",
        body_text="Newest",
        direction="incoming",
        origin="provider",
    )
    local = _message(
        user_id,
        account_id,
        "local-draft",
        hours=3,
        conversation_id="thread-9",
        subject="Unsent",
        body_text="Do not show",
        direction="outgoing",
        origin="local",
    )
    other = register_user("bob@example.com")

    page = client.get(f"/messages/{latest}/conversation", headers=headers)
    assert page.status_code == 200
    body = page.json()
    assert [item["id"] for item in body["messages"]] == [sent, latest]
    assert body["messages"][0]["direction"] == "outgoing"
    assert body["messages"][0]["body_text"] == "Sent reply"
    assert local not in [item["id"] for item in body["messages"]]
    assert body["next_cursor"]

    older = client.get(
        f"/messages/{latest}/conversation",
        headers=headers,
        params={"cursor": body["next_cursor"]},
    )
    assert older.status_code == 200
    assert [item["id"] for item in older.json()["messages"]] == [first]
    assert older.json()["next_cursor"] is None

    assert client.get(f"/messages/{latest}/conversation", headers=other).status_code == 404
    bad = client.get(
        f"/messages/{latest}/conversation",
        headers=headers,
        params={"cursor": "not-a-cursor"},
    )
    assert bad.status_code == 422

    listed = client.get("/messages", headers=headers).json()
    by_id = {item["id"]: item for item in listed}
    assert by_id[latest]["conversation_id"] == "thread-9"
    assert by_id[latest]["new_email"] is None


def test_reply_all_drops_the_mailbox_and_a_patch_keeps_the_edited_list(client, register_user):
    headers, user_id, account_id = _mailbox(client, register_user)
    message_id = _message(
        user_id,
        account_id,
        "reply-1",
        subject="Waiver",
        from_name="Luis",
        from_email="luis@keystone.example",
        to_recipients=["Ana <ana@outlook.com>", "office@keystone.example"],
        cc_recipients=["bob@keystone.example"],
        reply_to=["Luis <replies@keystone.example>"],
    )
    with Session(engine) as session:
        from uuid import UUID

        from app.models.reply_draft import ReplyDraft, ReplyDraftStatus

        session.add(
            ReplyDraft(
                user_id=UUID(user_id),
                message_id=UUID(message_id),
                status=ReplyDraftStatus.ready,
                body="Thanks",
                source="user",
            )
        )
        session.commit()

    detail = client.get(f"/messages/{message_id}", headers=headers).json()
    assert detail["reply_to"] == ["Luis <replies@keystone.example>"]
    assert detail["reply_all_recipients"] == {
        "to": ["Luis <replies@keystone.example>"],
        "cc": ["office@keystone.example", "bob@keystone.example"],
    }
    assert "ana@outlook.com" not in str(detail["reply_all_recipients"])
    assert detail["can_edit_recipients"] is False
    assert detail["draft"]["recipients"] == {
        "to": ["Luis <replies@keystone.example>"],
        "cc": [],
    }

    saved = client.patch(
        f"/messages/{message_id}/draft",
        headers=headers,
        json={"body": "Thanks", "to": ["office@keystone.example"], "cc": ["bob@keystone.example"]},
    )
    assert saved.status_code == 200
    assert saved.json()["recipients"] == {
        "to": ["office@keystone.example"],
        "cc": ["bob@keystone.example"],
    }
    again = client.get(f"/messages/{message_id}", headers=headers).json()
    assert again["draft"]["recipients"]["to"] == ["office@keystone.example"]
    assert client.patch(
        f"/messages/{message_id}/draft",
        headers=headers,
        json={"to": ["not-an-email"]},
    ).status_code == 422
    foreign = client.get(f"/messages/{message_id}", headers=register_user("cara@example.com"))
    assert foreign.status_code == 404


def test_forward_echoes_the_source_and_the_requested_account(client, register_user):
    headers, user_id, account_id = _mailbox(client, register_user)
    source = _message(
        user_id,
        account_id,
        "src",
        subject="Original",
        body_text="Please see the note.",
        from_email="luis@keystone.example",
    )
    created = client.post(
        "/messages/new",
        headers=headers,
        json={
            "account_id": account_id,
            "to": ["maya@example.com"],
            "cc": [],
            "subject": "Fwd: Original",
            "body": "Please see the note.",
            "instructions": None,
            "material_ids": [],
            "forward_of_id": source,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["account_id"] == account_id
    assert body["account_email"] == "ana@outlook.com"
    assert body["new_email"]["forward_of_id"] == source
    assert body["new_email"]["to"] == ["maya@example.com"]
    assert body["new_email"]["attachments"] == []
    assert body["draft"]["body"] == "Please see the note."
    assert client.get(f"/messages/{body['id']}/conversation", headers=headers).json()["messages"] == []

    other = register_user("drew@example.com")
    other_id = _user_id(client, other)
    other_account = _add_account(other_id, email="drew@outlook.com")
    stolen = client.post(
        "/messages/new",
        headers=headers,
        json={
            "account_id": other_account,
            "to": ["maya@example.com"],
            "subject": "Fwd: Original",
            "body": "Please see the note.",
            "forward_of_id": source,
        },
    )
    assert stolen.status_code == 404
    missing_source = client.post(
        "/messages/new",
        headers=other,
        json={
            "account_id": other_account,
            "to": ["maya@example.com"],
            "subject": "Fwd: Original",
            "body": "Please see the note.",
            "forward_of_id": source,
        },
    )
    assert missing_source.status_code == 404


def test_staged_file_is_bound_on_save_and_rejected_when_it_is_not_ours(client, register_user):
    headers, user_id, account_id = _mailbox(client, register_user)
    upload = client.post(
        "/attachments",
        headers=headers,
        files={"file": ("waiver.pdf", b"%PDF-1.4 hello", "application/pdf")},
    )
    assert upload.status_code == 201
    uploaded = upload.json()
    assert uploaded["file_name"] == "waiver.pdf"
    assert uploaded["size_bytes"] == len(b"%PDF-1.4 hello")
    assert uploaded["content_type"] == "application/pdf"

    created = client.post(
        "/messages/new",
        headers=headers,
        json={
            "account_id": account_id,
            "to": ["maya@example.com"],
            "subject": "Waiver",
            "body": "Attached.",
            "attachment_ids": [uploaded["id"]],
        },
    )
    assert created.status_code == 201
    message_id = created.json()["id"]
    assert created.json()["draft"]["attachments"] == [
        {
            "id": uploaded["id"],
            "file_name": "waiver.pdf",
            "size_bytes": len(b"%PDF-1.4 hello"),
            "content_type": "application/pdf",
        }
    ]
    reloaded = client.get(f"/messages/{message_id}", headers=headers).json()
    assert reloaded["draft"]["attachments"][0]["id"] == uploaded["id"]

    source = _message(user_id, account_id, "mail", from_email="luis@keystone.example", from_name="Luis")
    with Session(engine) as session:
        from uuid import UUID

        from app.models.reply_draft import ReplyDraft, ReplyDraftStatus

        session.add(
            ReplyDraft(
                user_id=UUID(user_id),
                message_id=UUID(source),
                status=ReplyDraftStatus.ready,
                body="Reply",
                source="user",
            )
        )
        session.commit()
    second = client.post(
        "/attachments",
        headers=headers,
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert second.status_code == 201
    patched = client.patch(
        f"/messages/{source}/draft",
        headers=headers,
        json={"attachmentIds": [second.json()["id"]]},
    )
    assert patched.status_code == 200
    assert patched.json()["attachments"][0]["file_name"] == "note.txt"
    cleared = client.patch(
        f"/messages/{source}/draft",
        headers=headers,
        json={"attachmentIds": []},
    )
    assert cleared.status_code == 200
    assert cleared.json()["attachments"] == []

    stranger = register_user("erin@example.com")
    stranger_id = _user_id(client, stranger)
    _add_account(stranger_id, email="erin@outlook.com")
    assert client.post(
        "/messages/new",
        headers=stranger,
        json={
            "to": ["maya@example.com"],
            "subject": "Nope",
            "body": "Attached.",
            "attachment_ids": [uploaded["id"]],
        },
    ).status_code == 404
    huge = b"x" * (10 * 1024 * 1024 + 1)
    assert client.post(
        "/attachments",
        headers=headers,
        files={"file": ("big.pdf", huge, "application/pdf")},
    ).status_code == 413
    assert client.post(
        "/attachments",
        headers=headers,
        files={"file": ("run.exe", b"MZ", "application/octet-stream")},
    ).status_code == 415
    assert client.get(f"/messages/{uuid4()}", headers=headers).status_code == 404
