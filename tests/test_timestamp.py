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
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.events import EventLog, canonical_json, verify_jsonl
from corla_observer.timestamp import (
    OTS_BITCOIN_TAG,
    CosignOptions,
    TimestampError,
    collect_evidence,
    commit_evidence,
    fetched_path,
    parse_sums,
    publish_evidence,
    sums_name,
    sums_version,
    verify_evidence,
)
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace
from synthetic import ballots, make_cvr

ACTOR = "coordinator:c1"
ELECTION = "2026/general"
SUBDIR = "2026/general/observerfiles"
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
    moment = datetime(2026, 11, 6, 12, 37, 20, 999, tzinfo=ZoneInfo("America/Denver")).replace(microsecond=0)
    assert sums_name(1, moment) == "SHA256SUMS-v1-20261106T193720Z.txt"
    assert sums_name(3, moment) == "SHA256SUMS-v3-20261106T193720Z.txt"
    assert sums_version("SHA256SUMS-v1-20261106T193720Z.txt") == 1
    assert sums_version("SHA256SUMS-v12-20261106T193720Z.txt") == 12
    assert sums_version("SHA256SUMS-v2-2026-11-06T19:37:20Z.txt") is None
    assert sums_version("SHA256SUMS-v2-20261106T193720Z.txt.ots") is None
    # The earlier names, as for the July 2026 primary.
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
    assert paths["observerfiles/uploads/Boulder/county_manifest/Boulder_manifest.csv"].sha256 == sha(MANIFEST)
    cvr = next(f for f in evidence.files if f.sha256 == sha(GOOD_CVR))
    assert cvr.publishable and cvr.path.startswith("observerfiles/uploads/Boulder/")
    assert sha(BAD_CVR) not in {f.sha256 for f in evidence.files}
    assert "observerfiles/derived/prediction.json" in paths
    assert "observerfiles/derived/extract-Boulder.json" in paths
    assert "observerfiles/derived/extract-Boulder-pages.json" not in paths  # no page index recorded
    assert paths["observerfiles/derived/extract-Adams-pages.json"].origin == "page_index"
    assert paths["observerfiles/derived/extract-Adams-pages.json"].sha256 == next(
        e.data["page_index_sha256"] for e in log.iter_events() if e.kind == "extract.built"
    )
    # Each board's printout in each layout, the PDF whose hash the event records.
    assert paths["observerfiles/derived/extract-Adams-board1-landscape-3.pdf"].origin == "printout"
    assert paths["observerfiles/derived/extract-Adams-board1-landscape-3.pdf"].sha256 == sha(b"%PDF-1.3 a printout")
    assert evidence.log_head == log.head()
    assert evidence.software.startswith("corla-observer ")
    assert [f.path for f in evidence.files] == sorted(f.path for f in evidence.files)


def test_skip_path_without_tools(ws: Workspace, log: EventLog, out_repo: Path, no_tools: Path) -> None:
    head = log.head()
    before = git(out_repo, "rev-parse", "HEAD").strip()
    result = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, actor=ACTOR, clock=lambda: NOW)
    assert result.sums_name == "SHA256SUMS-v1-20261116T160000Z.txt" and result.version == 1
    status = {s.name: s.status for s in result.steps}
    assert status == {"opentimestamps": "skipped", "sigstore": "skipped"}
    assert "not installed" in next(s.detail for s in result.steps if s.name == "opentimestamps")
    assert result.proofs == [] and not result.failed
    sums = (out_repo / SUBDIR / result.sums_name).read_bytes()
    parsed = parse_sums(sums.decode())
    assert parsed.header["event-log-head"] == f"{head[0]} {head[1]}"
    assert parsed.header["time"] == "2026-11-16T16:00:00Z"
    assert parsed.header["version"] == "1"
    assert parsed.header["check"] == f"(from {ELECTION}/) sha256sum --check observerfiles/{result.sums_name}"
    assert parsed.header["base"] == ".."
    assert [p for _, p in parsed.entries] == sorted(p for _, p in parsed.entries)
    assert not parsed.malformed
    # Nothing is committed: the SUMS file waits in the working tree, to be published.
    assert git(out_repo, "rev-parse", "HEAD").strip() == before
    assert git(out_repo, "status", "--porcelain", "--untracked-files=all").split() == ["??", f"{SUBDIR}/{result.sums_name}"]
    [event] = events(log, "evidence.committed")
    assert event["sums_sha256"] == sha(sums) and "commit" not in event and "pushed" not in event
    assert event["log_head_seq"] == head[0] and event["log_head_hash"] == head[1]
    assert event["version"] == 1 and event["proofs"] == [] and event["included"] == []
    assert ws.has(sha(sums))
    # No local paths anywhere in what is published or recorded.
    for text in (sums.decode(), json.dumps(event)):
        assert str(out_repo.parent) not in text


