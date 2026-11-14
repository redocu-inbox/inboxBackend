"""Local reply drafts, the unsent Outlook push, and the stub LLM fallback."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.main import app
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.models.reply_draft import ReplyDraft, ReplyDraftStatus
from app.services.graph.drafts import GraphDraftError
from app.services.graph.tokens import TokenRefreshError
from app.services.llm.factory import get_llm
from app.services.llm.providers import AnthropicClient, OpenAIClient, _anthropic_text, _openai_text
from app.services.llm.stub import StubLLM
from app.services.reply_drafts.generation import STUB_REPLY_SENTENCE, final_reply_body
from app.worker.settings import WorkerSettings
from app.worker.tasks import generate_reply_draft
from tests.test_frontend_connect import DRAFT_KEYS, LIST_KEYS, _add_account, _add_message, _user_id

_CONTRACT = {
    "id",
    "message_id",
    "status",
    "body",
    "source",
    "graph_draft_id",
    "error",
    "updated_at",
}


def _fresh_token(account_id: str) -> None:
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        account.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
        account.refresh_token = "refresh-secret"
        session.add(account)
        session.commit()


def _mailbox(client, register_user, *, email="ana@example.com", provider="microsoft", graph_id="graph-msg-1"):
    headers = register_user(email)
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, provider=provider, email=f"{email}.box")
    message_id = _add_message(
        user_id,
        account_id,
        graph_id=graph_id,
        received_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
        priority_score=None,
        subject="Roof leak",
        body_text="The roof leaks.",
    )
    return headers, user_id, account_id, message_id


def _insert_draft(user_id: str, message_id: str, **fields) -> None:
    with Session(engine) as session:
        session.add(
            ReplyDraft(
                user_id=UUID(user_id),
                message_id=UUID(message_id),
                status=fields.pop("status", ReplyDraftStatus.ready),
                body=fields.pop("body", "Hello there"),
                source=fields.pop("source", "llm"),
                **fields,
            )
        )
        session.commit()


def _assert_contract(payload: dict, message_id: str) -> None:
    assert set(payload) == _CONTRACT
    assert set(payload) == DRAFT_KEYS
    assert "created_at" not in payload
    assert payload["message_id"] == message_id


def test_draft_routes_keep_the_contract_and_leave_the_list_alone(client, register_user):
    headers, _user_id, _account_id, message_id = _mailbox(client, register_user)
    created = client.post(f"/messages/{message_id}/draft", headers=headers)
    assert created.status_code == 200
    body = created.json()
    _assert_contract(body, message_id)
    assert body["status"] == "pending"
    assert body["body"] is None
    assert body["source"] is None
    assert body["graph_draft_id"] is None
    assert body["error"] is None
    app.state.arq.enqueue_job.assert_awaited_once_with("generate_reply_draft", message_id)
    with Session(engine) as session:
        row = session.get(ReplyDraft, UUID(body["id"]))
        assert row.created_at is not None

    detail = client.get(f"/messages/{message_id}", headers=headers).json()
    assert set(detail) == LIST_KEYS | {"body_text", "draft"}
    _assert_contract(detail["draft"], message_id)
    listed = client.get("/messages", headers=headers).json()
    assert set(listed[0]) == LIST_KEYS
    assert "draft" not in listed[0]

    missing = client.get(f"/messages/{message_id}/draft")
    assert missing.status_code == 401
    fetched = client.get(f"/messages/{message_id}/draft", headers=headers)
    assert fetched.status_code == 200
    _assert_contract(fetched.json(), message_id)

    patched = client.patch(
        f"/messages/{message_id}/draft",
        headers=headers,
        json={"body": "  Keep this reply  "},
    )
    assert patched.status_code == 200
    saved = patched.json()
    _assert_contract(saved, message_id)
    assert saved["body"] == "Keep this reply"
    assert saved["source"] == "user"
    assert saved["status"] == "ready"
    assert client.patch(
        f"/messages/{message_id}/draft",
        headers=headers,
        json={"body": "   "},
    ).status_code == 422

    deleted = client.delete(f"/messages/{message_id}/draft", headers=headers)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(f"/messages/{message_id}/draft", headers=headers).json() == {
        "detail": "Draft not found."
    }
    assert client.get(f"/messages/{message_id}", headers=headers).json()["draft"] is None
    assert client.delete(f"/messages/{message_id}/draft", headers=headers).status_code == 404


def test_other_users_message_is_not_found(client, register_user):
    headers, _user_id, _account_id, message_id = _mailbox(client, register_user)
    bob = register_user("bob@example.com")
    for method in ("post", "get", "patch", "delete"):
        kwargs = {"headers": bob}
        if method == "patch":
            kwargs["json"] = {"body": "nope"}
        response = getattr(client, method)(f"/messages/{message_id}/draft", **kwargs)
        assert response.status_code == 404
        assert response.json() == {"detail": "Message not found."}
    outlook = client.post(f"/messages/{message_id}/draft/outlook", headers=bob)
    assert outlook.status_code == 404
    assert outlook.json() == {"detail": "Message not found."}


def test_generate_reply_draft_stores_the_stub_sentence_outside_the_session(
    client, register_user, monkeypatch
):
    headers, _user_id, _account_id, message_id = _mailbox(client, register_user)
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        row.subject = "Roof leak"
        row.body_text = "The roof leaks."
        row.body_preview = "leaks"
        session.add(row)
        session.commit()
    assert client.post(f"/messages/{message_id}/draft", headers=headers).status_code == 200

    open_sessions = {"n": 0}
    seen = {}

    class CountingSession(Session):
        def __enter__(self):
            open_sessions["n"] += 1
            return super().__enter__()

        def __exit__(self, exc_type, exc, tb):
            open_sessions["n"] -= 1
            return super().__exit__(exc_type, exc, tb)

    def prompts(*, subject, body_text, body_preview):
        assert open_sessions["n"] == 0
        seen["fields"] = (subject, body_text, body_preview)
        return "system", "user"

    calls = {"n": 0}
    real_get_llm = get_llm

    def wrapped_get_llm():
        assert open_sessions["n"] == 0
        calls["n"] += 1
        return real_get_llm()

    def refuse_graph(*_args, **_kwargs):
        raise AssertionError("generate_reply_draft called Graph")

    monkeypatch.setattr("app.worker.tasks.Session", CountingSession)
    monkeypatch.setattr("app.worker.tasks.build_reply_prompts", prompts)
    monkeypatch.setattr("app.worker.tasks.get_llm", wrapped_get_llm)
    monkeypatch.setattr("app.services.graph.drafts._graph_request", refuse_graph)

    asyncio.run(generate_reply_draft({"job_id": "draft-1"}, message_id))

    assert calls["n"] == 1
    assert seen["fields"] == ("Roof leak", "The roof leaks.", "leaks")
    assert generate_reply_draft in WorkerSettings.functions
    stored = client.get(f"/messages/{message_id}/draft", headers=headers).json()
    assert stored["status"] == "ready"
    assert stored["source"] == "llm"
    assert stored["body"] == STUB_REPLY_SENTENCE
    assert stored["body"] != "[stub llm]"
    assert stored["error"] is None
    assert asyncio.run(StubLLM().complete(system="a", user="b")) == "[stub llm]"


def test_failed_regenerate_keeps_the_pending_body(client, register_user, monkeypatch):
    headers, _user_id, _account_id, message_id = _mailbox(client, register_user)
    assert client.post(f"/messages/{message_id}/draft", headers=headers).status_code == 200
    asyncio.run(generate_reply_draft({}, message_id))
    patched = client.patch(
        f"/messages/{message_id}/draft",
        headers=headers,
        json={"body": "Keep this reply"},
    )
    assert patched.json()["status"] == "ready"
    with Session(engine) as session:
        draft = session.exec(select(ReplyDraft).where(ReplyDraft.message_id == UUID(message_id))).one()
        draft.graph_draft_id = "old-outlook"
        draft.error = "outlook draft failed"
        session.add(draft)
        session.commit()

    reset = client.post(f"/messages/{message_id}/draft", headers=headers)
    assert reset.status_code == 200
    assert reset.json()["status"] == "pending"
    assert reset.json()["body"] == "Keep this reply"
    assert reset.json()["graph_draft_id"] is None
    assert reset.json()["error"] is None
    assert reset.json()["source"] == "user"

    async def explode(self, *, system, user):
        raise RuntimeError("model down")

    monkeypatch.setattr(StubLLM, "complete", explode)
    asyncio.run(generate_reply_draft({"job_id": "draft-2"}, message_id))
    failed = client.get(f"/messages/{message_id}/draft", headers=headers).json()
    assert failed["status"] == "failed"
    assert failed["error"] == "generation failed"
    assert failed["body"] == "Keep this reply"
    assert failed["source"] == "user"


def test_configured_provider_stores_model_text_and_a_blank_key_stays_on_the_stub(
    client, register_user, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "   ")
    monkeypatch.setattr(settings, "llm_model", "")
    assert isinstance(get_llm(), StubLLM)
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    assert isinstance(get_llm(), StubLLM)
    monkeypatch.setattr(settings, "llm_provider", "unknown")
    assert isinstance(get_llm(), StubLLM)
    monkeypatch.setattr(settings, "llm_provider", "stub")
    assert isinstance(get_llm(), StubLLM)

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_model", "gpt-test")
    client_model = get_llm()
    assert isinstance(client_model, OpenAIClient)
    assert client_model.model == "gpt-test"
    monkeypatch.setattr(settings, "llm_model", "")
    assert get_llm().model == "gpt-4o-mini"
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")
    monkeypatch.setattr(settings, "llm_model", "claude-test")
    anthropic = get_llm()
    assert isinstance(anthropic, AnthropicClient)
    assert anthropic.model == "claude-test"
    assert _openai_text({"choices": [{"message": {"content": "Hello"}}]}) == "Hello"
    assert _anthropic_text({"content": [{"type": "text", "text": "Hello"}]}) == "Hello"
    with pytest.raises(RuntimeError):
        _openai_text({"choices": []})

    def explode(*_args, **_kwargs):
        raise AssertionError("llm client opened a socket")

    monkeypatch.setattr("app.services.llm.providers.urlopen", explode)
    headers, _user_id, _account_id, message_id = _mailbox(client, register_user, email="model@example.com")
    assert client.post(f"/messages/{message_id}/draft", headers=headers).status_code == 200
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "")
    asyncio.run(generate_reply_draft({}, message_id))
    blank = client.get(f"/messages/{message_id}/draft", headers=headers).json()
    assert blank["body"] == STUB_REPLY_SENTENCE
    assert final_reply_body("stub", "[stub llm]") == STUB_REPLY_SENTENCE

    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_model", "gpt-test")
    with Session(engine) as session:
        draft = session.exec(select(ReplyDraft).where(ReplyDraft.message_id == UUID(message_id))).one()
        draft.status = ReplyDraftStatus.pending
        session.add(draft)
        session.commit()

    async def complete(self, *, system, user):
        assert self.model == "gpt-test"
        return "  See you Thursday. "

    monkeypatch.setattr(OpenAIClient, "complete", complete)
    asyncio.run(generate_reply_draft({}, message_id))
    ready = client.get(f"/messages/{message_id}/draft", headers=headers).json()
    assert ready["status"] == "ready"
    assert ready["body"] == "See you Thursday."
    assert ready["source"] == "llm"


def test_outlook_push_creates_then_patches_the_same_unsent_draft(client, register_user, monkeypatch):
    headers, user_id, account_id, message_id = _mailbox(client, register_user)
    _fresh_token(account_id)
    _insert_draft(user_id, message_id, body="Hello there", source="user")
    calls = []

    def fake_request(method, url, access_token, payload):
        assert "send" not in url.lower()
        assert access_token not in url
        assert access_token == "secret-access-token"
        calls.append((method, url, payload))
        if method == "POST":
            return {"id": "outlook-draft-1"}
        return {}

    def explode(*_args, **_kwargs):
        raise AssertionError("socket opened")

    monkeypatch.setattr("app.services.graph.drafts._graph_request", fake_request)
    monkeypatch.setattr("app.services.graph.drafts.urlopen", explode)
    monkeypatch.setattr(
        "app.api.messages.refresh_access_token",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("refresh")),
    )

    first = client.post(f"/messages/{message_id}/draft/outlook", headers=headers)
    assert first.status_code == 200
    pushed = first.json()
    _assert_contract(pushed, message_id)
    assert pushed["graph_draft_id"] == "outlook-draft-1"
    assert pushed["status"] == "ready"
    assert pushed["error"] is None
    assert pushed["body"] == "Hello there"
    assert pushed["source"] == "user"
    assert calls == [
        (
            "POST",
            "https://graph.microsoft.com/v1.0/me/messages/graph-msg-1/createReply",
            {},
        ),
        (
            "PATCH",
            "https://graph.microsoft.com/v1.0/me/messages/outlook-draft-1",
            {"body": {"contentType": "Text", "content": "Hello there"}},
        ),
    ]

    calls.clear()
    second = client.post(f"/messages/{message_id}/draft/outlook", headers=headers)
    assert second.status_code == 200
    assert second.json()["graph_draft_id"] == "outlook-draft-1"
    assert calls == [
        (
            "PATCH",
            "https://graph.microsoft.com/v1.0/me/messages/outlook-draft-1",
            {"body": {"contentType": "Text", "content": "Hello there"}},
        ),
    ]
    assert all("send" not in url.lower() for _method, url, _payload in calls)


def test_outlook_failures_stay_ready_and_keep_a_created_id(client, register_user, monkeypatch):
    headers, user_id, account_id, message_id = _mailbox(client, register_user)
    _fresh_token(account_id)
    _insert_draft(user_id, message_id, body="Hello there")
    calls = []

    def fake_request(method, url, access_token, payload):
        assert "send" not in url.lower()
        calls.append(url)
        if method == "POST":
            return {"id": "outlook-draft-9"}
        raise GraphDraftError("graph body must not be stored")

    monkeypatch.setattr("app.services.graph.drafts._graph_request", fake_request)
    monkeypatch.setattr(
        "app.services.graph.drafts.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("socket opened")),
    )

    failed = client.post(f"/messages/{message_id}/draft/outlook", headers=headers)
    assert failed.status_code == 502
    assert failed.json() == {"detail": "Outlook draft could not be saved."}
    assert "Hello there" not in failed.text
    assert "secret-access-token" not in failed.text
    assert "graph body must not be stored" not in failed.text
    saved = client.get(f"/messages/{message_id}/draft", headers=headers).json()
    assert saved["status"] == "ready"
    assert saved["body"] == "Hello there"
    assert saved["graph_draft_id"] == "outlook-draft-9"
    assert saved["error"] == "outlook draft failed"
    assert calls == [
        "https://graph.microsoft.com/v1.0/me/messages/graph-msg-1/createReply",
        "https://graph.microsoft.com/v1.0/me/messages/outlook-draft-9",
    ]

    def create_fails(method, url, access_token, payload):
        raise GraphDraftError("nope")

    monkeypatch.setattr("app.services.graph.drafts._graph_request", create_fails)
    with Session(engine) as session:
        draft = session.exec(select(ReplyDraft).where(ReplyDraft.message_id == UUID(message_id))).one()
        draft.graph_draft_id = None
        session.add(draft)
        session.commit()
    again = client.post(f"/messages/{message_id}/draft/outlook", headers=headers)
    assert again.status_code == 502
    untouched = client.get(f"/messages/{message_id}/draft", headers=headers).json()
    assert untouched["status"] == "ready"
    assert untouched["graph_draft_id"] is None
    assert untouched["body"] == "Hello there"


def test_outlook_push_rejects_gmail_pending_and_empty_drafts(client, register_user, monkeypatch):
    def refuse_graph(*_args, **_kwargs):
        raise AssertionError("Graph was called")

    monkeypatch.setattr("app.services.graph.drafts._graph_request", refuse_graph)
    monkeypatch.setattr(
        "app.api.messages.refresh_access_token",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("refresh")),
    )

    headers, user_id, _account_id, message_id = _mailbox(
        client, register_user, email="gmail@example.com", provider="google", graph_id="gmail-1"
    )
    _insert_draft(user_id, message_id, body="Hello there")
    gmail = client.post(f"/messages/{message_id}/draft/outlook", headers=headers)
    assert gmail.status_code == 422
    assert gmail.json() == {"detail": "Only an Outlook mailbox can save this draft."}

    outlook_headers, _outlook_user, _account, outlook_message = _mailbox(
        client, register_user, email="outlook@example.com", graph_id="graph-msg-2"
    )
    pending = client.post(f"/messages/{outlook_message}/draft", headers=outlook_headers)
    assert pending.status_code == 200
    not_ready = client.post(f"/messages/{outlook_message}/draft/outlook", headers=outlook_headers)
    assert not_ready.status_code == 409
    assert not_ready.json() == {"detail": "Draft is not ready."}

    with Session(engine) as session:
        draft = session.exec(select(ReplyDraft).where(ReplyDraft.message_id == UUID(outlook_message))).one()
        draft.status = ReplyDraftStatus.ready
        draft.body = "   "
        session.add(draft)
        session.commit()
    empty = client.post(f"/messages/{outlook_message}/draft/outlook", headers=outlook_headers)
    assert empty.status_code == 409
    assert empty.json() == {"detail": "Draft is not ready."}


def test_outlook_push_uses_a_refreshed_token(client, register_user, monkeypatch):
    headers, user_id, _account_id, message_id = _mailbox(client, register_user, email="fresh@example.com")
    _insert_draft(user_id, message_id, body="Hello there")
    calls = []

    def fake_refresh(session, account, generation=None):
        account.access_token = "refreshed-token"
        session.add(account)
        session.commit()
        session.refresh(account)
        return account

    def fake_request(method, url, access_token, payload):
        calls.append((method, url, access_token))
        assert "send" not in url.lower()
        if method == "POST":
            return {"id": "outlook-draft-2"}
        return {}

    monkeypatch.setattr("app.api.messages.refresh_access_token", fake_refresh)
    monkeypatch.setattr("app.services.graph.drafts._graph_request", fake_request)
    monkeypatch.setattr(
        "app.services.graph.drafts.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("socket opened")),
    )

    response = client.post(f"/messages/{message_id}/draft/outlook", headers=headers)
    assert response.status_code == 200
    assert response.json()["graph_draft_id"] == "outlook-draft-2"
    assert [item[0] for item in calls] == ["POST", "PATCH"]
    assert calls[0][1].endswith("/graph-msg-1/createReply")
    assert calls[1][1].endswith("/outlook-draft-2")
    assert {item[2] for item in calls} == {"refreshed-token"}


def test_outlook_refresh_failure_stays_ready(client, register_user, monkeypatch):
    headers, user_id, _account_id, message_id = _mailbox(client, register_user, email="refresh@example.com")
    _insert_draft(user_id, message_id, body="Hello there")

    def refuse(*_args, **_kwargs):
        raise TokenRefreshError("refresh_rejected")

    def refuse_graph(*_args, **_kwargs):
        raise AssertionError("Graph was called")

    monkeypatch.setattr("app.api.messages.refresh_access_token", refuse)
    monkeypatch.setattr("app.services.graph.drafts._graph_request", refuse_graph)

    response = client.post(f"/messages/{message_id}/draft/outlook", headers=headers)
    assert response.status_code == 502
    assert response.json() == {"detail": "Outlook token could not be refreshed."}
    assert "secret-access-token" not in response.text
    assert "refresh_rejected" not in response.text
    saved = client.get(f"/messages/{message_id}/draft", headers=headers).json()
    assert saved["status"] == "ready"
    assert saved["body"] == "Hello there"
    assert saved["error"] == "token refresh failed"
    assert saved["graph_draft_id"] is None


def test_queue_failure_keeps_the_body(client, register_user):
    headers, _user_id, _account_id, message_id = _mailbox(client, register_user, email="queue@example.com")
    assert client.post(f"/messages/{message_id}/draft", headers=headers).status_code == 200
    asyncio.run(generate_reply_draft({}, message_id))
    assert (
        client.patch(
            f"/messages/{message_id}/draft",
            headers=headers,
            json={"body": "Keep this reply"},
        ).status_code
        == 200
    )
    app.state.arq.enqueue_job.side_effect = RuntimeError("queue down")
    queued = client.post(f"/messages/{message_id}/draft", headers=headers)
    assert queued.status_code == 503
    assert queued.json() == {"detail": "Reply draft could not be queued."}
    saved = client.get(f"/messages/{message_id}/draft", headers=headers).json()
    assert saved["status"] == "failed"
    assert saved["error"] == "generation failed"
    assert saved["body"] == "Keep this reply"
