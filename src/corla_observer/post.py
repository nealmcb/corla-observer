"""The public post proposed before the seed is rolled, giving the hash of the evidence's SHA256SUMS file.

Since 2018 a post on X has given, shortly before each dice roll, the
SHA-256 of a file listing the hashes of the data to be audited: a public,
dated commitment beside the OpenTimestamps and Sigstore ones.
`commit_evidence` proposes the text of that post (`propose_post`) whenever
it writes a SHA256SUMS file before a seed is recorded, so that it can be
posted while the file is being signed. The post is the coordinator's to
make; nothing here posts it.

The text follows the earlier posts (kept in each election's
``observerfiles/``, such as ``2026/primary/observerfiles/2026-07-13-status-
2076696781304574361.txt``), shortened to fit X's limit of 280 characters,
in which every link counts as 23 (`x_length`). The hash list's file name
does not fit, so the post gives only its hash; ``evidence-timestamping.md``
in the mirror explains how to find the file from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from corla_observer.localtime import parse_instant, to_colorado

#: The Audit Center page, with ``www.``: the address without it now lands on the Secretary of State's home page.
AUDIT_CENTER_LINK = "https://www.coloradosos.gov/pubs/elections/auditCenter.html"
#: How to check the hash: the mirror's explanation of its timestamps.
CHECK_LINK = "https://github.com/nealmcb/auditcenter/blob/main/observerfiles/evidence-timestamping.md"
#: X's limit for a post, and the length it counts for every link.
X_LIMIT = 280
X_LINK_LENGTH = 23

_LINK_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class ProposedPost:
    text: str
    #: The length as X counts it.
    length: int

    @property
    def too_long(self) -> bool:
        return self.length > X_LIMIT


def x_length(text: str) -> int:
    """The length of ``text`` as X counts it: every link as 23 characters, everything else one each."""
    return len(_LINK_RE.sub("x" * X_LINK_LENGTH, text))


def roll_phrase(roll_time: str | None, now: datetime) -> str:
    """When the seed is rolled, as the post says it: ``at 10 AM`` the same Colorado day, ``Monday at 10 AM`` before it, ``soon`` if unknown."""
    moment = parse_instant(roll_time) if roll_time else None
    if moment is None:
        return "soon"
    local, today = to_colorado(moment), to_colorado(now)
    hour = local.strftime("%I").lstrip("0")
    clock = (
        f"{hour} {local.strftime('%p')}"
        if local.minute == 0
        else f"{hour}:{local.strftime('%M %p')}"
    )
    return (
        f"at {clock}"
        if local.date() == today.date()
        else f"{local.strftime('%A')} at {clock}"
    )


def propose_post(
    sums_sha256: str, roll_time: str | None, now: datetime
) -> ProposedPost:
    """The post giving ``sums_sha256``, the hash of the SHA256SUMS file, before the roll at ``roll_time`` (UTC; None if unknown)."""
    text = "\n".join(
        [
            f"Watch Colorado's risk-limiting audits! Seed dice roll {roll_phrase(roll_time, now)}: {AUDIT_CENTER_LINK}",
            f"Secure timestamps of data to be audited via SHA-256: {sums_sha256}",
            CHECK_LINK,
            "@COSecofState",
            "#RiskLimitingAudits",
        ]
    )
    return ProposedPost(text, x_length(text))
