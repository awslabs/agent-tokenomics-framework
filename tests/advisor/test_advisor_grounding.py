"""Unit tests for Advisor grounding, catalog split, KB retrieval and the data tools.

Run from the repo root:
    src/app-plane/.venv/bin/python -m pytest tests/advisor/test_advisor_grounding.py -q
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
CATALOG = (_ROOT / "seed" / "data" / "advisor" / "tokenomics-patterns-kb.md").read_text(encoding="utf-8")


@pytest.fixture
def mods(monkeypatch, cp_import):
    monkeypatch.delenv("ADVISOR_KB_ID", raising=False)
    cat, gr, kb = cp_import("advisor.catalog", "advisor.grounding", "advisor.kb")
    cat.load.cache_clear()
    return cat, gr, kb


def call(ref, tool, figures=None, note=None, patterns=None):
    res = {"tool": tool, "available": True, "figures": figures or {}}
    if note:
        res["note"] = note
    if patterns is not None:
        res["patterns"] = patterns
    return {"ref": ref, "tool": tool, "result": res}


def fig(value, display, signal, level, headline=False):
    f = {"value": value, "display": display, "signal": signal, "level": level}
    if headline:
        f["headline"] = True
    return f


@pytest.fixture
def calls(mods):
    cat, _, _ = mods
    p = cat.load()
    detail = lambda pid: {
        **{
            k: p[pid][k]
            for k in (
                "pattern_id",
                "name",
                "ref_no",
                "summary",
                "expected_effect",
                "recommendation",
                "threshold",
            )
        },
        "pillar": p[pid]["pillar_key"],
        "quality_risk": p[pid]["risk_key"],
    }
    return [
        call(
            "t1",
            "get_cost_summary",
            {
                "agent.orchestrator.cost": fig(0.4213, "$0.42", "orchestrator cost", "agent", True),
                "agent.orchestrator.share_pct": fig(
                    71.3, "71.3%", "orchestrator share of spend", "agent", True
                ),
            },
            note="totals reconcile with Analytics",
        ),
        call(
            "t2",
            "get_cache_health",
            {"hit_pct": fig(22.04, "22.0%", "Cache hit rate", "fleet", True)},
            note="cache hit = cache read / (input + cache read + cache write)",
        ),
        call("t3", "search_patterns", patterns=[detail("TKN-03"), detail("TKN-18")]),
    ]


# ---- catalog -------------------------------------------------------------------


def test_split_catalog_one_doc_per_pattern(mods):
    cat, _, _ = mods
    docs = cat.split_catalog(CATALOG)
    patterns = [d for d in docs if d[0].startswith("patterns/")]
    assert [d[0] for d in patterns] == [f"patterns/TKN-{i:02d}.md" for i in range(1, 21)]
    assert {d[0] for d in docs} - {d[0] for d in patterns} == {
        "context/applying-patterns.md",
        "context/key-cost-concepts.md",
    }
    for key, text, meta in patterns:
        pid = key.split("/")[1][:-3]
        assert text.startswith(f"### {pid} — ") and "**recommendation:**" in text
        assert "### TKN-" not in text[4:] and "## " not in text.split("\n", 1)[1]  # exactly one pattern
        assert len(meta.encode()) < 1024
        attrs = json.loads(meta)["metadataAttributes"]
        assert attrs["pattern_id"]["value"]["stringValue"] == pid
        assert attrs["doc_type"]["value"]["stringValue"] == "pattern"
        assert all(a["includeForEmbedding"] is False for a in attrs.values())
    assert cat.split_catalog(CATALOG) == docs  # deterministic


def test_parse_catalog_fields_and_normalisation(mods):
    cat, _, _ = mods
    p = cat.parse_catalog(CATALOG)
    assert len(p) == 20
    for e in p.values():
        for f in ("problem", "signal", "recommendation", "expected_effect", "quality_risk", "applies_to"):
            assert e[f], (e["pattern_id"], f)
    assert p["TKN-03"]["name"] == "Prompt-cache placement" and p["TKN-03"]["ref_no"] == 3
    assert p["TKN-05"]["pillar_key"] == "optimisation"  # "Optimisation lever"
    assert p["TKN-04"]["risk_key"] == "low_medium"  # "Low–medium"
    assert cat.threshold_figures(p["TKN-03"]) == {"50%", "3:1"}
    assert p["TKN-05"]["threshold"] == ""  # optional field
    idx = cat.index_text(p).splitlines()
    assert len(idx) == 20 and idx[2].startswith("TKN-03 · Prompt-cache placement · ")


def test_keyword_fallback(mods):
    cat, _, _ = mods
    got = cat.keyword_search(cat.load(), "cache hit rate low read write ratio", 3)
    assert {"TKN-03", "TKN-18"} <= {g["pattern_id"] for g in got}


# ---- grounding -----------------------------------------------------------------


def test_grounded_reply_renders_refs_findings_citations(mods, calls):
    _, gr, _ = mods
    out = gr.ground(
        "The orchestrator drives [[t1.agent.orchestrator.share_pct]] of spend ([[t1.agent.orchestrator.cost]]); "
        "cache hit is [[t2.hit_pct]], below the ~50% target, so prompt-cache placement applies.",
        [{"id": "TKN-03", "why": "low hit rate"}],
        calls,
        "why is cost high?",
        table=["[[t2.hit_pct]]", "t1.agent.orchestrator.share_pct"],
    )
    assert out["grounded"] and out["failure"] is None
    assert out["reply"].startswith("The orchestrator drives 71.3% of spend ($0.42)")
    # The evidence table is the answer's `table`, in its order -- not every figure the text cites.
    assert [finding["observed"] for finding in out["findings"]] == ["22.0%", "71.3%"]
    assert out["findings"][1] == {
        "signal": "orchestrator share of spend",
        "observed": "71.3%",
        "value": 71.3,
        "level": "agent",
    }
    assert out["citations"] == ["TKN-03"]
    assert out["citation_details"][0]["name"] == "Prompt-cache placement"
    assert set(out["citation_details"][0]) == {
        "pattern_id",
        "name",
        "ref_no",
        "pillar",
        "quality_risk",
        "summary",
        "expected_effect",
    }
    assert out["notes"] == [
        "totals reconcile with Analytics",
        "cache hit = cache read / (input + cache read + cache write)",
    ]


def test_unresolved_ref_fails(mods, calls):
    _, gr, _ = mods
    out = gr.ground("Cost is [[t1.agent.order.cost]].", [], calls, "")
    assert not out["grounded"] and out["failure"].startswith("unresolved_ref")
    assert out["reply"] == gr.NOTE_UNVERIFIED
    assert [f["observed"] for f in out["findings"]] == ["$0.42", "71.3%", "22.0%"]  # headline figures


# ---- ground() case tables: (text, cited pattern id or None, admin question) -------------------

FAIL_TYPED = [
    # typed figures
    ("The orchestrator drives 75% of spend.", None, ""),
    ("Total spend is $2.31 this week.", None, ""),
    ("That is 14.2× the median user.", None, ""),
    ("Read:write is 3:1 today.", None, ""),
    ("Eva ran 12 sessions.", None, ""),
    ("Combined that is [[t1.agent.orchestrator.cost]] plus $0.10.", None, ""),  # derived arithmetic
    # review regressions: fail-closed number check
    ("Total spend is [[$9.99]] this week.", None, "compare 7d"),  # malformed ref with a number inside
    ("Cost is [[t100.total.cost]].", None, "compare 7d"),  # out-of-range ref id
    ("The orchestrator drives 75 percent of spend.", None, "compare 7d"),
    ("Spend is 2.31 dollars.", None, "compare 7d"),
    ("Spend was USD 2.31.", None, "compare 7d"),
    ("Eva cost 0.42 USD.", None, "compare 7d"),
    ("That is €2.31.", None, "compare 7d"),
    ("Eva used 1,200 input tokens.", None, "compare 7d"),
    ("About 1.2 million tokens were used.", None, "compare 7d"),
    ("You spent fifty dollars.", None, "compare 7d"),
    ("Cost rose 40 percent this week.", None, "compare 7d"),
    # an admin number must match verbatim
    ("Cost rose 7%.", None, "Why did cost rise over the last 7 days?"),
    ("You spent $5.", None, "Show the top 5 users"),
    # unit words need the same unit from the admin
    ("Cost was 2000 dollars.", None, "cap 2000 tokens"),
    ("There were 5 sessions.", None, "Show the top 5 users"),
    ("It costs 50 percent more.", None, "is it 50?"),
    ("It costs 50 USD.", None, "is it 50?"),
    ("That is 50 times higher.", None, "is it 50?"),
    ("About a hundred bucks.", None, ""),
    # the admin's bare number may be echoed as a count only
    ("A cap of 2000 dollars is set on the Governance page.", None, "Change the orchestrator's cap to 2000"),
    ("A cap of 3000 tokens is set on the Governance page.", None, "Change the orchestrator's cap to 2000"),
    # teen and compound number words
    ("That is seventeen percent of spend.", None, ""),
    ("Eighteen dollars went to the orchestrator.", None, ""),
    ("That is twenty-five percent of spend.", None, ""),
    # a threshold must come from the cited, retrieved pattern:
    # 90% is TKN-01's threshold, but only TKN-03/TKN-18 were retrieved.
    ("Input is [[t2.hit_pct]], above ~90%.", None, ""),
    # the matched span is removed, not the first occurrence of the digits
    ("$3 a day, above 3:1 read:write.", "TKN-03", ""),
]

FAIL_SAVINGS = [
    # savings projections always fail
    ("Enabling caching would cut cost by ~70%.", "TKN-03", ""),
    ("This could reduce spend by $5 a week.", "TKN-03", ""),
    ("Enabling caching would cut cost by [[t2.hit_pct]].", "TKN-03", ""),  # through a reference
    # savings projections without "by"
    ("Caching could save you [[t1.agent.orchestrator.cost]].", "TKN-03", ""),
    ("You would save [[t1.agent.orchestrator.cost]] per week.", "TKN-03", ""),
    ("That is a saving of [[t1.agent.orchestrator.cost]].", "TKN-03", ""),
    ("A [[t1.agent.orchestrator.cost]] saving is available.", "TKN-03", ""),
    ("It would shave [[t1.agent.orchestrator.cost]] off the bill.", "TKN-03", ""),
    ("Spend drops to [[t1.agent.orchestrator.cost]] with caching.", "TKN-03", ""),
    # written savings amounts
    ("Cut input by half.", "TKN-03", ""),
    ("Prompt caching would save about a third of the bill.", "TKN-03", ""),
    ("Spend could fall by a third.", "TKN-03", ""),
    ("Enable caching to reduce spend by half.", "TKN-03", ""),
    ("Reduce spend by thirteen percent.", None, ""),  # teen number word after a reduce verb
    # hypothetical decreases
    ("Caching would deliver a [[t2.hit_pct]] reduction versus the previous period.", None, ""),
    ("If you route to a cheaper model, spend is lower by [[t1.agent.orchestrator.cost]].", None, ""),
    ("Spend is expected to drop by [[t1.agent.orchestrator.cost]] next week.", None, ""),
    ("Spend is lower by [[t1.agent.orchestrator.cost]] with caching.", None, ""),
    ("Your spend saves [[t1.agent.orchestrator.cost]] week over week.", None, ""),  # save-forms always fail
    # a hypothetical decrease inside a clause
    ("Total cost: lower if you cut input by half.", "TKN-03", ""),  # modal between the verb and "by"
    ("Spend fell last week; you could cut it by half.", "TKN-03", ""),  # a later verb is the hypothetical one
    ("Spend dropped; cut input by half.", "TKN-03", ""),  # imperative after a clause break
    ("Next step: reduce spend by a third.", "TKN-03", ""),
]

PASS = [
    (
        "A large, low-risk reduction on the repeated prefix [[t2.hit_pct]].",
        "TKN-03",
        "",
    ),  # no projected amount
    # admin numbers are allowed with the same number and unit
    (
        "Setting the cap to 2000 tokens is done on the Governance page.",
        None,
        "Change the orchestrator's cap to 2000 tokens",
    ),
    ("A cap of 2000 tokens is set on the Governance page.", None, "Change the cap to 2000 tokens"),
    ("A cap of 2000 tokens is set on the Governance page.", None, "Change the orchestrator's cap to 2000"),
    ("Over the 7d window the orchestrator is [[t1.agent.orchestrator.share_pct]].", None, "and over 7d?"),
    # comparisons, a cheaper-model question and pattern names are not savings claims
    (
        "Hit rate is [[t2.hit_pct]], lower than the orchestrator's share [[t1.agent.orchestrator.share_pct]].",
        None,
        "",
    ),
    (
        "Orchestrator is [[t1.agent.orchestrator.share_pct]] of spend; a cheaper model is already used by the "
        "specialists.",
        None,
        "is a cheaper model doing similar work?",
    ),
    ("Prompt-cache hit-rate and savings observability applies: hit rate is [[t2.hit_pct]].", "TKN-18", ""),
    # observed decreases are not savings
    ("Total cost shows a [[t2.hit_pct]] reduction versus the previous period.", None, ""),
    ("Spend is lower by [[t1.agent.orchestrator.cost]] than last week.", None, ""),
    ("Your spend drops by [[t1.agent.orchestrator.cost]] week over week.", None, ""),
    ("Spend dropped to [[t1.agent.orchestrator.cost]] on Tuesday.", None, ""),
    # "weight" ends in "eight", "behalf" in "half": not written amounts.
    ("You could ask for a model weight reduction.", None, ""),
    ("Caching often helps on behalf savings plans.", None, ""),
]


def _ground(gr, calls, text, cite, admin):
    return gr.ground(text, [{"id": cite}] if cite else [], calls, admin)


@pytest.mark.parametrize("text,cite,admin", FAIL_TYPED)
def test_typed_figures_fail(mods, calls, text, cite, admin):
    _, gr, _ = mods
    out = _ground(gr, calls, text, cite, admin)
    assert not out["grounded"] and out["failure"].startswith(("typed_figure", "malformed_ref")), (
        text,
        out["failure"],
    )


@pytest.mark.parametrize("text,cite,admin", FAIL_SAVINGS)
def test_savings_projections_fail(mods, calls, text, cite, admin):
    _, gr, _ = mods
    assert _ground(gr, calls, text, cite, admin)["failure"] == "savings_projection", text


@pytest.mark.parametrize("text,cite,admin", PASS)
def test_figure_free_or_referenced_text_passes(mods, calls, text, cite, admin):
    _, gr, _ = mods
    out = _ground(gr, calls, text, cite, admin)
    assert out["grounded"], (text, out["failure"])


def test_threshold_phrase_only_from_retrieved_patterns(mods, calls):
    _, gr, _ = mods
    ok = gr.ground("Hit rate is [[t2.hit_pct]], below the ~50% target.", [{"id": "TKN-03"}], calls, "")
    assert ok["grounded"]
    no_kb = [c for c in calls if c["tool"] != "search_patterns"]
    bad = gr.ground("Hit rate is [[t2.hit_pct]], below the ~50% target.", [], no_kb, "")
    assert not bad["grounded"]
    assert not gr.ground("Your cache hit is 50%.", [], calls, "")["grounded"]


def test_citation_must_be_retrieved_and_ids_never_shown(mods, calls):
    _, gr, _ = mods
    out = gr.ground(
        "See TKN-03 for the fix.", [{"id": "TKN-03"}, {"id": "TKN-02"}, {"id": "TKN-99"}], calls, ""
    )
    assert out["citations"] == ["TKN-03"]
    assert "TKN-" not in out["reply"] and "Prompt-cache placement" in out["reply"]


_SPLIT = ["[[t1.agent.orchestrator.share_pct]]", "[[t1.agent.orchestrator.cost]]", "[[t2.hit_pct]]"]


@pytest.mark.parametrize(
    "narrative,applied,tools,table,evidence,cites",
    [
        (
            "Orchestrator is [[t1.agent.orchestrator.share_pct]]; apply prompt-cache placement.",
            [{"id": "TKN-03"}],
            "all",
            _SPLIT,
            True,
            True,
        ),
        ("Orchestrator is [[t1.agent.orchestrator.share_pct]] of spend.", [], "all", _SPLIT, True, False),
        (
            "Orchestrator is [[t1.agent.orchestrator.share_pct]] of spend.",
            [],
            "all",
            [],
            False,
            False,
        ),  # text says it all
        (
            "Keep read:write above 3:1 by caching the stable prefix.",
            [{"id": "TKN-03"}],
            "kb",
            [],
            False,
            True,
        ),
        ("I can only help with tokenomics questions.", [], "none", [], False, False),
    ],
)
def test_design_block_table(mods, calls, narrative, applied, tools, table, evidence, cites):
    _, gr, _ = mods
    tc = {"all": calls, "kb": [c for c in calls if c["tool"] == "search_patterns"], "none": []}[tools]
    out = gr.ground(narrative, applied, tc, "", table)
    assert out["grounded"]
    assert bool(out["findings"]) is evidence and bool(out["citations"]) is cites


@pytest.mark.parametrize(
    "table,failure",
    [
        (["[[t1.agent.order.cost]]"], "unresolved_ref:t1.agent.order.cost"),  # no tool returned it
        (["[[t1.agent.orchestrator.cost]] [[t2.hit_pct]]"], "malformed_ref"),  # one reference per row
        (["Orchestrator cost: $0.42"], "malformed_ref"),  # typed text, not a reference
    ],
)
def test_table_rows_fail_closed_like_the_narrative(mods, calls, table, failure):
    _, gr, _ = mods
    out = gr.ground("Orchestrator is [[t1.agent.orchestrator.share_pct]].", [], calls, "", table)
    assert not out["grounded"] and out["failure"] == failure
    headline = [finding["observed"] for finding in out["findings"]]
    assert out["reply"] == gr.NOTE_UNVERIFIED and headline == ["$0.42", "71.3%", "22.0%"]


def test_table_rows_are_repaired_and_deduplicated(mods, calls):
    _, gr, _ = mods
    out = gr.ground(
        "Cache hit is [[t2.hit_pct]].",
        [],
        calls,
        "",
        [" [[t1.agent.orchestrator_share_pct]] ", "t1.agent.orchestrator.share_pct", "[[t2.hit_pct]]"],
    )
    assert out["grounded"] and [finding["observed"] for finding in out["findings"]] == ["71.3%", "22.0%"]
    assert out["notes"] == [
        "totals reconcile with Analytics",
        "cache hit = cache read / (input + cache read + cache write)",
    ]  # table-only call too


def test_no_tools_conceptual_vs_invented_number(mods, calls):
    _, gr, _ = mods
    kb_only = [c for c in calls if c["tool"] == "search_patterns"]
    ok = gr.ground("Keep read:write above 3:1 [TKN-18].", [{"id": "TKN-18"}], kb_only, "")
    assert ok["grounded"] and ok["findings"] == [] and ok["citations"] == ["TKN-18"]
    bad = gr.ground("Your cache hit is 50%.", [], [], "")
    assert bad["reply"] == gr.NOTE_NO_DATA and bad["findings"] == []


def test_validate_body_and_defaults(mods):
    _, gr, _ = mods
    assert gr.validate_body({"message": " hi "}) == {
        "message": "hi",
        "window": "7d",
        "user": "all",
        "chat_id": None,
    }
    for bad in (
        {},
        {"message": ""},
        {"message": "x" * 2001},
        {"message": "hi", "window": "90d"},
        {"message": "hi", "chat_id": "../x"},
        [],
        {"message": 5},
    ):
        with pytest.raises(ValueError):
            gr.validate_body(bad)
    cid = gr.new_id()
    assert gr.validate_body({"message": "q", "chat_id": cid})["chat_id"] == cid


def test_history_for_agent_only_completed_alternating(mods):
    _, gr, _ = mods
    msgs = [
        {"role": "user", "text": "q1"},
        {"role": "assistant", "text": "a1", "status": "complete"},
        {"role": "user", "text": "q2"},
        {"role": "assistant", "text": "", "status": "failed"},
        {"role": "user", "text": "q3"},
        {"role": "assistant", "text": "a3", "status": "complete"},
        {"role": "user", "text": "q4"},
        {"role": "assistant", "text": "", "status": "pending"},
    ]
    assert gr.history_for_agent(msgs) == [
        {"role": "user", "text": "q1"},
        {"role": "assistant", "text": "a1"},
        {"role": "user", "text": "q3"},
        {"role": "assistant", "text": "a3"},
    ]
    many = []
    for i in range(30):
        many += [
            {"role": "user", "text": f"q{i}"},
            {"role": "assistant", "text": f"a{i}", "status": "complete"},
        ]
    history = gr.history_for_agent(many)  # every completed pair: the agent's sliding window trims it
    assert len(history) == 60
    assert history[0] == {"role": "user", "text": "q0"} and history[-1] == {
        "role": "assistant",
        "text": "a29",
    }


# ---- KB retrieval ----------------------------------------------------------------


def _stub_kb(kb, monkeypatch, responses):
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.stub import Stubber

    # Stubbed calls never reach AWS, so an unsigned client needs no credentials.
    client = boto3.client(
        "bedrock-agent-runtime", region_name="us-west-2", config=Config(signature_version=UNSIGNED)
    )
    st = Stubber(client)
    for r in responses:
        if isinstance(r, str):
            st.add_client_error("retrieve", service_error_code=r)
        else:
            st.add_response("retrieve", r)
    st.activate()
    monkeypatch.setattr(kb, "_client", lambda: client)
    monkeypatch.setattr(kb.config, "ADVISOR_KB_ID", "KB12345678")
    monkeypatch.setattr(kb.time, "sleep", lambda s: None)
    return st


def _res(pid, score):
    return {"content": {"text": "x"}, "score": score, "metadata": {"pattern_id": pid, "doc_type": "pattern"}}


def test_search_patterns_kb_cutoff_dedupe_and_fields(mods, monkeypatch):
    _, _, kb = mods
    st = _stub_kb(
        kb,
        monkeypatch,
        [
            {
                "retrievalResults": [
                    _res("TKN-03", 0.81),
                    _res("TKN-03", 0.80),
                    _res("TKN-18", 0.7),
                    _res("TKN-02", 0.05),
                ]
            }
        ],
    )
    out = kb.search_patterns("cache hit rate low", 3)
    assert out["source"] == "kb" and [p["pattern_id"] for p in out["patterns"]] == ["TKN-03", "TKN-18"]
    p = out["patterns"][0]
    assert p["name"] == "Prompt-cache placement" and p["quality_risk"] == "low" and p["score"] == 0.81
    st.assert_no_pending_responses()


def test_search_patterns_retry_then_fallback(mods, monkeypatch):
    _, _, kb = mods
    _stub_kb(kb, monkeypatch, ["ThrottlingException", "ThrottlingException", "ThrottlingException"])
    out = kb.search_patterns("model routing cheaper model spend", 3)
    assert (
        out["source"] == "fallback"
        and out["available"]
        and "TKN-02" in [p["pattern_id"] for p in out["patterns"]]
    )


def test_retrieve_retries_only_within_the_tool_time_limit(mods, monkeypatch):
    _, _, kb = mods
    st = _stub_kb(kb, monkeypatch, ["ThrottlingException"] * 3)
    now = iter([0.0, 0.1, 0.4, 0.8])  # three quick throttles: retried twice, then raised
    monkeypatch.setattr(kb.time, "monotonic", lambda: next(now))
    with pytest.raises(kb.ClientError):
        kb._retrieve("cache hit rate", 3)
    st.assert_no_pending_responses()
    st = _stub_kb(kb, monkeypatch, ["ThrottlingException"] * 2)
    now = iter([0.0, 1.4])  # a slow first failure: a full retry would pass 7.5 s
    monkeypatch.setattr(kb.time, "monotonic", lambda: next(now))
    with pytest.raises(kb.ClientError):
        kb._retrieve("cache hit rate", 3)
    with pytest.raises(AssertionError):
        st.assert_no_pending_responses()  # the second response was never requested


def test_search_patterns_validation_and_empty(mods):
    _, _, kb = mods
    with pytest.raises(ValueError):
        kb.search_patterns("x")
    out = kb.search_patterns("zzzz qqqq")
    assert out["available"] is False and out["patterns"] == []


# ---- data tools ------------------------------------------------------------------


def test_data_tools_allow_list_and_safe_errors(mods, monkeypatch):
    te = importlib.import_module("advisor.data_tools")
    assert te.dispatch("delete_everything", {}) == {"error": "unknown tool"}
    assert te.dispatch("get_outliers", "x") == {"error": "args must be an object"}
    broken = te.svc.TurnContext(lambda: (_ for _ in ()).throw(RuntimeError("secret ddb arn")))
    out = te.dispatch("get_outliers", {"window": "7d"}, broken)
    assert out == {"error": "tool failed; try again or ask a different question"}
    idx = te.dispatch("get_catalog_index", {})
    assert len(idx["index"].splitlines()) == 20
    out = te.dispatch("search_patterns", {"query": "cache hit rate", "k": 2, "extra": "ignored"})
    assert out["tool"] == "search_patterns" and len(out["patterns"]) <= 2


# ---- review regressions: fail-closed number check ----------------------------------


def test_allowed_non_figure_digits(mods, calls):
    _, gr, _ = mods
    calls = calls + [call("t4", "get_cache_health", {}, None)]
    calls[-1]["result"]["labels"] = {"first_drop_day": "2026-10-10", "models": ["us.amazon.nova-lite-v1:0"]}
    text = (
        "1. The orchestrator runs on Sonnet 4.5 and is [[t1.agent.orchestrator.share_pct]] of spend.\n"
        "2. Cache hit first dropped on 2026-10-10 (window 7d)."
    )
    out = gr.ground(text, [], calls, "")
    assert out["grounded"], out["failure"]


def test_notes_only_for_referenced_calls_and_empty_on_failure(mods, calls):
    _, gr, _ = mods
    ok = gr.ground("Orchestrator is [[t1.agent.orchestrator.share_pct]].", [], calls, "")
    assert ok["notes"] == ["totals reconcile with Analytics"]
    bad = gr.ground("Orchestrator is 70%.", [], calls, "")
    assert bad["notes"] == []


def test_search_patterns_sends_doc_type_filter(mods, monkeypatch):
    _, _, kb = mods
    st = _stub_kb(kb, monkeypatch, [])
    st.add_response(
        "retrieve",
        {"retrievalResults": [_res("TKN-03", 0.9)]},
        expected_params={
            "knowledgeBaseId": "KB12345678",
            "retrievalQuery": {"text": "Cost signal: cache hit rate low"},
            "retrievalConfiguration": {
                "vectorSearchConfiguration": {
                    "numberOfResults": 5,
                    "filter": {"equals": {"key": "doc_type", "value": "pattern"}},
                }
            },
        },
    )
    assert kb.search_patterns("cache hit rate low")["patterns"][0]["pattern_id"] == "TKN-03"
    st.assert_no_pending_responses()


def test_tool_text_digits_and_window_phrases_are_not_figures(mods, calls):
    _, gr, _ = mods
    calls = [dict(c) for c in calls]
    calls[0] = {
        **calls[0],
        "result": {**calls[0]["result"], "note": "user outlier: daily spend > 10× peer median"},
    }
    calls[0]["result"]["figures"] = {
        **calls[0]["result"]["figures"],
        "agent.order.cost_per_1k": {
            "value": 0.0012,
            "display": "$0.0012",
            "signal": "order cost per 1k tokens",
            "level": "agent",
        },
    }
    out = gr.ground(
        "Over the last 7 days order costs [[t1.agent.order.cost_per_1k]] per 1k tokens; "
        "no user is above 10× the peer median.",
        [],
        calls,
        "",
    )
    assert out["grounded"], out["failure"]


def test_markdown_bold_is_stripped(mods, calls):
    _, gr, _ = mods
    out = gr.ground("**Orchestrator** is [[t1.agent.orchestrator.share_pct]].", [], calls, "")
    assert out["reply"] == "Orchestrator is 71.3%."


@pytest.mark.parametrize(
    "q,title",
    [
        (
            "Which agent (orchestrator / knowledge / order) drives the most cost, and why?",
            "Agent orchestrator knowledge order",
        ),
        ("How healthy is our prompt-cache — hit rate and read:write ratio?", "Healthy prompt-cache hit rate"),
        ("Is any single user spending far more than everyone else right now?", "Single user spending"),
        ("??", "New chat"),
    ],
)
def test_short_title_heuristic(mods, q, title):
    _, gr, _ = mods
    assert gr.short_title(q) == title


def test_clean_title_from_agent(mods):
    _, gr, _ = mods
    assert gr.clean_title("Agent cost breakdown") == "Agent cost breakdown"
    assert gr.clean_title("Top 5 users by $ spend!!") == "Top users by spend"
    assert gr.clean_title("one two three four five six seven") == "One two three four five"
    assert gr.clean_title("<script>alert(1)</script>") == "Script alert script"
    assert gr.clean_title("") == ""


def test_text_labels_are_citable_but_not_evidence(mods, calls):
    _, gr, _ = mods
    calls = calls + [
        {
            "ref": "t4",
            "tool": "get_cache_health",
            "result": {
                "figures": {},
                "labels": {"first_drop_day": "2026-10-09", "days": ["2026-10-09"]},
                "data_level": "fleet",
            },
        }
    ]
    out = gr.ground(
        "Cache hit rate first dropped on [[t4.first_drop_day]] and is [[t2.hit_pct]] now.",
        [],
        calls,
        "",
        table=["[[t4.first_drop_day]]", "[[t2.hit_pct]]"],
    )
    assert out["grounded"] and out["reply"] == "Cache hit rate first dropped on 2026-10-09 and is 22.0% now."
    assert [f["observed"] for f in out["findings"]] == ["22.0%"]


@pytest.mark.parametrize("d", ["October 11", "Oct 11th", "11 October", "October 11, 2026"])
def test_written_dates_are_not_figures(mods, calls, d):
    _, gr, _ = mods
    assert gr.ground(f"The spike was on {d}, at [[t1.agent.orchestrator.cost]].", [], calls, "")["grounded"]
    assert not gr.ground(f"The spike was on {d}, costing $4.", [], calls, "")["grounded"]


@pytest.mark.parametrize(
    "text,ok",
    [
        ("Read:write is [[t2.hit_pct]], short of the healthy 3:1 target.", True),
        ("No user is above ~10× the peer median.", True),
        ("The orchestrator's per-1k-token cost is [[t1.agent.orchestrator.cost]].", True),
        ("Turns used 5k-10k tokens each.", False),
        ("Cache hit is 50%, the healthy level.", False),
    ],
)
def test_review_round_phrasings(mods, calls, text, ok):
    _, gr, _ = mods
    calls = [dict(c) for c in calls]
    calls[0] = {
        **calls[0],
        "result": {**calls[0]["result"], "note": "user outlier: daily spend > 10× peer median"},
    }
    calls[0]["result"]["figures"] = {
        **calls[0]["result"]["figures"],
        "agent.order.cost_per_1k": {
            "value": 0.0012,
            "display": "$0.0012",
            "signal": "order cost per 1k tokens",
            "level": "agent",
        },
    }
    assert gr.ground(text, [{"id": "TKN-03"}], calls, "")["grounded"] is ok


def test_ref_key_spelling_repair_is_unique_only(mods, calls):
    _, gr, _ = mods
    out = gr.ground("Orchestrator share is [[t1.agent.orchestrator_share_pct]].", [], calls, "")
    assert out["grounded"] and out["reply"] == "Orchestrator share is 71.3%."
    assert not gr.ground("Cost is [[t1.agent.order.cost]].", [], calls, "")["grounded"]


def test_trailing_comma_and_em_dash_are_not_part_of_a_number(mods, calls):
    _, gr, _ = mods
    assert gr._NUM_UNIT_WORD.findall("from turn 1, tokens grew") == []
    assert gr.ground("It spiked at turn [[t1.agent.orchestrator_share_pct]]—the 2—then", [], calls, "turn 2")[
        "grounded"
    ]


def test_unit_repeated_after_a_reference_is_collapsed(mods, calls):
    _, gr, _ = mods
    c = call("t9", "get_cost_summary", {"sessions": fig(65, "65 sessions", "Sessions", "fleet", True)})
    out = gr.ground("Across [[t9.sessions]] sessions.", [], [c], "")
    assert out["grounded"] and out["reply"] == "Across 65 sessions."


# ---- signals: patterns attached by the data tools ---------------------------------------------


def test_data_tools_attach_catalog_details_to_signals(mods, monkeypatch):
    te = importlib.import_module("advisor.data_tools")
    fake = {
        "tool": "get_cache_health",
        "available": True,
        "figures": {},
        "signals": [
            {
                "signal": "input dominates the token mix",
                "level": "fleet",
                "patterns": ["TKN-01", "TKN-99"],
                "figures": [],
            }
        ],
    }
    monkeypatch.setitem(te.svc.TOOLS, "get_cache_health", lambda ctx, **kw: fake)
    out = te.dispatch("get_cache_health", {"window": "7d"}, te.svc.TurnContext(lambda: None))
    pats = out["signals"][0]["patterns"]
    assert [p["pattern_id"] for p in pats] == ["TKN-01"]  # unknown ids are dropped
    assert pats[0]["name"] == "Context management" and pats[0]["recommendation"] and pats[0]["pillar"]


def test_signal_patterns_are_citable_without_search(mods, calls):
    cat, gr, _ = mods
    p = cat.load()["TKN-17"]
    det = {
        **{
            k: p[k]
            for k in (
                "pattern_id",
                "name",
                "ref_no",
                "summary",
                "expected_effect",
                "recommendation",
                "threshold",
            )
        },
        "pillar": p["pillar_key"],
        "quality_risk": p["risk_key"],
    }
    data = [dict(c) for c in calls if c["tool"] != "search_patterns"]
    data[0] = {
        **data[0],
        "result": {
            **data[0]["result"],
            "signals": [
                {
                    "signal": "the orchestrator agent carries most of the spend",
                    "level": "agent",
                    "patterns": [det],
                    "figures": ["agent.orchestrator.share_pct"],
                }
            ],
        },
    }
    out = gr.ground(
        "Orchestrator is [[t1.agent.orchestrator.share_pct]] of spend; see Cost attribution and "
        "high-usage identification.",
        [{"id": "TKN-17", "why": "concentration"}, {"id": "TKN-02"}],
        data,
        "",
    )
    assert out["grounded"] and out["citations"] == ["TKN-17"]  # TKN-02 was neither signalled nor retrieved
    assert out["citation_details"][0]["name"] == "Cost attribution and high-usage identification"


def test_cited_signal_figure_cites_its_first_pattern_once(mods, calls):
    cat, gr, _ = mods

    def det(pid):
        p = cat.load()[pid]
        return {
            **{
                k: p[k]
                for k in (
                    "pattern_id",
                    "name",
                    "ref_no",
                    "summary",
                    "expected_effect",
                    "recommendation",
                    "threshold",
                )
            },
            "pillar": p["pillar_key"],
            "quality_risk": p["risk_key"],
        }

    data = [dict(c) for c in calls if c["tool"] != "search_patterns"]
    data[0] = {
        **data[0],
        "result": {
            **data[0]["result"],
            "signals": [
                {
                    "signal": "total cost rose sharply",
                    "level": "fleet",
                    "patterns": [det("TKN-19"), det("TKN-17")],
                    "figures": ["agent.orchestrator.cost"],
                }
            ],
        },
    }
    out = gr.ground("Orchestrator cost is [[t1.agent.orchestrator.cost]].", [], data, "")
    assert out["grounded"] and out["citations"] == ["TKN-19"]
    assert out["citation_details"][0]["name"] == "Real-time anomaly and runaway-loop detection"
    # already cited one of the signal's patterns -> nothing added
    out = gr.ground("Orchestrator cost is [[t1.agent.orchestrator.cost]].", [{"id": "TKN-17"}], data, "")
    assert out["citations"] == ["TKN-17"]
    # figure not cited -> no citation from the signal
    assert gr.ground("Share is [[t1.agent.orchestrator.share_pct]].", [], data, "")["citations"] == []


@pytest.mark.parametrize(
    "query,text",
    [
        ("$4,000", "Spend is $4,000 per month."),
        ("40%", "Your hit rate is 40%."),
    ],
)
def test_search_query_cannot_ground_a_number(mods, calls, query, text):
    _, gr, kb = mods
    assert "query" not in kb.search_patterns(f"cache {query}")["labels"]
    calls = calls[:2] + [{**calls[2], "result": {**calls[2]["result"], "labels": {"query": query}}}]
    assert not gr.ground(text, [], calls, "")["grounded"]  # typed
    out = gr.ground("Spend is [[t3.query]].", [], calls, "")  # cited
    assert not out["grounded"] and out["failure"] == "unresolved_ref:t3.query"


def test_markdown_bold_list_marker_is_not_a_typed_figure(mods, calls):
    _, gr, _ = mods
    out = gr.ground("**1. Fix the cache** first: hit rate is [[t2.hit_pct]].", [], calls, "")
    assert out["grounded"], out["failure"]
    assert not gr.ground("**Spend is 40%** higher.", [], calls, "")["grounded"]  # bold never hides a figure
