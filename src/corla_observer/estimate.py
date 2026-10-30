"""Estimates of each targeted contest's round-one sample count, before CORLA publishes it.

CORLA's round-one draws for a targeted contest are its optimistic sample
size plus one (`corla_observer.predict.round_one_sample_count`): ``ceil(2
* 1.03905 * ln(1 / risk limit) / diluted margin) + 1``, the diluted margin
being the contest's minimum margin over its domain's ballot cards (the
manifest totals of its counties, not only the cards with the contest on
them). CORLA's count is published only with its contest list, after the
roll; until then `estimate_contests` estimates it from the sources below,
each ranked: audit center data first (CORLA's contest list, then what is
derived from CORLA's files, then the target list), then the election
night results, and inputs supplied by hand last, so that a later posting
replaces a hand input by itself (the estimate's notes say when it did).

**The margin**, the first of:

1. CORLA's contest list (its ``min_margin``), once posted;
2. the latest election night results (`corla_observer.enr`): the state's
   results, summed over the contest's counties (for a statewide contest,
   every county but San Juan, which is not in CORLA's audit), or a
   county's own results for its own contests;
3. the target contest list's margin (unofficial results of its date);
4. a margin supplied by hand, in the overrides file's ``[[estimate]]``
   (`corla_observer.datamap`: ``margin``, or ``winner_votes`` and
   ``loser_votes``, with a reason, applied and logged like every
   override), an estimate only. Its ``enr_contest`` names the contest in
   the results when the names don't match. (Margins entered in earlier
   versions, ``results.margin_entered`` events, are read the same way.)

**The denominator** (the domain's ballot cards), the first of:

1. CORLA's contest list (its ``ballot_card_count``), once posted;
2. the manifest totals of the contest's counties, once every one is in the
   data map (with its manifest adjustments): exact;
3. per county, its manifest total if it has one, else its ballots cast in
   the latest results (or, without them, supplied by hand in a
   ``[[county_estimate]]``) times its ballot cards per ballot, or a ballot
   card total supplied by hand there. Cards per ballot come from the target
   list's remarks ("2 ballot cards per ballot*", which the target list's
   own diluted margins use), then a ``[[county_estimate]]``, then the
   election setup's ``[cards_per_ballot]`` (`corla_observer.election`);
   the output says which;
4. the target list's own denominator (its margin over its diluted margin);
5. a domain total supplied by hand (``ballot_cards`` in ``[[estimate]]``).

Each estimate has a **central** count, from those, and a **low** count, from
a margin wider by the setup's ``low_margin_factor_preliminary`` (or
``_final``, once results count as final, or 1 for a coordinator's entry
marked final) and, before the manifests, ``low_cards_per_ballot`` (1)
cards per ballot. CORLA's draws are a fixed sequence, so every ballot among
the first *low* draws will be drawn whatever the final count, provided the
real count is at least the low one; the draws between the low and the
central count are only possible (`corla_observer.extract`). Once CORLA's
contest list gives a count, it is used for both, and the estimate made
from the results is kept beside it for comparison.

`record_estimates` records an ``estimate.made`` event whenever any
contest's estimate or its inputs changed since the last one, with every
input's source; `estimate_history` lists how each contest's estimate
changed.

Events:

``estimate.made``
    ``contests``: per contest, ``name``, ``counties``, ``statewide``,
    ``risk_limit``, ``margin``, ``margin_source``, ``denominator``,
    ``denominator_source``, ``cards_note``, ``low_margin_factor``,
    ``low_denominator``, ``central``, ``low``, ``corla_count``,
    ``from_results`` (the central count from the margin and denominator,
    even when CORLA's count is known), ``notes``; ``inputs``: the results
    files' and the setup's SHA-256 and the seqs of earlier versions'
    margin entries (the inputs supplied by hand in the overrides are in
    each contest's sources).

Standard library only.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from corla_observer.county_status import NOT_IN_CORLA
from corla_observer.classify import COLORADO_COUNTIES, canonical_county
from corla_observer.datamap import ContestSpec, DataMap, corla_contest_name_for
from corla_observer.election import ElectionSetup, EstimatesSetup
from corla_observer.enr import ContestResult, FetchedResults, Results, contest_votes, find_contest, margin_of
from corla_observer.events import EventLog, JSONValue, canonical_json, check_actor
from corla_observer.localtime import colorado_time
from corla_observer.predict import round_one_sample_count
from corla_observer.sampling import build_domain_from_manifests, county_key

DEFAULT_RISK_LIMIT = "0.03"


class EstimateError(ValueError):
    """An estimate input is not acceptable."""


@dataclass(frozen=True)
class MarginEntry:
    """A coordinator's margin for a contest, or the name of the contest in the results."""

    seq: int
    contest: str
    margin: int | None
    enr_contest: str | None
    final: bool
    source: str
    actor: str
    time: str


