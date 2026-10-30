"""Re-run parts of the July 2026 primary audit from the data gathered then, stage by stage.

A rehearsal builds a fresh workspace from what was public or collected at
the time, and runs it through the same code a live audit uses:

``fetch``
    The audit center's files as the public mirror,
    https://github.com/nealmcb/auditcenter, held them at a chosen commit,
    served in place of the Secretary of State's site (`MirrorFetcher`). The
    events are the real fetcher's (`corla_observer.fetch.fetch_urls`): each
    URL is the file's original audit center URL, each ``final_url`` the
    mirror's raw URL at that commit, and every event's data carries
    ``simulated`` (``{"source": "mirror", "repository": ..., "commit": ...,
    "time": ...}``, the time being the commit's, when the mirror recorded
    the files) so that a rehearsal's fetch can never pass for a real one,
    and so that changes are judged against the roll at the time they were
    seen (`corla_observer.flags`). Fetching at
    several commits in turn replays how the files changed, with the alerts
    the real fetcher raised or would have raised (identical content served
    under more than one county's name, and files replaced after the roll).
``uploads``
    The three counties' redacted CVRs and manifests that observers received
    in July 2026 (La Plata, Morgan and Weld, in the mirror's
    ``2026/primary/observerfiles/``), received through the real upload path
    (`corla_observer.uploads.receive`), redaction check included, marked
    ``simulated`` with the time each county's files were received
    (`PRIMARY_2026_RECEIVED`); and each county's audit board count, a
    placeholder flagged as such unless given.
``overrides``
    The data map overrides the primary needs, written to the workspace's
    ``overrides.toml`` if it has none (`PRIMARY_2026_OVERRIDES`: the
    misspelled "Prowes" in Prowers's manifest, and a manifest adjustment
    for a batch that CORLA's own comparison and contest list account for,
    with the evidence), and applied
    (`corla_observer.datamap.apply_overrides`).
``seed``
    The seed, entered by hand as on the day by a single coordinator
    (`corla_observer.seeds`; the entry marked ``rehearsal``), and the time
    of the roll
    (`corla_observer.datamap.record_audit_time`), since the rehearsal's seed
    entry is not at the time of the roll.
``predict``
    The data map, and the predicted draws for the chosen counties' targeted
    contests and the statewide contest (`corla_observer.predict`), stored and
    recorded.
``compare``
    The latest prediction compared with CORLA's published selections, once
    the final reports have been fetched.

Each stage reads only the workspace and its event log (and the mirror for
``fetch`` and ``uploads``), so any stage can be re-run on its own, for
example ``fetch`` at a later commit followed by ``predict`` and
``compare``. `run_all` runs them in order on a new workspace. The mirror's
local path is never recorded; only its public repository URL and the commit.

See docs/REHEARSAL.md.
"""

from __future__ import annotations

import io
import subprocess
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from corla_observer.datamap import (
    OVERRIDES_FILENAME,
    ApplyResult,
    DataMap,
    apply_overrides,
    build_map,
    parse_time,
    record_audit_time,
)
from corla_observer.election import MirrorSetup
from corla_observer.events import EventLog, JSONValue, format_server_time
from corla_observer.flags import Flag, check_flags
from corla_observer.fetch import CrawlReport, FetchPolicy, FetchResult, fetch_urls
from corla_observer.predict import (
    ContestComparison,
    Prediction,
    compare,
    corla_selections,
    latest_prediction,
    predict,
    record_comparison,
    record_prediction,
)
from corla_observer.seeds import enter_seed
from corla_observer.uploads import UploadResult, receive, record_audit_boards
from corla_observer.workspace import Workspace

#: The public mirror of the audit center.
MIRROR_REPOSITORY = "https://github.com/nealmcb/auditcenter"
#: The mirror's raw-file URL at a commit.
MIRROR_RAW_URL = "https://raw.githubusercontent.com/nealmcb/auditcenter/{commit}/{path}"
#: Where the audit center serves the files the mirror keeps under ``<year>/<election>/``.
AUDIT_CENTER_RLA_URL = "https://www.coloradosos.gov/pubs/elections/RLA/"
ELECTION = "2026/primary"
ACTOR = "coordinator:rehearsal"

