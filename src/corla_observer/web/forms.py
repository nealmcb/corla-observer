"""A streaming multipart/form-data parser for uploads, writing the file straight to staging.

Starlette's own form parser spools uploads to the system's temporary
directory. An unchecked redacted CVR must never land anywhere but the
workspace's ``staging/`` directory (which is never backed up), so uploads are
parsed here instead, with python-multipart's streaming parser:

- Text fields are kept in memory, at most `MAX_FIELDS` of at most
  `MAX_FIELD_BYTES` each.
- Up to ``max_files`` file parts are accepted (one by default), all with
  the field name ``file_field``. A file part with an empty file name, which
  is what a browser sends for a file input left empty (a form may offer
  two inputs, "Take a photo" and "Choose a file"), is skipped. When a
  file part's headers have been read, ``before_file`` is called with the
  text fields seen so far, which must include everything needed to
  authorize and limit the upload (the CSRF token; browsers send fields in
  document order, so the form puts them before the file inputs). It
  returns the size limit for that file, or raises `FormError` to refuse
  the upload before any of the file is written.
- Each file is written in chunks to a new file (mode 0600) in the staging
  directory. Past its limit, or past ``max_total`` bytes for all files
  together, writing stops, every file is deleted and the result says
  ``too_large``.

The caller deletes the files (`ParsedForm.discard`) when done with them.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from python_multipart.multipart import MultipartParser, parse_options_header

MAX_FIELDS = 16
MAX_FIELD_BYTES = 4096
MAX_HEADER_BYTES = 1024


class FormError(ValueError):
    """The request is not an acceptable upload form."""


@dataclass
class ParsedFile:
    """One uploaded file, in staging."""

    path: Path | None
    name: str
    size: int = 0
    #: The size limit ``before_file`` returned.
    limit: int = 0
    too_large: bool = False


@dataclass
class ParsedForm:
    fields: dict[str, str] = field(default_factory=dict)
    files: list[ParsedFile] = field(default_factory=list)
    #: Set if a file passed its own limit or all of them ``max_total``; every file is then discarded.
    too_large: bool = False

    @property
    def file_path(self) -> Path | None:
        return self.files[0].path if self.files else None

    @property
    def file_name(self) -> str:
        return self.files[0].name if self.files else ""

    @property
    def file_size(self) -> int:
        return self.files[-1].size if self.too_large and self.files else (self.files[0].size if self.files else 0)

    @property
    def limit(self) -> int:
        return self.files[-1].limit if self.files else 0

    def discard(self) -> None:
        for f in self.files:
            if f.path is not None:
                f.path.unlink(missing_ok=True)
                f.path = None


class _State:
    def __init__(
        self, staging: Path, file_field: str, before_file: Callable[[dict[str, str]], int], max_files: int, max_total: int | None
    ) -> None:
        self.staging = staging
        self.file_field = file_field
        self.before_file = before_file
        self.max_files = max_files
        self.max_total = max_total
        self.total = 0
        self.skipping = False
        self.form = ParsedForm()
        self.header_name = b""
        self.header_value = b""
        self.headers: dict[bytes, bytes] = {}
        self.part_name: str | None = None
        self.is_file = False
        self.buffer = bytearray()
        self.out: int | None = None
        self.error: FormError | None = None
        self.done = False

    def fail(self, message: str) -> None:
        if self.error is None:
            self.error = FormError(message)

    # -- callbacks -----------------------------------------------------------

    def on_part_begin(self) -> None:
        self.headers = {}
        self.part_name = None
        self.is_file = False
        self.skipping = False
        self.buffer = bytearray()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.header_value += data[start:end]
        if len(self.header_value) > MAX_HEADER_BYTES:
            self.fail("a part header is too long")

    def on_header_end(self) -> None:
        self.headers[self.header_name.strip().lower()] = self.header_value.strip()
        self.header_name = b""
        self.header_value = b""

    def on_headers_finished(self) -> None:
        if self.error:
            return
        _, params = parse_options_header(self.headers.get(b"content-disposition", b""))
        name = params.get(b"name")
        if name is None:
            self.fail("a part has no name")
            return
        self.part_name = name.decode("utf-8", "replace")
        filename = params.get(b"filename")
        if filename is None:
            if len(self.form.fields) >= MAX_FIELDS:
                self.fail("too many fields")
            return
        if self.part_name != self.file_field or self.form.too_large:
            self.fail("unexpected file")
            return
        if not filename:
            # An empty file input: nothing chosen.
            self.skipping = True
            return
        if len(self.form.files) >= self.max_files:
            self.fail("too many files" if self.max_files > 1 else "unexpected file")
            return
        self.is_file = True
        try:
            limit = self.before_file(dict(self.form.fields))
        except FormError as exc:
            self.error = exc
            return
        fd, name_ = tempfile.mkstemp(dir=self.staging, prefix="web-upload-")
        os.fchmod(fd, 0o600)
        self.out = fd
        self.form.files.append(ParsedFile(Path(name_), filename.decode("utf-8", "replace"), 0, limit))

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self.error or self.form.too_large:
            return
        chunk = data[start:end]
        if self.skipping:
            return
        if self.is_file:
            if self.out is None:
                return
            current = self.form.files[-1]
            current.size += len(chunk)
            self.total += len(chunk)
            if current.size > current.limit or (self.max_total is not None and self.total > self.max_total):
                current.too_large = True
                self.form.too_large = True
                self.close_file()
                self.form.discard()
                return
            os.write(self.out, chunk)
        else:
            self.buffer += chunk
            if len(self.buffer) > MAX_FIELD_BYTES:
                self.fail("a field is too long")

    def on_part_end(self) -> None:
        if self.error or self.skipping:
            return
        if self.is_file:
            self.close_file()
        elif self.part_name is not None:
            self.form.fields[self.part_name] = bytes(self.buffer).decode("utf-8", "replace")

    def on_end(self) -> None:
        self.done = True

    def close_file(self) -> None:
        if self.out is not None:
            os.fsync(self.out)
            os.close(self.out)
            self.out = None


async def parse_upload(
    stream: AsyncIterator[bytes],
    content_type: str,
    staging: Path,
    *,
    file_field: str,
    before_file: Callable[[dict[str, str]], int],
    max_files: int = 1,
    max_total: int | None = None,
) -> ParsedForm:
    """Parse a multipart upload form from ``stream``; see the module documentation.

    Raises:
        FormError: for a request that isn't an acceptable form, or that
            ``before_file`` refused; any partial file is deleted.
    """
    media, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if media != b"multipart/form-data" or not boundary:
        raise FormError("expected multipart/form-data")
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = _State(staging, file_field, before_file, max_files, max_total)
    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": state.on_part_begin,
            "on_part_data": state.on_part_data,
            "on_part_end": state.on_part_end,
            "on_header_field": state.on_header_field,
            "on_header_value": state.on_header_value,
            "on_header_end": state.on_header_end,
            "on_headers_finished": state.on_headers_finished,
            "on_end": state.on_end,
        },
    )
    try:
        async for chunk in stream:
            parser.write(chunk)
            if state.error or state.form.too_large:
                break
        else:
            parser.finalize()
        if state.error:
            raise state.error
        if not state.form.too_large and not state.done:
            raise FormError("the form ended early")
    except BaseException as exc:
        state.close_file()
        state.form.discard()
        if isinstance(exc, FormError):
            raise
        if isinstance(exc, Exception):
            raise FormError(f"the form could not be read: {type(exc).__name__}") from None
        raise
    state.close_file()
    return state.form
