"""The coordinator's targeting menu: which of a county's contests extra random draws would help, and how many.

After CORLA's rounds (or at any time, recomputed), for a county whose redacted CVR is in the
workspace, this module classifies each contest of the CVR, groups the county's own contests into
sampling domains, lists the domains worth drawing from (the **menu**), suggests how to spend a
budget of extra cards, and says how many draws would bring each contest's risk under the risk
limit. The draws themselves are domain draws (`corla_observer.targets.domain_draw`), sources of
the working list like any other. The method is docs/TARGETING.md, "The targeting menu"; it was
worked out on the 2020 general election.

**Local and regional.** A contest held only in this county is *local*; one that spans several
counties is *regional*. CORLA's contest list by county (``contest_ids_by_county``) says which;
without it a contest is regional when its name is a statewide office's or a US House seat's
(`corla_observer.targets.statewide_by_name`, `corla_observer.targets.is_house`), else local, and
the menu says it assumed so.

**Pooled risk (local contests).** CORLA draws cards uniformly over the whole county (or the
state). Its draws that landed on cards carrying a local contest are, given where they landed,
uniform over that contest's own cards; extra cards drawn uniformly **with replacement** from the
same cards (a domain draw) join them as one uniform sample of the contest's domain, and the
sequential Kaplan-Markov test may go on after seeing results. So the risk of a local contest, at
every point, is Kaplan-Markov (`km_risk`, gamma 1.03905) with

- diluted margin = the contest's margin / its cards in the county (the larger of the CVR's count
  and CORLA's ``contest_ballot_card_count``);
- ``n`` = CORLA's draws on cards carrying the contest, **counted with multiplicity** (a card
  drawn twice counts twice), plus the domain draws made from its domain;
- the discrepancies (o1, o2, u1, u2) seen on those draws.

This is fixed in advance: the menu never takes the lower of two risks. It assumes the CVR's
card-style information (which cards carry which contest) is accurate, though the CVR is what is
being audited; formal checks of that information are not made, and only the CVR's contest card
counts are compared with CORLA's.

**Where the numbers come from** (`gather`). CORLA's draws are its own once posted: its
selection lists placed in the county by its CVR-to-audit-board comparison (`posted_draws`).
Until then they are reproduced from the seed and the manifests (`corla_observer.predict`,
`corla_observer.sampling`), one sequence per domain of targeted contests, up to its contests'
latest cumulative draw count (`corla_observer.datamap.round_target`); without a seed or a
contest list, the RLA extract's distinct cards stand in, each counted once (which can only
understate ``n``). CVR contests are matched to CORLA's by name (`menu_key`), and where a county
spells one otherwise, by spelling (`match_corla`). Discrepancies come from CORLA's posted CVR-to-audit-board comparison
(``cvr_comparison``) when the workspace has it; otherwise from the observers' own entries
(``observation.contest``: what was heard instead of the expected choice). Domain draws' cards are
never in CORLA's comparison, so their discrepancies always come from the observers' entries;
until a card is examined it is assumed to show none. The menu names the source of each.

**Margins and the recount rule.** A contest's margin for the diluted margin is CORLA's
``min_margin`` when its contest list has the contest, else the state's election night results
(`corla_observer.enr`), else the county's CVR. The automatic recount (C.R.S. 1-10.5-101) is due at
a margin of at most 0.5% of the highest vote, or for a multi-winner contest of the lowest
winner's votes, from the results, else the CVR's votes. That is a note (*recount*) beside the
contest, for information only: Colorado's recounts usually re-tabulate by machine rather than
interpret the paper ballots by hand, so such a contest still needs checking and is treated like
any other.

**Properties** (`ContestFacts`), independent of one another: *unseen* (no card of the official
sample carried it; domain draws landing on it later are shown apart, "seen by N domain draws");
*flagged* (a discrepancy was found on its cards, with its types); *thin* or *settled* (its risk
above the limit or not; neither for an uncontested contest, which has no risk); and the notes
*uncontested* (no more candidates than seats), *recount* and *tie* (a margin of zero: each card
checks the CVR's count, but the risk can't fall). Unseen says nothing about the risk by itself. The
page shows these as labelled badges (`Tag`); the command as plain words. A regional contest's risk is
CORLA's own, computed from its contest list (`CorlaContest.risk`); regional contests get coverage
only, since in-county draws are not uniform over a multi-county domain, and an unseen or thin one
of which the county holds under `REGIONAL_SHARE_CUTOFF` of the cards is *left off*.

**Sampling domains** (`domain_groups`). Local contests carried by exactly the same cards form
one domain: one draw from it is a valid draw for every one of them. A contest whose cards merely
overlap the domain gets coverage only.

**The menu** (`menu_lines`, `line_rows`): one line per domain any of whose local contests is
unseen, thin or flagged (an unseen uncontested contest included: one card checks that the CVR
records it), riskiest first, with the riskiest contest's margin, and naming the menu contests the
domain's cards also land on (coverage only). **The suggestion** (`allocate`): a budget spread over
domains. First each domain holding an unseen local contest or a tie that no domain draw has reached
gets one draw; the rest goes by *minimax* (lower the highest pooled risk first) or *ladder*
(cheapest domains under 90%, then 50%, 20%, 10% and the limit). Ties are left out of the highest
risk those lower.

Standard library only.
"""

from __future__ import annotations

import difflib
import math
import re
import threading
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from corla_observer.datamap import contest_key
from corla_observer.enr import contest_label, margin_of
from corla_observer.extract import id_key

if TYPE_CHECKING:
    from corla_observer.datamap import DataMap
    from corla_observer.enr import Results
    from corla_observer.events import EventLog
    from corla_observer.targets import CvrVotes, DomainDrawRecord
    from corla_observer.workspace import Workspace

#: CORLA's gamma.
GAMMA = 1.03905
#: C.R.S. 1-10.5-101: an automatic recount at a margin of at most 0.5% of the reference vote.
RECOUNT_FRACTION = 0.005
#: A regional contest of which the county holds a smaller share of the cards stays off its menu.
REGIONAL_SHARE_CUTOFF = 0.05
#: Draws from a domain the menu shows the risk after.
STEPS: tuple[int, ...] = (5, 10, 20)
#: The ladder's tolerances, highest first; the risk limit comes after them.
TOLERANCES: tuple[float, ...] = (0.90, 0.50, 0.20, 0.10)
#: The risk limit when neither CORLA's contest list nor the target list gives one.
DEFAULT_RISK_LIMIT = 0.03
#: The largest budget the suggestion spreads.
MAX_BUDGET = 2000

LOCAL = "local"
REGIONAL = "regional"

FLAGGED = "flagged"
UNSEEN = "unseen"
SETTLED = "settled"
THIN = "thin"
#: Notes beside a contest's status, for information.
UNCONTESTED = "uncontested"
RECOUNT = "recount"
#: A local contest whose margin is zero or less: no number of draws lowers its risk.
TIE = "tie"

MINIMAX = "minimax"
LADDER = "ladder"

_WRITE_IN_RE = re.compile(r"^\s*write[\s-]*in", re.IGNORECASE)
_BALLOT_WORD_RE = re.compile(r"\bballot\b")


def menu_key(name: str) -> str:
    """A contest's name as the menu matches it across the CVR and CORLA's files: `corla_observer.datamap.contest_key`
    without the word "ballot", which counties' CVRs often leave out ("Gilpin County Issue 1A" for CORLA's "Gilpin
    County Ballot Issue 1A").

    >>> menu_key("City of Black Hawk Ballot Question 2A") == menu_key("City of Black Hawk Question 2A (Vote For=1)")
    True
    """
    return " ".join(
        _BALLOT_WORD_RE.sub(" ", contest_key(contest_label(name)[0])).split()
    )


# ---------------------------------------------------------------------------
# Kaplan-Markov


def _km_raw(
    n: int, diluted_margin: float, o1: int, o2: int, u1: int, u2: int, gamma: float
) -> float:
    return (
        (1 - diluted_margin / (2 * gamma)) ** n
        * (1 - 1 / (2 * gamma)) ** -o1
        * (1 - 1 / gamma) ** -o2
        * (1 + 1 / (2 * gamma)) ** -u1
        * (1 + 1 / gamma) ** -u2
    )


def km_risk(
    n: int,
    diluted_margin: float,
    o1: int = 0,
    o2: int = 0,
    u1: int = 0,
    u2: int = 0,
    gamma: float = GAMMA,
) -> float:
    """Kaplan-Markov risk of a ballot-level comparison audit after ``n`` draws, capped at 1.

    ``(1 - m/(2g))^n (1 - 1/(2g))^-o1 (1 - 1/g)^-o2 (1 + 1/(2g))^-u1 (1 + 1/g)^-u2``, with
    ``m`` the diluted margin and ``g`` gamma; 1 for a margin of zero or less.

    >>> round(km_risk(10, 0.1), 6)
    0.610686
    """
    if diluted_margin <= 0:
        return 1.0
    return min(1.0, _km_raw(n, diluted_margin, o1, o2, u1, u2, gamma))


def draws_to(
    target: float,
    diluted_margin: float,
    n0: int = 0,
    o1: int = 0,
    o2: int = 0,
    u1: int = 0,
    u2: int = 0,
    gamma: float = GAMMA,
) -> int | None:
    """Fewest extra draws k with ``km_risk(n0 + k, ...) <= target``, assuming they show no discrepancy.

    None when no number of draws gets there (a margin of zero or less).
    """
    if diluted_margin <= 0 or target <= 0:
        return None
    if km_risk(n0, diluted_margin, o1, o2, u1, u2, gamma) <= target:
        return 0
    base = 1 - diluted_margin / (2 * gamma)
    factor = _km_raw(0, diluted_margin, o1, o2, u1, u2, gamma)
    total = max(0, math.ceil(math.log(target / factor) / math.log(base)))
    while (
        total > 0
        and km_risk(total - 1, diluted_margin, o1, o2, u1, u2, gamma) <= target
    ):
        total -= 1
    while km_risk(total, diluted_margin, o1, o2, u1, u2, gamma) > target:
        total += 1
    return max(0, total - n0)


