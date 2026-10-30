"""A fictional county with very long ballots, for trying the observer pages on synthetic data.

Testland (`corla_observer.classify.TEST_COUNTIES`) is not a Colorado county.
`generate` writes, for it, the files a county would provide: a ballot
manifest, a Dominion-format redacted CVR with random votes, and a pull list
for one audit board. Every vote is random: no real ballot is in it, so the
files can be shown on any server. They are uploaded with the county chosen
explicitly (``corla-observer upload --county Testland``), since content
detection rightly names no Colorado county for them, and then the extract
and practice links are built as for any county (README, "Practice").

**Why.** Review asked what the observer's screen looks like for a ballot
with perhaps 50 contests. In the 2020 general election the longest ballot
cards had 48 contests (Denver, one-card ballots), 39 (Adams), 38 (Arapahoe)
and 36 (Jefferson), counted from the published CVRs.
Testland's longest style combines their structure, and adds a few more
contests, to 60: the 2020 federal and statewide contests and statewide
measures, which are on every Colorado ballot (their real names and choices,
which are public), and fictional local offices, judicial retentions
(Denver's ballot had 23 of them) and local ballot issues, some with long
titles, three with Vote For 2 or 3.

**Styles.** Four ballot styles (`STYLES`), each the ballots of two
precincts (one for the shortest): A (60 contests), B (47), C (42) and D
(27), each a subset of the longest in its order; the BallotType column says
``Style A (60 contests)``. Every precinct has at least 100 ballot cards, so the file passes the redaction check
(`corla_observer.redaction`): no style, precinct and ballot type group is
smaller than 10.

**Votes.** Drawn with ``random.Random(seed)`` (`DEFAULT_SEED`), so the same
seed always gives the same files. In each contest a card has no vote with
probability `NO_VOTE_RATE`, an overvote (one choice more than Vote For)
with probability `OVERVOTE_RATE`, and otherwise Vote For choices (sometimes
fewer, in a multi-seat contest), the major candidates more likely. Besides:

- `REDACTED_ROWS` cards of styles B and C have every vote cell blank, as a county's redaction
  leaves a row (the extract shows the whole ballot as redacted);
- `REDACTED_CONTEST_CARDS` cards of one style A precinct have ``*`` in
  every cell of one contest (`REDACTED_CONTEST`), as a county redacts a
  rare contest; they are one group of that size, so the check still passes.

**Pull list.** `PULL_LIST_SIZE` ballots for Audit Board 1, a third of them
on the 60-contest style A, in CORLA's pull order (storage location, scanner,
batch, record), with the county, imprinted ID, board and storage location;
among them at least one card with an overvote, one with ``*`` in a contest
and one fully redacted card.

**Self-contained.** A test county needs nothing from a real election: its
extract (`corla_observer.extract.build_extract`) is built from its own
uploads only, its pull list and redacted CVR, with its manifest for storage
locations. Without its pull list (uploading it is optional), its
selections are predicted as for a real county, from a seed, its own
uploaded manifest, its targeted contest (`TARGETED`) and a fixed sample
count (`SAMPLE_COUNT`, 50 by default; ``extract build --sample-count``),
recorded as fixed, not estimated from margins (`prediction`). The seed is
the one a coordinator entered, or else the test county's own (`SEED`).
Every ballot card in the manifest is in the targeted contest's domain, and
the draws are CORLA's (`corla_observer.sampling.select_ballots`), in
CORLA's pull order once the extract sorts them. The prediction is stored
and named in the extract's inputs, but never recorded as a
``prediction.made`` event, so it never stands in for a real election's.

Standard library only.
"""

from __future__ import annotations

import csv
import io
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corla_observer.sampling import ManifestBatch, build_domain_from_manifests, select_ballots

COUNTY = "Testland"
DEFAULT_SEED = 20201103
ELECTION_TITLE = "2026 Testland Synthetic General Election (TEST DATA: random votes)"
SOFTWARE = "5.17.17.1"
NO_VOTE_RATE = 0.05
OVERVOTE_RATE = 0.006
WRITE_IN_RATE = 0.01
REDACTED_ROWS = 3
REDACTED_CONTEST_CARDS = 12
PULL_LIST_SIZE = 21
CVR_NAME = "Testland_CVR_Export_TEST.csv"
MANIFEST_NAME = "Testland_BallotManifest_TEST.csv"
PULL_LIST_NAME = "Testland_ballots_to_audit_TEST.csv"
#: The test county's targeted contest, for `prediction`: on every ballot style, so every card is in its domain.
TARGETED = "Testland County Commissioner - District 1"
#: Its round-one sample count, fixed (not estimated from a margin).
SAMPLE_COUNT = 50
#: The test county's own seed (fictional), used when no seed has been entered.
SEED = "41368579216342078921"