def test_stamped_signed_and_versioned(ws: Workspace, log: EventLog, out_repo: Path, fake_tools: Path) -> None:
    first = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, actor=ACTOR)
    status = {s.name: s.status for s in first.steps}
    assert status["opentimestamps"] == "pending" and status["sigstore"] == "done"
    assert first.rekor_log_index == 4242
    one = first.sums_name
    assert one.startswith("SHA256SUMS-v1-") and first.proofs == [f"{one}.ots", f"{one}.cosign-bundle"]
    assert all((out_repo / SUBDIR / n).is_file() for n in (one, *first.proofs))
    v1 = (out_repo / SUBDIR / one).read_bytes()
    assert '"key":"keyless"' in (out_repo / SUBDIR / f"{one}.cosign-bundle").read_text()

    second = commit_evidence(
        ws, log, election=ELECTION, out_repo=out_repo, actor=ACTOR, cosign=CosignOptions(key=out_repo / "k.key")
    )
    two = second.sums_name
    assert two.startswith("SHA256SUMS-v2-") and second.version == 2
    assert (out_repo / SUBDIR / one).read_bytes() == v1
    assert "k.key" in (out_repo / SUBDIR / f"{two}.cosign-bundle").read_text()
    # The first commitment is now in the log the second one's header commits to.
    head = parse_sums((out_repo / SUBDIR / two).read_text()).header["event-log-head"]
    assert int(head.split()[0]) == events_seq(log, "evidence.committed")[0]


def event_seq_of(log: EventLog, kind: str) -> int:
    return [e.seq for e in log.iter_events() if e.kind == kind][-1]


def events_seq(log: EventLog, kind: str) -> list[int]:
    return [e.seq for e in log.iter_events() if e.kind == kind]


def test_versions_never_reused(ws: Workspace, log: EventLog, out_repo: Path, no_tools: Path) -> None:
    first = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo)
    (out_repo / SUBDIR / first.sums_name).unlink()  # never published, and gone: the log still has it
    again = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo)
    assert again.version == 2 and again.sums_name.startswith("SHA256SUMS-v2-")
    # A version used under the earlier names counts too (SHA256SUMS.v3.txt, as for the July 2026 primary).
    (out_repo / SUBDIR / "SHA256SUMS.v3.txt").write_text("# version: 3\n")
    git(out_repo, "add", f"{SUBDIR}/SHA256SUMS.v3.txt")
    git(out_repo, "commit", "--quiet", "-m", "earlier name")
    (out_repo / SUBDIR / "SHA256SUMS.v3.txt").unlink()
    git(out_repo, "commit", "--quiet", "-am", "removed, but in the history")
    assert commit_evidence(ws, log, election=ELECTION, out_repo=out_repo).version == 4


def test_failed_signing_is_reported(
    ws: Workspace, log: EventLog, out_repo: Path, fake_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COSIGN_FAIL", "1")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["commit-evidence", "--workspace", str(ws.root), "--election", ELECTION, "--out-repo", str(out_repo)],
    )
    assert result.exit_code == 1, result.output
    assert "sigstore: FAILED" in result.output and "no OIDC token" in result.output
    assert "opentimestamps: PENDING" in result.output
    [event] = events(log, "evidence.committed")
    name = str(event["sums_name"])
    assert not (out_repo / SUBDIR / f"{name}.cosign-bundle").exists()
    assert event["proofs"] == [f"{name}.ots"]


def _remote(out_repo: Path, ref: str = "main") -> Path:
    """A bare remote for the repository, holding ``ref`` as its main."""
    remote = out_repo.parent / "remote.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)
    git(out_repo, "remote", "add", "origin", str(remote))
    git(out_repo, "push", "--quiet", "origin", f"{ref}:refs/heads/main")
    return remote