@dataclass(frozen=True)
class Discrepancies:
    """Counts of one- and two-vote overstatements and understatements."""

    o1: int = 0
    o2: int = 0
    u1: int = 0
    u2: int = 0

    @property
    def total(self) -> int:
        return self.o1 + self.o2 + self.u1 + self.u2

    def __add__(self, other: Discrepancies) -> Discrepancies:
        return Discrepancies(
            self.o1 + other.o1,
            self.o2 + other.o2,
            self.u1 + other.u1,
            self.u2 + other.u2,
        )

    @classmethod
    def of(cls, overstatement: int) -> Discrepancies:
        """One card's discrepancy (2, 1, 0, -1 or -2 votes overstated) as counts."""
        return cls(
            o1=int(overstatement == 1),
            o2=int(overstatement >= 2),
            u1=int(overstatement == -1),
            u2=int(overstatement <= -2),
        )

    def tag_text(self) -> str:
        """The types found, as in a tag: "o1 x1, u2 x2"."""
        return ", ".join(
            f"{name} x{n}"
            for name, n in (
                ("o1", self.o1),
                ("o2", self.o2),
                ("u1", self.u1),
                ("u2", self.u2),
            )
            if n
        )

    def text(self) -> str:
        parts = [
            f"{n} {name}"
            for name, n in (
                ("o1", self.o1),
                ("o2", self.o2),
                ("u1", self.u1),
                ("u2", self.u2),
            )
            if n
        ]
        return ", ".join(parts) or "none"


def _norm(choice: str) -> str:
    return " ".join(choice.strip().strip('"').casefold().split())


def overstatement(
    cvr: Collection[str],
    audited: Collection[str],
    winners: Collection[str],
    losers: Collection[str],
) -> int:
    """A card's overstatement of a contest's margin, as CORLA counts it: the largest, over every winner w and loser l,
    of ``(cvr_w - cvr_l) - (audited_w - audited_l)``, each 1 if the choice is voted and 0 if not; 0 without a loser.

    ``cvr`` and ``audited`` are the choices voted (an overvote or no vote is empty).

    >>> overstatement({"A"}, {"B"}, {"A"}, {"B"})
    2
    >>> overstatement(set(), {"A"}, {"A"}, {"B"})
    -1
    """
    c, a = {_norm(x) for x in cvr}, {_norm(x) for x in audited}
    pairs = [(_norm(w), _norm(lo)) for w in winners for lo in losers]
    if not pairs:
        return 0
    return max(((w in c) - (lo in c)) - ((w in a) - (lo in a)) for w, lo in pairs)


# ---------------------------------------------------------------------------
# The recount rule and the statuses


@dataclass(frozen=True)
class Recount:
    """A contest's margin against the automatic-recount threshold."""

    margin: int | None
    reference: int | None
    threshold: float | None
    recount: bool
    uncontested: bool
    #: Where the votes came from.
    source: str = ""


def candidates(choices: Iterable[str]) -> list[str]:
    """The choices that are candidates or answers: write-in placeholders left out."""
    return [c for c in choices if not _WRITE_IN_RE.match(c)]


def recount_status(
    votes: Mapping[str, int],
    seats: int,
    choices: Iterable[str] | None = None,
    source: str = "",
) -> Recount:
    """C.R.S. 1-10.5-101's automatic recount, for one contest's votes.

    Uncontested when there are no more candidates (``choices``, else the voted choices, write-in
    placeholders left out) than ``seats``. Otherwise a recount is due when the margin (the last
    winner's votes minus the first loser's) is at most 0.5% of the highest vote, or for a
    multi-winner contest of the lowest winner's votes.

    >>> recount_status({"A": 1000, "B": 996}, 1).recount
    True
    >>> recount_status({"A": 1000, "B": 990}, 1).recount
    False
    """
    seats = max(1, seats)
    names = candidates(choices if choices is not None else votes)
    if len(names) <= seats:
        return Recount(None, None, None, False, True, source)
    if not votes or not any(votes.values()):
        return Recount(None, None, None, False, False, source)
    ordered = sorted(votes.values(), reverse=True)
    reference = ordered[0] if seats == 1 else ordered[min(seats, len(ordered)) - 1]
    margin = margin_of(votes, seats)
    threshold = RECOUNT_FRACTION * reference
    return Recount(margin, reference, threshold, margin <= threshold, False, source)


# ---------------------------------------------------------------------------
# CORLA's contest list


@dataclass(frozen=True)
class CorlaContest:
    """One row of CORLA's contest list (``ContestsListRound*.csv``), with the counties holding the contest."""

    name: str
    audit_reason: str = ""
    seats: int = 1
    #: The contest's domain for CORLA: every card of its counties.
    ballot_cards: int | None = None
    #: The cards carrying the contest.
    contest_cards: int | None = None
    min_margin: int | None = None
    risk_limit: float | None = None
    #: Draws CORLA has counted for the contest (``audited_sample_count``).
    audited: int = 0
    discrepancies: Discrepancies = Discrepancies()
    gamma: float = GAMMA
    #: How many counties hold it, from CORLA's contest list by county; None when not known.
    counties: int | None = None
    #: The counties holding it, as CORLA's contest list by county names them (empty when not known).
    county_names: tuple[str, ...] = ()

    @property
    def risk(self) -> float | None:
        """CORLA's measured risk, from its own numbers: Kaplan-Markov over its domain (None without a margin or domain)."""
        if not self.ballot_cards or self.min_margin is None:
            return None
        d = self.discrepancies
        return km_risk(
            self.audited,
            self.min_margin / self.ballot_cards,
            d.o1,
            d.o2,
            d.u1,
            d.u2,
            self.gamma,
        )


def _int(text: str | None) -> int | None:
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return None


def _float(text: str | None) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def corla_contest(row: Mapping[str, str], counties: int | None = None) -> CorlaContest:
    """A contest list row (columns as CORLA names them) as a `CorlaContest`."""
    return CorlaContest(
        name=row.get("contest_name", "").strip(),
        audit_reason=row.get("audit_reason", "").strip(),
        seats=_int(row.get("winners_allowed")) or 1,
        ballot_cards=_int(row.get("ballot_card_count")),
        contest_cards=_int(row.get("contest_ballot_card_count")),
        min_margin=_int(row.get("min_margin")),
        risk_limit=_float(row.get("risk_limit")),
        audited=_int(row.get("audited_sample_count")) or 0,
        discrepancies=Discrepancies(
            _int(row.get("one_vote_over_count")) or 0,
            _int(row.get("two_vote_over_count")) or 0,
            _int(row.get("one_vote_under_count")) or 0,
            _int(row.get("two_vote_under_count")) or 0,
        ),
        gamma=_float(row.get("gamma")) or GAMMA,
        counties=counties,
    )


# ---------------------------------------------------------------------------
# What the menu is computed from


#: A card's comparison for one contest: the choices the CVR shows and the choices read, or None
#: for what was read when it could not be matched to the contest's choices (counted as a
#: two-vote overstatement, the conservative reading).
Comparison = tuple[tuple[str, ...], tuple[str, ...] | None]


@dataclass
class MenuInputs:
    """Everything `build_menu` needs for one county, gathered by `gather` (or built by hand in tests).

    Keys by card and contest are ``(id_key(imprinted ID), menu_key(contest name))``.
    """

    county: str
    cvr_sha256: str
    votes: CvrVotes
    #: CORLA's contest list, by `contest_key`.
    corla: dict[str, CorlaContest] = field(default_factory=dict)
    #: Whether CORLA's contest list by county was available (else local and regional are assumed by name).
    counties_known: bool = False
    #: Per `contest_key`: the votes per choice from the results, and their source.
    results: dict[str, tuple[dict[str, int], str]] = field(default_factory=dict)
    #: CORLA's draws on this county's cards: imprinted IDs, a card drawn twice listed twice.
    draws: list[str] = field(default_factory=list)
    draws_source: str = "no RLA draws known yet"
    #: Each card's comparison per contest, and where they came from.
    comparisons: dict[tuple[str, str], list[Comparison]] = field(default_factory=dict)
    comparisons_source: str = "none recorded"
    #: Whether ``comparisons`` are CORLA's (then a drawn card missing from them is not yet examined).
    corla_comparisons: bool = False
    #: Observers' comparisons, used for the domain draws' cards.
    observed: dict[tuple[str, str], list[Comparison]] = field(default_factory=dict)
    #: The domain draws made and counted (not excluded or discarded), for this CVR.
    domain_draws: list[DomainDrawRecord] = field(default_factory=list)
    risk_limit: float = DEFAULT_RISK_LIMIT
    risk_limit_source: str = "assumed (no contest list or target list gives it)"
    #: The county's allowed total less the ballots already sent, or None without an allowed total.
    budget: int | None = None
    budget_source: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Contests


@dataclass(frozen=True)
class Tag:
    """One of a contest's properties shown as a labelled badge: its words, and the badge's kind on the page."""

    word: str
    #: `corla_observer.web` badge kind: ``info`` (unseen), ``high`` (flagged), ``tag`` (the notes).
    kind: str = "tag"

    def __str__(self) -> str:
        return self.word


#: A row of a menu line: text and tags in order (`line_rows`).
Row = list[str | Tag]


