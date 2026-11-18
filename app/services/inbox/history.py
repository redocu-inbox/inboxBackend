"""Paged plain-text history for one provider conversation.

Pages are read from mail already stored for that mailbox. A local unsent
draft is not history.
"""

import base64
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, or_
from sqlmodel import Session, select

from app.models.connected_account import ConnectedAccount
from app.models.message import Message

PAGE_SIZE = 50


def encode_cursor(message: Message) -> str:
    raw = f"{message.received_at.isoformat()}|{message.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        padded = value + "=" * (-len(value) % 4)
        text = base64.urlsafe_b64decode(padded.encode()).decode()
        stamp, raw_id = text.split("|", 1)
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            raise ValueError
        return moment, UUID(raw_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid cursor.",
        ) from exc


def conversation_page(
    session: Session,
    message: Message,
    account: ConnectedAccount,
    cursor: str | None,
) -> dict:
    """Latest page first. Each page is oldest-to-newest. `next_cursor` is null at the start."""
    if not message.conversation_id:
        return {"messages": [], "next_cursor": None}
    statement = select(Message).where(
        Message.user_id == message.user_id,
        Message.connected_account_id == account.id,
        Message.conversation_id == message.conversation_id,
        Message.origin == "provider",
    )
    if cursor:
        moment, message_id = decode_cursor(cursor)
        statement = statement.where(
            or_(
                Message.received_at < moment,
                and_(Message.received_at == moment, Message.id < message_id),
            )
        )
    rows = session.exec(
        statement.order_by(Message.received_at.desc(), Message.id.desc()).limit(PAGE_SIZE + 1)
    ).all()
    more = len(rows) > PAGE_SIZE
    chunk = list(rows[:PAGE_SIZE])
    page = list(reversed(chunk))
    return {
        "messages": [_history_item(row) for row in page],
        "next_cursor": encode_cursor(chunk[-1]) if more and chunk else None,
    }


def _history_item(row: Message) -> dict:
    def listed(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    direction = row.direction if row.direction in {"incoming", "outgoing"} else "incoming"
    return {
        "id": str(row.id),
        "from_name": row.from_name,
        "from_email": row.from_email,
        "to": listed(row.to_recipients),
        "cc": listed(row.cc_recipients),
        "body_text": row.body_text or "",
        "received_at": row.received_at,
        "direction": direction,
        "slack_channel_id": row.slack_channel_id,
        "slack_thread_ts": row.slack_thread_ts,
        "slack_permalink": row.slack_permalink,
    }
