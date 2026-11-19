"""The small set of operations the rest of the app needs from file storage."""

from typing import Protocol


class ObjectStorage(Protocol):
    def ready(self) -> None:
        """Raise if the backend cannot read and write."""

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        """Store bytes at `key` and return that key."""

    def get(self, key: str) -> bytes:
        """Return the bytes stored at `key`."""

    def delete(self, key: str) -> None:
        """Remove `key`. Do nothing if it is already gone."""

    def delete_tree(self, prefix: str) -> None:
        """Remove the directory at `prefix` and all its contents, if present."""

