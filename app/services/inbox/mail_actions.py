"""Update a synced Outlook or Gmail message using its mailbox grant."""

import json
from typing import Literal
from urllib.parse import quote
from urllib.request import Request, urlopen

MoveAction = Literal["archive", "trash"]


def write_provider_message(
    provider: str,
    token: str,
    provider_message_id: str,
    change: bool | MoveAction,
) -> None:
    message_id = quote(provider_message_id, safe="")
    method = "POST"

    if provider == "microsoft":
        url = f"https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/{message_id}"
        if isinstance(change, bool):
            method = "PATCH"
            payload = {"isRead": change}
        else:
            url += "/move"
            folder = "archive" if change == "archive" else "deleteditems"
            payload = {"destinationId": folder}
    elif provider == "google":
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"
        if change == "trash":
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

    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=10):
        pass