#: Mirror paths (relative to the election directory) that are not audit
#: center files: observers' own files and the evidence commitments.
NOT_AUDIT_CENTER = ("observerfiles/", "SHA256SUMS")

#: The mirror's commits for the 2026 primary, oldest first, with what each holds.
PRIMARY_2026_COMMITS: tuple[tuple[str, str], ...] = (
    (
        "a4a2b935445406da32f6ce01ba9bc317990c99fa",
        "2026-07-13 09:14 MDT, before the dice roll: 61 county manifests and the target contest list; "
        "two of the manifests hold content identical to a third's",
    ),
    ("23af1c61f97071337b29386d3ceea675678a44f9", "2026-07-13 10:12 MDT: two more county manifests added"),
    ("f46c6630655e115bcf959f797aee42c0b35d7c86", "2026-07-13 10:33 MDT: two county manifests replaced"),
    ("43b6c782887becdd48bd07d3ebcd31915f144c3f", "2026-07-13 16:15 MDT: a county manifest as CORLA imported it, upload status"),
    ("ba18ba6874c5ed4be713b3745a5d32c261cc6083", "2026-07-16: CORLA's final reports"),
)

PRIMARY_2026_COMMIT_IDS = tuple(commit for commit, _ in PRIMARY_2026_COMMITS)

#: The 2026 primary's seed, from the Secretary of State's State Audit Report.
PRIMARY_2026_SEED = "49006417086137856424"
PRIMARY_2026_SEED_SOURCE = (
    "announced at the public dice roll on 2026-07-13; also the 'Audit Random Seed' on the Summary sheet of "
    "the Secretary of State's 2026 primary State Audit Report"
)

#: When the dice roll ended, with how it is known.
PRIMARY_2026_ROLL_TIME = "2026-07-13T10:10:00-06:00"
PRIMARY_2026_ROLL_SOURCE = (
    "the dice roll concluded at approximately 10:10 MDT on 2026-07-13 (the mirror's "
    "2026/primary/observerfiles/2026-primary-details.md)"
)

#: When observers received each county's files, with how it is known.
PRIMARY_2026_RECEIVED: dict[str, tuple[str, str]] = {
    "La Plata": (
        "2026-07-13T09:43:09-06:00",
        "received on 2026-07-13 and committed to the mirror at 09:43 MDT (the mirror's commit 7569ddb)",
    ),
    "Morgan": (
        "2026-07-13T09:43:09-06:00",
        "received on 2026-07-13 and committed to the mirror at 09:43 MDT (the mirror's commit 7569ddb)",
    ),
    "Weld": (
        "2026-07-13T10:03:00-06:00",
        "received by email at 10:03 MDT on 2026-07-13 (the mirror's 2026/primary/observerfiles/2026-primary-details.md)",
    ),
}

#: The counties whose files observers received, by mirror file name.
PRIMARY_2026_COUNTY_FILES: dict[str, dict[str, str]] = {
    "La Plata": {
        "redacted_cvr": "LaPlata_RedactedTest_NoContestBalance_CVR_Export_20260709081424.csv",
        "manifest": "LaPlata_BallotManifest.csv",
    },
    "Morgan": {
        "redacted_cvr": "Morgan_CVR_Export_20260709092315_Redacted.csv",
        "manifest": "Morgan_BallotManifest.csv",
    },
    "Weld": {
        "redacted_cvr": "Weld_CVR_Export_20260709140755.csv",
        "manifest": "Weld_BallotManifest.csv",
    },
}
#: The commit the county files are read at by default.
PRIMARY_2026_UPLOADS_COMMIT = "ba18ba6874c5ed4be713b3745a5d32c261cc6083"

#: The audit board counts are not known; CORLA's final Audit Activity Report
#: shows only audit board 1 for each of these counties.
AUDIT_BOARDS_NOTE = (
    "not known; a rehearsal placeholder. CORLA's final Audit Activity Report for the 2026 primary shows "
    "only audit board 1 for this county"
)

