"""Follow-ups to the shared inbox signals: zone, dates, asks, and periodic sync."""

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.models.user import utcnow
from app.services.google.messages import GmailRequestError
from app.services.graph.tokens import TokenRefreshError
from app.services.priority.apply import classify_and_score
from app.services.priority.classify import classify, event_dates
from app.services.priority.score import score
from app.worker.settings import mailbox_sync_minutes
from app.worker.tasks import sync_connected_mailboxes
from tests.test_frontend_connect import _user_id
from tests.test_mailbox_reliability import GRAPH_ITEM, _future
from tests.test_phase6_rules import _account

ET = ZoneInfo("America/New_York")
FIXED = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)
RECENT = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)
OLD = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
FRIDAY_EVENING = datetime(2026, 11, 13, 20, 0, tzinfo=ET)


@pytest.fixture(autouse=True)
def _no_model_or_provider_call(monkeypatch):
    def _forbid(*args, **kwargs):
        raise AssertionError("phase 6 rules must not call a provider or a model")

    monkeypatch.setattr("app.services.llm.factory.get_llm", _forbid)
    monkeypatch.setattr("app.worker.tasks._graph_get", _forbid)
    monkeypatch.setattr("app.services.google.http.google_get", _forbid)


def _codes(reasons) -> list[str]:
    return [item["code"] for item in reasons]


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
        for key, value in kwargs.items():
            setattr(self, key, value)


def _apply(now=FIXED, **kwargs):
    row = _Row(**kwargs)
    assert classify_and_score(row, now) is True
    return row


def test_evening_mail_uses_eastern_time_for_today_and_due():
    """8 PM ET is the next UTC date. Friday evening stays Friday."""
    assert FRIDAY_EVENING.astimezone(timezone.utc).date() == date(2026, 11, 14)
    friday = classify(
        {"subject": "Submittal due by Friday", "received_at": FRIDAY_EVENING}
    )
    assert friday["due_date"] == date(2026, 11, 13)
    tomorrow = classify(
        {"subject": "Please confirm by tomorrow", "received_at": FRIDAY_EVENING}
    )
    assert tomorrow["due_date"] == date(2026, 11, 14)
    today_meeting = event_dates(
        {"subject": "OAC meeting today", "received_at": FRIDAY_EVENING}
    )
    assert date(2026, 11, 13) in today_meeting
    assert date(2026, 11, 14) not in today_meeting

    value, band, reasons, _ = score(
        {
            "subject": "Hello",
            "is_read": True,
            "received_at": OLD,
            "due_date": date(2026, 11, 13),
        },
        FRIDAY_EVENING,
    )
    assert _codes(reasons) == ["due_soon"]
    assert (value, band) == (14.0, "low")


def test_invalid_scoring_timezone_falls_back_to_eastern(monkeypatch):
    monkeypatch.setattr(get_settings(), "scoring_timezone", "Not/AZone")
    result = classify(
        {"subject": "Submittal due by Friday", "received_at": FRIDAY_EVENING}
    )
    assert result["due_date"] == date(2026, 11, 13)


def test_last_weekday_resolves_backward():
    received = datetime(2026, 11, 11, 15, 0, tzinfo=ET)  # Wednesday
    result = classify(
        {"subject": "RFI response needed by last Monday", "received_at": received}
    )
    assert result["due_date"] == date(2026, 11, 9)
    forward = classify(
        {"subject": "RFI response needed by Monday", "received_at": received}
    )
    assert forward["due_date"] == date(2026, 11, 16)
    on_friday = datetime(2026, 11, 13, 15, 0, tzinfo=ET)
    last_friday = classify(
        {"subject": "Needed by last Friday", "received_at": on_friday}
    )
    assert last_friday["due_date"] == date(2026, 11, 6)


def test_drop_by_today_is_not_a_due_date():
    dropped = classify(
        {"subject": "Can you drop by today", "received_at": RECENT}
    )
    assert dropped["due_date"] is None
    confirmed = classify(
        {"subject": "Please confirm by 5pm today", "received_at": RECENT}
    )
    assert confirmed["due_date"] == date(2026, 11, 9)