@dataclass(frozen=True)
class Contest:
    name: str
    choices: tuple[str, ...]
    vote_for: int = 1
    #: Choices that win most votes (by index), for realistic tallies.
    major: tuple[int, ...] = ()

    @property
    def header(self) -> str:
        return f"{self.name} (Vote For={self.vote_for})"


YES_NO = ("Yes", "No")
FOR_AGAINST = ("Yes/For", "No/Against")

_PRESIDENT = (
    "Joseph R. Biden / Kamala D. Harris", "Donald J. Trump / Michael R. Pence", "Don Blankenship / William Mohr",
    "Bill Hammons / Eric Bodenstab", "Howie Hawkins / Angela Nicole Walker", "Blake Huber / Frank Atwood",
    'Jo Jorgensen / Jeremy "Spike" Cohen', "Brian Carroll / Amar Patel", "Mark Charles / Adrian Wallace",
    "Phil Collins / Billy Joe Parker", 'Roque "Rocky" De La Fuente / Darcy G. Richardson',
    "Dario Hunter / Dawn Neptune Adams", "Princess Khadijah Maryam Jacob-Fambro / Khadijah Maryam Jacob Sr.",
    "Alyson Kennedy / Malcolm Jarrett", "Joseph Kishore / Norissa Santa Cruz", "Kyle Kenley Kopitke / Nathan Re Vo Sorenson",
    "Gloria La Riva / Sunil Freeman", "Joe McHugh / Elizabeth Storm", "Brock Pierce / Karla Ballard",
    'Jordan "Cancer" Scott / Jennifer Tepool', "Kanye West / Michelle Tidball", "Write-in",
)  # fmt: skip
_SENATE = (
    "John W. Hickenlooper", "Cory Gardner", "Daniel Doyle", 'Stephan "Seku" Evans', "Raymon Anthony Doane", "Write-in",
)  # fmt: skip

