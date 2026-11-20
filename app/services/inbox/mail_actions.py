"""Update a synced Outlook or Gmail message using its mailbox grant."""

import json
from typing import Literal
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.models.message import MailFolder

MoveAction = Literal["archive", "trash", "restore"]
DESTINATION: dict[MoveAction, MailFolder] = {"archive": "archive", "trash": "deleted", "restore": "inbox"}
_GRAPH_DESTINATION = {"archive": "archive", "trash": "deleteditems", "restore": "inbox"}


def write_provider_message(
    provider: str,
    token: str,
    provider_message_id: str,
    change: bool | MoveAction,
    current_folder: MailFolder = "inbox",
) -> str | None:
    """Return the replacement provider ID when Graph confirms a move."""
    message_id = quote(provider_message_id, safe="")
    method = "POST"

    if provider == "microsoft":
        route = "mailFolders/inbox/messages" if current_folder == "inbox" else "messages"
        url = f"https://graph.microsoft.com/v1.0/me/{route}/{message_id}"
        if isinstance(change, bool):
            method = "PATCH"
            payload = {"isRead": change}
        else:
            url += "/move"
            payload = {"destinationId": _GRAPH_DESTINATION[change]}
    elif provider == "google":
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"
        if change == "restore":
            if current_folder == "deleted":
                _write(f"{url}/untrash", "POST", token, None)
            url += "/modify"
            payload = {"addLabelIds": ["INBOX"]}
        elif change == "trash":
            url += "/trash"
            payload = None
        else:
            url += "/modify"
            if change == "archive":
                payload = {"removeLabelIds": ["INBOX"]}
            elif change is True:
                payload = {"removeLabelIds": ["UNREAD"]}
            else:
                payload = {"addLabelIds": ["UNREAD"]}
    else:
        raise ValueError("Unsupported mail provider")

    return _write(url, method, token, payload, moved=provider == "microsoft" and not isinstance(change, bool))


def _write(url: str, method: str, token: str, payload: dict | None, *, moved: bool = False) -> str | None:
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=10) as response:
        if not moved:
            return None
        try:
            result = json.load(response)
        except (ValueError, UnicodeError) as exc:
            raise OSError("Outlook did not return the moved message") from exc
        new_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(new_id, str) or not new_id or len(new_id) > 512:
            raise OSError("Outlook did not return the moved message ID")
        return new_id
