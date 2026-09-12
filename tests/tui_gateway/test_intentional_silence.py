"""Presentation silence must not erase provider history or failure evidence."""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

from gateway.response_filters import LIVE_GATEWAY_SILENT_MARKERS, is_intentional_silence_response
from hermes_state import SessionDB
from tui_gateway import server


class InlineThread:
    def __init__(self, target=None, args=(), **kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)
    def join(self):
        pass


def run_turn(monkeypatch, tmp_path, chunks, *, result_extra=None, interim=None,
             warning=None, crash=None, receipt_crash=False, persist=True, stamp_error=False):
    db = SessionDB(tmp_path / 'state.db')
    key = 'silence-test'
    db.create_session(key, source='desktop', model='test')
    # An identical older row must never be relabeled by a later turn.
    db.append_message(key, 'assistant', 'NO_REPLY')
    before = db.get_messages_as_conversation(key)
    stamp_attempts = []
    if stamp_error:
        def fail_stamp(*args, **kwargs):
            stamp_attempts.append(kwargs.get('display_kind'))
            raise RuntimeError('database unavailable')
        monkeypatch.setattr(db, 'set_latest_matching_message_display_kind', fail_stamp)
    events, snapshots, rendered, spoken, raw_consumers = [], [], [], queue.Queue(), []
    session = dict(session_key=key, history=before, history_lock=threading.Lock(),
                   history_version=0, running=True, cols=80)
    agent = SimpleNamespace(session_id=key, _session_db=db)
    session['agent'] = agent
    marker = ''.join(chunks)

    def converse(prompt, **kwargs):
        messages = list(before) + [{'role': 'user', 'content': prompt}]
        db.append_message(key, 'user', prompt)
        if interim is not None:
            kwargs['stream_callback'](interim)
            agent.interim_assistant_callback(interim, already_streamed=True)
            messages.append({'role': 'assistant', 'content': interim})
            db.append_message(key, 'assistant', interim)
            server._emit('tool.complete', 'sid', {'tool_name': 'read_file', 'result': 'NO_REPLY'})
        for chunk in chunks:
            kwargs['stream_callback'](chunk)
            snapshots.append(server._inflight_snapshot(session))
        if crash:
            raise RuntimeError(crash)
        messages.append({'role': 'assistant', 'content': marker})
        if persist:
            db.append_message(key, 'assistant', marker)
        return dict(final_response=marker, messages=messages, **(result_extra or {}))
    agent.run_conversation = converse
    def prepare(sid, sess, st, text, images):
        st.history = list(sess['history'])
        st.history_version = sess['history_version']
        st.tts_queue = spoken
        return text, text, 80, SimpleNamespace(feed=lambda delta: rendered.append(delta) or delta)
    monkeypatch.setattr(server.threading, 'Thread', InlineThread)
    monkeypatch.setattr(server, '_admit_prompt_turn', lambda *a: ([], agent))
    monkeypatch.setattr(server, '_prepare_turn_input', prepare)
    monkeypatch.setattr(server, '_record_turn_marker', lambda *a: '')
    monkeypatch.setattr(server, '_retire_turn_marker', lambda *a: None)
    monkeypatch.setattr(server, '_start_usage_ticker', lambda *a: (threading.Event(), SimpleNamespace(join=lambda: None)))
    monkeypatch.setattr(server, '_get_usage', lambda *a: {'output': 7})
    monkeypatch.setattr(server, '_load_interim_assistant_messages', lambda: True)
    monkeypatch.setattr(server, 'make_stream_renderer', lambda *a: SimpleNamespace(feed=lambda d: rendered.append(d) or d))
    monkeypatch.setattr(server, '_sync_session_key_after_compress', lambda *a, **k: None)
    monkeypatch.setattr(server, '_emit_settled_session_info', lambda *a: None)
    monkeypatch.setattr(server, '_run_post_turn_followups', lambda *a: None)
    monkeypatch.setattr(server, '_goal_followup_after_turn', lambda *a: raw_consumers.append(a[-1]))
    monkeypatch.setattr(server, '_after_complete_turn', lambda *a: raw_consumers.append(a[-1]))
    monkeypatch.setattr(server, '_finish_turn', lambda *a: None)
    monkeypatch.setattr(server, '_emit', lambda event, sid, payload=None: events.append((event, payload)))
    if warning:
        monkeypatch.setattr(server, '_commit_turn_history', lambda *a: warning)
    def receipt(payload):
        if receipt_crash:
            raise RuntimeError('receipt failed')
    server._start_inflight_turn(session, 'NO_REPLY')
    server._run_prompt_submit('rid', 'sid', session, 'NO_REPLY', terminal_callback=receipt)
    return SimpleNamespace(db=db, session=session, events=events, snapshots=snapshots,
                           rendered=rendered, spoken=list(spoken.queue), raw=raw_consumers,
                           marker=marker, key=key, stamp_attempts=stamp_attempts)


MARKERS = sorted(LIVE_GATEWAY_SILENT_MARKERS) + [' \n*no_reply*.\n', ' “NO   REPLY!” ', '.silent.', ' [silent] ']
SPLITS = [(m, i) for m in MARKERS for i in range(len(m) + 1)]


