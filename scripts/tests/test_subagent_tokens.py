from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "subagent_tokens", ROOT / "artifacts/subagents/tokens_and_reports/analyze.py"
)
assert SPEC is not None and SPEC.loader is not None
tokens = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokens)

from extract_codex_rounds import extract_codex_session
from sanitize_round_trace import StableIdSanitizer, sanitize_row

CODEX_IDS = {
    name: f"{index}0000000-0000-4000-8000-00000000000{index}"
    for index, name in enumerate(("root", "child", "second", "middle", "unknown"), start=1)
}


def session_id(provider, sid):
    return f"{provider}:{CODEX_IDS.get(sid, sid) if provider == 'codex' else sid}"


def model_output(timestamp="2026-01-01T00:00:13Z", **extra):
    return {"event_type": "text", "timestamp": timestamp, **extra}


def row(provider="claude", sid="root", index=1, *, parent=None, i=1000, p=800, a=200, o=100, **extra):
    origin = f"{provider}_{'subagent' if parent else 'top_level'}_{'layout' if provider == 'claude' else 'source'}"
    return {
        "identity_scope_id": "10000000-0000-4000-8000-000000000001",
        "provider": provider, "session_id": session_id(provider, sid), "session_origin": origin,
        "parent_session_id": session_id(provider, parent) if parent else None,
        "round_index": index, "round_id": "duplicate-is-not-a-key",
        "model": "claude-sonnet" if provider == "claude" else "gpt-5.5",
        "input_tokens_total": i, "prefix_tokens": p, "newly_append_tokens": a,
        "output_tokens": o, "reasoning_output_tokens": 90, **extra,
    }


def receipt(provider, call="return", children=("child",), **extra):
    return {"tool_index": 1, "tool_name": "Agent" if provider == "claude" else "wait",
            "tool_call_id": call, "report_source": tokens.SOURCES[provider],
            "report_session_ids": [session_id(provider, sid) for sid in children] if provider == "codex" else None,
            "emitted_at": "2026-01-01T00:00:01Z",
            **extra}


def result(call="return", **extra):
    return {"event_type": "tool_result", "tool_call_id": call, "timestamp": "2026-01-01T00:00:12Z", **extra}


def family(provider="claude", **current):
    # Consumption follows emission by two rounds; k-1, not the emitting round, supplies the baseline.
    return [
        row(provider, index=1, i=200, p=100, a=100, o=5, tools=[receipt(provider)],
            timing_events=[model_output("2026-01-01T00:00:01Z", event_type="tool_call", tool_call_id="return")]),
        row(provider, "child", parent="root", i=4000, p=3000, a=1000, o=20),
        row(provider, index=2, timing_events=[model_output("2026-01-01T00:00:10Z")]),
        row(provider, index=3, i=1400, p=1000, a=400, timing_events=[result(), model_output()], **current),
    ]


@pytest.fixture
def collect(tmp_path):
    def build(rows):
        trace = tmp_path / "trace.jsonl"
        db = tmp_path / "trace.duckdb"
        trace.write_text("".join(json.dumps(r) + "\n" for r in rows))
        tokens.trace_db.materialize(trace, db)
        con = tokens.trace_db.connect(db)
        try:
            return tokens.collect(con)
        finally:
            con.close()
    return build