def test_yearless_dates_and_the_rfi_047_notice():
    december = datetime(2026, 12, 30, 15, 0, tzinfo=ET)
    new_year = classify({"subject": "Response due 1/5", "received_at": december})
    assert new_year["due_date"] == date(2027, 1, 5)
    january = datetime(2027, 1, 3, 15, 0, tzinfo=ET)
    prior = classify({"subject": "Response due 12/30", "received_at": january})
    assert prior["due_date"] == date(2026, 12, 30)

    # Monday and 11/16 disagree on Nov 9. The numeric date wins.
    monday = classify(
        {
            "subject": "Response Due Monday 11/16",
            "received_at": RECENT,
        }
    )
    assert monday["due_date"] == date(2026, 11, 16)

    received = datetime(2026, 11, 14, 15, 0, tzinfo=ET)
    now = datetime(2026, 11, 14, 16, 0, tzinfo=ET)
    row = _apply(
        now=now,
        subject="RFI #047 Waterproofing at level 1 slab edge",
        body_text="Response Due Monday 11/16",
        is_read=False,
        received_at=received,
    )
    assert row.category == "rfi"
    assert row.reference_number == "RFI 047"
    assert row.due_date == date(2026, 11, 16)
    assert "keyword_deadline" in _codes(row.priority_reasons)
    assert "keyword_rfi" in _codes(row.priority_reasons)
    assert row.priority_band == "high"
    assert row.priority_reasons[0]["code"] == "keyword_rfi"


def test_urgent_rfi_052_still_scores_high():
    row = _apply(
        subject=(
            "URGENT: RFI #052- Waterproofing at level 1 Slab Edge - "
            "Response needed by 11/16/26"
        ),
        is_read=False,
        received_at=RECENT,
    )
    assert row.reference_number == "RFI 052"
    assert row.due_date == date(2026, 11, 16)
    assert row.category == "rfi"
    assert row.priority_score == 92.0
    assert row.priority_band == "high"
    assert _codes(row.priority_reasons)[0] == "keyword_rfi"


def test_project_numbers_and_rfi_reference_shapes():
    hashed = classify({"subject": "See Project #24-118 for the lobby"})
    assert hashed["project_number"] == "24-118"
    tight = classify({"body_text": "Project number:24-118"})
    assert tight["project_number"] == "24-118"
    spaced = classify({"body_text": "Project # 24-118"})
    assert spaced["project_number"] == "24-118"

    hyphen = classify({"subject": "RFI-052 waterproofing detail"})
    assert hyphen["category"] == "rfi"
    assert hyphen["reference_number"] == "RFI 052"
    numbered = classify({"subject": "RFI No. 52 waterproofing detail"})
    assert numbered["category"] == "rfi"
    assert numbered["reference_number"] == "RFI 52"


def test_signature_let_me_know_does_not_outrank_an_rfi():
    value, band, reasons, _ = score(
        {
            "subject": "RFI 12 door hardware",
            "body_text": (
                "See the attached sketch.\n\n"
                "Thanks,\n"
                "Alex Rivera\n"
                "Let me know if you have any questions.\n"
                "Could you please confirm the hardware?\n"
            ),
            "is_read": False,
            "received_at": RECENT,
        },
        FIXED,
    )
    assert "asks_you" not in _codes(reasons)
    assert reasons[0]["code"] == "keyword_rfi"
    assert reasons[0]["weight"] == 22
    assert all(item["weight"] < 22 for item in reasons[1:])
    assert value == 42.0
    assert band == "medium"

    buried = score(
        {
            "subject": "Door hardware",
            "body_text": "See the sketch.\n\nThanks,\nAlex\nPlease confirm the hardware.",
            "is_read": True,
            "received_at": OLD,
        },
        FIXED,
    )[2]
    assert "asks_you" not in _codes(buried)

    quoted = score(
        {
            "subject": "Re: door hardware",
            "body_text": (
                "Sounds good.\n\n"
                "On Tue, Nov 10, 2026, Pat wrote:\n"
                "> Could you please confirm the hardware?\n"
            ),
            "is_read": True,
            "received_at": OLD,
        },
        FIXED,
    )[2]
    assert "asks_you" not in _codes(quoted)

    direct = score(
        {
            "subject": "Door hardware",
            "body_text": "Please confirm the hardware.\n\nThanks,\nAlex\nLet me know.",
            "is_read": True,
            "received_at": OLD,
        },
        FIXED,
    )[2]
    assert any(item["code"] == "asks_you" and item["weight"] == 20 for item in direct)

    casual = score(
        {"subject": "Let me know", "body_text": "Could you take a look?", "is_read": True},
        FIXED,
    )[2]
    assert "asks_you" not in _codes(casual)
    follow = score(
        {"subject": "Re: follow up", "body_text": "Thanks for the follow up.", "is_read": True},
        FIXED,
    )[2]
    assert "repeat_ask" not in _codes(follow)
    repeating = score(
        {"subject": "Following up on the pour", "is_read": True, "received_at": OLD},
        FIXED,
    )[2]
    assert any(item["code"] == "repeat_ask" for item in repeating)


