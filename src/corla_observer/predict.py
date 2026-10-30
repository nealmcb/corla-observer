"""Predict CORLA's round-one selections from an election's data map, and compare them with CORLA's.

`predict` takes a `corla_observer.datamap.DataMap` and returns, for each
targeted contest, its sampling domain (the chosen manifests of its counties,
combined in county-ID order, with the map's manifest adjustments) and its
first *N* draws (`corla_observer.sampling.select_ballots`), where *N* is the
contest's round-one sample count. CORLA's own order for the county's pull
list (its database order and board assignment) is not reproduced: the draws
are in draw order.

**Sample counts.** CORLA's contest list (``audited_sample_count``) gives
them, but it is published only once the audit has started. Before that,
`round_one_sample_count` estimates them from the target contest list's
margin and the domain size, as CORLA computes them: the optimistic sample
size for a comparison audit with no discrepancies, ``ceil(2 * gamma *
ln(1 / risk limit) / diluted margin)`` with CORLA's gamma of 1.03905, plus
one. The plus one is what every round-one contest list surveyed shows (2019
to 2026: ``audited_sample_count`` is ``optimistic_samples_to_audit`` + 1).
The target list's margins come from unofficial results, so an estimate can
be off; but CORLA's draws are a fixed sequence, so the first draws predicted
are right whatever the final count, and `compare` checks a short prediction
as a prefix.

`corla_selections` reads CORLA's published selections from the map (the
selection list's ``contest_cvr_ids`` and the CVR comparison file's imprinted
IDs), and `compare` compares each predicted contest with them, as multisets
since CORLA's published order is not draw order.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from corla_observer.classify import Classification, canonical_county
from corla_observer.datamap import (
    ContestSpec,
    DataMap,
    MapEntry,
    corla_contest_name_for,
    role_column,
    table_rows,
)
from corla_observer.events import EventLog, JSONValue, canonical_json, check_actor
from corla_observer.sampling import Draw, build_domain_from_manifests, county_key, select_ballots
from corla_observer.workspace import ObjectNotFoundError, Workspace

if TYPE_CHECKING:
    from corla_observer.estimate import ContestEstimate

#: CORLA's error inflation factor, gamma.
CORLA_GAMMA = Decimal("1.03905")
#: Draws CORLA's round one makes beyond the optimistic sample size.
ROUND_ONE_EXTRA_DRAWS = 1


class PredictionError(ValueError):
    """A prediction cannot be made: the seed, a manifest or a sample count is missing."""


def optimistic_sample_size(risk_limit: Decimal | str | float, diluted_margin: Decimal | float, gamma: Decimal = CORLA_GAMMA) -> int:
    """CORLA's optimistic sample size with no discrepancies: ``ceil(2 gamma ln(1/alpha) / margin)``."""
    alpha = float(risk_limit)
    margin = float(diluted_margin)
    if not 0 < alpha < 1 or margin <= 0:
        raise ValueError(f"need 0 < risk limit < 1 and a positive margin, got {risk_limit}, {diluted_margin}")
    return math.ceil(2 * float(gamma) * math.log(1 / alpha) / margin)


def round_one_sample_count(risk_limit: Decimal | str | float, margin: int, ballot_cards: int) -> int:
    """CORLA's round-one draws for a contest: the optimistic size for ``margin / ballot_cards``, plus one."""
    if ballot_cards <= 0 or margin <= 0:
        raise ValueError(f"need a positive margin and domain size, got {margin}, {ballot_cards}")
    return optimistic_sample_size(risk_limit, Decimal(margin) / Decimal(ballot_cards)) + ROUND_ONE_EXTRA_DRAWS


@dataclass
class ContestPrediction:
    """One contest's predicted draws."""

    name: str
    counties: list[str]
    statewide: bool
    domain_size: int
    sample_count: int
    count_source: str
    estimated: bool
    draws: list[Draw]
    adjustments: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    #: A conservatively low count: every draw up to it will be made if CORLA's
    #: count is at least this (equal to ``sample_count`` once CORLA's is known).
    low_count: int | None = None

    def draws_for(self, county: str) -> list[Draw]:
        """The draws that fall in ``county``, in draw order."""
        key = county_key(county)
        return [d for d in self.draws if county_key(d.county) == key]

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "name": self.name,
            "counties": list(self.counties),
            "statewide": self.statewide,
            "domain_size": self.domain_size,
            "sample_count": self.sample_count,
            "count_source": self.count_source,
            "estimated": self.estimated,
            "low_count": self.low_count if self.low_count is not None else self.sample_count,
            "adjustments": list(self.adjustments),
            "flags": list(self.flags),
            "draws": [
                {"index": d.index, "position": d.position, "county": d.county, "imprinted_id": d.imprinted_id}
                for d in self.draws
            ],
        }