#: The overrides file's first lines.
PRIMARY_2026_OVERRIDES_HEADER = """\
# Data map overrides for the July 2026 primary rehearsal (corla-observer).
# Apply with `corla-observer map apply --workspace DIR --actor coordinator:ID`.
format = 1
election = "2026/primary"
"""

#: Prowers's manifest (files/Prowers.csv, posted with the manifests before the roll) misspells its county once.
PRIMARY_2026_PROWES_ALIAS = """\

[[county_alias]]
value = "Prowes"
county = "Prowers"
reason = "files/Prowers.csv spells its county 'Prowes' on one row (batch 102-4); every other row says Prowers."
"""

#: The manifest adjustment for Alamosa, with the rows of CORLA's final reports that establish it.
PRIMARY_2026_ALAMOSA_ADJUSTMENT = """\

[[manifest_adjustment]]
county = "Alamosa"
append = [{ tabulator = "102", batch = "139", count = 11, location = "" }]
evidence = [
  "finalReports/CountyBallotManifestToCVRcomparison.csv, CORLA's batch-by-batch comparison of the manifest it holds with the CVR: Alamosa,102,139,11,11,0",
  "finalReports/ContestsListRound1.csv: ballot_card_count 3475 for Alamosa's contests and 1444047 statewide; files/Alamosa.csv sums to 3464 and the 63 published manifests to 1444036",
]
reason = "Appends batch 102-139 (11 cards) to Alamosa's rows, so the domain is the 1444047 cards CORLA sampled. The two finalReports files quoted above establish it: the batch-by-batch comparison lists the batch, and the contest list's ballot_card_count accounts for its 11 cards. No storage location for the batch is published. Both files were published after the audit."
"""

PRIMARY_2026_OVERRIDES = PRIMARY_2026_OVERRIDES_HEADER + PRIMARY_2026_PROWES_ALIAS + PRIMARY_2026_ALAMOSA_ADJUSTMENT


class RehearsalError(RuntimeError):
    """A rehearsal stage cannot run (no mirror, an unknown commit, a missing file)."""