#: The longest ballot's contests, in ballot order: 2020's federal and
#: statewide contests and measures (real, public), then fictional ones.
CONTESTS: tuple[Contest, ...] = (
    Contest("Presidential Electors", _PRESIDENT, major=(0, 1)),
    Contest("United States Senator", _SENATE, major=(0, 1)),
    Contest("Representative to the 117th United States Congress - District 9",
            ("Rowan Achterberg", "Delphine Okonkwo-Marchetti", "Write-in"), major=(0, 1)),
    Contest("State Board of Education Member - Congressional District 9", ("Imelda Farrow", "Teodor Lindqvist")),
    Contest("Regent of the University of Colorado - Congressional District 9", ("Priya Sandoval-Hughes", "Gareth Moon")),
    Contest("State Senator - District 41", ("Wilhelmina Castellanos-Featherstonehaugh", "Bo Tran")),
    Contest("State Representative - District 77", ("Odessa Kimura", "Lachlan Pryce", "Marisol Ybarra")),
    Contest("District Attorney - 99th Judicial District", ("Cornelius Abernathy", "Yasmin Delacroix")),
    Contest("Regional Transportation District Director - District T", ("Hollis Gutierrez", "Nadia Brennan", "Ezra Whitlock")),
    Contest("Testland County Commissioner - District 1", ("Augusta Merriweather", "Felix Okafor")),
    Contest("Testland County Commissioner - District 2", ("Bartholomew Esquivel-Lindgren", "June Park-Halvorsen", "Rex Dunmore")),
    Contest("Testland County Commissioner - District 3", ("Saoirse Nakamura", "Desmond Achebe")),
    Contest("Testland County Surveyor", ("Ingrid Vasquez",)),
    Contest("Testland County Coroner", ("Thaddeus Moreau", "Kiri Waititi")),
    Contest("Testland Valley School District RE-1 Board of Education Director At Large",
            ("Anya Petrova", "Malik Oyelaran", "Constance Whitby", "Hiroshi Tanaka", "Esperanza Quintero", "Dov Rosenthal"),
            vote_for=3),
    Contest("City of Testland City Council At Large",
            ("Penelope Achterberg", "Ravi Subramanian", "Gwendolyn Okoro", "Lars Eriksen", "Mirela Popescu"), vote_for=2),
    Contest("Testland Fire Protection District Director", ("Clement Ashby", "Noor Haddad", "Silas Brightwater", "Beatriz Lomeli"),
            vote_for=2),
    Contest("Justice of the Colorado Supreme Court - Hart", YES_NO),
    Contest("Justice of the Colorado Supreme Court - Samour", YES_NO),
    Contest("Colorado Court of Appeals Judge - Tow", YES_NO),
    Contest("Colorado Court of Appeals Judge - Welling", YES_NO),
    *(Contest(f"District Court Judge - 99th Judicial District - {judge}", YES_NO)
      for judge in ("Alder", "Birchwood", "Calloway", "Dunsmuir", "Ellsworth", "Fairbanks", "Greenleaf")),
    *(Contest(f"Testland County Court Judge - {judge}", YES_NO)
      for judge in ("Hawthorne", "Ives", "Juniper", "Kestrel", "Larkspur", "Merrow", "Northcott")),
    Contest("Amendment B (CONSTITUTIONAL) - SCR20-001 Repeal Property Tax Assessment Rates", FOR_AGAINST),
    Contest("Amendment C (CONSTITUTIONAL) - HCR20-1001 Conduct of Charitable Gaming", FOR_AGAINST),
    Contest("Amendment 76 (CONSTITUTIONAL) - Citizenship Qualification of Electors", FOR_AGAINST),
    Contest("Amendment 77 (CONSTITUTIONAL) - Local Voter Approval of Gaming Limits in Black Hawk, Central City, and Cripple Creek",
            FOR_AGAINST),
    Contest("Proposition EE (STATUTORY) - HB20-1427 Taxes on Nicotine Products", FOR_AGAINST),
    Contest("Proposition 113 (STATUTORY) - Adopt Agreement to Elect U.S. President by National Popular Vote", FOR_AGAINST),
    Contest("Proposition 114 (STATUTORY) - Reintroduction and Management of Gray Wolves", FOR_AGAINST),
    Contest("Proposition 115 (STATUTORY) - Prohibit Abortion After 22 Weeks", FOR_AGAINST),
    Contest("Proposition 116 (STATUTORY) - Decrease Income Tax Rate from 4.63% to 4.55%", FOR_AGAINST),
    Contest("Proposition 117 (STATUTORY) - Voter Approval of Certain New Enterprises that are Exempt from TABOR", FOR_AGAINST),
    Contest("Proposition 118 (STATUTORY) - Paid Family and Medical Leave Insurance Program", FOR_AGAINST),
    Contest("Testland County Ballot Issue 1A - Countywide Sales and Use Tax Increase of 0.25 Percent for Open Space, Trails, "
            "Wildfire Mitigation and Watershed Protection Through December 31, 2045", FOR_AGAINST),
    Contest("Testland County Ballot Issue 1B - Retain and Spend Revenue Above the TABOR Limit for Roads and Bridges", FOR_AGAINST),
    Contest("Testland County Ballot Question 1C - Term Limits for the County Coroner and Surveyor", FOR_AGAINST),
    Contest("City of Testland Ballot Issue 2A - Lodging Tax for Affordable Housing", FOR_AGAINST),
    Contest("City of Testland Ballot Question 2B - Charter Amendment Concerning Council Vacancies", FOR_AGAINST),
    Contest("City of Testland Ballot Question 2C - Ranked Choice Voting for Municipal Elections Beginning in 2027", FOR_AGAINST),
    Contest("City of Testland Ballot Issue 2D - Bonds of $48,500,000 for a Recreation Center and Library Branch", FOR_AGAINST),
    Contest("City of Testland Ballot Question 2E - Retail Marijuana Stores", FOR_AGAINST),
    Contest("Testland Valley School District RE-1 Ballot Issue 4A - Mill Levy Override for Teacher Compensation, "
            "Mental Health Staff and Building Repairs", FOR_AGAINST),
    Contest("Testland Valley School District RE-1 Ballot Issue 4B - Bonded Indebtedness of $125,000,000", FOR_AGAINST),
    Contest("Testland Fire Protection District Ballot Issue 6A - Mill Levy Increase for Paid Firefighters", FOR_AGAINST),
    Contest("Testland Library District Ballot Issue 7A", FOR_AGAINST),
    Contest("North Testland Water and Sanitation District Ballot Issue 8A", FOR_AGAINST),
    Contest("Upper Testland Mosquito Control District Ballot Issue 9A", FOR_AGAINST),
)  # fmt: skip

