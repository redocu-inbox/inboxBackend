"""Phase 6 PR 2: stored priority and classification columns round-trip through the API.

The scorer, the classifier, and the correction route are not part of this change.
Columns are written directly. GET does not score.
"""

from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.db import engine
from app.models.message import Message
from tests.test_frontend_connect import LIST_KEYS, _add_account, _add_message, _user_id

_CHANGE_ORDER_REASONS = [
    {"code": "keyword_change_order", "label": "Change order", "weight": 22},
    {"code": "keyword_deadline", "label": "Deadline", "weight": 16},
    {"code": "keyword_urgent", "label": "Marked urgent", "weight": 16},
    {"code": "recent", "label": "Received in the last 2 days", "weight": 12},
    {"code": "unread", "label": "Unread", "weight": 8},
]

_NEW_COLUMNS = (
    "priority_band",
    "priority_reasons",
    "priority_scored_at",
    "category",
    "category_source",
    "category_confidence",
    "project_name",
    "project_number",
    "due_date",
    "responsible_party",
    "reference_number",
    "user_corrected",
)


def test_stored_priority_and_classification_round_trip(client, register_user):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    received = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)
    blank = _add_message(
        ana_id,
        account_id,
        graph_id="blank",
        received_at=received,
        priority_score=None,
    )
    scored = _add_message(
        ana_id,
        account_id,
        graph_id="scored",
        received_at=received,
        priority_score=74,
        priority_band="high",
        priority_reasons=_CHANGE_ORDER_REASONS,
        priority_scored_at=datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc),
        category="rfi",
        category_source="rule",
        category_confidence=1.0,
        project_name="Harbor School",
        project_number="24-118",
        due_date=date(2026, 11, 11),
        responsible_party="Superintendent",
        reference_number="RFI 042",
        user_corrected=False,
    )
    empty_reasons = _add_message(
        ana_id,
        account_id,
        graph_id="empty-reasons",
        received_at=received,
        priority_score=0,
        priority_band="low",
        priority_reasons=[],
    )
    reversed_reasons = list(reversed(_CHANGE_ORDER_REASONS))
    unordered = _add_message(
        ana_id,
        account_id,
        graph_id="unordered",
        received_at=received,
        priority_score=1,
        priority_band="low",
        priority_reasons=reversed_reasons,
        category_confidence=0.5,
        category="safety",
        category_source="rule",
    )
    corrected = _add_message(
        ana_id,
        account_id,
        graph_id="corrected",
        received_at=received,
        priority_score=None,
        category="other",
        category_source="user",
        category_confidence=1.0,
        user_corrected=True,
    )

    listed = client.get("/messages", headers=ana)
    assert listed.status_code == 200
    by_id = {item["id"]: item for item in listed.json()}

    blank_item = by_id[blank]
    assert blank_item["priority_score"] is None
    assert blank_item["priority_band"] is None
    assert blank_item["priority_reasons"] == []
    assert blank_item["category"] is None
    assert blank_item["category_source"] is None
    assert blank_item["category_confidence"] is None
    assert blank_item["project_name"] is None
    assert blank_item["project_number"] is None
    assert blank_item["due_date"] is None
    assert blank_item["responsible_party"] is None
    assert blank_item["reference_number"] is None
    assert blank_item["user_corrected"] is False
    assert set(blank_item) == LIST_KEYS

    item = by_id[scored]
    assert item["priority_score"] == 74.0
    assert item["priority_band"] == "high"
    assert item["priority_reasons"] == _CHANGE_ORDER_REASONS
    assert [set(reason) for reason in item["priority_reasons"]] == [
        {"code", "label", "weight"}
    ] * len(_CHANGE_ORDER_REASONS)
    assert item["category"] == "rfi"
    assert item["category_source"] == "rule"
    assert item["category_confidence"] == 1.0
    assert item["project_name"] == "Harbor School"
    assert item["project_number"] == "24-118"
    assert item["due_date"] == "2026-11-11"
    assert item["responsible_party"] == "Superintendent"
    assert item["reference_number"] == "RFI 042"
    assert item["user_corrected"] is False
    assert "priority_scored_at" not in item

    assert by_id[empty_reasons]["priority_reasons"] == []
    assert by_id[empty_reasons]["priority_score"] == 0.0
    assert by_id[empty_reasons]["priority_band"] == "low"
    assert by_id[unordered]["priority_reasons"] == reversed_reasons
    assert by_id[unordered]["category_confidence"] == 0.5
    assert by_id[corrected]["user_corrected"] is True
    assert by_id[corrected]["category"] == "other"
    assert by_id[corrected]["category_source"] == "user"
    assert by_id[corrected]["category_confidence"] == 1.0

    detail = client.get(f"/messages/{scored}", headers=ana)
    assert detail.status_code == 200
    body = detail.json()
    assert set(body) == LIST_KEYS | {"body_text", "draft"}
    assert body["priority_reasons"] == _CHANGE_ORDER_REASONS
    assert body["due_date"] == "2026-11-11"
    assert body["category"] == "rfi"
    assert "priority_scored_at" not in body

    missing = client.patch(
        f"/messages/{scored}/classification",
        headers=ana,
        json={"category": "meeting_notes"},
    )
    assert missing.status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert "/messages/{message_id}/classification" not in paths


