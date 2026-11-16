"""Phase 6 PR 4: correction log, the patch route, and the sync lock."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from app.db import engine
from app.models.classification_correction import MessageClassificationCorrection
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.worker.mail_tasks import rescore_mailbox_priority, sync_mailbox_delta
from tests.test_frontend_connect import LIST_KEYS, _add_account, _add_message, _user_id

FIXED = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)
OLD = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_provider_or_model(monkeypatch):
    def _forbid(*args, **kwargs):
        raise AssertionError("correction must not call a provider or a model")

    monkeypatch.setattr("app.services.llm.factory.get_llm", _forbid)
    monkeypatch.setattr("app.worker.mail_tasks._graph_get", _forbid)
    monkeypatch.setattr("app.services.google.http.google_get", _forbid)
    monkeypatch.setattr("app.api.messages.utcnow", lambda: FIXED)


def _invoice(user_id: str, account_id: str, graph_id: str = "invoice") -> str:
    return _add_message(
        user_id,
        account_id,
        graph_id=graph_id,
        received_at=OLD,
        priority_score=18,
        subject="Invoice 441",
        priority_band="low",
        priority_reasons=[
            {"code": "keyword_payment", "label": "Payment", "weight": 12},
            {"code": "category_invoice", "label": "Invoice", "weight": 6},
        ],
        priority_scored_at=FIXED,
        category="invoice",
        category_source="rule",
        category_confidence=1.0,
        is_read=True,
    )


def _logs(message_id: str) -> list[MessageClassificationCorrection]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(MessageClassificationCorrection)
                .where(MessageClassificationCorrection.message_id == UUID(message_id))
                .order_by(MessageClassificationCorrection.field)
            ).all()
        )


def test_category_correction_rescores_and_records_the_original(client, register_user):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _invoice(ana_id, account_id)

    response = client.patch(
        f"/messages/{message_id}/classification",
        headers=ana,
        json={"category": "rfi"},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == LIST_KEYS | {"body_text", "draft"}
    assert body["category"] == "rfi"
    assert body["category_source"] == "user"
    assert body["category_confidence"] == 1.0
    assert body["user_corrected"] is True
    assert body["priority_score"] == 22.0
    assert body["priority_band"] == "low"
    assert body["priority_reasons"] == [
        {"code": "keyword_payment", "label": "Payment", "weight": 12},
        {"code": "category_rfi", "label": "RFI", "weight": 10},
    ]
    assert "priority_scored_at" not in body

    rows = _logs(message_id)
    assert len(rows) == 1
    assert rows[0].field == "category"
    assert rows[0].original_value == "invoice"
    assert rows[0].corrected_value == "rfi"
    assert rows[0].created_at == FIXED
    assert rows[0].user_id == UUID(ana_id)


def test_three_fields_share_one_timestamp(client, register_user):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _invoice(ana_id, account_id)

    response = client.patch(
        f"/messages/{message_id}/classification",
        headers=ana,
        json={
            "category": "rfi",
            "project_name": "Harbor School",
            "due_date": "2026-12-01",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["project_name"] == "Harbor School"
    assert body["due_date"] == "2026-12-01"
    assert body["category_source"] == "user"
    rows = _logs(message_id)
    assert [row.field for row in rows] == ["category", "due_date", "project_name"]
    assert {row.created_at for row in rows} == {FIXED}
    due = next(row for row in rows if row.field == "due_date")
    assert due.original_value is None
    assert due.corrected_value == "2026-12-01"


def test_unchanged_value_writes_no_log_and_does_not_rescore(client, register_user):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _invoice(ana_id, account_id)

    response = client.patch(
        f"/messages/{message_id}/classification",
        headers=ana,
        json={"category": "invoice", "project_name": ""},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["category_source"] == "rule"
    assert body["category_confidence"] == 1.0
    assert body["user_corrected"] is False
    assert body["priority_score"] == 18.0
    assert body["priority_reasons"][1]["code"] == "category_invoice"
    assert _logs(message_id) == []


def test_clearing_category_is_a_user_correction(client, register_user):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _invoice(ana_id, account_id)

    response = client.patch(
        f"/messages/{message_id}/classification",
        headers=ana,
        json={"category": None},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["category"] is None
    assert body["category_source"] == "user"
    assert body["category_confidence"] == 1.0
    assert body["user_corrected"] is True
    assert body["priority_reasons"] == [
        {"code": "keyword_payment", "label": "Payment", "weight": 12}
    ]
    row = _logs(message_id)[0]
    assert row.original_value == "invoice"
    assert row.corrected_value is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"nope": "rfi"},
        {"category": "RFI"},
        {"due_date": "Friday"},
        {"due_date": "2026-02-31"},
        {"project_name": "x" * 121},
    ],
)
def test_invalid_correction_is_422_and_writes_nothing(client, register_user, payload):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _invoice(ana_id, account_id)

    response = client.patch(
        f"/messages/{message_id}/classification",
        headers=ana,
        json=payload,
    )
    assert response.status_code == 422
    assert _logs(message_id) == []
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row.category == "invoice"
        assert row.user_corrected is None


def test_correction_is_hidden_for_other_users_and_disconnected_mail(client, register_user):
    ana = register_user("ana@example.com")
    bob = register_user("bob@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _invoice(ana_id, account_id)

    foreign = client.patch(
        f"/messages/{message_id}/classification",
        headers=bob,
        json={"category": "rfi"},
    )
    missing = client.patch(
        f"/messages/{uuid4()}/classification",
        headers=ana,
        json={"category": "rfi"},
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "Message not found."}
    assert client.patch(
        "/messages/not-a-uuid/classification",
        headers=ana,
        json={"category": "rfi"},
    ).status_code == 422

    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        account.disconnected_at = FIXED
        session.add(account)
        session.commit()
    hidden = client.patch(
        f"/messages/{message_id}/classification",
        headers=ana,
        json={"category": "rfi"},
    )
    assert hidden.status_code == 404
    assert hidden.json() == {"detail": "Message not found."}
    assert _logs(message_id) == []


def test_scorer_failure_rolls_back_the_correction(client, register_user, monkeypatch):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _invoice(ana_id, account_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("scorer bug")

    monkeypatch.setattr("app.api.messages.score", _boom)
    with pytest.raises(RuntimeError, match="scorer bug"):
        client.patch(
            f"/messages/{message_id}/classification",
            headers=ana,
            json={"category": "safety"},
        )
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row.category == "invoice"
        assert row.category_source == "rule"
        assert row.priority_score == 18
        assert row.user_corrected is None
    assert _logs(message_id) == []


def test_later_sync_keeps_corrected_fields_and_refreshes_the_rest(client, register_user, monkeypatch):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _invoice(ana_id, account_id, graph_id="graph-lock")
    saved = client.patch(
        f"/messages/{message_id}/classification",
        headers=ana,
        json={"category": "meeting_notes", "due_date": "2027-06-01"},
    )
    assert saved.status_code == 200

    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        account.access_token = "access"
        account.refresh_token = "refresh"
        account.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
        account.sync_generation = 1
        session.add(account)
        session.commit()

    monkeypatch.setattr(
        "app.worker.mail_tasks._graph_get",
        lambda url, token: {
            "value": [
                {
                    "id": "graph-lock",
                    "subject": "RFI 12 door hardware",
                    "bodyPreview": "See the sketch",
                    "receivedDateTime": "2024-06-01T12:00:00Z",
                    "from": {"emailAddress": {"name": "Ada", "address": "ada@example.com"}},
                    "isRead": True,
                    "body": {
                        "contentType": "text",
                        "content": "Project name: Harbor School\nDue date 2026-11-11",
                    },
                }
            ]
        },
    )
    asyncio.run(sync_mailbox_delta({"job_id": "job-1"}, account_id, 1))

    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row.subject == "RFI 12 door hardware"
        assert row.category == "meeting_notes"
        assert row.category_source == "user"
        assert row.category_confidence == 1.0
        assert row.due_date == date(2027, 6, 1)
        assert row.project_name == "Harbor School"
        assert row.reference_number == "RFI 12"
        assert row.user_corrected is True
        codes = [item["code"] for item in row.priority_reasons]
        assert "keyword_rfi" in codes
        assert "category_rfi" not in codes
        assert "category_meeting_notes" not in codes


def test_rescore_respects_the_same_lock(client, register_user):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _invoice(ana_id, account_id)
    assert (
        client.patch(
            f"/messages/{message_id}/classification",
            headers=ana,
            json={"category": "meeting_notes"},
        ).status_code
        == 200
    )
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        row.subject = "Submittal for curtain wall"
        row.body_text = "Project name: Harbor School"
        session.add(row)
        session.commit()

    asyncio.run(rescore_mailbox_priority({"job_id": "backfill"}, account_id))
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row.category == "meeting_notes"
        assert row.category_source == "user"
        assert row.project_name == "Harbor School"
        assert row.reference_number is None
