"""Live recall check for the pattern Knowledge Base (opt-in: -m live).

Each pattern's own signal text is used as the query; the pattern must come back
in the top 3 with a score at or above ADVISOR_KB_MIN_SCORE (the cutoff
search_patterns applies), so the cutoff never drops a correct hit. Prints the
score ranges so the cutoff can be tuned.
"""

from __future__ import annotations

import re

import boto3
import pytest
from conftest import REGION

pytestmark = pytest.mark.live


def test_recall_at_3(stack_outputs, cp_import):
    kb = stack_outputs.get("AdvisorKbId")
    if not kb:
        pytest.skip("AdvisorKbId output missing")
    rt = boto3.client("bedrock-agent-runtime", region_name=REGION)
    hits, scores, own = 0, [], []
    pats = cp_import("advisor.catalog").load()  # the seed catalog when run from the repo
    cutoff = cp_import("config").ADVISOR_KB_MIN_SCORE
    for pid, p in pats.items():
        q = re.split(r"[;.]", p["signal"])[0]
        res = rt.retrieve(
            knowledgeBaseId=kb,
            retrievalQuery={"text": f"Cost signal: {q}"},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": 3,
                    "filter": {"equals": {"key": "doc_type", "value": "pattern"}},
                }
            },
        )
        got = [r["metadata"].get("pattern_id") for r in res["retrievalResults"]]
        scores += [r["score"] for r in res["retrievalResults"]]
        own += [r["score"] for r in res["retrievalResults"] if r["metadata"].get("pattern_id") == pid][:1]
        hits += pid in got
    print(
        f"recall@3 {hits}/{len(pats)}; score range {min(scores):.3f}..{max(scores):.3f}; "
        f"correct hits {min(own):.3f}..{max(own):.3f} (cutoff {cutoff})"
    )
    assert hits >= len(pats) - 2
    assert min(own) >= cutoff
