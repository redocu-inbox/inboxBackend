"""Phase 6 PR 3: rule scorer, rule classifier, and the worker that runs both."""

import asyncio
import logging
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, func, select

from app.config import get_settings
from app.db import engine
from app.models.classification_correction import MessageClassificationCorrection
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.services.priority.apply import classify_and_score
from app.services.priority.classify import classify
from app.services.priority.score import score
from app.worker.settings import WorkerSettings, startup
from app.worker.tasks import rescore_connected_mailboxes, rescore_mailbox_priority, sync_mailbox_delta
from tests.test_frontend_connect import _user_id
from tests.test_mailbox_reliability import GRAPH_ITEM, _future, _gmail_item, _run_sync

FIXED = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)
OLD = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
RECENT = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)
THIS_WEEK = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def app_logs(caplog):
    """Capture app.* logs.

    Alembic's logging config disables loggers that already exist, and
    setup_logging keeps app logs off the root logger. Pytest still attaches
    its capture handler to that logger.
    """
    for name, obj in logging.root.manager.loggerDict.items():
        if isinstance(obj, logging.Logger) and (name == "app" or name.startswith("app.")):
            obj.disabled = False
    caplog.set_level(logging.INFO)
    yield caplog


@pytest.fixture(autouse=True)
def _no_model_or_provider_call(monkeypatch):
    def _forbid(*args, **kwargs):
        raise AssertionError("phase 6 rules must not call a provider or a model")

    monkeypatch.setattr("app.services.llm.factory.get_llm", _forbid)
    monkeypatch.setattr("app.worker.tasks._graph_get", _forbid)
    monkeypatch.setattr("app.services.google.http.google_get", _forbid)


def _score(**overrides):
    fields = {
        "subject": None,
        "body_preview": None,
        "body_text": None,
        "from_name": None,
        "from_email": None,
        "is_read": True,
        "received_at": OLD,
        "category": None,
        "due_date": None,
    }
    vip = overrides.pop("vip_domains", None)
    fields.update(overrides)
    value, band, reasons, stamp = score(fields, FIXED, vip_domains=vip)
    return value, band, reasons, stamp


def _reasons(*pairs: tuple[str, str, int]) -> list[dict[str, object]]:
    return [{"code": code, "label": label, "weight": weight} for code, label, weight in pairs]