@dataclass
class Prediction:
    """A prediction for some or all of an election's targeted contests."""

    election: str | None
    seed: str
    seed_source: str
    county_order_source: str
    overrides_sha256: str | None
    contests: list[ContestPrediction]

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "election": self.election,
            "seed": self.seed,
            "seed_source": self.seed_source,
            "county_order_source": self.county_order_source,
            "overrides_sha256": self.overrides_sha256,
            "contests": [c.to_dict() for c in self.contests],
        }

    def to_json(self) -> bytes:
        """The canonical JSON of the prediction, as stored and hashed."""
        return canonical_json(self.to_dict())


def predict(
    datamap: DataMap,
    *,
    counties: Iterable[str] = (),
    contests: Iterable[str] = (),
    seed: str | None = None,
    estimates: Mapping[str, ContestEstimate] | None = None,
) -> Prediction:
    """Predict the draws for the map's targeted contests.

    With ``counties``, only the statewide contests and those entirely in
    those counties (`DataMap.contests_for`); with ``contests``, only those
    named. ``seed`` overrides the map's seed. ``estimates``
    (`corla_observer.estimate`, by contest name) give the central and low
    counts of contests whose count CORLA has not published; without them,
    each is estimated from the data map alone (the target list's margin).

    Raises:
        PredictionError: if there is no seed, or a chosen contest lacks a
            manifest, a county ID or a sample count.
    """
    chosen_seed = seed or (datamap.seed.value if datamap.seed else None)
    if not chosen_seed:
        raise PredictionError("no seed: enter it, or add a [seed] override")
    wanted_counties = list(counties)
    specs = datamap.contests_for(wanted_counties) if wanted_counties else list(datamap.contests)
    names = set(contests)
    if names:
        unknown = sorted(names - {s.name for s in specs})
        if unknown:
            raise PredictionError(f"no such targeted contest: {unknown}")
        specs = [s for s in specs if s.name in names]
    if not specs:
        raise PredictionError("no targeted contests to predict")
    order = {county_key(c): n for c, n in datamap.county_order.value.items()}
    results = [_predict_one(datamap, spec, chosen_seed, order, (estimates or {}).get(spec.name)) for spec in specs]
    return Prediction(
        election=datamap.election,
        seed=chosen_seed,
        seed_source="given" if seed else (datamap.seed.source if datamap.seed else ""),
        county_order_source=datamap.county_order.source,
        overrides_sha256=datamap.overrides_sha256,
        contests=results,
    )


def _predict_one(
    datamap: DataMap, spec: ContestSpec, seed: str, order: Mapping[str, int], estimate: ContestEstimate | None
) -> ContestPrediction:
    missing = [c for c in spec.counties if c not in datamap.manifests]
    if missing:
        raise PredictionError(f"{spec.name}: no manifest for {', '.join(missing)}")
    manifests = [datamap.manifests[c].batches for c in spec.counties]
    multi = len(spec.counties) > 1
    if multi:
        unordered = [c for c in spec.counties if county_key(c) not in order]
        if unordered:
            raise PredictionError(f"{spec.name}: no county ID for {', '.join(unordered)}")
    domain = build_domain_from_manifests(manifests, order if multi else None, datamap.adjustments)
    estimated = spec.sample_count is None
    low: int | None
    if spec.sample_count is not None:
        count = low = spec.sample_count
        source = spec.count_source
    else:
        if estimate is None:
            from corla_observer.estimate import estimate_contest

            estimate = estimate_contest(spec, datamap)
        if estimate.central is None or estimate.low is None:
            raise PredictionError(
                f"{spec.name}: no sample count, and no margin and denominator to estimate one ({estimate.margin_source}; {estimate.denominator_source})"
            )
        count, low = estimate.central, estimate.low
        source = (
            f"ESTIMATED: central {count}, low {low}, from the margin {estimate.margin} ({estimate.margin_source}) "
            f"over {estimate.denominator} ballot cards ({estimate.denominator_source}"
            + (f"; {estimate.cards_note}" if estimate.cards_note else "")
            + f"), risk limit {estimate.risk_limit}"
        )
    return ContestPrediction(
        name=spec.name,
        counties=list(spec.counties),
        statewide=spec.statewide,
        domain_size=domain.size,
        sample_count=count,
        count_source=source,
        estimated=estimated,
        draws=select_ballots(seed, domain, count),
        adjustments=[f"{a.county}: {a.reason}" for a in domain.applied_adjustments],
        flags=list(domain.epistemic_flags),
        low_count=low,
    )


