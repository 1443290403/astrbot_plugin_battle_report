"""官方等车队列：模拟时钟与官方接口，不连接 QQ。"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import official_queue as queue

SCOPE = ('official-a', 'app-a', 'group-openid')


@pytest.fixture
def setup_queue(tmp_path):
    clock = [0.0]
    send = AsyncMock(return_value={'id': 'response'})
    profile = AsyncMock(side_effect=lambda scope, uid, binding: (uid, '60%'))
    bind = AsyncMock(return_value={'home_team': 'a队', 'query': '云玩家'})
    service = queue.WaitingQueues(tmp_path / 'queues.json', send, profile, bind, clock=lambda: clock[0])
    counter = [0]
    async def act(action='等车', raw='', uid='u1', scope=SCOPE, msg=None):
        counter[0] += 1
        await service.execute(scope, action, raw, uid, f'云玩家{uid}', msg or str(counter[0]))
    return service, clock, send, act


def test_buttons_use_scoped_car_tokens_and_departure_prefills_without_sending(setup_queue):
    service, _, send, act = setup_queue
    asyncio.run(act())
    car = service.room(SCOPE)['cars'][0]
    text, buttons = send.await_args.args[1:3]
    assert '1号车' in text and '60%' in text and '等待0分00秒' in text
    assert '游戏中' not in text and '等级' not in text
    buttons = [b for row in buttons['content']['rows'] for b in row['buttons']]
    assert [b['render_data']['label'] for b in buttons] == ['我也要等车', '跑路了再见', '人齐了发车', '再开一车', '等车帮助']
    go = buttons[2]['action']
    assert go['data'] == f'/人齐了发车 {car["id"]} ' and go['enter'] is False
    assert buttons[0]['action']['enter'] is True
    assert buttons[0]['action']['permission'] == {'type': 2}


def test_panel_separates_avatar_name_and_wait_from_secondary_quote_lines(setup_queue):
    service, _, send, act = setup_queue
    service.profile.side_effect = lambda scope, uid, binding: ('示例 玩家', '60%')
    asyncio.run(act(uid='official-member-openid'))
    text = send.await_args.args[1]
    assert ('![头像 #32px #32px](https://q.qlogo.cn/qqapp/app-a/official-member-openid/100) '
            '示例 玩家 ｜ 已等待0分00秒') in text.splitlines()
    assert '> 当月胜率：60%' in text.splitlines()
    assert '> 自动清理：9分钟' in text.splitlines()
    assert '> 广播间隔：3分钟' in text.splitlines()
    assert all(line.startswith('> ') for line in text.splitlines()
               if any(label in line for label in ('当月胜率：', '自动清理：', '广播间隔：')))
    assert '每3分钟刷新' not in text and '剩余' not in text
    assert '游戏中' not in text and '等级' not in text


def test_panel_avatar_and_cleanup_remaining_survive_refresh_restart_and_ready_transition(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        original_deadline = service.room(SCOPE)['cars'][0]['deadline']
        clock[0] = 180
        restored = queue.WaitingQueues(service.path, send, service.profile, service.bind, clock=lambda: clock[0])
        await restored.tick()
        text = send.await_args.args[1]
        assert 'https://q.qlogo.cn/qqapp/app-a/u1/100' in text
        assert '已等待3分00秒' in text and '> 自动清理：6分钟' in text
        assert restored.room(SCOPE)['cars'][0]['deadline'] == original_deadline
        await restored.execute(SCOPE, '我也要等车', '', 'u2', '萌新', 'join-2')
        await restored.execute(SCOPE, '我也要等车', '', 'u3', '遗老', 'join-3')
        assert '> 自动清理：15分钟' in send.await_args.args[1]
        assert send.await_args.args[1].count('![头像 #32px #32px]') == 3
        clock[0] = 361
        await restored.tick()
        assert '> 自动清理：11分59秒' in send.await_args.args[1]
        assert '> 广播间隔：3分钟' in send.await_args.args[1]
    asyncio.run(scenario())


def test_partial_car_refreshes_each_three_minutes_and_dissolves_at_nine(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        await act('我也要等车', uid='u2')
        clock[0] = 179
        await service.tick()
        assert send.await_count == 2
        clock[0] = 180
        await service.tick()
        assert send.await_count == 3 and '等待3分00秒' in send.await_args.args[1]
        clock[0] = 360
        await service.tick()
        assert send.await_count == 4
        clock[0] = 540
        await service.tick()
        assert not service.room(SCOPE)['cars']
        text = send.await_args.args[1]
        assert '已自动解散' in text
        assert queue.mention('u1') in text and queue.mention('u2') in text
        assert not service.room(SCOPE)['outbox']
    asyncio.run(scenario())


def test_three_members_start_fifteen_minutes_extra_and_duplicate_joins_do_not_reset(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        clock[0] = 100
        await act('我也要等车', uid='u2')
        clock[0] = 200
        await act('我也要等车', uid='u3')
        car = service.room(SCOPE)['cars'][0]
        assert car['ready'] and car['deadline'] == 1100
        clock[0] = 300
        await act('我也要等车', uid='u4')
        clock[0] = 700
        await act('我也要等车', uid='u3')
        assert car['deadline'] == 1100 and car['members']['u3']['joined_at'] == 200
        clock[0] = 1099
        await service.tick()
        assert service.room(SCOPE)['cars']
        clock[0] = 1100
        await service.tick()
        assert not service.room(SCOPE)['cars']
        assert all(queue.mention(f'u{i}') in send.await_args.args[1] for i in range(1, 5))
    asyncio.run(scenario())


def test_leaving_restarts_only_when_count_drops_below_three(setup_queue):
    service, clock, _, act = setup_queue
    async def scenario():
        await act()
        for uid in ['u2', 'u3', 'u4']:
            await act('我也要等车', uid=uid)
        car = service.room(SCOPE)['cars'][0]
        clock[0] = 60
        await act('跑路了再见', uid='u4')
        assert car['deadline'] == 900
        clock[0] = 120
        await act('跑路了再见', uid='u3')
        assert not car['ready'] and car['deadline'] == 660
        clock[0] = 180
        await act('我也要等车', uid='u3')
        assert car['deadline'] == 1080 and car['members']['u1']['joined_at'] == 0
    asyncio.run(scenario())


def test_multiple_cars_are_independent_and_departure_mentions_other_members_only(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        await act('我也要等车', uid='u2')
        await act('我也要等车', uid='u3')
        clock[0] = 30
        await act('再开一车')
        first, second = service.room(SCOPE)['cars']
        assert not second['members']  # 已在别车的创建者不会同时加入两车。
        await act('我也要等车', second['id'], 'u4')
        await act('我也要等车', second['id'], 'u2')
        assert '另一辆车' in send.await_args.args[1]
        assert list(second['members']) == ['u4']
        before = second['deadline']
        await act('人齐了发车', first['id'] + ' 123456789')
        assert service.room(SCOPE)['cars'] == [second] and second['deadline'] == before
        text = send.await_args.args[1]
        assert '123456789' in text and '发车啦' in text
        assert queue.mention('u2') in text and queue.mention('u3') in text
        assert queue.mention('u1') not in text and queue.mention('u4') not in text
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['too_few', 'outsider', 'bad_group'])
def test_departure_guard_does_not_remove_car(setup_queue, case):
    service, _, send, act = setup_queue
    async def scenario():
        await act()
        if case != 'too_few':
            await act('我也要等车', uid='u2')
            await act('我也要等车', uid='u3')
        token = service.room(SCOPE)['cars'][0]['id']
        await act('人齐了发车', token + (' invalid' if case == 'bad_group' else ' 123456789'),
                  uid='u4' if case == 'outsider' else 'u1')
        assert len(service.room(SCOPE)['cars']) == 1
        assert '发车啦' not in send.await_args.args[1]
    asyncio.run(scenario())


def test_old_panel_cannot_modify_a_new_car_and_duplicate_events_are_ignored(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act(msg='original')
        old = service.room(SCOPE)['cars'][0]['id']
        await act('再开一车', msg='original')
        assert len(service.room(SCOPE)['cars']) == 1
        clock[0] = 540
        await service.tick()
        await act()
        new = service.room(SCOPE)['cars'][0]
        await act('我也要等车', old, 'u2')
        assert list(service.room(SCOPE)['cars'][0]['members']) == ['u1']
        assert '已经结束' in send.await_args.args[1] and new['number'] == 2
    asyncio.run(scenario())


def test_restart_restores_deadlines_and_retries_failed_dissolution_notice(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        send.side_effect = OSError('network unavailable')
        clock[0] = 540
        await service.tick()
        assert not service.room(SCOPE)['cars'] and len(service.room(SCOPE)['outbox']) == 1
        restored = queue.WaitingQueues(service.path, send, service.profile, service.bind, clock=lambda: clock[0])
        send.side_effect = None
        calls = send.await_count
        clock[0] = 600
        await restored.tick()
        assert send.await_count == calls
        clock[0] = 720
        await restored.tick()
        assert not restored.room(SCOPE)['outbox']
        assert '已自动解散' in send.await_args.args[1]
        assert not send.await_args.args[3]  # 旧被动消息过期后走已获授权的主动消息。
    asyncio.run(scenario())


def test_same_group_ids_are_isolated_by_platform_and_bot_appid(setup_queue):
    service, _, _, act = setup_queue
    async def scenario():
        for scope in [SCOPE, ('official-b', 'app-a', SCOPE[2]), (SCOPE[0], 'app-b', SCOPE[2])]:
            await act(scope=scope)
        assert len(service.state['rooms']) == 3
        token = service.room(SCOPE)['cars'][0]['id']
        await act('跑路了再见', token, scope=('official-b', 'app-a', SCOPE[2]))
        assert all(len(r['cars']) == 1 for r in service.state['rooms'].values())
    asyncio.run(scenario())


def test_panel_profile_failure_does_not_prevent_queue_operations(setup_queue):
    service, _, send, act = setup_queue
    service.profile.side_effect = RuntimeError('database read failure')
    asyncio.run(act())
    assert '暂不可用' in send.await_args.args[1]
    assert len(service.room(SCOPE)['cars'][0]['members']) == 1


def test_official_id_display_binding_is_scoped_and_keeps_internal_spaces(setup_queue):
    service, _, _, act = setup_queue
    async def scenario():
        await act('等车ID', '"示例 玩家"')
        service.bind.assert_awaited_once_with(SCOPE, 'u1', '示例 玩家')
        assert service.room(SCOPE)['bindings']['u1']['query'] == '云玩家'
    asyncio.run(scenario())


def test_corrupt_store_is_not_silently_overwritten(tmp_path):
    path = tmp_path / 'bad.json'
    original = '{broken'
    path.write_text(original)
    with pytest.raises(json.JSONDecodeError):
        queue.WaitingQueues(path, None, None, None)
    assert path.read_text() == original


def test_atomic_save_failure_rolls_back_queue_change(setup_queue, monkeypatch):
    service, _, _, act = setup_queue
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
    assert list(service.room(SCOPE)['cars'][0]['members']) == ['u1']
    assert 'u2' not in next(iter(json.loads(service.path.read_text())['rooms'].values()))['cars'][0]['members']


@pytest.mark.parametrize('text,expected', [
    ('/等车', ('等车', '')), ('等车', ('等车', '')), ('/人齐了发车 ABC123 123456789', ('人齐了发车', 'ABC123 123456789')),
    ('/帮助 等车', ('等车帮助', '')), ('战报帮助 等车', ('等车帮助', '')),
    ('/帮助 等车 其他', None),
    ('今天等车很久', None), ('/等车天气', None), ('/我也要等车ABC', None)])
def test_command_filter_is_strict(text, expected):
    assert queue.parse_command(text) == expected


def test_other_group_validation_error_cannot_restore_an_inflight_notification(setup_queue):
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
        clock[0] = 540
        tick = asyncio.create_task(service.tick())
        await asyncio.wait_for(started.wait(), 1)
        await act('unknown', scope=other)
        release.set()
        await tick
        assert service.room(SCOPE)['outbox'] == []
        assert len(service.room(other)['cars']) == 1
    asyncio.run(scenario())


def test_without_proactive_permission_expired_reply_does_not_drop_pending_notification(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        service.proactive = False
        clock[0] = 540
        await service.tick()
        assert service.room(SCOPE)['outbox'] and send.await_count == 1
        await act('等车帮助')  # 新交互获得新的被动回复额度，再补发解散提醒。
        assert not service.room(SCOPE)['outbox']
        assert '自动解散' in send.await_args_list[-2].args[1]
    asyncio.run(scenario())


def test_departure_button_without_a_group_number_never_launches_even_for_numeric_uuid(setup_queue, monkeypatch):
    from types import SimpleNamespace
    service, _, send, act = setup_queue
    monkeypatch.setattr(queue, 'uuid4', lambda: SimpleNamespace(hex='12345678901234567890123456789012'))
    async def scenario():
        await act()
        await act('我也要等车', uid='u2')
        await act('我也要等车', uid='u3')
        car = service.room(SCOPE)['cars'][0]
        assert car['id'].startswith('C')
        await act('人齐了发车', car['id'])
        assert service.room(SCOPE)['cars'] and '用法' in send.await_args.args[1]
    asyncio.run(scenario())


def test_restart_keeps_ready_deadline_instead_of_giving_another_fifteen_minutes(setup_queue):
    service, clock, send, act = setup_queue
    async def scenario():
        await act()
        await act('我也要等车', uid='u2')
        clock[0] = 100
        await act('我也要等车', uid='u3')
        restored = queue.WaitingQueues(service.path, send, service.profile, service.bind, clock=lambda: clock[0])
        assert restored.room(SCOPE)['cars'][0]['deadline'] == 1000
        clock[0] = 999
        await restored.tick()
        assert restored.room(SCOPE)['cars']
        clock[0] = 1000
        await restored.tick()
        assert not restored.room(SCOPE)['cars']
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value', [('deadline', 'broken'), ('next_refresh', float('nan')), ('members', []), ('ready', 3)])
def test_invalid_saved_car_is_rejected_without_overwriting_file(setup_queue, field, value):
    service, _, _, act = setup_queue
    asyncio.run(act())
    saved = json.loads(service.path.read_text())
    next(iter(saved['rooms'].values()))['cars'][0][field] = value
    original = json.dumps(saved)
    service.path.write_text(original)
    with pytest.raises(ValueError):
        queue.WaitingQueues(service.path, service.send, service.profile, service.bind)
    assert service.path.read_text() == original


def test_pending_identity_request_does_not_become_a_trusted_saved_binding(setup_queue):
    service, _, send, act = setup_queue
    service.bind.return_value = dict(pending=True, message='请用原 QQ 发送 /确认关联 AABBCCDDEEFF0011')
    asyncio.run(act('等车ID', '云玩家'))
    assert service.room(SCOPE)['bindings'] == {}
    assert '/确认关联' in send.await_args.args[1]
    assert '已关联' not in send.await_args.args[1]