@dataclass
class ContestFacts:
    """One contest of the county's CVR, as the menu sees it.

    Its properties are independent of one another: *unseen* (no card of the official sample
    carried it), *flagged* (a discrepancy was found on its cards), *thin* or *settled* (its risk
    against the limit; neither for an uncontested contest, which has no risk), and the notes
    *uncontested* and *recount*.
    """

    name: str
    #: Its index among the CVR's contests.
    index: int
    scope: str
    scope_source: str
    seats: int
    choices: list[str]
    #: Cards of the CVR carrying it.
    cvr_cards: int
    corla: CorlaContest | None
    margin: int | None
    margin_source: str
    recount: Recount
    #: CORLA's draws on cards carrying it, with multiplicity, and the discrepancies seen on them.
    official_draws: int = 0
    official: Discrepancies = Discrepancies()
    #: Of the official draws, those with no comparison yet (assumed to show no discrepancy).
    unexamined: int = 0
    #: Domain draws from its domain, and the discrepancies observers recorded on them.
    extra_draws: int = 0
    extra: Discrepancies = Discrepancies()
    extra_unexamined: int = 0
    #: Cards of the official sample that carried it: in this county for a local contest; for a
    #: regional one, CORLA's count over all its counties when its contest list gives one.
    official_sampled: int = 0
    limit: float = DEFAULT_RISK_LIMIT
    #: CORLA's name for it when the CVR spells it differently (`match_corla`), else "".
    corla_name: str = ""

    @property
    def local(self) -> bool:
        return self.scope == LOCAL

    @property
    def domain_cards(self) -> int:
        """Its cards in the county: for a local contest the larger of the CVR's count and CORLA's."""
        if self.local and self.corla is not None and self.corla.contest_cards:
            return max(self.cvr_cards, self.corla.contest_cards)
        return self.cvr_cards

    @property
    def share(self) -> float | None:
        """For a regional contest, the county's share of its cards (CVR count over CORLA's), or None."""
        if self.corla is None or not self.corla.contest_cards:
            return None
        return min(1.0, self.cvr_cards / self.corla.contest_cards)

    @property
    def diluted_margin(self) -> float:
        return (
            self.margin / self.domain_cards
            if self.margin and self.domain_cards
            else 0.0
        )

    @property
    def draws(self) -> int:
        """Draws counted in the pooled risk: CORLA's on its cards and the domain draws."""
        return self.official_draws + self.extra_draws

    @property
    def discrepancies(self) -> Discrepancies:
        return self.official + self.extra

    # The properties

    @property
    def unseen(self) -> bool:
        """No card of the official sample carried it (domain draws do not change this)."""
        return self.official_sampled == 0

    @property
    def flagged(self) -> bool:
        """A discrepancy was found on its cards (official draws or domain draws)."""
        return self.discrepancies.total > 0

    @property
    def uncontested(self) -> bool:
        return self.recount.uncontested

    @property
    def recount_margin(self) -> bool:
        """Its margin is within the automatic recount threshold (a note, for information)."""
        return self.recount.recount

    @property
    def tie(self) -> bool:
        """A local contested contest whose risk no number of draws lowers (a margin of zero or less)."""
        return self.eligible and self.diluted_margin <= 0

    @property
    def thin(self) -> bool:
        risk = self.risk
        return risk is not None and risk > self.limit

    @property
    def settled(self) -> bool:
        risk = self.risk
        return risk is not None and risk <= self.limit

    @property
    def level(self) -> str:
        """``thin``, ``settled``, or "" when it has no risk (uncontested, or a regional contest without CORLA's)."""
        return THIN if self.thin else SETTLED if self.settled else ""

    @property
    def tag_list(self) -> list[Tag]:
        """Its properties other than thin and settled, as tags: unseen, "flagged: o1 x1", uncontested, recount, tie."""
        found = []
        if self.unseen:
            found.append(Tag(UNSEEN, "info"))
        if self.flagged:
            found.append(Tag(f"{FLAGGED}: {self.discrepancies.tag_text()}", "high"))
        if self.uncontested:
            found.append(Tag(UNCONTESTED))
        if self.recount_margin:
            found.append(Tag(RECOUNT))
        if self.tie:
            found.append(Tag(TIE))
        return found

    @property
    def tags(self) -> list[str]:
        """The tags' words: ["unseen", "flagged: o1 x1"]."""
        return [t.word for t in self.tag_list]

    @property
    def margin_share(self) -> float | None:
        """Its margin as a share of its own cards in the county (the diluted margin), or None without a margin."""
        return (
            self.diluted_margin
            if self.margin is not None and self.domain_cards
            else None
        )

    @property
    def margin_text(self) -> str:
        """ "margin 34 votes, 7.9% of its 428 cards"."""
        if self.margin is None or not self.domain_cards:
            return "margin not known"
        return f"margin {self.margin:,} vote{'s' if self.margin != 1 else ''}, {100 * self.diluted_margin:.1f}% of its {self.domain_cards:,} cards"

    @property
    def seen_text(self) -> str:
        """How domain draws saw it, if they did: "seen by 3 domain draws"."""
        n = self.extra_draws
        return f"seen by {n} domain draw{'s' if n != 1 else ''}" if n else ""

    @property
    def label(self) -> str:
        """Its level, tags and domain draws in one phrase: "thin unseen seen by 5 domain draws"."""
        return " ".join(x for x in (self.level, *self.tags, self.seen_text) if x)

    @property
    def eligible(self) -> bool:
        """A local contest with an outcome for the pooled risk to confirm (contested; a recount margin included)."""
        return self.local and not self.uncontested

    @property
    def allocatable(self) -> bool:
        """Eligible and not a tie: draws can lower its risk, so the suggestion considers it."""
        return self.eligible and not self.tie

    def on_menu(self, cutoff: float = REGIONAL_SHARE_CUTOFF) -> bool:
        """A local contest that is unseen, thin or flagged; a regional one that is unseen or thin and not left off."""
        if self.local:
            return self.unseen or self.thin or self.flagged
        return (self.unseen or self.thin) and not left_off(self, cutoff)

    def risk_after(self, extra: int = 0) -> float | None:
        """The pooled risk after ``extra`` more draws from its domain (assumed to show no discrepancy); for a
        regional contest CORLA's risk; None when there is no risk to give (uncontested)."""
        if not self.local:
            return (
                self.corla.risk
                if self.corla is not None and not self.uncontested
                else None
            )
        if not self.eligible:
            return None
        d = self.discrepancies
        return km_risk(self.draws + extra, self.diluted_margin, d.o1, d.o2, d.u1, d.u2)

    @property
    def risk(self) -> float | None:
        return self.risk_after(0)

    def draws_to(self, target: float) -> int | None:
        """More draws from its domain bringing its pooled risk to ``target`` or under; None for a tie."""
        if not self.eligible:
            return None
        d = self.discrepancies
        return draws_to(target, self.diluted_margin, self.draws, d.o1, d.o2, d.u1, d.u2)


def _is_regional_by_name(name: str) -> bool:
    from corla_observer.targets import is_house, statewide_by_name

    return statewide_by_name(name) or is_house(name)


def _discrepancy(
    comparisons: Sequence[Comparison], winners: Collection[str], losers: Collection[str]
) -> int:
    """The largest overstatement among a card's comparisons (an unmatched reading counts as 2)."""
    return max(
        2 if read is None else overstatement(cvr, read, winners, losers)
        for cvr, read in comparisons
    )


_STOP_WORDS = frozenset({"of", "the", "and", "ballot"})


def _words(key: str) -> frozenset[str]:
    return frozenset(w for w in key.split() if w not in _STOP_WORDS)


def match_corla(
    keys: Iterable[str],
    corla: Mapping[str, CorlaContest],
    county: str | None = None,
    cutoff: float = 0.9,
) -> dict[str, str]:
    """CORLA's key for each CVR contest key (`menu_key`) that CORLA's list does not have as it is.

    Counties' CVRs sometimes spell a contest otherwise: "Ciyt of Craig", "Town of DinosaurBallot
    Issue 2A", "BALLOT ISSUE 6AB" for "Redlands 360 Metropolitan District No. 1 Ballot Issue 6AB",
    "Colorado Supreme Court Justice - Hart" for "Justice of the Colorado Supreme Court - Hart". Such
    a key is matched to a CORLA contest of ``county`` (any, when CORLA's list does not say which
    counties hold it) that the CVR does not otherwise name, by the first rule that gives exactly
    one: the same with the spaces and the word "ballot" taken out; the closest spelling, at least
    ``cutoff`` alike (`difflib.SequenceMatcher`) and closer than any other; the same words in any
    order (but "of", "the", "and" and "ballot"); one's words all among the other's, with the same
    last word (the issue's letter, the judge's name). Two CVR contests matched to one CORLA contest
    are both left unmatched. Keys with no match are left out.
    """
    wanted = list(keys)
    taken = set(wanted)

    def squeeze(key: str) -> str:
        return key.replace(" ", "").replace("ballot", "")

    free = {
        k: squeeze(k)
        for k, c in corla.items()
        if k not in taken
        and (county is None or not c.county_names or county in c.county_names)
    }
    by_compact = {c: k for k, c in free.items()}
    found: dict[str, str] = {}
    for key in wanted:
        if key in corla:
            continue
        compact = squeeze(key)
        if compact in by_compact:
            found[key] = by_compact[compact]
            continue
        scored = sorted(
            (
                (difflib.SequenceMatcher(None, compact, c).ratio(), k)
                for k, c in free.items()
            ),
            reverse=True,
        )
        if (
            scored
            and scored[0][0] >= cutoff
            and (len(scored) == 1 or scored[0][0] > scored[1][0])
        ):
            found[key] = scored[0][1]
            continue
        words = _words(key)
        same = [k for k in free if _words(k) == words]
        if len(same) == 1:
            found[key] = same[0]
            continue
        last = key.split()[-1] if key.split() else ""
        within = [
            k for k in free
            if k.split() and k.split()[-1] == last and (words <= _words(k) or _words(k) <= words)
        ]  # fmt: skip
        if len(words) >= 2 and len(within) == 1:
            found[key] = within[0]
    counts: dict[str, int] = {}
    for v in found.values():
        counts[v] = counts.get(v, 0) + 1
    return {k: v for k, v in found.items() if counts[v] == 1}


