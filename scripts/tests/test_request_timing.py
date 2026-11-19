from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'artifacts/utils'))
import request_timing as rt


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def con():
    con = duckdb.connect(':memory:')
    con.execute('CREATE TABLE rounds(round_pk BIGINT, provider VARCHAR, session_id VARCHAR, turn_id VARCHAR, session_kind VARCHAR, first_input_event_type VARCHAR)')
    con.execute('CREATE TABLE timing_events(round_pk BIGINT, event_index BIGINT, event_type VARCHAR, timestamp TIMESTAMP, turn_id VARCHAR, tool_call_id VARCHAR)')
    con.execute('CREATE TABLE tool_calls(round_pk BIGINT, tool_index BIGINT, tool_name VARCHAR, emitted_at TIMESTAMP, result_at TIMESTAMP, tool_internal_latency_ms BIGINT, tool_wall_latency_ms BIGINT)')
    con.execute('CREATE TABLE sessions(provider VARCHAR, session_id VARCHAR, session_origin VARCHAR, root_status VARCHAR, resolved_root_session_id VARCHAR)')
    yield con
    con.close()


def stamp(seconds):
    return None if seconds is None else datetime(2026, 1, 1) + timedelta(seconds=seconds)


def row(con, rpk, *, sid='child', turn=None, kind='worker', events=(), tools=()):
    con.execute('INSERT INTO rounds (round_pk,provider,session_id,turn_id,session_kind,first_input_event_type) VALUES (?, ?, ?, ?, ?, ?)', [rpk, 'codex', sid, turn, kind, 'user_message'])
    for index, event in enumerate(events):
        event_type, seconds, *identity = event
        native = identity[0] if identity else None
        call = identity[1] if len(identity) > 1 else None
        con.execute('INSERT INTO timing_events VALUES (?, ?, ?, ?, ?, ?)', [rpk, index, event_type, stamp(seconds), native, call])
    for index, tool in enumerate(tools):
        name, start, end, internal, wall = tool
        con.execute('INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?, ?, ?)', [rpk, index, name, stamp(start), stamp(end), internal, wall])


def test_nine_day_tool_work_is_not_divided_by_short_response(con):
    row(con, 1, turn='a', events=[('request_start', 0, 'a'), ('user_message', 0), ('tool_call', 90), ('request_end', 90, 'a')],
        tools=[('AskUserQuestion', 90, 777690, None, 777600000)])
    measured = rt.collect_request_timing(con)[0]
    assert measured['e2e'] == 90
    assert measured['tool'] == 777600
    assert measured['interaction'] == 777600
    assert measured['outstanding'] == 0
    overall = module('timing_overall_boundary', 'artifacts/session/session_timing_distribution/analyze.py')
    assert overall.shares([measured], 'request') == {'outstanding': 0}


def test_legacy_next_prompt_and_eof_never_complete_requests(con):
    row(con, 1, events=[('user_message', 0), ('tool_call', 90)], tools=[('AskUserQuestion', 90, 777690, None, 777600000)])
    row(con, 2, events=[('user_message', 777690), ('text', 777700)])
    requests = rt.collect_requests(con)
    assert len(requests) == 2
    assert [request.status for request in requests] == ['legacy', 'legacy']
    assert all(request.elapsed_seconds is None for request in requests)


def test_native_identity_survives_overlapping_turns_and_marker_order(con):
    row(con, 1, turn='a', events=[('request_start', 0, 'a'), ('text', 1)])
    row(con, 2, turn='b', events=[('request_end', 10, 'a'), ('request_end', 12, 'b'), ('request_start', 2, 'b'), ('text', 8)])
    requests = {request.turn_id: request for request in rt.collect_requests(con)}
    assert requests['a'].elapsed_seconds == 10
    assert requests['b'].elapsed_seconds == 10
    assert requests['a'].round_pks == [1]
    assert requests['b'].round_pks == [2]


def test_aborted_open_and_missing_start_are_not_wall_samples(con):
    row(con, 1, turn='aborted', events=[('request_start', 0, 'aborted'), ('request_aborted', 3, 'aborted')])
    row(con, 2, turn='open', events=[('request_start', 4, 'open'), ('text', 5)])
    row(con, 3, turn='orphan', events=[('request_end', 8, 'orphan')])
    requests = rt.collect_requests(con)
    assert {request.status for request in requests} == {'aborted', 'open', 'missing_start'}
    assert all(request.elapsed_seconds is None for request in requests)


def test_subsecond_positive_internal_zero_endpoint_is_unfit_for_wall(con):
    row(con, 1, turn='a', events=[('request_start', 0, 'a'), ('text', 1), ('request_end', 2, 'a')],
        tools=[('exec_command', 1, 1, 300, 0)])
    measured = rt.collect_request_timing(con)[0]
    assert measured['tool'] == .3
    assert measured['compressed_timestamps']
    assert measured['e2e'] is None
    assert measured['outstanding'] is None