def margin_entries(log: EventLog) -> tuple[dict[str, MarginEntry], dict[str, MarginEntry]]:
    """The margins and result-name mappings entered in earlier versions (``results.margin_entered``), by contest.

    A ``clear`` removes both. They rank as inputs supplied by hand, after
    the overrides file's ``[[estimate]]``.
    """
    margins: dict[str, MarginEntry] = {}
    names: dict[str, MarginEntry] = {}
    for event in log.iter_events():
        if event.kind != "results.margin_entered":
            continue
        d = event.data
        contest = str(d.get("contest"))
        if d.get("clear"):
            margins.pop(contest, None)
            names.pop(contest, None)
            continue
        margin = d.get("margin")
        enr = d.get("enr_contest")
        entry = MarginEntry(
            event.seq,
            contest,
            margin if isinstance(margin, int) else None,
            str(enr) if isinstance(enr, str) else None,
            bool(d.get("final")),
            str(d.get("source") or ""),
            event.actor,
            event.server_time,
        )
        if entry.margin is not None:
            margins[contest] = entry
        elif entry.enr_contest is not None:
            names[contest] = entry
    return margins, names


@dataclass(frozen=True)
class HandInputs:
    """The estimate inputs supplied by hand: per contest (``[[estimate]]``) and per county (``[[county_estimate]]``)."""

    margins: Mapping[str, MarginEntry] = field(default_factory=dict)
    names: Mapping[str, MarginEntry] = field(default_factory=dict)
    #: Per contest: (ballot cards, source).
    ballot_cards: Mapping[str, tuple[int, str]] = field(default_factory=dict)
    #: Per county: {"ballots_cast" | "cards_per_ballot" | "ballot_cards": (value, source)}.
    counties: Mapping[str, Mapping[str, tuple[float, str]]] = field(default_factory=dict)

    def for_contest(self, table: Mapping[str, Any], spec: ContestSpec) -> Any:
        """The entry for ``spec`` in ``table``: by its name, or by the name CORLA's list gives the same contest."""
        if spec.name in table:
            return table[spec.name]
        county = spec.counties[0] if len(spec.counties) == 1 and not spec.statewide else None
        for name, value in table.items():
            if corla_contest_name_for(name, county, [spec.name]) == spec.name:
                return value
        return None


def hand_inputs(datamap: DataMap, log: EventLog | None = None) -> HandInputs:
    """The inputs supplied by hand: the data map's applied ``[[estimate]]`` and ``[[county_estimate]]``, then earlier versions' entries."""
    legacy_margins, legacy_names = margin_entries(log) if log is not None else ({}, {})
    margins: dict[str, MarginEntry] = {}
    names: dict[str, MarginEntry] = {}
    cards: dict[str, tuple[int, str]] = {}
    counties: dict[str, dict[str, tuple[float, str]]] = {}
    time = ""
    for item in datamap.estimate_items:
        v = item.values
        source = f"supplied by hand in {item.label}: {item.reason}"
        if item.section == "estimate":
            contest = str(v["contest"])
            margin = v.get("margin")
            if margin is None and "winner_votes" in v:
                margin = int(v["winner_votes"]) - int(v["loser_votes"])
            if margin is not None:
                margins[contest] = MarginEntry(datamap.overrides_seq or 0, contest, int(margin), None, bool(v.get("final")), source, "overrides", time)
            if "enr_contest" in v:
                names[contest] = MarginEntry(datamap.overrides_seq or 0, contest, None, str(v["enr_contest"]), False, source, "overrides", time)
            if "ballot_cards" in v:
                cards[contest] = (int(v["ballot_cards"]), source)
        else:
            county = canonical_county(str(v["county"])) or str(v["county"])
            for key in ("ballots_cast", "cards_per_ballot", "ballot_cards"):
                if key in v:
                    counties.setdefault(county, {})[key] = (float(v[key]), source)
    for table, legacy in ((margins, legacy_margins), (names, legacy_names)):
        for contest, entry in legacy.items():
            table.setdefault(contest, entry)
    return HandInputs(margins, names, cards, counties)


