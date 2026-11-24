"""Deterministic grounding for Advisor replies (R6 trust rules, A4 number-check).

The agent never types numbers. Its structured output is
    {"narrative": str, "patterns_applied": [{"id": "TKN-NN", "why": str}]}
and every figure in the narrative is a reference `[[t<call>.<key>]]` to a value a
tool returned in THIS request. Here, in plain Python:

1. every reference must resolve, else the check fails;
2. outside references, any typed figure with a unit fails -- except a threshold
   phrase taken from a pattern retrieved in this request (e.g. "below ~50%") or
   a number the admin typed; a savings projection ("cut cost by ...") always fails;
3. on pass, references are rendered to their display strings; `findings` are
   the figures the answer listed in its `table` (empty when the narrative alone
   answers -> no evidence block);
   `citations` are patterns the agent applied AND got this request (from
   search_patterns, or attached by a data tool to a signal it detected);
4. on fail, the narration is replaced by a fixed note and the headline figures.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from advisor import WINDOWS
from advisor import catalog as advisor_catalog

MAX_MESSAGE_CHARS = 2000
MAX_FAIL_FINDINGS = 12

NOTE_UNVERIFIED = "I couldn't verify every figure in that answer, so here are the numbers directly."
NOTE_NO_DATA = (
    "I can't state figures without looking at the data. Ask me about a specific window, agent or user."
)
NOTE_DECLINED = (
    "I can only help with this solution's token economics — cost, usage, caching, limits, agents and users."
)
NOTE_TIMEOUT = "That analysis took too long to finish. Here are the figures gathered so far."
NOTE_FAILED = "The Advisor couldn't complete that request. Please try again."

REF_RE = re.compile(r"\[\[(t\d{1,2}\.[a-z0-9_.-]{1,80})\]\]")
_ANY_REF_RE = re.compile(r"\[\[.*?\]\]", re.S)
_ID_RE = re.compile(r"\bTKN-\d{2}\b")
_NUM = r"\d(?:[\d,]*\d)?(?:\.\d+)?"  # no trailing comma ("turn 1, tokens")
_PLACEHOLDER = "\ue000"  # stands in for a rendered figure reference
# A threshold quoted from a retrieved pattern, e.g. "below ~50%" or "above 3:1".
_THRESHOLD_PHRASE = re.compile(
    rf"(?:~|≈|below|above|over|under|more than|less than|at least|at most|exceeds?|beyond)\s*~?\s*({_NUM})\s*(%|:1)",
    re.I,
)
_WORD_NUMS = (
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|thousand|million|billion|half|quarter|dozen"
)
# A figure: a currency sign, digit or reference -- or a written amount ("half", "a third", "thirteen").
_FIG = (
    rf"(?:[$~€£{_PLACEHOLDER}\d]|\b(?:(?:a|one|two|three)[\s-]+)?(?:half|halves|thirds?|quarters?|fifths?|tenths?)\b"
    rf"|\b(?:{_WORD_NUMS})\b)"
)
_SAVE_WORDS = r"save|saves|saving|savings"
# Always a projection, whatever the tense.
_SAVINGS_RE = re.compile(
    # "... save ... by <figure>"
    rf"\b(?:{_SAVE_WORDS})\b[^.\n]{{0,60}}?\bby\b\s*(?:about|around|roughly|up to|~)?\s*{_FIG}"
    # "save <figure>", "savings of <figure>", "saving up to <figure>"
    rf"|\b(?:{_SAVE_WORDS})\s+(?:you\s+)?(?:of\s+|up\s+to\s+|about\s+|around\s+|roughly\s+|~\s*)?{_FIG}"
    # "shave/trim <figure> off"
    rf"|\b(?:shave|shaves|trim|trims)\s+(?:off\s+)?{_FIG}"
    # "<figure> saving(s)", "<figure> in savings"
    rf"|{_FIG}[^\s.]*\s+(?:in\s+)?(?:saving|savings)\b",
    re.I,
)
# A decrease: a projection only in a hypothetical, conditional or imperative sentence
# ("would cut cost by ...", "drops to ... with caching", "Cut input by half"), not an observed one
# ("Spend is lower by [[t1.total.delta]] than last week").
_DECREASE_RE = re.compile(
    # "... cut/reduce/lower ... by <figure>"
    r"\b(?P<verb>cut|cuts|reduce|reduces|reduced|reduction|lower|lowers|lowered|drop|drops|dropped|shave|trim|"
    r"fall|falls|fell|decrease|decreases|decreased|shrink|shrinks)\b"
    rf"[^.\n]{{0,60}}?\bby\b\s*(?:about|around|roughly|up to|~)?\s*{_FIG}"
    # "<figure> reduction"
    rf"|{_FIG}[^\s.]*\s+reduction\b"
    # "cost/spend/bill drops/falls/lowers to <figure>"
    rf"|\b(?:cost|costs|spend|bill)\b[^.\n]{{0,20}}?\b(?:drop|drops|fall|falls|lower|lowers|go|goes)\s+(?:down\s+)?to\b\s*{_FIG}",
    re.I,
)
_MODAL = re.compile(r"\b(?:would|could|will|can|might|may|should|expected\s+to|if|estimated)\b", re.I)
_CONDITION = re.compile(
    r"\b(?:with|after|by|once|when)\s+(?:enabling|switching|routing|caching|batching|using|"
    r"moving|adding|turning|applying|trimming|compressing|capping)\b",
    re.I,
)
_IMPERATIVE_LEAD = re.compile(r"^[\s\W]*(?:(?:and|then|just|simply|please)\s+)?$|\bto\s+$", re.I)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")
_CLAUSE_END = re.compile(r"[;:—]")
# A digit run followed by a unit WORD ("50 percent", "2000 dollars", "5 sessions").
_NUM_UNIT_WORD = re.compile(
    rf"({_NUM})[\s-]*(percent|per cent|pct|dollars?|usd|cents?|bucks|times|fold|million|billion|thousand|"
    r"tokens?|sessions?|turns?|users?|calls?|requests?)\b",
    re.I,
)
_COUNT_UNIT = re.compile(r"tokens?|sessions?|turns?|users?|calls?|requests?", re.I)
_NUMBER_WORDS = re.compile(
    rf"\b(?:{_WORD_NUMS})\b"
    r"[\s-]+(?:percent|per cent|dollars?|usd|bucks|grand|tokens?|times|fold|x)\b",
    re.I,
)
_LIST_ORDINAL = re.compile(r"(?m)^\s*\d{1,2}[.)]\s")
_DIGIT_TOKEN = re.compile(r"\S*\d\S*")
_WINDOW_TOKEN = re.compile(r"^(?:24h|7d|30d)$")
# Words that introduce a catalog guideline ("the healthy 3:1 target", "a ~50% threshold").
_GUIDELINE_WORDS = (
    "target",
    "threshold",
    "guideline",
    "healthy",
    "benchmark",
    "rule of thumb",
    "recommended",
    "below",
    "above",
    "under",
    "over",
    "less than",
    "more than",
    "at least",
    "~",
    "≈",
)
# Written-out dates ("October 11", "11 Oct", "Oct 11th") -- dates, not figures.
_MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_PHRASE = re.compile(
    rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}\b",
    re.I,
)
# Plain-language time windows ("last 7 days", "past 24 hours", "30-day window").
_WINDOW_PHRASE = re.compile(
    r"\b(?:24[\s\-‐‑–]*hours?|7[\s\-‐‑–]*days?|30[\s\-‐‑–]*days?|1[\s\-‐‑–]*day)\b", re.I
)
_SAFE_TOKEN = re.compile(r"^(?:TKN-\d{2}|#\d{1,2})$")
_MODEL_FAMILY = re.compile(r"(?:sonnet|haiku|opus|nova|claude|lite|pro)\s+$", re.I)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SUB_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


# ---- request validation ------------------------------------------------------


def validate_body(body: Any) -> dict[str, Any]:
    """POST /advisor/chats body -> {message, window, user, chat_id}. ValueError => 400."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message is required")
    message = message.strip()
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message must be at most {MAX_MESSAGE_CHARS} characters")
    window = body.get("window") or "7d"
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {', '.join(WINDOWS)}")
    user = body.get("user") or "all"
    if not isinstance(user, str) or len(user) > 254:
        raise ValueError("user must be 'all', an email or a name")
    chat_id = body.get("chat_id")
    if chat_id is not None and (not isinstance(chat_id, str) or not _UUID_RE.match(chat_id)):
        raise ValueError("chat_id must be a UUID")
    return {"message": message, "window": window, "user": user.strip(), "chat_id": chat_id}


