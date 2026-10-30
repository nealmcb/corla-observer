"""A content-addressed store for every file the project accepts.

Each workspace (one per election) keeps every accepted file unmodified,
exactly once, under its SHA-256::

    <root>/files/ab/abcdef0123...    (the full 64-hex-digit digest)

The store path carries no original filename, extension or uploader: those
belong in the event that records the file's arrival (see
`corla_observer.events`), so the store itself reveals nothing beyond the
content and can be copied or published wholesale.

Guarantees:

- **Write once.** An object is never overwritten. Storing content that is
  already present leaves the existing object untouched (after checking that
  it is still intact).
- **Atomic.** Content is written to a temporary file in the workspace,
  fsynced, and then hard-linked into place, which fails rather than replaces
  if the name exists. A crash leaves either the complete object or no object,
  never a partial one under a hash name. Leftover temporary files are only
  ever in ``<root>/files/.incoming/``.
- **Verified on read.** Reading an object recomputes its SHA-256 and raises
  `CorruptObjectError` if the content no longer matches its name.
- **Streaming.** `Workspace.put_file` and `Workspace.copy_to` read and write
  in fixed-size chunks, so files of hundreds of megabytes never need to fit
  in memory.

Stored objects are made read-only (mode 0444) as a guard against accidental
modification; that is a convenience, not a security boundary. Integrity rests
on the hashes, which are committed and timestamped outside this store.

Standard library only.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

#: Bytes read or written per step when streaming.
CHUNK_SIZE = 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkspaceError(Exception):
    """Base class for workspace errors."""


class CorruptObjectError(WorkspaceError):
    """A stored object's content no longer matches the SHA-256 it is stored under."""


class ObjectNotFoundError(WorkspaceError, FileNotFoundError):
    """No object is stored under the requested SHA-256."""


class FileTooLargeError(WorkspaceError):
    """The content exceeds the caller's size limit; nothing was stored."""


@dataclass(frozen=True)
class StoredFile:
    """The identity of one stored object."""

    sha256: str
    size: int
    #: True if the store already held this content before the call.
    already_present: bool = False


