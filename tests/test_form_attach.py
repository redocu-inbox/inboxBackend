"""Phase 10: suggest a fill on the message, and attach it to an unsent Outlook draft."""

import base64
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlmodel import Session

from app.api.messages import _MAX_ATTACHMENT_BYTES
import app.storage as storage_module
from app.db import engine
from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.form_template import FormTemplate
from app.models.reply_draft import ReplyDraft
from app.services.graph.drafts import GraphDraftError
from tests.pdf_fixtures import text_field_pdf_bytes
from tests.test_frontend_connect import DRAFT_KEYS, LIST_KEYS, _add_account, _add_message, _user_id
from tests.test_reply_drafts import _fresh_token, _insert_draft, _mailbox

_SUGGESTED_KEYS = {
    "job_id",
    "template_id",
    "template_filename",
    "status",
    "confidence",
}
_T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)


def _template(user_id: str, filename: str) -> str:
    with Session(engine) as session:
        template = FormTemplate(
            user_id=UUID(user_id),
            filename=filename,
            storage_path=f"forms/{user_id}/unused/{filename}",
            mime_type="application/pdf",
            tags=[],
            form_schema={"kind": "acroform", "fields": []},
        )
        session.add(template)
        session.commit()
        return str(template.id)


def _job(
    user_id: str,
    template_id: str,
    message_id: str | None,
    *,
    status: FormFillStatus,
    created_at: datetime,
    confidence: float | None = 1.0,
    pdf: bytes | None = None,
) -> str:
    job_id = uuid4()
    filled = None
    if pdf is not None:
        filled = f"forms/{user_id}/{job_id}/filled.pdf"
        storage_module.get_storage().put(filled, pdf, "application/pdf")
    with Session(engine) as session:
        session.add(
            FormFillJob(
                id=job_id,
                user_id=UUID(user_id),
                template_id=UUID(template_id),
                message_id=UUID(message_id) if message_id is not None else None,
                status=status,
                confidence=confidence,
                extracted_values={"Name": "Ada"} if pdf is not None else {},
                dropped_fields=[],
                filled_file_path=filled,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.commit()
    return str(job_id)


def _suggested(payload: dict, **expected) -> None:
    assert set(payload) == _SUGGESTED_KEYS
    assert payload == expected


def test_suggested_form_is_the_newest_reviewable_job_for_either_provider(client, register_user):
    headers, user_id, _account_id, outlook_id = _mailbox(client, register_user)
    gmail_account = _add_account(user_id, provider="google", email="ana@gmail.com")
    gmail_id = _add_message(
        user_id,
        gmail_account,
        graph_id="gmail-1",
        received_at=datetime(2024, 6, 2, tzinfo=timezone.utc),
        priority_score=None,
        subject="Payroll",
    )
    invoice = _template(user_id, "invoice.pdf")
    safety = _template(user_id, "safety.pdf")
    older = _job(
        user_id,
        invoice,
        outlook_id,
        status=FormFillStatus.completed,
        created_at=_T0,
        confidence=1.0,
    )
    _job(
        user_id,
        safety,
        outlook_id,
        status=FormFillStatus.rejected,
        created_at=_T0 + timedelta(minutes=1),
        confidence=0.25,
    )
    gmail_job = _job(
        user_id,
        safety,
        gmail_id,
        status=FormFillStatus.completed,
        created_at=_T0,
        confidence=0.75,
    )

    listed = client.get("/messages", headers=headers)
    assert listed.status_code == 200
    by_id = {item["id"]: item for item in listed.json()}
    for item in by_id.values():
        assert set(item) == LIST_KEYS
        assert "body_text" not in item
        assert "draft" not in item
    _suggested(
        by_id[outlook_id]["suggested_form"],
        job_id=older,
        template_id=invoice,
        template_filename="invoice.pdf",
        status="completed",
        confidence=1.0,
    )
    _suggested(
        by_id[gmail_id]["suggested_form"],
        job_id=gmail_job,
        template_id=safety,
        template_filename="safety.pdf",
        status="completed",
        confidence=0.75,
    )

    detail = client.get(f"/messages/{outlook_id}", headers=headers).json()
    assert set(detail) == LIST_KEYS | {"body_text", "draft"}
    assert detail["suggested_form"] == by_id[outlook_id]["suggested_form"]
    gmail_detail = client.get(f"/messages/{gmail_id}", headers=headers).json()
    assert gmail_detail["suggested_form"] == by_id[gmail_id]["suggested_form"]

    newer = _job(
        user_id,
        safety,
        outlook_id,
        status=FormFillStatus.needs_review,
        created_at=_T0 + timedelta(minutes=2),
        confidence=0.5,
    )
    shown = client.get(f"/messages/{outlook_id}", headers=headers).json()["suggested_form"]
    _suggested(
        shown,
        job_id=newer,
        template_id=safety,
        template_filename="safety.pdf",
        status="needs_review",
        confidence=0.5,
    )
    _job(
        user_id,
        invoice,
        outlook_id,
        status=FormFillStatus.failed,
        created_at=_T0 + timedelta(minutes=3),
        confidence=0,
    )
    still = client.get(f"/messages/{outlook_id}", headers=headers).json()["suggested_form"]
    assert still["job_id"] == newer


def test_reject_hides_the_job_and_refill_runs_the_filler_again(client, register_user):
    headers, user_id, _account_id, message_id = _mailbox(client, register_user, email="reject@example.com")
    template_id = _template(user_id, "invoice.pdf")
    pdf = text_field_pdf_bytes("Name")
    job_id = _job(
        user_id,
        template_id,
        message_id,
        status=FormFillStatus.completed,
        created_at=_T0,
        pdf=pdf,
    )
    assert client.get(f"/messages/{message_id}", headers=headers).json()["suggested_form"]["job_id"] == job_id

    rejected = client.post(f"/forms/jobs/{job_id}/reject", headers=headers)
    assert rejected.status_code == 200
    assert rejected.json() == {"id": job_id, "status": "rejected"}
    assert client.get(f"/messages/{message_id}", headers=headers).json()["suggested_form"] is None
    downloaded = client.get(f"/forms/jobs/{job_id}/file", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == pdf

    other = register_user("other-reject@example.com")
    foreign = client.post(f"/forms/jobs/{job_id}/reject", headers=other)
    assert foreign.status_code == 404
    assert foreign.json() == {"detail": "Fill job not found"}

    client.app.state.arq.enqueue_job.reset_mock()
    refilled = client.post(
        f"/forms/jobs/{job_id}/refill",
        headers=headers,
        json={"values": {"Name": "Ada"}},
    )
    assert refilled.status_code == 200
    assert refilled.json() == {"id": job_id, "status": "pending"}
    client.app.state.arq.enqueue_job.assert_awaited_once_with("run_form_fill", job_id)
    assert client.get(f"/forms/jobs/{job_id}", headers=headers).json()["file_ready"] is False


def _silence_providers(monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("provider was called")

    monkeypatch.setattr("app.services.graph.drafts._graph_request", refuse)
    monkeypatch.setattr("app.services.graph.drafts.urlopen", refuse)
    monkeypatch.setattr("app.services.google.http.google_get", refuse)
    monkeypatch.setattr("app.services.google.messages.collect_gmail_messages", refuse)
    monkeypatch.setattr(
        "app.api.messages.refresh_access_token",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("refresh")),
    )


def _attach(client, headers, message_id, job_id):
    return client.post(
        f"/messages/{message_id}/draft/attachments",
        headers=headers,
        json={"form_fill_job_id": job_id},
    )


def test_attachment_posts_the_stored_pdf_and_does_not_send(client, register_user, monkeypatch):
    headers, user_id, account_id, message_id = _mailbox(client, register_user, email="attach@example.com")
    _fresh_token(account_id)
    _insert_draft(user_id, message_id, body="Hello there", source="user")
    template_id = _template(user_id, "invoice.pdf")
    pdf = text_field_pdf_bytes("Name")
    job_id = _job(
        user_id,
        template_id,
        message_id,
        status=FormFillStatus.completed,
        created_at=_T0,
        pdf=pdf,
    )
    calls = []

    def fake_request(method, url, access_token, payload):
        assert "send" not in url.lower()
        assert access_token == "secret-access-token"
        calls.append((method, url, payload))
        if url.endswith("/createReply"):
            return {"id": "outlook-draft-1"}
        return {}

    monkeypatch.setattr("app.services.graph.drafts._graph_request", fake_request)
    monkeypatch.setattr(
        "app.services.graph.drafts.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("socket opened")),
    )
    monkeypatch.setattr(
        "app.api.messages.refresh_access_token",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("refresh")),
    )

    attached = _attach(client, headers, message_id, job_id)
    assert attached.status_code == 200, attached.text
    body = attached.json()
    assert set(body) == DRAFT_KEYS
    assert body["graph_draft_id"] == "outlook-draft-1"
    assert body["body"] == "Hello there"
    assert body["status"] == "ready"
    assert body["source"] == "user"
    assert body["error"] is None
    assert [item[0] for item in calls] == ["POST", "PATCH", "POST"]
    assert [item[1] for item in calls] == [
        "https://graph.microsoft.com/v1.0/me/messages/graph-msg-1/createReply",
        "https://graph.microsoft.com/v1.0/me/messages/outlook-draft-1",
        "https://graph.microsoft.com/v1.0/me/messages/outlook-draft-1/attachments",
    ]
    attachment = calls[2][2]
    assert attachment["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert attachment["name"] == "invoice.pdf"
    assert base64.b64decode(attachment["contentBytes"]) == pdf
    assert all("send" not in url.lower() for _method, url, _payload in calls)

    calls.clear()
    with Session(engine) as session:
        draft = session.get(ReplyDraft, UUID(body["id"]))
        draft.graph_draft_id = "already-there"
        session.add(draft)
        session.commit()
    again = _attach(client, headers, message_id, job_id)
    assert again.status_code == 200
    assert again.json()["graph_draft_id"] == "already-there"
    assert [item[1] for item in calls] == [
        "https://graph.microsoft.com/v1.0/me/messages/already-there/attachments",
    ]


def test_attachment_refuses_gmail_and_bad_jobs(client, register_user, monkeypatch):
    _silence_providers(monkeypatch)
    headers, user_id, _account_id, message_id = _mailbox(
        client, register_user, email="gmail-attach@example.com", provider="google", graph_id="gmail-msg"
    )
    _insert_draft(user_id, message_id, body="Hello there")
    template_id = _template(user_id, "invoice.pdf")
    pdf = text_field_pdf_bytes("Name")
    job_id = _job(
        user_id,
        template_id,
        message_id,
        status=FormFillStatus.completed,
        created_at=_T0,
        pdf=pdf,
    )
    gmail = _attach(client, headers, message_id, job_id)
    assert gmail.status_code == 422
    assert gmail.json() == {"detail": "Only an Outlook mailbox can save this draft."}
    downloaded = client.get(f"/forms/jobs/{job_id}/file", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == pdf

    outlook_headers, outlook_user, outlook_account, outlook_message = _mailbox(
        client, register_user, email="outlook-attach@example.com", graph_id="graph-msg-2"
    )
    _fresh_token(outlook_account)
    _insert_draft(outlook_user, outlook_message, body="Hello there")
    outlook_template = _template(outlook_user, "invoice.pdf")
    rejected_job = _job(
        outlook_user,
        outlook_template,
        outlook_message,
        status=FormFillStatus.rejected,
        created_at=_T0,
        pdf=pdf,
    )
    rejected = _attach(client, outlook_headers, outlook_message, rejected_job)
    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "This form is not ready to attach."}

    other_message = _add_message(
        outlook_user,
        outlook_account,
        graph_id="graph-msg-3",
        received_at=datetime(2024, 6, 3, tzinfo=timezone.utc),
        priority_score=None,
    )
    _insert_draft(outlook_user, other_message, body="Hello there")
    completed = _job(
        outlook_user,
        outlook_template,
        outlook_message,
        status=FormFillStatus.completed,
        created_at=_T0,
        pdf=pdf,
    )
    wrong_message = _attach(client, outlook_headers, other_message, completed)
    assert wrong_message.status_code == 409
    assert wrong_message.json() == {"detail": "This form belongs to another message."}

    huge = b"%PDF" + b"0" * (_MAX_ATTACHMENT_BYTES - 3)
    assert len(huge) == _MAX_ATTACHMENT_BYTES + 1
    too_big = _job(
        outlook_user,
        outlook_template,
        outlook_message,
        status=FormFillStatus.completed,
        created_at=_T0,
        pdf=huge,
    )
    oversized = _attach(client, outlook_headers, outlook_message, too_big)
    assert oversized.status_code == 409
    assert oversized.json() == {"detail": "PDF must be 3 MB or smaller."}

    exact = b"%PDF" + b"0" * (_MAX_ATTACHMENT_BYTES - 4)
    assert len(exact) == _MAX_ATTACHMENT_BYTES
    exact_job = _job(
        outlook_user,
        outlook_template,
        outlook_message,
        status=FormFillStatus.completed,
        created_at=_T0,
        pdf=exact,
    )

    calls = []

    def fake_request(method, url, access_token, payload):
        calls.append(url)
        if url.endswith("/createReply"):
            return {"id": "outlook-draft-exact"}
        return {}

    monkeypatch.setattr("app.services.graph.drafts._graph_request", fake_request)
    exact_response = _attach(client, outlook_headers, outlook_message, exact_job)
    assert exact_response.status_code == 200, exact_response.text
    assert calls[-1].endswith("/attachments")
    assert "send" not in calls[-1].lower()

    bob = register_user("bob-attach@example.com")
    foreign_message = _attach(client, bob, outlook_message, completed)
    assert foreign_message.status_code == 404
    assert foreign_message.json() == {"detail": "Message not found."}
    bob_headers, bob_user, bob_account, bob_message = _mailbox(
        client, register_user, email="bob-own@example.com", graph_id="bob-graph"
    )
    _fresh_token(bob_account)
    _insert_draft(bob_user, bob_message, body="Hello there")
    foreign_job = _attach(client, bob_headers, bob_message, completed)
    assert foreign_job.status_code == 404
    assert foreign_job.json() == {"detail": "Fill job not found"}


def test_attachment_graph_failure_keeps_the_local_draft(client, register_user, monkeypatch):
    headers, user_id, account_id, message_id = _mailbox(client, register_user, email="fail-attach@example.com")
    _fresh_token(account_id)
    _insert_draft(user_id, message_id, body="Hello there", source="user")
    template_id = _template(user_id, "invoice.pdf")
    job_id = _job(
        user_id,
        template_id,
        message_id,
        status=FormFillStatus.completed,
        created_at=_T0,
        pdf=b"%PDF-1.4 saved",
    )
    calls = []

    def fake_request(method, url, access_token, payload):
        assert "send" not in url.lower()
        calls.append(url)
        if url.endswith("/createReply"):
            return {"id": "outlook-draft-kept"}
        if url.endswith("/attachments"):
            raise GraphDraftError("attachment failed")
        return {}

    monkeypatch.setattr("app.services.graph.drafts._graph_request", fake_request)
    monkeypatch.setattr(
        "app.services.graph.drafts.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("socket opened")),
    )

    failed = _attach(client, headers, message_id, job_id)
    assert failed.status_code == 502
    assert failed.json() == {"detail": "Outlook draft could not be saved."}
    assert "Hello there" not in failed.text
    assert "secret-access-token" not in failed.text
    assert b"%PDF-1.4 saved" not in failed.content
    saved = client.get(f"/messages/{message_id}/draft", headers=headers).json()
    assert saved["status"] == "ready"
    assert saved["body"] == "Hello there"
    assert saved["graph_draft_id"] == "outlook-draft-kept"
    assert saved["error"] == "outlook draft failed"
    assert calls == [
        "https://graph.microsoft.com/v1.0/me/messages/graph-msg-1/createReply",
        "https://graph.microsoft.com/v1.0/me/messages/outlook-draft-kept",
        "https://graph.microsoft.com/v1.0/me/messages/outlook-draft-kept/attachments",
    ]