def test_both_providers_consume_later_and_do_not_count_reasoning_twice(collect):
    stats = collect(family("claude") + family("codex"))
    for provider in ("claude", "codex"):
        report = stats["reports"][provider]["main"]
        assert report["delta"]["n"] == report["residual"]["n"] == 1
        assert report["delta"]["avg"] == 400
        assert report["residual"]["avg"] == 300
        usage = stats["usage"][provider]["totals"]
        assert usage["input_tokens_total"]["roles"]["family"]["total"] == 6600
        assert usage["output_tokens"]["roles"]["family"]["total"] == 225
        assert usage["input_tokens_total"]["descendant_majority_fraction"] == 1
    page = tokens.render_md(stats)
    assert tokens.DELTA_LABEL in page and tokens.RESIDUAL_LABEL in page
    assert "input changes, not exact report-token counts" in page
    assert "Possible replies (Agent/Task)" in page and "Completed replies recorded by wait" in page
    assert "round receiving a reply with the preceding round in the same session" in page
    assert "The receiving session may be a main or a subagent" in page
    assert "not equivalent report counts" in page
    assert "Missing evidence does not prove no replies" in page
    assert "| Families where subagents use more tokens than the main | 1 (100.0%) | 1 (100.0%) |" in page
    assert tokens.render_headline(stats) == [
        {"label": f"{provider} median {metric} tokens per complete family", "value": value}
        for provider in ("Claude", "Codex") for metric, value in (("input", "6,600"), ("output", "225"))
    ]
    assert "claude:root" not in page and "return" not in json.dumps(tokens.render_headline(stats))
    assert {entry["label"].split()[0] for entry in tokens.render_headline(stats)} == {"Claude", "Codex"}
    assert page.count("| Provider | Avg | P25 | P50 | P90 | P99 |") == 6
    assert page.count("| Provider | Sessions | Avg | P25 | P50 | P90 | P99 |") == 4
    assert page.count("| Provider | Round pairs | Avg | P25 | P50 | P90 | P99 |") == 4
    assert "| Claude | 1 | 400 | 400 | 400 | 400 | 400 |" in page
    assert "<br>" not in page and "; mean" not in page


def test_display_keeps_provider_columns_and_missing_statistics(collect):
    page = tokens.render_md(collect(family("claude")))
    assert "| Measure | Claude | Codex |" in page
    assert "| Whole families — total tokens | 6,600 | N/A |" in page
    assert "| Captured evidence | Evidence available | No report evidence |" in page
    assert "| Codex | 0 | N/A | N/A | N/A | N/A | N/A |" in page
    assert "| Families with complete usage | 1 | 0 |" in page
    assert "<br>" not in page and "; mean" not in page


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("p,a", [(1000, 400), (1100, 300), (200, 1200)])
def test_delta_ignores_cache_split(provider, p, a):
    previous = row(provider)
    current = row(provider, index=2, i=1400, p=p, a=a)
    assert tokens.transition_values(previous, current) == (400, 300, None)


@pytest.mark.parametrize("provider,model,expected", [
    ("claude", "claude-sonnet", (50, -50, None)),
    ("codex", "gpt-5.5", (50, -50, None)),
    ("codex", "gpt-5.4", (50, None, "residual_model_policy")),
    ("codex", "gpt-5.5-mini", (50, None, "residual_model_policy")),
])
def test_signed_residual_and_exact_policy(provider, model, expected):
    assert tokens.transition_values(
        row(provider, model=model), row(provider, index=2, i=1050, p=1000, a=50, model=model)
    ) == expected


def test_batch_and_multiple_results_count_one_transition(collect):
    rows = family("codex")
    rows[0]["tools"] = [receipt("codex", children=("child", "second")), receipt("codex", "return2", ("second",))]
    rows[-1]["timing_events"] = [result(), result(), result("return2"), result("unrelated"), model_output()]
    rows.append(row("codex", "second", parent="root", i=20, p=10, a=10))
    report = collect(rows)["reports"]["codex"]["main"]
    assert report["coverage"]["mapped_entries"] == 2
    assert report["coverage"]["consuming_transitions"] == report["delta"]["n"] == 1
    assert report["delta"]["avg"] == 400


@pytest.mark.parametrize("failure", [
    "no_consuming_round", "ambiguous_call_id", "ambiguous_result_consumer",
    "nonconsecutive_or_ambiguous_rounds", "not_later_round", "known_error_returns",
])
def test_receipt_exclusions_are_visible(collect, failure):
    rows = family()
    if failure == "no_consuming_round":
        rows[-1]["timing_events"] = [model_output()]
    elif failure == "ambiguous_call_id":
        rows[2]["tools"] = [{"tool_call_id": "return", "tool_name": "other"}]
    elif failure == "ambiguous_result_consumer":
        rows[2]["timing_events"] = [result(timestamp="2026-01-01T00:00:09Z"), model_output("2026-01-01T00:00:10Z")]
    elif failure == "nonconsecutive_or_ambiguous_rounds":
        rows.append(row(index=2))
    elif failure == "not_later_round":
        rows[-1]["timing_events"] = [model_output()]
        rows[0]["timing_events"] = [result(), model_output()]
    else:
        rows[0]["tools"][0]["is_error"] = True
    report = collect(rows)["reports"]["claude"]["main"]
    assert report["coverage"][failure] == 1
    assert report["delta"]["n"] == 0 and report["delta"]["avg"] is None


