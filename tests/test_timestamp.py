"""Fast tests for corla_observer.timestamp and its commands.

No network and no real ``ots`` or ``cosign``: each test puts a directory on
PATH holding links to git and a few standard tools, plus, where wanted, small
fake ``ots`` and ``cosign`` scripts, and commits into a temporary git
repository (with a bare repository as its remote where pushing is tested).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.events import EventLog, canonical_json
from corla_observer.timestamp import (
    OTS_BITCOIN_TAG,
    CosignOptions,
    TimestampError,
    collect_evidence,
    commit_evidence,
    fetched_path,
    parse_sums,
    sums_name,
    sums_version,
    upgrade_proofs,
    verify_evidence,
)
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace
from synthetic import ballots, make_cvr

ACTOR = "coordinator:c1"
ELECTION = "2026/general"
SUBDIR = "2026/general/observerfiles/evidence"
GOOD_CVR = make_cvr(ballots(10, "Mayor", "Council") + ballots(11, "Mayor", precinct="102"))
BAD_CVR = make_cvr(ballots(10, "Mayor", "Council") + ballots(3, "Mayor", "Fire", precinct="7"))
MANIFEST = b"County,Tabulator,Batch,# of Ballots,Location\r\nBoulder,101,1,25,Box 1\r\n,101,2,30,Box 1\r\n"
NOW = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)

FAKE_OTS = """#!/bin/sh
# A stand-in for the OpenTimestamps client: a 'pending' proof is 15 bytes, an upgraded one longer.
cmd=$1; shift
case "$cmd" in
  stamp)
    for f; do [ -f "$f" ] || exit 2; printf 'FAKEOTS\\203\\337\\343\\015\\056\\371\\014\\216' > "$f.ots"; done
    echo "Submitting to remote calendar https://calendar.example";;
  upgrade)
    for f; do cp "$f" "$f.bak"; printf '\\005\\210\\226\\015\\163\\327\\031\\001' >> "$f"; done
    echo "Success! Timestamp complete";;
  verify)
    if [ "$(wc -c < "$1")" -gt 15 ]; then echo "Success! Bitcoin block 1 attests existence"; exit 0; fi
    echo "Pending confirmation in Bitcoin blockchain"; exit 1;;
esac
"""

FAKE_COSIGN = """#!/bin/sh
# A stand-in for cosign: the 'bundle' records the blob's hash and how it was signed.
cmd=$1; shift
bundle=""; file=""; key="keyless"
while [ $# -gt 0 ]; do
  case "$1" in
    --bundle) bundle=$2; shift 2;;
    --key) key=$2; shift 2;;
    --yes) shift;;
    --certificate-*) shift 2;;
    *) file=$1; shift;;
  esac
done
sha=$(sha256sum "$file" | cut -d' ' -f1)
case "$cmd" in
  sign-blob)
    [ -n "$FAKE_COSIGN_FAIL" ] && { echo "error: no OIDC token"; exit 1; }
    echo "{\\"sha\\":\\"$sha\\",\\"key\\":\\"$key\\"}" > "$bundle"
    echo "tlog entry created with index: 4242" >&2;;
  verify-blob)
    if grep -q "$sha" "$bundle"; then echo "Verified OK"; else echo "error: invalid signature"; exit 1; fi;;