@pytest.mark.parametrize('marker,split', SPLITS)
def test_success_holds_every_split_and_reopens_with_only_terminal_row_hidden(monkeypatch, tmp_path, marker, split):
    assert is_intentional_silence_response(marker)
    run = run_turn(monkeypatch, tmp_path, [marker[:split], marker[split:]])
    completes = [p for e, p in run.events if e == 'message.complete']
    assert completes == [{'text': '', 'status': 'complete', 'silent': True, 'usage': {'output': 7}}]
    assert not [p for e, p in run.events if e == 'message.delta']
    assert not run.rendered and not run.spoken
    assert all(not s or not s.get('assistant') for s in run.snapshots)
    assert run.raw == [marker, marker]
    assert run.session['running'] is False
    rows = run.db.get_messages_as_conversation(run.key)
    # The existing conversation adapter trims text; the underlying provider row
    # and in-memory conversation must still have the original exact content.
    assert [r['content'] for r in run.db.get_messages(run.key)] == ['NO_REPLY', 'NO_REPLY', marker]
    assert run.session['history'][-1]['content'] == marker
    assert [r.get('display_kind') for r in rows] == [None, None, 'intentional_silence']
    assert [m['text'] for m in server._history_to_messages(rows)] == ['NO_REPLY', 'NO_REPLY']
    run.db.close()
    with SessionDB(tmp_path / 'state.db') as reopened:
        assert [m['text'] for m in server._history_to_messages(reopened.get_messages_as_conversation(run.key))] == ['NO_REPLY', 'NO_REPLY']


@pytest.mark.parametrize('case', [
    'prose', 'malformed', 'blank', 'divergence', 'interim', 'marker_interim',
    'failed', 'error', 'partial', 'interrupted', 'incomplete', 'billing_block',
    'warning', 'warnings', 'history_warning', 'crash', 'receipt_crash', 'not_persisted',
])
def test_ordinary_content_failures_and_boundaries_are_not_silenced(monkeypatch, tmp_path, case):
    extra = {}
    kwargs = {}
    text = {'prose': 'The marker NO_REPLY is literal.', 'malformed': '[SILENT',
            'blank': '', 'divergence': 'NO_REPLY is a token.', 'marker_interim': 'Useful result'}.get(case, 'NO_REPLY')
    if case in ('failed', 'error', 'partial', 'interrupted', 'incomplete', 'billing_block', 'warning', 'warnings'):
        extra[case] = 'real problem' if case in ('error', 'warning', 'warnings') else True
    if case == 'history_warning': kwargs['warning'] = 'History changed — not saved'
    if case == 'crash': kwargs['crash'] = 'provider crashed'
    if case == 'receipt_crash': kwargs['receipt_crash'] = True
    if case == 'not_persisted': kwargs['persist'] = False
    if case in ('interim', 'marker_interim'): kwargs['interim'] = 'Real progress' if case == 'interim' else 'NO_REPLY'
    run = run_turn(monkeypatch, tmp_path, list(text), result_extra=extra, **kwargs)
    completes = [p for e, p in run.events if e == 'message.complete']
    assert completes and run.session['running'] is False
    rows = run.db.get_messages_as_conversation(run.key)
    if case == 'interim':
        assert completes[-1]['silent'] is True
        assert ''.join(run.rendered) == 'Real progress'
        assert ''.join(run.spoken) == 'Real progress'
        assert [p['text'] for e, p in run.events if e == 'message.interim'] == ['Real progress']
        assert 'Real progress' in [m['text'] for m in server._history_to_messages(rows)]
    else:
        assert not completes[-1].get('silent')
        assert not any(r.get('display_kind') == 'intentional_silence' for r in rows)
        visible = ''.join(p['text'] for e, p in run.events if e == 'message.delta')
        assert visible == ('NO_REPLY' if case == 'marker_interim' else '') + text
    if case in ('crash', 'receipt_crash', 'not_persisted', 'failed', 'error'):
        assert completes[-1]['status'] == 'error'
        assert server._inflight_snapshot(run.session)['status'] == 'error'
    if case in ('warning', 'warnings', 'history_warning'):
        assert completes[-1]['warning']
    assert rows[0].get('display_kind') is None
    run.db.close()


def test_prefix_budget_fails_open_without_losing_text(monkeypatch, tmp_path):
    text = ' ' * 65 + 'NO_REPLY'
    run = run_turn(monkeypatch, tmp_path, list(text))
    completed = [p for e, p in run.events if e == 'message.complete'][-1]
    assert not completed.get('silent')
    assert ''.join(p['text'] for e, p in run.events if e == 'message.delta') == text
    assert not any(r.get('display_kind') == 'intentional_silence'
                   for r in run.db.get_messages(run.key))
    run.db.close()


def test_stamp_and_rollback_failures_still_deliver_terminal_error(monkeypatch, tmp_path):
    run = run_turn(monkeypatch, tmp_path, ['NO_', 'REPLY'], stamp_error=True)
    assert run.stamp_attempts[:2] == ['intentional_silence', 'message']
    payload = [p for e, p in run.events if e == 'message.complete'][-1]
    assert payload['status'] == 'error'
    assert not payload.get('silent')
    assert 'database unavailable' in payload.get('error', '')
    assert 'rollback also failed' in payload.get('error', '')
    assert ''.join(p['text'] for e, p in run.events if e == 'message.delta') == 'NO_REPLY'
    assert server._inflight_snapshot(run.session)['status'] == 'error'
    assert not any(row.get('display_kind') == 'intentional_silence'
                   for row in run.db.get_messages(run.key))
    run.db.close()


def test_silence_presentation_metadata_does_not_hide_user_messages():
    history = [
        {'role': 'user', 'content': 'NO_REPLY', 'display_kind': 'intentional_silence'},
        {'role': 'assistant', 'content': 'NO_REPLY'},
        {'role': 'assistant', 'content': 'NO_REPLY', 'display_kind': 'intentional_silence'},
    ]
    assert [m['text'] for m in server._history_to_messages(history)] == ['NO_REPLY', 'NO_REPLY']
