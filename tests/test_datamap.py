"""Fast tests for corla_observer.datamap: the data map, overrides, adapters and their commands.

Every test builds a small synthetic election (tests/synthetic.py) in a
temporary workspace, fetched through `corla_observer.fetch.fetch_urls` with a
fake fetcher, so the map is built from the same events a real fetch records.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.datamap import (
    DEFAULT_COUNTY_IDS,
    OVERRIDES_FILENAME,
    DataMap,
    OverridesError,
    append_override,
    apply_overrides,
    build_map,
    describe,
    election_from_url,
    explain,
    parse_cli_value,
    parse_overrides,
    record_seed,
)
from corla_observer.events import EventLog
from corla_observer.fetch import FetchPolicy, FetchResult, fetch_urls
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace
from synthetic import (
    ADAMS_MANIFEST,
    BOULDER_MANIFEST,
    CONTEST_IDS,
    ELECTION_URL,
    contests_list,
    election_files,
)

ACTOR = "coordinator:c1"


class FakeFetcher:
    """Serves fixed content by URL; anything else is a 404."""

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)
        self.policy = FetchPolicy()

    def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        body = self.files.get(url)
        return FetchResult(url, url, 200 if body is not None else 404, body or b"", {})


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def fetch(
    ws: Workspace, log: EventLog, files: Mapping[str, bytes], base: str = ELECTION_URL, report_missing: bool = True
) -> None:
    urls = {base + path: data for path, data in files.items()}
    fetch_urls(list(urls), workspace=ws, log=log, fetcher=FakeFetcher(urls), report_missing=report_missing)


def codes(datamap: DataMap) -> list[str]:
    return [p.code for p in datamap.problems]


def write_overrides(ws: Workspace, text: str) -> Path:
    path = ws.root / OVERRIDES_FILENAME
    path.write_text("format = 1\n" + text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The map without overrides


@pytest.mark.parametrize(
    ("url", "election"),
    [
        ("https://www.coloradosos.gov/pubs/elections/RLA/2026/primary/files/Adams.csv", "2026/primary"),
        ("https://www.coloradosos.gov/pubs/elections/RLA/files/2022/general/round_1/Adams.csv", "2022/general"),
        ("https://www.coloradosos.gov/pubs/elections/RLA/2025/coordinated/riskLimit.html", "2025/coordinated"),
        ("https://www.coloradosos.gov/pubs/elections/RLA/2025/files/AdamsManifest.csv", "2025"),
        ("https://www.coloradosos.gov/pubs/elections/RLA/faqs.html", None),
    ],
)
def test_election_from_url(url: str, election: str | None) -> None:
    assert election_from_url(url) == election


def test_default_county_ids_follow_corla_numbering() -> None:
    assert len(DEFAULT_COUNTY_IDS) == 64
    assert DEFAULT_COUNTY_IDS["Adams"] == 1
    assert (DEFAULT_COUNTY_IDS["Elbert"], DEFAULT_COUNTY_IDS["El Paso"]) == (20, 21)
    assert (DEFAULT_COUNTY_IDS["Lake"], DEFAULT_COUNTY_IDS["La Plata"]) == (33, 34)
    assert DEFAULT_COUNTY_IDS["Broomfield"] == 64
    assert sorted(DEFAULT_COUNTY_IDS.values()) == list(range(1, 65))


def test_pre_audit_map_from_the_target_list(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    datamap = build_map(log, ws)
    assert datamap.election == "2026/general"
    assert sorted(datamap.manifests) == ["Adams", "Boulder", "Weld"]
    assert datamap.manifests["Adams"].ballot_cards == 50
    assert "standard county numbering" in datamap.county_order.source
    statewide = next(c for c in datamap.contests if c.statewide)
    assert statewide.name == "Governor - Democratic Party"
    assert statewide.counties == ["Adams", "Boulder", "Weld"]
    assert statewide.sample_count is None and statewide.margin == 30
    county = next(c for c in datamap.contests if not c.statewide)
    assert (county.name, county.counties) == ("Adams County Clerk - Republican Party (Adams)", ["Adams"])
    found = {p.code: p for p in datamap.check()}
    assert found["no_seed"].severity == "error"
    assert found["statewide_counties_assumed"].severity == "warning"
    assert found["estimated_sample_count"].severity == "info"


def test_post_audit_map_uses_corla_contest_list_and_county_ids(ws: Workspace, log: EventLog) -> None:
    files = election_files(
        finalReports_ContestsListRound1_csv=contests_list(), finalReports_ContestIDsByCounty_csv=CONTEST_IDS
    )
    fetch(ws, log, files)
    datamap = build_map(log, ws)
    assert [(c.name, c.counties, c.sample_count) for c in datamap.contests] == [
        ("Governor - DEM", ["Adams", "Boulder", "Weld"], 9),
        ("Adams County Clerk - REP", ["Adams"], 4),
    ]
    assert datamap.county_order.value == {"Adams": 1, "Boulder": 7, "Weld": 62}
    assert "domain_size_differs" not in codes(datamap)


def test_domain_size_shortfall_is_an_error_until_adjusted(ws: Workspace, log: EventLog) -> None:
    files = election_files(
        finalReports_ContestsListRound1_csv=contests_list(statewide_cards=176),
        finalReports_ContestIDsByCounty_csv=CONTEST_IDS,
    )
    fetch(ws, log, files)
    problem = next(p for p in build_map(log, ws).problems if p.code == "domain_size_differs")
    assert "165" in problem.message and "176" in problem.message
    assert "manifest_adjustment" in problem.hint
    write_overrides(
        ws,
        '[[manifest_adjustment]]\ncounty = "Weld"\nappend = [{ tabulator = "7", batch = "4", count = 11 }]\n'
        'evidence = ["CORLA batch comparison: Weld,7,4,11,11,0"]\nreason = "batch 7-4 missing from the published manifest"\n',
    )
    assert apply_overrides(ws, log, ACTOR).applied
    datamap = build_map(log, ws)
    assert "domain_size_differs" not in codes(datamap)
    assert datamap.adjustments[0].append[0].ballot_count == 11
    assert "[evidence: CORLA batch comparison" in datamap.adjustments[0].reason


def test_manifest_under_another_countys_name_and_identical_copies(ws: Workspace, log: EventLog) -> None:
    saguache = b"County,Tabulator,Batch,# of Ballots\r\nSaguache,102,1,25\r\n"
    fetch(ws, log, election_files(files_Adams_csv=saguache, files_Saguache_csv=saguache))
    datamap = build_map(log, ws)
    warning = next(p for p in datamap.problems if p.code == "manifest_under_other_name")
    assert "Adams.csv holds a manifest for Saguache, not Adams" in warning.message
    assert "Adams" not in datamap.manifests
    assert datamap.manifests["Saguache"].ballot_cards == 25
    assert any("same content as" in n for e in datamap.entries for n in e.notes)


def test_typo_in_county_column_needs_an_alias(ws: Workspace, log: EventLog) -> None:
    typo = b"County,Tabulator,Batch,# of Ballots\r\nBoulder,1,1,40\r\nBouldr,1,2,10\r\n"
    fetch(ws, log, election_files(files_Boulder_csv=typo))
    problem = next(p for p in build_map(log, ws).problems if p.code == "manifest_names_several_counties")
    assert "'Bouldr'" in problem.message and "county_alias" in problem.hint
    write_overrides(ws, '[[county_alias]]\nvalue = "Bouldr"\ncounty = "Boulder"\nreason = "one row misspelled"\n')
    assert apply_overrides(ws, log, ACTOR).applied
    datamap = build_map(log, ws)
    assert datamap.manifests["Boulder"].ballot_cards == 50
    assert datamap.county_aliases == {"Bouldr": "Boulder"}


def test_manifest_that_does_not_load_says_what_to_do(ws: Workspace, log: EventLog) -> None:
    broken = b"County,Tabulator,Batch,# of Ballots\r\nBoulder,1,1,forty\r\n"
    fetch(ws, log, election_files(files_Boulder_csv=broken))
    problem = next(p for p in build_map(log, ws).problems if p.code == "manifest_does_not_load")
    assert "forty" in problem.message and "adapter" in problem.hint


def test_upload_and_audit_center_copies(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    fetch(ws, log, election_files())
    requoted = b'"County","Tabulator","Batch","# of Ballots","Location"\r\n"Adams",101,1,30,"Box 1"\r\n"Adams",101,2,20,"Box 1"\r\n'
    receive(ws, log, "Adams", "manifest", io.BytesIO(requoted), "Adams_manifest.csv", ACTOR)
    datamap = build_map(log, ws)
    chosen = datamap.manifests["Adams"]
    assert chosen.ref == ELECTION_URL + "files/Adams.csv"
    assert chosen.agrees_with == ["upload:Adams:manifest"]

    different = b"County,Tabulator,Batch,# of Ballots\r\nAdams,101,1,30\r\nAdams,101,3,20\r\n"
    receive(ws, log, "Adams", "manifest", io.BytesIO(different), "Adams_manifest2.csv", ACTOR)
    datamap = build_map(log, ws)
    problem = next(p for p in datamap.problems if p.code == "conflicting_manifests")
    assert problem.severity == "warning"
    assert "rows only here: 101-3 (20)" in problem.message
    # The one obtained most recently, the county's second upload, is used, and the problem says so.
    assert datamap.manifests["Adams"].ref == "upload:Adams:manifest"
    assert "using upload:Adams:manifest, the one obtained most recently, obtained 20" in problem.message
    assert any("obtained most recently" in n for n in datamap.entry("upload:Adams:manifest").notes)  # type: ignore[union-attr]
    write_overrides(ws, '[[file]]\nmatch = "upload:Adams:manifest"\nignore = true\nreason = "the county\'s draft"\n')
    assert apply_overrides(ws, log, ACTOR).applied
    datamap = build_map(log, ws)
    assert datamap.manifests["Adams"].ref == ELECTION_URL + "files/Adams.csv"
    upload = datamap.entry("upload:Adams:manifest")
    assert upload is not None and upload.ignored and "the county's draft" in upload.notes[-1]


def test_conflict_resolved_by_the_hash_corla_recorded_importing(ws: Workspace, log: EventLog) -> None:
    different = b"County,Tabulator,Batch,# of Ballots\r\nAdams,101,1,30\r\nAdams,101,3,20\r\n"
    status = (
        "name,filename,type,approx_count,size,status,timestamp,computed_hash,submitted_hash\r\n"
        f"Adams,m.csv,manifest,50,99,IMPORTED,2026-11-10 10:00:00,{hashlib.sha256(different).hexdigest().upper()},X\r\n"
        f"Boulder,b.csv,manifest,50,99,IMPORTED,2026-11-10 10:00:00,{hashlib.sha256(BOULDER_MANIFEST).hexdigest()},X\r\n"
    ).encode()
    fetch(ws, log, election_files(files_uploadStatus_csv=status))
    receive(ws, log, "Adams", "manifest", io.BytesIO(different), "Adams_manifest.csv", ACTOR)
    datamap = build_map(log, ws)
    problem = next(p for p in datamap.problems if p.code == "conflicting_manifests")
    assert problem.severity == "warning" and "byte-identical to the manifest CORLA recorded importing" in problem.message
    assert datamap.manifests["Adams"].ref == "upload:Adams:manifest"
    assert any("byte-identical" in n for n in datamap.manifests["Boulder"].notes)
    # Before the contest-to-county file, the statewide contest covers the counties CORLA imported manifests for.
    statewide = next(c for c in datamap.contests if c.statewide)
    assert statewide.counties == ["Adams", "Boulder"]
    assert "statewide_counties_assumed" not in codes(datamap)


def test_files_of_another_election_are_not_used(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    fetch(
        ws,
        log,
        {"files/Adams.csv": b"County,Tabulator,Batch,# of Ballots\r\nAdams,9,9,9\r\n"},
        base="https://www.coloradosos.gov/pubs/elections/RLA/2024/general/",
        report_missing=False,
    )
    datamap = build_map(log, ws)
    assert datamap.election == "2026/general"
    old = datamap.entry("https://www.coloradosos.gov/pubs/elections/RLA/2024/general/files/Adams.csv")
    assert old is not None and old.ignored and "names election 2024/general" in old.notes[-1]
    assert datamap.manifests["Adams"].ballot_cards == 50


def test_seed_sources_and_conflicts(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    record_seed(log, "12345678901234567890", ACTOR, "announced at the dice roll")
    datamap = build_map(log, ws)
    assert datamap.seed is not None and datamap.seed.value == "12345678901234567890"
    assert "announced at the dice roll" in datamap.seed.source
    fetch(ws, log, {"seed.csv": b"seed\r\n99999999999999999999\r\n"}, report_missing=False)
    assert "seed_conflict" in codes(build_map(log, ws))
    write_overrides(ws, '[seed]\nvalue = "12345678901234567890"\nreason = "the announced seed; seed.csv was wrong"\n')
    assert apply_overrides(ws, log, ACTOR).applied
    datamap = build_map(log, ws)
    assert datamap.seed is not None and datamap.seed.source.startswith("[seed] override")
    assert "seed_conflict" not in codes(datamap) and "seed_override_differs" in codes(datamap)
    with pytest.raises(ValueError):
        record_seed(log, "12ab", ACTOR, "")


# ---------------------------------------------------------------------------
# Overrides


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[[file]]\nmatch = "x"\nignore = true\n', "reason"),
        ('[[file]]\nmatch = "x"\nreason = "r"\ncolour = "red"\n', "unknown key 'colour'"),
        ('[[file]]\nmatch = "x"\nkind = "manifest"\nreason = "r"\n', "kind 'manifest' is not one of"),
        ('[[file]]\nmatch = "x"\ncounty = "Atlantis"\nreason = "r"\n', "not a Colorado county"),
        ('[[supply]]\nname = "a.csv"\npath = "a.csv"\nsha256 = "abc"\nreason = "r"\n', "sha256 must be 64 hex"),
        ('[[supply]]\nname = "a.csv"\nurl = "http://x/a"\nsha256 = "' + "0" * 64 + '"\nreason = "r"\n', "https://"),
        ('[[adapter]]\nmatch = "x"\nmodule = "../evil"\nreason = "r"\n', "module must be"),
        ("[seed]\nvalue = 123\nreason = \"r\"\n", "string of digits"),
        ('[county_order]\nuse = "alphabetical"\nreason = "r"\n', 'use must be "default"'),
        ('[[county_alias]]\nvalue = "Adams"\ncounty = "Weld"\nreason = "r"\n', "already a county's name"),
        ('[[manifest_adjustment]]\ncounty = "Weld"\nreason = "r"\n', "at least one of append"),
        ('[[manifest_adjustment]]\ncounty = "Weld"\nappend = [{ tabulator = "1" }]\nreason = "r"\n', "not a valid adjustment"),
        ('[[contest]]\nname = "X"\nsample_count = -1\nreason = "r"\n', "whole number"),
        ("[[nonsense]]\nreason = \"r\"\n", "unknown section"),
        ("format = 2\n", "format is 2"),
        ("this is not toml", "not valid TOML"),
    ],
)
def test_overrides_are_checked(text: str, message: str) -> None:
    with pytest.raises(OverridesError) as raised:
        parse_overrides(text)
    assert any(message in error for error in raised.value.errors), raised.value.errors


def test_apply_records_the_text_hash_reasons_and_actor(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    path = write_overrides(
        ws, '[[file]]\nmatch = "files/Weld.csv"\nignore = true\nreason = "Weld withdrew its manifest"\n'
    )
    result = apply_overrides(ws, log, ACTOR)
    assert result.applied
    event = list(log.iter_events())[-1]
    assert event.kind == "map.overrides_applied" and event.actor == ACTOR
    assert event.data["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest() == event.file_sha256
    assert event.data["text"] == path.read_text()
    assert event.data["items"] == [{"section": "file", "index": 1, "reason": "Weld withdrew its manifest"}]
    assert ws.get_bytes(result.sha256) == path.read_bytes()
    datamap = build_map(log, ws)
    assert "Weld" not in datamap.manifests and datamap.overrides_seq == event.seq
    assert "Weld withdrew its manifest" in describe(datamap)


def test_a_bad_file_is_rejected_and_the_previous_one_stays(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    write_overrides(ws, '[[file]]\nmatch = "files/Weld.csv"\nignore = true\nreason = "r1"\n')
    assert apply_overrides(ws, log, ACTOR).applied
    write_overrides(ws, '[[file]]\nmatch = "files/Nowhere.csv"\nignore = true\nreason = "r2"\n')
    result = apply_overrides(ws, log, ACTOR)
    assert not result.applied and "matches no file" in result.errors[0]
    assert list(log.iter_events())[-1].kind == "map.overrides_rejected"
    datamap = build_map(log, ws)
    assert "Weld" not in datamap.manifests
    assert "overrides_not_applied" in codes(datamap)
    preview = build_map(log, ws, preview=parse_overrides((ws.root / OVERRIDES_FILENAME).read_text()))
    assert "override_match" in codes(preview) and "Weld" in preview.manifests


def test_reassign_kind_and_county(ws: Workspace, log: EventLog) -> None:
    odd = b"Precinct,Ballots\r\n1,25\r\n"
    fetch(ws, log, election_files(files_Saguache_csv=odd))
    write_overrides(ws, '[[file]]\nmatch = "Saguache.csv"\ncounty = "Saguache"\nkind = "vote_totals"\nreason = "r"\n')
    assert apply_overrides(ws, log, ACTOR).applied
    entry = build_map(log, ws).entry(ELECTION_URL + "files/Saguache.csv")
    assert entry is not None
    assert (entry.classified_kind, entry.kind, entry.county) == ("unknown", "vote_totals", "Saguache")
    assert any("kind set to vote_totals" in n for n in entry.notes)


def test_supply_a_missing_file(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    fetch(ws, log, election_files(files_Weld_csv=None))
    assert "Weld" not in build_map(log, ws).manifests
    local = tmp_path / "private" / "dir" / "Weld_from_county.csv"
    local.parent.mkdir(parents=True)
    weld = b"County,Tabulator,Batch,# of Ballots\r\nWeld,7,1,65\r\n"
    local.write_bytes(weld)
    write_overrides(
        ws,
        f'[[supply]]\nname = "Weld.csv"\npath = "{local}"\nsha256 = "{"0" * 64}"\nreason = "from the county"\n',
    )
    result = apply_overrides(ws, log, ACTOR)
    assert not result.applied and "its SHA-256 is" in result.errors[0]
    write_overrides(
        ws,
        f'[[supply]]\nname = "Weld.csv"\npath = "{local}"\nsha256 = "{hashlib.sha256(weld).hexdigest()}"\n'
        'reason = "the county emailed it on 2026-11-14"\n',
    )
    result = apply_overrides(ws, log, ACTOR)
    assert result.applied and result.supplied == ("Weld.csv",)
    supplied = next(e for e in log.iter_events() if e.kind == "map.file_supplied")
    assert supplied.data["path_name"] == "Weld_from_county.csv"
    assert str(tmp_path) not in json.dumps([e.to_dict() for e in log.iter_events() if e.kind != "map.overrides_applied"])
    datamap = build_map(log, ws)
    assert datamap.manifests["Weld"].ref == "supplied:Weld.csv"
    # Applying the same file again supplies nothing new.
    assert apply_overrides(ws, log, ACTOR).supplied == ()


def test_supply_by_url(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_Weld_csv=None))
    weld = b"County,Tabulator,Batch,# of Ballots\r\nWeld,7,1,65\r\n"
    url = "https://weldcounty.example/manifest.csv"
    write_overrides(
        ws, f'[[supply]]\nname = "Weld.csv"\nurl = "{url}"\nsha256 = "{hashlib.sha256(weld).hexdigest()}"\nreason = "r"\n'
    )
    result = apply_overrides(ws, log, ACTOR, fetch_url=lambda u: weld if u == url else b"")
    assert result.applied
    supplied = next(e for e in log.iter_events() if e.kind == "map.file_supplied")
    assert supplied.data["url"] == url and "Weld" in build_map(log, ws).manifests


ADAPTER = '''\
"""Convert Weld's unusual semicolon layout (Scanner;Batch;Count) to the usual manifest."""