def test_intermediate_recipient_not_relabelled_as_main(collect):
    rows = family("codex")
    rows = [dict(r, session_id=session_id("codex", "middle"), session_origin="codex_subagent_source",
                 parent_session_id=session_id("codex", "root"))
            if r["session_id"] == session_id("codex", "root")
            else dict(r, parent_session_id=session_id("codex", "middle")) for r in rows]
    rows.append(row("codex"))
    reports = collect(rows)["reports"]["codex"]
    assert reports["main"]["delta"]["n"] == 0
    assert reports["intermediate"]["delta"]["avg"] == 400


def test_complete_family_filters_independent_metrics_without_fanout(collect):
    rows = family()
    rows[0]["tools"] *= 3
    rows.append(row("claude", "child", index=1, parent="root", i=None, p=None, a=None, o=30))
    stats = collect(rows)
    usage = stats["usage"]["claude"]
    assert usage["rounds"] == 5
    assert usage["totals"]["input_tokens_total"]["excluded_families"] == 1
    assert usage["totals"]["input_tokens_total"]["roles"]["family"]["total"] is None
    assert usage["totals"]["output_tokens"]["roles"]["family"]["total"] == 255
    assert usage["totals"]["output_tokens"]["excluded_families"] == 0


def test_first_missing_stays_missing_peak_available_and_zero_not_empty(collect):
    stats = collect([
        row(index=2, i=0, p=0, a=0, o=0), row(index=1, i=None, p=None, a=None, o=0),
        row(index=None, i=500, p=500, a=0, o=0),
        row("claude", "child", parent="root", i=0, p=0, a=0, o=0),
        row("codex", "unknown", session_origin="unknown", i=99999),
    ])
    context = stats["usage"]["claude"]["context"]
    assert context["main"]["first"]["n"] == 0
    assert context["main"]["peak"]["avg"] == 500
    assert context["main"]["valid_rounds"] == 2
    assert context["main"]["incomplete_sessions"] == 1
    assert context["descendant"]["first"]["avg"] == 0
    output = stats["usage"]["claude"]["totals"]["output_tokens"]
    assert output["roles"]["family"]["total"] == 0
    assert output["descendant_majority_fraction"] is None
    assert stats["usage"]["codex"]["totals"]["output_tokens"]["roles"]["family"]["total"] is None
    assert "No report evidence" in tokens.render_md(stats)


@pytest.mark.parametrize("changes,reason", [
    ({"model": ""}, "model_missing_or_changed"),
    ({"model": "different"}, "model_missing_or_changed"),
    ({"prefix_tokens": 999}, "invalid_input_usage"),
    ({"input_tokens_total": None}, "invalid_input_usage"),
    ({"newly_append_tokens": -1}, "invalid_input_usage"),
])
def test_invalid_usage_or_model_has_no_delta(changes, reason):
    current = row(index=2, i=1400, p=1000, a=400)
    current.update(changes)
    assert tokens.transition_values(row(), current) == (None, None, reason)


@pytest.mark.parametrize("output", [None, -1, 0])
def test_previous_output_required_only_for_residual(output):
    assert tokens.transition_values(row(o=output), row(index=2, i=1400, p=1000, a=400)) == (
        400, None, "residual_missing_or_nonpositive_output"
    )


