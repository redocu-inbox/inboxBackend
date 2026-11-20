"""The deletion request is durable and fences account writes."""

import asyncio
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlmodel import Session, select

from app import storage
from app.db import engine
from app.models.account_deletion import AccountDeletion
from app.models.connected_account import ConnectedAccount
from app.models.form_fill_job import FormFillJob
from app.models.form_template import FormTemplate
from app.models.message import Message
from app.models.password_reset import PasswordResetToken
from app.models.reply_draft import ReplyDraft
from app.models.staged_attachment import StagedAttachment
from app.models.user import User, utcnow
from app.services.account_deletion import purge_pending_accounts
from app.services.forms.filler import PdfFormFiller
from app.worker.form_tasks import run_form_fill
from app.worker.mail_tasks import sync_mailbox_delta
from app.worker.settings import WorkerSettings
from app.worker.tasks import purge_deleted_accounts, send_password_reset
from tests.pdf_fixtures import text_field_pdf_bytes
from tests.test_frontend_connect import _add_account, _add_message


def _upload_template(client, headers) -> str:
    response = client.post(
        "/forms/templates",
        headers=headers,
        files={"file": ("form.pdf", text_field_pdf_bytes("Name"), "application/pdf")},
    )
    assert response.status_code == 200
    return response.json()["id"]


def _upload_attachment(client, headers) -> UUID:
    response = client.post(
        "/attachments",
        headers=headers,
        files={"file": ("attachment.pdf", text_field_pdf_bytes("Name"), "application/pdf")},
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


def test_request_is_durable_and_blocks_access_for_only_its_owner(client, register_user, delete_account):
    headers = register_user("delete@example.com")
    other = register_user("other@example.com")
    user_id = UUID(client.get("/users/me", headers=headers).json()["id"])
    assert client.delete("/users/me").status_code == 401

    accepted = delete_account(headers, " DELETE@example.com ")
    repeated = delete_account(headers, "delete@example.com")
    assert accepted.status_code == repeated.status_code == 202
    assert accepted.json() == repeated.json()
    assert accepted.json()["status"] == "pending"
    assert accepted.json()["requested_at"]
    assert client.get("/users/me", headers=headers).status_code == 401
    assert client.patch("/users/me", headers=headers, json={"display_name": "Changed"}).status_code == 401
    assert client.post(
        "/auth/login", json={"email": "delete@example.com", "password": "correct-horse-battery"}
    ).status_code == 401
    assert client.get("/users/me", headers=other).status_code == 200
    client.app.state.arq.enqueue_job.assert_not_awaited()

    with Session(engine) as session:
        assert len(session.exec(select(AccountDeletion)).all()) == 1
        session.delete(session.get(User, user_id))
        session.commit()
    with Session(engine) as session:
        assert session.get(AccountDeletion, user_id) is not None


@pytest.mark.parametrize(
    ("body", "expected_status", "detail"),
    [
        ({"confirmation_email": "confirm@example.com"}, 422, "invalid_deletion_confirmation"),
        ({"confirmation_email": "confirm@example.com", "password": "secret" * 30}, 422, "invalid_deletion_confirmation"),
        ({"confirmation_email": "confirm@example.com", "password": "secret", "unknown": "private input"}, 422, "invalid_deletion_confirmation"),
        ({"confirmation_email": "other@example.com", "password": "correct-horse-battery"}, 422, "confirmation_email_mismatch"),
        ({"confirmation_email": "confirm@example.com", "password": "wrong password"}, 403, "incorrect_password"),
        ({"confirmation_email": "confirm@example.com", "password": " correct-horse-battery "}, 403, "incorrect_password"),
        ({"confirmation_email": "confirm@example.com", "password": "correct-horse-battery", "mfa_code": "123456"}, 422, "mfa_verification_unavailable"),
    ],
)
def test_confirmation_failure_preserves_account(client, register_user, deletion_rate_store, body, expected_status, detail):
    headers = register_user("confirm@example.com")
    response = client.request("DELETE", "/users/me", headers=headers, json=body)
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert client.get("/users/me", headers=headers).status_code == 200
    with Session(engine) as session:
        assert session.exec(select(AccountDeletion)).first() is None


def test_rate_limit_and_store_failure_do_not_accept_deletion(client, register_user, deletion_rate_store, delete_account):
    headers = register_user("limited@example.com")
    deletion_rate_store.eval.return_value = 6
    limited = delete_account(headers, "limited@example.com")
    assert limited.status_code == 429
    assert limited.json() == {"detail": "rate_limited"}

    deletion_rate_store.eval.side_effect = ConnectionError
    unavailable = delete_account(headers, "limited@example.com")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "deletion_confirmation_unavailable"}
    assert client.get("/users/me", headers=headers).status_code == 200
    with Session(engine) as session:
        assert session.exec(select(AccountDeletion)).first() is None