_STOP = {
    "a",
    "an",
    "the",
    "of",
    "to",
    "in",
    "for",
    "on",
    "at",
    "by",
    "with",
    "and",
    "or",
    "but",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "do",
    "does",
    "did",
    "what",
    "which",
    "who",
    "whom",
    "whose",
    "why",
    "how",
    "when",
    "where",
    "i",
    "me",
    "my",
    "we",
    "our",
    "us",
    "you",
    "your",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "there",
    "here",
    "any",
    "all",
    "some",
    "right",
    "now",
    "over",
    "last",
    "past",
    "week",
    "month",
    "day",
    "today",
    "show",
    "tell",
    "give",
    "please",
    "can",
    "could",
    "would",
    "should",
    "will",
    "shall",
    "has",
    "have",
    "had",
    "much",
    "many",
    "more",
    "most",
    "far",
    "than",
    "everyone",
    "else",
    "so",
    "if",
    "about",
    "across",
    "vs",
    "versus",
    "just",
    "only",
    "really",
    "again",
    "also",
    "way",
}


def short_title(message: str, max_words: int = 4) -> str:
    """Instant chat title from the first question (replaced by the agent's summary)."""
    words = [
        w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", message or "") if w.lower().strip("'") not in _STOP
    ]
    title = " ".join(words[:max_words]) or "New chat"
    return title[:1].upper() + title[1:]