def _git(mirror: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(mirror), *args], capture_output=True)
    if result.returncode != 0:
        raise RehearsalError(
            f"git {' '.join(args)} failed in the mirror: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def resolve_commit(mirror: Path, commit: str) -> str:
    """The full hash of ``commit`` in the mirror checkout."""
    return _git(mirror, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()


def commit_time(mirror: Path, commit: str, path: str | None = None) -> str:
    """The committer time (UTC) of ``commit``, or of the last commit up to it that changed ``path``."""
    args = ["log", "-1", "--format=%ct", commit] + (["--", path] if path else [])
    stamp = _git(mirror, *args).decode().strip()
    if not stamp:
        raise RehearsalError(f"no commit up to {commit} changes {path}")
    return format_server_time(datetime.fromtimestamp(int(stamp), UTC))


def mirror_paths(mirror: Path, commit: str, election: str = ELECTION) -> list[str]:
    """The audit center files under ``election/`` in the mirror at ``commit``, without observers' files."""
    names = _git(mirror, "ls-tree", "-r", "--name-only", commit, "--", f"{election}/").decode("utf-8").splitlines()
    prefix = f"{election}/"
    return sorted(
        name
        for name in names
        if name.startswith(prefix) and not any(name[len(prefix) :].startswith(p) for p in NOT_AUDIT_CENTER)
    )


def audit_center_url(path: str) -> str:
    """The audit center URL of a mirror path (``2026/primary/files/Adams.csv``)."""
    return AUDIT_CENTER_RLA_URL + urllib.parse.quote(path)


def mirror_path(url: str) -> str | None:
    """The mirror path of an audit center URL, or None if it is not under the RLA path."""
    if not url.startswith(AUDIT_CENTER_RLA_URL):
        return None
    return urllib.parse.unquote(url[len(AUDIT_CENTER_RLA_URL) :])


class MirrorFetcher:
    """Serves audit center URLs from the mirror at one commit, in place of a `corla_observer.fetch.Fetcher`.

    A URL whose file is not in the mirror at that commit gets a 404.
    """

    def __init__(self, mirror: Path, commit: str, repository: str = MIRROR_REPOSITORY) -> None:
        self.mirror = mirror
        self.commit = resolve_commit(mirror, commit)
        self.repository = repository
        self.time = commit_time(mirror, self.commit)
        self.policy = FetchPolicy()

    def raw_url(self, path: str) -> str:
        return MIRROR_RAW_URL.format(commit=self.commit, path=urllib.parse.quote(path))

    def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        path = mirror_path(url)
        if path is None:
            return FetchResult(url, url, 404, b"", {})
        result = subprocess.run(
            ["git", "-C", str(self.mirror), "show", f"{self.commit}:{path}"], capture_output=True
        )
        if result.returncode != 0:
            return FetchResult(url, self.raw_url(path), 404, b"", {})
        return FetchResult(url, self.raw_url(path), 200, result.stdout, {})

    @property
    def marker(self) -> dict[str, JSONValue]:
        """The ``simulated`` marker recorded in every event of a fetch from this mirror."""
        return {"source": "mirror", "repository": self.repository, "commit": self.commit, "time": self.time}


def stage_fetch(
    workspace: Workspace,
    log: EventLog,
    mirror: Path,
    commit: str,
    *,
    election: str = ELECTION,
    repository: str = MIRROR_REPOSITORY,
    exclude: Sequence[str] = (),
) -> CrawlReport:
    """Fetch the election's audit center files as the mirror held them at ``commit``.

    ``exclude`` lists paths relative to the election's directory (a file, or
    a prefix such as ``round_2/``) left out of the fetch: for an election
    whose mirror history does not show what was posted when (the 2020
    general's was mirrored in one commit, after the audit), the files posted
    after a chosen moment are left out by name instead.
    """
    fetcher = MirrorFetcher(mirror, commit, repository)
    prefix = f"{election}/"
    paths = [p for p in mirror_paths(mirror, fetcher.commit, election) if not any(p[len(prefix) :].startswith(x) for x in exclude if x)]
    urls = [audit_center_url(p) for p in paths]
    if not urls:
        raise RehearsalError(f"no files under {election}/ in the mirror at {fetcher.commit}")
    return fetch_urls(urls, workspace=workspace, log=log, fetcher=fetcher, simulated=fetcher.marker)


#: The workspace file naming the mirror's local checkout for an election replayed from it (never in the event log).
MIRROR_FILENAME = "audit-center-mirror.json"


def set_mirror_checkout(workspace: Workspace, mirror: Path) -> None:
    """Name the mirror's local checkout for `mirror_update`, in the workspace's `MIRROR_FILENAME`."""
    import json

    (workspace.root / MIRROR_FILENAME).write_text(json.dumps({"path": str(mirror)}) + "\n", encoding="utf-8")


def mirror_updates_applied(log: EventLog) -> int:
    """How many ``audit_center.mirror_update`` events the log holds."""
    return sum(1 for event in log.iter_events() if event.kind == "audit_center.mirror_update")


def mirror_update(workspace: Workspace, log: EventLog, setup: MirrorSetup, *, actor: str = "system") -> CrawlReport:
    """Apply the next update of an audit center replayed from the public mirror (after the last, the last again).

    Records ``audit_center.mirror_update`` (its ``index`` from 0, ``title``,
    ``repository``, ``commit``, ``election`` and ``exclude``), then fetches
    the election's files as the mirror held them at the update's commit, less
    its exclusions (`stage_fetch`): ordinary fetch events, each marked
    ``simulated`` with the mirror's repository and commit. The checkout is
    the one `set_mirror_checkout` named.

    Raises:
        RehearsalError: when no checkout is named, or it lacks the commit.
    """
    import json

    path = workspace.root / MIRROR_FILENAME
    try:
        mirror = Path(json.loads(path.read_text(encoding="utf-8"))["path"])
    except (OSError, ValueError, KeyError, TypeError):
        raise RehearsalError(f"no mirror checkout named in the workspace's {MIRROR_FILENAME}") from None
    done = mirror_updates_applied(log)
    index = min(done, len(setup.updates) - 1)
    update = setup.updates[index]
    full = resolve_commit(mirror, update.commit)
    log.append(
        "audit_center.mirror_update", actor,
        data={"index": index, "title": update.title, "repository": setup.repository, "commit": full,
              "election": setup.election, "exclude": list(update.exclude), "again": done >= len(setup.updates)},
    )  # fmt: skip
    return stage_fetch(workspace, log, mirror, full, election=setup.election, repository=setup.repository, exclude=update.exclude)


@dataclass
class UploadsReport:
    results: list[UploadResult] = field(default_factory=list)
    audit_boards: dict[str, tuple[int, bool]] = field(default_factory=dict)


def stage_uploads(
    workspace: Workspace,
    log: EventLog,
    mirror: Path,
    *,
    commit: str = PRIMARY_2026_UPLOADS_COMMIT,
    counties: Iterable[str] = tuple(PRIMARY_2026_COUNTY_FILES),
    audit_boards: Mapping[str, int] | None = None,
    actor: str = ACTOR,
    election: str = ELECTION,
    county_files: Mapping[str, Mapping[str, str]] = PRIMARY_2026_COUNTY_FILES,
    received: Mapping[str, tuple[str, str]] = PRIMARY_2026_RECEIVED,
    audit_boards_note: str = AUDIT_BOARDS_NOTE,
) -> UploadsReport:
    """Receive each county's files from the mirror's observer files through the real upload path.

    ``county_files`` maps each county to its files in the election's
    ``observerfiles/`` directory, by upload kind. Each upload is marked
    ``simulated``, with the time from ``received`` (a time `parse_time`
    reads and its source) or else the time of the commit that added the
    file to the mirror. Each county's audit board
    count is recorded from ``audit_boards`` if given there, otherwise as a
    placeholder of 1, flagged as such, with ``audit_boards_note``.
    """
    full = resolve_commit(mirror, commit)
    report = UploadsReport()
    for county in counties:
        files = county_files.get(county)
        if files is None:
            raise RehearsalError(f"no observer files known for {county}; known: {', '.join(county_files)}")
        for kind, name in files.items():
            path = f"{election}/observerfiles/{name}"
            data = _git(mirror, "show", f"{full}:{path}")
            if county in received:
                moment, time_source = parse_time(received[county][0]), received[county][1]
            else:
                moment, time_source = commit_time(mirror, full, path), "the time the mirror recorded the file"
            marker: dict[str, JSONValue] = {
                "source": "mirror",
                "repository": MIRROR_REPOSITORY,
                "commit": full,
                "time": moment,
                "time_source": time_source,
            }
            report.results.append(
                receive(workspace, log, county, kind, io.BytesIO(data), name, actor, simulated=marker)
            )
        known = (audit_boards or {}).get(county)
        if known is not None:
            record_audit_boards(log, county, known, actor, note="given for the rehearsal")
            report.audit_boards[county] = (known, False)
        else:
            record_audit_boards(log, county, 1, actor, placeholder=True, note=audit_boards_note)
            report.audit_boards[county] = (1, True)
    return report


def stage_overrides(
    workspace: Workspace, log: EventLog, *, actor: str = ACTOR, text: str = PRIMARY_2026_OVERRIDES
) -> ApplyResult:
    """Write ``text`` as the workspace's overrides file if it has none, then apply the file."""
    path = workspace.root / OVERRIDES_FILENAME
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    return apply_overrides(workspace, log, actor, path=path)


def stage_seed(
    log: EventLog,
    *,
    seed: str = PRIMARY_2026_SEED,
    source: str = PRIMARY_2026_SEED_SOURCE,
    actor: str = ACTOR,
    roll_time: str | None = PRIMARY_2026_ROLL_TIME,
    roll_source: str = PRIMARY_2026_ROLL_SOURCE,
) -> int:
    """Enter the seed, and record the time of the roll unless ``roll_time`` is None.

    The entry is marked ``rehearsal``. Returns the ``seed.entered`` event's seq.
    """
    seq = enter_seed(log, seed, actor, source, rehearsal=True)
    if roll_time:
        record_audit_time(log, "roll", roll_time, actor, roll_source)
    return seq


def stage_predict(
    workspace: Workspace,
    log: EventLog,
    *,
    counties: Sequence[str] = tuple(PRIMARY_2026_COUNTY_FILES),
    actor: str = ACTOR,
) -> tuple[DataMap, Prediction, int, str]:
    """Build the data map and predict the draws for ``counties``' contests and the statewide ones; record it."""
    datamap = build_map(log, workspace)
    prediction = predict(datamap, counties=counties)
    seq, sha = record_prediction(workspace, log, prediction, actor)
    return datamap, prediction, seq, sha


def stage_compare(
    workspace: Workspace, log: EventLog, *, actor: str = ACTOR
) -> tuple[int, list[ContestComparison], list[str]]:
    """Compare the latest prediction with CORLA's published selections; record and return the result."""
    latest = latest_prediction(workspace, log)
    if latest is None:
        raise RehearsalError("no prediction yet: run the predict stage first")
    seq, prediction = latest
    published, notes = corla_selections(build_map(log, workspace), workspace)
    if not published:
        raise RehearsalError("CORLA's selections are not in the workspace: " + "; ".join(notes))
    results = compare(prediction, published)
    record_comparison(log, seq, results, notes, actor)
    return seq, results, notes


@dataclass
class RehearsalReport:
    fetches: list[tuple[str, CrawlReport]] = field(default_factory=list)
    uploads: UploadsReport | None = None
    overrides: ApplyResult | None = None
    seed_seq: int | None = None
    datamap: DataMap | None = None
    prediction: Prediction | None = None
    prediction_sha256: str | None = None
    comparisons: list[ContestComparison] = field(default_factory=list)
    compare_notes: list[str] = field(default_factory=list)
    #: The change flags at the end (`corla_observer.flags.check_flags`).
    flags: list[Flag] = field(default_factory=list)


def run_all(
    workspace_dir: Path,
    mirror: Path,
    *,
    commits: Sequence[str] = PRIMARY_2026_COMMIT_IDS,
    counties: Sequence[str] = tuple(PRIMARY_2026_COUNTY_FILES),
    audit_boards: Mapping[str, int] | None = None,
    actor: str = ACTOR,
    election: str = ELECTION,
    county_files: Mapping[str, Mapping[str, str]] = PRIMARY_2026_COUNTY_FILES,
    uploads_commit: str = PRIMARY_2026_UPLOADS_COMMIT,
    overrides_text: str = PRIMARY_2026_OVERRIDES,
    seed: str = PRIMARY_2026_SEED,
    seed_source: str = PRIMARY_2026_SEED_SOURCE,
    roll_time: str | None = PRIMARY_2026_ROLL_TIME,
) -> RehearsalReport:
    """Run every stage in order on a new workspace: fetch at each commit, uploads, overrides, seed, predict, compare.

    Raises:
        RehearsalError: if ``workspace_dir`` exists and is not empty.
    """
    if workspace_dir.exists() and any(workspace_dir.iterdir()):
        raise RehearsalError(f"{workspace_dir} is not empty: a rehearsal starts from a new workspace")
    workspace = Workspace.open(workspace_dir)
    report = RehearsalReport()
    with EventLog.open(workspace_dir) as log:
        for commit in commits:
            report.fetches.append((commit, stage_fetch(workspace, log, mirror, commit, election=election)))
        report.uploads = stage_uploads(
            workspace,
            log,
            mirror,
            commit=uploads_commit,
            counties=counties,
            audit_boards=audit_boards,
            actor=actor,
            election=election,
            county_files=county_files,
        )
        report.overrides = stage_overrides(workspace, log, actor=actor, text=overrides_text)
        report.seed_seq = stage_seed(log, seed=seed, source=seed_source, actor=actor, roll_time=roll_time)
        report.datamap, report.prediction, _, report.prediction_sha256 = stage_predict(
            workspace, log, counties=counties, actor=actor
        )
        try:
            _, report.comparisons, report.compare_notes = stage_compare(workspace, log, actor=actor)
        except RehearsalError as exc:
            report.compare_notes = [str(exc)]
        report.flags = check_flags(log, workspace)
    return report