@dataclass
class ContestEstimate:
    """One contest's estimated round-one count, with every input and its source."""

    name: str
    counties: list[str]
    statewide: bool
    risk_limit: str
    margin: int | None
    margin_source: str
    denominator: int | None
    denominator_source: str
    cards_note: str
    low_margin_factor: float
    low_denominator: int | None
    central: int | None
    low: int | None
    corla_count: int | None
    from_results: int | None
    notes: list[str] = field(default_factory=list)

    @property
    def exact(self) -> bool:
        """Whether the count is CORLA's own."""
        return self.corla_count is not None

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "name": self.name,
            "counties": list(self.counties),
            "statewide": self.statewide,
            "risk_limit": self.risk_limit,
            "margin": self.margin,
            "margin_source": self.margin_source,
            "denominator": self.denominator,
            "denominator_source": self.denominator_source,
            "cards_note": self.cards_note,
            "low_margin_factor": str(self.low_margin_factor),
            "low_denominator": self.low_denominator,
            "central": self.central,
            "low": self.low,
            "corla_count": self.corla_count,
            "from_results": self.from_results,
            "notes": list(self.notes),
        }


def _corla_counties() -> list[str]:
    return [c for c in COLORADO_COUNTIES if c not in NOT_IN_CORLA]


def _is_corla_count(spec: ContestSpec) -> bool:
    return spec.sample_count is not None and spec.source.startswith("contests_list")


def _contest_counties(spec: ContestSpec, results: Mapping[str, tuple[Results, FetchedResults]]) -> tuple[list[str], str | None]:
    """The counties to sum results over: the spec's, or for a statewide contest every county in CORLA's audit.

    Before every manifest is in, a statewide contest's counties are those
    in the state's results (every county), but San Juan, which is not in
    CORLA's audit.
    """
    if spec.statewide and len(spec.counties) < len(_corla_counties()):
        listed = list(results["state"][0].ballots_cast) if "state" in results else _corla_counties()
        return (
            [c for c in listed if c not in NOT_IN_CORLA],
            "statewide: every county in the results but San Juan (not in CORLA's audit), until the manifests are in",
        )
    return list(spec.counties), None


def excluded_votes_notes(contest: ContestResult, kept: Mapping[str, int]) -> list[str]:
    """A note for each county not in CORLA's audit with votes in ``contest`` (by county), saying what leaving them out does to the margin."""
    out = []
    for area, per in sorted(contest.by_area.items()):
        if area not in NOT_IN_CORLA or not any(per.values()):
            continue
        with_them = dict(kept)
        for choice, n in per.items():
            with_them[choice] = with_them.get(choice, 0) + n
        listed = ", ".join(f"{choice} {n:,}" for choice, n in sorted(per.items(), key=lambda kv: -kv[1]))
        out.append(
            f"left out: {area}'s votes ({listed}), not in CORLA's audit; with them the margin would be "
            f"{margin_of(with_them, contest.vote_for):,}, not {margin_of(kept, contest.vote_for):,}"
        )
    return out


def _hand_margin(entry: MarginEntry) -> str:
    when = f" at {colorado_time(entry.time, seconds=False)}" if entry.time else ""
    return entry.source if entry.actor == "overrides" else f"entered by {entry.actor}{when}: {entry.source}"


