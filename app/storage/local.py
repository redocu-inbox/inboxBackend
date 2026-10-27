"""Store files on the local disk. Fine for development; not shared across machines."""

from pathlib import Path


class LocalStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def ready(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        # content_type is unused on disk. S3/R2 will store it as object metadata.
        del content_type
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        path.unlink(missing_ok=True)

    def _resolve(self, key: str) -> Path:
        if not key or key.startswith(("/", "\\")) or ".." in Path(key).parts:
            raise ValueError(f"Invalid storage key: {key!r}")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"Invalid storage key: {key!r}")
        return path