def test_negative_delta_and_zero_input_residual_guard():
    assert tokens.transition_values(row(), row(index=2, i=950, p=900, a=50)) == (-50, -150, None)
    assert tokens.transition_values(row(), row(index=2, i=0, p=0, a=0)) == (
        -1000, None, "residual_nonpositive_input"
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("event_type", ["reasoning", "text", "tool_call"])
def test_report_cannot_feed_model_output_already_started(collect, provider, event_type):
    rows = family(provider)
    # List position is not chronology, and a later text event is not the first output.
    rows[-1]["timing_events"] = [result(), model_output(), model_output(
        "2026-01-01T00:00:11Z", event_type=event_type,
    )]
    report = collect(rows)["reports"][provider]["main"]
    assert report["coverage"]["evidence_entries"] == 1
    assert report["coverage"]["report_after_model_output"] == 1
    assert report["coverage"]["mapped_entries"] == 0
    assert report["delta"]["n"] == report["residual"]["n"] == 0


@pytest.mark.parametrize("case", [
    "emission", "arrival", "no_output", "missing_output_time", "partly_timed_output",
    "unparseable_output",
])
def test_unavailable_timeline_keeps_receipt_but_no_metric(collect, case):
    rows = family()
    if case == "emission":
        rows[0]["tools"][0].pop("emitted_at")
    elif case == "arrival":
        rows[-1]["timing_events"][0].pop("timestamp")
    elif case == "no_output":
        rows[-1]["timing_events"] = [result(), {"event_type": "usage_report", "timestamp": "2026-01-01T00:00:20Z"}]
    elif case == "partly_timed_output":
        rows[-1]["timing_events"].append({"event_type": "reasoning"})
    else:
        timestamp = {"missing_output_time": None, "unparseable_output": "not-a-time"}[case]
        rows[-1]["timing_events"][1]["timestamp"] = timestamp
    stats = collect(rows)
    report = stats["reports"]["claude"]["main"]
    assert report["coverage"]["evidence_entries"] == 1
    assert report["coverage"]["unavailable_consumption_timing"] == 1
    assert report["delta"]["avg"] is report["residual"]["avg"] is None
    assert "| Missing timing |" in tokens.render_md(stats)


@pytest.mark.parametrize("field", ["emission", "arrival", "output"])
def test_nonfinite_database_timestamps_are_unavailable(tmp_path, field):
    trace, db = tmp_path / "trace.jsonl", tmp_path / "trace.duckdb"
    trace.write_text("".join(json.dumps(r) + "\n" for r in family()))
    tokens.trace_db.materialize(trace, db)
    with tokens.trace_db.connect(db, read_only=False) as con:
        # Set typed DB values: JSON inference can coerce non-ISO strings before analysis sees them.
        if field == "emission":
            con.execute("UPDATE tool_calls SET emitted_at = 'infinity'::TIMESTAMP")
        else:
            event_type = "tool_result" if field == "arrival" else "text"
            con.execute("UPDATE timing_events SET timestamp = 'infinity'::TIMESTAMP WHERE round_pk = 4 AND event_type = ?", [event_type])
        report = tokens.collect(con)["reports"]["claude"]["main"]
    assert report["coverage"]["unavailable_consumption_timing"] == 1
    assert report["delta"]["n"] == report["residual"]["n"] == 0


@pytest.mark.parametrize("case", ["arrival_before_emission", "output_before_emission", "conflicting_arrivals"])
def test_impossible_timeline_has_explicit_coverage(collect, case):
    rows = family()
    if case == "arrival_before_emission":
        rows[-1]["timing_events"][0]["timestamp"] = "2026-01-01T00:00:00Z"
    elif case == "output_before_emission":
        rows[-1]["timing_events"][1]["timestamp"] = "2026-01-01T00:00:00Z"
    else:
        rows[-1]["timing_events"].append(result(timestamp="2026-01-01T00:00:11Z"))
    report = collect(rows)["reports"]["claude"]["main"]
    assert report["coverage"]["evidence_entries"] == 1
    assert report["coverage"]["impossible_consumption_timing"] == 1
    assert report["delta"]["n"] == report["residual"]["n"] == 0


def test_arrival_equal_to_first_model_output_is_eligible(collect):
    rows = family()
    rows[-1]["timing_events"] = [model_output("2026-01-01T00:00:12Z"), result()]
    report = collect(rows)["reports"]["claude"]["main"]
    assert report["delta"]["avg"] == 400 and report["residual"]["avg"] == 300


@pytest.mark.parametrize("provider,metadata", [
    ("codex", {"report_session_ids": ["codex:not-a-uuid"]}),
    ("codex", {"report_session_ids": [session_id("codex", "child")] * 2}),
    ("codex", {"report_session_ids": [session_id("codex", "root")]}),
    ("codex", {"report_session_ids": ["claude:child"]}),
    ("codex", {"report_session_ids": []}),
    ("codex", {"report_session_ids": None}),
    ("codex", {"report_source": None}),
    ("codex", {"report_source": "unsupported"}),
    ("codex", {"report_source": "claude_agent_task_result", "report_session_ids": None}),
    ("claude", {"report_session_ids": ["claude:child"]}),
])
def test_direct_materialization_excludes_invalid_annotations_not_entire_analysis(collect, provider, metadata):
    rows = family(provider)
    rows[0]["tools"][0].update(metadata)
    other = "claude" if provider == "codex" else "codex"
    stats = collect(rows + family(other))
    report = stats["reports"][provider]["main"]
    assert report["coverage"]["annotated_entries"] == 1
    assert report["coverage"]["invalid_report_metadata"] == 1
    assert report["coverage"]["evidence_entries"] == 0
    assert report["delta"]["n"] == report["residual"]["n"] == 0
    assert stats["reports"][other]["main"]["delta"]["avg"] == 400
    assert "Invalid report metadata (excluded)" in tokens.render_md(stats)


@pytest.mark.parametrize("late", [False, True])
def test_raw_codex_consumption_order_through_sanitizer_and_database(tmp_path, collect, late):
    # Synthetic source-derived wait records, not real captures or report-token ground truth.
    def native(top, second, **payload):
        return {"type": top, "timestamp": f"2026-01-01T00:00:{second:02d}Z", "payload": payload}

    def usage(second, sequence, i, cached, o):
        return native("event_msg", second, type="token_count", info={
            "last_token_usage": {"input_tokens": i, "cached_input_tokens": cached, "output_tokens": o},
            "total_token_usage": {"total_tokens": sequence},
        })

    parent, child = CODEX_IDS["root"], CODEX_IDS["child"]
    arrival = native("response_item", 12, type="function_call_output", call_id="call_wait", output=json.dumps({
        "status": {child: {"completed": "PRIVATE_REPORT_SENTINEL"}}, "timed_out": False,
    }))
    output = native("response_item", 11 if late else 13, type="message", role="assistant", content=[])
    parent_records = [
        native("session_meta", 0, id=parent, source="cli"),
        native("turn_context", 0, turn_id="turn-parent", model="gpt-5.5"),
        native("response_item", 1, type="function_call", name="wait", call_id="call_wait", arguments="{}"),
        usage(10, 1, 1000, 800, 100),
        *([output, arrival] if late else [arrival, output]),
        usage(20, 2, 1400, 1000, 50),
        # A later round consumes the report only when the boundary router carries its result there.
        native("response_item", 21, type="message", role="assistant", content=[]),
        usage(30, 3, 1600, 1400, 25),
    ]
    child_records = [
        native("session_meta", 2, id=child, source={"subagent": {"thread_spawn": {"parent_thread_id": parent}}}),
        native("turn_context", 2, turn_id="ffffffff-ffff-4fff-8fff-ffffffffffff", model="gpt-5.5"),
        native("response_item", 3, type="message", role="assistant", content=[]),
        usage(9, 1, 200, 100, 20),
    ]
    normalized = []
    for name, records in (("parent", parent_records), ("child", child_records)):
        path = tmp_path / f"{name}.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        normalized.extend(extract_codex_session(path))
    consumer_index = 2 if late else 1
    events = normalized[consumer_index]["timing_events"]
    assert [event["event_type"] for event in events[:2]] == ["tool_result", "text"]
    if late:
        assert [event["event_type"] for event in normalized[1]["timing_events"]] == ["text", "usage_report"]
    assert normalized[0]["tools"][0]["report_source"] == "codex_wait_completed"
    assert "PRIVATE_REPORT_SENTINEL" not in json.dumps(normalized)
    ids = StableIdSanitizer("gate2")
    safe = [sanitize_row(r, ids) for r in normalized]
    assert safe[0]["tools"][0]["report_session_ids"] == [safe[-1]["session_id"]]
    assert safe[-1]["parent_session_id"] == safe[0]["session_id"]
    report = collect(safe)["reports"]["codex"]["main"]
    assert report["coverage"]["evidence_entries"] == 1
    assert report["coverage"]["consuming_transitions"] == 1
    assert report["delta"]["avg"] == (200 if late else 400)
    assert report["residual"]["avg"] == (150 if late else 300)