def _margin(
    spec: ContestSpec,
    counties: Sequence[str],
    results: Mapping[str, tuple[Results, FetchedResults]],
    hand: HandInputs,
    setup: EstimatesSetup,
    final_after: str | None,
    notes: list[str],
) -> tuple[int | None, str, float]:
    entry: MarginEntry | None = hand.for_contest(hand.margins, spec)
    found = _published_margin(spec, counties, results, hand, setup, final_after, notes)
    if found is not None:
        if entry is not None and entry.margin is not None:
            notes.append(f"the margin supplied by hand ({entry.margin}, {_hand_margin(entry)}) is not used: {found[1]} ranks above it")
        return found
    if entry is not None and entry.margin is not None:
        factor = 1.0 if entry.final else setup.low_margin_factor_preliminary
        return entry.margin, f"{_hand_margin(entry)} (ESTIMATED: no published margin yet)", factor
    return None, "no margin yet", setup.low_margin_factor_preliminary


def _published_margin(
    spec: ContestSpec,
    counties: Sequence[str],
    results: Mapping[str, tuple[Results, FetchedResults]],
    hand: HandInputs,
    setup: EstimatesSetup,
    final_after: str | None,
    notes: list[str] | None = None,
) -> tuple[int, str, float] | None:
    """The margin from CORLA's contest list, the results or the target list, in that order, or None.

    From the state's results, the margin is over the contest's counties in
    CORLA's audit only: a multi-county or statewide contest's votes in a
    county not in it (San Juan) are left out, and ``notes`` says how many.
    """
    if _is_corla_count(spec) and spec.margin:
        return spec.margin, "CORLA's contest list (min_margin)", 1.0
    named: MarginEntry | None = hand.for_contest(hand.names, spec)
    enr_name = named.enr_contest if named is not None else None
    scopes = [("state", True)] + ([(spec.counties[0], False)] if len(spec.counties) == 1 else [])
    for scope, _ in scopes:
        if scope not in results:
            continue
        found, fetched = results[scope]
        contest = find_contest(found, spec.name, enr_name)
        if contest is None:
            continue
        domain = [c for c in counties if c not in NOT_IN_CORLA] if scope == "state" else None
        votes = contest_votes(contest, domain, found.areas_are_counties)
        if not votes:
            continue
        if scope == "state" and notes is not None and found.areas_are_counties and (spec.statewide or len(spec.counties) > 1):
            notes.extend(excluded_votes_notes(contest, votes))
        moment = fetched.rank_time
        final = final_after is not None and moment >= final_after
        factor = setup.low_margin_factor_final if final else setup.low_margin_factor_preliminary
        where = "the state's results" if scope == "state" else f"{scope}'s own results"
        version = f" version {fetched.version}" if fetched.version else ""
        when = f"generated {colorado_time(fetched.generated, seconds=False)}, " if fetched.generated else ""
        how = f"{fetched.origin} {colorado_time(fetched.time, seconds=False)}"
        if not found.areas_are_counties and scope == "state":
            how += "; a summary without counties, so every county's votes"
        return (
            margin_of(votes, contest.vote_for),
            f"{where}{version} ({'final' if final else 'preliminary'}), {when}{how}: {contest.name!r}",
            factor,
        )
    if spec.margin:
        return spec.margin, "the target contest list's margin (unofficial results of its date)", setup.low_margin_factor_preliminary
    return None


def _cards_per_ballot(spec: ContestSpec, county: str, datamap: DataMap, hand: HandInputs, setup: ElectionSetup | None) -> tuple[float, str]:
    """A county's ballot cards per ballot: the target list's remarks, then a hand-supplied number, then the setup."""
    if len(spec.counties) == 1 and spec.target_cards_per_ballot:
        return float(spec.target_cards_per_ballot), "the target list's remarks"
    if county in datamap.target_cards:
        return float(datamap.target_cards[county][0]), "the target list's remarks"
    supplied = hand.counties.get(county, {}).get("cards_per_ballot")
    if supplied is not None:
        return supplied[0], supplied[1]
    if setup is not None:
        return setup.cards_for(county)
    return EstimatesSetup().default_cards_per_ballot, "default"


def _denominator(
    spec: ContestSpec,
    counties: Sequence[str],
    datamap: DataMap,
    results: Mapping[str, tuple[Results, FetchedResults]],
    setup: ElectionSetup | None,
    hand: HandInputs,
    notes: list[str],
) -> tuple[int | None, int | None, str, str]:
    """``(central, low, source, cards note)``."""
    estimates = setup.estimates if setup else EstimatesSetup()
    supplied = hand.for_contest(hand.ballot_cards, spec)
    found = _published_denominator(spec, counties, datamap, results, setup, hand, estimates)
    if found is not None:
        if supplied is not None:
            notes.append(f"the ballot cards supplied by hand ({supplied[0]}, {supplied[1]}) are not used: {found[2]} ranks above them")
        return found
    if supplied is not None:
        return supplied[0], supplied[0], f"{supplied[1]} (ESTIMATED: no published count yet)", ""
    return None, None, "no denominator yet: neither manifests nor results", ""