def test_overlap_keeps_canonical_work_and_same_pooled_main_child_definition(con):
    for rpk, sid, kind in [(1, 'main', 'main'), (2, 'child', 'worker')]:
        row(con, rpk, sid=sid, kind=kind, events=[('user_message', 0), ('text', 10)],
            tools=[('exec_command', 0, 10, 10000, 10000), ('exec_command', 0, 10, 10000, 10000)])
    con.execute("INSERT INTO sessions VALUES ('codex','main','codex_top_level_source','resolved_explicit','main'), ('codex','child','codex_subagent_source','resolved_explicit','main')")
    overall = module('timing_overall_consistency', 'artifacts/session/session_timing_distribution/analyze.py')
    child = module('timing_child_consistency', 'artifacts/subagents/timing/analyze.py')
    sessions = rt.collect_session_timing(con)
    assert sessions['codex', 'child']['tool'] == 20
    assert sessions['codex', 'child']['outstanding'] == 10
    data = child.collect(con)['codex']
    assert data['pooled_share'] == 200
    assert data['groups']['matched mains']['pooled_share'] == 200
    assert overall.shares(list(sessions.values()), 'session')['tool'] == 2
    assert overall.shares(list(sessions.values()), 'session')['outstanding'] == 1


def test_missing_and_anonymous_native_tool_observations_do_not_become_zero(con):
    row(con, 1, events=[('user_message', 0), ('web_search_observation', 1), ('text', 10)],
        tools=[('web_search', None, 1, None, None)])
    summary = rt.collect_session_timing(con)['codex', 'child']
    assert summary['tool'] == 0
    assert summary['missing_tool_timing'] == 2
    assert summary['unmeasured_tool_observations'] == 1
    assert summary['outstanding'] is None


def test_native_start_metadata_does_not_erase_input_gap(con):
    row(con, 1, turn='a', events=[('user_message', 0), ('text', 10), ('request_end', 10, 'a')])
    row(con, 2, turn='b', events=[('request_start', 100, 'b'), ('user_message', 100), ('text', 110)])
    assert rt.collect_session_timing(con)['codex', 'child']['human'] == 90


def test_duplicate_start_after_abort_cannot_resurrect_native_request(con):
    row(con, 1, turn='a', events=[('request_start', 0, 'a'), ('request_aborted', 5, 'a'),
                                 ('request_start', 0, 'a'), ('request_end', 6, 'a')])
    request = rt.collect_requests(con)[0]
    assert request.status == 'invalid'  # conflicting completion cannot turn an abort into success
    assert request.elapsed_seconds is None


def test_native_identity_groups_legacy_rows_without_completion_markers(con):
    row(con, 1, turn='a', events=[('user_message', 0), ('text', 1)])
    row(con, 2, turn='a', events=[('user_message', 2), ('text', 3)])
    row(con, 3, turn='b', events=[('text', 4)])
    requests = rt.collect_requests(con)
    assert [request.round_pks for request in requests] == [[1, 2], [3]]
    assert all(request.elapsed_seconds is None for request in requests)


def test_marker_identity_can_own_row_when_round_turn_id_is_missing(con):
    row(con, 1, events=[('request_start', 0, 'a'), ('text', 1), ('request_end', 2, 'a')])
    request, = rt.collect_requests(con)
    assert request.round_pks == [1]
    assert request.elapsed_seconds == 2


def test_request_start_marker_does_not_erase_input_gap():
    from timing import human_waits_from_event_pairs
    waits, count, _ = human_waits_from_event_pairs([
        ("text", stamp(10)), ("request_start", stamp(100)), ("user_message", stamp(100))], None)
    assert waits == [90]
    assert count == 1