def test_scorer_fixtures_with_category_and_due_date_null():
    value, band, reasons, stamp = _score(
        subject="URGENT: change order 18 due by Friday",
        body_text="Please approve this change order.",
        is_read=False,
        received_at=RECENT,
    )
    assert (value, band, stamp) == (74.0, "high", FIXED)
    assert reasons == _reasons(
        ("keyword_change_order", "Change order", 22),
        ("keyword_deadline", "Deadline", 16),
        ("keyword_urgent", "Marked urgent", 16),
        ("recent", "Received in the last 2 days", 12),
        ("unread", "Unread", 8),
    )

    value, band, reasons, _ = _score(
        subject="RFI 12 — door hardware",
        body_text="See the attached sketch.",
        is_read=False,
        received_at=RECENT,
        from_name="Pat Lee",
        from_email="pat@gc.example",
    )
    assert (value, band) == (42.0, "medium")
    assert reasons == _reasons(
        ("keyword_rfi", "RFI", 22),
        ("recent", "Received in the last 2 days", 12),
        ("unread", "Unread", 8),
    )

    value, band, reasons, _ = _score(subject="Invoice 441", body_text=None, is_read=True)
    assert (value, band) == (12.0, "low")
    assert reasons == _reasons(("keyword_payment", "Payment", 12))

    value, band, reasons, _ = _score(
        subject="Submittal for curtain wall",
        is_read=True,
        received_at=THIS_WEEK,
    )
    assert (value, band) == (20.0, "low")
    assert reasons == _reasons(
        ("keyword_submittal", "Submittal", 14),
        ("this_week", "Received this week", 6),
    )
    assert all(item["code"] != "recent" for item in reasons)

    value, band, reasons, _ = _score(is_read=False)
    assert (value, band) == (8.0, "low")
    assert reasons == _reasons(("unread", "Unread", 8))

    value, band, reasons, _ = _score(is_read=True)
    assert (value, band, reasons) == (0.0, "low", [])

    value, band, reasons, _ = _score(subject="Hello", is_read=None)
    assert (value, band, reasons) == (0.0, "low", [])

    value, band, reasons, _ = _score(
        subject="Solicitud de información",
        is_read=True,
        received_at=RECENT,
    )
    assert (value, band) == (12.0, "low")
    assert reasons == _reasons(("recent", "Received in the last 2 days", 12))

    value, band, reasons, _ = _score(
        from_name="Alex Rivera, Superintendent",
        subject="Site walk",
        is_read=True,
        received_at=RECENT,
    )
    assert (value, band) == (20.0, "low")
    assert reasons == _reasons(
        ("recent", "Received in the last 2 days", 12),
        ("sender_project_role", "Project role", 8),
    )

    value, band, reasons, _ = _score(
        from_email="pat@vip.example",
        vip_domains=("vip.example",),
    )
    assert (value, band) == (10.0, "low")
    assert reasons == _reasons(("sender_vip", "VIP sender", 10))

    value, band, reasons, _ = _score(from_email="pat@vip.example", vip_domains=())
    assert (value, band, reasons) == (0.0, "low", [])

    value, band, reasons, _ = _score(
        subject="URGENT RFI and change order",
        body_text=(
            "Submittal attached. Stop work until you respond by Friday. "
            "Lien and pay app are past due. There is a deadline."
        ),
        from_name="Project Manager",
        is_read=False,
        received_at=datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc),
    )
    assert (value, band) == (100.0, "high")
    assert reasons == _reasons(
        ("keyword_change_order", "Change order", 22),
        ("keyword_rfi", "RFI", 22),
        ("keyword_safety", "Safety", 20),
        ("keyword_deadline", "Deadline", 16),
        ("keyword_urgent", "Marked urgent", 16),
        ("keyword_submittal", "Submittal", 14),
        ("keyword_payment", "Payment", 12),
        ("recent", "Received in the last 2 days", 12),
        ("sender_project_role", "Project role", 8),
        ("unread", "Unread", 8),
    )
    assert sum(item["weight"] for item in reasons) == 150


def test_scorer_edges():
    once, _, reasons, _ = _score(subject="Invoice 9", body_text="Please see the invoice.")
    assert once == 12.0
    assert reasons == _reasons(("keyword_payment", "Payment", 12))

    _, _, reasons, _ = _score(from_name="Architecture Office", subject="Hello")
    assert reasons == []
    _, _, reasons, _ = _score(from_email="pm@mail.vip.example", vip_domains=("vip.example",))
    assert reasons == []

    hidden = "a" * 8000 + " rfi"
    _, _, buried, _ = _score(body_text=hidden, subject="Hello")
    assert buried == []
    _, _, visible, _ = _score(body_text=hidden, subject="RFI 3")
    assert visible[0]["code"] == "keyword_rfi"

    _, _, html, _ = _score(body_text="Please see the <b>RFI</b>")
    assert html == _reasons(("keyword_rfi", "RFI", 22))
    assert all("b" != item["code"] for item in html)

    value, _, reasons, _ = _score(subject="Hello", category="rfi", due_date=None, is_read=True)
    assert value == 10.0
    assert reasons == _reasons(("category_rfi", "RFI", 10))

    _, _, soon, _ = _score(due_date=date(2026, 11, 11))
    assert soon == _reasons(("due_soon", "Due within 3 days", 14))
    _, _, overdue, _ = _score(due_date=date(2026, 11, 8))
    assert overdue == _reasons(("due_overdue", "Overdue", 25))
    _, _, week, _ = _score(due_date=date(2026, 11, 14))
    assert week == _reasons(("due_within_7_days", "Due within 7 days", 8))
    _, _, later, _ = _score(due_date=date(2026, 12, 1))
    assert later == []

    for category in ("meeting_notes", "other"):
        _, _, reasons, _ = _score(category=category, subject="Hello")
        assert reasons == []