def _published_denominator(
    spec: ContestSpec,
    counties: Sequence[str],
    datamap: DataMap,
    results: Mapping[str, tuple[Results, FetchedResults]],
    setup: ElectionSetup | None,
    hand: HandInputs,
    estimates: EstimatesSetup,
) -> tuple[int | None, int | None, str, str] | None:
    if _is_corla_count(spec) and spec.ballot_card_count:
        return spec.ballot_card_count, spec.ballot_card_count, "CORLA's contest list (ballot_card_count): exact", ""
    partial = spec.statewide and len(spec.counties) < len(_corla_counties())
    if spec.counties and all(c in datamap.manifests for c in spec.counties) and not (partial and "state" in results):
        try:
            size = build_domain_from_manifests([datamap.manifests[c].batches for c in spec.counties], None, datamap.adjustments).size
        except ValueError:
            size = 0
        if size:
            if partial:
                return size, size, f"the manifests of the {len(spec.counties)} counties that have one (not yet every county's)", ""
            return size, size, f"the manifests of {len(spec.counties)} count{'y' if len(spec.counties) == 1 else 'ies'} (exact)", ""
    by_key = {county_key(b): n for b, n in results["state"][0].ballots_cast.items()} if "state" in results else {}
    if counties:
        total = 0.0
        low_total = 0.0
        notes: list[str] = []
        used_results = used_hand = False
        covered = True
        from_manifests = 0
        for county in counties:
            given = hand.counties.get(county, {})
            if county in datamap.manifests:
                cards_in = float(datamap.manifests[county].ballot_cards)
                total += cards_in
                low_total += cards_in
                from_manifests += 1
                continue
            cast = by_key.get(county_key(county))
            if cast is None and "ballot_cards" in given:
                total += given["ballot_cards"][0]
                low_total += given["ballot_cards"][0]
                used_hand = True
                notes.append(f"{county} {given['ballot_cards'][0]:g} ballot cards ({given['ballot_cards'][1]})")
                continue
            if cast is None and "ballots_cast" in given:
                cast = int(given["ballots_cast"][0])
                used_hand = True
                notes.append(f"{county} {cast} ballots cast ({given['ballots_cast'][1]})")
            elif cast is not None:
                used_results = True
            if cast is None:
                covered = False
                break
            cards, source = _cards_per_ballot(spec, county, datamap, hand, setup)
            total += cast * cards
            low_total += cast * min(cards, estimates.low_cards_per_ballot)
            if cards != 1.0 or len(counties) == 1:
                notes.append(f"{county} {cards:g} cards per ballot ({source})")
        if covered and (used_results or used_hand):
            if from_manifests:
                notes.insert(0, f"{from_manifests} count{'y' if from_manifests == 1 else 'ies'} from {'its' if from_manifests == 1 else 'their'} manifests")
            note = "; ".join(notes[:8]) + (f"; and {len(notes) - 8} more" if len(notes) > 8 else "")
            if len(counties) > 1 and not note:
                note = "one card per ballot in every county"
            if used_results:
                fetched = results["state"][1]
                source = f"ballots cast in the state's results{' version ' + fetched.version if fetched.version else ''} times cards per ballot"
                source += " (and inputs supplied by hand)" if used_hand else ""
            else:
                source = "ballots cast or ballot cards supplied by hand"
            return math.ceil(total), math.ceil(low_total), source + " (ESTIMATED)", note
    if spec.target_denominator:
        cards = spec.target_cards_per_ballot or 1
        return (
            spec.target_denominator,
            math.ceil(spec.target_denominator / cards),
            "the target list's denominator (its margin over its diluted margin; ESTIMATED)",
            f"{cards} cards per ballot (the target list)" if spec.target_cards_per_ballot else "",
        )
    return None


