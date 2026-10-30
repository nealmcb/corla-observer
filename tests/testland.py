"""A workspace with Testland's extract (corla_observer.testcounty: up to 60 contests a ballot) and an observer link, for tests.

`make_testland` builds it the way the tests of the extract do: the
synthetic election's audit center files, a seed and a prediction (which any
extract needs), then Testland's files uploaded with the county chosen, its
extract and one observer link. ``python tests/testland.py DIR NAME...`` builds one
in DIR with a link for each NAME and prints their tokens, for the browser check
(tests/test_browser_per_contest.py, scripts/browser_per_contest.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))

from corla_observer.datamap import build_map  # noqa: E402
from corla_observer.events import EventLog  # noqa: E402
from corla_observer.extract import build_extract  # noqa: E402
from corla_observer.intake import take  # noqa: E402
from corla_observer.practice import LinkStore  # noqa: E402
from corla_observer.predict import predict, record_prediction  # noqa: E402
from corla_observer.seeds import enter_seed  # noqa: E402
from corla_observer.testcounty import COUNTY, generate  # noqa: E402
from corla_observer.workspace import Workspace  # noqa: E402
from synthetic import CONTEST_IDS, contests_list, election_files  # noqa: E402
from test_datamap import fetch  # noqa: E402

ACTOR = "coordinator:alice"
SEED = "12345678901234567890"


def make_testland(root: Path, observers: tuple[str, ...] = ("lori",)) -> list[str]:
    """Build a workspace at ``root`` with Testland's extract; return an observer link's token for board 1 for each observer."""
    ws = Workspace.open(root)
    with EventLog.open(ws.root) as log:
        fetch(ws, log, election_files(finalReports_ContestsListRound1_csv=contests_list(), finalReports_ContestIDsByCounty_csv=CONTEST_IDS))
        enter_seed(log, SEED, ACTOR, "announced at the dice roll")
        record_prediction(ws, log, predict(build_map(log, ws), counties=["Adams"]), ACTOR)
        for name, path in generate(root.parent / (root.name + "-files")).items():
            intake = take(ws, log, path, name, ACTOR, county=COUNTY)
            assert intake.accepted, intake.error
        build_extract(ws, log, COUNTY, actor=ACTOR)
        tokens = [LinkStore(ws.root).create("observer", name, COUNTY, 1, log, actor=ACTOR)[0] for name in observers]
    return tokens


if __name__ == "__main__":
    print("\n".join(make_testland(Path(sys.argv[1]), tuple(sys.argv[2:]) or ("lori",))))
