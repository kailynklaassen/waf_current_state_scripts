#!/usr/bin/env python3
"""Regenerate ``waf_questions.py`` from the published WAF Assessment Tool question bank.

The WAF Assessment Tool (https://databricks-solutions.github.io/waf-assessment-tool/)
serves its question bank as a static JSON file. This script downloads it and emits the
``PILLAR_NAMES`` / ``QUESTIONS`` metadata module used by the pillar notebooks.

Usage:
    python tools/gen_questions.py [--url URL] [--out waf_questions.py]
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request

DEFAULT_URL = (
    "https://databricks-solutions.github.io/waf-assessment-tool/src/data/waf-data.json"
)


def build(data: list[dict]) -> str:
    pillars: dict[str, str] = {}
    meta: dict[str, dict[str, str]] = {}
    for pillar in data:
        pillars[pillar["id"]] = pillar["name"]
        for q in pillar.get("questions", []):
            meta[q["identifier"]] = {
                "pillar_id": pillar["id"],
                "principle": q.get("principle") or "",
                "title": (q.get("title") or "").strip(),
            }

    out = [
        '"""Auto-generated WAF question metadata.',
        "",
        f"Source: {DEFAULT_URL}",
        "(the question bank behind the Databricks WAF Assessment Tool).",
        "",
        "Regenerate with ``python tools/gen_questions.py``.",
        '"""',
        "",
        "PILLAR_NAMES = {",
    ]
    out += [f"    {k!r}: {v!r}," for k, v in pillars.items()]
    out += ["}", "", "#: question id -> {pillar_id, principle, title}", "QUESTIONS = {"]
    for qid, q in meta.items():
        out += [
            f"    {qid!r}: {{",
            f"        \"pillar_id\": {q['pillar_id']!r},",
            f"        \"principle\": {q['principle']!r},",
            f"        \"title\": {q['title']!r},",
            "    },",
        ]
    out += [
        "}",
        "",
        "",
        "def pillar_questions(pillar_id: str) -> dict:",
        '    """Metadata for every question in a pillar, in source order."""',
        "    return {",
        '        qid: q for qid, q in QUESTIONS.items() if q["pillar_id"] == pillar_id',
        "    }",
        "",
        "",
        "def pillar_meta(pillar_id: str) -> dict:",
        '    """``{qid: {principle, title}}`` shaped for ``Ctx.run_checks``."""',
        "    return {",
        '        qid: {"principle": q["principle"], "title": q["title"]}',
        "        for qid, q in QUESTIONS.items()",
        '        if q["pillar_id"] == pillar_id',
        "    }",
        "",
    ]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(os.path.dirname(__file__)), "waf_questions.py"),
    )
    args = ap.parse_args()

    with urllib.request.urlopen(args.url) as resp:  # noqa: S310 - fixed https URL
        data = json.load(resp)

    src = build(data)
    with open(args.out, "w") as fh:
        fh.write(src)

    total = sum(len(p.get("questions", [])) for p in data)
    print(f"Wrote {args.out}: {len(data)} pillars, {total} questions")


if __name__ == "__main__":
    main()