esac
"""


def _bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ots: bool, cosign: bool) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for tool in ("git", "sha256sum", "cut", "grep", "cp", "wc"):
        found = shutil.which(tool)
        assert found, tool
        link = directory / tool
        if not link.exists():
            link.symlink_to(found)
    for name, script, wanted in (("ots", FAKE_OTS, ots), ("cosign", FAKE_COSIGN, cosign)):
        path = directory / name
        if wanted:
            path.write_text(script)
            path.chmod(0o755)
        else:
            path.unlink(missing_ok=True)
    monkeypatch.setenv("PATH", str(directory))
    return directory


@pytest.fixture
def no_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _bin(tmp_path, monkeypatch, ots=False, cosign=False)


@pytest.fixture
def fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _bin(tmp_path, monkeypatch, ots=True, cosign=True)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def out_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "mirror"
    repo.mkdir()
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("mirror\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "-m", "start")
    return repo


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        for county, kind, data, name in (
            ("Boulder", "manifest", MANIFEST, "Boulder manifest.csv"),
            ("Boulder", "redacted_cvr", GOOD_CVR, "boulder-cvr.csv"),
            ("Adams", "redacted_cvr", BAD_CVR, "adams-cvr.csv"),
        ):
            source = tmp_path / name
            source.write_bytes(data)
            receive(store, log, county, kind, source, name, ACTOR)
        prediction = store.put_bytes(canonical_json({"contests": [], "seed": "1"}))
        log.append("prediction.made", ACTOR, file_sha256=prediction.sha256, data={"sha256": prediction.sha256})
        extract = store.put_bytes(canonical_json({"county": "Boulder", "ballots": []}))
        log.append("practice.extract_built", ACTOR, county="Boulder", file_sha256=extract.sha256, data={})
        adams = store.put_bytes(canonical_json({"county": "Adams", "ballots": []}))
        pages = store.put_bytes(canonical_json({"boards": {}, "extract_sha256": adams.sha256}))
        pdf = store.put_bytes(b"%PDF-1.3 a printout")
        log.append(
            "extract.built", ACTOR, county="Adams", file_sha256=adams.sha256,
            data={"sha256": adams.sha256, "page_index_sha256": pages.sha256, "pdfs": {"landscape-3": {"1": pdf.sha256}}},
        )
    return store


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def events(log: EventLog, kind: str) -> list[dict[str, object]]:
    return [dict(e.data) for e in log.iter_events() if e.kind == kind]


def test_names_and_paths() -> None:
    assert sums_name(1) == "SHA256SUMS.txt"
    assert sums_name(3) == "SHA256SUMS.v3.txt"
    assert sums_version("SHA256SUMS.txt") == 1
    assert sums_version("SHA256SUMS.v12.txt") == 12
    assert sums_version("SHA256SUMS.v0.txt") is None
    assert sums_version("SHA256SUMS.txt.ots") is None
    url = "https://www.coloradosos.gov/pubs/elections/RLA/2026/general/files/Boulder%20Manifest.csv"
    assert fetched_path(url) == "auditcenter/2026/general/files/Boulder_Manifest.csv"
    assert fetched_path("https://www.coloradosos.gov/pubs/elections/RLA/").startswith("auditcenter/index.html")
    assert ".." not in fetched_path("https://example.org/a/../../etc/passwd")


def test_collect_evidence(ws: Workspace, log: EventLog, no_tools: Path) -> None:
    evidence = collect_evidence(ws, log)
    paths = {f.path: f for f in evidence.files}
    assert paths["uploads/Boulder/county_manifest/Boulder_manifest.csv"].sha256 == sha(MANIFEST)
    cvr = next(f for f in evidence.files if f.sha256 == sha(GOOD_CVR))
    assert cvr.publishable and cvr.path.startswith("uploads/Boulder/")
    assert sha(BAD_CVR) not in {f.sha256 for f in evidence.files}
    assert "derived/prediction.json" in paths
    assert "derived/extract-Boulder.json" in paths
    assert "derived/extract-Boulder-pages.json" not in paths  # no page index recorded
    assert paths["derived/extract-Adams-pages.json"].origin == "page_index"
    assert paths["derived/extract-Adams-pages.json"].sha256 == next(
        e.data["page_index_sha256"] for e in log.iter_events() if e.kind == "extract.built"
    )
    # Each board's printout in each layout, the PDF whose hash the event records.
    assert paths["derived/extract-Adams-board1-landscape-3.pdf"].origin == "printout"
    assert paths["derived/extract-Adams-board1-landscape-3.pdf"].sha256 == sha(b"%PDF-1.3 a printout")
    assert evidence.log_head == log.head()
    assert evidence.software.startswith("corla-observer ")
    assert [f.path for f in evidence.files] == sorted(f.path for f in evidence.files)


def test_skip_path_without_tools(ws: Workspace, log: EventLog, out_repo: Path, no_tools: Path) -> None:
    head = log.head()
    result = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, actor=ACTOR, clock=lambda: NOW)
    assert result.sums_name == "SHA256SUMS.txt" and result.version == 1
    status = {s.name: s.status for s in result.steps}
    assert status == {"opentimestamps": "skipped", "sigstore": "skipped", "git_commit": "done", "push": "skipped"}
    assert "not installed" in next(s.detail for s in result.steps if s.name == "opentimestamps")
    assert result.proofs == [] and not result.pushed and not result.failed
    sums = (out_repo / SUBDIR / "SHA256SUMS.txt").read_bytes()
    parsed = parse_sums(sums.decode())
    assert parsed.header["event-log-head"] == f"{head[0]} {head[1]}"
    assert parsed.header["time"] == "2026-11-16T16:00:00Z"
    assert parsed.header["version"] == "1"
    assert [p for _, p in parsed.entries] == sorted(p for _, p in parsed.entries)
    assert not parsed.malformed
    assert git(out_repo, "rev-parse", "HEAD").strip() == result.commit
    assert git(out_repo, "show", "--name-only", "--format=", "HEAD").split() == [f"{SUBDIR}/SHA256SUMS.txt"]
    assert "opentimestamps: skipped" in git(out_repo, "log", "-1", "--format=%B")
    [event] = events(log, "evidence.committed")
    assert event["sums_sha256"] == sha(sums) and event["commit"] == result.commit
    assert event["log_head_seq"] == head[0] and event["log_head_hash"] == head[1]
    assert event["version"] == 1 and event["proofs"] == [] and event["pushed"] is False
    assert ws.has(sha(sums))
    # No local paths anywhere in what is published or recorded.
    message = git(out_repo, "log", "-1", "--format=%B")
    for text in (sums.decode(), message, json.dumps(event)):
        assert str(out_repo.parent) not in text


def test_stamped_signed_and_versioned(ws: Workspace, log: EventLog, out_repo: Path, fake_tools: Path) -> None:
    first = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, actor=ACTOR, push=False)
    status = {s.name: s.status for s in first.steps}
    assert status["opentimestamps"] == "pending" and status["sigstore"] == "done"
    assert first.rekor_log_index == 4242
    assert first.proofs == ["SHA256SUMS.txt.ots", "SHA256SUMS.txt.cosign-bundle"]
    committed = set(git(out_repo, "show", "--name-only", "--format=", "HEAD").split())
    assert committed == {f"{SUBDIR}/{n}" for n in ("SHA256SUMS.txt", *first.proofs)}
    v1 = (out_repo / SUBDIR / "SHA256SUMS.txt").read_bytes()
    assert '"key":"keyless"' in (out_repo / SUBDIR / "SHA256SUMS.txt.cosign-bundle").read_text()

    second = commit_evidence(
        ws, log, election=ELECTION, out_repo=out_repo, actor=ACTOR, push=False, cosign=CosignOptions(key=out_repo / "k.key")
    )
    assert second.sums_name == "SHA256SUMS.v2.txt" and second.version == 2
    assert (out_repo / SUBDIR / "SHA256SUMS.txt").read_bytes() == v1
    assert "k.key" in (out_repo / SUBDIR / "SHA256SUMS.v2.txt.cosign-bundle").read_text()
    # The first commitment is now in the log the second one's header commits to.
    head = parse_sums((out_repo / SUBDIR / "SHA256SUMS.v2.txt").read_text()).header["event-log-head"]
    assert int(head.split()[0]) == events_seq(log, "evidence.committed")[0]


def events_seq(log: EventLog, kind: str) -> list[int]:
    return [e.seq for e in log.iter_events() if e.kind == kind]


def test_versions_never_reused(ws: Workspace, log: EventLog, out_repo: Path, no_tools: Path) -> None:
    commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, push=False)
    git(out_repo, "rm", "--quiet", f"{SUBDIR}/SHA256SUMS.txt")
    git(out_repo, "commit", "--quiet", "-m", "remove")
    again = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, push=False)
    assert again.sums_name == "SHA256SUMS.v2.txt"


def test_failed_signing_is_reported(
    ws: Workspace, log: EventLog, out_repo: Path, fake_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COSIGN_FAIL", "1")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["commit-evidence", "--workspace", str(ws.root), "--election", ELECTION, "--out-repo", str(out_repo), "--no-push"],
    )
    assert result.exit_code == 1, result.output
    assert "sigstore: FAILED" in result.output and "no OIDC token" in result.output
    assert "opentimestamps: PENDING" in result.output
    assert not (out_repo / SUBDIR / "SHA256SUMS.txt.cosign-bundle").exists()
    [event] = events(log, "evidence.committed")
    assert event["proofs"] == ["SHA256SUMS.txt.ots"]


def test_cli_skip_path_and_push(ws: Workspace, out_repo: Path, tmp_path: Path, no_tools: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    git(out_repo, "remote", "add", "origin", str(remote))
    runner = CliRunner()
    args = ["commit-evidence", "--workspace", str(ws.root), "--election", ELECTION, "--out-repo", str(out_repo)]
    result = runner.invoke(app, [*args, "--subdir", "observerfiles/test", "--actor", ACTOR])
    assert result.exit_code == 0, result.output
    assert "opentimestamps: SKIPPED" in result.output and "sigstore: SKIPPED" in result.output
    assert "(pushed)" in result.output
    head = git(out_repo, "rev-parse", "HEAD").strip()
    assert git(remote, "rev-parse", "main").strip() == head
    result = runner.invoke(app, [*args, "--subdir", "observerfiles/test", "--no-push"])
    assert result.exit_code == 0 and "(not pushed)" in result.output
    assert (out_repo / "observerfiles/test/SHA256SUMS.v2.txt").is_file()
    assert git(remote, "rev-parse", "main").strip() == head


def test_bad_arguments(ws: Workspace, log: EventLog, out_repo: Path, tmp_path: Path, no_tools: Path) -> None:
    with pytest.raises(TimestampError):
        commit_evidence(ws, log, election="../x", out_repo=out_repo)
    with pytest.raises(TimestampError):
        commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, subdir="../outside")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(TimestampError, match="not a git"):
        commit_evidence(ws, log, election=ELECTION, out_repo=plain)
    assert events(log, "evidence.committed") == []


def test_include_files_and_verify(ws: Workspace, log: EventLog, out_repo: Path, fake_tools: Path) -> None:
    # A CVR in the store with no passing redaction check on record must never be copied.
    unchecked = ws.put_bytes(GOOD_CVR + b"\r\n")
    log.append(
        "file.received", ACTOR, county="Weld", file_sha256=unchecked.sha256,
        data={"kind": "redacted_cvr", "original_name": "weld.csv", "sha256": unchecked.sha256, "size": unchecked.size},
    )
    result = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, push=False, include_files=True)
    target = out_repo / SUBDIR
    withheld = [f for f in result.evidence.files if not f.publishable]
    assert [f.sha256 for f in withheld] == [unchecked.sha256]
    assert not (target / withheld[0].path).exists()
    assert len(result.included) == len(result.evidence.files) - 1
    committed = git(out_repo, "show", "--name-only", "--format=", "HEAD").split()
    assert f"{SUBDIR}/{withheld[0].path}" not in committed
    assert any(p.endswith("Boulder_manifest.csv") for p in committed)

    sums = target / "SHA256SUMS.txt"
    report = verify_evidence(sums)
    status = {c.subject: c.status for c in report.checks}
    assert status[withheld[0].path] == "missing"
    assert status["SHA256SUMS.txt.ots"] == "pending"
    assert status["SHA256SUMS.txt.cosign-bundle"] == "ok"
    assert report.ok and report.count("mismatch") == 0
    # The withheld file is found by hash in the workspace.
    assert {c.subject: c.status for c in verify_evidence(sums, workspace=ws).checks}[withheld[0].path] == "ok"

    manifest = next(target / f.path for f in result.evidence.files if f.path.endswith("Boulder_manifest.csv"))
    manifest.chmod(0o644)
    manifest.write_bytes(MANIFEST + b"extra\r\n")
    runner = CliRunner()
    out = runner.invoke(app, ["verify-evidence", str(sums), "--quiet"])
    assert out.exit_code == 1 and "MISMATCH" in out.output, out.output
    assert "1 differ" in out.output

    upgraded = upgrade_proofs(log, out_repo=out_repo, push=False)
    assert upgraded.upgraded == [f"{SUBDIR}/SHA256SUMS.txt.ots"] and upgraded.commit
    assert not (target / "SHA256SUMS.txt.ots.bak").exists()
    assert OTS_BITCOIN_TAG in (target / "SHA256SUMS.txt.ots").read_bytes()
    assert {c.subject: c.status for c in verify_evidence(sums).checks}["SHA256SUMS.txt.ots"] == "ok"


def test_upgrade_proofs_cli(ws: Workspace, log: EventLog, out_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bin(tmp_path, monkeypatch, ots=True, cosign=False)
    commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, push=False)
    runner = CliRunner()
    args = ["upgrade-proofs", "--out-repo", str(out_repo), "--workspace", str(ws.root), "--no-push"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert f"upgraded: {SUBDIR}/SHA256SUMS.txt.ots" in result.output and "commit " in result.output
    result = runner.invoke(app, args)
    assert result.exit_code == 0 and "already complete" in result.output and "nothing to commit" in result.output
    upgrades = events(log, "evidence.proofs_upgraded")
    assert len(upgrades) == 2 and upgrades[0]["upgraded"] == [f"{SUBDIR}/SHA256SUMS.txt.ots"]
    # The fake upgrade appends a Bitcoin attestation's tag without its block height.
    assert upgrades[0]["bitcoin_blocks"] == {f"{SUBDIR}/SHA256SUMS.txt.ots": None}

    _bin(tmp_path, monkeypatch, ots=False, cosign=False)
    result = runner.invoke(app, [*args, "--all"])
    assert result.exit_code == 1 and "SKIPPED" in result.output
    assert events(log, "evidence.proofs_upgraded")[-1]["skipped"]


def test_verify_without_tools(ws: Workspace, log: EventLog, out_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bin(tmp_path, monkeypatch, ots=True, cosign=True)
    result = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, push=False)
    _bin(tmp_path, monkeypatch, ots=False, cosign=False)
    sums = out_repo / SUBDIR / result.sums_name
    report = verify_evidence(sums)
    assert {c.status for c in report.checks if c.subject.endswith((".ots", ".cosign-bundle"))} == {"skipped"}
    assert report.ok and report.count("missing") == len(result.evidence.files)
    # Unsafe paths in a SUMS file are refused, not read.
    evil = tmp_path / "SHA256SUMS.txt"
    evil.write_text(f"{'0' * 64}  ../../etc/passwd\n{'0' * 64}  /etc/passwd\nnot a line\n")
    report = verify_evidence(evil)
    assert not report.ok and report.count("error") == 3