def _check_sha256(sha256: str) -> str:
    if not _SHA256_RE.match(sha256):
        raise ValueError(f"not a lowercase hex SHA-256 digest: {sha256!r}")
    return sha256


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha256_of_file(path: Path) -> tuple[str, int]:
    """Return the SHA-256 (lowercase hex) and size of a file, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class Workspace:
    """A content-addressed file store rooted at a directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.files_dir = self.root / "files"
        self._incoming_dir = self.files_dir / ".incoming"

    @classmethod
    def open(cls, root: Path | str) -> Workspace:
        """Open the workspace at ``root``, creating its directories if needed."""
        workspace = cls(Path(root))
        workspace._incoming_dir.mkdir(parents=True, exist_ok=True)
        return workspace

    # -- paths ---------------------------------------------------------------

    def path_for(self, sha256: str) -> Path:
        """The path an object with this SHA-256 is (or would be) stored at."""
        _check_sha256(sha256)
        return self.files_dir / sha256[:2] / sha256

    def has(self, sha256: str) -> bool:
        """Whether an object is stored under ``sha256`` (without verifying it)."""
        return self.path_for(sha256).is_file()

    def iter_objects(self) -> Iterator[str]:
        """Yield the SHA-256 of every stored object, in sorted order."""
        if not self.files_dir.is_dir():
            return
        for shard in sorted(self.files_dir.iterdir()):
            if not shard.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", shard.name):
                continue
            for obj in sorted(shard.iterdir()):
                if _SHA256_RE.match(obj.name) and obj.name.startswith(shard.name):
                    yield obj.name

    # -- writing -------------------------------------------------------------

    def put_bytes(self, data: bytes) -> StoredFile:
        """Store ``data`` under its SHA-256, unless it is already stored."""
        sha256 = hashlib.sha256(data).hexdigest()
        if self.has(sha256):
            return self._existing(sha256, len(data))
        tmp = self._new_temp()
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            return self._commit(tmp, sha256, len(data))
        finally:
            tmp.unlink(missing_ok=True)

    def put_file(self, source: Path | str | BinaryIO, max_size: int | None = None) -> StoredFile:
        """Store a file's content, streaming it in chunks.

        ``source`` is a path or a binary file object positioned at the start
        of the content (an upload stream, say). The SHA-256 is computed while
        copying, so the source is read only once.

        Raises:
            FileTooLargeError: if ``max_size`` is given and the content is
                longer; nothing is stored.
        """
        if isinstance(source, (str, Path)):
            with open(source, "rb") as f:
                return self._put_stream(f, max_size)
        return self._put_stream(source, max_size)

    def _put_stream(self, stream: BinaryIO, max_size: int | None) -> StoredFile:
        digest = hashlib.sha256()
        size = 0
        tmp = self._new_temp()
        try:
            with open(tmp, "wb") as out:
                while chunk := stream.read(CHUNK_SIZE):
                    size += len(chunk)
                    if max_size is not None and size > max_size:
                        raise FileTooLargeError(f"content exceeds the limit of {max_size} bytes")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha256 = digest.hexdigest()
            if self.has(sha256):
                return self._existing(sha256, size)
            return self._commit(tmp, sha256, size)
        finally:
            tmp.unlink(missing_ok=True)

    def _new_temp(self) -> Path:
        self._incoming_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self._incoming_dir, prefix="tmp-")
        os.close(fd)
        return Path(name)

    def _commit(self, tmp: Path, sha256: str, size: int) -> StoredFile:
        """Move a fully written, fsynced temporary file into place without overwriting."""
        os.chmod(tmp, 0o444)
        dest = self.path_for(sha256)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(tmp, dest)
        except FileExistsError:
            # Stored concurrently by another writer: keep theirs.
            return self._existing(sha256, size)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV):
                raise
            # A filesystem without hard links: fall back to a checked rename.
            # The window between the check and the rename is a race only
            # between two writers of identical content.
            if dest.exists():
                return self._existing(sha256, size)
            os.rename(tmp, dest)
        _fsync_dir(dest.parent)
        return StoredFile(sha256=sha256, size=size, already_present=False)

    def _existing(self, sha256: str, size: int) -> StoredFile:
        """Confirm the stored copy of content we were asked to store again is intact."""
        self.verify(sha256)
        return StoredFile(sha256=sha256, size=size, already_present=True)

    # -- reading -------------------------------------------------------------

    def verify(self, sha256: str) -> int:
        """Recompute a stored object's hash; return its size.

        Raises:
            ObjectNotFoundError: if no such object is stored.
            CorruptObjectError: if its content does not match ``sha256``.
        """
        path = self.path_for(sha256)
        if not path.is_file():
            raise ObjectNotFoundError(f"no stored object {sha256}")
        actual, size = sha256_of_file(path)
        if actual != sha256:
            raise CorruptObjectError(f"object {sha256} has content hashing to {actual}")
        return size

    def get_bytes(self, sha256: str) -> bytes:
        """Return a stored object's content, after checking it against its hash."""
        path = self.path_for(sha256)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ObjectNotFoundError(f"no stored object {sha256}") from None
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise CorruptObjectError(f"object {sha256} has content hashing to {actual}")
        return data

    def copy_to(self, sha256: str, dest: Path | str) -> StoredFile:
        """Copy a stored object to ``dest``, streaming, and verify it on the way.

        The copy is written to a temporary file beside ``dest`` and renamed
        into place only if the content matches ``sha256``, so a corrupt
        object never appears at ``dest``.
        """
        src = self.path_for(sha256)
        dest = Path(dest)
        digest = hashlib.sha256()
        size = 0
        try:
            f_in = open(src, "rb")
        except FileNotFoundError:
            raise ObjectNotFoundError(f"no stored object {sha256}") from None
        fd, name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.tmp-")
        tmp = Path(name)
        try:
            with f_in, os.fdopen(fd, "wb") as f_out:
                while chunk := f_in.read(CHUNK_SIZE):
                    digest.update(chunk)
                    size += len(chunk)
                    f_out.write(chunk)
                f_out.flush()
                os.fsync(f_out.fileno())
            if digest.hexdigest() != sha256:
                raise CorruptObjectError(
                    f"object {sha256} has content hashing to {digest.hexdigest()}"
                )
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return StoredFile(sha256=sha256, size=size, already_present=True)