def test_deletion_can_retry_after_a_request_that_already_committed(client, register_user, delete_account):
    headers = register_user("busy@example.com")
    user_id = UUID(client.get("/users/me", headers=headers).json()["id"])
    template_id = _upload_template(client, headers)

    async def check_during_enqueue(_name, job_id):
        with Session(engine) as session:
            assert session.get(FormFillJob, UUID(job_id)) is not None
        response = await asyncio.to_thread(delete_account, headers, "busy@example.com")
        assert response.status_code == 409
        assert response.headers["Retry-After"] == "1"

    client.app.state.arq.enqueue_job.side_effect = check_during_enqueue
    created = client.post(
        "/forms/jobs", headers=headers, json={"template_id": template_id, "values": {"Name": "Ada"}}
    )
    assert created.status_code == 200
    with Session(engine) as session:
        assert session.get(AccountDeletion, user_id) is None
    assert delete_account(headers, "busy@example.com").status_code == 202


def test_queued_mailbox_and_password_reset_work_skip_deleting_user(client, register_user, monkeypatch, delete_account):
    headers = register_user("queued@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    account_id = _add_account(user_id, provider="google", email="queued@gmail.com")
    monkeypatch.setattr("app.api.auth.enforce_password_reset_limit", Mock())
    provider = Mock()
    monkeypatch.setattr("app.worker.mail_tasks.collect_gmail_messages", provider)
    monkeypatch.setattr("app.worker.mail_tasks.refresh_google_access_token", provider)
    assert client.post("/auth/forgot-password", json={"email": "queued@example.com"}).status_code == 202
    _, _, request_id = client.app.state.arq.enqueue_job.await_args.args
    assert delete_account(headers, "queued@example.com").status_code == 202

    asyncio.run(send_password_reset({}, user_id, request_id))
    asyncio.run(sync_mailbox_delta({}, account_id))
    with Session(engine) as session:
        assert session.exec(select(PasswordResetToken)).first() is None
    provider.assert_not_called()
    assert client.post("/auth/forgot-password", json={"email": "queued@example.com"}).status_code == 202
    assert client.app.state.arq.enqueue_job.await_count == 1


@pytest.mark.parametrize("purge_before_save", [False, True])
def test_form_fill_cannot_save_after_deletion_is_requested(
    client, register_user, tmp_path, monkeypatch, purge_before_save, delete_account
):
    headers = register_user("filling@example.com")
    template_id = _upload_template(client, headers)
    created = client.post(
        "/forms/jobs", headers=headers, json={"template_id": template_id, "values": {"Name": "Ada"}}
    )
    assert created.status_code == 200
    job_id = created.json()["id"]

    class DeletingFiller(PdfFormFiller):
        def fill(self, template, values):
            result = super().fill(template, values)
            assert delete_account(headers, "filling@example.com").status_code == 202
            if purge_before_save:
                purge_pending_accounts()
            return result

    monkeypatch.setattr("app.services.forms.fill_job.get_filler", lambda _mime: DeletingFiller())
    asyncio.run(run_form_fill({}, job_id))

    with Session(engine) as session:
        job = session.get(FormFillJob, UUID(job_id))
        if purge_before_save:
            assert job is None
        else:
            assert job.status == "running"
            assert job.filled_file_path is None
    assert list(tmp_path.rglob("filled.pdf")) == []


def test_worker_purges_only_owned_rows_and_files(client, register_user, tmp_path, delete_account):
    headers = register_user("purge@example.com")
    other = register_user("keep@example.com")
    user_id = UUID(client.get("/users/me", headers=headers).json()["id"])
    other_id = UUID(client.get("/users/me", headers=other).json()["id"])
    template_id = UUID(_upload_template(client, headers))
    attachment_id = _upload_attachment(client, headers)
    other_attachment = _upload_attachment(client, other)
    account_id = _add_account(str(user_id))
    message_id = _add_message(
        str(user_id), account_id, graph_id="message", received_at=utcnow(), priority_score=None
    )
    assert client.post(f"/messages/{message_id}/draft", headers=headers).status_code == 200

    created = client.post(
        "/forms/jobs", headers=headers, json={"template_id": str(template_id), "values": {"Name": "Ada"}}
    )
    assert created.status_code == 200
    job_id = created.json()["id"]
    asyncio.run(run_form_fill({}, job_id))
    assert (tmp_path / f"forms/{user_id}/{job_id}/filled.pdf").is_file()
    storage.get_storage().put(f"forms/{user_id}/orphan.pdf", b"orphan")
    other_file = tmp_path / f"attachments/{other_id}/{other_attachment}"
    (tmp_path / f"attachments/{user_id}/link").symlink_to(other_file)

    assert delete_account(headers, "purge@example.com").status_code == 202
    asyncio.run(purge_deleted_accounts({}))
    asyncio.run(purge_deleted_accounts({}))

    with Session(engine) as session:
        assert session.get(AccountDeletion, user_id).local_purged_at is not None
        assert session.get(User, user_id) is None
        assert session.get(ConnectedAccount, UUID(account_id)) is None
        assert session.get(Message, UUID(message_id)) is None
        assert session.exec(select(ReplyDraft).where(ReplyDraft.user_id == user_id)).first() is None
        assert session.get(FormTemplate, template_id) is None
        assert session.get(FormFillJob, UUID(job_id)) is None
        assert session.get(StagedAttachment, attachment_id) is None
        assert session.get(StagedAttachment, other_attachment) is not None
    assert not (tmp_path / f"forms/{user_id}").exists()
    assert not (tmp_path / f"attachments/{user_id}").exists()
    assert other_file.is_file()
    assert client.get("/users/me", headers=other).status_code == 200


def test_worker_retries_after_removing_only_some_files(client, register_user, tmp_path, monkeypatch, delete_account):
    headers = register_user("retry@example.com")
    other = register_user("next@example.com")
    user_id = UUID(client.get("/users/me", headers=headers).json()["id"])
    other_id = UUID(client.get("/users/me", headers=other).json()["id"])
    template_id = UUID(_upload_template(client, headers))
    attachment_id = _upload_attachment(client, headers)
    assert delete_account(headers, "retry@example.com").status_code == 202
    assert delete_account(other, "next@example.com").status_code == 202

    file_store = storage.get_storage()
    delete_tree = file_store.delete_tree

    def delete_until_attachment(prefix):
        if prefix == f"attachments/{user_id}":
            raise PermissionError("storage temporarily unavailable")
        delete_tree(prefix)

    with monkeypatch.context() as failed_attempt:
        failed_attempt.setattr(file_store, "delete_tree", delete_until_attachment)
        asyncio.run(purge_deleted_accounts({}))

    with Session(engine) as session:
        assert session.get(AccountDeletion, user_id).local_purged_at is None
        assert session.get(User, user_id) is not None
        assert session.get(FormTemplate, template_id) is not None
        assert session.get(AccountDeletion, other_id).local_purged_at is not None
        assert session.get(User, other_id) is None
    assert not (tmp_path / f"forms/{user_id}").exists()
    assert (tmp_path / f"attachments/{user_id}/{attachment_id}").is_file()

    asyncio.run(purge_deleted_accounts({}))
    with Session(engine) as session:
        assert session.get(AccountDeletion, user_id).local_purged_at is not None
        assert session.get(User, user_id) is None
    assert not (tmp_path / f"attachments/{user_id}").exists()


def test_purge_is_scheduled_at_startup_and_every_minute():
    job = next(job for job in WorkerSettings.cron_jobs if job.coroutine is purge_deleted_accounts)
    assert job.run_at_startup
    assert job.minute is None
    assert job.second == 0
