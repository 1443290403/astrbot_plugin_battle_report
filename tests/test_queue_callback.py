import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from queue_callback import QueueCallbackHooks, callback_identity, mapped_group, owns, validate_ack_response
from official_queue import CALL_ALL_CALLBACK


@pytest.mark.parametrize('reply', [None, '', ' \r\n', {}, '{}', {'code':0}, {'code':'0'},
    '{"code":0,"err_code":"0"}', {'code':0,'err_code':0,'retcode':'0'}])
def test_sdk_successful_empty_or_json_text_ack_is_accepted(reply):
    validate_ack_response(reply)


@pytest.mark.parametrize('reply', ['not json', '[]', 'null', '0', [], False, b'',
    {'code':630003}, '{"code":630003}', {'code':'630003'}, {'code':None}, {'code':False},
    {'code':0,'err_code':630002}, {'status':'failed'}, '{"status":"failed","code":0}'])
def test_ack_errors_and_unknown_shapes_remain_rejected(reply):
    with pytest.raises(RuntimeError):
        validate_ack_response(reply)


def test_sdk_objects_and_raw_payloads_keep_actual_interaction_id():
    source = SimpleNamespace(appid='123')
    payload = SimpleNamespace(id='interaction-id', event_id='INTERACTION_CREATE:outer', application_id='123',
        type=11, scene='group', chat_type=1, group_openid='group-id', group_member_openid='member-id',
        data=SimpleNamespace(resolved=SimpleNamespace(button_data=CALL_ALL_CALLBACK, button_id='call_all')))
    assert owns(payload)
    assert callback_identity(source, payload) == ['interaction-id','group-id','member-id']


@pytest.mark.parametrize('key,value', [('onebot_self_id',''),('onebot_self_id','openid'),
    ('onebot_group_id','10001;other'),('onebot_group_id',True),('onebot_platform_id',None)])
def test_invalid_mapping_never_guesses_target(key, value):
    row = dict(official_platform_id='official',official_group_openid='group',
        onebot_platform_id='snow',onebot_self_id='10001',onebot_group_id='20002')
    row[key] = value
    with pytest.raises(ValueError):
        mapped_group({'official_queue_call_all_routes':[row]},'official','group')


def test_sdk_hooks_only_intercept_owned_buttons_and_restore_without_stacking():
    original = AsyncMock()
    client = SimpleNamespace(intents=1, _connection=None, on_interaction_create=original)
    source = SimpleNamespace(appid='123',client=client,meta=lambda:SimpleNamespace(name='qq_official',id='official'))
    context = SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[source]))
    handler = AsyncMock()
    hooks = QueueCallbackHooks(context,handler,logging.getLogger('hooks'))
    hooks.sync(); first = client.on_interaction_create
    hooks.sync(); assert client.on_interaction_create is first
    assert client.intents & (1 << 26)
    async def run():
        await first({'data':{'resolved':{'button_data':'another-plugin'}}})
        original.assert_awaited_once(); handler.assert_not_awaited()
        await first({'data':{'resolved':{'button_data':CALL_ALL_CALLBACK}}})
        handler.assert_awaited_once()
        await hooks.close()
        assert client.on_interaction_create is original
        await first({'data':{'resolved':{'button_data':CALL_ALL_CALLBACK}}})
        assert handler.await_count == 1
    asyncio.run(run())


def test_removed_connection_detaches_and_shutdown_cancels_pending_work():
    client = SimpleNamespace(intents=1,_connection=None)
    source = SimpleNamespace(appid='123',client=client,meta=lambda:SimpleNamespace(name='qq_official',id='official'))
    context = SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[source]))
    async def run():
        started = asyncio.Event()
        async def handler(*args):
            started.set()
            await asyncio.Event().wait()
        hooks = QueueCallbackHooks(context,handler,logging.getLogger('hooks'))
        hooks.sync()
        task = asyncio.create_task(client.on_interaction_create({'data':{'resolved':{'button_data':CALL_ALL_CALLBACK}}}))
        await started.wait()
        await hooks.close()
        assert task.cancelled() and not hasattr(client,'on_interaction_create')
        hooks2 = QueueCallbackHooks(context,handler,logging.getLogger('hooks'))
        hooks2.sync()
        context.platform_manager.platform_insts.clear(); hooks2.sync()
        assert not hasattr(client,'on_interaction_create')
    asyncio.run(run())
