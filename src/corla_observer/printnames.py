"""Contest and choice names as printed on the extract, short enough never to wrap.

The printed extract (`corla_observer.extract`, docs/EXTRACT.md, "What the
printout shows") puts each contest on one line: its name, a check box and
the expected choices. Each print layout has a budget for the name, for the
choices and for a ballot's line of marks (`budget`): for the PDF layouts,
the width in points of the cell they are drawn in, measured with the
bundled fonts' real metrics (`corla_observer.printlayout`); for the plain
text, a number of characters. A name is shortened only as far as it must
be to fit its budget, so that no name ever wraps or overflows.

**Contest names.** Every printed contest name loses "(Vote For=1)",
"(Statutory)" and "(Constitutional)", and the three long names that Lori
Stevens's RLAAuditHelper shortens are always shortened ("US Rep - CD8",
"CU Regent - CD2", "State BOE - CD5"). A name still too wide for its budget
is shortened by the rules of `ABBREVIATIONS`, one at a time in order, until
it fits; only if it still does not fit is it cut, with "…" at the end.

**Choices.** A choice too wide for its budget has its first names shortened
to initials ("Kelley Anne Dennison" is "K. A. Dennison"), and is cut with
"…" only if that is still too wide. Several choices (a multi-seat contest,
or an overvote) are printed one per line (`vote_lines`), each with the
whole budget, the first after the contest's name and the rest on
continuation lines below it, so that no name is cut to make room for
another; `vote` joins them by ", " on one line, for a single line of text.

**Unique.** Two different names never print alike within an extract
(`unique_forms`): if two shortened names would collide, the colliding ones
are cut in the middle instead, keeping the end of the name (a district
number, a party), as much of it as it takes to tell them apart.

All of this is presentation: the extract, its hash and its page hashes
hold the names in full. `PrintNames` records which abbreviations it used
and which contests it shortened, for the key on the provenance page.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"

#: A text's size: its width in points, or ``len`` for characters.
Size = Callable[[str], float]

#: The plain text's budgets, in characters: 80 columns, as name, " __ ", choices; and the marks after " * ".
TEXT_WIDTHS = (36, 40, 77)


@dataclass(frozen=True)
class Budget:
    """How wide a layout's printed contest names, choices and marks lines may be, and how their size is measured."""

    contest: float
    choice: float
    marks: float
    contest_size: Size = len
    choice_size: Size = len
    marks_size: Size = len
    #: The least share of the choices' budget each of several choices gets.
    min_share: float = 4


#: The observer screen's cells, in CSS pixels, for a phone 390 pixels wide: the contest's name (12.8 px, regular) and each choice
#: (14.4 px, bold), measured with the bundled fonts' metrics widened by `SCREEN_WIDEN` for a phone's system font. On the
#: screen a choice is never cut: one still too wide with initials wraps in its cell; a contest's name is shortened to fit
#: one line where its abbreviations can, else wraps, and is cut (as on paper) only beyond two lines' width.
SCREEN_WIDTHS = (125.0, 150.0, 330.0)
SCREEN_CONTEST_LINES = 2
SCREEN_SIZES = (12.8, 14.4)
SCREEN_WIDEN = 1.15


def budget(layout: str) -> Budget:
    """The budgets of a print layout (`corla_observer.printlayout.LAYOUTS`, measured in points with the fonts it is drawn in), of the plain text ("text", in characters), or of the observer screen ("screen", `SCREEN_WIDTHS`)."""
    if layout == "text":
        return Budget(*TEXT_WIDTHS)
    if layout == "screen":
        from corla_observer import printlayout

        regular, bold = printlayout.measure(printlayout.SANS, SCREEN_SIZES[0]), printlayout.measure(printlayout.BOLD, SCREEN_SIZES[1])
        return Budget(
            *SCREEN_WIDTHS,
            contest_size=lambda t: SCREEN_WIDEN * regular(t),
            choice_size=lambda t: SCREEN_WIDEN * bold(t),
            marks_size=lambda t: SCREEN_WIDEN * bold(t),
            min_share=4 * SCREEN_SIZES[1],
        )
    from corla_observer import printlayout

    if layout not in printlayout.LAYOUTS and layout != "portrait":
        raise ValueError(f"no such print layout: {layout!r}")
    lay = printlayout.layout(layout)
    regular = printlayout.measure(printlayout.SANS, lay.size)
    bold = printlayout.measure(printlayout.BOLD, lay.size)
    return Budget(lay.contest_budget, lay.vote_budget, lay.marks_budget, regular, bold, bold, min_share=4 * lay.size)

