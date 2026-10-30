"""Fast tests for the printed names (corla_observer.printnames): shortened to fit each layout, unique, never wrapped."""

from __future__ import annotations

import pytest

from corla_observer.printnames import (
    ELLIPSIS,
    PrintNames,
    abbreviate_contest,
    base_contest,
    budget,
    cut,
    initials,
    shorten_choice,
    split_value,
    unique_forms,
)

CONTESTS = [
    "State Representative - District 19 - DEM",
    "State Representative - District 19 - REP",
    "Weld County Commissioner - District 2 - REP",
    "Weld County Commissioner - At-Large - REP",
    "Regional Transportation District Director - District K",
    "Weld County School District RE-1 Director District 3",
    "Judge of the Court of Appeals - Retain Judge Smith",
    "Representative to the 120th United States Congress - District 8 - DEM",
    "Governor - DEM",
    "Amendment 79 (Constitutional)",
    "County Clerk (Vote For=1)",
]


def test_always_shortened_as_lori_does() -> None:
    assert base_contest("Representative to the 120th United States Congress - District 4 - REP") == "US Rep - CD4 - REP"
    assert base_contest("Regent of the University of Colorado - Congressional District 2 - DEM") == "CU Regent - CD2 - DEM"
    assert base_contest("State Board of Education Member - Congressional District 5") == "State BOE - CD5"
    assert base_contest("Proposition JJ (Statutory)") == "Proposition JJ"
    assert base_contest("County Clerk (Vote For=1)") == "County Clerk"
    assert base_contest("Town Council (Vote For=3)") == "Town Council (Vote For=3)"


def test_abbreviations_only_until_it_fits() -> None:
    name = "State Representative - District 48 - DEM"
    assert abbreviate_contest(name, 60)[0] == name
    short, used = abbreviate_contest(name, 36)
    assert short == "State Rep - District 48 - DEM" and [a.short for a in used] == ["State Rep"]
    assert abbreviate_contest(name, 20)[0] == "State Rep - Dist 48 DEM"
    assert abbreviate_contest("Weld County Commissioner - District 2 - REP", 36)[0] == "Weld Co Commr - District 2 - REP"
    assert abbreviate_contest("Regional Transportation District Director - District K", 30)[0] == "RTD Director - District K"
    assert abbreviate_contest("Weld County School District RE-1 Director District 3", 40)[0] == "Weld County SD RE-1 Director District 3"
    assert abbreviate_contest("Colorado Supreme Court Justice Smith", 30)[0] == "Colorado Sup Ct Justice Smith"
    assert abbreviate_contest("Town of Erie Ballot Question 2A", 25)[0] == "Town of Erie Q 2A"
    assert abbreviate_contest("Senator Democratic Party", 20)[0] == "Senator DEM"


def test_choices_get_initials_then_are_cut() -> None:
    assert initials("Kelley Anne Dennison") == "K. A. Dennison"
    assert initials('Kelvin "K-Man" Wimberly') == 'K. "K-Man" Wimberly'
    assert initials("Christina Blunt (Ducommun)") == "C. Blunt (Ducommun)"
    assert initials("Write-in") == "Write-in"
    assert shorten_choice("John Hickenlooper", 40) == "John Hickenlooper"
    assert shorten_choice("Kelley Anne Dennison", 15) == "K. A. Dennison"
    assert shorten_choice("Maximilian Featherstonehaugh", 12) == "M. Feathers" + ELLIPSIS
    assert split_value("overvote: A + B") == ("overvote: ", ["A", "B"])
    assert split_value("no vote") == ("", [])


def test_unique_keeps_distinguishing_text() -> None:
    names = [f"Very Long Water Conservancy District Director Seat {n}" for n in (1, 2, 3)]
    forms = unique_forms(names, 30, cut)
    assert len(set(forms.values())) == 3 and all(len(f) <= 30 for f in forms.values())
    assert all(f.endswith(f"Seat {n}") for n, f in zip((1, 2, 3), (forms[x] for x in names), strict=True))