#: The contest a county redacted on some cards (with ``*``): on the longest style only.
REDACTED_CONTEST = "Upper Testland Mosquito Control District Ballot Issue 9A"


#: Each ballot style's contests, as the prefixes of the longest ballot's
#: contests it leaves out (`style_contests` resolves them). Style A is the
#: longest ballot, with every contest.
STYLES: dict[str, tuple[str, ...]] = {
    "A": (),
    "B": ("City of Testland", "Testland Fire", "North Testland", "Upper Testland", "Testland Library",
          "Testland County Ballot Question 1C", "Testland Valley School District RE-1 Ballot Issue 4B"),
    "C": ("City of Testland", "Testland Fire", "North Testland", "Upper Testland", "Testland Library", "Testland Valley",
          "Testland County Court Judge - M", "Testland County Court Judge - N", "Testland County Commissioner - District 3",
          "Testland County Ballot Question 1C"),
    "D": ("City of Testland", "Testland Fire", "North Testland", "Upper Testland", "Testland Library", "Testland Valley",
          "Testland County Court", "District Court Judge", "Regional Transportation", "Testland County Commissioner - District 2",
          "Testland County Commissioner - District 3", "Testland County Ballot Question 1C", "Testland County Coroner"),
}  # fmt: skip
#: The longest style, with every contest.
LONGEST = "A"
#: Each precinct's style and number of ballot cards.
PRECINCTS: dict[str, tuple[str, int]] = {
    "101": ("A", 150), "102": ("A", 130),
    "201": ("B", 140), "202": ("B", 120),
    "301": ("C", 160), "302": ("C", 110),
    "401": ("D", 130),
}  # fmt: skip
#: Scanners and their batches' storage locations.
TABULATORS = {"101": "Testland Vault Shelf A", "102": "Testland Vault Shelf B"}
BATCH_SIZE = 75


def style_contests() -> dict[str, tuple[str, ...]]:
    """Each style's contests, by name, in ballot order."""
    return {
        style: tuple(c.name for c in CONTESTS if not any(c.name.startswith(p) for p in left_out))
        for style, left_out in STYLES.items()
    }


def ballot_type(style: str, styles: dict[str, tuple[str, ...]] | None = None) -> str:
    """The BallotType cell of a style: ``Style A (60 contests)``."""
    styles = styles or style_contests()
    return f"Style {style} ({len(styles[style])} contests)"


@dataclass
class Card:
    cvr_number: int
    tabulator: str
    batch: str
    record: int
    precinct: str
    style: str
    #: Per contest on the card, the indexes of the choices marked (empty: no vote).
    marks: dict[str, tuple[int, ...]] = field(default_factory=dict)
    #: The contests whose cells are ``*`` (redacted by the county).
    redacted_contests: frozenset[str] = frozenset()
    #: Every vote cell blank: a fully redacted row.
    blank: bool = False

    @property
    def imprinted_id(self) -> str:
        return f"{self.tabulator}-{self.batch}-{self.record}"

    @property
    def location(self) -> str:
        return f"{TABULATORS[self.tabulator]}, Box {self.batch}"


@dataclass
class TestCounty:
    seed: int
    cards: list[Card]
    pulled: list[Card]
    styles: dict[str, tuple[str, ...]]

    def files(self) -> dict[str, bytes]:
        """The county's files, by name: the CVR, the manifest and the pull list."""
        return {CVR_NAME: cvr_bytes(self), MANIFEST_NAME: manifest_bytes(self), PULL_LIST_NAME: pull_list_bytes(self)}


