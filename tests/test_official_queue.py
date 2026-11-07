"""友谊等车 v2：真实队列逻辑、模拟时钟/API、存档和消息边界。"""
import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import unquote
from unittest.mock import AsyncMock

import pytest
import official_queue as queue

SCOPE = ('official-a', 'app-a', 'group-openid')


@pytest.fixture
def setup_queue(tmp_path):
    clock = [0.0]
    send = AsyncMock(return_value={'id': 'response'})
    profile = AsyncMock(side_effect=AssertionError('面板不再查询胜率'))
    bind = AsyncMock(return_value={'home_team': 'a队', 'query': '云玩家'})
    service = queue.WaitingQueues(tmp_path / 'queues.json', send, profile, bind, clock=lambda: clock[0])
    counter = [0]
    async def act(action='等车', raw='', uid='u1', scope=SCOPE, msg=None):
        counter[0] += 1
        await service.execute(scope, action, raw, uid, f'群名片{uid}', msg or str(counter[0]))
    return service, clock, send, act


def texts(send, needle=''):
    return [unquote(c.args[1]) for c in send.await_args_list if needle in unquote(c.args[1])]


def test_shared_keyboard_has_three_buttons_per_row_and_no_car_required_for_own_actions(setup_queue):
    _, _, send, act = setup_queue
    asyncio.run(act())
    rows = send.await_args.args[2]['content']['rows']
    assert [len(r['buttons']) for r in rows] == [3, 3, 1]
    buttons = [b for r in rows for b in r['buttons']]
    assert [b['render_data']['label'] for b in buttons] == ['进队等车', '跑路了再见', '发送群号', '再开一车', '解散队伍', '等车帮助', '呼叫全体']
    assert buttons[1]['action']['data'] == '/离队'
    assert buttons[2]['action']['data'] == '/发送群号 ' and buttons[2]['action']['enter'] is False
    assert buttons[4]['action']['data'] == '/解散'
    assert buttons[5]['action']['data'] == '/等车帮助'
    assert buttons[6]['action']['data'] == '/呼叫全体' and buttons[6]['action']['enter'] is True
    assert all(b['action']['type'] == 2 and b['action']['permission']['type'] == 2 for b in buttons)


def test_all_cars_in_one_compact_panel_members_on_one_logical_line_without_stats(setup_queue):
    service, _, send, act = setup_queue
    async def scenario():
        await act()
        await act('我也要等车', uid='u2')
        await act('再开一车', uid='u3')
        text = texts(send)[-1]
        assert '**1号车 · 2人**' in text and '**2号车 · 1人**' in text
        assert '##' not in text and '胜率' not in text
        lines = text.splitlines()
        assert any(line.count('![头像 #32px #32px]') == 2 for line in lines)
        assert all('广播间隔：4分钟' in line for line in lines if '自动清理：' in line)
        assert 'https://q.qlogo.cn/qqapp/app-a/u1/100' in text
        assert 'qqbot-at-user' not in text
        assert 'text="@群名片u1 " show="群名片u1"' in text
        service.profile.assert_not_awaited()
    asyncio.run(scenario())


