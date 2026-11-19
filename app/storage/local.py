"""Store files on the local disk. Fine for development; not shared across machines."""

import os
from pathlib import Path
from shutil import rmtree
from stat import S_ISDIR


def _relative_key(key: str) -> Path:
    path = Path(key)
    if not path.parts or path.anchor or key.startswith("\\") or ".." in path.parts:
        raise ValueError(f"Invalid storage key: {key!r}")
    return path


def _ignore_missing(_operation, _path, error: Exception) -> None:
    if not isinstance(error, FileNotFoundError):
        raise error


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

    def delete_tree(self, prefix: str) -> None:
        path = self.root.resolve() / _relative_key(prefix)
        if path.parent.resolve() != path.parent:
            raise ValueError("Storage tree has a symlink in its parent path.")
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if S_ISDIR(mode):
            rmtree(path, onexc=_ignore_missing)
        else:
            # Unlink a symlink at the prefix without following it.
            path.unlink(missing_ok=True)

    def _resolve(self, key: str) -> Path:
        root = os.path.realpath(self.root)
        path = os.path.realpath(os.path.join(root, _relative_key(key)))
        root_prefix = root.rstrip(os.sep) + os.sep
        if path == root or not path.startswith(root_prefix):
            raise ValueError(f"Invalid storage key: {key!r}")
        return Path(path)
