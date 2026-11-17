"""Address lists for replies, Reply all, and new email.

The strings match what the frontend accepts: "address" or "Name <address>".
"""

import re

from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.services.graph.html_text import to_plain_text

_ADDRESS = re.compile(r"^[^\s@<>,;\"]+@[^\s@<>,;\"]+\.[^\s@<>,;\"]+$")
_NAMED = re.compile(r"^(.*?)\s*<([^<>]+)>$")
MAX_RECIPIENTS = 50


def format_person(name: str | None, email: str | None) -> str | None:
    """One recipient, or None when the address is not a single mailbox."""
    if not isinstance(email, str):
        return None
    address = email.strip()
    if not _ADDRESS.match(address):
        return None
    clean = to_plain_text(name) if name else None
    if not clean or any(char in clean for char in '<>"\r\n'):
        return address
    if "," in clean or ";" in clean:
        return f'"{clean}" <{address}>'
    return f"{clean} <{address}>"


def normalize_recipient(value: object) -> str | None:
    """Canonical form of one recipient, or None when it is not one address."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _NAMED.match(text)
    email = (match.group(2) if match else text).strip()
    if not _ADDRESS.match(email):
        return None
    if match is None:
        return email
    name = match.group(1).strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1].strip()
    if not name:
        return email
    if any(char in name for char in '<>"\r\n'):
        return None
    if "," in name or ";" in name:
        return f'"{name}" <{email}>'
    return f"{name} <{email}>"


def email_of(recipient: str) -> str:
    match = _NAMED.match(recipient.strip())
    return (match.group(2) if match else recipient).strip().lower()


def normalize_list(values: list[str]) -> list[str]:
    """Validate and de-duplicate. Raises ValueError on a bad address or a long list."""
    if len(values) > MAX_RECIPIENTS:
        raise ValueError("too many recipients")
    found: list[str] = []
    seen: set[str] = set()
    for value in values:
        canonical = normalize_recipient(value)
        if canonical is None:
            raise ValueError("invalid recipient")
        key = email_of(canonical)
        if key in seen:
            continue
        seen.add(key)
        found.append(canonical)
    return found


def graph_people(value: object) -> list[str]:
    """Graph recipient array to canonical strings. Bad entries are dropped."""
    if not isinstance(value, list):
        return []
    people: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        email_address = item.get("emailAddress")
        if not isinstance(email_address, dict):
            continue
        formatted = format_person(
            email_address.get("name") if isinstance(email_address.get("name"), str) else None,
            email_address.get("address") if isinstance(email_address.get("address"), str) else None,
        )
        if formatted:
            people.append(formatted)
    return people


def header_people(value: str | None) -> list[str]:
    """Split a To, Cc, or Reply-To header. Commas inside quotes stay in the name."""
    if not value:
        return []
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    people: list[str] = []
    for part in parts:
        canonical = normalize_recipient(part)
        if canonical:
            people.append(canonical)
    return people


def _as_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _dedupe(values: list[str], skip: set[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = email_of(value)
        if key in skip or key in seen:
            continue
        seen.add(key)
        found.append(value)
    return found


def mailbox_addresses(account: ConnectedAccount) -> set[str]:
    """Addresses that belong to this mailbox. Aliases are not stored yet."""
    email = account.email.strip().lower()
    return {email} if email else set()


def reply_all(message: Message, account: ConnectedAccount) -> dict[str, list[str]]:
    """Reply all: Reply-To or the sender, plus original To and Cc, without this mailbox."""
    own = mailbox_addresses(account)
    reply_to = _as_list(message.reply_to)
    sender = format_person(message.from_name, message.from_email)
    seed = reply_to if reply_to else ([sender] if sender else [])
    to = _dedupe(seed, own)
    pool = _as_list(message.to_recipients) + _as_list(message.cc_recipients)
    cc = _dedupe(pool, own | {email_of(item) for item in to})
    return {"to": to, "cc": cc}


def default_reply(message: Message) -> dict[str, list[str]] | None:
    """Who a plain Reply goes to. None when there is no usable address."""
    reply_to = _as_list(message.reply_to)
    if reply_to:
        return {"to": reply_to, "cc": []}
    sender = format_person(message.from_name, message.from_email)
    if sender is None:
        return None
    return {"to": [sender], "cc": []}


def saved_or_default(message: Message, to_recipients: object, cc_recipients: object) -> dict[str, list[str]] | None:
    """Saved draft recipients, or the plain Reply list when the draft has none."""
    if isinstance(to_recipients, list):
        to = _as_list(to_recipients)
        cc = _as_list(cc_recipients)
        if not to:
            return None
        return {"to": to, "cc": cc}
    return default_reply(message)
