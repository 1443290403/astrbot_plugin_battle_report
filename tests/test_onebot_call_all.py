import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from onebot_call_all import OneBotCallAll


SCOPE = ('snowluma-A', '10001', '20002')


def test_confirmed_delivery_cooldown_shared_by_users_and_survives_restart(tmp_path):
    path = tmp_path / 'call.json'
    now = [100]
    service = OneBotCallAll(path, clock=lambda: now[0])
    send = AsyncMock(return_value={'message_id': -123})
    async def run():
        assert await service.execute(SCOPE, 'event-1', send) is None
        assert '冷却中' in await service.execute(SCOPE, 'other-user-event', send)
        assert send.await_count == 1
        restarted = OneBotCallAll(path, clock=lambda: now[0])
        now[0] = 1899
        assert '冷却中' in await restarted.execute(SCOPE, 'new-event', send)
        now[0] = 1900
        assert await restarted.execute(SCOPE, 'next-event', send) is None
        assert send.await_count == 2
    asyncio.run(run())


@pytest.mark.parametrize('result', [None, {}, {'message_id': True}, {'message_id': 0},
    {'message_id': 'bad'}, {'retcode': 100, 'message_id': 23},
    {'status': 'failed', 'message_id': 23}, TimeoutError(), RuntimeError('权限不足')])
def test_failed_send_allows_new_explicit_retry_but_never_replays_same_event(tmp_path, result):
    service = OneBotCallAll(tmp_path / 'call.json')
    send = AsyncMock(side_effect=[result, {'status': 'ok', 'data': {'message_id': '789'}}])
    async def run():
        assert '未进入冷却' in await service.execute(SCOPE, 'event-1', send)
        assert await service.execute(SCOPE, 'event-1', send) is None
        restarted = OneBotCallAll(service.path)
        assert await restarted.execute(SCOPE, 'event-1', send) is None
        assert await restarted.execute(SCOPE, 'event-2', send) is None
        assert send.await_count == 2
        assert restarted.state['rooms'][service.key(SCOPE)]['until'] > restarted.clock()
    asyncio.run(run())


def test_parallel_requests_only_send_once_and_scopes_are_isolated(tmp_path):
    service = OneBotCallAll(tmp_path / 'call.json')
    send = AsyncMock(return_value={'message_id': 123})
    async def run():
        results = await asyncio.gather(*(service.execute(SCOPE, str(i), send) for i in range(5)))
        assert sum(v is None for v in results) == 1
        assert send.await_count == 1
        for i, scope in enumerate([('snowluma-B', '10001', '20002'),
                                  ('snowluma-A', '10002', '20002'),
                                  ('snowluma-A', '10001', '20003')]):
            await service.execute(scope, 'independent-' + str(i), send)
        assert send.await_count == 4
    asyncio.run(run())


def test_event_must_be_persisted_before_send_and_missing_id_does_not_send(tmp_path, monkeypatch):
    service = OneBotCallAll(tmp_path / 'call.json')
    send = AsyncMock()
    async def run():
        assert '消息编号' in await service.execute(SCOPE, '', send)
        monkeypatch.setattr(service, 'save', lambda: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):
            await service.execute(SCOPE, 'event-1', send)
        assert service.state['rooms'][service.key(SCOPE)]['seen'] == []
        send.assert_not_awaited()
    asyncio.run(run())


def test_cancellation_does_not_set_cooldown_and_no_automatic_retry(tmp_path):
    service = OneBotCallAll(tmp_path / 'call.json')
    send = AsyncMock(side_effect=asyncio.CancelledError)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await service.execute(SCOPE, 'event-1', send)
        assert await service.execute(SCOPE, 'event-1', send) is None
        assert service.state['rooms'][service.key(SCOPE)]['until'] == 0
        send.side_effect = None
        send.return_value = {'message_id': 123}
        await service.execute(SCOPE, 'event-2', send)
        assert send.await_count == 2
    asyncio.run(run())


def test_cooldown_starts_after_receipt_and_retained_if_post_send_save_fails(tmp_path, monkeypatch):
    now = [100]
    service = OneBotCallAll(tmp_path / 'call.json', clock=lambda: now[0])
    async def send():
        now[0] = 500
        monkeypatch.setattr(service, 'save', lambda: (_ for _ in ()).throw(OSError('disk full')))
        return {'message_id': 123}
    asyncio.run(service.execute(SCOPE, 'event-1', send))
    assert service.state['rooms'][service.key(SCOPE)]['until'] == 2300


@pytest.mark.parametrize('until', [True, -1, float('nan'), '1800'])
def test_invalid_saved_cooldown_is_rejected(tmp_path, until):
    path = tmp_path / 'call.json'
    path.write_text(json.dumps({'version': 1, 'rooms': {OneBotCallAll.key(SCOPE): {
        'scope': list(SCOPE), 'until': until, 'seen': []}}}))
    with pytest.raises(ValueError):
        OneBotCallAll(path)