def contest_facts(inputs: MenuInputs) -> list[ContestFacts]:
    """Every contest of the county's CVR, with its scope, margin, draws, discrepancies, status and notes."""
    votes = inputs.votes
    drawn_rows: list[int] = []
    for imprinted in inputs.draws:
        row = votes.row_of(imprinted)
        if row is not None:
            drawn_rows.append(row)
    extra_by_contest: dict[str, list[str]] = {}
    for record in inputs.domain_draws:
        if record.cvr_sha256 != inputs.cvr_sha256:
            continue
        for name in record.contests:
            extra_by_contest.setdefault(menu_key(name), []).extend(record.drawn_ids)
    matched = match_corla(
        [menu_key(cv.name) for cv in votes.contests.values()],
        inputs.corla,
        inputs.county,
    )
    facts = []
    for index, cv in sorted(votes.contests.items()):
        key = menu_key(cv.name)
        ckey = matched.get(key, key)
        corla = inputs.corla.get(ckey)
        if inputs.counties_known and corla is not None and corla.counties is not None:
            scope = LOCAL if corla.counties == 1 else REGIONAL
            scope_source = "CORLA's contest list by county"
        elif inputs.counties_known:
            scope = REGIONAL if _is_regional_by_name(cv.name) else LOCAL
            scope_source = "assumed by name (not in CORLA's contest list by county)"
        else:
            scope = REGIONAL if _is_regional_by_name(cv.name) else LOCAL
            scope_source = "assumed by name (no CORLA contest list by county)"
        seats = corla.seats if corla is not None and corla.seats else cv.vote_for
        cvr_votes = dict(zip(cv.choices, cv.votes, strict=True))
        result = inputs.results.get(key) or inputs.results.get(ckey)
        tally, tally_source = (
            (result[0], result[1]) if result else (cvr_votes, "the county's CVR")
        )
        recount = recount_status(tally, seats, cv.choices, tally_source)
        if (
            corla is not None
            and corla.min_margin is not None
            and (corla.min_margin > 0 or not result)
        ):
            margin: int | None = corla.min_margin
            margin_source = "CORLA's contest list"
        else:
            margin, margin_source = recount.margin, tally_source
        ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
        winners = [c for c, _ in ranked[:seats]]
        losers = [c for c in cv.choices if _norm(c) not in {_norm(w) for w in winners}]
        fact = ContestFacts(
            name=cv.name, index=index, scope=scope, scope_source=scope_source, seats=seats, choices=list(cv.choices),
            cvr_cards=cv.carry.bit_count(), corla=corla, margin=margin, margin_source=margin_source, recount=recount,
            limit=inputs.risk_limit,
        )  # fmt: skip
        if ckey != key and corla is not None:
            fact.corla_name = corla.name
        official = Discrepancies()
        for row in drawn_rows:
            if not cv.carry >> row & 1:
                continue
            fact.official_draws += 1
            found = inputs.comparisons.get((id_key(votes.imprinted(row)), ckey))
            if found:
                official = official + Discrepancies.of(
                    _discrepancy(found, winners, losers)
                )
            elif inputs.corla_comparisons:
                fact.unexamined += 1
        fact.official = official
        extra = Discrepancies()
        for imprinted in extra_by_contest.get(key, []):
            fact.extra_draws += 1
            seen = inputs.observed.get((id_key(imprinted), key))
            if seen:
                extra = extra + Discrepancies.of(_discrepancy(seen, winners, losers))
            else:
                fact.extra_unexamined += 1
        fact.extra = extra
        if fact.local:
            fact.official_sampled = fact.official_draws
        else:
            fact.official_sampled = (
                corla.audited
                if corla is not None and corla.audited
                else fact.official_draws
            )
        facts.append(fact)
    return facts


def left_off(fact: ContestFacts, cutoff: float = REGIONAL_SHARE_CUTOFF) -> bool:
    """A regional contest that is unseen or thin, of which the county holds under ``cutoff`` of the cards."""
    share = fact.share
    return (
        not fact.local
        and (fact.unseen or fact.thin)
        and share is not None
        and share < cutoff
    )


# ---------------------------------------------------------------------------
# Sampling domains


@dataclass(frozen=True)
class DomainGroup:
    """Local contests carried by exactly the same cards of the county: one sampling domain.

    ``covered`` are the other contests on some of these cards: a draw from the domain lands on
    them too, but it is not a uniform draw from their own domain, so it gives them coverage only.
    """

    contests: tuple[str, ...]
    indexes: tuple[int, ...]
    cards: int
    covered: tuple[int, ...]

    @property
    def key(self) -> str:
        """The domain's name in forms and allocations: its contests' indexes."""
        return ",".join(str(i) for i in self.indexes)


def domain_groups(votes: CvrVotes, local: Iterable[int]) -> list[DomainGroup]:
    """Group the local contests (CVR indexes) by identical card sets; a contest on no card gets no group."""
    by_mask: dict[int, list[int]] = {}
    for index in local:
        cv = votes.contests.get(index)
        if cv is not None and cv.carry:
            by_mask.setdefault(cv.carry, []).append(index)
    groups = []
    for mask, members in by_mask.items():
        covered = tuple(
            sorted(
                i
                for i, cv in votes.contests.items()
                if i not in members and cv.carry & mask
            )
        )
        indexes = tuple(sorted(members))
        groups.append(
            DomainGroup(
                tuple(votes.contests[i].name for i in indexes),
                indexes,
                mask.bit_count(),
                covered,
            )
        )
    return sorted(groups, key=lambda g: g.indexes)


# ---------------------------------------------------------------------------
# The menu


def domain_title(name: str, count: int) -> str:
    """A domain's name: its riskiest contest's, "and N others" when it holds more than one."""
    others = count - 1
    return name + (
        f" and {others} other{'s' if others != 1 else ''}" if others > 0 else ""
    )


@dataclass(frozen=True)
class MenuLine:
    """One domain on the menu: its riskiest contest and the others, now and after more draws."""

    group: DomainGroup
    title: str
    #: The domain's local contests, and how many are on the menu (unseen, thin or flagged).
    members: tuple[ContestFacts, ...]
    on_menu: int
    #: The riskiest contest draws can help (contested, not a tie), and its risk now and after more draws.
    riskiest: ContestFacts | None
    now: float | None
    after: tuple[float | None, ...]
    needed: int | None
    #: The other contests draws can help, how many draws bring them all under the limit.
    others: tuple[ContestFacts, ...]
    others_needed: int | None
    uncontested: tuple[ContestFacts, ...]
    ties: tuple[ContestFacts, ...]
    #: Other contests on these cards (coverage only), and how many of them are on the menu themselves.
    #: The contests on these cards (coverage only, not drawn for) that are on the menu themselves.
    covered_on_menu: tuple[ContestFacts, ...]
    #: Domain draws already made from it, counted in the risks.
    drawn: int = 0


def _members(
    group: DomainGroup, by_index: Mapping[int, ContestFacts]
) -> list[ContestFacts]:
    return [by_index[i] for i in group.indexes if i in by_index]


def _allocatable(
    group: DomainGroup, by_index: Mapping[int, ContestFacts]
) -> list[ContestFacts]:
    found = [f for f in _members(group, by_index) if f.allocatable]
    return sorted(found, key=lambda f: (-(f.risk or 0.0), f.index))


def _highest(members: Sequence[ContestFacts], extra: int) -> float | None:
    values = [r for m in members if (r := m.risk_after(extra)) is not None]
    return max(values) if values else None


def _needed(members: Sequence[ContestFacts], target: float) -> int | None:
    needs = [m.draws_to(target) for m in members]
    if not needs or any(n is None for n in needs):
        return None
    return max(n for n in needs if n is not None)


def on_menu(fact: ContestFacts, cutoff: float = REGIONAL_SHARE_CUTOFF) -> bool:
    """`ContestFacts.on_menu`."""
    return fact.on_menu(cutoff)


def menu_lines(
    groups: Sequence[DomainGroup],
    facts: Sequence[ContestFacts],
    steps: Sequence[int] = STEPS,
    cutoff: float = REGIONAL_SHARE_CUTOFF,
) -> list[MenuLine]:
    """One line per domain any of whose local contests is unseen, thin or flagged; riskiest first."""
    by_index = {f.index: f for f in facts}
    lines = []
    for group in groups:
        members = _members(group, by_index)
        menu_members = [f for f in members if f.on_menu(cutoff)]
        if not menu_members:
            continue
        units = _allocatable(group, by_index)
        top, rest = (units[0], units[1:]) if units else (None, [])
        limit = members[0].limit
        ordered = sorted(
            members, key=lambda f: (f.risk is None, -(f.risk or 0.0), f.index)
        )
        lines.append(
            MenuLine(
                group=group,
                title=domain_title((top or ordered[0]).name, len(members)),
                members=tuple(ordered),
                on_menu=len(menu_members),
                riskiest=top,
                now=top.risk if top else None,
                after=tuple(top.risk_after(k) for k in steps) if top else (),
                needed=top.draws_to(limit) if top else None,
                others=tuple(rest),
                others_needed=_needed(rest, limit) if rest else None,
                uncontested=tuple(f for f in members if f.uncontested),
                ties=tuple(f for f in members if f.tie),
                covered_on_menu=tuple(
                    by_index[i]
                    for i in group.covered
                    if i in by_index and by_index[i].on_menu(cutoff)
                ),
                drawn=max((m.extra_draws for m in members), default=0),
            )
        )
    return sorted(
        lines, key=lambda m: (m.now is None, -(m.now or 0.0), m.group.indexes)
    )


