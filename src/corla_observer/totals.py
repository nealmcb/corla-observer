"""The totals check: each targeted contest's votes and ballots cast in the results, against every other count of them.

Run whenever results arrive, fetched or uploaded (`record_totals_check`, from
each cycle's `corla_observer.schedule.update_derived` and after an upload),
it compares, for every targeted contest and every county with data, the
votes of the contest's last winner and first loser (as the results used for
the estimates rank them, over the contest's counties in CORLA's audit) and
each county's ballots cast:

``results``
    with the other results files on record for the same scope (the next
    ranked, and the newest fetched and the newest uploaded,
    `corla_observer.enr.ranked`), and a county's own results with the
    state's for that county. Results move during the canvass, so a newer
    file differing from an older one is information; two files generated at
    the same time that differ are flagged.
``excluded``
    a county in the results but not in CORLA's audit (San Juan, which counts
    by hand): its votes are left out of a multi-county or statewide margin,
    as CORLA leaves them out; the line says what they would change.
``cvr``
    with the county's redacted CVR as uploaded (`corla_observer.tally`,
    counting the anonymizer's aggregate rows). A redacted row's votes are
    missing from the tally, so results higher by at most the redacted rows
    are explained; any other difference is flagged.
``corla_votes``
    with CORLA's vote totals by county (``CandidateVoteTotalsByCounty.csv``
    and the like, kind ``vote_totals``), once posted: flagged when they
    differ. Results with more votes than CORLA's tally or the CVR mean
    ballots that may not be in the audit's population: ask the county.
``corla_margin``, ``corla_cards``
    with CORLA's contest list: its ``min_margin`` against the results'
    margin over the same counties (flagged only when no county's totals
    explain it), and its ``ballot_card_count`` against the manifests'
    total (flagged) and the ballots cast times cards per ballot.
``manifest``
    a county's manifest total against its ballots cast times its cards per
    ballot: flagged when there are fewer cards than ballots, or, at one card
    per ballot, any difference.

Each difference is one line with the numbers, and ``flag`` when it is worth
a flag (normal severity, reason ``totals_differ``,
`corla_observer.flags`). Each run whose inputs changed is a
``totals.checked`` event: ``inputs`` (every file's SHA-256), ``digest``,
``compared``, ``differences`` (``check``, ``contest``, ``county``,
``line``, ``flag``, ``key``).

Standard library only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from corla_observer.classify import Classification, canonical_county
from corla_observer.county_status import NOT_IN_CORLA
from corla_observer.datamap import ContestSpec, DataMap, column, contest_key, table_rows
from corla_observer.election import ElectionSetup
from corla_observer.enr import (
    ContestResult,
    FetchedResults,
    Results,
    ResultsError,
    contest_votes,
    find_contest,
    margin_of,
    read_results,
    results_by_scope,
)
from corla_observer.events import EventLog, JSONValue, canonical_json, check_actor
from corla_observer.localtime import colorado_time
from corla_observer.sampling import county_key
from corla_observer.workspace import ObjectNotFoundError, Workspace

#: Most per-county lines listed for one pair of results files and one contest.
MAX_COUNTY_LINES = 8


@dataclass(frozen=True)
class Difference:
    """One difference found, as one line with the numbers."""

    check: str
    contest: str | None
    county: str | None
    line: str
    flag: bool
    key: str

    def to_dict(self) -> dict[str, JSONValue]:
        return {"check": self.check, "contest": self.contest, "county": self.county, "line": self.line, "flag": self.flag, "key": self.key}


@dataclass
class TotalsReport:
    """What `check_totals` found."""

    differences: list[Difference] = field(default_factory=list)
    compared: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> list[Difference]:
        return [d for d in self.differences if d.flag]


def _norm(name: str) -> str:
    return " ".join(name.casefold().split())


def _get(votes: Mapping[str, int], choice: str) -> int | None:
    wanted = _norm(choice)
    return next((n for c, n in votes.items() if _norm(c) == wanted), None)


def _pair(votes: Mapping[str, int], vote_for: int) -> tuple[str, str | None] | None:
    """The last winner's and the first loser's names (the loser None if there is none)."""
    ordered = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ordered) < vote_for or vote_for < 1:
        return None
    return ordered[vote_for - 1][0], (ordered[vote_for][0] if len(ordered) > vote_for else None)


def _two(votes: Mapping[str, int], pair: tuple[str, str | None]) -> tuple[int, int] | None:
    w = _get(votes, pair[0])
    loser = 0 if pair[1] is None else _get(votes, pair[1])
    if w is None and loser is None:
        return None
    return w or 0, loser or 0


def _fmt(two: tuple[int, int]) -> str:
    return f"{two[0]:,}–{two[1]:,} (margin {two[0] - two[1]:,})"


def _signed(n: int) -> str:
    return f"{n:+,}"


def _label(item: FetchedResults) -> str:
    when = colorado_time(item.rank_time, seconds=False)
    return f"{'results' if item.scope == 'state' else item.scope + '’s own results'} of {when} ({item.origin})"


class _Inputs:
    """Every input of the check, read once."""

    def __init__(self, workspace: Workspace, log: EventLog, datamap: DataMap, setup: ElectionSetup | None) -> None:
        from corla_observer.uploads import status

        self.workspace, self.datamap, self.setup = workspace, datamap, setup
        self.scopes = results_by_scope(log, workspace)
        self._read: dict[str, Results | None] = {}
        self.chosen: dict[str, FetchedResults] = {}
        for scope, items in self.scopes.items():
            for item in items:
                if self.read(item) is not None:
                    self.chosen[scope] = item
                    break
        self.cvrs: dict[str, tuple[str, str]] = {}
        for county, entry in status(log).items():
            cvr = entry.latest.get("redacted_cvr")
            if cvr is not None and cvr.verdict == "PASS":
                self.cvrs[county] = (cvr.sha256, cvr.server_time)
        self.vote_totals = [
            e for e in datamap.used("vote_totals") if e.election in (None, datamap.election) or datamap.election is None
        ]

    def candidates(self, scope: str) -> list[FetchedResults]:
        """The chosen file of ``scope`` and those it is compared with: the next ranked, the newest fetched and the newest uploaded."""
        chosen = self.chosen.get(scope)
        if chosen is None:
            return []
        readable = [i for i in self.scopes.get(scope, []) if i.sha256 != chosen.sha256]
        picks: list[FetchedResults | None] = [
            *readable[:1], next((i for i in readable if not i.uploaded), None), next((i for i in readable if i.uploaded), None)
        ]
        out = [chosen]
        for item in picks:
            if item is not None and item.sha256 not in {o.sha256 for o in out} and self.read(item) is not None:
                out.append(item)
        return out

    def read(self, item: FetchedResults) -> Results | None:
        if item.sha256 not in self._read:
            try:
                self._read[item.sha256] = read_results(self.workspace.get_bytes(item.sha256), item.scope)
            except (ResultsError, ObjectNotFoundError, ValueError):
                self._read[item.sha256] = None
        return self._read[item.sha256]

    def digest_inputs(self) -> dict[str, JSONValue]:
        contests = [
            [s.name, s.margin, s.ballot_card_count, sorted(s.counties), s.source.startswith("contests_list")] for s in self.datamap.contests
        ]
        return {
            "results": list[JSONValue](sorted({i.sha256 for scope in self.chosen for i in self.candidates(scope)})),
            "cvrs": {c: sha for c, (sha, _) in sorted(self.cvrs.items())},
            "vote_totals": list[JSONValue](sorted(e.sha256 for e in self.vote_totals)),
            "manifests": {c: m.sha256 for c, m in sorted(self.datamap.manifests.items())},
            "adjustments": len(self.datamap.adjustments),
            "contests": hashlib.sha256(canonical_json(contests)).hexdigest(),  # type: ignore[arg-type]
            "setup": self.setup.sha256 if self.setup else None,
        }

    def corla_totals(self) -> tuple[dict[tuple[str, str], dict[str, int]], dict[str, dict[str, int]], str | None]:
        """CORLA's vote totals by (contest key, county), and by contest key without counties, and the file's SHA-256."""
        by_county: dict[tuple[str, str], dict[str, int]] = {}
        overall: dict[str, dict[str, int]] = {}
        shas = []
        for entry in sorted(self.vote_totals, key=lambda e: e.ref):
            try:
                data = self.workspace.get_bytes(entry.sha256)
            except ObjectNotFoundError:
                continue
            header, rows = table_rows(data, Classification(entry.classified_kind, entry.classified_county, entry.evidence))
            contest_col = column(header, "contest_name", "contest name", "contest")
            choice_col = column(header, "choice", "choice name", "candidate")
            votes_col = column(header, "votes", "total votes")
            county_col = column(header, "county_name", "county name", "county")
            if not (contest_col and choice_col and votes_col):
                continue
            shas.append(entry.sha256)
            for row in rows:
                votes = (row.get(votes_col) or "").strip()
                if not votes.isdigit():
                    continue
                key = contest_key(row.get(contest_col) or "")
                choice = (row.get(choice_col) or "").strip()
                county = canonical_county(row.get(county_col) or "") if county_col else None
                table = by_county.setdefault((key, county), {}) if county else overall.setdefault(key, {})
                table[choice] = table.get(choice, 0) + int(votes)
        return by_county, overall, ",".join(shas[:2]) or None

    def cards_per_ballot(self, county: str) -> tuple[float, str]:
        if county in self.datamap.target_cards:
            return float(self.datamap.target_cards[county][0]), "the target list's remarks"
        if self.setup is not None:
            return self.setup.cards_for(county)
        return 1.0, "default"


def _domain(spec: ContestSpec, state: Results | None) -> list[str]:
    from corla_observer.estimate import _corla_counties

    if spec.statewide and len(spec.counties) < len(_corla_counties()) and state is not None and state.ballots_cast:
        return [c for c in state.ballots_cast if c not in NOT_IN_CORLA]
    return [c for c in spec.counties if c not in NOT_IN_CORLA]


def _area(contest: ContestResult, county: str) -> dict[str, int] | None:
    key = county_key(county)
    return next((v for a, v in contest.by_area.items() if county_key(a) == key), None)


def check_totals(workspace: Workspace, log: EventLog, datamap: DataMap, setup: ElectionSetup | None = None) -> TotalsReport:
    """Every difference between the results and the other counts; see the module documentation."""
    return _check(_Inputs(workspace, log, datamap, setup))


def _check(inputs: _Inputs) -> TotalsReport:
    from corla_observer.estimate import _is_corla_count
    from corla_observer.tally import tally_cvr

    report = TotalsReport()
    out = report.differences
    state_item = inputs.chosen.get("state")
    state = inputs.read(state_item) if state_item else None
    by_county_state = state is not None and state.areas_are_counties
    corla_by_county, corla_overall, corla_sha = inputs.corla_totals()
    tallies: dict[str, object] = {}

    def tally(county: str) -> object:
        if county not in tallies:
            sha, _ = inputs.cvrs[county]
            try:
                tallies[county] = tally_cvr(inputs.workspace.path_for(sha))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                report.notes.append(f"{county}'s CVR could not be tallied: {exc}")
                tallies[county] = None
        return tallies[county]

    if not inputs.chosen:
        report.notes.append("no results yet")
    for spec in datamap_contests(inputs.datamap):
        domain = _domain(spec, state)
        single = len(spec.counties) == 1 and not spec.statewide
        own_item = inputs.chosen.get(spec.counties[0]) if single else None
        own = inputs.read(own_item) if own_item else None
        # The reference: the state's results, else for a one-county contest the county's own.
        ref_item, ref = (state_item, state) if state is not None and find_contest(state, spec.name) else (own_item, own)
        if ref is None or ref_item is None:
            continue
        contest = find_contest(ref, spec.name)
        if contest is None:
            continue
        ref_counties = domain if ref is state else None
        votes = contest_votes(contest, ref_counties, ref.areas_are_counties)
        pair = _pair(votes, contest.vote_for)
        if pair is None:
            continue
        names = f"{pair[0]}–{pair[1] or '(no loser)'}"
        cname = spec.name

        def key(check: str, county: str | None, a: str | None, b: str | None, cname: str = cname) -> str:
            return f"totals|{check}|{cname}|{county or '-'}|{(a or '-')[:12]}|{(b or '-')[:12]}"

        # Counties in the results but not in CORLA's audit.
        if ref is state and by_county_state and (spec.statewide or len(spec.counties) > 1):
            for area, per in sorted(contest.by_area.items()):
                if area in NOT_IN_CORLA and any(per.values()):
                    two = _two(per, pair) or (0, 0)
                    out.append(Difference(
                        "excluded", cname, area,
                        f"{cname}, {area}: in the results ({names} {_fmt(two)}) but not in CORLA's audit: left out of the margin "
                        f"({_signed(two[0] - two[1])} with it)", False, key("excluded", area, ref_item.sha256, None),
                    ))  # fmt: skip

        # (a) The other results files on record.
        for scope in ["state"] + ([spec.counties[0]] if single else []):
            items = inputs.candidates(scope)
            base_item = items[0] if items else None
            base = inputs.read(base_item) if base_item else None
            base_contest = find_contest(base, spec.name) if base else None
            if base is None or base_item is None or base_contest is None:
                continue
            for other_item in items[1:]:
                other = inputs.read(other_item)
                other_contest = find_contest(other, spec.name) if other else None
                if other is None or other_contest is None:
                    continue
                report.compared += 1
                same_time = other_item.rank_time == base_item.rank_time
                why = "generated at the same time" if same_time else ("newer" if base_item.rank_time > other_item.rank_time else "older")
                lines = []
                if base.areas_are_counties and other.areas_are_counties:
                    for county in domain:
                        a, b = _area(base_contest, county), _area(other_contest, county)
                        if a is None or b is None:
                            continue
                        ta, tb = _two(a, pair), _two(b, pair)
                        if ta and tb and ta != tb:
                            lines.append((county, ta, tb))
                da = _two(contest_votes(base_contest, domain if base.areas_are_counties else None, base.areas_are_counties), pair)
                db = _two(contest_votes(other_contest, domain if other.areas_are_counties else None, other.areas_are_counties), pair)
                for county, ta, tb in lines[:MAX_COUNTY_LINES]:
                    out.append(Difference(
                        "results", cname, county,
                        f"{cname}, {county}: {_label(base_item)} {_fmt(ta)} vs {_label(other_item)} {_fmt(tb)}, {names}: "
                        f"margin {_signed((ta[0] - ta[1]) - (tb[0] - tb[1]))} in the {why} file",
                        same_time, key("results", county, base_item.sha256, other_item.sha256),
                    ))  # fmt: skip
                if len(lines) > MAX_COUNTY_LINES:
                    out.append(Difference("results", cname, None, f"{cname}: and {len(lines) - MAX_COUNTY_LINES} more counties differ between these two files",
                                          False, key("results_more", None, base_item.sha256, other_item.sha256)))  # fmt: skip
                if da and db and da != db and len(domain) > 1:
                    out.append(Difference(
                        "results", cname, None,
                        f"{cname}, its {len(domain)} counties in CORLA: {_label(base_item)} {_fmt(da)} vs {_label(other_item)} {_fmt(db)}",
                        same_time and not lines, key("results_domain", None, base_item.sha256, other_item.sha256),
                    ))  # fmt: skip
        if single and own is not None and own_item is not None and by_county_state and state_item is not None and ref is state:
            own_contest = find_contest(own, spec.name)
            in_state = _area(contest, spec.counties[0])
            if own_contest is not None and in_state is not None:
                report.compared += 1
                ta, tb = _two(own_contest.totals, pair), _two(in_state, pair)
                if ta and tb and ta != tb:
                    out.append(Difference(
                        "results", cname, spec.counties[0],
                        f"{cname}: {_label(own_item)} {_fmt(ta)} vs the state's {_label(state_item)} {_fmt(tb)}, {names}",
                        False, key("county_results", spec.counties[0], own_item.sha256, state_item.sha256),
                    ))  # fmt: skip

        def ref_area(county: str) -> tuple[dict[str, int] | None, FetchedResults]:
            if ref is state and by_county_state:
                return _area(contest, county), ref_item
            return (dict(contest.totals) if county == spec.counties[0] else None), ref_item

        def agreeing(county: str, expected: tuple[int, int], slack: int = 0) -> str:
            """Which other results file on record agrees with ``expected`` for ``county``."""
            for scope in ("state", county):
                for item in inputs.candidates(scope)[1:] if scope == ref_item.scope else inputs.candidates(scope):
                    found = inputs.read(item)
                    c = find_contest(found, spec.name) if found else None
                    per = (_area(c, county) if found and found.areas_are_counties else (c.totals if c else None)) if c else None
                    two = _two(per, pair) if per else None
                    if two and 0 <= two[0] - expected[0] <= slack and 0 <= two[1] - expected[1] <= slack:
                        return f"; the {_label(item)} agreed"
            return ""

        # (b) The counties' redacted CVRs.
        flagged_effect = 0
        for county in domain:
            if county not in inputs.cvrs:
                continue
            t = tally(county)
            if t is None:
                continue
            ct = t.find(spec.name)  # type: ignore[attr-defined]
            got, item = ref_area(county)
            if ct is None or got is None:
                continue
            report.compared += 1
            tr, tc = _two(got, pair), _two(ct.votes, pair)
            if tr is None or tc is None:
                out.append(Difference("cvr", cname, county, f"{cname}, {county}: the CVR has neither {names} (choice names differ?)", False,
                                      key("cvr_names", county, item.sha256, inputs.cvrs[county][0])))  # fmt: skip
                continue
            if tr == tc:
                continue
            dw, dl = tr[0] - tc[0], tr[1] - tc[1]
            cast = ref.ballots_cast.get(county) if ref is state else None
            every_ballot = cast is not None and t.rows == cast and ct.aggregate_rows == 0  # type: ignore[attr-defined]
            explained = dw >= 0 and dl >= 0 and (every_ballot or (dw <= ct.redacted_rows and dl <= ct.redacted_rows))
            if explained and every_ballot and (dw > ct.redacted_rows or dl > ct.redacted_rows):
                why = (f"the CVR has a row for each of the {cast:,} ballots cast, so the {dw + dl} votes it lacks were blanked "
                       f"by its redaction (margin {_signed(dw - dl)})")  # fmt: skip
            elif explained:
                why = f"within the CVR's {ct.redacted_rows} redacted rows"
            elif dw >= 0 and dl >= 0:
                why = (f"the results have {dw + dl} more votes than the CVR ({_signed(dw)}, {_signed(dl)}; {ct.redacted_rows} rows redacted): "
                       "ballots the CVR does not have may not be in the audit's population; ask the county")  # fmt: skip
            else:
                why = f"the CVR differs by {_signed(-dw)}, {_signed(-dl)} ({ct.redacted_rows} rows redacted): ask the county"
            if not explained:
                flagged_effect += (dw - dl)
            out.append(Difference(
                "cvr", cname, county,
                f"{cname}, {county}: {_label(item)} {_fmt(tr)} vs the redacted CVR {_fmt(tc)}, {names}: {why}"
                + ("" if explained else agreeing(county, tc, ct.redacted_rows)),
                not explained, key("cvr", county, item.sha256, inputs.cvrs[county][0]),
            ))  # fmt: skip

        # (c) CORLA's tallies and contest list.
        ckey = contest_key(spec.name)
        corla_effect = 0
        corla_counties = 0
        for county in domain:
            corla = corla_by_county.get((ckey, county))
            got, item = ref_area(county)
            if corla is None or got is None:
                continue
            corla_counties += 1
            report.compared += 1
            tr, tc = _two(got, pair), _two(corla, pair)
            if tr is None or tc is None or tr == tc:
                continue
            dw, dl = tr[0] - tc[0], tr[1] - tc[1]
            corla_effect += dw - dl
            more = dw >= 0 and dl >= 0
            why = (f"the results have {dw + dl} more votes than CORLA's tally: ballots CORLA does not have may not be in the audit's population; ask the county"
                   if more else f"CORLA's tally differs by {_signed(-dw)}, {_signed(-dl)}: ask the county")  # fmt: skip
            out.append(Difference(
                "corla_votes", cname, county,
                f"{cname}, {county}: {_label(item)} {_fmt(tr)} vs CORLA's vote totals {_fmt(tc)}, {names}: margin {_signed(dw - dl)}; {why}"
                + agreeing(county, tc),
                True, key("corla_votes", county, item.sha256, corla_sha),
            ))  # fmt: skip
        if not corla_counties and corla_overall.get(ckey) and ref is state:
            tr, tc = _two(votes, pair), _two(corla_overall[ckey], pair)
            report.compared += 1
            if tr and tc and tr != tc:
                out.append(Difference("corla_votes", cname, None,
                                      f"{cname}: {_label(ref_item)} {_fmt(tr)} over CORLA's counties vs CORLA's vote totals {_fmt(tc)}, {names}",
                                      True, key("corla_votes_total", None, ref_item.sha256, corla_sha)))  # fmt: skip
        if _is_corla_count(spec) and spec.margin is not None:
            report.compared += 1
            margin = margin_of(votes, contest.vote_for)
            if margin != spec.margin:
                gap = margin - spec.margin
                explained = corla_counties > 0 and gap == corla_effect
                if explained:
                    why = "the county differences above account for it"
                elif flagged_effect == gap and flagged_effect:
                    why = "the CVR differences above account for it"
                else:
                    why = "no county's totals explain it"
                out.append(Difference(
                    "corla_margin", cname, None,
                    f"{cname}: the margin in the {_label(ref_item)} over {len(domain)} count{'y' if len(domain) == 1 else 'ies'} in CORLA, "
                    f"{margin:,}, vs CORLA's min_margin {spec.margin:,} ({_signed(gap)}): {why}",
                    why == "no county's totals explain it", key("corla_margin", None, ref_item.sha256, str(spec.margin)),
                ))  # fmt: skip
        if _is_corla_count(spec) and spec.ballot_card_count:
            manifests = [inputs.datamap.manifests.get(c) for c in spec.counties]
            if spec.counties and all(m is not None for m in manifests):
                report.compared += 1
                total = sum(m.ballot_cards for m in manifests if m is not None)
                if total != spec.ballot_card_count:
                    out.append(Difference(
                        "corla_cards", cname, None,
                        f"{cname}: CORLA's ballot_card_count {spec.ballot_card_count:,} vs the manifests' {total:,} ({_signed(spec.ballot_card_count - total)})",
                        True, key("corla_cards", None, str(spec.ballot_card_count), str(total)),
                    ))  # fmt: skip
            if state is not None and by_county_state and all(c in state.ballots_cast for c in domain) and domain:
                report.compared += 1
                cast_cards = round(sum(state.ballots_cast[c] * inputs.cards_per_ballot(c)[0] for c in domain))
                if cast_cards != spec.ballot_card_count:
                    out.append(Difference(
                        "corla_cards", cname, None,
                        f"{cname}: CORLA's ballot_card_count {spec.ballot_card_count:,} vs ballots cast in the {_label(state_item)} "  # type: ignore[arg-type]
                        f"times cards per ballot {cast_cards:,}", False, key("corla_cards_cast", None, state_item.sha256, str(spec.ballot_card_count)),  # type: ignore[union-attr]
                    ))  # fmt: skip

    # (d) The manifests against ballots cast; and ballots cast between results files.
    if state is not None and state_item is not None and by_county_state:
        for county, manifest in sorted(inputs.datamap.manifests.items()):
            cast = state.ballots_cast.get(county)
            if cast is None:
                continue
            report.compared += 1
            cards = manifest.ballot_cards
            per_ballot, source = inputs.cards_per_ballot(county)
            expected = round(cast * per_ballot)
            if cards == expected:
                continue
            flag = cards < cast or per_ballot == 1.0
            agreed = next(
                (i for i in inputs.candidates("state")[1:] if (r := inputs.read(i)) is not None and r.ballots_cast.get(county) is not None
                 and round(r.ballots_cast[county] * per_ballot) == cards),
                None,
            )  # fmt: skip
            out.append(Difference(
                "manifest", None, county,
                f"{county}: the manifest's {cards:,} ballot cards vs {cast:,} ballots cast in the {_label(state_item)} times {per_ballot:g} card{'' if per_ballot == 1 else 's'} per ballot "
                f"({source}) = {expected:,} ({_signed(cards - expected)}; {cards / cast:.3f} cards per ballot)"
                + (f"; the {_label(agreed)} agreed" if agreed else "") if cast else f"{county}: no ballots cast",
                flag, f"totals|manifest|-|{county}|{manifest.sha256[:12]}|{state_item.sha256[:12]}",
            ))  # fmt: skip
        for other_item in inputs.candidates("state")[1:]:
            other = inputs.read(other_item)
            if other is None or not other.ballots_cast:
                continue
            changed = [(c, n, other.ballots_cast[c]) for c, n in sorted(state.ballots_cast.items()) if c in other.ballots_cast and other.ballots_cast[c] != n]
            for county, n, m in changed[:MAX_COUNTY_LINES]:
                out.append(Difference(
                    "results", None, county,
                    f"{county}: ballots cast {n:,} in the {_label(state_item)} vs {m:,} in the {_label(other_item)} ({_signed(n - m)})",
                    other_item.rank_time == state_item.rank_time, f"totals|ballots_cast|-|{county}|{state_item.sha256[:12]}|{other_item.sha256[:12]}",
                ))  # fmt: skip
            if len(changed) > MAX_COUNTY_LINES:
                out.append(Difference("results", None, None, f"and {len(changed) - MAX_COUNTY_LINES} more counties' ballots cast differ between these two files",
                                      False, f"totals|ballots_cast_more|-|-|{state_item.sha256[:12]}|{other_item.sha256[:12]}"))  # fmt: skip
    seen: set[str] = set()
    report.differences = [d for d in out if not (d.key in seen or seen.add(d.key))]  # type: ignore[func-returns-value]
    return report


def datamap_contests(datamap: DataMap) -> Sequence[ContestSpec]:
    """The targeted contests checked."""
    return datamap.contests


def last_totals_check(log: EventLog) -> tuple[int, str, dict[str, JSONValue]] | None:
    """The last ``totals.checked`` event's seq, time and data, or None."""
    found = None
    for event in log.iter_events():
        if event.kind == "totals.checked":
            found = (event.seq, event.server_time, dict(event.data))
    return found