def test_meeting_minutes_are_not_happening_soon():
    value, _, reasons, _ = score(
        {
            "subject": "Meeting minutes November 9, 2026",
            "body_text": "Meeting notes from the site walk.",
            "is_read": True,
            "received_at": RECENT,
            "category": "meeting_notes",
        },
        FIXED,
    )
    assert "happening_soon" not in _codes(reasons)
    assert value < 15

    soon = score(
        {
            "subject": "Inspection tomorrow at the dock",
            "is_read": True,
            "received_at": datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc),
        },
        FIXED,
    )[2]
    assert any(item["code"] == "happening_soon" and item["weight"] == 15 for item in soon)

    meeting = score(
        {
            "subject": "OAC meeting tomorrow",
            "is_read": True,
            "received_at": datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc),
        },
        FIXED,
    )[2]
    assert "happening_soon" in _codes(meeting)

    deadline = score(
        {
            "subject": "The budget meeting is due tomorrow",
            "is_read": True,
            "received_at": datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc),
        },
        FIXED,
    )[2]
    assert "happening_soon" not in _codes(deadline)


def test_construction_notice_from_noreply_is_not_buried():
    row = _apply(
        subject="RFI #047 assigned to you",
        body_text="Please review this RFI.\n\nUnsubscribe from these notifications.",
        from_email="no-reply@notifications.procore.com",
        is_read=False,
        received_at=OLD,
    )
    assert row.category == "rfi"
    assert "bulk_automated" not in _codes(row.priority_reasons)
    assert row.priority_score == 40.0
    assert row.priority_band == "medium"

    _, _, daily, _ = score(
        {
            "subject": "Daily log posted",
            "body_text": "Unsubscribe from these notifications.",
            "from_email": "no-reply@procore.com",
            "is_read": False,
            "received_at": RECENT,
        },
        FIXED,
    )
    assert "bulk_automated" not in _codes(daily)
    assert sum(item["weight"] for item in daily) == 20

    _, _, change, _ = score(
        {
            "subject": "Change order 18",
            "body_text": "You are receiving this notice.",
            "from_email": "no-reply@gc.example",
            "is_read": True,
            "received_at": OLD,
        },
        FIXED,
    )
    assert "keyword_change_order" in _codes(change)
    assert "bulk_automated" not in _codes(change)

    _, _, invoice, _ = score(
        {
            "subject": "Your invoice",
            "from_email": "no-reply@billing.example",
            "is_read": True,
            "received_at": OLD,
        },
        FIXED,
    )
    assert any(item["code"] == "bulk_automated" and item["weight"] == -40 for item in invoice)


def test_sync_minute_marks():
    assert mailbox_sync_minutes(5) == set(range(0, 60, 5))
    assert mailbox_sync_minutes(60) == {0}
    assert mailbox_sync_minutes(0) == set(range(0, 60, 5))