def pct(value: float | None) -> str:
    """A risk as a percentage: one decimal, "<0.1%" below 0.05%, "n/a" for none."""
    if value is None:
        return "n/a"
    if value < 0.0005:
        return "<0.1%"
    return f"{100 * value:.1f}%"


def limit_text(limit: float) -> str:
    """The risk limit as a percentage ("3%")."""
    return f"{100 * limit:g}%"


TIE_TEXT = "tie: each card checks the CVR's count; the risk can't fall"
#: The menu contests a line names that its domain's cards also land on; the rest are counted.
COVERAGE_NAMES = 5


def _count_tags(contests: Sequence[ContestFacts], also: bool) -> Row:
    """How many of ``contests`` have each tag: ["7 also ", Tag("unseen"), ", 1 ", Tag("recount")]."""
    row: Row = []
    counts = (
        (UNSEEN, "info", sum(f.unseen for f in contests)),
        (FLAGGED, "high", sum(f.flagged for f in contests)),
        (RECOUNT, "tag", sum(f.recount_margin for f in contests)),
        (TIE, "tag", sum(f.tie for f in contests)),
    )
    for word, kind, count in counts:
        if count:
            row += [
                ", " if row else "",
                f"{count} {'also ' if also and word == UNSEEN else ''}",
                Tag(word, kind),
            ]
    return [x for x in row if x != ""]


def line_rows(line: MenuLine, steps: Sequence[int] = STEPS) -> list[Row]:
    """The menu line as the coordinator reads it, a row each: the domain; the riskiest contest with its margin;
    the risk after more draws; the others, counted by tag; the uncontested contests; the ties; the menu contests
    the domain's cards also land on; draws already made. Tags are `Tag`s, shown as badges on the page."""
    size = len(line.members)
    limit = limit_text(line.members[0].limit) if line.members else ""
    rows: list[Row] = [
        [
            f"{line.title}  ·  {line.group.cards:,} cards, {size} contest{'s' if size != 1 else ''} ({line.on_menu} on the menu)"
        ]
    ]
    top = line.riskiest
    if top is not None:
        rows.append(
            [
                f"Riskiest: {top.name}   {pct(line.now)}  {top.level}",
                *(x for t in top.tag_list for x in (" ", t)),
            ]
            + ([f"  {top.seen_text}"] if top.seen_text else [])
            + [f"; {top.margin_text}"]
        )
        text = f"after {' / '.join(str(k) for k in steps)} draws: {' / '.join(pct(r) for r in line.after)}"
        if line.needed:
            text += f"; under {limit} after {line.needed} draw{'s' if line.needed != 1 else ''}"
        rows.append([text])
    if line.others:
        n = len(line.others)
        counted = _count_tags(line.others, top is not None and top.unseen)
        thin = sum(o.thin for o in line.others)
        level = (
            "all thin"
            if thin == n
            else "all settled"
            if thin == 0
            else f"{thin} thin, {n - thin} settled"
        )
        if line.others_needed == 0:
            need = f"; all already under {limit}"
        elif line.others_needed is not None:
            need = f"; all under {limit} after {line.others_needed} draw{'s' if line.others_needed != 1 else ''}"
        else:
            need = ""
        rows.append(
            [
                f"Others ({n}): ",
                *counted,
                (", " if counted else "") + level + need + ".",
            ]
        )
    if line.uncontested:
        unseen = sum(u.unseen for u in line.uncontested)
        row: Row = ["Also: "]
        if unseen:
            row += [f"{unseen} ", Tag(UNCONTESTED), " ", Tag(UNSEEN, "info")]
        if len(line.uncontested) - unseen:
            row += [
                (", " if unseen else "") + f"{len(line.uncontested) - unseen} ",
                Tag(UNCONTESTED),
                ", seen",
            ]
        if unseen:
            row.append(" (one card checks that the CVR records it)")
        rows.append(row)
    for tie in line.ties:
        rows.append([f"{tie.name}: ", Tag(TIE), f": {TIE_TEXT.removeprefix('tie: ')}"])
    if line.covered_on_menu:
        n = len(line.covered_on_menu)
        shown = [f.name for f in line.covered_on_menu[:COVERAGE_NAMES]]
        more = n - len(shown)
        names = ", ".join(shown) + (f" and {more} more" if more else "")
        rows.append(
            [
                f"Also lands on {n} menu contest{'s' if n != 1 else ''}: {names} (coverage only)."
            ]
        )
    if line.drawn:
        rows.append(
            [
                f"Includes {line.drawn} domain draw{'s' if line.drawn != 1 else ''} already made."
            ]
        )
    return rows


def line_parts(line: MenuLine, steps: Sequence[int] = STEPS) -> list[str]:
    """`line_rows` as plain text, a string per row (tags as their words)."""
    return ["".join(str(x) for x in row) for row in line_rows(line, steps)]


def line_text(line: MenuLine, steps: Sequence[int] = STEPS) -> str:
    """The menu line as plain text, its rows indented under the domain."""
    first, *rest = line_parts(line, steps)
    return "\n".join([first, *(f"  {r}" for r in rest)])


# ---------------------------------------------------------------------------
# The suggestion


@dataclass
class Allocation:
    """Draws per domain (`DomainGroup.key`) for a budget, and the highest pooled risk before and after (ties left out)."""

    strategy: str
    budget: int
    draws: dict[str, int]
    before: float
    after: float
    unspent: int
    #: The domains given their one first draw for an unseen local contest.
    seeded: list[str] = field(default_factory=list)


def allocate(
    groups: Sequence[DomainGroup],
    facts: Sequence[ContestFacts],
    budget: int,
    strategy: str = MINIMAX,
    tolerances: Sequence[float] = TOLERANCES,
) -> Allocation:
    """Spread ``budget`` draws over the domains, each counted by its riskiest contested contest's pooled risk.

    First each domain holding an unseen local contest or a tie that no domain draw has reached yet
    (uncontested ones included: one card checks that the CVR records the contest; for a tie, the
    CVR's count is the whole question) gets one draw,
    the largest domains first if the budget is short. The rest goes by ``strategy``: ``minimax``
    repeatedly gives the domain with the highest risk the draws that bring it down to the next
    highest (or to the limit), the rest of the budget when that is too many; ``ladder`` goes down
    the tolerances (90%, 50%, 20%, 10%, then the limit) and at each brings the cheapest domains
    under it first, leaving one it cannot afford as it is. Both stop when every contest is at or
    under the limit; draws left are unspent. Ties (their risk can't fall) are left out of the
    highest risk minimax and ladder lower, and that is reported.
    """
    by_index = {f.index: f for f in facts}
    units = {g.key: m for g in groups if (m := _allocatable(g, by_index))}
    limit = next(iter(facts)).limit if facts else DEFAULT_RISK_LIMIT
    draws = {key: 0 for key in units}
    remaining = max(0, budget)
    unseen = [
        g for g in groups
        if any((f.unseen or f.tie) and f.extra_draws == 0 for f in _members(g, by_index))
    ]  # fmt: skip
    seeded = []

    def risk(key: str) -> float:
        return _highest(units[key], draws[key]) or 0.0

    before = max((risk(k) for k in units), default=0.0)
    for group in sorted(unseen, key=lambda g: (-g.cards, g.indexes)):
        if remaining <= 0:
            break
        draws[group.key] = draws.get(group.key, 0) + 1
        seeded.append(group.key)
        remaining -= 1
    frozen: set[str] = set()
    if strategy == LADDER:
        for level in sorted({*tolerances, limit}, reverse=True):
            if level < limit:
                continue
            costs = []
            for key in units:
                if key in frozen or risk(key) <= level:
                    continue
                needed = _needed(units[key], level)
                if needed is None:
                    frozen.add(key)
                    continue
                costs.append((max(1, needed - draws[key]), key))
            for cost, key in sorted(costs):
                if cost <= remaining:
                    draws[key] += cost
                    remaining -= cost
                else:
                    frozen.add(key)
    elif strategy == MINIMAX:
        while remaining > 0:
            ordered = sorted(
                (k for k in units if k not in frozen), key=lambda k: (-risk(k), k)
            )
            if not ordered or risk(ordered[0]) <= limit:
                break
            top = ordered[0]
            target = max(limit, risk(ordered[1]) if len(ordered) > 1 else limit)
            needed = _needed(units[top], target)
            if needed is None:
                frozen.add(top)
                continue
            step = min(remaining, max(1, needed - draws[top]))
            draws[top] += step
            remaining -= step
    else:
        raise ValueError(f"unknown strategy {strategy!r}")
    after = max((risk(k) for k in units), default=0.0)
    return Allocation(
        strategy,
        budget,
        {k: v for k, v in draws.items() if v},
        before,
        after,
        remaining,
        seeded,
    )


# ---------------------------------------------------------------------------
# The whole menu


#: What the page and the command say about the assumption behind every pooled risk.
STYLE_NOTE = (
    "The pooled risks assume the CVR's card-style information (which cards carry which contest) is accurate, "
    "although the CVR is what is being audited. Formal checks of that style information are not made; only the "
    "CVR's contest card counts are compared with CORLA's."
)
#: What the page and the command say about ties and the suggestion.
TIE_NOTE = (
    "A tie (a margin of zero) gets a first draw like an unseen contest: each card checks the CVR's count, which "
    "for a tie is the whole question, though its risk can't fall. It is left out of the highest risk that minimax "
    "and ladder lower, and that the suggestion reports."
)