def convert(data, context):
    lines = data.decode("utf-8").split()
    rows = ["County,Tabulator,Batch,# of Ballots"]
    for line in lines[1:]:
        scanner, batch, count = line.split(";")
        rows.append(f"{context['county']},{scanner},{batch},{count}")
    return "\\r\\n".join(rows) + "\\r\\n"
'''


def test_adapter_converts_an_unexpected_layout(ws: Workspace, log: EventLog) -> None:
    weird = b"Scanner;Batch;Count\r\n7;1;25\r\n7;2;40\r\n"
    fetch(ws, log, election_files(files_Weld_csv=weird))
    assert "Weld" not in build_map(log, ws).manifests
    adapters = ws.root / "adapters"
    adapters.mkdir()
    (adapters / "weld_layout.py").write_text(ADAPTER)
    (adapters / "never_named.py").write_text("raise SystemExit('this must never run')\n")
    write_overrides(
        ws,
        '[[adapter]]\nmatch = "files/Weld.csv"\nmodule = "weld_layout"\nname = "Weld-converted.csv"\n'
        'county = "Weld"\nignore_input = true\nreason = "Weld published a semicolon-separated layout"\n',
    )
    result = apply_overrides(ws, log, ACTOR)
    assert result.applied, result.errors
    ran = next(e for e in log.iter_events() if e.kind == "map.adapter_ran")
    source = (adapters / "weld_layout.py").read_bytes()
    assert ran.data["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert ws.get_bytes(str(ran.data["source_sha256"])) == source
    assert ran.data["input_sha256"] == hashlib.sha256(weird).hexdigest()
    datamap = build_map(log, ws)
    assert datamap.manifests["Weld"].ref == "adapter:Weld-converted.csv"
    assert datamap.manifests["Weld"].ballot_cards == 65
    original = datamap.entry(ELECTION_URL + "files/Weld.csv")
    assert original is not None and original.ignored
    # Applying again does not run the adapter again.
    apply_overrides(ws, log, ACTOR)
    assert sum(1 for e in log.iter_events() if e.kind == "map.adapter_ran") == 1


@pytest.mark.parametrize(
    ("module_text", "message"),
    [
        (None, "no adapter module adapters/fix.py"),
        ("def other(data, context):\n    return data\n", "has no function convert"),
        ("def convert(data, context):\n    raise ValueError('bad row 3')\n", "raised ValueError: bad row 3"),
        ("def convert(data, context):\n    return 42\n", "must return non-empty bytes or str"),
        ("def convert(:\n", "invalid syntax"),
    ],
)
def test_adapter_failures_reject_the_overrides(
    ws: Workspace, log: EventLog, module_text: str | None, message: str
) -> None:
    fetch(ws, log, election_files())
    (ws.root / "adapters").mkdir()
    if module_text is not None:
        (ws.root / "adapters" / "fix.py").write_text(module_text)
    write_overrides(ws, '[[adapter]]\nmatch = "files/Weld.csv"\nmodule = "fix"\nreason = "r"\n')
    result = apply_overrides(ws, log, ACTOR)
    assert not result.applied
    assert any(message in e for e in result.errors), result.errors


def test_county_order_and_contest_overrides(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    write_overrides(
        ws,
        '[county_order]\nids = { Adams = 3, Boulder = 2, Weld = 1 }\nreason = "test order"\n\n'
        '[[contest]]\nname = "Governor - Democratic Party"\nsample_count = 12\nreason = "CORLA\'s count, read off its screen"\n\n'
        '[[contest]]\nname = "Adams County Clerk - Republican Party (Adams)"\nexclude = true\nreason = "not targeted after all"\n\n'
        '[[contest]]\nname = "Boulder Mayor"\ncounties = ["Boulder"]\nsample_count = 3\nreason = "added late"\n',
    )
    assert apply_overrides(ws, log, ACTOR).applied
    datamap = build_map(log, ws)
    assert datamap.county_order.value == {"Adams": 3, "Boulder": 2, "Weld": 1}
    assert [(c.name, c.sample_count) for c in datamap.contests] == [
        ("Governor - Democratic Party", 12),
        ("Boulder Mayor", 3),
    ]


def test_append_override_checks_before_writing(tmp_path: Path) -> None:
    path = tmp_path / OVERRIDES_FILENAME
    append_override(path, "file", {"match": "files/A.csv", "ignore": True, "reason": 'says "no"'})
    append_override(path, "seed", {"value": "123", "reason": "r"})
    parsed = parse_overrides(path.read_text())
    assert [i.section for i in parsed.items] == ["file", "seed"]
    assert parsed.items[0].values["reason"] == 'says "no"'
    before = path.read_text()
    with pytest.raises(OverridesError, match="already in"):
        append_override(path, "seed", {"value": "456", "reason": "r"})
    with pytest.raises(OverridesError, match="reason"):
        append_override(path, "file", {"match": "x", "ignore": True})
    assert path.read_text() == before
    assert parse_cli_value("42") == 42 and parse_cli_value("true") is True
    assert parse_cli_value('["a", "b"]') == ["a", "b"] and parse_cli_value("files/A.csv") == "files/A.csv"
    assert parse_cli_value("09175059342981586505", "value") == "09175059342981586505"


def test_describe_for_ai_and_explain(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    datamap = build_map(log, ws)
    text = describe(datamap, for_ai=True)
    assert text.startswith("# corla-observer data map")
    assert "ERROR no_seed" in text and "## Overrides format" in text and "[[manifest_adjustment]]" in text
    explained = explain(datamap, "files/Boulder.csv")
    assert '"count": "# of Ballot Cards"' in explained and "chosen as Boulder's manifest" in explained
    assert "matches no file" in explain(datamap, "nothing.csv")


# ---------------------------------------------------------------------------
# Command line


def test_cli_map_commands(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_Weld_csv=None))
    log.close()
    runner = CliRunner()
    root = str(ws.root)
    early = runner.invoke(app, ["map", "show", "--workspace", root, "--as-of", "2026-11-07 09:00"])
    assert early.exit_code == 0, early.output
    assert "Seed: not yet entered, due at the roll, Mon 16 Nov 10:00 AM" in early.output and "no_seed" not in early.output
    show = runner.invoke(app, ["map", "show", "--workspace", root, "--as-of", "2026-11-16 11:00"])
    assert show.exit_code == 0, show.output
    assert "election: 2026/general" in show.output and "ERROR no_seed" in show.output
    waiting = runner.invoke(app, ["map", "check", "--workspace", root, "--county", "Adams", "--as-of", "2026-11-07 09:00"])
    assert waiting.exit_code == 0 and "not yet expected:" in waiting.output
    check = runner.invoke(app, ["map", "check", "--workspace", root, "--county", "Adams", "--as-of", "2026-11-16 11:00"])
    assert check.exit_code == 1 and "no_seed" in check.output
    assert "statewide_counties_assumed" in check.output and "Weld" in check.output
    as_json = runner.invoke(app, ["map", "show", "--workspace", root, "--json"])
    assert json.loads(as_json.output)["election"] == "2026/general"
    added = runner.invoke(
        app,
        ["map", "override", "seed", "--workspace", root, "--set", "value=12345678901234567890", "--reason", "announced"],
    )
    assert added.exit_code == 0, added.output
    bad = runner.invoke(
        app, ["map", "override", "file", "--workspace", root, "--set", "match=x", "--set", "colour=red", "--reason", "r"]
    )
    assert bad.exit_code == 2 and "unknown key 'colour'" in bad.output
    applied = runner.invoke(app, ["map", "apply", "--workspace", root, "--actor", ACTOR])
    assert applied.exit_code == 0, applied.output
    explained = runner.invoke(app, ["map", "explain", "files/Adams.csv", "--workspace", root])
    assert explained.exit_code == 0 and "chosen as Adams's manifest" in explained.output
    local = ws.root.parent / "loose.csv"
    local.write_bytes(ADAMS_MANIFEST)
    loose = runner.invoke(app, ["map", "explain", str(local), "--workspace", root])
    assert "not in the data map; by content it is county_manifest, county Adams" in loose.output
    check = runner.invoke(app, ["map", "check", "--workspace", root, "--for-ai"])
    assert check.exit_code == 0 and "## Overrides format" in check.output


# ---------------------------------------------------------------------------
# Contest lists of several rounds, in any order


def round_list(rows: list[tuple[str, str, int, int]]) -> bytes:
    """A contest list with ``(name, reason, audited_sample_count, optimistic_samples_to_audit)`` rows."""
    head = (
        "contest_name,audit_reason,random_audit_status,winners_allowed,ballot_card_count,contest_ballot_card_count,"
        "winners,min_margin,risk_limit,audited_sample_count,gamma,overstatements,optimistic_samples_to_audit,"
        "estimated_samples_to_audit\r\n"
    )
    body = "".join(
        f'{n},{r},in_progress,1,{165 if r.startswith("state") else 50},150,"[""A""]",30,0.03,{a},1.03905,0,{o},{o}\r\n'
        for n, r, a, o in rows
    )
    return (head + body).encode()


ROUND1 = round_list(
    [("Governor - DEM", "state_wide_contest", 0, 8), ("Adams County Clerk - REP", "county_wide_contest", 0, 3),
     ("Adams County Coroner - DEM", "opportunistic_benefits", 0, 0)]
)  # fmt: skip
ROUND2 = round_list([("Governor - DEM", "state_wide_contest", 9, 14), ("Adams County Clerk - REP", "county_wide_contest", 4, 3)])


def test_round_of_names() -> None:
    from corla_observer.datamap import round_of

    assert round_of(ELECTION_URL + "round2/contest.csv") == 2
    assert round_of(ELECTION_URL + "round_1/contest.csv") == 1
    assert round_of(ELECTION_URL + "finalReports/ContestsListRound3.csv") == 3
    assert round_of(ELECTION_URL + "files/Contests%20list%20-%20Round%202.csv") == 2
    assert round_of(ELECTION_URL + "files/Adams.csv") is None


def test_round_target_rule() -> None:
    from corla_observer.datamap import round_target

    assert round_target(0, 193) == 194
    assert round_target(194, 193) == 194
    assert round_target(210, 209) == 210
    assert round_target(194, 300) == 301
    assert round_target(7, None) == 7
    assert round_target(0, 0) is None


def test_round_one_list_posted_before_the_audit_holds_zeros(ws: Workspace, log: EventLog) -> None:
    # Named without a round: the round is read from its content (no ballot audited yet).
    fetch(ws, log, election_files() | {"files/contest.csv": ROUND1, "finalReports/ContestIDsByCounty.csv": CONTEST_IDS})
    datamap = build_map(log, ws)
    counts = {c.name: (c.sample_count, c.round_targets) for c in datamap.contests}
    assert counts == {"Governor - DEM": (9, {1: 9}), "Adams County Clerk - REP": (4, {1: 4})}
    governor = next(c for c in datamap.contests if c.statewide)
    assert "optimistic_samples_to_audit 8 + 1" in governor.count_source
    assert any("no ballot audited yet" in n for n in governor.notes)


@pytest.mark.parametrize("order", ["round1_first", "round2_first", "together"])
def test_rounds_in_any_order(ws: Workspace, log: EventLog, order: str) -> None:
    lists = {"round1/contest.csv": ROUND1, "round2/contest.csv": ROUND2}
    base = election_files() | {"finalReports/ContestIDsByCounty.csv": CONTEST_IDS}
    if order == "together":
        fetch(ws, log, base | lists)
    else:
        first, second = sorted(lists, reverse=order == "round2_first")
        fetch(ws, log, base | {first: lists[first]})
        fetch(ws, log, base | lists)
    datamap = build_map(log, ws)
    counts = {c.name: (c.sample_count, c.round_targets) for c in datamap.contests}
    assert counts == {"Governor - DEM": (9, {1: 9, 2: 15}), "Adams County Clerk - REP": (4, {1: 4, 2: 4})}
    assert "several_contest_lists" not in codes(datamap)


def test_only_a_later_round_list(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files() | {"round2/contest.csv": ROUND2, "finalReports/ContestIDsByCounty.csv": CONTEST_IDS})
    datamap = build_map(log, ws)
    governor = next(c for c in datamap.contests if c.statewide)
    assert (governor.sample_count, governor.round_targets) == (9, {2: 15})
    assert "draws made before round 2" in governor.count_source


def test_two_round_one_lists_that_differ(ws: Workspace, log: EventLog) -> None:
    other = round_list([("Governor - DEM", "state_wide_contest", 0, 20), ("Adams County Clerk - REP", "county_wide_contest", 0, 3)])
    fetch(ws, log, election_files() | {"round1/contest.csv": ROUND1, "finalReports/ContestsListRound1.csv": other})
    datamap = build_map(log, ws)
    problem = next(p for p in datamap.problems if p.code == "several_contest_lists")
    assert problem.severity == "warning" and "different targets" in problem.message


def test_target_list_details(ws: Workspace, log: EventLog) -> None:
    targets = (
        b"County,Contest,Vote For,Lowest Winner,Highest Loser,Contest Margin,Diluted Margin,Risk Limit,"
        b"Estimated # of CVRs to audit,# of CVRs,Remarks\r\n"
        b"Colorado,Governor - Democratic Party,1,90,60,30,0.2,0.03,40,165,All counties\r\n"
        b"Adams,Adams County Clerk - Republican Party,1,30,10,20,0.2,0.03,20,50,2 ballot cards per ballot*\r\n"
    )
    fetch(ws, log, election_files(files_targetContests_csv=targets))
    specs = {c.name: c for c in build_map(log, ws).contests}
    clerk = specs["Adams County Clerk - Republican Party (Adams)"]
    assert (clerk.target_denominator, clerk.target_cards_per_ballot) == (100, 2)
    assert specs["Governor - Democratic Party"].target_denominator == 150
    # Once CORLA's list arrives, its contests keep the target list's details.
    fetch(ws, log, election_files(files_targetContests_csv=targets) | {"round1/contest.csv": ROUND1})
    specs = {c.name: c for c in build_map(log, ws).contests}
    assert specs["Adams County Clerk - REP"].target_cards_per_ballot == 2
