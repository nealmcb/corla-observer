"""Fast tests for corla_observer.workspace, in temporary directories."""

from __future__ import annotations

import hashlib
import io
import os
import stat
from pathlib import Path

import pytest

from corla_observer import workspace as workspace_module
from corla_observer.workspace import (
    CorruptObjectError,
    FileTooLargeError,
    ObjectNotFoundError,
    StoredFile,
    Workspace,
)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


def _corrupt(path: Path, content: bytes) -> None:
    os.chmod(path, 0o644)
    path.write_bytes(content)


def _incoming(ws: Workspace) -> list[Path]:
    return list((ws.files_dir / ".incoming").iterdir())


def test_put_bytes_stores_under_sha256(ws: Workspace) -> None:
    data = b"Tabulator,Batch,Count\n1,1,25\n"
    sha = hashlib.sha256(data).hexdigest()
    stored = ws.put_bytes(data)
    assert stored == StoredFile(sha256=sha, size=len(data), already_present=False)
    path = ws.files_dir / sha[:2] / sha
    assert ws.path_for(sha) == path
    assert path.read_bytes() == data
    assert ws.get_bytes(sha) == data
    assert ws.has(sha)
    assert list(ws.iter_objects()) == [sha]
    assert ws.verify(sha) == len(data)
    # Read-only, and nothing left behind in the incoming directory.
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert _incoming(ws) == []


def test_store_path_has_no_original_name(ws: Workspace, tmp_path: Path) -> None:
    source = tmp_path / "Boulder_redacted_CVR.csv"
    source.write_bytes(b"cvr")
    stored = ws.put_file(source)
    names = {p.name for p in ws.files_dir.rglob("*")}
    assert "Boulder_redacted_CVR.csv" not in names
    assert stored.sha256 in names


def test_dedupe_leaves_existing_object_untouched(ws: Workspace, tmp_path: Path) -> None:
    data = b"same content"
    first = ws.put_bytes(data)
    path = ws.path_for(first.sha256)
    before = path.stat()
    again = ws.put_bytes(data)
    source = tmp_path / "copy.bin"
    source.write_bytes(data)
    via_file = ws.put_file(source)
    assert again.already_present and via_file.already_present
    assert again.sha256 == via_file.sha256 == first.sha256
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert list(ws.iter_objects()) == [first.sha256]
    assert _incoming(ws) == []


def test_never_overwrites_a_corrupted_object(ws: Workspace) -> None:
    data = b"original"
    stored = ws.put_bytes(data)
    path = ws.path_for(stored.sha256)
    _corrupt(path, b"tampered")
    with pytest.raises(CorruptObjectError):
        ws.put_bytes(data)
    with pytest.raises(CorruptObjectError):
        ws.put_file(io.BytesIO(data))
    # The damage is reported, not silently repaired by overwriting.
    assert path.read_bytes() == b"tampered"


def test_reading_verifies_content(ws: Workspace, tmp_path: Path) -> None:
    stored = ws.put_bytes(b"manifest")
    _corrupt(ws.path_for(stored.sha256), b"manifesT")
    with pytest.raises(CorruptObjectError):
        ws.get_bytes(stored.sha256)
    with pytest.raises(CorruptObjectError):
        ws.verify(stored.sha256)
    dest = tmp_path / "out.csv"
    with pytest.raises(CorruptObjectError):
        ws.copy_to(stored.sha256, dest)
    assert not dest.exists()
    assert [p for p in tmp_path.iterdir() if p.name.startswith(".out.csv")] == []


def test_missing_and_malformed_hashes(ws: Workspace, tmp_path: Path) -> None:
    absent = hashlib.sha256(b"absent").hexdigest()
    assert not ws.has(absent)
    with pytest.raises(ObjectNotFoundError):
        ws.get_bytes(absent)
    with pytest.raises(ObjectNotFoundError):
        ws.verify(absent)
    with pytest.raises(ObjectNotFoundError):
        ws.copy_to(absent, tmp_path / "x")
    for bad in ["", "ab", absent.upper(), absent[:-1] + "g", "../" + absent[3:]]:
        with pytest.raises(ValueError):
            ws.path_for(bad)


def test_put_file_from_stream(ws: Workspace) -> None:
    data = b"pull list\n" * 100
    stored = ws.put_file(io.BytesIO(data))
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    assert stored.size == len(data)
    assert ws.get_bytes(stored.sha256) == data


# A 4099-byte block (a prime length, so it never lines up with chunk sizes)
# repeated to make synthetic content quickly.
_PATTERN = b"".join(hashlib.sha256(bytes([i])).digest() for i in range(129))[:4099]


class _ChunkRecordingStream(io.RawIOBase):
    """A synthetic stream of ``size`` bytes of a repeated pattern that records read sizes."""

    def __init__(self, size: int, fail_after: int | None = None) -> None:
        self.size = size
        self.position = 0
        self.fail_after = fail_after
        self.read_sizes: list[int] = []

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        self.read_sizes.append(n)
        if self.fail_after is not None and self.position >= self.fail_after:
            raise OSError("connection dropped")
        if n < 0:
            n = self.size - self.position
        n = min(n, self.size - self.position)
        start = self.position
        self.position += n
        offset = start % len(_PATTERN)
        repeats = (offset + n) // len(_PATTERN) + 1
        return (_PATTERN * repeats)[offset : offset + n]


def test_large_file_streams_in_chunks(
    ws: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, "CHUNK_SIZE", 64 * 1024)
    size = 3 * 1024 * 1024 + 17
    stream = _ChunkRecordingStream(size)
    stored = ws.put_file(stream)  # type: ignore[arg-type]
    assert stored.size == size
    assert max(stream.read_sizes) == 64 * 1024
    assert len(stream.read_sizes) > 40
    expected = hashlib.sha256(_ChunkRecordingStream(size).read()).hexdigest()
    assert stored.sha256 == expected
    dest = tmp_path / "restored.bin"
    copied = ws.copy_to(stored.sha256, dest)
    assert copied.size == size
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == expected


def test_failed_write_stores_nothing(ws: Workspace) -> None:
    stream = _ChunkRecordingStream(10 * 1024 * 1024, fail_after=2 * 1024 * 1024)
    with pytest.raises(OSError, match="connection dropped"):
        ws.put_file(stream)  # type: ignore[arg-type]
    assert list(ws.iter_objects()) == []
    assert _incoming(ws) == []


def test_max_size(ws: Workspace) -> None:
    with pytest.raises(FileTooLargeError):
        ws.put_file(io.BytesIO(b"x" * 1001), max_size=1000)
    assert list(ws.iter_objects()) == []
    assert _incoming(ws) == []
    assert ws.put_file(io.BytesIO(b"x" * 1000), max_size=1000).size == 1000


def test_concurrent_identical_store_keeps_first(ws: Workspace) -> None:
    """If another writer links the same content first, the second keeps theirs."""
    data = b"raced"
    sha = hashlib.sha256(data).hexdigest()
    dest = ws.path_for(sha)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(data)
    inode = dest.stat().st_ino
    # Bypass the up-front presence check to reach the no-overwrite link.
    tmp = ws._new_temp()
    tmp.write_bytes(data)
    stored = ws._commit(tmp, sha, len(data))
    tmp.unlink()
    assert stored.already_present
    assert dest.stat().st_ino == inode