@dataclass
class Menu:
    """One county's targeting menu: the facts, the domains, the lines, and what was left off."""

    inputs: MenuInputs
    facts: list[ContestFacts]
    groups: list[DomainGroup]
    lines: list[MenuLine]
    steps: tuple[int, ...] = STEPS
    cutoff: float = REGIONAL_SHARE_CUTOFF

    @property
    def limit(self) -> float:
        return self.inputs.risk_limit

    def local(self) -> list[ContestFacts]:
        return [f for f in self.facts if f.local]

    @property
    def regional(self) -> list[ContestFacts]:
        return [f for f in self.facts if not f.local]

    @property
    def off_menu(self) -> list[ContestFacts]:
        """Local contests in domains not on the menu."""
        listed = {f.index for line in self.lines for f in line.members}
        return [f for f in self.local() if f.index not in listed]

    @property
    def left_off(self) -> list[ContestFacts]:
        return [f for f in self.facts if left_off(f, self.cutoff)]

    def totals(self) -> list[tuple[str, int]]:
        """The county's local contests counted by property: unseen, flagged, thin, settled, uncontested, recount, tie."""
        local = self.local()
        return [
            (UNSEEN, sum(f.unseen for f in local)),
            (FLAGGED, sum(f.flagged for f in local)),
            (THIN, sum(f.thin for f in local)),
            (SETTLED, sum(f.settled for f in local)),
            (UNCONTESTED, sum(f.uncontested for f in local)),
            (RECOUNT, sum(f.recount_margin for f in local)),
            (TIE, sum(f.tie for f in local)),
        ]

    def totals_text(self) -> str:
        """The totals in one line: "12 local contests: 3 unseen, 1 flagged, 4 thin, 8 settled, ..."."""
        parts = [f"{n} {w}" for w, n in self.totals()]
        return f"{len(self.local())} local contests: " + ", ".join(parts)

    def group(self, key: str) -> DomainGroup | None:
        return next((g for g in self.groups if g.key == key), None)

    def allocate(self, budget: int, strategy: str = MINIMAX) -> Allocation:
        return allocate(self.groups, self.facts, budget, strategy)

    def card_checks(self) -> list[tuple[ContestFacts, int]]:
        """Local contests whose CVR card count differs from CORLA's by more than 1%: the contest and CORLA's count."""
        found = []
        for f in self.local():
            if (
                f.corla is not None
                and f.corla.contest_cards
                and abs(f.cvr_cards - f.corla.contest_cards)
                > 0.01 * f.corla.contest_cards
            ):
                found.append((f, f.corla.contest_cards))
        return found

    def sources(self) -> list[str]:
        """Where each number came from, in words."""
        i = self.inputs
        unexamined = sum(f.unexamined for f in self.local())
        lines = [
            f"Risk limit {limit_text(i.risk_limit)}: {i.risk_limit_source}.",
            f"CORLA's draws on this county's cards: {i.draws_source}.",
            f"Discrepancies: {i.comparisons_source}."
            + (
                f" {unexamined} draws on local contests' cards have no comparison yet and are assumed to show none."
                if unexamined
                else ""
            ),
            "Domain draws: discrepancies from the observers' entries; a card not yet examined is assumed to show none.",
            *(
                f"The CVR's “{f.name}” is CORLA's “{f.corla_name}” (matched by spelling)."
                for f in self.facts
                if f.corla_name
            ),
            "Margins: CORLA's contest list where it has the contest, else the results, else the county's CVR; "
            "the recount note uses the results, else the CVR.",
        ]
        return lines + list(i.notes)


def build_menu(
    inputs: MenuInputs,
    steps: Sequence[int] = STEPS,
    cutoff: float = REGIONAL_SHARE_CUTOFF,
) -> Menu:
    """The county's menu from its inputs (pure: no workspace)."""
    facts = contest_facts(inputs)
    groups = domain_groups(inputs.votes, [f.index for f in facts if f.local])
    return Menu(
        inputs,
        facts,
        groups,
        menu_lines(groups, facts, steps, cutoff),
        tuple(steps),
        cutoff,
    )


def menu_text(menu: Menu, budget: int | None = None) -> str:
    """The menu as plain text, for the command line."""
    out = [f"Targeting menu: {menu.inputs.county}", "", STYLE_NOTE, ""]
    out += [f"- {s}" for s in menu.sources()]
    out += [
        "",
        menu.totals_text() + ".",
        "",
        f"Domains on the menu ({len(menu.lines)}):",
    ]
    for n, line in enumerate(menu.lines, start=1):
        first, *rest = line_parts(line, menu.steps)
        out.append(f"{n}. {first}")
        out += [f"     {r}" for r in rest]
        out.append(f"     [domain {line.group.key}]")
    if not menu.lines:
        out.append("Nothing on the menu.")
    if menu.off_menu:
        out += [
            "",
            f"Local contests in domains not on the menu ({len(menu.off_menu)}): "
            + "; ".join(
                f"{f.name} {pct(f.risk)} {f.label}".rstrip() for f in menu.off_menu
            ),
        ]
    gone = menu.left_off
    if gone:
        out += [
            f"Left off, regional with under {menu.cutoff:.0%} of the cards here ({len(gone)}): "
            + "; ".join(f.name for f in gone)
        ]
    for check, corla_cards in menu.card_checks():
        out.append(
            f"Card count: {check.name}: CVR {check.cvr_cards:,}, CORLA {corla_cards:,}."
        )
    chosen = budget if budget is not None else menu.inputs.budget
    if chosen:
        out += [
            "",
            f"Suggestion for {chosen} draws ({menu.inputs.budget_source or 'as asked'}). {TIE_NOTE}",
        ]
        for strategy in (MINIMAX, LADDER):
            result = menu.allocate(chosen, strategy)
            parts = [
                f"{d} from domain {k}"
                for k, d in sorted(result.draws.items(), key=lambda kv: -kv[1])
            ]
            out.append(
                f"- {strategy}: highest local pooled risk {pct(result.before)} -> {pct(result.after)}; "
                + (", ".join(parts) or "nothing to draw")
                + (
                    f" (first one each to {len(result.seeded)} domain(s) with an unseen contest or a tie)"
                    if result.seeded
                    else ""
                )
                + (f"; {result.unspent} unspent" if result.unspent else "")
            )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Gathering the inputs from a workspace


def _corla_lists(
    datamap: DataMap, workspace: Workspace
) -> tuple[dict[str, CorlaContest], bool, float | None]:
    """CORLA's contest list (every round fetched: per contest, the row with the most draws), whether its
    contest list by county was found, and the risk limit it gives."""
    from corla_observer.classify import Classification, canonical_county
    from corla_observer.datamap import role_column, table_rows

    counties: dict[str, set[str]] = {}
    for entry in datamap.used("contest_ids_by_county"):
        try:
            data = workspace.get_bytes(entry.sha256)
        except Exception:  # noqa: BLE001 - a file missing from the workspace: as if not fetched
            continue
        classification = Classification(
            entry.classified_kind, entry.classified_county, entry.evidence
        )
        _, rows = table_rows(data, classification)
        contest_col, county_col = (
            role_column(classification, "contest"),
            role_column(classification, "county"),
        )
        if contest_col is None or county_col is None:
            continue
        for row in rows:
            county = canonical_county(row.get(county_col, ""))
            if county:
                counties.setdefault(menu_key(row.get(contest_col, "")), set()).add(
                    county
                )
    found: dict[str, tuple[CorlaContest, int]] = {}
    limit = None
    for entry in datamap.used("contests_list"):
        try:
            data = workspace.get_bytes(entry.sha256)
        except Exception:  # noqa: BLE001
            continue
        _, rows = table_rows(
            data,
            Classification(
                entry.classified_kind, entry.classified_county, entry.evidence
            ),
        )
        for row in rows:
            key = menu_key(row.get("contest_name", ""))
            if not key:
                continue
            held = counties.get(key)
            contest = replace(
                corla_contest(row, len(held) if held else None),
                county_names=tuple(sorted(held or ())),
            )
            limit = limit or contest.risk_limit
            previous = found.get(key)
            if previous is None or (contest.audited, entry.seq or 0) >= (
                previous[0].audited,
                previous[1],
            ):
                found[key] = (contest, entry.seq or 0)
    return {k: v[0] for k, v in found.items()}, bool(counties), limit


def posted_draws(
    datamap: DataMap,
    workspace: Workspace,
    county: str,
    corla: Mapping[str, CorlaContest],
) -> tuple[list[str], str] | None:
    """CORLA's own draws on the county's cards, from its posted selection lists, with multiplicity; None when it
    has not posted both a selection list and a CVR-to-audit-board comparison.

    A selection list (``contest_selection.csv``) gives each targeted contest's draws as CVR IDs in
    draw order, a card drawn twice listed twice; of the lists of every round, each contest's longest
    is used. Contests with the same domain (CORLA's contest list by county: the same counties) draw
    one sequence, so the longest list of each domain is counted, once. The comparison file maps each
    CVR ID to its county and imprinted ID; a drawn ID it does not list is not placed, and counted.
    """
    selections, comparisons = (
        datamap.used("selection_list"),
        datamap.used("cvr_comparison"),
    )
    if not selections or not comparisons:
        return None
    domains = tuple(
        sorted((k, c.county_names) for k, c in corla.items() if c.county_names)
    )
    cache_key = (
        tuple(e.sha256 for e in selections),
        tuple(e.sha256 for e in comparisons),
        county,
        hash(domains),
    )
    with _DRAWS_LOCK:
        cached = _POSTED.get(cache_key)
    if cached is not None:
        return list(cached[0]), cached[1]
    found = _read_posted(selections, comparisons, workspace, county, corla)
    if found is not None:
        with _DRAWS_LOCK:
            _POSTED[cache_key] = found
            while len(_POSTED) > 8:
                _POSTED.pop(next(iter(_POSTED)))
    return found