def estimate_contest(
    spec: ContestSpec,
    datamap: DataMap,
    *,
    setup: ElectionSetup | None = None,
    results: Mapping[str, tuple[Results, FetchedResults]] | None = None,
    hand: HandInputs | None = None,
) -> ContestEstimate:
    """One contest's estimate; see the module documentation."""
    hand = hand or HandInputs()
    results = results or {}
    estimates = setup.estimates if setup else EstimatesSetup()
    final_after = setup.results.final_after if setup else None
    counties, county_note = _contest_counties(spec, results)
    risk = spec.risk_limit or DEFAULT_RISK_LIMIT
    notes = [county_note] if county_note else []
    if not spec.risk_limit:
        notes.append(f"risk limit {DEFAULT_RISK_LIMIT} assumed (the Secretary of State's notice says 3%)")
    margin, margin_source, factor = _margin(spec, counties, results, hand, estimates, final_after, notes)
    denominator, low_denominator, denominator_source, cards_note = _denominator(spec, counties, datamap, results, setup, hand, notes)
    central = low = None
    if margin and denominator:
        try:
            central = round_one_sample_count(risk, margin, denominator)
            low = round_one_sample_count(risk, max(margin, math.ceil(margin * factor)), low_denominator or denominator)
            low = min(low, central)
        except ValueError as exc:
            notes.append(f"cannot compute: {exc}")
    corla = spec.sample_count if _is_corla_count(spec) else None
    from_results = central
    if corla is not None:
        central = low = corla
    elif spec.sample_count is not None:
        # A count set by an override: taken as given.
        central = low = spec.sample_count
        notes.append(f"count set by {spec.count_source}")
    return ContestEstimate(
        name=spec.name,
        counties=list(spec.counties),
        statewide=spec.statewide,
        risk_limit=risk,
        margin=margin,
        margin_source=margin_source,
        denominator=denominator,
        denominator_source=denominator_source,
        cards_note=cards_note,
        low_margin_factor=factor,
        low_denominator=low_denominator,
        central=central,
        low=low,
        corla_count=corla,
        from_results=from_results,
        notes=notes,
    )


def estimate_contests(
    datamap: DataMap,
    *,
    setup: ElectionSetup | None = None,
    results: Mapping[str, tuple[Results, FetchedResults]] | None = None,
    log: EventLog | None = None,
) -> list[ContestEstimate]:
    """Every targeted contest's estimate, from the data map (with its inputs supplied by hand), the setup and the latest results."""
    hand = hand_inputs(datamap, log)
    return [estimate_contest(spec, datamap, setup=setup, results=results, hand=hand) for spec in datamap.contests]


def _inputs(
    setup: ElectionSetup | None, results: Mapping[str, tuple[Results, FetchedResults]], log: EventLog
) -> dict[str, JSONValue]:
    margins, names = margin_entries(log)
    entries: list[JSONValue] = [e.seq for e in sorted(list(margins.values()) + list(names.values()), key=lambda e: e.seq)]
    return {
        "setup": setup.sha256 if setup else None,
        "results": {scope: fetched.sha256 for scope, (_, fetched) in sorted(results.items())},
        "margin_entries": entries,
    }


def last_estimate(log: EventLog) -> tuple[int, dict[str, JSONValue]] | None:
    """The last ``estimate.made`` event's seq and data, or None."""
    found = None
    for event in log.iter_events():
        if event.kind == "estimate.made":
            found = (event.seq, dict(event.data))
    return found


def record_estimates(
    log: EventLog,
    estimates: Sequence[ContestEstimate],
    *,
    setup: ElectionSetup | None,
    results: Mapping[str, tuple[Results, FetchedResults]],
    actor: str = "system",
    force: bool = False,
) -> int | None:
    """Record an ``estimate.made`` event if anything changed since the last one (or ``force``); return its seq, or None."""
    check_actor(actor)
    contests: list[JSONValue] = [e.to_dict() for e in estimates]
    data: dict[str, JSONValue] = {"contests": contests, "inputs": _inputs(setup, results, log)}
    digest = hashlib.sha256(canonical_json(data)).hexdigest()
    previous = last_estimate(log)
    if not force and previous is not None and previous[1].get("digest") == digest:
        return None
    return log.append("estimate.made", actor, data=data | {"digest": digest}).seq


