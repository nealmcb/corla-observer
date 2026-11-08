"""Which functions the tests leave unrun: missed lines per function, from a coverage data file.

Written for the November 2026 review of corla-observer's test coverage, to
find which functions in the evidence and scheduling code the default test
suite never reaches, rather than reading line ranges off a coverage report.

Make the data file with branch coverage, for example:

    uv run --with pytest-cov pytest --cov=corla_observer --cov-branch --cov-report=

then list each module's functions with the most missed lines:

    uv run --with coverage python scripts/coverage_by_function.py src/corla_observer/timestamp.py

A function's count includes lines in functions nested inside it.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


def functions(path: Path) -> list[tuple[int, int, str]]:
    """Each function in ``path``: its first and last line, and its name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def missed_by_function(data_file: str, path: Path) -> list[tuple[str, int, int, int]]:
    """Each function with missed lines: its name, first line, lines missed and statements, most missed first."""
    import coverage

    cov = coverage.Coverage(data_file=data_file)
    cov.load()
    _, statements, _, missing, _ = cov.analysis2(str(path))
    rows = []
    for first, last, name in functions(path):
        body = [line for line in statements if first <= line <= last]
        missed = [line for line in missing if first <= line <= last]
        if missed:
            rows.append((name, first, len(missed), len(body)))
    return sorted(rows, key=lambda row: (-row[2], row[1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("modules", nargs="+", type=Path, help="source files to report on")
    parser.add_argument("--data-file", default=".coverage", help="the coverage data file (default: .coverage)")
    parser.add_argument("--top", type=int, default=12, help="functions to show per module (default: 12)")
    args = parser.parse_args()
    for path in args.modules:
        print(f"== {path}")
        for name, first, missed, body in missed_by_function(args.data_file, path)[: args.top]:
            never = "  (never run)" if missed == body else ""
            print(f"  {name:45} line {first:5}  missed {missed:3} of {body:3}{never}")


if __name__ == "__main__":
    main()