def clean_title(title: str) -> str:
    """Sanitise an agent-proposed title: letters/spaces/hyphens only, at most 5 words."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", title or "")[:5]
    out = " ".join(words)[:40].strip()
    return out[:1].upper() + out[1:] if out else ""


def new_id() -> str:
    return str(uuid.uuid4())


def history_for_agent(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Completed turns only (the reply as stored after grounding, including a fallback note)
    -> alternating user/assistant, starting with user and ending with assistant. All of them:
    the agent applies its own window."""
    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for m in messages:
        if m.get("role") == "user":
            pending_user = str(m.get("text") or "")
        elif m.get("role") == "assistant" and m.get("status") == "complete" and pending_user is not None:
            pairs.append((pending_user, str(m.get("text") or "")))
            pending_user = None
    out: list[dict[str, str]] = []
    for u, a in pairs:
        out.append({"role": "user", "text": u})
        out.append({"role": "assistant", "text": a})
    return out


# ---- grounding ---------------------------------------------------------------


def _norm(n: str) -> str:
    return f"{float(n.replace(',', '')):g}"


def _figure_map(tool_calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every citable value of this request: tool figures, plus scalar text labels
    (a date such as first_drop_day, a model or user name) the model may also cite."""
    figs = {}
    for call in tool_calls:
        prefix = call.get("ref")
        res = call.get("result") or {}
        # search_patterns labels echo the model-chosen query: never citable.
        labels = {} if call.get("tool") == "search_patterns" else (res.get("labels") or {})
        for key, v in labels.items():
            if isinstance(v, str) and v and REF_RE.fullmatch(f"[[{prefix}.{key.lower()}]]"):
                figs[f"{prefix}.{key.lower()}"] = {
                    "value": v,
                    "display": v,
                    "signal": key.replace("_", " "),
                    "level": res.get("data_level") or "fleet",
                    "label": True,
                }
        for key, f in (res.get("figures") or {}).items():
            figs[f"{prefix}.{key}"] = f
    return figs


def _retrieved(tool_calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for call in tool_calls:
        res = call.get("result") or {}
        if call.get("tool") == "search_patterns":
            for p in res.get("patterns") or []:
                out[p["pattern_id"]] = p
        # Patterns a data tool matched to a detected signal count as retrieved too.
        for s in res.get("signals") or []:
            for p in s.get("patterns") or []:
                if isinstance(p, dict) and p.get("pattern_id"):
                    out.setdefault(p["pattern_id"], p)
    return out


def _headline(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for call in tool_calls:
        for f in ((call.get("result") or {}).get("figures") or {}).values():
            if f.get("headline"):
                out.append(_finding(f))
    return out[:MAX_FAIL_FINDINGS]


def _finding(f: dict[str, Any]) -> dict[str, Any]:
    return {"signal": f["signal"], "observed": f["display"], "value": f["value"], "level": f["level"]}


def _citation_detail(p: dict[str, Any]) -> dict[str, Any]:
    return {
        k: p.get(k)
        for k in ("pattern_id", "name", "ref_no", "pillar", "quality_risk", "summary", "expected_effect")
    }


def _strip_ids(text: str) -> str:
    """Catalog ids are machine keys only (R7): show the pattern name instead."""
    names = {pid: p["name"] for pid, p in advisor_catalog.load().items()}
    return _ID_RE.sub(lambda m: names.get(m.group(0), "a catalog pattern"), text)


def _label_tokens(tool_calls: list[dict[str, Any]]) -> set[str]:
    """Digit-bearing strings tools returned as text: labels (dates, model ids, session ids,
    emails), figure signals ("cost per 1k tokens") and notes ("> 10× peer median")."""
    out: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, str):
            if any(ch.isdigit() for ch in v):
                out.add(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for call in tool_calls:
        if call.get("tool") == "search_patterns":  # its text is the model's own query, not data
            continue
        res = call.get("result") or {}
        walk(res.get("labels"))
        texts = [res.get("note") or ""] + [f.get("signal") or "" for f in (res.get("figures") or {}).values()]
        for t in texts:
            out |= {_strip_token(tok) for tok in _DIGIT_TOKEN.findall(t)}
    return out


def _strip_token(t: str) -> str:
    return t.strip(".,;:!?()[]{}\"'“”‘’~≈<>≥≤")


def _decrease_projection(text: str) -> bool:
    """A decrease phrase in a sentence that is hypothetical (a modal before it), conditional
    ("with caching") or imperative ("Cut input by half")."""
    pos = 0
    while m := _DECREASE_RE.search(text, pos):  # overlapping: a later verb may be the imperative one
        pos = m.start() + 1
        ends = [e.end() for e in _SENTENCE_END.finditer(text, 0, m.start())]
        start = ends[-1] if ends else 0
        nxt = _SENTENCE_END.search(text, m.end())
        sentence = text[start : nxt.start() if nxt else len(text)]
        # The clause lead ("Next step: cut ...") decides imperative; a modal anywhere before the
        # figure ("lower if you cut ... by") decides hypothetical.
        clause = [c.end() for c in _CLAUSE_END.finditer(text, start, m.start())]
        lead = text[clause[-1] if clause else start : m.start()]
        if (
            _MODAL.search(text, start, m.end())
            or _CONDITION.search(sentence)
            or (m.group("verb") and _IMPERATIVE_LEAD.search(lead))
        ):
            return True
    return False


def check_figures(
    narrative: str,
    figs: dict[str, Any],
    retrieved: dict[str, dict[str, Any]],
    admin_message: str,
    labels: set[str],
) -> str | None:
    """Return None if every figure is grounded, else a short failure code.

    Fail-closed: after removing valid references and allowed threshold phrases,
    ANY remaining digit fails unless it is a label a tool returned (a date, a model
    or session id), a time window, a list number, or a token the admin typed
    verbatim. Number words with a unit ("fifty dollars") also fail.
    """
    for span in _ANY_REF_RE.findall(narrative):
        m = REF_RE.fullmatch(span)
        if not m:
            return "malformed_ref"
        if m.group(1) not in figs:
            return f"unresolved_ref:{m.group(1)}"
    text = re.sub(
        r"\*\*|__", "", REF_RE.sub(_PLACEHOLDER, narrative)
    )  # markdown bold is dropped from the reply too
    for p in retrieved.values():  # pattern names (e.g. "...and savings observability") are not claims
        if p.get("name"):
            text = re.sub(re.escape(p["name"]), " ", text, flags=re.I)
    if _SAVINGS_RE.search(text) or _decrease_projection(text):
        return "savings_projection"
    words = _NUMBER_WORDS.search(text)
    if words:
        return f"typed_figure:number_words:{words.group(0)[:20]}"
    allowed = set()
    for p in retrieved.values():
        allowed |= advisor_catalog.threshold_figures(p)
    text = _THRESHOLD_PHRASE.sub(
        lambda m: " " if f"{_norm(m.group(1))}{m.group(2)}" in allowed else m.group(0), text
    )

    def _guideline(m: re.Match) -> str:
        before = text[max(0, m.start() - 45) : m.start()].lower()
        ok = f"{_norm(m.group(1))}{m.group(2)}" in allowed and any(w in before for w in _GUIDELINE_WORDS)
        return " " if ok else m.group(0)

    text = re.sub(rf"({_NUM})\s*(%|:1)", _guideline, text)
    text = _LIST_ORDINAL.sub(" ", text)
    text = _WINDOW_PHRASE.sub(" ", text)
    text = _DATE_PHRASE.sub(" ", text)
    # A number with a unit word must appear with the SAME unit word in the admin's message.
    # Echoing the admin's own bare (unit-less) number as a count ("cap at 2000" -> "2000 tokens") is allowed;
    # money, percent and multiple units still need the admin's exact unit.
    admin_pairs = {(_norm(n), u.lower()) for n, u in _NUM_UNIT_WORD.findall(admin_message or "")}
    admin_nums = {_norm(n) for n in re.findall(_NUM, admin_message or "")} - {
        _norm(n) for n, _ in admin_pairs
    }
    for n, u in _NUM_UNIT_WORD.findall(text):
        if (_norm(n), u.lower()) in admin_pairs:
            continue
        if _norm(n) in admin_nums and _COUNT_UNIT.fullmatch(u):
            continue
        return f"typed_figure:{n} {u}"[:40]
    admin_tokens = {_strip_token(t) for t in _DIGIT_TOKEN.findall(admin_message or "")}

    def _ok(tok: str, start: int) -> bool:
        if tok in labels or tok in admin_tokens or _WINDOW_TOKEN.match(tok) or _SAFE_TOKEN.match(tok):
            return True
        # a model family version, e.g. "Sonnet 5.5"
        return bool(re.fullmatch(r"\d(?:\.\d)?", tok) and _MODEL_FAMILY.search(text[:start][-12:]))

    for m in _DIGIT_TOKEN.finditer(text):
        tok = _strip_token(m.group(0))
        if _ok(tok, m.start()):
            continue
        # "per-1k-token": judge each hyphen-joined part ("5k-10k" still fails).
        parts = [_strip_token(p) for p in re.split(r"[-–—]", tok) if any(ch.isdigit() for ch in p)]
        if not all(_ok(p, m.start()) for p in parts):
            return f"typed_figure:{tok[:20]}"
    return None


def _canon(ref: str) -> str:
    head, _, key = ref.partition(".")
    return head + "." + re.sub(r"[._-]", "", key)


def _repair_refs(narrative: str, figs: dict[str, Any]) -> str:
    """Accept a reference whose key differs only in '.', '_' or '-' (e.g. users.near_quota vs
    users_near_quota) when exactly one real figure matches; anything else stays unresolved."""
    by_canon: dict[str, list[str]] = {}
    for ref in figs:
        by_canon.setdefault(_canon(ref), []).append(ref)

    def fix(m: re.Match) -> str:
        ref = m.group(1)
        if ref in figs:
            return m.group(0)
        hits = by_canon.get(_canon(ref), [])
        return f"[[{hits[0]}]]" if len(hits) == 1 else m.group(0)

    return REF_RE.sub(fix, narrative)


def _cite_signals(
    used: list[str], tool_calls: list[dict[str, Any]], citations: list[str], details: list[dict[str, Any]]
) -> tuple[list[str], list[dict[str, Any]]]:
    """A cited figure that belongs to a tool signal cites that signal's first pattern, unless the
    agent already cited one of its patterns -- so the same finding always carries its pattern."""
    citations, details, used_set = list(citations), list(details), set(used)
    for call in tool_calls:
        for s in (call.get("result") or {}).get("signals") or []:
            pats = [p for p in s.get("patterns") or [] if isinstance(p, dict) and p.get("pattern_id")]
            refs = {f"{call.get('ref')}.{k}" for k in s.get("figures") or []}
            if pats and refs & used_set and not any(p["pattern_id"] in citations for p in pats):
                citations.append(pats[0]["pattern_id"])
                details.append(_citation_detail(pats[0]))
    return citations, details


def _table_refs(table: list[Any] | None, figs: dict[str, Any]) -> tuple[list[str], str | None]:
    """The answer's table rows as figure references, or a failure code.

    Fail-closed like the narrative: each row must be exactly one figure reference that a tool
    returned this turn. The [[ ]] brackets are optional, and a key whose '.', '_' or '-' differ
    is repaired as in the narrative. A row repeated in the table is shown once.
    """
    table_refs: list[str] = []
    for row in table or []:
        row_text = str(row).strip()
        bracketed = row_text if row_text.startswith("[[") else f"[[{row_text}]]"
        ref_match = REF_RE.fullmatch(_repair_refs(bracketed, figs))
        if ref_match is None:
            return [], "malformed_ref"
        ref = ref_match.group(1)
        if ref not in figs:
            return [], f"unresolved_ref:{ref}"
        if ref not in table_refs:
            table_refs.append(ref)
    return table_refs, None


def ground(
    narrative: str,
    patterns_applied: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    admin_message: str,
    table: list[Any] | None = None,
) -> dict[str, Any]:
    """-> {reply, findings, citations, citation_details, notes, grounded, failure}.

    `findings` (the evidence table) are the figures the answer put in its `table`, not every
    figure the narrative cites, so an answer without a breakdown shows no table."""
    figs = _figure_map(tool_calls)
    narrative = _repair_refs(str(narrative or ""), figs)
    retrieved = _retrieved(tool_calls)
    ids = (str((p or {}).get("id") or "") for p in patterns_applied or [])
    citations = list(dict.fromkeys(pid for pid in ids if pid in retrieved))
    details = [_citation_detail(retrieved[pid]) for pid in citations]

    failure = check_figures(narrative, figs, retrieved, admin_message, _label_tokens(tool_calls))
    table_refs, table_failure = _table_refs(table, figs)
    failure = failure or table_failure
    if failure:
        return {
            "reply": NOTE_UNVERIFIED if figs else NOTE_NO_DATA,
            "findings": _headline(tool_calls),
            "citations": citations,
            "citation_details": details,
            "notes": [],
            "grounded": False,
            "failure": failure,
        }
    used = list(dict.fromkeys(REF_RE.findall(narrative)))
    reply = REF_RE.sub(lambda m: figs[m.group(1)]["display"], narrative)
    for ref in used:  # "[[t1.sessions]] sessions" -> "65 sessions", not "65 sessions sessions"
        d = figs[ref]["display"]
        w = re.search(r"[A-Za-z]+$", d)
        if w:
            reply = re.sub(re.escape(d) + r"\s+" + re.escape(w.group(0)) + r"\b", lambda _m, d=d: d, reply)
    reply = re.sub(r"\*\*|__", "", reply)  # plain-text UI: drop markdown bold
    refs_shown = list(dict.fromkeys(used + table_refs))  # in the text or the table, each once
    citations, details = _cite_signals(refs_shown, tool_calls, citations, details)
    notes = []
    for call in tool_calls:
        if any(ref.startswith(f"{call.get('ref')}.") for ref in refs_shown):
            note = (call.get("result") or {}).get("note")
            if note and note not in notes:
                notes.append(note)
    return {
        "reply": _strip_ids(reply).strip(),
        "findings": [_finding(figs[ref]) for ref in table_refs if not figs[ref].get("label")],
        "citations": citations,
        "citation_details": details,
        "notes": notes,
        "grounded": True,
        "failure": None,
    }


def fallback_reply(note: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Timeout / turn-limit / empty output: the note plus whatever headline figures exist."""
    return {
        "reply": note,
        "findings": _headline(tool_calls),
        "citations": [],
        "citation_details": [],
        "notes": [],
        "grounded": False,
        "failure": "no_narrative",
    }


def declined() -> dict[str, Any]:
    return {
        "reply": NOTE_DECLINED,
        "findings": [],
        "citations": [],
        "citation_details": [],
        "notes": [],
        "grounded": True,
        "failure": None,
    }