_POSTED: dict[tuple[Any, ...], tuple[list[str], str]] = {}


def _read_posted(
    selections: Sequence[Any],
    comparisons: Sequence[Any],
    workspace: Workspace,
    county: str,
    corla: Mapping[str, CorlaContest],
) -> tuple[list[str], str] | None:
    """`posted_draws`, read from the files (``MapEntry``s)."""
    import json

    from corla_observer.classify import Classification, canonical_county
    from corla_observer.datamap import role_column, table_rows

    lists: dict[str, list[str]] = {}
    for entry in selections:
        try:
            data = workspace.get_bytes(entry.sha256)
        except Exception:  # noqa: BLE001
            continue
        classification = Classification(entry.classified_kind, None, entry.evidence)
        _, rows = table_rows(data, classification)
        contest_col = role_column(classification, "contest") or "contest_name"
        ids_col = role_column(classification, "cvr_ids") or "contest_cvr_ids"
        for row in rows:
            try:
                ids = [str(i) for i in json.loads(row.get(ids_col) or "[]")]
            except (json.JSONDecodeError, TypeError):
                continue
            key = menu_key(row.get(contest_col, ""))
            if ids and len(ids) > len(lists.get(key, [])):
                lists[key] = ids
    if not lists:
        return None
    where: dict[str, tuple[str, str]] = {}
    for entry in comparisons:
        try:
            data = workspace.get_bytes(entry.sha256)
        except Exception:  # noqa: BLE001
            continue
        classification = Classification(entry.classified_kind, None, entry.evidence)
        _, rows = table_rows(data, classification)
        id_col = role_column(classification, "cvr_id") or "cvr_id"
        imprinted_col = role_column(classification, "imprinted_id") or "imprinted_id"
        county_col = role_column(classification, "county") or "county_name"
        for row in rows:
            cvr_id = row.get(id_col, "").strip()
            if cvr_id:
                where[cvr_id] = (
                    canonical_county(row.get(county_col, "")) or "",
                    row.get(imprinted_col, "").strip(),
                )
    by_domain: dict[tuple[str, ...], list[str]] = {}
    for key, ids in lists.items():
        found = corla.get(key)
        domain = (
            found.county_names if found is not None and found.county_names else (key,)
        )
        if len(ids) > len(by_domain.get(domain, [])):
            by_domain[domain] = ids
    draws: list[str] = []
    unplaced = 0
    for ids in by_domain.values():
        for cvr_id in ids:
            place = where.get(cvr_id)
            if place is None:
                unplaced += 1
            elif place[0] == county:
                draws.append(place[1])
    source = (
        f"CORLA's posted selection lists ({len(by_domain)} sampling domain(s) of targeted contests, each at its longest "
        f"list of every round), placed by its CVR-to-audit-board comparison: {len(draws)} draws, "
        f"{len({id_key(i) for i in draws})} distinct cards"
    )
    if unplaced:
        source += f"; {unplaced} drawn CVR ID(s) in no comparison row, not placed"
    return draws, source


_DRAWS: dict[tuple[Any, ...], list[tuple[str, str]]] = {}
_DRAWS_LOCK = threading.Lock()


def corla_draws(
    workspace: Workspace, log: EventLog, datamap: DataMap | None, county: str
) -> tuple[list[str], str, list[str]]:
    """CORLA's draws on the county's cards with multiplicity, where they came from, and notes.

    Reproduced from the seed and manifests for every targeted contest whose domain includes the
    county, up to its latest cumulative draw count (`corla_observer.datamap.round_target`, from
    each round's contest list), taken from the latest recorded prediction when it has enough draws
    and predicted afresh otherwise. Without that, the RLA extract's distinct cards, once each.
    """
    from corla_observer.extract import latest_extract
    from corla_observer.predict import PredictionError, latest_prediction, predict
    from corla_observer.sampling import county_key

    notes: list[str] = []
    found = latest_extract(workspace, log, county)
    extract_ids = (
        list(dict.fromkeys(str(b["imprinted_id"]) for b in found[0]["ballots"]))
        if found
        else []
    )
    target = county_key(county)
    specs = [
        s
        for s in (datamap.contests if datamap else [])
        if s.statewide or target in {county_key(c) for c in s.counties}
    ]
    if datamap is None or not specs or datamap.seed is None:
        if extract_ids:
            return (
                extract_ids,
                "the RLA extract's distinct cards, each counted once (without the seed and CORLA's contest lists the draws can't be reproduced; this can only understate n)",
                notes,
            )
        return [], "no RLA draws known yet", notes
    needed = {
        s.name: max([*s.round_targets.values(), s.sample_count or 0]) for s in specs
    }
    # Contests with the same domain (the same counties) draw the same sequence from the same seed: CORLA's draws
    # for that domain are the longest of them, not their sum.
    by_domain: dict[tuple[str, ...], Any] = {}
    for spec in specs:
        domain = (
            ("statewide",)
            if spec.statewide
            else tuple(sorted(county_key(c) for c in spec.counties))
        )
        held = by_domain.get(domain)
        if held is None or needed[spec.name] > needed[held.name]:
            by_domain[domain] = spec
    domains = len(by_domain)
    specs = list(by_domain.values())
    latest = latest_prediction(workspace, log)
    contests: Any = latest[1].get("contests") if latest else None
    predicted = {str(c["name"]): c for c in contests or []}
    ids: list[str] = []
    missing = []
    estimated = []
    for spec in specs:
        item: Any = predicted.get(spec.name)
        count = needed[spec.name]
        if item is not None and (count == 0 or len(item["draws"]) >= count):
            if count == 0 or item.get("estimated"):
                estimated.append(spec.name)
            chosen = sorted(item["draws"], key=lambda d: int(d["index"]))[
                : count or None
            ]
            ids += [
                str(d["imprinted_id"])
                for d in chosen
                if county_key(str(d["county"])) == target
            ]
        elif count:
            missing.append(replace(spec, sample_count=count))
    manifests = tuple(sorted(e.sha256 for e in datamap.used("county_manifest")))
    for spec in missing:
        cache_key = (
            datamap.seed.value,
            spec.name,
            spec.sample_count,
            datamap.overrides_sha256,
            manifests,
        )
        with _DRAWS_LOCK:
            cached = _DRAWS.get(cache_key)
        if cached is None:
            try:
                (contest,) = predict(
                    replace(datamap, contests=[spec]), contests=[spec.name]
                ).contests
            except (PredictionError, ValueError) as exc:
                notes.append(f"Could not reproduce the draws of {spec.name}: {exc}.")
                continue
            cached = [(d.county, d.imprinted_id) for d in contest.draws]
            with _DRAWS_LOCK:
                _DRAWS[cache_key] = cached
                while len(_DRAWS) > 32:
                    _DRAWS.pop(next(iter(_DRAWS)))
        ids += [imprinted for where, imprinted in cached if county_key(where) == target]
    distinct = {id_key(i) for i in ids}
    outside = [i for i in extract_ids if id_key(i) not in distinct]
    if outside:
        notes.append(
            f"{len(outside)} card(s) of the RLA extract are not among the reproduced draws; each is counted once."
        )
        ids += outside
    source = (
        f"reproduced from the seed and the manifests for {domains} sampling domain(s) of targeted contests, each "
        f"drawn as far as its contests' latest round needs: {len(ids)} draws, {len({id_key(i) for i in ids})} "
        "distinct cards"
    )
    if estimated:
        source += f" (sample counts estimated for {len(estimated)} contest(s): no contest list yet)"
    return ids, source, notes


def _choices(text: str) -> tuple[str, ...]:
    """CORLA's choice cell (``"A","B"``) as choices."""
    return tuple(
        c
        for c in (
            part.strip().strip('"').strip()
            for part in re.split(r'"\s*,\s*"', text.strip())
        )
        if c
    )


def _read_comparisons(
    datamap: DataMap, workspace: Workspace, county: str, votes: CvrVotes
) -> tuple[dict[tuple[str, str], list[Comparison]], str | None]:
    """CORLA's CVR-to-audit-board comparison for the county, per card and contest (the last row of each), and the file's name; None when not posted.

    An overvote (more choices than the contest's seats) is no vote.
    """
    from corla_observer.classify import Classification, canonical_county
    from corla_observer.datamap import role_column, table_rows

    entries = sorted(datamap.used("cvr_comparison"), key=lambda e: e.seq or 0)
    if not entries:
        return {}, None
    seats = {menu_key(cv.name): cv.vote_for for cv in votes.contests.values()}
    found: dict[tuple[str, str], list[Comparison]] = {}
    names = []
    for entry in entries:
        try:
            data = workspace.get_bytes(entry.sha256)
        except Exception:  # noqa: BLE001
            continue
        names.append(entry.name)
        classification = Classification(entry.classified_kind, None, entry.evidence)
        _, rows = table_rows(data, classification)
        county_col = role_column(classification, "county") or "county_name"
        contest_col = role_column(classification, "contest") or "contest_name"
        id_col = role_column(classification, "imprinted_id") or "imprinted_id"
        cvr_col = (
            role_column(classification, "cvr_choice") or "choice_per_voting_computer"
        )
        read_col = role_column(classification, "audit_board") or "audit_board_selection"
        for row in rows:
            if canonical_county(row.get(county_col, "")) != county:
                continue
            key = menu_key(row.get(contest_col, ""))
            limit = seats.get(key, 1)
            cvr, read = _choices(row.get(cvr_col, "")), _choices(row.get(read_col, ""))
            found[(id_key(row.get(id_col, "")), key)] = [
                (cvr if len(cvr) <= limit else (), read if len(read) <= limit else ())
            ]
    return found, ", ".join(dict.fromkeys(names)) or None