def test_classifier_fixtures():
    cases = [
        ("RFI #042 door hardware", "rfi", 1.0, "RFI 042"),
        ("CO #18", "change_order", 1.0, "CO 18"),
        ("Change order 18", "change_order", 1.0, "CO 18"),
        ("Submittal for curtain wall", "submittal", 1.0, None),
        ("Pay App 12", "invoice", 1.0, "Pay App 12"),
        ("Invoice 441", "invoice", 1.0, "Invoice 441"),
        ("Near miss at the loading dock", "safety", 1.0, None),
        ("Project schedule update", "schedule", 1.0, None),
        ("Meeting minutes for the site walk", "meeting_notes", 1.0, None),
        ("Hello from the trailer", None, 0.0, None),
        ("RFI 7 and submittal 3", "rfi", 0.5, "RFI 7"),
        ("Stop work on RFI 042", "safety", 0.5, None),
        ("Acme Co delivery", None, 0.0, None),
        ("RFI 12 due by Friday", "rfi", 1.0, "RFI 12"),
    ]
    for subject, category, confidence, reference in cases:
        result = classify({"subject": subject})
        assert result["category"] == category, subject
        assert result["category_confidence"] == confidence
        assert result["reference_number"] == reference
        if subject != "RFI 12 due by Friday":
            assert result["due_date"] is None
        else:
            assert result["due_date"] is None

    body = "Project name: Harbor School\nProject # 24-118\nDue date 2026-11-11"
    result = classify(
        {
            "subject": "RFI 042",
            "body_text": body,
            "from_name": "Alex Rivera, Superintendent",
        }
    )
    assert result["category"] == "rfi"
    assert result["category_confidence"] == 1.0
    assert result["project_name"] == "Harbor School"
    assert result["project_number"] == "24-118"
    assert result["due_date"] == date(2026, 11, 11)
    assert result["responsible_party"] == "Superintendent"
    assert result["reference_number"] == "RFI 042"

    buried = classify(
        {
            "subject": "Hello",
            "body_text": ("a" * 8000) + "\nDue date 2026-11-11",
            "body_preview": None,
        }
    )
    assert buried["category"] is None
    assert buried["category_confidence"] == 0.0
    assert buried["due_date"] is None

    preview = classify(
        {"subject": "Hello", "body_text": None, "body_preview": "Due date 2026-11-11"}
    )
    assert preview["category"] is None
    assert preview["category_confidence"] == 0.0
    assert preview["due_date"] == date(2026, 11, 11)
    assert preview["project_name"] is None

    plural = classify({"subject": "RFIs for the lobby"})
    assert plural["category"] == "rfi"
    office = classify({"from_name": "Architecture Office", "subject": "Hello"})
    assert office["responsible_party"] is None
    assert office["category"] is None


class _Row:
    def __init__(self, **kwargs):
        self.id = "row"
        self.subject = None
        self.body_preview = None
        self.body_text = None
        self.from_name = None
        self.from_email = None
        self.is_read = True
        self.received_at = OLD
        self.category = None
        self.category_source = None
        self.category_confidence = None
        self.project_name = None
        self.project_number = None
        self.due_date = None
        self.responsible_party = None
        self.reference_number = None
        self.priority_score = None
        self.priority_band = None
        self.priority_reasons = None
        self.priority_scored_at = None
        self.__dict__.update(kwargs)


def _apply(**kwargs):
    row = _Row(**kwargs)
    assert classify_and_score(row, FIXED) is True
    return row


