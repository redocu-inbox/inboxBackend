"""Object storage factory.

Callers depend on the ObjectStorage protocol, not on the local disk. When we
move uploads to S3 or R2, we add another class and change this factory.
"""

from app.config import get_settings
from app.storage.base import ObjectStorage
from app.storage.local import LocalStorage


def get_storage() -> ObjectStorage:
    settings = get_settings()
    if settings.storage_backend == "local":
        return LocalStorage(settings.storage_local_path)
    raise NotImplementedError(
        f"Storage backend {settings.storage_backend!r} is not implemented yet. Use 'local'."
    )


__all__ = ["ObjectStorage", "get_storage"]
