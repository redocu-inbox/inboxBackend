"""Thin client for calling the Microsoft Graph API.

Delta pages are read here. The worker saves the delta link and the messages.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.models.connected_account import ConnectedAccount

_GRAPH_HOST = "https://graph.microsoft.com/"
_SELECT = (
    "id,subject,bodyPreview,receivedDateTime,from,isRead,body,"
    "conversationId,toRecipients,ccRecipients,replyTo"
)
INBOX_DELTA_URL = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"
    f"?$select={_SELECT}"
)
_PAGE_CAP = 20


@dataclass(frozen=True)
class InboxDelta:
    """One delta walk. `truncated` means a 21st page was left unrequested."""

    changed: list[dict]
    removed_ids: list[str]
    delta_link: str | None
    truncated: bool


class GraphClient:
    """One mailbox's Graph calls."""

    def __init__(self, account: ConnectedAccount) -> None:
        self.account = account

    async def sync_inbox_delta(self, get: Callable[[str, str], dict]) -> InboxDelta:
        """GET the inbox delta and follow nextLink, at most 20 pages.

        `get` is the worker's Graph GET. A stored delta link is requested
        instead of the initial URL. A removed item is returned as an id.
        A delta link seen before the walk is cut short is not returned.
        """
        stored = self.account.delta_link
        if isinstance(stored, str) and stored.startswith(_GRAPH_HOST):
            url: str | None = stored
        else:
            url = INBOX_DELTA_URL
        changed: list[dict] = []
        removed: list[str] = []
        delta_link: str | None = None
        pages = 0
        token = self.account.access_token or ""
        while url and pages < _PAGE_CAP:
            if not url.startswith(_GRAPH_HOST):
                raise ValueError("unexpected message page host")
            payload = get(url, token)
            pages += 1
            values = payload.get("value", [])
            if isinstance(values, list):
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    if item.get("@removed") is not None:
                        graph_id = item.get("id")
                        if isinstance(graph_id, str) and graph_id:
                            removed.append(graph_id)
                        continue
                    changed.append(item)
            link = payload.get("@odata.deltaLink")
            if isinstance(link, str) and link:
                delta_link = link
            next_link = payload.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) and next_link else None
        truncated = url is not None
        if truncated:
            delta_link = None
        return InboxDelta(
            changed=changed,
            removed_ids=removed,
            delta_link=delta_link,
            truncated=truncated,
        )