def test_worker_pipeline_adds_category_and_due_date_to_the_score():
    change = _apply(
        subject="URGENT: change order 18 due by Friday",
        body_text="Please approve this change order.",
        is_read=False,
        received_at=RECENT,
    )
    assert change.category == "change_order"
    assert change.category_confidence == 1.0
    assert change.category_source == "rule"
    assert change.due_date == date(2026, 11, 13)
    assert change.reference_number == "CO 18"
    assert change.priority_score == 92.0
    assert change.priority_band == "high"
    assert [item["code"] for item in change.priority_reasons] == [
        "keyword_change_order",
        "keyword_deadline",
        "keyword_urgent",
        "recent",
        "category_change_order",
        "due_within_7_days",
        "unread",
    ]

    rfi = _apply(
        subject="RFI 12 — door hardware",
        body_text="See the attached sketch.",
        is_read=False,
        received_at=RECENT,
        from_email="pat@gc.example",
    )
    assert rfi.reference_number == "RFI 12"
    assert rfi.priority_score == 52.0
    assert rfi.priority_band == "medium"
    assert [item["code"] for item in rfi.priority_reasons] == [
        "keyword_rfi",
        "recent",
        "category_rfi",
        "unread",
    ]

    invoice = _apply(subject="Invoice 441", is_read=True)
    assert invoice.reference_number == "Invoice 441"
    assert invoice.priority_score == 18.0
    assert invoice.priority_band == "low"

    submittal = _apply(subject="Submittal for curtain wall", is_read=True, received_at=THIS_WEEK)
    assert submittal.reference_number is None
    assert submittal.priority_score == 28.0
    assert submittal.priority_band == "low"
    assert "recent" not in [item["code"] for item in submittal.priority_reasons]

    cap = _apply(
        subject="URGENT RFI and change order",
        body_text=(
            "Submittal attached. Stop work until you respond by Friday. "
            "Lien and pay app are past due. There is a deadline."
        ),
        from_name="Project Manager",
        is_read=False,
        received_at=RECENT,
    )
    assert cap.category == "safety"
    assert cap.category_confidence == 0.5
    assert cap.due_date == date(2026, 11, 13)
    assert cap.priority_score == 100.0
    assert cap.priority_band == "high"
    codes = [item["code"] for item in cap.priority_reasons]
    assert codes[6:9] == ["category_safety", "keyword_payment", "recent"]

    far = _apply(
        subject="RFI 042 door hardware",
        body_text="Due date 2026-12-01.",
        is_read=False,
        received_at=RECENT,
    )
    near = _apply(
        subject="RFI 042 door hardware",
        body_text="Due date 2026-11-11.",
        is_read=False,
        received_at=RECENT,
    )
    assert far.priority_score == 68.0
    assert far.priority_band == "medium"
    assert near.priority_score == 82.0
    assert near.priority_band == "high"
    assert near.due_date == date(2026, 11, 11)
    assert [item["code"] for item in near.priority_reasons] == [
        "keyword_rfi",
        "keyword_deadline",
        "due_soon",
        "recent",
        "category_rfi",
        "unread",
    ]

    old_invoice = _apply(
        subject="Invoice 100",
        body_text="Due by 2026-10-01.",
        is_read=True,
        received_at=OLD,
    )
    assert old_invoice.priority_score == 59.0
    assert old_invoice.priority_band == "medium"
    assert old_invoice.priority_reasons == _reasons(
        ("due_overdue", "Overdue", 25),
        ("keyword_deadline", "Deadline", 16),
        ("keyword_payment", "Payment", 12),
        ("category_invoice", "Invoice", 6),
    )


def test_shared_inbox_signals_and_weekday_deadlines():
    friday = classify(
        {
            "subject": "RFI 12 due by Friday",
            "received_at": RECENT,
        }
    )
    assert friday["due_date"] == date(2026, 11, 13)
    assert friday["reference_number"] == "RFI 12"
    today = classify(
        {
            "subject": "Please confirm by 5pm today",
            "received_at": RECENT,
        }
    )
    assert today["due_date"] == date(2026, 11, 9)

    value, band, reasons, _ = _score(
        subject="Can you confirm the hardware?",
        is_read=False,
        received_at=RECENT,
    )
    assert value == 40.0
    assert band == "medium"
    assert reasons == _reasons(
        ("asks_you", "Asks something of you", 20),
        ("recent", "Received in the last 2 days", 12),
        ("unread", "Unread", 8),
    )

    _, _, reasons, _ = _score(subject="Final notice: second request", is_read=True)
    assert ("repeat_ask", "Repeat ask", 15) in [
        (item["code"], item["label"], item["weight"]) for item in reasons
    ]

    value, _, reasons, _ = _score(
        subject="Inspection tomorrow at the dock",
        is_read=True,
        received_at=datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc),
    )
    assert any(item["code"] == "happening_soon" and item["weight"] == 15 for item in reasons)
    assert value >= 15

    _, _, reasons, _ = _score(subject="Sign and return the waiver", is_read=True)
    codes = {item["code"] for item in reasons}
    assert "asks_you" in codes
    assert "act_on_attachment" in codes

    value, band, reasons, _ = _score(
        subject="November newsletter",
        body_text="Unsubscribe at the link below.",
        from_email="news@vendor.example",
        is_read=False,
        received_at=RECENT,
    )
    assert value == 0.0
    assert band == "low"
    assert any(item["code"] == "bulk_automated" and item["weight"] == -20 for item in reasons)

    value, _, reasons, _ = _score(
        subject="Your invoice",
        from_email="no-reply@billing.example",
        is_read=True,
        received_at=OLD,
    )
    assert value == 0.0
    assert any(item["code"] == "bulk_automated" and item["weight"] == -40 for item in reasons)