#: Always applied: Lori Stevens's shortenings, from RLAAuditHelper
#: (https://github.com/loriinboulder/AuditVerificationAssistant).
_LORI = (
    (re.compile(r"^Representative to the \d+\w* United States Congress - District (\d+)"), r"US Rep - CD\1"),
    (re.compile(r"^Regent of the University of Colorado - Congressional District (\d+)"), r"CU Regent - CD\1"),
    (re.compile(r"^State Board of Education Member - Congressional District (\d+)"), r"State BOE - CD\1"),
)
_MEASURE_KIND_RE = re.compile(r"\s*\((?:statutory|constitutional)\)\s*", re.IGNORECASE)
_VOTE_FOR_ONE_RE = re.compile(r"\s*\(Vote For=1\)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Abbreviation:
    """A shortening of contest names: ``pattern`` replaced by ``short``, listed in the key as ``label`` = ``meaning``."""

    pattern: re.Pattern[str]
    short: str
    meaning: str
    label: str = ""

    @property
    def key_label(self) -> str:
        return self.label or self.short.strip()

    def apply(self, name: str) -> str:
        return self.pattern.sub(self.short, name)


def _word(text: str) -> re.Pattern[str]:
    return re.compile(r"\b" + re.escape(text) + r"\b")


#: The shortenings of contest names, in the order they are tried, each only
#: while a name is still too long for its budget.
ABBREVIATIONS: tuple[Abbreviation, ...] = (
    Abbreviation(_word("Regional Transportation District"), "RTD", "Regional Transportation District"),
    Abbreviation(_word("United States"), "US", "United States"),
    Abbreviation(_word("State Representative"), "State Rep", "State Representative"),
    Abbreviation(_word("State Senator"), "State Sen", "State Senator"),
    Abbreviation(_word("County Commissioner"), "Co Commr", "County Commissioner"),
    Abbreviation(_word("Court of Appeals"), "Ct App", "Court of Appeals"),
    Abbreviation(_word("Supreme Court"), "Sup Ct", "Supreme Court"),
    Abbreviation(_word("School District"), "SD", "School District"),
    Abbreviation(_word("Judicial District"), "JD", "Judicial District"),
    Abbreviation(_word("Ballot Issue"), "Issue", "Ballot Issue"),
    Abbreviation(_word("Ballot Question"), "Q", "Ballot Question"),
    Abbreviation(re.compile(r"\s*-?\s*\bDemocratic Party\b"), " DEM", "Democratic Party"),
    Abbreviation(re.compile(r"\s*-?\s*\bRepublican Party\b"), " REP", "Republican Party"),
    Abbreviation(_word("District"), "Dist", "District"),
    Abbreviation(_word("County"), "Co", "County"),
    Abbreviation(re.compile(r" - (DEM|REP|LBR|LIB|GRN|UNI|ACN|APV)$"), r" \1", "a party's primary, without \" - \" (\"Governor - DEM\" is \"Governor DEM\")", label="DEM, REP"),
)


def base_contest(name: str) -> str:
    """A contest's name as always printed: without "(Vote For=1)", "(Statutory)" or "(Constitutional)", and with Lori Stevens's shortenings."""
    name = _VOTE_FOR_ONE_RE.sub("", name)
    name = re.sub(r"\s+", " ", _MEASURE_KIND_RE.sub(" ", name)).strip()
    for pattern, replacement in _LORI:
        name = pattern.sub(replacement, name)
    return name


def _longest(fits: Callable[[int], bool], n: int) -> int:
    """The largest k in 0..n with ``fits(k)``, for ``fits`` true up to some k and false after (0 if none)."""
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def cut(text: str, width: float, size: Size = len) -> str:
    """``text`` if it fits in ``width``, else as much of its start as fits with "…"."""
    if size(text) <= width:
        return text
    keep = _longest(lambda k: size(text[:k] + ELLIPSIS) <= width, len(text))
    return text[:keep].rstrip() + ELLIPSIS


def abbreviate_contest(name: str, width: float | None = None, size: Size = len) -> tuple[str, tuple[Abbreviation, ...]]:
    """A contest's name shortened by `ABBREVIATIONS` until it fits ``width`` (all of them, for None), and the ones used. Not cut."""
    short = base_contest(name)
    used: list[Abbreviation] = []
    for abbreviation in ABBREVIATIONS:
        if width is not None and size(short) <= width:
            break
        changed = re.sub(r"\s+", " ", abbreviation.apply(short)).strip()
        if changed != short:
            short = changed
            used.append(abbreviation)
    return short, tuple(used)


_BRACKETED = re.compile(r"^[\"'(\[“].*[\"')\]”]$")


def initials(choice: str) -> str:
    """A choice with its first names as initials: "Kelley Anne Dennison" is "K. A. Dennison".

    Nicknames in quotes and words in brackets are kept, and so is the last
    name (the last word before any bracketed words at the end).
    """
    if " / " in choice:  # a ticket ("Joseph R. Biden / Kamala D. Harris"): each name on its own
        return " / ".join(initials(part) for part in choice.split(" / "))
    words = choice.split()
    end = len(words)
    while end > 1 and _BRACKETED.match(words[end - 1]):
        end -= 1
    if end < 2:
        return choice
    out = [w if _BRACKETED.match(w) or not w[0].isalpha() else f"{w[0]}." for w in words[: end - 1]]
    return " ".join(out + words[end - 1 :])


def shorten_choice(choice: str, width: float, size: Size = len) -> str:
    """A choice that fits ``width``: as it is, else with initials for first names, else cut."""
    if size(choice) <= width:
        return choice
    return cut(initials(choice), width, size)


def _elide(text: str, width: float, tail: int, size: Size = len) -> str:
    """``text`` cut in the middle to fit ``width``, keeping its last ``tail`` characters (and at least 3 of its first); else cut at the end."""
    if size(text) <= width:
        return text
    end = text[len(text) - tail :] if tail > 0 else ""
    if tail <= 0 or size(text[:3] + ELLIPSIS + end) > width:
        return cut(text, width, size)
    head = _longest(lambda k: size(text[:k] + ELLIPSIS + end) <= width, len(text))
    return text[:head].rstrip() + ELLIPSIS + end


def _word_tail(k: int) -> Callable[[str], int]:
    """The length of a name's last ``k`` words, at least 6 characters' worth (so "Seat 2", not "2")."""

    def tail(text: str) -> int:
        words = text.split()
        n = k
        while n < len(words) and len(" ".join(words[-n:])) < 6:
            n += 1
        return len(" ".join(words[-n:]))

    return tail


def _char_tail(t: int) -> Callable[[str], int]:
    return lambda _text: t


def unique_forms(
    names: Iterable[str], width: float, shorten: Callable[[str, float], str], fuller: Iterable[Callable[[str], str]] = (), size: Size = len
) -> dict[str, str]:
    """Each name's printed form, at most ``width`` wide (by ``size``), no two different names alike.

    Each name is ``shorten(name, width)``. Names whose forms collide are
    instead cut in the middle (`_elide`), keeping as much of their end as
    it takes to tell them apart, from the first of ``fuller`` forms (and
    then the name itself) that differ among them; failing all that, they
    are numbered. The end kept is whole words if that is enough.
    """
    distinct = sorted(set(names))
    forms = {n: shorten(n, width) for n in distinct}
    candidates: list[Callable[[str], str]] = [*fuller, lambda n: n]
    for _ in range(4):
        by_form: dict[str, list[str]] = {}
        for name, form in forms.items():
            by_form.setdefault(form, []).append(name)
        groups = [group for group in by_form.values() if len(group) > 1]
        if not groups:
            return forms
        taken = set(forms.values())
        for group in groups:
            others = taken - {forms[n] for n in group}
            done = False
            for full in candidates:
                bases = {n: full(n) for n in group}
                if len(set(bases.values())) < len(group):
                    continue
                # Whole words at the end first ("Seat 2", "Dist 3 DEM"), then any number of characters.
                tails: list[Callable[[str], int]] = [_word_tail(k) for k in range(1, 6)]
                tails += [_char_tail(t) for t in range(0, max(len(b) for b in bases.values()) + 1)]
                for tail in tails:
                    trial = {n: _elide(bases[n], width, tail(bases[n]), size) for n in group}
                    if len(set(trial.values())) == len(group) and not set(trial.values()) & others:
                        forms.update(trial)
                        done = True
                        break
                if done:
                    break
            if not done:
                for i, name in enumerate(group, start=1):
                    tag = f" #{i}"
                    forms[name] = cut(forms[name], width - size(tag), size) + tag
    return forms


@dataclass
class PrintNames:
    """The printed forms of an extract's contests and choices in one layout, unique within the extract."""

    contests: Iterable[str]
    choices: Iterable[str]
    layout: str = "portrait-1"
    budget: Budget = field(init=False)
    _contests: dict[str, str] = field(init=False)
    _choice_names: tuple[str, ...] = field(init=False)
    _choices: dict[float, dict[str, str]] = field(init=False, default_factory=dict)

    @property
    def wraps(self) -> bool:
        """Whether a name too wide is left whole, to wrap (the screen), rather than cut (paper)."""
        return self.layout == "screen"

    def __post_init__(self) -> None:
        self.contests = tuple(self.contests)
        self.budget = budget(self.layout)
        b = self.budget
        self._contests = unique_forms(
            self.contests,
            b.contest * (SCREEN_CONTEST_LINES if self.wraps else 1),
            (lambda n, w: cut(abbreviate_contest(n, w / SCREEN_CONTEST_LINES, b.contest_size)[0], w, b.contest_size))
            if self.wraps
            else (lambda n, w: cut(abbreviate_contest(n, w, b.contest_size)[0], w, b.contest_size)),
            fuller=(lambda n: abbreviate_contest(n)[0], base_contest),
            size=b.contest_size,
        )
        self._choice_names = tuple(sorted(set(self.choices)))

    @property
    def contest_width(self) -> float:
        return self.budget.contest

    @property
    def choice_width(self) -> float:
        return self.budget.choice

    def fit_choice(self, text: str, shorter: Iterable[str] = ()) -> str:
        """``text`` in the choices' cell: as it is if it fits, else the first of ``shorter`` that fits, else cut."""
        b = self.budget
        for form in (text, *shorter):
            if b.choice_size(form) <= b.choice:
                return form
        return cut(text, b.choice, b.choice_size)

    def marks_fit(self, text: str) -> bool:
        """Whether a line of marks fits the layout's marks line."""
        return self.budget.marks_size(text) <= self.budget.marks

    def cut_marks(self, text: str) -> str:
        return cut(text, self.budget.marks, self.budget.marks_size)

    @classmethod
    def for_extract(cls, extract: Mapping[str, object], layout: str = "portrait-1") -> PrintNames:
        """The printed names for every contest and choice in ``extract`` (its ballots' votes and its ``choices``)."""
        contests: set[str] = set()
        choices: set[str] = set()
        ballots = extract.get("ballots") or []
        assert isinstance(ballots, list)
        for ballot in ballots:
            for contest, value in ballot.get("votes") or []:
                contests.add(str(contest))
                choices.update(split_value(str(value))[1])
        listed = extract.get("choices") or {}
        assert isinstance(listed, dict)
        for contest, names in listed.items():
            contests.add(str(contest))
            choices.update(str(n) for n in names)
        return cls(contests, choices, layout)

    def contest(self, name: str) -> str:
        """A contest's printed name."""
        form = self._contests.get(name)
        if form is not None:
            return form
        b = self.budget
        if self.wraps:
            return cut(abbreviate_contest(name, b.contest, b.contest_size)[0], SCREEN_CONTEST_LINES * b.contest, b.contest_size)
        return cut(abbreviate_contest(name, b.contest, b.contest_size)[0], b.contest, b.contest_size)

    def _choice_map(self, width: float) -> dict[str, str]:
        if width not in self._choices:
            size = self.budget.choice_size
            if self.wraps:
                self._choices[width] = {n: n if size(n) <= width else initials(n) for n in self._choice_names}
            else:
                self._choices[width] = unique_forms(
                    self._choice_names, width, lambda n, w: shorten_choice(n, w, size), fuller=(initials,), size=size
                )
        return self._choices[width]

    def vote(self, value: str) -> str:
        """An expected value as printed: "NO VOTE", "redacted", or the choices joined by ", " (after "overvote: " for an overvote), shortened to fit."""
        b = self.budget
        if value == "no vote":
            return "NO VOTE"
        prefix, names = split_value(value)
        if not names:
            return cut(value, b.choice, b.choice_size)
        room = b.choice - b.choice_size(prefix) - b.choice_size(", ") * (len(names) - 1)
        share = max(b.min_share, math.floor(room / len(names)))
        forms = self._choice_map(share)
        return cut(prefix + ", ".join(forms.get(n, shorten_choice(n, share, b.choice_size)) for n in names), b.choice, b.choice_size)

    def vote_lines(self, value: str) -> list[str]:
        """An expected value as printed one choice per line (see the module documentation): "NO VOTE", "redacted", or each choice, the first after "overvote: " for an overvote, each shortened to fit the whole cell."""
        b = self.budget
        if value == "no vote":
            return ["NO VOTE"]
        prefix, names = split_value(value)
        if not names:
            return [cut(value, b.choice, b.choice_size)]
        forms = self._choice_map(b.choice)
        lines = [forms.get(n, n if self.wraps else shorten_choice(n, b.choice, b.choice_size)) for n in names]
        if prefix:
            room = b.choice - b.choice_size(prefix)
            first = names[0]
            short_first = lines[0] if b.choice_size(lines[0]) <= room or self.wraps else shorten_choice(first, room, b.choice_size)
            lines[0] = prefix + short_first
        return lines

    def shortened_contests(self) -> list[tuple[str, str]]:
        """``(printed, full)`` for each contest printed shorter than its full name (beyond the shortenings always made), by printed name."""
        return sorted((form, name) for name, form in self._contests.items() if form != base_contest(name))

    def key(self) -> list[tuple[str, str]]:
        """``(short, meaning)`` for each abbreviation used, in `ABBREVIATIONS` order, and "…" if anything was cut."""
        used_set: set[Abbreviation] = set()
        for name, form in self._contests.items():
            if form != base_contest(name):
                used_set.update(abbreviate_contest(name, None if ELLIPSIS in form else self.budget.contest, self.budget.contest_size)[1])
        used = [(a.key_label, a.meaning) for a in ABBREVIATIONS if a in used_set]
        cut_any = any(ELLIPSIS in f for f in self._contests.values()) or any(
            ELLIPSIS in f for m in self._choices.values() for f in m.values()
        )
        return used + ([(ELLIPSIS, "the rest of the name is left out")] if cut_any else [])


def split_value(value: str) -> tuple[str, list[str]]:
    """An expected value's prefix ("overvote: " or "") and its choice names; no names for "no vote" and "redacted"."""
    if value in ("no vote", "redacted"):
        return "", []
    prefix = ""
    if value.startswith("overvote: "):
        prefix, value = "overvote: ", value[len("overvote: ") :]
    return prefix, value.split(" + ")
