"""Thin client for calling the Microsoft Graph API."""

from app.models.connected_account import ConnectedAccount

class GraphClient:
    """One mailbox's Graph calls.

    TODO: refresh the token, then GET the inbox delta and follow nextLink
    until Graph returns a deltaLink. Save that link on the account.
    """

    def __init__(self, account: ConnectedAccount) -> None:
        self.account = account

    async def sync_inbox_delta(self) -> None:
        raise NotImplementedError("Graph delta sync is a later phase")