def test_rescore_is_registered_and_not_on_the_cron():
    assert rescore_mailbox_priority in WorkerSettings.functions
    assert rescore_connected_mailboxes not in WorkerSettings.functions
    names = [getattr(job.coroutine, "__name__", "") for job in WorkerSettings.cron_jobs]
    assert names == ["renew_graph_subscriptions", "sync_connected_mailboxes"]
    assert WorkerSettings.cron_jobs[1].minute == set(range(0, 60, 5))
    assert WorkerSettings.on_startup is startup


def _account(user_id: str, *, provider: str = "microsoft", disconnected: bool = False) -> ConnectedAccount:
    return ConnectedAccount(
        user_id=UUID(user_id),
        provider=provider,
        provider_account_id=f"{provider}-{uuid4()}",
        email="ana@example.com",
        access_token="access",
        refresh_token="refresh",
        token_expires_at=_future(),
        sync_generation=1,
        sync_status="pending",
        disconnected_at=datetime(2026, 1, 1, tzinfo=timezone.utc) if disconnected else None,
    )


def test_second_upsert_replaces_classification_for_outlook_and_gmail(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    with Session(engine) as session:
        outlook = _account(user_id)
        gmail = _account(user_id, provider="google")
        session.add(outlook)
        session.add(gmail)
        session.commit()
        outlook_id = str(outlook.id)
        gmail_id = str(gmail.id)

    monkeypatch.setattr("app.worker.tasks._graph_get", lambda url, token: {"value": [GRAPH_ITEM]})
    _run_sync(outlook_id, 1)
    monkeypatch.setattr(
        "app.worker.tasks._graph_get",
        lambda url, token: {"value": [{**GRAPH_ITEM, "subject": "RFI 12 door hardware"}]},
    )
    _run_sync(outlook_id, 1)
    with Session(engine) as session:
        rows = session.exec(
            select(Message).where(Message.connected_account_id == UUID(outlook_id))
        ).all()
        assert len(rows) == 1
        assert rows[0].subject == "RFI 12 door hardware"
        assert rows[0].category == "rfi"
        assert rows[0].priority_score == 40.0

    item = _gmail_item(message_id="gmail-1", unread=True)
    monkeypatch.setattr("app.worker.tasks.collect_gmail_messages", lambda token: [item])
    _run_sync(gmail_id, 1)
    item["payload"]["headers"][0]["value"] = "Submittal for curtain wall"
    monkeypatch.setattr("app.worker.tasks.collect_gmail_messages", lambda token: [item])
    _run_sync(gmail_id, 1)
    with Session(engine) as session:
        rows = session.exec(
            select(Message).where(Message.connected_account_id == UUID(gmail_id))
        ).all()
        assert len(rows) == 1
        assert rows[0].category == "submittal"
        assert session.exec(select(func.count()).select_from(Message)).one() == 2


def test_classifier_and_scorer_failures_still_finish_sync_idle(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    with Session(engine) as session:
        account = _account(user_id)
        session.add(account)
        session.commit()
        account_id = account.id
        kept = Message(
            user_id=UUID(user_id),
            connected_account_id=account_id,
            graph_id="graph-1",
            subject="Old subject",
            body_text="Old body",
            received_at=OLD,
            category="meeting_notes",
            category_source="user",
            category_confidence=1.0,
            due_date=date(2027, 6, 1),
            priority_score=3.0,
            priority_band="low",
            priority_reasons=[],
        )
        session.add(kept)
        session.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("classifier bug")

    monkeypatch.setattr("app.services.priority.apply.classify", _boom)
    monkeypatch.setattr("app.worker.tasks._graph_get", lambda url, token: {"value": [GRAPH_ITEM]})
    _run_sync(str(account_id), 1)
    with Session(engine) as session:
        row = session.exec(select(Message)).one()
        saved = session.get(ConnectedAccount, account_id)
        assert row.subject == "Invoice 12"
        assert row.body_text == "Hello"
        assert row.category == "meeting_notes"
        assert row.category_source == "user"
        assert row.category_confidence == 1.0
        assert row.due_date == date(2027, 6, 1)
        assert row.priority_score == 20.0
        assert row.priority_band == "low"
        assert [item["code"] for item in row.priority_reasons] == ["keyword_payment", "unread"]
        assert saved.sync_status == "idle"
        assert saved.sync_error_code is None

    monkeypatch.setattr("app.services.priority.apply.classify", __import__(
        "app.services.priority.classify", fromlist=["classify"]
    ).classify)
    monkeypatch.setattr(
        "app.services.priority.apply.score",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("scorer bug")),
    )
    monkeypatch.setattr(
        "app.worker.tasks._graph_get",
        lambda url, token: {"value": [{**GRAPH_ITEM, "id": "graph-new", "subject": "RFI 9"}]},
    )
    _run_sync(str(account_id), 1)
    with Session(engine) as session:
        fresh = session.exec(select(Message).where(Message.graph_id == "graph-new")).one()
        saved = session.get(ConnectedAccount, account_id)
        assert fresh.subject == "RFI 9"
        assert fresh.category == "rfi"
        assert fresh.category_source == "rule"
        assert fresh.priority_score is None
        assert fresh.priority_band is None
        assert fresh.priority_reasons is None
        assert saved.sync_status == "idle"


def test_rescore_updates_one_mailbox_and_skips_a_disconnected_one(client, register_user):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    with Session(engine) as session:
        live = _account(user_id)
        live.sync_status = "idle"
        live.last_sync_at = OLD
        live.sync_generation = 4
        gone = _account(user_id, disconnected=True)
        gone.sync_status = "idle"
        session.add(live)
        session.add(gone)
        session.commit()
        live_id = live.id
        gone_id = gone.id
        session.add(
            Message(
                user_id=UUID(user_id),
                connected_account_id=live_id,
                graph_id="live",
                subject="Invoice 441",
                received_at=OLD,
                is_read=True,
            )
        )
        session.add(
            Message(
                user_id=UUID(user_id),
                connected_account_id=gone_id,
                graph_id="gone",
                subject="Invoice 441",
                received_at=OLD,
                is_read=True,
            )
        )
        session.commit()

    assert asyncio.run(rescore_mailbox_priority({"job_id": "backfill"}, str(live_id))) == 1
    assert asyncio.run(rescore_mailbox_priority({"job_id": "missing"}, str(uuid4()))) is None
    assert asyncio.run(rescore_mailbox_priority({"job_id": "bad-id"}, "not-a-uuid")) is None
    assert asyncio.run(rescore_mailbox_priority({"job_id": "disconnected"}, str(gone_id))) is None
    with Session(engine) as session:
        live_row = session.exec(
            select(Message).where(Message.connected_account_id == live_id)
        ).one()
        gone_row = session.exec(
            select(Message).where(Message.connected_account_id == gone_id)
        ).one()
        account = session.get(ConnectedAccount, live_id)
        assert live_row.category == "invoice"
        assert live_row.reference_number == "Invoice 441"
        assert live_row.priority_score == 18.0
        assert live_row.priority_band == "low"
        assert live_row.updated_at is not None
        assert gone_row.category is None
        assert gone_row.priority_score is None
        assert account.sync_status == "idle"
        assert account.last_sync_at == OLD
        assert account.sync_generation == 4
        assert client.get("/messages", headers=headers).status_code == 200


def _unscored(user_id: str, account_id: UUID, graph_id: str, subject: str) -> Message:
    return Message(
        user_id=UUID(user_id),
        connected_account_id=account_id,
        graph_id=graph_id,
        subject=subject,
        received_at=OLD,
        is_read=True,
    )


def test_startup_rescored_connected_outlook_and_gmail(client, register_user, app_logs):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    other_id = _user_id(client, register_user("ben@example.com"))
    secret = "token-should-not-be-logged"
    with Session(engine) as session:
        outlook = _account(user_id)
        outlook.access_token = secret
        outlook.sync_status = "idle"
        outlook.last_sync_at = OLD
        outlook.sync_generation = 4
        gmail = _account(user_id, provider="google")
        gmail.sync_status = "idle"
        gmail.last_sync_at = OLD
        gmail.sync_generation = 2
        gone = _account(user_id, provider="google", disconnected=True)
        gone.sync_status = "failed"
        gone.sync_error_code = "gmail_unavailable"
        empty = _account(other_id)
        empty.sync_status = "pending"
        session.add(outlook)
        session.add(gmail)
        session.add(gone)
        session.add(empty)
        session.commit()
        outlook_id = outlook.id
        gmail_id = gmail.id
        gone_id = gone.id
        empty_id = empty.id
        session.add(_unscored(user_id, outlook_id, "outlook-old", "Invoice 441"))
        session.add(_unscored(user_id, gmail_id, "gmail-old", "RFI 12 door hardware"))
        session.add(_unscored(user_id, gone_id, "gone-old", "Invoice 441"))
        session.commit()
        before = session.exec(select(func.count()).select_from(Message)).one()

    assert asyncio.run(rescore_connected_mailboxes({"job_id": "startup"})) == (3, 2)
    assert "priority rescore on startup finished accounts=3 messages=2" in app_logs.text
    assert secret not in app_logs.text
    assert "Invoice 441" not in app_logs.text

    with Session(engine) as session:
        outlook_row = session.exec(
            select(Message).where(Message.connected_account_id == outlook_id)
        ).one()
        gmail_row = session.exec(
            select(Message).where(Message.connected_account_id == gmail_id)
        ).one()
        gone_row = session.exec(
            select(Message).where(Message.connected_account_id == gone_id)
        ).one()
        outlook_account = session.get(ConnectedAccount, outlook_id)
        gmail_account = session.get(ConnectedAccount, gmail_id)
        gone_account = session.get(ConnectedAccount, gone_id)
        empty_account = session.get(ConnectedAccount, empty_id)
        assert outlook_row.category == "invoice"
        assert outlook_row.priority_score == 18.0
        assert outlook_row.priority_band == "low"
        assert gmail_row.category == "rfi"
        assert gmail_row.priority_score == 32.0
        assert gone_row.category is None
        assert gone_row.priority_score is None
        assert outlook_account.sync_status == "idle"
        assert outlook_account.last_sync_at == OLD
        assert outlook_account.sync_generation == 4
        assert outlook_account.access_token == secret
        assert gmail_account.sync_status == "idle"
        assert gmail_account.sync_generation == 2
        assert gone_account.sync_status == "failed"
        assert gone_account.sync_error_code == "gmail_unavailable"
        assert gone_account.disconnected_at is not None
        assert empty_account.sync_status == "pending"
        assert session.exec(select(func.count()).select_from(Message)).one() == before

    again = asyncio.run(rescore_connected_mailboxes({}))
    assert again == (3, 2)
    with Session(engine) as session:
        outlook_row = session.exec(
            select(Message).where(Message.connected_account_id == outlook_id)
        ).one()
        assert outlook_row.priority_score == 18.0
        assert session.exec(select(func.count()).select_from(Message)).one() == before


def test_startup_rescore_keeps_a_user_corrected_field(client, register_user):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    with Session(engine) as session:
        account = _account(user_id, provider="google")
        account.sync_status = "idle"
        account.sync_generation = 3
        session.add(account)
        session.commit()
        account_id = account.id
        row = _unscored(user_id, account_id, "gmail-corrected", "Invoice 441")
        row.category = "meeting_notes"
        row.category_source = "user"
        row.category_confidence = 1.0
        row.user_corrected = True
        session.add(row)
        session.commit()
        message_id = row.id
        session.add(
            MessageClassificationCorrection(
                message_id=message_id,
                user_id=UUID(user_id),
                field="category",
                original_value="invoice",
                corrected_value="meeting_notes",
                created_at=OLD,
            )
        )
        session.commit()

    assert asyncio.run(rescore_connected_mailboxes({})) == (1, 1)
    with Session(engine) as session:
        saved = session.get(Message, message_id)
        mailbox = session.get(ConnectedAccount, account_id)
        assert saved.category == "meeting_notes"
        assert saved.category_source == "user"
        assert saved.category_confidence == 1.0
        assert saved.user_corrected is True
        assert saved.priority_score == 12.0
        assert mailbox.sync_status == "idle"
        assert mailbox.sync_generation == 3


def test_startup_rescore_continues_after_one_mailbox_fails(client, register_user, monkeypatch, app_logs):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    with Session(engine) as session:
        outlook = _account(user_id)
        outlook.sync_status = "idle"
        gmail = _account(user_id, provider="google")
        gmail.sync_status = "idle"
        session.add(outlook)
        session.add(gmail)
        session.commit()
        outlook_id = outlook.id
        gmail_id = gmail.id
        session.add(_unscored(user_id, outlook_id, "outlook-old", "Invoice 441"))
        session.add(_unscored(user_id, gmail_id, "gmail-old", "RFI 12 door hardware"))
        session.commit()

    original = rescore_mailbox_priority

    async def flaky(ctx, account_id):
        if account_id == str(outlook_id):
            raise RuntimeError("rescore failed")
        return await original(ctx, account_id)

    monkeypatch.setattr("app.worker.tasks.rescore_mailbox_priority", flaky)
    assert asyncio.run(rescore_connected_mailboxes({})) == (1, 1)
    assert (
        f"priority rescore on startup failed account={outlook_id} error_type=RuntimeError"
        in app_logs.text
    )
    assert "priority rescore on startup finished accounts=1 messages=1" in app_logs.text
    assert "rescore failed" not in app_logs.text
    with Session(engine) as session:
        outlook_row = session.exec(
            select(Message).where(Message.connected_account_id == outlook_id)
        ).one()
        gmail_row = session.exec(
            select(Message).where(Message.connected_account_id == gmail_id)
        ).one()
        assert outlook_row.priority_score is None
        assert gmail_row.category == "rfi"
        assert gmail_row.priority_score == 32.0
        assert session.get(ConnectedAccount, outlook_id).sync_status == "idle"


def test_startup_rescore_logs_and_returns_when_listing_fails(monkeypatch, app_logs):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.worker.tasks.Session", boom)
    assert asyncio.run(rescore_connected_mailboxes({})) == (0, 0)
    assert "priority rescore on startup failed error_type=RuntimeError" in app_logs.text
    assert "priority rescore on startup finished" not in app_logs.text
    assert "db down" not in app_logs.text


def test_rescore_on_startup_can_be_turned_off(client, register_user, monkeypatch, app_logs):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    with Session(engine) as session:
        account = _account(user_id)
        session.add(account)
        session.commit()
        account_id = account.id
        session.add(_unscored(user_id, account_id, "outlook-old", "Invoice 441"))
        session.commit()

    monkeypatch.setattr(get_settings(), "rescore_on_startup", False)

    def boom(*args, **kwargs):
        raise AssertionError("disabled rescore must not open a session")

    monkeypatch.setattr("app.worker.tasks.Session", boom)
    assert asyncio.run(rescore_connected_mailboxes({})) == (0, 0)
    assert "priority rescore on startup skipped" in app_logs.text
    with Session(engine) as session:
        row = session.exec(select(Message)).one()
        assert row.priority_score is None
        assert row.category is None


def test_worker_startup_keeps_running_when_rescore_raises(monkeypatch, app_logs):
    monkeypatch.setattr("app.worker.settings.setup_logging", lambda level: None)

    async def boom(ctx):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.worker.settings.rescore_connected_mailboxes", boom)
    asyncio.run(startup({}))
    assert "priority rescore on startup failed error_type=RuntimeError" in app_logs.text
    assert "db down" not in app_logs.text