def _vote(contest: Contest, rng: random.Random) -> tuple[int, ...]:
    n = len(contest.choices)
    roll = rng.random()
    if roll < NO_VOTE_RATE:
        return ()
    write_in = contest.choices.index("Write-in") if "Write-in" in contest.choices else None
    candidates = [i for i in range(n) if i != write_in]
    weights = [6.0 if i in contest.major else 1.0 for i in candidates]
    if write_in is not None and rng.random() < WRITE_IN_RATE:
        return (write_in,)
    if roll < NO_VOTE_RATE + OVERVOTE_RATE and n > contest.vote_for:
        return tuple(sorted(rng.sample(range(n), contest.vote_for + 1)))
    k = contest.vote_for if contest.vote_for == 1 or rng.random() < 0.75 else rng.randint(1, contest.vote_for - 1)
    k = min(k, len(candidates))
    chosen: set[int] = set()
    while len(chosen) < k:
        chosen.add(rng.choices(candidates, weights)[0])
    return tuple(sorted(chosen))


def make(seed: int = DEFAULT_SEED) -> TestCounty:
    """The test county's cards and pull list, from ``seed``."""
    rng = random.Random(seed)
    styles = style_contests()
    by_name = {c.name: c for c in CONTESTS}
    ballots = [(precinct, style) for precinct, (style, count) in PRECINCTS.items() for _ in range(count)]
    rng.shuffle(ballots)  # mail ballots arrive mixed: every batch holds several styles
    cards: list[Card] = []
    tabulators = list(TABULATORS)
    for index, (precinct, style) in enumerate(ballots):
        batch_number, record = divmod(index, BATCH_SIZE)
        tabulator = tabulators[batch_number % len(tabulators)]
        card = Card(len(cards) + 1, tabulator, str(batch_number // len(tabulators) + 1), record + 1, precinct, style)
        card.marks = {name: _vote(by_name[name], rng) for name in styles[style]}
        cards.append(card)
    longest = [c for c in cards if c.style == LONGEST]
    # A county's redactions: whole rows, and one contest on one precinct's cards.
    for card in rng.sample([c for c in cards if c.style in ("B", "C")], REDACTED_ROWS):
        card.blank = True
    in_102 = [c for c in longest if c.precinct == "102" and not c.blank]
    for card in rng.sample(in_102, REDACTED_CONTEST_CARDS):
        card.redacted_contests = frozenset({REDACTED_CONTEST})
    pulled = _pull_list(cards, rng)
    return TestCounty(seed, cards, pulled, styles)


def _pull_list(cards: Sequence[Card], rng: random.Random) -> list[Card]:
    """`PULL_LIST_SIZE` cards, a third on the longest style, with the special cases, in CORLA's pull order."""
    longest = [c for c in cards if c.style == LONGEST and not c.blank]
    others = [c for c in cards if c.style != LONGEST and not c.blank]
    third = PULL_LIST_SIZE // 3
    special = [
        next(c for c in longest if c.redacted_contests),
        next(c for c in longest if not c.redacted_contests and any(len(m) > _vote_for(n) for n, m in c.marks.items())),
    ]
    chosen = special + rng.sample([c for c in longest if c not in special and not c.redacted_contests], third - len(special))
    chosen.append(next(c for c in cards if c.blank))
    chosen += rng.sample(others, PULL_LIST_SIZE - len(chosen))
    return sorted(chosen, key=lambda c: (c.location, int(c.tabulator), int(c.batch), c.record))


def _vote_for(name: str) -> int:
    return next(c.vote_for for c in CONTESTS if c.name == name)


def cvr_bytes(county: TestCounty) -> bytes:
    """The Dominion-format CVR export: four header rows, then one row per card."""
    meta = ["CvrNumber", "TabulatorNum", "BatchId", "RecordId", "ImprintedId", "CountingGroup", "PrecinctPortion", "BallotType"]
    columns = [(c, i) for c in CONTESTS for i in range(len(c.choices))]
    pad = [""] * len(meta)
    rows: list[list[str]] = [
        [ELECTION_TITLE, SOFTWARE] + [""] * (len(meta) + len(columns) - 2),
        pad + [c.header for c, _ in columns],
        pad + [c.choices[i] for c, i in columns],
        meta + [""] * len(columns),
    ]
    for card in county.cards:
        row = [str(card.cvr_number), card.tabulator, card.batch, str(card.record), card.imprinted_id, "Mail",
               f"Precinct {card.precinct}", ballot_type(card.style, county.styles)]  # fmt: skip
        for contest, i in columns:
            marks = card.marks.get(contest.name)
            if card.blank or marks is None:
                row.append("")
            elif contest.name in card.redacted_contests:
                row.append("*")
            else:
                row.append("1" if i in marks else "0")
        rows.append(row)
    return _csv(rows)


def manifest_bytes(county: TestCounty) -> bytes:
    """The ballot manifest: one row per batch, with its storage location."""
    batches: dict[tuple[str, str], int] = {}
    location: dict[tuple[str, str], str] = {}
    for card in county.cards:
        key = (card.tabulator, card.batch)
        batches[key] = batches.get(key, 0) + 1
        location[key] = card.location
    rows = [["County", "Tabulator", "Batch", "# of Ballots", "Storage Location"]]
    for key in sorted(batches, key=lambda k: (int(k[0]), int(k[1]))):
        rows.append([COUNTY, key[0], key[1], str(batches[key]), location[key]])
    return _csv(rows)


def pull_list_bytes(county: TestCounty) -> bytes:
    """The pull list for Audit Board 1, in CORLA's pull order."""
    rows = [["County", "Imprinted ID", "Audit Board", "Storage Location", "Ballot Type"]]
    rows += [[COUNTY, c.imprinted_id, "Audit Board 1", c.location, ballot_type(c.style, county.styles)] for c in county.pulled]
    return _csv(rows)


def _csv(rows: Sequence[Sequence[str]]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


def generate(out: Path | str, seed: int = DEFAULT_SEED) -> dict[str, Path]:
    """Write the test county's files into ``out`` (created if missing); return their paths by name."""
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, data in make(seed).files().items():
        path = directory / name
        path.write_bytes(data)
        written[name] = path
    return written


def summary(county: TestCounty) -> list[str]:
    """Counts only: styles, cards and the pull list's make-up."""
    lines = [f"{COUNTY} (fictional test county), seed {county.seed}: {len(county.cards)} cards"]
    for style, names in county.styles.items():
        count = sum(1 for c in county.cards if c.style == style)
        lines.append(f"  {ballot_type(style, county.styles)}: {count} cards")
    lines.append(f"  fully redacted rows: {sum(1 for c in county.cards if c.blank)}; "
                 f"cards with {REDACTED_CONTEST!r} redacted: {sum(1 for c in county.cards if c.redacted_contests)}")  # fmt: skip
    longest = sum(1 for c in county.pulled if c.style == LONGEST)
    lines.append(f"  pull list: {len(county.pulled)} ballots for Audit Board 1, {longest} on {ballot_type(LONGEST, county.styles)}")
    return lines


def prediction(
    seed: str, batches: Sequence[ManifestBatch], county: str = COUNTY, sample_count: int = SAMPLE_COUNT, seed_source: str = "the seed in effect"
) -> dict[str, Any]:
    """The test county's predicted selections, from ``seed`` and its own manifest's ``batches``, in a prediction's form.

    The same structure as a recorded prediction (`corla_observer.predict`):
    `TARGETED`'s first ``sample_count`` draws from the county's manifest
    alone, by CORLA's sampling. Every draw is certain (its low count is its
    sample count, which is fixed, not estimated).
    """
    if not 1 <= sample_count <= 10_000:
        raise ValueError("the sample count must be 1 to 10000")
    domain = build_domain_from_manifests([list(batches)])
    contests: list[dict[str, Any]] = []
    for name, count in ((TARGETED, sample_count),):
        draws = select_ballots(seed, domain, count)
        contests.append(
            {
                "name": name,
                "counties": [county],
                "statewide": False,
                "domain_size": domain.size,
                "sample_count": count,
                "count_source": f"fixed for the test county: {count} (not estimated)",
                "estimated": False,
                "low_count": count,
                "adjustments": [],
                "flags": [],
                "draws": [{"index": d.index, "position": d.position, "county": county, "imprinted_id": d.imprinted_id} for d in draws],
            }
        )
    return {
        "election": None,
        "seed": seed,
        "seed_source": seed_source,
        "county_order_source": "one county",
        "overrides_sha256": None,
        "contests": contests,
    }