_COMPARISONS: dict[
    tuple[Any, ...], tuple[dict[tuple[str, str], list[Comparison]], str | None]
] = {}


def corla_comparisons(
    datamap: DataMap, workspace: Workspace, county: str, votes: CvrVotes
) -> tuple[dict[tuple[str, str], list[Comparison]], str | None]:
    """`_read_comparisons`, kept for the last few (files, county, CVR) asked: the posted file can be tens of megabytes."""
    key = (
        tuple(e.sha256 for e in datamap.used("cvr_comparison")),
        county,
        tuple(sorted((c.name, c.vote_for) for c in votes.contests.values())),
    )
    with _DRAWS_LOCK:
        found = _COMPARISONS.get(key)
    if found is None:
        found = _read_comparisons(datamap, workspace, county, votes)
        with _DRAWS_LOCK:
            _COMPARISONS[key] = found
            while len(_COMPARISONS) > 8:
                _COMPARISONS.pop(next(iter(_COMPARISONS)))
    return found


_NO_VOTE = {"no vote", "none", "blank", "undervote", "no votes", "nothing"}


def parse_reading(
    text: str, choices: Sequence[str], seats: int
) -> tuple[str, ...] | None:
    """What an observer heard (or an extract's expected text) as choices: "no vote" and an overvote are none;
    several choices joined by "+", "," or ";"; None when a part matches no choice of the contest."""
    cleaned = " ".join(text.strip().split())
    lowered = cleaned.casefold()
    if (
        not cleaned
        or lowered in _NO_VOTE
        or lowered.startswith("overvote")
        or lowered == "redacted"
    ):
        return ()
    by_norm = {_norm(c): c for c in choices}
    picked = []
    for part in re.split(r"\s*[+,;]\s*", cleaned):
        if not part:
            continue
        match = by_norm.get(_norm(part))
        if match is None:
            close = [c for n, c in by_norm.items() if _norm(part) in n]
            match = close[0] if len(close) == 1 else None
        if match is None:
            return None
        picked.append(match)
    return () if len(picked) > seats else tuple(dict.fromkeys(picked))


def observed_comparisons(
    log: EventLog, county: str, votes: CvrVotes
) -> dict[tuple[str, str], list[Comparison]]:
    """The observers' entries for the county (every list, and lookups), per card and contest: the expected
    choices and what was read in the final reading (second, else first) of each observer; a reading
    not heard clearly is left out. Several observers give several comparisons."""
    from corla_observer.contests import CONTEST_LIKE

    by_key = {menu_key(cv.name): cv for cv in votes.contests.values()}
    latest: dict[
        tuple[str, str, str, int, str, int], tuple[str, str, str, str, str]
    ] = {}
    for event in log.iter_events():
        if event.kind not in CONTEST_LIKE or event.county != county:
            continue
        d = event.data
        position, reading = d.get("position"), d.get("reading")
        if (
            not isinstance(position, int)
            or not isinstance(reading, int)
            or reading not in (1, 2)
        ):
            continue
        place = (
            event.actor,
            str(d.get("extract_sha256") or ""),
            str(event.board or ""),
            position,
            str(d.get("contest") or ""),
            reading,
        )
        latest[place] = (
            str(d.get("imprinted_id") or ""),
            str(d.get("contest") or ""),
            str(d.get("outcome") or ""),
            str(d.get("expected") or ""),
            str(d.get("heard") or ""),
        )
    finals: dict[tuple[str, str, str, int, str], tuple[str, str, str, str, str]] = {}
    for place, value in sorted(latest.items(), key=lambda kv: kv[0][5]):
        finals[place[:5]] = value
    found: dict[tuple[str, str], list[Comparison]] = {}
    for imprinted, contest, outcome, expected, heard in finals.values():
        cv = by_key.get(menu_key(contest))
        if cv is None or outcome == "unclear" or not imprinted:
            continue
        shown = parse_reading(expected, cv.choices, cv.vote_for) or ()
        read = (
            shown
            if outcome == "confirmed"
            else parse_reading(heard, cv.choices, cv.vote_for)
        )
        found.setdefault((id_key(imprinted), menu_key(cv.name)), []).append(
            (shown, read)
        )
    return found


def _results(
    workspace: Workspace, log: EventLog, county: str
) -> dict[str, tuple[dict[str, int], str]]:
    """Votes per choice per contest from the results in hand: the state's (the whole contest, every county), else the county's own file."""
    from corla_observer.enr import load_results

    found: dict[str, tuple[dict[str, int], str]] = {}
    try:
        loaded = load_results(workspace, log)
    except Exception:  # noqa: BLE001 - results that can't be read: as if none
        return found
    state: Results | None = loaded["state"][0] if "state" in loaded else None
    own = next(
        (
            r
            for scope, (r, _) in loaded.items()
            if scope != "state" and contest_key(scope) == contest_key(county)
        ),
        None,
    )
    for results, label in (
        (own, "the county's results"),
        (state, "the state's results"),
    ):
        if results is None:
            continue
        for contest in results.contests:
            if contest.totals:
                found[menu_key(contest.name)] = (dict(contest.totals), label)
    return found


def remaining_budget(
    workspace: Workspace, log: EventLog, county: str
) -> tuple[int | None, str]:
    """The county's allowed total of targeted ballots less the distinct ballots of its sent lists (not retired)."""
    from corla_observer import pulllists
    from corla_observer.targets import allowed_total, sent_entries

    allowed = allowed_total(log, county)
    if allowed is None:
        return None, "no allowed total recorded"
    sent: set[str] = set()
    for item in pulllists.county_lists(log.iter_events(), county, retired=True):
        if item.targeted and not item.draft and not item.retired:
            record = pulllists.record(workspace, item)
            if record is not None:
                sent |= {id_key(e.imprinted_id) for e in sent_entries(record)}
    left = max(0, allowed - len(sent))
    return (
        left,
        f"the county's allowed total {allowed} less {len(sent)} ballots already sent",
    )


def gather(
    workspace: Workspace, log: EventLog, county: str, *, datamap: DataMap | None = None
) -> MenuInputs:
    """The menu's inputs for a county with a redacted CVR in the workspace.

    ``datamap`` defaults to `corla_observer.datamap.build_map`. A test or demonstration county
    uses none of CORLA's data, which is not its own.

    Raises:
        LookupError: if the county has no redacted CVR.
    """
    from corla_observer.classify import is_test_county
    from corla_observer.datamap import build_map
    from corla_observer.targets import cached_votes, sources
    from corla_observer.uploads import status as upload_status

    entry = upload_status(log, [county]).get(county)
    received = entry.latest.get("redacted_cvr") if entry else None
    if received is None:
        raise LookupError(f"no redacted CVR from {county}")
    votes = cached_votes(workspace, received.sha256)
    inputs = MenuInputs(county=county, cvr_sha256=received.sha256, votes=votes)
    test = is_test_county(county)
    if not test:
        datamap = datamap if datamap is not None else build_map(log, workspace)
        inputs.corla, inputs.counties_known, limit = _corla_lists(datamap, workspace)
        specs = [s.risk_limit for s in datamap.contests if s.risk_limit]
        if limit:
            inputs.risk_limit, inputs.risk_limit_source = limit, "CORLA's contest list"
        elif specs and _float(specs[0]):
            inputs.risk_limit, inputs.risk_limit_source = (
                float(str(specs[0])),
                "the target contest list",
            )
        inputs.results = _results(workspace, log, county)
        posted_list = posted_draws(datamap, workspace, county, inputs.corla)
        if posted_list is not None:
            inputs.draws, inputs.draws_source = posted_list
        else:
            inputs.draws, inputs.draws_source, notes = corla_draws(
                workspace, log, datamap, county
            )
            inputs.notes += notes
        comparisons, posted = corla_comparisons(datamap, workspace, county, votes)
    else:
        inputs.draws, inputs.draws_source, notes = corla_draws(
            workspace, log, None, county
        )
        inputs.notes += notes + [
            "A test or demonstration county: CORLA's contest lists and comparisons are not used."
        ]
        comparisons, posted = {}, None
    inputs.observed = observed_comparisons(log, county, votes)
    if posted is not None:
        inputs.comparisons, inputs.corla_comparisons = comparisons, True
        inputs.comparisons_source = (
            f"CORLA's posted CVR-to-audit-board comparison ({posted})"
        )
    else:
        inputs.comparisons = inputs.observed
        inputs.comparisons_source = (
            "the observers' own entries (CORLA's CVR-to-audit-board comparison is not in the workspace); "
            "a draw no observer entered is assumed to show none"
        )
    counted = [
        s.domain for s in sources(workspace, log, county)
        if s.domain is not None and (s.sent_in is not None or s.drafted_key is not None or s.included)
    ]  # fmt: skip
    inputs.domain_draws = [d for d in counted if d.cvr_sha256 == received.sha256]
    if len(inputs.domain_draws) < len(counted):
        inputs.notes.append(
            f"{len(counted) - len(inputs.domain_draws)} domain draw(s) from an earlier CVR are not counted."
        )
    inputs.budget, inputs.budget_source = remaining_budget(workspace, log, county)
    return inputs
