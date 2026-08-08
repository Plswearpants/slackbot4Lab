"""Measure whether the right evidence reaches the prompt.

Every failure observed in production so far has been a retrieval failure, not a
generation one: the answer to "I remember we have X-ray spectroscopy" was in the
index the whole time, but the chunk never reached the model, because the channel
writes XRD and the question said "X-ray spectroscopy". The model behaved
correctly on what it was handed.

So this measures recall, not answer quality. A case names a question and the
strings that must appear in the retrieved chunks for the question to be
answerable at all. That runs offline against local embeddings — no API key, no
spend, no sampling variance — which makes it cheap enough to run on every change
to chunking, fusion, or the embedding model, the three things most likely to
break retrieval silently.

Whether a good answer was then written from good evidence is a separate question
this does not attempt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Case:
    question: str
    channel: str
    must_retrieve: list[str] = field(default_factory=list)
    why: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Case:
        return cls(
            question=str(d["question"]),
            channel=str(d["channel"]),
            must_retrieve=[str(s) for s in d.get("must_retrieve", [])],
            why=str(d.get("why", "")),
        )


@dataclass
class Result:
    case: Case
    found: list[str]
    missing: list[str]
    chunk_ids: list[int]

    @property
    def passed(self) -> bool:
        return not self.missing


def load_cases(path: Path | str) -> list[Case]:
    import yaml

    path = Path(path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or []
    return [Case.from_dict(d) for d in data]


async def run_case(retriever, case: Case, top_k: int, glossary=None) -> Result:
    from slackqa.answerer import build_search_query

    # Exactly the query production would build, glossary expansion included.
    query = build_search_query(
        case.question, glossary=glossary, channel_id=case.channel
    )
    hits = await retriever.retrieve(case.channel, query, top_k)
    haystack = "\n".join(h.text for h in hits).lower()
    found, missing = [], []
    for needle in case.must_retrieve:
        (found if needle.lower() in haystack else missing).append(needle)
    return Result(case, found, missing, [h.chunk_id for h in hits])


async def run(retriever, cases: list[Case], top_k: int, glossary=None) -> list[Result]:
    return [await run_case(retriever, c, top_k, glossary) for c in cases]


def format_report(results: list[Result], top_k: int) -> str:
    lines: list[str] = []
    passed = sum(1 for r in results if r.passed)

    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines.append(f"{mark}  {r.case.question[:66]}")
        if not r.passed:
            lines.append(f"      missing from top-{top_k}: {', '.join(r.missing)}")
            if r.case.why:
                lines.append(f"      why it matters: {r.case.why}")

    if not results:
        return "No eval cases. Add some to evals/retrieval.yaml"

    lines.append("")
    lines.append(f"recall: {passed}/{len(results)} cases have their evidence in top-{top_k}")
    return "\n".join(lines)