def record_totals_check(
    workspace: Workspace,
    log: EventLog,
    datamap: DataMap,
    setup: ElectionSetup | None = None,
    *,
    actor: str = "system",
    force: bool = False,
) -> int | None:
    """Run the check and record a ``totals.checked`` event if its inputs changed since the last (or ``force``); return its seq, or None."""
    check_actor(actor)
    inputs = _Inputs(workspace, log, datamap, setup)
    used = inputs.digest_inputs()
    digest = hashlib.sha256(canonical_json(used)).hexdigest()
    previous = last_totals_check(log)
    if not force and previous is not None and previous[2].get("digest") == digest:
        return None
    if previous is None and not inputs.chosen and not force:
        return None
    report = _check(inputs)
    data: dict[str, JSONValue] = {
        "inputs": used,
        "digest": digest,
        "compared": report.compared,
        "differences": [d.to_dict() for d in report.differences],
        "notes": list(report.notes),
    }
    return log.append("totals.checked", actor, data=data).seq


def report_lines(data: Mapping[str, JSONValue]) -> list[str]:
    """A recorded check as text lines, the flagged differences first."""
    diffs = [d for d in data.get("differences") or [] if isinstance(d, dict)]  # type: ignore[union-attr]
    ordered = sorted(diffs, key=lambda d: not d.get("flag"))
    lines = [f"{'FLAG ' if d.get('flag') else '     '}{d.get('line')}" for d in ordered]
    return lines or [f"no differences in {data.get('compared', 0)} comparisons"]