def test_periodic_sync_skips_in_progress_and_isolates_provider_errors(
    client, register_user, monkeypatch
):
    users = {
        name: _user_id(client, register_user(f"{name}@example.com"))
        for name in ("ana", "ben", "cara", "dana", "evan")
    }
    with Session(engine) as session:
        success = _account(users["ana"])
        success.sync_status = "failed"
        success.sync_error_code = "graph_unavailable"
        gmail = _account(users["ben"], provider="google")
        gmail.sync_status = "idle"
        gmail.email = "ben@example.com"
        refresh = _account(users["cara"])
        refresh.sync_status = "idle"
        refresh.token_expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        refresh.email = "cara@example.com"
        running = _account(users["dana"])
        running.sync_status = "running"
        running.sync_status_at = utcnow()
        running.sync_generation = 4
        running.email = "dana@example.com"
        gone = _account(users["evan"], disconnected=True)
        gone.sync_status = "idle"
        gone.sync_generation = 3
        session.add(success)
        session.add(gmail)
        session.add(refresh)
        session.add(running)
        session.add(gone)
        session.commit()
        success_id = success.id
        gmail_id = gmail.id
        refresh_id = refresh.id
        running_id = running.id
        gone_id = gone.id

    monkeypatch.setattr(
        "app.worker.tasks._graph_get",
        lambda url, token: {"value": [GRAPH_ITEM]},
    )
    def _gmail_403(token):
        raise GmailRequestError(403)

    monkeypatch.setattr("app.worker.tasks.collect_gmail_messages", _gmail_403)

    def _refresh_fails(session, account, generation=None):
        raise TokenRefreshError("token_refresh_failed")

    monkeypatch.setattr("app.worker.tasks.refresh_access_token", _refresh_fails)
    result = asyncio.run(sync_connected_mailboxes({"job_id": "cron-1"}))

    assert result["ran"] == 3
    assert result["skipped"] == 1
    assert result["errors"] == 0
    with Session(engine) as session:
        saved = session.get(ConnectedAccount, success_id)
        assert saved.sync_status == "idle"
        assert saved.sync_error_code is None
        assert session.exec(
            select(Message).where(Message.connected_account_id == success_id)
        ).first() is not None
        denied = session.get(ConnectedAccount, gmail_id)
        assert denied.sync_status == "failed"
        assert denied.sync_error_code == "gmail_unavailable"
        expired = session.get(ConnectedAccount, refresh_id)
        assert expired.sync_status == "failed"
        assert expired.sync_error_code == "token_refresh_failed"
        busy = session.get(ConnectedAccount, running_id)
        assert busy.sync_status == "running"
        assert busy.sync_generation == 4
        disconnected = session.get(ConnectedAccount, gone_id)
        assert disconnected.disconnected_at is not None
        assert disconnected.sync_generation == 3


def test_periodic_sync_continues_when_one_account_raises(client, register_user, monkeypatch):
    users = {
        name: _user_id(client, register_user(f"{name}-raise@example.com"))
        for name in ("good", "bad", "busy")
    }
    with Session(engine) as session:
        good = _account(users["good"])
        good.sync_status = "idle"
        bad = _account(users["bad"], provider="google")
        bad.sync_status = "idle"
        bad.email = "bad@example.com"
        busy = _account(users["busy"])
        busy.sync_status = "running"
        busy.sync_status_at = utcnow()
        busy.email = "busy@example.com"
        session.add(good)
        session.add(bad)
        session.add(busy)
        session.commit()
        good_id = str(good.id)
        bad_id = str(bad.id)
        busy_id = str(busy.id)

    calls: list[str] = []

    async def _sync(ctx, account_id, generation=None):
        calls.append(str(account_id))
        if str(account_id) == bad_id:
            raise GmailRequestError(403)
        if str(account_id) == good_id:
            return None
        raise TokenRefreshError("token_refresh_failed")

    monkeypatch.setattr("app.worker.tasks.sync_mailbox_delta", _sync)
    result = asyncio.run(sync_connected_mailboxes({"job_id": "cron-2"}))
    assert set(calls) == {good_id, bad_id}
    assert busy_id not in calls
    assert result["ran"] == 1
    assert result["errors"] == 1
    assert result["skipped"] == 1


def test_periodic_sync_can_be_disabled(client, register_user, monkeypatch):
    headers = register_user("off@example.com")
    user_id = _user_id(client, headers)
    with Session(engine) as session:
        account = _account(user_id)
        account.sync_status = "idle"
        account.sync_generation = 2
        session.add(account)
        session.commit()
        account_id = account.id

    monkeypatch.setattr(get_settings(), "periodic_mailbox_sync", False)

    def _forbid_sync(*args, **kwargs):
        raise AssertionError("disabled cron must not sync")

    monkeypatch.setattr("app.worker.tasks.sync_mailbox_delta", _forbid_sync)
    result = asyncio.run(sync_connected_mailboxes({"job_id": "cron-off"}))
    assert result == {"ran": 0, "skipped": 0, "errors": 0}
    with Session(engine) as session:
        saved = session.get(ConnectedAccount, account_id)
        assert saved.sync_status == "idle"
        assert saved.sync_generation == 2
