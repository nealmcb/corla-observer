"""Fast tests of mirror.py, the acceptance tests' file fetcher, with no network."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mirror import (
    MirrorFile,
    MirrorMismatch,
    MirrorUnavailable,
    fetch,
    load_mirror_files,
    read_from_clone,
    read_via_cache,
    sha256_hex,
)

CONTENT = b"County,Tabulator,Batch,# of Ballots\nX,1,1,5\n"
ENTRY = MirrorFile("2026/test/files/X.csv", "0" * 40, sha256_hex(CONTENT))


def no_network(url: str) -> bytes:
    raise MirrorUnavailable(f"no network in this test: {url}")


def test_mirror_files_json_is_well_formed() -> None:
    entries = load_mirror_files()
    assert len({e.path for e in entries}) == len(entries)
    for e in entries:
        assert len(e.commit) == 40 and len(e.sha256) == 64
        assert e.url == f"https://raw.githubusercontent.com/nealmcb/auditcenter/{e.commit}/{e.path}"


def test_download_is_verified_and_cached(tmp_path: Path) -> None:
    urls: list[str] = []

    def serve(url: str) -> bytes:
        urls.append(url)
        return CONTENT

    assert read_via_cache(tmp_path, ENTRY, serve) == CONTENT
    assert (tmp_path / ENTRY.commit / ENTRY.path).read_bytes() == CONTENT
    assert read_via_cache(tmp_path, ENTRY, no_network) == CONTENT
    assert urls == [ENTRY.url]


def test_download_with_wrong_hash_is_refused_and_not_cached(tmp_path: Path) -> None:
    with pytest.raises(MirrorMismatch):
        read_via_cache(tmp_path, ENTRY, lambda url: CONTENT + b"tampered\n")
    assert not (tmp_path / ENTRY.commit / ENTRY.path).exists()


def test_corrupt_cached_copy_is_downloaded_again(tmp_path: Path) -> None:
    cached = tmp_path / ENTRY.commit / ENTRY.path
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"truncated")
    assert read_via_cache(tmp_path, ENTRY, lambda url: CONTENT) == CONTENT
    assert cached.read_bytes() == CONTENT


def test_unavailable_everywhere(tmp_path: Path) -> None:
    with pytest.raises(MirrorUnavailable, match="no network"):
        fetch(ENTRY, clone=tmp_path / "not-a-clone", cache_dir=tmp_path, downloader=no_network)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_clone_is_read_at_the_pinned_commit(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(clone), *args], check=True, capture_output=True, text=True, env=env
        ).stdout.strip()

    clone.mkdir()
    git("init", "-q")
    target = clone / ENTRY.path
    target.parent.mkdir(parents=True)
    target.write_bytes(CONTENT)
    git("add", ".")
    git("commit", "-q", "-m", "pinned")
    pinned = MirrorFile(ENTRY.path, git("rev-parse", "HEAD"), ENTRY.sha256)
    target.write_bytes(b"changed later\n")
    git("commit", "-q", "-am", "later")

    assert read_from_clone(clone, pinned) == CONTENT
    cache = tmp_path / "cache"
    assert fetch(pinned, clone=clone, cache_dir=cache, downloader=no_network) == CONTENT
    with pytest.raises(MirrorMismatch):
        read_from_clone(clone, MirrorFile(ENTRY.path, git("rev-parse", "HEAD"), ENTRY.sha256))
