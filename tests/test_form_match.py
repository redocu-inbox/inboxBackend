"""Phase 9B: an on-demand match extracts values and fills when it can."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO
from uuid import UUID, uuid4

from pypdf import PdfReader
from sqlalchemy import event
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.db import engine
from app.models.connected_account import ConnectedAccount
from app.models.form_fill_job import FormFillJob
from app.models.message import Message
from app.services.forms.match import (
    choose_score,
    consider_form_match_on_sync,
    message_text,
    parse_extracted_object,
    template_tokens,
    tokens,
)
from app.worker.form_tasks import match_form_template, run_form_fill
from app.worker.mail_tasks import sync_mailbox_delta
from tests.pdf_fixtures import blank_pdf_bytes, text_field_pdf_bytes

_SAFETY = "safety-inspection-report"
_PAYROLL = "payroll-summary-notice"


class _Queue:
    def __init__(self):
        self.calls: list[tuple] = []

    async def enqueue_job(self, name, *args):
        self.calls.append((name, args))


def _upload(client, headers, data, filename="a.pdf"):
    return client.post(
        "/forms/templates",
        headers=headers,
        files={"file": (filename, data, "application/pdf")},
    )


def _tag(client, headers, template_id, tag):
    response = client.patch(
        f"/forms/templates/{template_id}",
        headers=headers,
        json={"tags": [tag]},
    )
    assert response.status_code == 200, response.text


def _message(user_id, *, provider, body_text, body_preview, subject="Please review"):
    with Session(engine) as session:
        account = ConnectedAccount(
            user_id=UUID(user_id),
            provider=provider,
            provider_account_id=f"{provider}-{uuid4()}",
            email=f"{provider}@example.com",
            access_token="access-token",
            refresh_token="refresh-token",
            token_expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            sync_generation=1,
            sync_status="idle",
        )
        session.add(account)
        session.flush()
        message = Message(
            user_id=UUID(user_id),
            connected_account_id=account.id,
            graph_id=f"graph-{uuid4()}",
            subject=subject,
            body_preview=body_preview,
            body_text=body_text,
            received_at=datetime.now(timezone.utc),
        )
        session.add(message)
        session.commit()
        return str(message.id)


def _run_match(job_id, queue):
    asyncio.run(match_form_template({"redis": queue, "job_id": "match-1"}, job_id))


def _values(data: bytes) -> dict[str, str]:
    fields = PdfReader(BytesIO(data)).get_fields() or {}
    return {
        name: str(field["/V"])
        for name, field in fields.items()
        if isinstance(field, dict) and "/V" in field
    }


def test_token_rules_ignore_short_words_and_case():
    assert tokens("Safety Inspection, W-9") == {"safety", "inspection"}
    assert template_tokens("Invoice.pdf", ["w9-form"], "Tax packet") == {"invoice", "form", "packet"}
    assert message_text("Hello", None, "preview text") == "Hello\npreview text"
    assert message_text("Hello", "body text", "preview text") == "Hello\nbody text"
    assert choose_score([2, 1]) == 0
    assert choose_score([1, 0]) is None
    assert choose_score([2, 2]) is None
    assert choose_score([4]) == 0
    assert parse_extracted_object("[stub llm]") is None
    assert parse_extracted_object("[]") is None
    assert parse_extracted_object("{}") is None
    assert parse_extracted_object('{"Name": "Ada"}') == {"Name": "Ada"}


def test_match_picks_the_tagged_template_for_outlook_and_gmail(client, register_user):
    headers = register_user("match@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    safety = _upload(client, headers, text_field_pdf_bytes("Name")).json()["id"]
    payroll = _upload(client, headers, text_field_pdf_bytes("Name"), filename="b.pdf").json()["id"]
    blank = _upload(client, headers, blank_pdf_bytes(), filename="c.pdf").json()["id"]
    _tag(client, headers, safety, _SAFETY)
    _tag(client, headers, payroll, _PAYROLL)
    _tag(client, headers, blank, _SAFETY)

    other = register_user("other-match@example.com")
    foreign = _upload(client, other, text_field_pdf_bytes("Name"), filename="d.pdf").json()["id"]
    _tag(client, other, foreign, _SAFETY)

    outlook = _message(
        user_id,
        provider="microsoft",
        body_text=None,
        body_preview="The safety inspection report is due.",
    )
    gmail = _message(
        user_id,
        provider="google",
        body_text="The safety inspection report is due.",
        body_preview="The payroll summary notice is also due.",
    )

    class Fake:
        def __init__(self):
            self.prompts = []

        async def complete(self, *, system, user):
            self.prompts.append((system, user))
            return '{"Name": "Ada"}'

    fake = Fake()
    statements = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        del conn, cursor, parameters, context, executemany
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        from app.services.forms import match as match_module

        original = match_module.get_llm
        match_module.get_llm = lambda: fake
        chosen = []
        for message_id in (outlook, gmail):
            queue = _Queue()
            created = client.post("/forms/jobs", headers=headers, json={"message_id": message_id})
            assert created.status_code == 200, created.text
            assert created.json()["status"] == "pending"
            client.app.state.arq.enqueue_job.assert_awaited_with("match_form_template", created.json()["id"])
            _run_match(created.json()["id"], queue)
            assert queue.calls == [("run_form_fill", (created.json()["id"],))]
            asyncio.run(run_form_fill({}, created.json()["id"]))
            body = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers).json()
            assert body["template_id"] == safety
            assert body["status"] == "completed"
            assert body["confidence"] == 0.75
            assert body["extracted_values"] == {"Name": "Ada"}
            downloaded = client.get(f"/forms/jobs/{created.json()['id']}/file", headers=headers)
            assert _values(downloaded.content)["Name"] == "Ada"
            chosen.append(body["template_id"])
        assert chosen == [safety, safety]
        assert all("JSON object" in system for system, _user in fake.prompts)
        outlook_prompt = fake.prompts[0][1]
        gmail_prompt = fake.prompts[1][1]
        assert "safety inspection report" in outlook_prompt
        assert "safety inspection report" in gmail_prompt
        assert "payroll" not in gmail_prompt
        assert all("<=>" not in statement and "embedding" not in statement.lower() for statement in statements)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        match_module.get_llm = original


def test_tie_or_a_score_of_one_fails_and_create_stays_200(client, register_user):
    headers = register_user("tie@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    first = _upload(client, headers, text_field_pdf_bytes("Name")).json()["id"]
    second = _upload(client, headers, text_field_pdf_bytes("Name"), filename="b.pdf").json()["id"]
    _tag(client, headers, first, "alpha-beta")
    _tag(client, headers, second, "alpha-beta")
    tied = _message(
        user_id,
        provider="microsoft",
        body_text="alpha beta arrived",
        body_preview=None,
    )
    created = client.post("/forms/jobs", headers=headers, json={"message_id": tied})
    assert created.status_code == 200, created.text
    queue = _Queue()
    _run_match(created.json()["id"], queue)
    body = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers).json()
    assert body["status"] == "failed"
    assert body["error"] == "No matching form."
    assert body["template_id"] is None
    assert queue.calls == []

    weak = _upload(client, headers, text_field_pdf_bytes("Name"), filename="c.pdf").json()["id"]
    _tag(client, headers, weak, "invoice")
    single = _message(
        user_id,
        provider="google",
        body_text="Please send the invoice",
        body_preview=None,
    )
    # The two alpha-beta templates still exist and score 0. invoice scores 1.
    created = client.post("/forms/jobs", headers=headers, json={"message_id": single})
    assert created.status_code == 200
    _run_match(created.json()["id"], _Queue())
    body = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers).json()
    assert body["status"] == "failed"
    assert body["error"] == "No matching form."


def test_stub_stops_for_review_without_a_file(client, register_user, tmp_path, caplog):
    headers = register_user("stub@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    template_id = _upload(client, headers, text_field_pdf_bytes("Name")).json()["id"]
    message_id = _message(
        user_id,
        provider="microsoft",
        body_text="Ada Lovelace starts Monday",
        body_preview=None,
    )
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"template_id": template_id, "message_id": message_id},
    )
    assert created.status_code == 200, created.text
    for name, obj in logging.root.manager.loggerDict.items():
        if isinstance(obj, logging.Logger) and (name == "app" or name.startswith("app.")):
            obj.disabled = False
    caplog.set_level(logging.INFO)
    queue = _Queue()
    _run_match(created.json()["id"], queue)
    body = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers).json()
    assert body["status"] == "needs_review"
    assert body["confidence"] == 0
    assert body["extracted_values"] == {}
    assert body["file_ready"] is False
    assert body["template_id"] == template_id
    assert queue.calls == []
    assert list(tmp_path.rglob("filled.pdf")) == []
    assert "Ada Lovelace" not in caplog.text


def test_chosen_template_uses_confidence_half_and_fills(client, register_user):
    headers = register_user("chosen@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    template_id = _upload(client, headers, text_field_pdf_bytes("Name")).json()["id"]
    _tag(client, headers, template_id, _SAFETY)
    message_id = _message(
        user_id,
        provider="google",
        body_text="The safety inspection report names Ada",
        body_preview=None,
    )

    class Fake:
        async def complete(self, *, system, user):
            del system, user
            return '{"Name": "Ada"}'

    from app.services.forms import match as match_module

    original = match_module.get_llm
    match_module.get_llm = lambda: Fake()
    try:
        created = client.post(
            "/forms/jobs",
            headers=headers,
            json={"template_id": template_id, "message_id": message_id},
        )
        job_id = created.json()["id"]
        _run_match(job_id, _Queue())
        asyncio.run(run_form_fill({}, job_id))
    finally:
        match_module.get_llm = original
    body = client.get(f"/forms/jobs/{job_id}", headers=headers).json()
    assert body["status"] == "completed"
    assert body["confidence"] == 0.5
    assert body["template_id"] == template_id
    assert _values(client.get(f"/forms/jobs/{job_id}/file", headers=headers).content)["Name"] == "Ada"


def test_values_without_a_template_is_422(client, register_user):
    headers = register_user("values@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    message_id = _message(user_id, provider="microsoft", body_text="Hello", body_preview=None)
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"message_id": message_id, "values": {"Name": "Ada"}},
    )
    assert created.status_code == 422
    assert created.json()["detail"] == "A template is required when values are sent."
    client.app.state.arq.enqueue_job.assert_not_awaited()


def test_sync_does_not_enqueue_a_match(client, register_user, monkeypatch):
    headers = register_user("sync-match@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    assert Settings(_env_file=None, app_env="development").form_match_on_sync is False
    seen = []
    real = consider_form_match_on_sync

    def _spy(ctx, account_id):
        seen.append((ctx.get("redis"), account_id))
        real(ctx, account_id)

    monkeypatch.setattr("app.worker.mail_tasks.consider_form_match_on_sync", _spy)
    monkeypatch.setattr(
        "app.worker.mail_tasks._graph_get",
        lambda url, token: {"value": []},
    )
    monkeypatch.setattr("app.worker.mail_tasks.collect_gmail_messages", lambda token: [])

    def _account(provider):
        with Session(engine) as session:
            account = ConnectedAccount(
                user_id=UUID(user_id),
                provider=provider,
                provider_account_id=f"sync-{provider}-{uuid4()}",
                email=f"{provider}@example.com",
                access_token="access-token",
                refresh_token="refresh-token",
                token_expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                sync_generation=1,
                sync_status="idle",
            )
            session.add(account)
            session.commit()
            return account.id

    queue = _Queue()
    for provider in ("microsoft", "google"):
        account_id = _account(provider)
        asyncio.run(sync_mailbox_delta({"job_id": "sync-1", "redis": queue}, str(account_id), 1))
    assert len(seen) == 2
    assert queue.calls == []
    with Session(engine) as session:
        own_jobs = session.exec(select(FormFillJob).where(FormFillJob.user_id == UUID(user_id))).all()
        assert own_jobs == []

    monkeypatch.setenv("FORM_MATCH_ON_SYNC", "1")
    get_settings.cache_clear()
    try:
        consider_form_match_on_sync({"job_id": "sync-2", "redis": queue}, uuid4())
        assert queue.calls == []
    finally:
        monkeypatch.delenv("FORM_MATCH_ON_SYNC", raising=False)
        get_settings.cache_clear()