def test_cli_commit_then_publish(ws: Workspace, out_repo: Path, tmp_path: Path, no_tools: Path) -> None:
    remote = _remote(out_repo)
    runner = CliRunner()
    args = ["commit-evidence", "--workspace", str(ws.root), "--election", ELECTION, "--out-repo", str(out_repo)]
    result = runner.invoke(app, [*args, "--subdir", "observerfiles/test", "--actor", ACTOR])
    assert result.exit_code == 0, result.output
    assert "opentimestamps: SKIPPED" in result.output and "sigstore: SKIPPED" in result.output and "publish-evidence" in result.output
    start = git(remote, "rev-parse", "main").strip()
    publish = ["publish-evidence", "--out-repo", str(out_repo), "--workspace", str(ws.root), "--title", "A clear headline"]
    shown = runner.invoke(app, [*publish, "--dry-run"])
    assert shown.exit_code == 0 and "dry run" in shown.output and "| A clear headline" in shown.output, shown.output
    assert git(remote, "rev-parse", "main").strip() == start
    done = runner.invoke(app, publish)
    assert done.exit_code == 0 and "published as commit" in done.output, done.output
    assert git(remote, "rev-parse", "main").strip() == git(out_repo, "rev-parse", "HEAD").strip() != start
    assert git(out_repo, "log", "-1", "--format=%s").strip() == "A clear headline"
    again = runner.invoke(app, publish)
    assert again.exit_code == 2 and "no commitment waiting" in again.output


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
    result = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, include="all")
    target = out_repo / SUBDIR
    folder = out_repo / ELECTION  # copies are at their paths in the election's folder
    withheld = [f for f in result.evidence.files if not f.publishable]
    assert [f.sha256 for f in withheld] == [unchecked.sha256]
    assert not (folder / withheld[0].path).exists()
    assert len(result.included) == len(result.evidence.files) - 1
    assert not any(withheld[0].path == p for p in result.included)
    assert any(p.endswith("Boulder_manifest.csv") for p in result.included)

    name = result.sums_name
    sums = target / name
    report = verify_evidence(sums)
    status = {c.subject: c.status for c in report.checks}
    assert status[withheld[0].path] == "missing"
    assert status[f"{name}.ots"] == "pending"
    assert status[f"{name}.cosign-bundle"] == "ok"
    assert report.ok and report.count("mismatch") == 0
    # The withheld file is found by hash in the workspace.
    assert {c.subject: c.status for c in verify_evidence(sums, workspace=ws).checks}[withheld[0].path] == "ok"

    manifest = next(folder / f.path for f in result.evidence.files if f.path.endswith("Boulder_manifest.csv"))
    manifest.chmod(0o644)
    manifest.write_bytes(MANIFEST + b"extra\r\n")
    runner = CliRunner()
    out = runner.invoke(app, ["verify-evidence", str(sums), "--quiet"])
    assert out.exit_code == 1 and "MISMATCH" in out.output, out.output
    assert "1 differ" in out.output

    manifest.write_bytes(MANIFEST)
    # Published: the proof completed, one commit holding the SUMS file, its proofs and the copied files.
    _remote(out_repo)
    published = publish_evidence(log, out_repo=out_repo, actor=ACTOR)
    assert published.opentimestamps == "complete" and published.commit
    assert not (target / f"{name}.ots.bak").exists()
    assert OTS_BITCOIN_TAG in (target / f"{name}.ots").read_bytes()
    assert {c.subject: c.status for c in verify_evidence(sums).checks}[f"{name}.ots"] == "ok"
    committed = set(git(out_repo, "show", "--name-only", "--format=", "HEAD").split())
    assert committed == {f"{SUBDIR}/{p}" for p in (name, f"{name}.ots", f"{name}.cosign-bundle", "events.jsonl")} | {f"{ELECTION}/{p}" for p in result.included}
    assert f"{ELECTION}/{withheld[0].path}" not in committed


