"""search_patterns: semantic retrieval over the pattern catalog (Bedrock KB on S3 Vectors).

The KB ranks; the bundled catalog supplies the pattern fields, so citation
details are byte-identical to the catalog file. If the KB is not configured,
not yet ingested, or erroring, a deterministic keyword match over the same
catalog is used (`source: "fallback"`), so the Advisor keeps working.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

import config
from advisor import catalog as advisor_catalog

_RETRYABLE = {
    "ThrottlingException",
    "DependencyFailedException",
    "BadGatewayException",
    "InternalServerException",
    "ServiceQuotaExceededException",
}
# One Retrieve attempt can take up to connect 2 s + read 4 s (see _client). Retry only while a
# whole further attempt still fits in this budget, so search_patterns returns (or falls back)
# inside the agent's 8 s per-tool limit.
_ATTEMPT_SECONDS = 6.0
_RETRIEVE_BUDGET_SECONDS = 7.5
_DETAIL_FIELDS = (
    "pattern_id",
    "name",
    "ref_no",
    "pillar",
    "quality_risk",
    "summary",
    "expected_effect",
    "recommendation",
    "threshold",
)


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client():
    return boto3.client(
        "bedrock-agent-runtime",
        region_name=config.AWS_REGION,
        config=BotoConfig(connect_timeout=2, read_timeout=4, retries={"total_max_attempts": 1}),
    )


def _detail(p: dict[str, Any], score: float | None) -> dict[str, Any]:
    d = {f: p.get(f) for f in _DETAIL_FIELDS}
    d["pillar"] = p["pillar_key"]
    d["quality_risk"] = p["risk_key"]
    d["quality_risk_text"] = p["quality_risk"]
    if score is not None:
        d["score"] = round(float(score), 4)
    return d


def _retrieve(query: str, n: int) -> list[tuple[str, float]]:
    req = {
        "knowledgeBaseId": config.ADVISOR_KB_ID,
        "retrievalQuery": {"text": f"Cost signal: {query}"},
        "retrievalConfiguration": {
            "vectorSearchConfiguration": {
                "numberOfResults": n,
                "filter": {"equals": {"key": "doc_type", "value": "pattern"}},
            }
        },
    }
    start = time.monotonic()
    attempt = 0
    while True:
        try:
            resp = _client().retrieve(**req)
            break
        except ClientError as exc:
            attempt += 1
            wait = 0.2 * attempt
            fits = time.monotonic() - start + wait + _ATTEMPT_SECONDS <= _RETRIEVE_BUDGET_SECONDS
            if exc.response.get("Error", {}).get("Code") in _RETRYABLE and attempt < 3 and fits:
                time.sleep(wait)
                continue
            raise
    out = []
    for r in resp.get("retrievalResults", []):
        meta = r.get("metadata") or {}
        pid = str(meta.get("pattern_id") or "")
        if pid:
            out.append((pid, float(r.get("score") or 0.0)))
    return out


def pattern_details(ids: list[str]) -> list[dict[str, Any]]:
    """Catalog fields for the given pattern ids (same shape as a search result)."""
    patterns = advisor_catalog.load()
    return [_detail(patterns[p], None) for p in ids if p in patterns]


def search_patterns(query: str, k: int = 3) -> dict[str, Any]:
    q = str(query or "").strip()
    if not 3 <= len(q) <= 500:
        raise ValueError("query must be 3-500 characters")
    k = max(1, min(int(k or 3), 5))
    patterns = advisor_catalog.load()
    results: list[dict[str, Any]] = []
    source = "kb"
    if config.ADVISOR_KB_ID:
        try:
            seen = set()
            for pid, score in _retrieve(q, 5):
                if pid not in patterns:
                    logger.warning(
                        "[advisor-kb] KB returned %r, not in the bundled catalog (KB/catalog drift)", pid
                    )
                    continue
                if score < config.ADVISOR_KB_MIN_SCORE or pid in seen:
                    continue
                seen.add(pid)
                results.append(_detail(patterns[pid], score))
                if len(results) >= k:
                    break
        except (ClientError, BotoCoreError) as exc:
            logger.warning("[advisor-kb] retrieve failed, using fallback (%s)", type(exc).__name__)
            results = []
    if not results:
        source = "fallback"
        results = [_detail(p, None) for p in advisor_catalog.keyword_search(patterns, q, k)]
    # No echo of the query in labels: labels are citable, and the query is model-chosen text.
    out = {
        "tool": "search_patterns",
        "available": bool(results),
        "data_level": "catalog",
        "figures": {},
        "labels": {},
        "patterns": results,
        "source": source,
    }
    if not results:
        out["reason"] = "no catalog pattern matched this signal"
    return out