def test_classification_checks_reject_values_outside_the_contract(client, register_user):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _add_message(
        ana_id,
        account_id,
        graph_id="checks",
        received_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
        priority_score=50,
    )
    _reject(message_id, category="RFI")
    _reject(message_id, priority_band="urgent")
    _reject(message_id, category_source="llm")
    _reject(message_id, category_confidence=1.5)

    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row is not None
        assert row.priority_score == 50
        assert row.category is None
        row.category = "rfi"
        row.category_source = "rule"
        row.category_confidence = 0.0
        row.priority_band = "medium"
        session.add(row)
        session.commit()


def test_downgrade_drops_new_columns_and_keeps_priority_score(client, register_user):
    ana = register_user("ana@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _add_message(
        ana_id,
        account_id,
        graph_id="kept",
        received_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        priority_score=50,
    )
    engine.dispose()
    config = Config("alembic.ini")
    try:
        command.downgrade(config, "f5a2c8d14e60")
        with engine.connect() as connection:
            columns = _message_columns(connection)
            assert "priority_score" in columns
            assert not set(_NEW_COLUMNS) & columns
            score = connection.execute(
                text("SELECT priority_score FROM messages WHERE id = :id"),
                {"id": message_id},
            ).scalar_one()
            assert score == 50
            indexes = connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'messages'")
            ).scalars().all()
            assert "ix_messages_priority_score" in indexes
    finally:
        command.upgrade(config, "head")
        engine.dispose()

    with engine.connect() as connection:
        columns = _message_columns(connection)
        assert set(_NEW_COLUMNS) <= columns
        checks = connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'messages'::regclass AND contype = 'c'"
            )
        ).scalars().all()
        assert {
            "ck_messages_priority_band",
            "ck_messages_category",
            "ck_messages_category_source",
            "ck_messages_category_confidence",
        } <= set(checks)
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row is not None
        assert row.priority_score == 50
        assert row.priority_band is None
        assert row.priority_reasons is None
        assert row.priority_scored_at is None
        assert row.category is None
        assert row.category_source is None
        assert row.category_confidence is None
        assert row.project_name is None
        assert row.project_number is None
        assert row.due_date is None
        assert row.responsible_party is None
        assert row.reference_number is None
        assert row.user_corrected is None


def _reject(message_id: str, **values) -> None:
    with Session(engine) as session:
        row = session.get(Message, UUID(message_id))
        assert row is not None
        for key, value in values.items():
            setattr(row, key, value)
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()


def _message_columns(connection) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'messages'"
        )
    ).scalars()
    return set(rows)