def test_upgrade_proofs_cli(ws: Workspace, log: EventLog, out_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bin(tmp_path, monkeypatch, ots=True, cosign=False)
    name = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo).sums_name
    git(out_repo, "add", SUBDIR)  # committed with its pending proof, as in July 2026
    git(out_repo, "commit", "--quiet", "-m", "evidence")
    runner = CliRunner()
    args = ["upgrade-proofs", "--out-repo", str(out_repo), "--workspace", str(ws.root), "--no-push"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert f"upgraded: {SUBDIR}/{name}.ots" in result.output and "commit " in result.output
    result = runner.invoke(app, args)
    assert result.exit_code == 0 and "already complete" in result.output and "nothing to commit" in result.output
    upgrades = events(log, "evidence.proofs_upgraded")
    assert len(upgrades) == 2 and upgrades[0]["upgraded"] == [f"{SUBDIR}/{name}.ots"]
    # The fake upgrade appends a Bitcoin attestation's tag without its block height.
    assert upgrades[0]["bitcoin_blocks"] == {f"{SUBDIR}/{name}.ots": None}

    _bin(tmp_path, monkeypatch, ots=False, cosign=False)
    result = runner.invoke(app, [*args, "--all"])
    assert result.exit_code == 1 and "SKIPPED" in result.output
    assert events(log, "evidence.proofs_upgraded")[-1]["skipped"]


def test_verify_without_tools(ws: Workspace, log: EventLog, out_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bin(tmp_path, monkeypatch, ots=True, cosign=True)
    result = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo)
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


def _keyless_bundle(path: Path, email: str, issuer: str) -> None:
    """A cosign bundle whose certificate names a signer as Fulcio's do: the email, and the issuer in extension 1.3.6.1.4.1.57264.1.8."""
    import base64
    from datetime import timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "sigstore.dev")])
    der_utf8 = b"\x0c" + bytes([len(issuer)]) + issuer.encode()
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
        .not_valid_before(NOW).not_valid_after(NOW + timedelta(minutes=10))
        .add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email)]), critical=True)
        .add_extension(x509.UnrecognizedExtension(x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.8"), der_utf8), critical=False)
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    pem = cert.public_bytes(serialization.Encoding.PEM)
    path.write_text(json.dumps({"base64Signature": "AA==", "cert": base64.b64encode(pem).decode(), "rekorBundle": {}}))


def test_bundle_identity(tmp_path: Path) -> None:
    from corla_observer.timestamp import bundle_identity, verify_command

    bundle = tmp_path / "x.cosign-bundle"
    _keyless_bundle(bundle, "signer@example.org", "https://accounts.google.com")
    signer = bundle_identity(bundle)
    assert signer == ("signer@example.org", "https://accounts.google.com")
    assert verify_command("S.txt", signer) == (
        "cosign verify-blob --bundle S.txt.cosign-bundle --certificate-identity signer@example.org "
        "--certificate-oidc-issuer https://accounts.google.com S.txt"
    )
    bundle.write_text('{"sha": "x", "key": "k.key"}')  # a key file's bundle has no certificate
    assert bundle_identity(bundle) is None


def test_keyless_signing_uses_the_device_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keyless, cosign's input is closed (the device flow: nothing to paste back); with a key file it is the terminal's, for the password."""
    from corla_observer import timestamp

    seen: list[bool] = []

    def fake_run(argv: list[str], **kwargs: object) -> timestamp.ToolRun:
        seen.append(bool(kwargs.get("interactive")))
        return timestamp.ToolRun(1, "no")

    monkeypatch.setattr(timestamp, "run_tool", fake_run)
    monkeypatch.setattr(timestamp.shutil, "which", lambda name: "/bin/cosign")
    blob = tmp_path / "S.txt"
    blob.write_text("x")
    timestamp.sign_cosign(blob, CosignOptions(), timeout=5)
    timestamp.sign_cosign(blob, CosignOptions(key=tmp_path / "k.key"), timeout=5)
    assert seen == [False, True]


def test_published_once_as_one_commit(ws: Workspace, log: EventLog, out_repo: Path, fake_tools: Path) -> None:
    """``publish_evidence``: the proof completed, one commit with the title and the signer, pushed, then ``evidence.published``."""
    title = "2026 General: Auditcenter risk limit with digital timestamp"
    remote = _remote(out_repo)
    first = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, actor=ACTOR)
    name = first.sums_name
    _keyless_bundle(out_repo / SUBDIR / f"{name}.cosign-bundle", "signer@example.org", "https://accounts.google.com")
    # The bundle was replaced after the commitment: fine, the log holds the SUMS file's hash, not the bundle's.
    shown = publish_evidence(log, out_repo=out_repo, title=title, dry_run=True)
    assert shown.commit is None and git(out_repo, "status", "--porcelain").strip()  # nothing committed
    assert events(log, "evidence.published") == []
    published = publish_evidence(log, out_repo=out_repo, actor=ACTOR, title=title)
    assert git(out_repo, "rev-list", "--count", "HEAD").strip() == "2"
    message = git(out_repo, "log", "-1", "--format=%B")
    assert message.splitlines()[0] == title and f"{name}: " in message and "OpenTimestamps: complete" in message
    assert "Signed keyless by signer@example.org (issuer https://accounts.google.com)" in message
    assert f"--certificate-identity signer@example.org --certificate-oidc-issuer https://accounts.google.com {name}" in message
    assert f"Recorded in the observers' event log as event {first.event_seq}." in message
    assert git(remote, "rev-parse", "main").strip() == published.commit
    [event] = events(log, "evidence.published")
    assert event["sums_sha256"] == first.sums_sha256 and event["committed_in_event"] == first.event_seq
    assert event["commit"] == published.commit and event["branch"] == "main" and event["repository"] == str(remote)
    assert event["files"] == [f"{SUBDIR}/{n}" for n in (name, f"{name}.ots", f"{name}.cosign-bundle", "events.jsonl")]
    assert event["opentimestamps"] == "complete" and event["replaces_unpublished_commits"] == []
    # The event log, as it stood: it verifies, passes through the SUMS file's head, and ends just before this event.
    published_log = out_repo / SUBDIR / "events.jsonl"
    checked = verify_jsonl(published_log)
    assert checked.ok and checked.head_seq == event_seq_of(log, "evidence.published") - 1
    assert event["log"] == {"path": f"{SUBDIR}/events.jsonl", "events": checked.head_seq, "head_hash": checked.head_hash}
    assert f"Event log: events.jsonl, events 1 to {checked.head_seq} (head {checked.head_hash})." in message
    with pytest.raises(TimestampError, match="no commitment waiting"):
        publish_evidence(log, out_repo=out_repo)


def test_publishing_refuses_pending_and_altered_files_and_records_only_a_push(
    ws: Workspace, log: EventLog, out_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bin(tmp_path, monkeypatch, ots=True, cosign=False)
    first = commit_evidence(ws, log, election=ELECTION, out_repo=out_repo)
    ots = out_repo / SUBDIR / f"{first.sums_name}.ots"
    (tmp_path / "bin" / "ots").write_text("#!/bin/sh\necho 'Pending confirmation in Bitcoin blockchain'\n")  # never completes
    with pytest.raises(TimestampError, match="still pending"):
        publish_evidence(log, out_repo=out_repo)
    assert publish_evidence(log, out_repo=out_repo, dry_run=True).opentimestamps == "pending"  # shown, not refused
    _bin(tmp_path, monkeypatch, ots=True, cosign=False)
    sums = out_repo / SUBDIR / first.sums_name
    original = sums.read_bytes()
    sums.write_bytes(original + b"# changed\n")
    with pytest.raises(TimestampError, match="differs from the SUMS file the log records"):
        publish_evidence(log, out_repo=out_repo)
    sums.write_bytes(original)
    # No remote: committed, nothing recorded; with one, the same commit is pushed and recorded.
    with pytest.raises(TimestampError, match="no remote"):
        publish_evidence(log, out_repo=out_repo)
    made = git(out_repo, "rev-parse", "HEAD").strip()
    assert events(log, "evidence.published") == [] and OTS_BITCOIN_TAG in ots.read_bytes()
    _remote(out_repo, "HEAD~1")  # the remote has everything but the commitment's commit
    published = publish_evidence(log, out_repo=out_repo)
    assert published.commit == made and git(out_repo, "rev-list", "--count", "HEAD").strip() == "2"


def test_an_earlier_commit_the_log_names_is_said_to_be_replaced(log: EventLog, out_repo: Path, no_tools: Path) -> None:
    """A commitment recorded before publishing was its own step names a commit that was never published: the published event says so."""
    target = out_repo / SUBDIR
    target.mkdir(parents=True)
    content = b"# version: 1\n" + b"0" * 64 + b"  auditcenter/x.html\n"
    (target / "SHA256SUMS-v1-20261106T203137Z.txt").write_bytes(content)
    log.append("evidence.committed", ACTOR, file_sha256=sha(content), data={
        "election": ELECTION, "sums_name": "SHA256SUMS-v1-20261106T203137Z.txt", "sums_sha256": sha(content), "version": 1,
        "subdir": SUBDIR, "files": 1, "included_files": 0, "log_head_seq": 1, "log_head_hash": "0" * 64, "software": "x",
        "steps": [], "proofs": [], "rekor_log_index": None, "commit": "b" * 40, "pushed": False,
    })  # fmt: skip
    _remote(out_repo)
    published = publish_evidence(log, out_repo=out_repo, actor=ACTOR, include_log=False)  # its log head is made up
    assert published.replaces == ["b" * 40]
    assert events(log, "evidence.published")[0]["replaces_unpublished_commits"] == ["b" * 40]


def test_titles_are_one_short_line(ws: Workspace, log: EventLog, out_repo: Path, no_tools: Path) -> None:
    commit_evidence(ws, log, election=ELECTION, out_repo=out_repo)
    for bad in ("", "two\nlines", "x" * 101):
        with pytest.raises(TimestampError, match="one line"):
            publish_evidence(log, out_repo=out_repo, title=bad)


def _fetched(ws: Workspace, log: EventLog, url: str, body: bytes, kind: str, normalized: str | None = None) -> str:
    """Record ``body`` as fetched from ``url`` and classified as ``kind``, as a crawl would."""
    stored = ws.put_bytes(body)
    data: dict[str, Any] = {"url": url, "status": 200, "sha256": stored.sha256, "size": stored.size}
    if normalized is not None:
        data["normalized_sha256"] = normalized
    log.append("fetch.file", "system", file_sha256=stored.sha256, data=data)
    log.append("fetch.classified", "system", file_sha256=stored.sha256, data={"url": url, "sha256": stored.sha256, "kind": kind, "county": None, "evidence": {}})
    return stored.sha256


RLA_URL = "https://www.coloradosos.gov/pubs/elections/RLA/"


def test_the_audit_centers_files_at_their_mirror_paths_and_no_volatile_ones(ws: Workspace, out_repo: Path, no_tools: Path) -> None:
    """A commitment lists the Audit Center's files at the paths the mirror keeps them (relative to the election's folder),
    copies them there by default, and leaves out files that change routinely, such as pages and the upload status."""
    with EventLog.open(ws.root) as log:
        targets = _fetched(ws, log, RLA_URL + "2026/general/files/2026GeneralRLAContestList.xlsx", b"PK targets", "target_contests")
        _fetched(ws, log, RLA_URL + "2026/general/riskLimit.html", b"<html>3%</html>", "html_page", normalized="c" * 64)
        _fetched(ws, log, RLA_URL + "2026/general/files/uploadStatus.csv", b"county,status", "upload_status")
    runner = CliRunner()
    args = ["commit-evidence", "--workspace", str(ws.root), "--election", ELECTION, "--out-repo", str(out_repo)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "1 copied (the files fetched from the Audit Center)" in result.output
    assert (out_repo / ELECTION / "files" / "2026GeneralRLAContestList.xlsx").read_bytes() == b"PK targets"
    assert not (out_repo / ELECTION / "riskLimit.html").exists() and not (out_repo / ELECTION / "files" / "uploadStatus.csv").exists()
    sums = next((out_repo / SUBDIR).glob("SHA256SUMS-v1-*.txt"))
    lines = parse_sums(sums.read_text()).entries
    assert (targets, "files/2026GeneralRLAContestList.xlsx") in lines
    assert not any(p.endswith(("riskLimit.html", "uploadStatus.csv")) for _, p in lines)
    # The check line works as written, from the election's folder; and verify-evidence finds the files by the base.
    check = subprocess.run(["sha256sum", "--check", "--ignore-missing", f"observerfiles/{sums.name}"], cwd=out_repo / ELECTION,
                           capture_output=True, text=True, check=False)  # fmt: skip
    assert check.returncode == 0 and "2026GeneralRLAContestList.xlsx: OK" in check.stdout, check.stdout + check.stderr
    assert {c.subject: c.status for c in verify_evidence(sums).checks}["files/2026GeneralRLAContestList.xlsx"] == "ok"
    with EventLog.open(ws.root, read_only=True) as log:
        assert events(log, "evidence.committed")[-1]["included"] == ["files/2026GeneralRLAContestList.xlsx"]
    bad = runner.invoke(app, [*args, "--include", "everything"])
    assert bad.exit_code == 2 and "include must be one of none, fetched, all" in bad.output


def test_a_replaced_file_is_called_out_with_where_its_previous_version_is(ws: Workspace, out_repo: Path, no_tools: Path) -> None:
    """Publishing a file that replaces an earlier version names it, and the commit that published that version, in the commit and the event."""
    from corla_observer.timestamp import _web_link

    path = f"{ELECTION}/files/Weld.csv"
    _remote(out_repo)
    with EventLog.open(ws.root) as log:
        for body in (b"County,Tabulator\r\nWeld,1\r\n", b"County,Tabulator\r\nWeld,2\r\n"):
            _fetched(ws, log, RLA_URL + "2026/general/files/Weld.csv", body, "county_manifest")
            commit_evidence(ws, log, election=ELECTION, out_repo=out_repo, include="fetched")
            published = publish_evidence(log, out_repo=out_repo, actor=ACTOR)
            if body.endswith(b"1\r\n"):
                first = published.commit
                assert "Changed since" not in published.message and events(log, "evidence.published")[-1]["replaced"] == {}
        assert events(log, "evidence.published")[-1]["replaced"] == {path: first}
    assert f"Changed since its last publication: {path}\n  previous version: commit {first}\n" in published.message
    assert "events.jsonl\n  previous version" not in published.message  # the log only grows: never called out
    assert _web_link("https://github.com/nealmcb/auditcenter.git", "abc", "x/y.html") == "https://github.com/nealmcb/auditcenter/blob/abc/x/y.html"
    assert _web_link("git@github.com:nealmcb/auditcenter.git", "abc", "x/y.html") == "https://github.com/nealmcb/auditcenter/blob/abc/x/y.html"
    assert _web_link("/srv/some/bare.git", "abc", "x") is None


def test_the_previous_version_is_found_in_a_shallow_clone(tmp_path: Path) -> None:
    """In a shallow clone the oldest commit present seems to have added every file: history is fetched further back."""
    from corla_observer.timestamp import _last_change

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--quiet", "-b", "main")
    git(origin, "config", "user.email", "t@example.org")
    git(origin, "config", "user.name", "T")
    (origin / "page.html").write_text("one\n")
    git(origin, "add", ".")
    git(origin, "commit", "--quiet", "-m", "the page")
    published = git(origin, "rev-parse", "HEAD").strip()
    for n in range(3):
        (origin / f"other{n}.txt").write_text("x\n")
        git(origin, "add", ".")
        git(origin, "commit", "--quiet", "-m", f"other {n}")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", "--depth", "1", f"file://{origin}", str(clone)], check=True)
    assert _last_change(clone, "page.html") == published


def test_a_pre_layout_sums_files_audit_center_files_go_where_the_mirror_keeps_them() -> None:
    from corla_observer.timestamp import legacy_mirror_place as place

    assert place("auditcenter/2026/general/files/2026GeneralRLAContestList.xlsx", None) == "2026/general/files/2026GeneralRLAContestList.xlsx"
    assert place("auditcenter/faqs.html", None) == "faqs.html"
    page = "auditcenter/www.coloradosos.gov/pubs/elections/auditCenter.html"
    assert place(page, "2026-11-07T00:24:08.123Z") == "auditCenters/auditCenter-2026-11-06T17:24:08-07:00.html"
    assert place(page, "2026-07-07T18:30:05.000Z") == "auditCenters/auditCenter-2026-07-07T12:30:05-06:00.html"
    assert place("auditcenter/example.org/x.html", None) is None and place("observerfiles/x", None) is None
