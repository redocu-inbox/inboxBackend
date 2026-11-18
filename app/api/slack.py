"""Explicit Slack send routes. Nothing on this router runs from a background job."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.api.deps import get_current_user, get_session
from app.api.messages import MessageDetailResponse, _detail_response
from app.models.connected_account import ConnectedAccount
from app.models.user import User
from app.services.slack.send import send_slack_new, send_slack_reply

router = APIRouter(tags=["slack"])


class SlackReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=40_000)


class SlackSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=40_000)
    thread_ts: str | None = Field(default=None, max_length=32)


def _detail(session: Session, message) -> MessageDetailResponse:
    account = session.get(ConnectedAccount, message.connected_account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    return _detail_response(session, message, account)


@router.post("/messages/{message_id}/slack-reply")
def slack_reply(
    message_id: UUID,
    body: SlackReplyRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MessageDetailResponse:
    """Reply in the thread of a stored Slack message. The user token posts as that user."""
    message = send_slack_reply(session, user, message_id, body.text)
    return _detail(session, message)


@router.post("/slack/send")
def slack_send(
    body: SlackSendRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MessageDetailResponse:
    """Send a new Slack message from the caller's connected account."""
    message = send_slack_new(
        session,
        user,
        channel=body.channel,
        text=body.text,
        thread_ts=body.thread_ts,
    )
    return _detail(session, message)