def record_prediction(workspace: Workspace, log: EventLog, prediction: Prediction, actor: str) -> tuple[int, str]:
    """Store the prediction's canonical JSON and record a ``prediction.made`` event; return its seq and SHA-256.

    The event carries, per contest, the sample count and whether it was
    estimated, the domain size and the SHA-256 of the list of drawn imprinted
    IDs, so a later run can be compared without the stored file.
    """
    check_actor(actor)
    body = prediction.to_json()
    stored = workspace.put_bytes(body)
    summary: list[JSONValue] = [
        {
            "name": c.name,
            "sample_count": c.sample_count,
            "estimated": c.estimated,
            "low_count": c.low_count if c.low_count is not None else c.sample_count,
            "domain_size": c.domain_size,
            "draws_sha256": hashlib.sha256("\n".join(d.imprinted_id for d in c.draws).encode("utf-8")).hexdigest(),
        }
        for c in prediction.contests
    ]
    event = log.append(
        "prediction.made",
        actor,
        data={
            "sha256": stored.sha256,
            "size": stored.size,
            "seed": prediction.seed,
            "overrides_sha256": prediction.overrides_sha256,
            "contests": summary,
        },
        file_sha256=stored.sha256,
    )
    return event.seq, stored.sha256


def latest_prediction(workspace: Workspace, log: EventLog) -> tuple[int, dict[str, JSONValue]] | None:
    """The most recent recorded prediction's seq and content, or None."""
    latest: tuple[int, str] | None = None
    for event in log.iter_events():
        if event.kind == "prediction.made" and event.file_sha256:
            latest = (event.seq, event.file_sha256)
    if latest is None:
        return None
    data = json.loads(workspace.get_bytes(latest[1]))
    return latest[0], data


# ---------------------------------------------------------------------------
# CORLA's published selections


def _round_one_first(entries: Sequence[MapEntry]) -> list[MapEntry]:
    def rank(entry: MapEntry) -> tuple[int, int]:
        text = entry.ref.lower()
        return (0 if re.search(r"round_?1(\D|$)", text) else 1, -(entry.seq or 0))

    return sorted(entries, key=rank)


@dataclass
class PublishedSelections:
    """CORLA's published round-one selections, read from its selection list and CVR comparison file.

    ``by_contest``: each contest's selected imprinted IDs, in CORLA's
    published order (not draw order). ``by_county``: per county, each
    contest's imprinted IDs in that county (an imprinted ID names a ballot
    only within its county). ``obtained``: when the later of the two files
    was obtained (UTC), or None.
    """

    by_contest: dict[str, list[str]] = field(default_factory=dict)
    by_county: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    obtained: str | None = None
    refs: tuple[str, ...] = ()


def published_selections(datamap: DataMap, workspace: Workspace) -> PublishedSelections:
    """CORLA's published round-one selections; empty until both files have been fetched."""
    found = PublishedSelections()
    notes = found.notes
    selections = _round_one_first(datamap.used("selection_list"))
    comparisons = _round_one_first(datamap.used("cvr_comparison"))
    if not selections or not comparisons:
        notes.append("CORLA's selection list and CVR comparison file have not been fetched")
        return found
    selection, comparison = selections[0], comparisons[0]
    notes.append(f"selections from {selection.ref}; imprinted IDs from {comparison.ref}")
    try:
        sel_data = workspace.get_bytes(selection.sha256)
        cmp_data = workspace.get_bytes(comparison.sha256)
    except ObjectNotFoundError as exc:
        notes.append(str(exc))
        return found
    sel_class = Classification(selection.classified_kind, None, selection.evidence)
    _, rows = table_rows(sel_data, sel_class)
    contest_col = role_column(sel_class, "contest") or "contest_name"
    ids_col = role_column(sel_class, "cvr_ids") or "contest_cvr_ids"
    cvr_ids: dict[str, list[str]] = {}
    for row in rows:
        try:
            ids = json.loads(row.get(ids_col) or "[]")
        except json.JSONDecodeError:
            notes.append(f"unreadable CVR ID list for {row.get(contest_col)}")
            continue
        if ids:
            cvr_ids[row.get(contest_col, "")] = [str(i) for i in ids]
    cmp_class = Classification(comparison.classified_kind, None, comparison.evidence)
    _, cmp_rows = table_rows(cmp_data, cmp_class)
    id_col = role_column(cmp_class, "cvr_id") or "cvr_id"
    imprinted_col = role_column(cmp_class, "imprinted_id") or "imprinted_id"
    county_col = role_column(cmp_class, "county") or "county_name"
    imprinted = {row.get(id_col, ""): (row.get(county_col, "").strip(), row.get(imprinted_col, "").strip()) for row in cmp_rows}
    for contest, ids in cvr_ids.items():
        missing = [i for i in ids if i not in imprinted]
        if missing:
            notes.append(f"{contest}: {len(missing)} selected CVR IDs not in the comparison file")
        found.by_contest[contest] = [imprinted[i][1] for i in ids if i in imprinted]
        for i in ids:
            if i in imprinted:
                county = canonical_county(imprinted[i][0]) or imprinted[i][0]
                found.by_county.setdefault(county, {}).setdefault(contest, []).append(imprinted[i][1])
    times = [e.obtained for e in (selection, comparison) if e.obtained]
    found.obtained = max(times) if len(times) == 2 else None
    found.refs = (selection.ref, comparison.ref)
    return found


