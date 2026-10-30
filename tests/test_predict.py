"""Fast tests for corla_observer.predict, on the synthetic election of tests/synthetic.py."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from corla_observer.datamap import apply_overrides, build_map
from corla_observer.events import EventLog
from corla_observer.predict import (
    PredictionError,
    compare,
    corla_contest_name,
    corla_selections,
    latest_prediction,
    optimistic_sample_size,
    predict,
    record_prediction,
    round_one_sample_count,
)
from corla_observer.sampling import ManifestBatch, build_domain_from_manifests, load_county_manifest, select_ballots
from corla_observer.seeds import enter_and_confirm
from corla_observer.workspace import Workspace
from synthetic import (
    ADAMS_MANIFEST,
    BOULDER_MANIFEST,
    CONTEST_IDS,
    WELD_MANIFEST,
    contests_list,
    election_files,
    published_selections,
)
from test_datamap import ACTOR, fetch, write_overrides

SEED = "12345678901234567890"


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def test_sample_sizes_follow_corla() -> None:
    # The 2026 primary's statewide contest: min_margin 50376 over 1444047
    # ballot cards at a 3% risk limit; CORLA's optimistic size 209, and its
    # round-one audited_sample_count 210.
    assert optimistic_sample_size("0.03", 50376 / 1444047) == 209
    assert round_one_sample_count("0.03", 50376, 1444047) == 210
    # State Representative - District 19 - DEM (Weld): 1661 over 69640, 307 draws.
    assert round_one_sample_count("0.03000000", 1661, 69640) == 307
    with pytest.raises(ValueError):
        round_one_sample_count("0.03", 0, 100)
    with pytest.raises(ValueError):
        optimistic_sample_size("1.5", 0.1)


def manifests(*data: bytes, tmp: Path) -> list[list[ManifestBatch]]:
    result = []
    for i, content in enumerate(data):
        path = tmp / f"m{i}.csv"
        path.write_bytes(content)
        result.append(load_county_manifest(path))
    return result


def test_predict_from_corla_contest_list(ws: Workspace, log: EventLog, tmp_path: Path) -> None:
    files = election_files(
        finalReports_ContestsListRound1_csv=contests_list(), finalReports_ContestIDsByCounty_csv=CONTEST_IDS
    )
    fetch(ws, log, files)
    enter_and_confirm(log, SEED, ACTOR, "test")
    prediction = predict(build_map(log, ws))
    statewide, adams = prediction.contests
    assert (statewide.domain_size, statewide.sample_count, statewide.estimated) == (165, 9, False)
    adams_m, boulder_m, weld_m = manifests(ADAMS_MANIFEST, BOULDER_MANIFEST, WELD_MANIFEST, tmp=tmp_path)
    # Combined in county-ID order (Adams 1, Boulder 7, Weld 62).
    domain = build_domain_from_manifests([weld_m, adams_m, boulder_m], {"adams": 1, "boulder": 7, "weld": 62})
    assert [d.imprinted_id for d in statewide.draws] == [d.imprinted_id for d in select_ballots(SEED, domain, 9)]
    assert sum(len(statewide.draws_for(c)) for c in ("Adams", "Boulder", "Weld")) == 9
    assert adams.draws == select_ballots(SEED, build_domain_from_manifests([adams_m]), 4)
    assert [c.name for c in predict(build_map(log, ws), counties=["Boulder"]).contests] == ["Governor - DEM"]
    assert [c.name for c in predict(build_map(log, ws), contests=["Adams County Clerk - REP"]).contests] == [
        "Adams County Clerk - REP"
    ]
    with pytest.raises(PredictionError, match="no such targeted contest"):
        predict(build_map(log, ws), contests=["Nope"])


def test_predict_estimates_counts_from_the_target_list(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    with pytest.raises(PredictionError, match="no seed"):
        predict(build_map(log, ws))
    prediction = predict(build_map(log, ws), seed=SEED)
    statewide = prediction.contests[0]
    assert statewide.estimated and "ESTIMATED" in statewide.count_source
    assert statewide.sample_count == round_one_sample_count("0.03", 30, 165)


def test_predict_applies_manifest_adjustments(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    enter_and_confirm(log, SEED, ACTOR, "test")
    write_overrides(
        ws,
        '[[manifest_adjustment]]\ncounty = "Weld"\nappend = [{ tabulator = "7", batch = "4", count = 11 }]\n'
        'reason = "missing batch"\n',
    )
    assert apply_overrides(ws, log, ACTOR).applied
    statewide = predict(build_map(log, ws)).contests[0]
    assert statewide.domain_size == 176 and statewide.adjustments == ["Weld: missing batch"]


def test_predict_needs_every_county_manifest(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(files_Adams_csv=None))
    enter_and_confirm(log, SEED, ACTOR, "test")
    with pytest.raises(PredictionError, match="no manifest for Adams"):
        predict(build_map(log, ws), contests=["Adams County Clerk - Republican Party (Adams)"])


def test_record_and_compare(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files(
        finalReports_ContestsListRound1_csv=contests_list(), finalReports_ContestIDsByCounty_csv=CONTEST_IDS
    ))  # fmt: skip
    enter_and_confirm(log, SEED, ACTOR, "test")
    prediction = predict(build_map(log, ws))
    seq, sha = record_prediction(ws, log, prediction, ACTOR)
    latest = latest_prediction(ws, log)
    assert latest is not None and latest[0] == seq and latest[1] == json.loads(prediction.to_json())
    event = list(log.iter_events())[-1]
    assert event.kind == "prediction.made" and event.file_sha256 == sha
    statewide_ids = [d.imprinted_id for d in prediction.contests[0].draws]
    adams_ids = [d.imprinted_id for d in prediction.contests[1].draws]
    # CORLA's published order is not draw order: compared as multisets.
    published = {"Governor - DEM": list(reversed(statewide_ids)), "Adams County Clerk - REP": adams_ids[:3] + ["101-9-9"]}
    results = compare(latest[1], published)
    assert [(r.status, r.compared) for r in results] == [("match", 9), ("mismatch", 4)]
    assert results[1].missing == ("101-9-9",) and results[1].extra == (adams_ids[3],)


def test_compare_short_and_long_predictions() -> None:
    def prediction(name: str, ids: list[str], county: str = "Weld") -> dict[str, object]:
        return {"contests": [{"name": name, "counties": [county], "statewide": False, "draws": [{"imprinted_id": i} for i in ids]}]}

    corla = {"Attorney General - REP (Weld)": ["1-1-1", "1-1-2", "1-1-3"]}
    short = compare(prediction("Attorney General - Republican Party (Weld)", ["1-1-2", "1-1-1"]), corla)[0]  # type: ignore[arg-type]
    assert (short.status, short.corla_name, short.compared) == ("consistent", "Attorney General - REP (Weld)", 2)
    long = compare(prediction("Attorney General - REP (Weld)", ["1-1-3", "1-1-1", "1-1-2", "9-9-9"]), corla)[0]  # type: ignore[arg-type]
    assert (long.status, long.compared) == ("match", 3)
    wrong = compare(prediction("Attorney General - REP (Weld)", ["1-1-3", "8-8-8"]), corla)[0]  # type: ignore[arg-type]
    assert wrong.status == "mismatch" and wrong.extra == ("8-8-8",)
    unpublished = compare(prediction("Coroner", ["1-1-1"]), corla)[0]  # type: ignore[arg-type]
    assert unpublished.status == "not_published"


@pytest.mark.parametrize(
    ("name", "county", "expected"),
    [
        ("Attorney General - Republican Party (Weld)", "Weld", "Attorney General - REP (Weld)"),
        ("Alamosa County Assessor - Democratic Party (Alamosa)", "Alamosa", "Alamosa County Assessor - DEM"),
        ("United States Senator - Democratic Party", None, "United States Senator - DEM"),
        ("Governor - DEM", None, "Governor - DEM"),
        ("Mayor", "Weld", None),
    ],
)
def test_corla_contest_name(name: str, county: str | None, expected: str | None) -> None:
    published = ["Attorney General - REP (Weld)", "Alamosa County Assessor - DEM", "United States Senator - DEM", "Governor - DEM"]
    assert corla_contest_name(name, county, published) == expected


def test_corla_selections_from_published_files(ws: Workspace, log: EventLog) -> None:
    fetch(ws, log, election_files())
    assert corla_selections(build_map(log, ws), ws)[0] == {}
    fetch(ws, log, election_files(**{
        k.replace("/", "_").replace(".", "_"): v
        for k, v in published_selections({"Governor - DEM": ["101-1-3", "101-1-3", "1-2-4"]}).items()
    }))  # fmt: skip
    selections, notes = corla_selections(build_map(log, ws), ws)
    assert selections == {"Governor - DEM": ["101-1-3", "101-1-3", "1-2-4"]}
    assert "ContestMarginAndContestCVR-IDs-Round1.csv" in notes[0]


def test_uploaded_manifest_feeds_the_prediction(ws: Workspace, log: EventLog) -> None:
    from corla_observer.uploads import receive

    fetch(ws, log, election_files(files_Weld_csv=None))
    receive(ws, log, "Weld", "manifest", io.BytesIO(WELD_MANIFEST), "Weld.csv", ACTOR)
    enter_and_confirm(log, SEED, ACTOR, "test")
    statewide = predict(build_map(log, ws)).contests[0]
    assert statewide.counties == ["Adams", "Boulder", "Weld"] and statewide.domain_size == 165