def test_distribution_and_ratio_populations_are_explicit(con):
    row(con, 1, sid='complete', turn='a', events=[('request_start', 0, 'a'), ('user_message', 0), ('text', 10), ('request_end', 10, 'a')],
        tools=[('exec_command', 1, 3, 2000, 2000)])
    row(con, 2, sid='compressed', turn='b', events=[('request_start', 0, 'b'), ('user_message', 0), ('text', 100), ('request_end', 100, 'b')],
        tools=[('exec_command', 1, 1, 300, 0)])
    row(con, 3, sid='incomplete', turn='c', events=[('request_start', 0, 'c'), ('user_message', 0), ('text', 20), ('request_end', 20, 'c')],
        tools=[('web_search', None, 5, None, None)])
    overall = module('timing_sample_populations', 'artifacts/session/session_timing_distribution/analyze.py')
    units = overall.collect(con)
    scope = units['merged']
    assert overall.stats(overall.series(scope['session'], 'session', 'total'))['n'] == 3
    assert overall.stats(overall.series(scope['session'], 'session', 'tool'))['n'] == 3
    assert len(overall.ratio_population(scope['session'], 'session', 'tool')) == 1
    assert overall.shares(scope['session'], 'session')['tool'] == .2
    assert overall.stats(overall.series(scope['request'], 'request', 'total'))['n'] == 2
    assert overall.stats(overall.series(scope['request'], 'request', 'tool'))['n'] == 3
    rendered = overall.render_md(units)
    tool_row = next(line for line in rendered.splitlines() if line.startswith('| Attributed tool work |'))
    assert tool_row.startswith('| Attributed tool work | 3 |')
    assert '20.0% (N=1)' in tool_row
    assert '| Observed elapsed span | 3 |' in rendered
    assert 'not necessarily the ratio population' in rendered
    assert '\\textbf{N}' in overall.render_tex(units)


def test_unowned_rows_tools_tokens_and_abort_markers_stay_visible(con):
    con.execute('ALTER TABLE rounds ADD COLUMN output_tokens BIGINT DEFAULT 0')
    row(con, 1, sid='owned', events=[('user_message', 0), ('text', 1)])
    row(con, 2, sid='orphan', events=[('text', 2), ('request_aborted', 3)],
        tools=[('exec_command', 1, 3, 2000, 2000)])
    con.execute('UPDATE rounds SET output_tokens=13 WHERE round_pk=2')
    coverage = rt.request_ownership_coverage(con)
    record = coverage['all']
    assert record['owned_rounds'] == 1
    assert record['unowned_rounds'] == 1
    assert record['unowned_output_tokens'] == 13
    assert record['unowned_tool_calls'] == 1
    assert record['unowned_tool_seconds'] == 2
    assert record['unowned_lifecycle']['request_aborted'] == 1
    assert 'Unowned start / end / abort markers' in rt.render_ownership_coverage(coverage)
    assert 'excluded from request statistics' in rt.render_request_audit(con)


def test_count_and_cost_sessions_preserve_provider_identity(con, monkeypatch):
    row(con, 1, sid='same', events=[('user_message', 0), ('text', 1)])
    row(con, 2, sid='same', events=[('user_message', 2), ('text', 3)])
    con.execute("UPDATE rounds SET provider='claude' WHERE round_pk=2")
    counts = module('count_provider_collision', 'artifacts/session/session_internal_counts/analyze.py')
    measured = counts.collect(con)
    assert measured['merged']['requests'] == [1, 1]
    assert measured['claude']['requests'] == [1]
    assert measured['codex']['requests'] == [1]
    for name, kind, default in [('model', 'VARCHAR', "'example'"), ('prefix_tokens', 'BIGINT', '0'),
                                ('newly_append_tokens', 'BIGINT', '0'), ('output_tokens', 'BIGINT', '0')]:
        con.execute(f'ALTER TABLE rounds ADD COLUMN {name} {kind} DEFAULT {default}')
    con.execute('UPDATE rounds SET output_tokens=7 WHERE round_pk=1')
    con.execute('UPDATE rounds SET output_tokens=13 WHERE round_pk=2')
    costs = module('cost_provider_collision', 'artifacts/session/session_cost_distribution/analyze.py')
    monkeypatch.setattr(costs.pricing, 'price_for', lambda *_: object())
    monkeypatch.setattr(costs.pricing, 'round_cost', lambda *_, **__: {'cachedCost': 0, 'inputCost': 0, 'outputCost': 1})
    units, meta = costs.collect(con)
    assert [unit['out_t'] for unit in units['merged']['session']] == [7, 13]
    assert [unit['out_t'] for unit in units['claude']['session']] == [13]
    assert [unit['out_t'] for unit in units['codex']['session']] == [7]
    assert meta['ownership_coverage']['all']['unowned_rounds'] == 0


def test_session_kind_uses_shared_reconciled_evidence(con):
    row(con, 1, sid='mixed', turn='a', kind='unknown', events=[('user_message', 0), ('text', 1)])
    row(con, 2, sid='mixed', turn='a', kind='worker', events=[('text', 2)])
    row(con, 3, sid='conflict', turn='b', kind='worker', events=[('user_message', 3), ('text', 4)])
    row(con, 4, sid='conflict', turn='b', kind='guardian', events=[('text', 5)])
    sessions = rt.collect_session_timing(con)
    requests = {request.session_id: request for request in rt.collect_requests(con)}
    assert sessions['codex', 'mixed']['session_kind'] == 'worker'
    assert requests['mixed'].session_kind == 'worker'
    assert sessions['codex', 'conflict']['session_kind'] == 'unknown'
    assert requests['conflict'].session_kind == 'unknown'
