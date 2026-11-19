import pytest

from app.storage.local import LocalStorage


def test_local_storage_rejects_a_symlink_outside_its_root(tmp_path):
    root = tmp_path / "storage"
    sibling = tmp_path / "storage-other"
    root.mkdir()
    sibling.mkdir()
    unrelated_file = sibling / "file.pdf"
    unrelated_file.write_bytes(b"unrelated file")
    (root / "linked").symlink_to(sibling, target_is_directory=True)
    storage = LocalStorage(str(root))

    with pytest.raises(ValueError):
        storage.put("linked/file.pdf", b"replacement")
    with pytest.raises(ValueError):
        storage.get("linked/file.pdf")
    with pytest.raises(ValueError):
        storage.delete("linked/file.pdf")

    assert unrelated_file.read_bytes() == b"unrelated file"
