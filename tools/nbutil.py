#!/usr/bin/env python3
"""Notebook helpers: build cells correctly and validate existing notebooks.

The `.ipynb` format stores a cell's body as a list of strings where **each line
ends with a newline** (except optionally the last). Consumers reconstruct the cell
with ``"".join(source)``. Emitting lines without terminators produces a file that
looks fine in a diff but collapses into one unparseable line when opened - so
notebooks here are always built through :func:`code` / :func:`markdown`.

Usage:
    python tools/nbutil.py validate            # check every notebook
    python tools/nbutil.py validate <file>...  # check specific notebooks
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import sys

NB_METADATA = {
    "application/vnd.databricks.v1+notebook": {
        "dashboards": [],
        "language": "python",
        "notebookMetadata": {"pythonIndentUnit": 4},
        "notebookName": "",
        "widgets": {},
    },
    "language_info": {"name": "python"},
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
}


def _lines(text: str) -> list[str]:
    """Split ``text`` into newline-terminated source lines."""
    text = text.strip("\n")
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + [parts[-1]]


def markdown(text: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": _lines(text)}


def code(text: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": _lines(text),
    }


def notebook(cells: list[dict]) -> dict:
    return {
        "cells": cells,
        "metadata": dict(NB_METADATA),
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def write(path: str, cells: list[dict]) -> None:
    with open(path, "w") as fh:
        fh.write(json.dumps(notebook(cells), indent=1) + "\n")


def validate(path: str) -> list[str]:
    """Return a list of problems found in ``path`` (empty when valid)."""
    problems: list[str] = []
    try:
        with open(path) as fh:
            nb = json.load(fh)
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc}"]

    if nb.get("nbformat") != 4:
        problems.append(f"nbformat is {nb.get('nbformat')}, expected 4")
    if "cells" not in nb:
        return problems + ["no 'cells' key"]

    for i, cell in enumerate(nb["cells"]):
        kind = cell.get("cell_type")
        src = cell.get("source")
        if kind not in ("code", "markdown"):
            problems.append(f"cell {i}: unexpected cell_type {kind!r}")
            continue
        if not isinstance(src, list):
            problems.append(f"cell {i}: source is {type(src).__name__}, expected list")
            continue

        # Every line but the last must be newline-terminated.
        for j, line in enumerate(src[:-1]):
            if not line.endswith("\n"):
                problems.append(
                    f"cell {i} line {j}: missing trailing newline "
                    "(cell would collapse into one line)"
                )
                break

        if kind == "code":
            if cell.get("outputs"):
                problems.append(f"cell {i}: committed outputs should be cleared")
            if cell.get("execution_count") is not None:
                problems.append(f"cell {i}: execution_count should be null")
            body = "".join(src)
            if not body.lstrip().startswith(("%", "!")):
                try:
                    ast.parse(body)
                except SyntaxError as exc:
                    problems.append(f"cell {i}: Python syntax error: {exc}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["validate"])
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args()

    paths = args.paths
    if not paths:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = sorted(glob.glob(os.path.join(root, "notebooks", "*.ipynb")))
    if not paths:
        print("no notebooks found")
        return 1

    failed = 0
    for path in paths:
        problems = validate(path)
        name = os.path.basename(path)
        if problems:
            failed += 1
            print(f"FAIL {name}")
            for p in problems:
                print(f"       {p}")
        else:
            print(f"ok   {name}")
    print()
    if failed:
        print(f"{failed} of {len(paths)} notebook(s) have problems")
        return 1
    print(f"All {len(paths)} notebook(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