@dataclass(frozen=True)
class EstimatePoint:
    """One recorded estimate of a contest: when, the counts, and their sources."""

    seq: int
    time: str
    central: int | None
    low: int | None
    corla_count: int | None
    margin: int | None
    margin_source: str
    denominator: int | None
    denominator_source: str


def estimate_history(log: EventLog) -> dict[str, list[EstimatePoint]]:
    """Per contest, each recorded estimate that differed from the one before, oldest first."""
    history: dict[str, list[EstimatePoint]] = {}
    for event in log.iter_events():
        if event.kind != "estimate.made":
            continue
        contests = event.data.get("contests")
        for item in contests if isinstance(contests, list) else []:
            if not isinstance(item, dict):
                continue

            def num(key: str, item: dict[str, JSONValue] = item) -> int | None:
                value = item.get(key)
                return value if isinstance(value, int) else None

            point = EstimatePoint(
                event.seq,
                event.server_time,
                num("central"),
                num("low"),
                num("corla_count"),
                num("margin"),
                str(item.get("margin_source") or ""),
                num("denominator"),
                str(item.get("denominator_source") or ""),
            )
            points = history.setdefault(str(item.get("name")), [])
            last = points[-1] if points else None
            if last is None or (last.central, last.low, last.corla_count, last.margin, last.denominator) != (
                point.central, point.low, point.corla_count, point.margin, point.denominator
            ):  # fmt: skip
                points.append(point)
    return history


# ---------------------------------------------------------------------------
# Checking estimates against CORLA's counts


@dataclass(frozen=True)
class CountCheck:
    """An estimate against CORLA's published count for one contest."""

    name: str
    corla_name: str | None
    corla_count: int | None
    central: int | None
    low: int | None
    margin: int | None
    corla_margin: int | None
    denominator: int | None
    corla_denominator: int | None
    margin_source: str
    denominator_source: str

    @property
    def error(self) -> int | None:
        """The central estimate minus CORLA's count."""
        return None if self.central is None or self.corla_count is None else self.central - self.corla_count

    @property
    def low_ok(self) -> bool | None:
        """Whether the low estimate is at most CORLA's count (so every ballot marked certain was drawn)."""
        return None if self.low is None or self.corla_count is None else self.low <= self.corla_count


def corla_contest_counts(data: bytes) -> dict[str, tuple[int, int | None, int | None]]:
    """CORLA's round-one count, minimum margin and ballot cards per targeted contest, from a contest list's bytes."""
    from corla_observer.classify import classify_bytes
    from corla_observer.datamap import column, round_target, table_rows

    classification = classify_bytes(data)
    header, rows = table_rows(data, classification)
    found: dict[str, tuple[int, int | None, int | None]] = {}

    def num(row: Mapping[str, str], name: str) -> int | None:
        col = column(header, name)
        text = (row.get(col) or "").strip() if col else ""
        return int(text) if text.isdigit() else None

    for row in rows:
        if (row.get("audit_reason") or "").strip() not in ("county_wide_contest", "state_wide_contest"):
            continue
        target = round_target(num(row, "audited_sample_count"), num(row, "optimistic_samples_to_audit"))
        if target:
            found[row.get("contest_name", "")] = (target, num(row, "min_margin"), num(row, "ballot_card_count"))
    return found


def check_counts(estimates: Sequence[ContestEstimate], corla: Mapping[str, tuple[int, int | None, int | None]]) -> list[CountCheck]:
    """Each estimate against CORLA's count for the same contest (names matched as `corla_observer.datamap.corla_contest_name_for` does)."""
    from corla_observer.datamap import corla_contest_name_for

    checks = []
    for e in estimates:
        county = e.counties[0] if len(e.counties) == 1 and not e.statewide else None
        name = corla_contest_name_for(e.name, county, corla)
        count, margin, cards = corla[name] if name else (None, None, None)
        checks.append(
            CountCheck(e.name, name, count, e.from_results if e.corla_count is not None else e.central, e.low if e.corla_count is None else None,
                       e.margin, margin, e.denominator, cards, e.margin_source, e.denominator_source)
        )  # fmt: skip
    return checks