def corla_selections(datamap: DataMap, workspace: Workspace) -> tuple[dict[str, list[str]], list[str]]:
    """CORLA's published round-one selections: ``{contest name: [imprinted IDs]}``, and notes.

    Read from the selection list (each contest's ``contest_cvr_ids``) and the
    CVR comparison file (each CVR ID's imprinted ID). Returns ``({}, notes)``
    when either is missing, which is the case until CORLA's reports are
    published. See `published_selections` for the same by county.
    """
    found = published_selections(datamap, workspace)
    return found.by_contest, found.notes


def corla_contest_name(name: str, county: str | None, published: Iterable[str]) -> str | None:
    """CORLA's name for a contest named as in the target list; see `corla_observer.datamap.corla_contest_name_for`."""
    return corla_contest_name_for(name, county, published)


@dataclass(frozen=True)
class ContestComparison:
    """How one predicted contest compares with CORLA's published selection.

    ``status`` is ``match`` (the same draws, as a multiset), ``consistent``
    (an estimated count smaller than CORLA's, whose draws are all among
    CORLA's), ``mismatch``, or ``not_published``.
    """

    name: str
    corla_name: str | None
    status: str
    predicted: int
    published: int
    compared: int
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    note: str = ""

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "name": self.name,
            "corla_name": self.corla_name,
            "status": self.status,
            "predicted": self.predicted,
            "published": self.published,
            "compared": self.compared,
            "missing": list(self.missing[:50]),
            "extra": list(self.extra[:50]),
            "note": self.note,
        }


def compare(prediction: Mapping[str, JSONValue], published: Mapping[str, Sequence[str]]) -> list[ContestComparison]:
    """Compare a recorded prediction (`Prediction.to_dict`) with CORLA's published selections.

    With the same number of draws, the multisets must be equal. With more
    predicted draws than CORLA made, the first CORLA-many predicted draws are
    compared. With fewer (an estimated count that came out low), every
    predicted draw must be among CORLA's, which is all that can be checked.
    """
    results = []
    contests = prediction.get("contests")
    for contest in contests if isinstance(contests, list) else []:
        if not isinstance(contest, dict):
            continue
        name = str(contest.get("name"))
        counties = contest.get("counties")
        county = counties[0] if isinstance(counties, list) and len(counties) == 1 and not contest.get("statewide") else None
        draws = contest.get("draws")
        ours = [str(d.get("imprinted_id")) for d in draws if isinstance(d, dict)] if isinstance(draws, list) else []
        corla_name = corla_contest_name(name, str(county) if county else None, published)
        if corla_name is None:
            results.append(ContestComparison(name, None, "not_published", len(ours), 0, 0, note="no published selection with this name"))
            continue
        theirs = list(published[corla_name])
        if len(ours) >= len(theirs):
            head = Counter(ours[: len(theirs)])
            want = Counter(theirs)
            compared = len(theirs)
            status = "match" if head == want else "mismatch"
            note = "" if len(ours) == len(theirs) else f"compared the first {compared} of {len(ours)} predicted draws"
        else:
            head = Counter(ours)
            want = Counter(theirs)
            compared = len(ours)
            status = "consistent" if not (head - want) else "mismatch"
            note = f"predicted {len(ours)} draws; CORLA made {len(theirs)}: the rest were not predicted"
        missing = tuple(sorted((want - head).elements())) if status == "mismatch" or len(ours) >= len(theirs) else ()
        extra = tuple(sorted((head - want).elements()))
        results.append(ContestComparison(name, corla_name, status, len(ours), len(theirs), compared, missing, extra, note))
    return results


def record_comparison(log: EventLog, prediction_seq: int, results: Sequence[ContestComparison], notes: Sequence[str], actor: str) -> int:
    """Record a ``prediction.compared`` event; return its seq."""
    check_actor(actor)
    summary: list[JSONValue] = [
        {k: v for k, v in r.to_dict().items() if k not in ("missing", "extra")}
        | {"missing": list(r.missing[:10]), "extra": list(r.extra[:10])}
        for r in results
    ]
    return log.append(
        "prediction.compared",
        actor,
        data={"prediction_seq": prediction_seq, "results": summary, "notes": [n[:300] for n in notes[:20]]},
    ).seq
