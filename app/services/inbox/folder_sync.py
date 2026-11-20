"""Recheck known messages that an inbox-only sync cannot see."""

import json
from dataclasses import dataclass
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.models.message import MailFolder

_GRAPH = "https://graph.microsoft.com/v1.0"
_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


@dataclass(frozen=True)
class FolderState:
    provider_id: str
    folder: MailFolder | None
    is_read: bool | None = None
    missing: bool = False


def _request(url: str, token: str, payload: dict | None = None) -> dict | None:
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, headers=headers, data=json.dumps(payload).encode() if payload is not None else None)
    with urlopen(request, timeout=4) as response:
        data = json.load(response)
    return data if isinstance(data, dict) else None


def read_provider_folders(provider: str, token: str, ids: list[str]) -> dict[str, FolderState]:
    if not ids:
        return {}
    if provider == "google":
        return _gmail_folders(token, ids)
    try:
        return _outlook_folders(token, ids)
    except HTTPError as exc:
        exc.close()
        return {}
    except (OSError, ValueError):
        return {}


def _gmail_folders(token: str, ids: list[str]) -> dict[str, FolderState]:
    states = {}
    until = monotonic() + 10
    for message_id in ids:
        if monotonic() >= until:
            break
        try:
            data = _request(f"{_GMAIL}/{quote(message_id, safe='')}?format=minimal", token)
        except HTTPError as exc:
            exc.close()
            if exc.code == 404:
                states[message_id] = FolderState(message_id, None, missing=True)
                continue
            break
        except (OSError, ValueError):
            break
        labels = data.get("labelIds") if data and data.get("id") == message_id else None
        if not isinstance(labels, list):
            continue
        folder: MailFolder | None = "archive"
        if "TRASH" in labels:
            folder = "deleted"
        elif "SPAM" in labels or "DRAFT" in labels:
            folder = None
        elif "INBOX" in labels:
            folder = "inbox"
        states[message_id] = FolderState(message_id, folder, "UNREAD" not in labels)
    return states


def _translate(token: str, ids: list[str], source: str, target: str) -> dict[str, str]:
    result = _request(f"{_GRAPH}/me/translateExchangeIds", token, {
        "inputIds": ids, "sourceIdType": source, "targetIdType": target,
    }) or {}
    values = result.get("value")
    if not isinstance(values, list):
        return {}
    return {
        row["sourceId"]: row["targetId"] for row in values
        if isinstance(row, dict) and isinstance(row.get("sourceId"), str)
        and isinstance(row.get("targetId"), str) and 0 < len(row["targetId"]) <= 512
    }


def _outlook_folders(token: str, ids: list[str]) -> dict[str, FolderState]:
    # Use stable IDs for lookups across folders, then store the current REST IDs
    # for existing draft, history, and attachment calls.
    stable = _translate(token, ids, "restId", "restImmutableEntryId")
    if not stable:
        return {}
    names: dict[str, MailFolder] = {"inbox": "inbox", "archive": "archive", "deleteditems": "deleted"}
    requests = [{"id": name, "method": "GET", "url": f"/me/mailFolders/{name}?$select=id"} for name in names]
    lookup = {str(i): original for i, original in enumerate(ids) if original in stable}
    requests.extend({
        "id": key, "method": "GET",
        "url": f"/me/messages/{quote(stable[original], safe='')}?$select=parentFolderId,isRead",
        "headers": {"Prefer": 'IdType="ImmutableId"'},
    } for key, original in lookup.items())
    body = _request(f"{_GRAPH}/$batch", token, {"requests": requests}) or {}
    entries = body.get("responses")
    if not isinstance(entries, list):
        return {}
    responses = {row["id"]: row for row in entries if isinstance(row, dict) and isinstance(row.get("id"), str)}
    by_id = {}
    for name, folder in names.items():
        entry = responses.get(name, {})
        data = entry.get("body")
        if entry.get("status") == 200 and isinstance(data, dict) and isinstance(data.get("id"), str):
            by_id[data["id"]] = folder

    states = {}
    found = {}
    for key, original in lookup.items():
        entry = responses.get(key, {})
        data = entry.get("body")
        if entry.get("status") == 404:
            states[original] = FolderState(original, None, missing=True)
        elif entry.get("status") == 200 and isinstance(data, dict) and len(by_id) == len(names):
            parent = data.get("parentFolderId")
            if isinstance(parent, str):
                found[original] = (by_id.get(parent), data.get("isRead"))
    if not found:
        return states
    current = _translate(token, [stable[original] for original in found], "restImmutableEntryId", "restId")
    for original, (folder, is_read) in found.items():
        new_id = current.get(stable[original])
        if new_id:
            states[original] = FolderState(new_id, folder, is_read if isinstance(is_read, bool) else None)
    return states