def test_partial_car_lives_ten_minutes_and_broadcasts_every_four_without_join_reset(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        clock[0] = 120
        await act('我也要等车', uid='u2')
        car = service.room(SCOPE)['cars'][0]
        assert car['deadline'] == 600 and not car['ready']
        before = send.await_count
        clock[0] = 239
        await service.tick()
        assert send.await_count == before
        clock[0] = 240
        await service.tick()
        assert send.await_count == before + 1
        assert '4:00' in texts(send)[-1]
        clock[0] = 480
        await service.tick()
        assert send.await_count == before + 2
        clock[0] = 600
        await service.tick()
        assert not service.room(SCOPE)['cars']
        assert all(queue.mention(uid) in texts(send)[-1] for uid in ['u1', 'u2'])
        assert '已自动解散' in texts(send)[-1]
    asyncio.run(scenario())


def test_first_three_reset_once_and_have_dedicated_notice_even_after_leave_and_rejoin(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        clock[0] = 100
        await act('我也要等车', uid='u2')
        clock[0] = 200
        await act('我也要等车', uid='u3')
        car = service.room(SCOPE)['cars'][0]
        assert car['ready'] and car['deadline'] == 800
        ready = texts(send, '快去接友谊')
        assert len(ready) == 1 and all(queue.mention(u) in ready[0] for u in ['u1', 'u2', 'u3'])
        clock[0] = 250
        await act('我也要等车', uid='u4')
        assert car['deadline'] == 800
        clock[0] = 300
        await act('跑路了再见', uid='u3')
        await act('跑路了再见', uid='u4')
        assert len(car['members']) == 2 and car['ready'] and car['deadline'] == 800
        clock[0] = 500
        await act('我也要等车', uid='u5')
        await act('我也要等车', uid='u5')
        assert car['deadline'] == 800 and len(texts(send, '快去接友谊')) == 1
        assert car['members']['u1']['joined_at'] == 0
        clock[0] = 800
        await service.tick()
        assert not service.room(SCOPE)['cars']
    asyncio.run(scenario())


def test_join_duplicate_events_do_not_notify_twice_and_leave_notifies_remaining_only(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        clock[0] = 100
        await act('我也要等车', uid='u2', msg='join2')
        joined = texts(send, '加入了1号车')
        assert len(joined) == 1 and queue.mention('u1') in joined[0] and queue.mention('u2') not in joined[0]
        before = send.await_count
        await act('我也要等车', uid='u2', msg='join2')
        assert send.await_count == before
        await act('我也要等车', uid='u2')
        assert len(texts(send, '加入了1号车')) == 1
        clock[0] = 200
        await act('跑路了再见', uid='u2')
        left = texts(send, '离开了1号车')[-1]
        assert queue.mention('u1') in left and queue.mention('u2') not in left
        assert service.room(SCOPE)['cars'][0]['deadline'] == 600
        assert 'qqbot-cmd-input' not in left and '群名片u1' not in left
        assert left.count(queue.mention('u1')) == 1
    asyncio.run(scenario())


def test_single_membership_and_global_join_selects_available_car(setup_queue):
    service, _, send, act = setup_queue
    async def scenario():
        await act()
        await act('再开一车')
        first, second = service.room(SCOPE)['cars']
        assert not second['members']
        await act('我也要等车', second['id'])
        assert '另一队' in texts(send)[-1] and list(first['members']) == ['u1']
        for uid in ['u2', 'u3']:
            await act('我也要等车', uid=uid)
        await act('我也要等车', uid='u4')
        assert list(second['members']) == ['u4']
        await act('跑路了再见', uid='u4')
        assert service.room(SCOPE)['cars'] == [first]
    asyncio.run(scenario())


def test_shared_refresh_and_independent_cleanup_for_many_cars(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        clock[0] = 100
        await act('再开一车', uid='u2')
        room = service.room(SCOPE)
        assert room['next_refresh'] == 240
        assert [c['deadline'] for c in room['cars']] == [600, 700]
        before = send.await_count
        clock[0] = 240
        await service.tick()
        assert send.await_count == before + 1
        assert '1号车' in texts(send)[-1] and '2号车' in texts(send)[-1]
        clock[0] = 600
        await service.tick()
        assert [c['number'] for c in room['cars']] == [2]
        assert room['cars'][0]['deadline'] == 700
        clock[0] = 700
        await service.tick()
        assert not room['cars']
    asyncio.run(scenario())


def test_dissolve_requires_membership_and_notifies_every_member(setup_queue):
    service, _, send, act = setup_queue
    async def scenario():
        await act()
        await act('我也要等车', uid='u2')
        car = service.room(SCOPE)['cars'][0]
        await act('解散队伍', car['id'], uid='outsider')
        assert service.room(SCOPE)['cars'] and '只有本车成员' in texts(send)[-1]
        await act('解散队伍', uid='u2')
        assert not service.room(SCOPE)['cars']
        notice = texts(send, '号车已解散')[-1]
        assert all(queue.mention(u) in notice for u in ['u1', 'u2'])
    asyncio.run(scenario())


def test_send_group_number_is_a_separate_plain_digit_message_and_mentions_others_only(setup_queue):
    service, _, send, act = setup_queue
    async def scenario():
        await act()
        for u in ['u2', 'u3']:
            await act('我也要等车', uid=u)
        await act('人齐了发车', '123456789')
        notice = texts(send, '发车啦')[-1]
        assert '123456789' not in notice
        number_calls = [c for c in send.await_args_list if c.kwargs.get('plain')]
        assert len(number_calls) == 1 and number_calls[0].args[1] == '123456789'
        assert number_calls[0].args[2] is None
        assert queue.mention('u1') not in notice
        assert queue.mention('u2') in notice and queue.mention('u3') in notice
        assert not service.room(SCOPE)['cars']
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['too_few', 'outsider', 'missing_group', 'bad_group'])
def test_send_group_guards_never_remove_car(setup_queue, case):
    service, _, send, act = setup_queue
    async def scenario():
        await act()
        if case != 'too_few':
            for u in ['u2', 'u3']:
                await act('我也要等车', uid=u)
        car = service.room(SCOPE)['cars'][0]
        raw = car['id'] if case == 'missing_group' else car['id'] + (' xyz' if case == 'bad_group' else ' 123456789')
        await act('人齐了发车', raw, uid='outsider' if case == 'outsider' else 'u1')
        assert service.room(SCOPE)['cars']
    asyncio.run(scenario())


def test_daily_numbers_reset_at_beijing_midnight_but_tokens_and_old_cars_remain_unique(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        clock[0] = datetime(2026, 11, 5, 15, 59, 30, tzinfo=timezone.utc).timestamp()
        await act()
        old = service.room(SCOPE)['cars'][0]
        clock[0] += 31
        await act('再开一车', uid='u2')
        new = service.room(SCOPE)['cars'][1]
        assert new['number'] == old['number'] == 1 and new['id'] != old['id']
        assert '11-05·1号车' in texts(send)[-1]
        await act('我也要等车', '1', uid='u3')
        assert '车次号有重复' in texts(send)[-1]
        await act('我也要等车', old['id'], uid='u3')
        assert 'u3' in old['members'] and 'u3' not in new['members']
        restored = queue.WaitingQueues(service.path, send, service.profile, service.bind, clock=lambda: clock[0])
        assert restored.room(SCOPE)['next_number'] == 2
    asyncio.run(scenario())


def test_old_car_token_never_joins_new_car_with_recycled_display_number(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act(msg='original')
        old = service.room(SCOPE)['cars'][0]['id']
        await act('再开一车', msg='original')
        assert len(service.room(SCOPE)['cars']) == 1
        clock[0] = 600
        await service.tick()
        await act()
        await act('我也要等车', old, uid='u2')
        assert '已经结束' in texts(send)[-1]
        assert list(service.room(SCOPE)['cars'][0]['members']) == ['u1']
    asyncio.run(scenario())


def test_restart_preserves_first_ready_flag_deadline_and_shared_broadcast(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        await act('我也要等车', uid='u2')
        clock[0] = 100
        await act('我也要等车', uid='u3')
        await act('跑路了再见', uid='u3')
        restored = queue.WaitingQueues(service.path, send, service.profile, service.bind, clock=lambda: clock[0])
        await restored.execute(SCOPE, '我也要等车', '', 'u4', '萌新', 'rejoin')
        assert restored.room(SCOPE)['cars'][0]['deadline'] == 700
        assert restored.room(SCOPE)['cars'][0]['ready']
        assert len(texts(send, '快去接友谊')) == 1
        assert restored.room(SCOPE)['next_refresh'] == 240
    asyncio.run(scenario())


def test_v1_migration_preserves_members_tokens_pending_notices_and_never_revives_expired_car(tmp_path):
    path = tmp_path / 'v1.json'
    state = {'version': 1, 'rooms': {queue.WaitingQueues.key(SCOPE): dict(scope=list(SCOPE), next_number=2,
             bindings={}, seen=[], outbox=[dict(id='notice', text='旧通知', users=['u1'], retry_at=900)],
             cars=[dict(id='C1_OLD', number=1, ready=True, deadline=300, next_refresh=180,
                        members={'u1': dict(name='云玩家', joined_at=0)})])}}
    path.write_text(json.dumps(state))
    service = queue.WaitingQueues(path, AsyncMock(return_value={'id': 'sent'}), None, None, clock=lambda: 400)
    assert service.state['version'] == json.loads(path.read_text())['version'] == 2
    room = service.room(SCOPE)
    assert room['cars'][0]['id'] == 'C1_OLD' and room['cars'][0]['deadline'] == 300
    assert room['outbox'][0]['id'] == 'notice' and room['next_refresh'] == 180
    asyncio.run(service.tick())
    assert not service.room(SCOPE)['cars']


def test_failed_notice_survives_restart_then_retries_at_four_minutes(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        send.side_effect = OSError('network unavailable')
        clock[0] = 600
        await service.tick()
        assert not service.room(SCOPE)['cars'] and service.room(SCOPE)['outbox']
        restored = queue.WaitingQueues(service.path, send, service.profile, service.bind, clock=lambda: clock[0])
        send.side_effect = None
        before = send.await_count
        clock[0] = 839
        await restored.tick()
        assert send.await_count == before
        clock[0] = 840
        await restored.tick()
        assert not restored.room(SCOPE)['outbox']
        assert '自动解散' in texts(send)[-1]
    asyncio.run(scenario())


def test_atomic_save_failure_rolls_back_members_timer_and_outbox(setup_queue, monkeypatch):
    service, _, send, act = setup_queue
    asyncio.run(act())
    original = queue.os.replace
    count = [0]
    def fail_second(*args):
        count[0] += 1
        if count[0] == 2:
            raise OSError('disk full')
        return original(*args)
    monkeypatch.setattr(queue.os, 'replace', fail_second)
    with pytest.raises(OSError, match='disk full'):
        asyncio.run(act('我也要等车', uid='u2'))
    room = service.room(SCOPE)
    assert list(room['cars'][0]['members']) == ['u1'] and room['cars'][0]['deadline'] == 600
    assert not room['outbox'] and not texts(send, '加入了')
    assert 'u2' not in next(iter(json.loads(service.path.read_text())['rooms'].values()))['cars'][0]['members']


def test_other_group_error_cannot_restore_inflight_dissolution_notice(setup_queue):
    service, clock, send, act = setup_queue
    other = ('official-a', 'app-a', 'other-group')
    async def scenario():
        await act()
        clock[0] = 400
        await act(scope=other)
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(scope, text, buttons, reply):
            if scope == SCOPE and '自动解散' in text:
                started.set()
                await release.wait()
            return {'id': 'ok'}
        send.side_effect = delayed
        clock[0] = 600
        task = asyncio.create_task(service.tick())
        await asyncio.wait_for(started.wait(), 1)
        await act('unknown', scope=other)
        release.set()
        await task
        assert not service.room(SCOPE)['outbox'] and service.room(other)['cars']
    asyncio.run(scenario())


def test_without_proactive_permission_keeps_notice_until_new_passive_reply(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        service.proactive = False
        clock[0] = 600
        await service.tick()
        assert service.room(SCOPE)['outbox'] and send.await_count == 1
        await act('等车帮助')
        assert not service.room(SCOPE)['outbox'] and texts(send, '自动解散')
    asyncio.run(scenario())


@pytest.mark.parametrize('other', [('official-b', 'app-a', SCOPE[2]), ('official-a', 'app-b', SCOPE[2]), ('official-a', 'app-a', 'other')])
def test_scope_isolation(setup_queue, other):
    service, _, send, act = setup_queue
    async def scenario():
        await act()
        await act(scope=other)
        token = service.room(SCOPE)['cars'][0]['id']
        await act('我也要等车', token, uid='u2', scope=other)
        assert list(service.room(SCOPE)['cars'][0]['members']) == ['u1']
        assert list(service.room(other)['cars'][0]['members']) == ['u1']
    asyncio.run(scenario())


def test_pending_identity_is_not_trusted_and_preserves_space_id(setup_queue):
    service, _, send, act = setup_queue
    service.bind.return_value = dict(pending=True, message='请 /确认关联 AABBCCDDEEFF0011')
    asyncio.run(act('等车ID', '"示例 玩家"'))
    service.bind.assert_awaited_once_with(SCOPE, 'u1', '示例 玩家')
    assert not service.room(SCOPE)['bindings']
    assert '/确认关联' in texts(send)[-1]


@pytest.mark.parametrize('field,value', [('deadline', 'broken'), ('deadline', float('nan')), ('members', []), ('ready', 3)])
def test_invalid_saved_car_is_rejected_without_overwriting_file(setup_queue, field, value):
    service, _, _, act = setup_queue
    asyncio.run(act())
    saved = json.loads(service.path.read_text())
    next(iter(saved['rooms'].values()))['cars'][0][field] = value
    original = json.dumps(saved)
    service.path.write_text(original)
    with pytest.raises(ValueError):
        queue.WaitingQueues(service.path, None, None, None)
    assert service.path.read_text() == original


def test_corrupt_store_is_not_overwritten(tmp_path):
    path = tmp_path / 'bad.json'
    path.write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        queue.WaitingQueues(path, None, None, None)
    assert path.read_text() == '{broken'


@pytest.mark.parametrize('text,expected', [
    ('/离队', ('跑路了再见', '')), ('/解散', ('解散队伍', '')),
    ('/进队等车', ('我也要等车', '')), ('/发送群号 123456789', ('人齐了发车', '123456789')),
    ('/解散队伍', ('解散队伍', '')), ('/友谊等车', ('等车', '')), ('等车', ('等车', '')),
    ('/我也要等车 C1_old', ('我也要等车', 'C1_old')), ('/人齐了发车 123456789', ('人齐了发车', '123456789')),
    ('/帮助 友谊等车', ('等车帮助', '')), ('/等车帮助', ('等车帮助', '')),
    ('/友谊等车帮助', ('等车帮助', '')), ('/呼叫全体', ('呼叫全体', '')),
    ('呼叫全体', ('呼叫全体', '')), ('/呼叫全体天气', None),
    ('/友谊等车天气', None), ('普通聊天', None), ('/帮助 等车 其他', None)])
def test_command_filter_is_strict_and_old_aliases_work(text, expected):
    assert queue.parse_command(text) == expected


def test_call_all_cooldown_is_shared_by_users_and_cars_without_changing_timers(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        await act('再开一车', uid='u2')
        before = [(c['id'], c['deadline'], dict(c['members'])) for c in service.room(SCOPE)['cars']]
        clock[0] = 50
        sent_before = send.await_count
        await act('呼叫全体')
        calls = texts(send, '<qqbot-at-everyone />')
        assert len(calls) == 1 and '友谊' in calls[0]
        assert send.await_count == sent_before + 1
        assert '**1号车 · 1人**' in calls[0] and '**2号车 · 1人**' in calls[0]
        assert all(name in calls[0] for name in ('群名片u1', '群名片u2'))
        assert calls[0].count('![头像 #32px #32px]') == 2
        assert calls[0].count('0:50') == 2 and calls[0].count('自动清理：9分10秒') == 2
        assert all(car['id'] in calls[0] for car in service.room(SCOPE)['cars'])
        assert 'qqbot-at-user' not in calls[0]
        await act('呼叫全体', uid='u2')
        assert len(texts(send, '<qqbot-at-everyone />')) == 1
        assert '30分00秒' in texts(send)[-1]
        clock[0] = 50.1
        await act('呼叫全体', uid='outsider')
        assert '30分00秒' in texts(send)[-1]
        assert before == [(c['id'], c['deadline'], dict(c['members'])) for c in service.room(SCOPE)['cars']]
        assert service.room(SCOPE)['next_refresh'] == 240
        assert not service.room(SCOPE)['outbox']
    asyncio.run(scenario())


def test_call_all_empty_panel_does_not_create_a_car(setup_queue):
    service, _, send, act = setup_queue
    asyncio.run(act('呼叫全体'))
    send.assert_awaited_once()
    text = texts(send)[0]
    assert text.count('<qqbot-at-everyone />') == 1
    assert '**友谊等车**' in text and '当前没有队伍喵。' in text
    assert not service.room(SCOPE)['cars']


def test_call_all_panel_excludes_expired_cars_and_does_not_repeat_on_broadcast(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        clock[0] = 100
        await act('再开一车', uid='u2')
        clock[0] = 600
        await act('呼叫全体', uid='u2')
        text = texts(send, '<qqbot-at-everyone />')[0]
        assert '1号车' not in text and '**2号车 · 1人**' in text
        assert '群名片u1' not in text and '群名片u2' in text
        clock[0] = 640
        await service.tick()
        assert '<qqbot-at-everyone' not in texts(send)[-1]
        assert '**2号车 · 1人**' in texts(send)[-1]
        assert len(texts(send, '<qqbot-at-everyone />')) == 1
        assert service.room(SCOPE)['call_all_until'] == 2400
    asyncio.run(scenario())


def test_call_all_expiry_boundary_survives_restart_empty_room_and_midnight(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        clock[0] = datetime(2026, 11, 7, 15, 59, tzinfo=timezone.utc).timestamp()
        await act()
        await act('呼叫全体')
        until = clock[0] + 1800
        clock[0] += 600
        await service.tick()
        restored = queue.WaitingQueues(service.path, send, service.profile, service.bind, clock=lambda: clock[0])
        assert not restored.room(SCOPE)['cars']
        clock[0] = until - 0.01
        await restored.execute(SCOPE, '呼叫全体', '', 'u2', '萌新', 'restored-1')
        assert len(texts(send, '<qqbot-at-everyone />')) == 1
        assert '0分01秒' in texts(send)[-1]
        clock[0] = until
        await restored.execute(SCOPE, '呼叫全体', '', 'u2', '萌新', 'restored-2')
        assert len(texts(send, '<qqbot-at-everyone />')) == 2
        assert restored.room(SCOPE)['call_all_until'] == until + 1800
    asyncio.run(scenario())


@pytest.mark.parametrize('other', [('official-b', 'app-a', SCOPE[2]), ('official-a', 'app-b', SCOPE[2]), ('official-a', 'app-a', 'other')])
def test_call_all_cooldown_isolated_by_original_connection_app_and_group(setup_queue, other):
    _, _, send, act = setup_queue
    async def scenario():
        await act('呼叫全体')
        await act('呼叫全体', scope=other)
        assert len(texts(send, '<qqbot-at-everyone />')) == 2
        assert [c.args[0] for c in send.await_args_list] == [SCOPE, other]
    asyncio.run(scenario())


def test_concurrent_call_all_and_redelivery_send_only_once(setup_queue):
    service, _, send, act = setup_queue
    async def scenario():
        await asyncio.gather(*(act('呼叫全体', uid=f'u{i}', msg=f'call-{i}') for i in range(5)))
        assert len(texts(send, '<qqbot-at-everyone />')) == 1
        before = send.await_count
        await act('呼叫全体', msg='call-0')
        assert send.await_count == before
        assert not service.room(SCOPE)['outbox']
    asyncio.run(scenario())


def test_call_all_failure_is_not_retried_by_interaction_tick_or_restart(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        send.side_effect = [TimeoutError('unknown delivery'), {'id': 'failure-notice'}]
        await act('呼叫全体')
        assert '发送未确认' in texts(send)[-1]
        assert service.room(SCOPE)['call_all_until'] == 1800
        assert not service.room(SCOPE)['outbox']
        send.side_effect = None
        clock[0] = 240
        await service.tick()
        await act('等车帮助')
        assert len(texts(send, '<qqbot-at-everyone />')) == 1
        restored = queue.WaitingQueues(service.path, send, service.profile, service.bind, clock=lambda: clock[0])
        await restored.execute(SCOPE, '呼叫全体', '', 'u2', '萌新', 'retry')
        assert len(texts(send, '<qqbot-at-everyone />')) == 1
    asyncio.run(scenario())


def test_cancelled_call_all_keeps_persisted_cooldown_without_retry(setup_queue):
    service, _, send, act = setup_queue
    send.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(act('呼叫全体'))
    restored = queue.WaitingQueues(service.path, None, None, None)
    assert restored.room(SCOPE)['call_all_until'] == 1800
    assert not restored.room(SCOPE)['outbox']


def test_invalid_call_all_arguments_do_not_consume_cooldown(setup_queue):
    service, _, send, act = setup_queue
    asyncio.run(act('呼叫全体', '1'))
    assert '用法' in texts(send)[-1]
    assert not service.room(SCOPE).get('call_all_until')
    assert not texts(send, '<qqbot-at-everyone />')


def test_call_all_persistence_failure_never_sends_mass_mention(setup_queue, monkeypatch):
    service, _, send, act = setup_queue
    asyncio.run(act())
    send.reset_mock()
    original = queue.os.replace
    def fail_reservation(*args):
        if service.room(SCOPE).get('call_all_until'):
            raise OSError('disk full')
        return original(*args)
    monkeypatch.setattr(queue.os, 'replace', fail_reservation)
    with pytest.raises(OSError, match='disk full'):
        asyncio.run(act('呼叫全体'))
    assert not service.room(SCOPE).get('call_all_until')
    send.assert_not_awaited()


@pytest.mark.parametrize('value', ['bad', True, -1, float('nan'), float('inf')])
def test_invalid_call_all_cooldown_does_not_overwrite_store(setup_queue, value):
    service, _, _, act = setup_queue
    asyncio.run(act())
    saved = json.loads(service.path.read_text())
    next(iter(saved['rooms'].values()))['call_all_until'] = value
    original = json.dumps(saved)
    service.path.write_text(original)
    with pytest.raises(ValueError, match='冷却'):
        queue.WaitingQueues(service.path, None, None, None)
    assert service.path.read_text() == original


@pytest.mark.parametrize('source,expected', [
    ({'member': {'nick': '群名片'}, 'author': {'username': 'QQ昵称'}}, '群名片'),
    ({'author': {'card': '群名片', 'username': 'QQ昵称'}}, '群名片'),
    ({'author': {'username': 'QQ昵称'}}, 'QQ昵称'),
    ({'raw_data': {'member': {'card': '群名片'}, 'author': {'username': 'QQ昵称'}}}, '群名片'),
    ({}, '适配器昵称'),
])
def test_group_card_has_priority_with_qq_name_fallback(source, expected):
    assert queue.group_nickname(source, '适配器昵称') == expected


def test_concurrent_join_is_serialized_with_one_ready_notice_and_one_deadline(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        clock[0] = 100
        await asyncio.gather(act('我也要等车', uid='u2'), act('我也要等车', uid='u3'),
                             act('我也要等车', uid='u3'))
        car = service.room(SCOPE)['cars'][0]
        assert len(car['members']) == 3 and car['deadline'] == 700
        assert len(texts(send, '快去接友谊')) == 1
    asyncio.run(scenario())


def test_full_transition_atomic_failure_cannot_reset_timer_or_queue_ready_notice(setup_queue, monkeypatch):
    service, clock, send, act = setup_queue
    asyncio.run(act())
    asyncio.run(act('我也要等车', uid='u2'))
    original = queue.os.replace
    counter = [0]
    def fail_membership(*args):
        counter[0] += 1
        if counter[0] == 2:
            raise OSError('disk full')
        return original(*args)
    monkeypatch.setattr(queue.os, 'replace', fail_membership)
    clock[0] = 100
    with pytest.raises(OSError):
        asyncio.run(act('我也要等车', uid='u3'))
    car = service.room(SCOPE)['cars'][0]
    assert not car['ready'] and car['deadline'] == 600 and list(car['members']) == ['u1', 'u2']
    assert not service.room(SCOPE)['outbox'] and not texts(send, '快去接友谊')


def test_notification_uses_native_member_ids_when_car_removed_before_api_confirmation(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        await act('我也要等车', uid='u2')
        send.side_effect = OSError('offline')
        clock[0] = 600
        await service.tick()
        restored = queue.WaitingQueues(service.path, send, None, None, clock=lambda: clock[0])
        send.side_effect = None
        clock[0] = 840
        await restored.tick()
        notice = texts(send)[-1]
        assert all(notice.count(queue.mention(u)) == 1 for u in ['u1', 'u2'])
        assert 'qqbot-cmd-input' not in notice and '群名片u1' not in notice
    asyncio.run(scenario())


@pytest.mark.parametrize('command', ['/离队', '/解散', '/发送群号 123456789'])
def test_joined_operations_without_car_number_only_target_callers_car(setup_queue, command):
    service, _, send, act = setup_queue
    async def scenario():
        await act()
        for u in ['u3', 'u4']:
            await act('我也要等车', uid=u)
        await act('再开一车', uid='u2')
        first, own = service.room(SCOPE)['cars']
        for u in ['u5', 'u6']:
            await act('我也要等车', own['id'], uid=u)
        first_snapshot = json.loads(json.dumps(first))
        action, raw = queue.parse_command(command)
        await act(action, raw, uid='u2')
        assert service.room(SCOPE)['cars'][0] == first_snapshot
        if command == '/离队':
            assert set(own['members']) == {'u5', 'u6'}
            notice = texts(send, '离开了')[-1]
        else:
            assert service.room(SCOPE)['cars'] == [first]
            notice = texts(send, '发车啦' if '群号' in command else '已解散')[-1]
        assert queue.mention('u5') in notice and queue.mention('u6') in notice
        assert not any(queue.mention(u) in notice for u in ['u1', 'u3', 'u4'])
    asyncio.run(scenario())


@pytest.mark.parametrize('command', ['/离队', '/解散', '/发送群号 123456789'])
def test_unjoined_operations_cannot_fall_back_to_other_users_only_car(setup_queue, command):
    service, _, send, act = setup_queue
    async def scenario():
        await act()
        before = json.loads(json.dumps(service.room(SCOPE)['cars']))
        action, raw = queue.parse_command(command)
        await act(action, raw, uid='outsider')
        assert service.room(SCOPE)['cars'] == before
        assert '还没有加入队伍' in texts(send)[-1]
    asyncio.run(scenario())


def test_number_failure_retries_only_plain_number_after_restart_without_mentioning_again(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        for uid in ['u2', 'u3']:
            await act('我也要等车', uid=uid)
        send.reset_mock()
        async def fail_number(scope, text, buttons, reply, *, plain=False):
            if plain:
                raise OSError('number send failed')
            return {'id': 'sent'}
        send.side_effect = fail_number
        await act('人齐了发车', '123456789')
        pending = service.room(SCOPE)['outbox']
        assert len(pending) == 1 and pending[0]['format'] == 'text'
        assert pending[0]['text'] == '123456789' and not pending[0]['users']
        restored = queue.WaitingQueues(service.path, send, None, None, clock=lambda: clock[0])
        send.side_effect = None
        before = send.await_count
        clock[0] = 239
        await restored.tick()
        assert send.await_count == before
        clock[0] = 240
        await restored.tick()
        assert not restored.room(SCOPE)['outbox']
        assert len(texts(send, '发车啦')) == 1
        assert send.await_args.kwargs == {'plain': True} and send.await_args.args[1] == '123456789'
    asyncio.run(scenario())


def test_number_waits_for_departure_notice_confirmation_before_sending(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        for uid in ['u2', 'u3']:
            await act('我也要等车', uid=uid)
        send.reset_mock()
        async def fail_notice(scope, text, buttons, reply, *, plain=False):
            if '发车啦' in text:
                raise OSError('notice send failed')
            return {'id': 'sent'}
        send.side_effect = fail_notice
        await act('人齐了发车', '123456789')
        pending = service.room(SCOPE)['outbox']
        assert len(pending) == 2 and pending[1]['after'] == pending[0]['id']
        assert not any(c.kwargs.get('plain') for c in send.await_args_list)
        restored = queue.WaitingQueues(service.path, send, None, None, clock=lambda: clock[0])
        send.side_effect = None
        before = send.await_count
        clock[0] = 240
        await restored.tick()
        retry = send.await_args_list[before:]
        assert len(retry) == 2 and '发车啦' in retry[0].args[1]
        assert retry[1].args[1] == '123456789' and retry[1].kwargs == {'plain': True}
        assert not restored.room(SCOPE)['outbox']
    asyncio.run(scenario())


def test_upgrade_converts_only_pending_legacy_group_number_link_and_is_replayable(setup_queue):
    service, _, send, act = setup_queue
    asyncio.run(act())
    room = service.room(SCOPE)
    room['cars'] = []
    room['outbox'] = [dict(id='legacy', users=['u2'], names={'u2': '萌新'}, retry_at=100,
                          text='1号车发车啦！群号：' + queue.input_text('123456789', '123456789') + '\n发车者：云玩家，快来集合喵～')]
    service.path.write_text(json.dumps(service.state, ensure_ascii=False))
    restored = queue.WaitingQueues(service.path, send, None, None, clock=lambda: 0)
    pending = restored.room(SCOPE)['outbox']
    assert len(pending) == 2 and pending[0]['id'] == 'legacy'
    assert pending[0]['users'] == ['u2'] and 'qqbot-cmd-input' not in pending[0]['text']
    assert pending[1]['format'] == 'text' and pending[1]['after'] == 'legacy'
    assert pending[1]['text'] == '123456789' and pending[1]['retry_at'] == 100
    again = queue.WaitingQueues(service.path, send, None, None, clock=lambda: 0)
    assert again.room(SCOPE)['outbox'] == pending


def test_legacy_number_conversion_write_failure_preserves_original_pending_record(setup_queue, monkeypatch):
    service, _, send, act = setup_queue
    asyncio.run(act())
    room = service.room(SCOPE)
    room['outbox'] = [dict(id='legacy', users=['u2'], retry_at=0,
                          text='1号车发车啦！群号：' + queue.input_text('123456789', '123456789') + '\n发车者：云玩家')]
    original = json.dumps(service.state, ensure_ascii=False)
    service.path.write_text(original)
    def fail_replace(*args):
        raise OSError('disk full')
    monkeypatch.setattr(queue.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='disk full'):
        queue.WaitingQueues(service.path, send, None, None, clock=lambda: 0)
    assert service.path.read_text() == original


def test_old_notice_name_snapshots_cannot_duplicate_or_override_native_group_names(setup_queue):
    service, _, send, act = setup_queue
    async def scenario():
        await act()
        room = service.room(SCOPE)
        room['outbox'] = [dict(id='legacy', users=['u1', 'u1', 'u2'], names={'u1': '旧QQ昵称', 'u2': '旧群名片'},
                              retry_at=0, text='1号车已解散喵。')]
        await service.flush(SCOPE, room)
        text = texts(send)[-1]
        assert text.count(queue.mention('u1')) == text.count(queue.mention('u2')) == 1
        assert '旧QQ昵称' not in text and '旧群名片' not in text and 'qqbot-cmd-input' not in text
    asyncio.run(scenario())


def test_panels_only_fill_names_but_notifications_keep_native_mentions(setup_queue):
    service, _, send, _ = setup_queue
    async def scenario():
        for i, (action, uid, raw) in enumerate([
            ('等车', 'u1', ''), ('我也要等车', 'u2', ''), ('跑路了再见', 'u2', ''),
            ('我也要等车', 'u2', ''), ('我也要等车', 'u3', ''), ('人齐了发车', 'u1', '123456789'),
        ]):
            await service.execute(SCOPE, action, raw, uid, '缓存QQ昵称' + uid, str(i))
        for text in texts(send):
            if '![头像' in text:
                assert 'qqbot-at-user' not in text
                assert 'text="@缓存QQ昵称' in text and 'show="@' not in text
            else:
                assert '缓存QQ昵称' not in text and 'text="@' not in text
        assert len(texts(send, '快去接友谊')) == 1
        assert len([c for c in send.await_args_list if c.kwargs.get('plain') and c.args[1] == '123456789']) == 1
    asyncio.run(scenario())