LAYOUT_NAMES = ["portrait-1", "portrait-2", "landscape-2", "landscape-3", "text"]


@pytest.mark.parametrize("layout", LAYOUT_NAMES)
def test_every_layout_fits_and_stays_unique(layout: str) -> None:
    choices = ["Kelley Anne Dennison", "Christina Blunt (Ducommun)", "Maximilian Bartholomew Featherstonehaugh-Williamson",
               "Maximilian Bartholomew Featherstonehaugh-Wilkinson", "Ann", "Bob"]  # fmt: skip
    names = PrintNames(CONTESTS + [c + " Extra Long Words To Force A Cut Here" for c in CONTESTS], choices, layout)
    b = budget(layout)
    printed = {c: names.contest(c) for c in names.contests}
    assert len(set(printed.values())) == len(printed)
    assert all(b.contest_size(p) <= b.contest for p in printed.values())
    votes = [names.vote(c) for c in choices] + [names.vote(" + ".join(choices[:3])), names.vote("overvote: " + " + ".join(choices[:2]))]
    assert all(b.choice_size(v) <= b.choice for v in votes)
    assert names.vote(choices[2]) != names.vote(choices[3])
    assert names.vote("no vote") == "NO VOTE" and names.vote("redacted") == "redacted" and names.vote("Ann + Bob") == "Ann, Bob"
    # The key and the list of shortened contests, for the provenance page.
    shortened = dict(names.shortened_contests())
    assert all(printed[full] == short for short, full in shortened.items())
    key = dict(names.key())
    if any(ELLIPSIS in p for p in printed.values()):
        assert ELLIPSIS in key


def test_names_not_needing_it_are_unchanged() -> None:
    names = PrintNames(["Governor - DEM", "County Clerk (Vote For=1)"], ["Ann"], "landscape-3")
    assert names.contest("Governor - DEM") == "Governor - DEM" and names.contest("County Clerk (Vote For=1)") == "County Clerk"
    assert names.shortened_contests() == [] and names.key() == []


def test_measured_budgets() -> None:
    """The PDF layouts' budgets are the widths of their cells in points, measured in the fonts they are drawn in: a narrow name fits where a wide one of the same length does not."""
    from corla_observer import printlayout

    b = budget("landscape-3")
    lay = printlayout.LAYOUTS["landscape-3"]
    assert b.contest == lay.contest_box - 2 * lay.pad and b.choice == lay.vote_box - 2 * lay.pad
    narrow, wide = "il" * 40, "WM" * 40
    assert b.contest_size(narrow) < b.contest_size(wide)
    assert len(cut(narrow, b.contest, b.contest_size)) > len(cut(wide, b.contest, b.contest_size))
    assert b.contest_size(cut(wide, b.contest, b.contest_size)) <= b.contest
    # The plain text counts characters.
    assert budget("text").contest == 36 and budget("text").contest_size("abc") == 3
    with pytest.raises(ValueError):
        budget("sideways")


def test_several_choices_take_a_line_each_and_are_not_cut() -> None:
    """A multi-seat contest's choices, each with the whole cell: "Malik Oyelaran" is not cut to share a line with two others."""
    choices = ["Malik Oyelaran", "Constance Whitby", "Dov Rosenthal"]
    names = PrintNames(["Testland Valley School District RE-1 Board of Education Director At Large"], choices, "landscape-3")
    assert names.vote_lines(" + ".join(choices)) == choices
    assert names.vote_lines("overvote: Malik Oyelaran + Dov Rosenthal") == ["overvote: Malik Oyelaran", "Dov Rosenthal"]
    assert names.vote_lines("no vote") == ["NO VOTE"] and names.vote_lines("redacted") == ["redacted"]
    screen = PrintNames(["Governor"], choices, "screen")
    shown = screen.vote_lines(" + ".join(choices))
    assert len(shown) == 3 and not any("\N{HORIZONTAL ELLIPSIS}" in c for c in shown)  # at most initials ("C. Whitby") on a phone
