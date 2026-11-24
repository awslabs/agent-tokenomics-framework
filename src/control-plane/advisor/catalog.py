"""Tokenomics pattern catalog: parse, split for the Knowledge Base, index, fallback.

Single source of truth: seed/data/advisor/tokenomics-patterns-kb.md (bundled with
the Advisor runtime image and the AdvisorWorker). Entries are `### TKN-NN — Name`
headings followed by `- **field:** value` bullets.

- `split_catalog(md)` -> one KB document per pattern (+ two context docs), each
  with a `.metadata.json` sidecar. Used by CDK at synth time and by the tests,
  so the ingested documents always match the catalog file.
- `load()` / `parse_catalog(md)` -> {id: pattern} for citation details and the
  keyword fallback used when the Knowledge Base is unavailable.
- `index_text(patterns)` -> the compact 20-line index inlined in the agent prompt.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

FIELDS = (
    "pillar",
    "problem",
    "signal",
    "threshold",
    "recommendation",
    "expected_effect",
    "quality_risk",
    "applies_to",
)
_ENTRY_RE = re.compile(r"^### (TKN-\d{2}) — (.+?)\s*$", re.M)
_SECTION_RE = re.compile(r"^## (.+?)\s*$", re.M)
_FIELD_RE = re.compile(r"^- \*\*(\w+):\*\*\s?(.*)$")
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|:1)")
_CONTEXT_SECTIONS = {
    "Key cost concepts": "key-cost-concepts",
    "Applying these patterns together": "applying-patterns",
}


def _risk(text: str) -> str:
    t = text.lower()
    if t.startswith("low–medium") or t.startswith("low-medium"):
        return "low_medium"
    for r in ("high", "medium", "low"):
        if t.startswith(r):
            return r
    return "medium"


def _pillar(text: str) -> str:
    t = text.lower()
    for p in ("optimisation", "governance", "observability"):
        if t.startswith(p):
            return p
    return "optimisation"


def _first_sentence(text: str) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text).strip()


def parse_catalog(md: str) -> dict[str, dict[str, Any]]:
    """{TKN-NN: {pattern_id, name, ref_no, fields..., pillar_key, risk_key, text}}."""
    out: dict[str, dict[str, Any]] = {}
    heads = list(_ENTRY_RE.finditer(md))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        nxt = _SECTION_RE.search(md, h.end())
        if nxt and nxt.start() < end:
            end = nxt.start()
        body = md[h.end() : end]
        text = md[h.start() : end].strip().rstrip("-").strip()
        fields: dict[str, str] = {}
        cur = None
        for line in body.splitlines():
            m = _FIELD_RE.match(line)
            if m:
                cur = m.group(1)
                fields[cur] = m.group(2).strip()
            elif cur and line.startswith("  ") and line.strip():
                fields[cur] += " " + line.strip()
            elif not line.strip():
                cur = None
        pid = h.group(1)
        out[pid] = {
            "pattern_id": pid,
            "name": h.group(2).strip(),
            "ref_no": int(pid.split("-")[1]),
            **{f: fields.get(f, "") for f in FIELDS},
            "pillar_key": _pillar(fields.get("pillar", "")),
            "risk_key": _risk(fields.get("quality_risk", "")),
            "summary": _first_sentence(fields.get("problem", "")),
            "text": text,
        }
    return out


def _context_sections(md: str) -> dict[str, str]:
    heads = list(_SECTION_RE.finditer(md))
    out = {}
    for i, h in enumerate(heads):
        key = _CONTEXT_SECTIONS.get(h.group(1).strip())
        if not key:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        out[key] = md[h.start() : end].strip().rstrip("-").strip()
    return out


def _meta(attrs: dict[str, Any]) -> dict[str, Any]:
    """Bedrock KB sidecar format; metadata is for filtering/citations, not embedded."""
    out = {}
    for k, v in attrs.items():
        if isinstance(v, list):
            value = {"type": "STRING_LIST", "stringListValue": [str(x) for x in v]}
        elif isinstance(v, (int, float)):
            value = {"type": "NUMBER", "numberValue": v}
        else:
            value = {"type": "STRING", "stringValue": str(v)}
        out[k] = {"value": value, "includeForEmbedding": False}
    return {"metadataAttributes": out}


def split_catalog(md: str) -> list[tuple[str, str, str]]:
    """[(s3_key, document_text, metadata_json)] -- one chunk per pattern (NONE chunking)."""
    docs = []
    for pid, p in sorted(parse_catalog(md).items()):
        meta = _meta(
            {
                "doc_type": "pattern",
                "pattern_id": pid,
                "name": p["name"],
                "ref_no": p["ref_no"],
                "pillar": p["pillar_key"],
                "quality_risk": p["risk_key"],
                "applies_to": [a.strip() for a in p["applies_to"].split(",") if a.strip()],
            }
        )
        docs.append((f"patterns/{pid}.md", p["text"] + "\n", json.dumps(meta, sort_keys=True)))
    for key, text in sorted(_context_sections(md).items()):
        docs.append(
            (f"context/{key}.md", text + "\n", json.dumps(_meta({"doc_type": "context"}), sort_keys=True))
        )
    return docs


def threshold_figures(p: dict[str, Any]) -> set[str]:
    """Numbers a pattern states as thresholds, e.g. {'50%', '3:1'} (grounding allow-list)."""
    return {f"{float(n):g}{u}" for n, u in _NUM_RE.findall(p.get("threshold", ""))}


def index_text(patterns: dict[str, dict[str, Any]]) -> str:
    """Compact index for the prompt: id · name · one-line signal."""
    lines = []
    for pid in sorted(patterns):
        p = patterns[pid]
        sig = _first_sentence(p["signal"])[:110]
        lines.append(f"{pid} · {p['name']} · {sig}")
    return "\n".join(lines)


def keyword_search(patterns: dict[str, dict[str, Any]], query: str, k: int = 3) -> list[dict[str, Any]]:
    """Deterministic fallback when the KB is unavailable: token-overlap ranking."""
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    scored = []
    for pid, p in patterns.items():
        hay = " ".join((p["name"], p["signal"], p["problem"], p["threshold"])).lower()
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((score, pid))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [patterns[pid] for _, pid in scored[:k]]


def _default_path() -> Path:
    import config

    p = Path(config.ADVISOR_CATALOG_FILE)
    if p.exists():
        return p
    # Running from the repo (tests / local): fall back to the seed copy.
    return Path(__file__).resolve().parents[3] / "seed" / "data" / "advisor" / "tokenomics-patterns-kb.md"


@lru_cache(maxsize=1)
def load() -> dict[str, dict[str, Any]]:
    return parse_catalog(_default_path().read_text(encoding="utf-8"))
