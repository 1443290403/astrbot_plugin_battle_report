"""OneBot 申请审批：转发、持久化、精确选单及重复/失败处理。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from request_review import ReviewInbox, parse_decision, request_payload

SCOPE = ('onebot-a', '12345')
ADMIN = '10001'


def raw_request(kind='friend', flag='opaque flag:原样', **extra):
    return {'post_type': 'request', 'self_id': 12345, 'request_type': kind,
            'user_id': 20002, 'flag': flag, 'comment': '你好 [CQ:at,qq=all]',
            **({'sub_type': 'invite', 'group_id': 30003} if kind == 'group' else {}), **extra}


def make_bot():
    async def action(name, **kwargs):
        if name == 'get_stranger_info':
            return {'nickname': '云玩家'}
        if name == 'get_group_info':
            return {'group_name': 'a队'}
        if name == 'send_private_msg':
            return {'message_id': 90000 + len([c for c in obj.call_action.await_args_list if c.args[0] == name])}
        return {}
    obj = SimpleNamespace(call_action=AsyncMock(side_effect=action))
    return obj


def action_calls(bot, name):
    return [c for c in bot.call_action.await_args_list if c.args[0] == name]


def receive(inbox, bot, kind='friend', flag='opaque flag:原样', scope=SCOPE):
    asyncio.run(inbox.receive(request_payload(raw_request(kind, flag), '12345'), scope, bot, ADMIN))
    return next(t for t, r in inbox.records.items() if r['flag'] == flag)


@pytest.mark.parametrize('text,expected', [('同意', (True, None)), ('拒绝', (False, None)),
    ('/同意 abcdef01', (True, 'ABCDEF01')), ('拒绝 ABCDEF01', (False, 'ABCDEF01')),
    ('我同意', None), ('同意 明天', None), ('同意 ABCDEF01 请继续', None)])
def test_decision_must_be_an_explicit_short_reply(text, expected):
    assert parse_decision(text) == expected


@pytest.mark.parametrize('patch', [
    {'post_type': 'notice'}, {'self_id': 99999}, {'request_type': 'unknown'},
    {'flag': ''}, {'flag': 123}, {'user_id': 0},
])
def test_invalid_or_unrelated_requests_are_ignored(patch):
    assert request_payload(raw_request(**patch), '12345') is None


@pytest.mark.parametrize('extra', [{'sub_type': 'add'}, {'invited_id': 99999}, {'group_id': 0}])
def test_only_invitations_to_this_bot_are_reviewed(extra):
    assert request_payload(raw_request('group', **extra), '12345') is None


@pytest.mark.parametrize('kind', ['friend', 'group'])
def test_notice_contains_qq_info_but_no_opaque_flag(tmp_path, kind):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot, kind)
    sent = action_calls(bot, 'send_private_msg')[0].kwargs
    assert sent['user_id'] == int(ADMIN) and sent['self_id'] == 12345
    assert sent['message'][0]['type'] == 'text'
    text = sent['message'][0]['data']['text']
    for expected in [token, '20002', '12345', '云玩家', '同意', '拒绝', '[CQ:at,qq=all]']:
        assert expected in text
    assert 'opaque flag' not in text
    if kind == 'group':
        assert '30003' in text and 'a队' in text
    assert not action_calls(bot, 'set_friend_add_request') and not action_calls(bot, 'set_group_add_request')


@pytest.mark.parametrize('kind,approve', [('friend', True), ('friend', False), ('group', True), ('group', False)])
def test_reply_approves_or_rejects_exact_original_request(tmp_path, kind, approve):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot, kind)
    reply = inbox.records[token]['notification_id']
    result = asyncio.run(inbox.decide(SCOPE, bot, ADMIN, approve, reply_id=reply))
    assert ('已同意' if approve else '已拒绝') in result
    calls = action_calls(bot, 'set_friend_add_request' if kind == 'friend' else 'set_group_add_request')
    expected = {'flag': 'opaque flag:原样', 'approve': approve, 'self_id': 12345}
    if kind == 'group':
        expected['sub_type'] = 'invite'
    assert len(calls) == 1 and calls[0].kwargs == expected
    again = asyncio.run(inbox.decide(SCOPE, bot, ADMIN, not approve, token=token))
    assert '已经处理' in again
    assert len(action_calls(bot, calls[0].args[0])) == 1


def test_duplicate_events_and_concurrent_reviews_do_not_repeat_actions(tmp_path):
    async def scenario():
        inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
        payload = request_payload(raw_request(), '12345')
        await asyncio.gather(*(inbox.receive(payload, SCOPE, bot, ADMIN) for _ in range(3)))
        assert len(inbox.records) == 1 and len(action_calls(bot, 'send_private_msg')) == 1
        token = next(iter(inbox.records))
        await asyncio.gather(*(inbox.decide(SCOPE, bot, ADMIN, True, token=token) for _ in range(3)))
        assert len(action_calls(bot, 'set_friend_add_request')) == 1
    asyncio.run(scenario())


def test_restart_preserves_notification_mapping_and_handled_status(tmp_path):
    path, bot = tmp_path / 'requests.json', make_bot()
    inbox = ReviewInbox(path)
    token = receive(inbox, bot)
    reply = inbox.records[token]['notification_id']
    restarted = ReviewInbox(path)
    assert '已拒绝' in asyncio.run(restarted.decide(SCOPE, bot, ADMIN, False, reply_id=reply))
    assert '已经处理' in asyncio.run(ReviewInbox(path).decide(SCOPE, bot, ADMIN, True, token=token))
    assert len(action_calls(bot, 'set_friend_add_request')) == 1


def test_multiple_requests_require_a_quote_or_identifier(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    first = receive(inbox, bot, flag='first')
    second = receive(inbox, bot, 'group', flag='second')
    assert '多条申请' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True))
    assert not action_calls(bot, 'set_friend_add_request')
    assert '已同意' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=second))
    assert inbox.records[first]['status'] == 'pending'
    assert action_calls(bot, 'set_group_add_request')[0].kwargs['flag'] == 'second'


def test_single_request_can_be_handled_without_quote(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    receive(inbox, bot)
    assert '已同意' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True))


def test_unknown_quote_or_foreign_bot_never_falls_back_to_single_request(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot)
    assert '无法识别引用' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, reply_id='forged'))
    for foreign in [('onebot-b', '12345'), ('onebot-a', '54321')]:
        assert '找不到' in asyncio.run(inbox.decide(foreign, bot, ADMIN, True, token=token))
    assert not action_calls(bot, 'set_friend_add_request')


def test_identifier_must_match_quoted_notice(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    first = receive(inbox, bot, flag='first')
    second = receive(inbox, bot, flag='second')
    result = asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=second,
                                     reply_id=inbox.records[first]['notification_id']))
    assert '不一致' in result and not action_calls(bot, 'set_friend_add_request')


def test_platform_rejection_stays_pending_and_can_retry(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot)
    bot.call_action.side_effect = None
    bot.call_action.return_value = {'status': 'failed', 'retcode': 100}
    assert '处理失败' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
    assert inbox.records[token]['status'] == 'pending'
    bot.call_action.return_value = {'status': 'ok', 'retcode': 0, 'data': {}}
    assert '已同意' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=token))


def test_timeout_or_crash_requires_verification_instead_of_repeating_action(tmp_path):
    path, bot = tmp_path / 'requests.json', make_bot()
    inbox = ReviewInbox(path)
    token = receive(inbox, bot)
    bot.call_action.side_effect = TimeoutError()
    assert '核实' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
    bot.call_action.reset_mock()
    assert '核实' in asyncio.run(ReviewInbox(path).decide(SCOPE, bot, ADMIN, True, token=token))
    bot.call_action.assert_not_awaited()
    inbox._update(token, status='processing')
    assert ReviewInbox(path).records[token]['status'] == 'uncertain'


def test_notification_failure_keeps_request_for_list_and_resend(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    original = bot.call_action.side_effect
    async def fail_send(name, **kwargs):
        if name == 'send_private_msg':
            raise ValueError('send failed')
        return await original(name, **kwargs)
    bot.call_action.side_effect = fail_send
    token = receive(inbox, bot)
    assert token in asyncio.run(inbox.list_requests(SCOPE))
    assert inbox.records[token]['notified_to'] == ''
    bot.call_action.side_effect = original
    receive(inbox, bot)
    assert inbox.records[token]['notified_to'] == ADMIN


def test_persistence_failure_does_not_submit_an_approval(tmp_path, monkeypatch):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot)
    monkeypatch.setattr(inbox, '_commit', lambda records: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
    assert inbox.records[token]['status'] == 'pending'
    assert not action_calls(bot, 'set_friend_add_request')


def test_corrupt_or_unknown_store_is_not_overwritten(tmp_path):
    path = tmp_path / 'requests.json'
    for content in ['{bad', '[]', json.dumps({'version': 99, 'requests': {}})]:
        path.write_text(content)
        with pytest.raises(ValueError):
            ReviewInbox(path)
        assert path.read_text() == content


def test_optional_profile_lookup_failure_still_sends_qq_notice(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    original = bot.call_action.side_effect
    async def no_profile(name, **kwargs):
        if name in ('get_stranger_info', 'get_group_info'):
            raise ValueError('not available')
        return await original(name, **kwargs)
    bot.call_action.side_effect = no_profile
    receive(inbox, bot, 'group')
    text = action_calls(bot, 'send_private_msg')[0].kwargs['message'][0]['data']['text']
    assert '20002' in text and '30003' in text


def test_failure_to_save_success_does_not_allow_a_second_action(tmp_path, monkeypatch):
    path, bot = tmp_path / 'requests.json', make_bot()
    inbox = ReviewInbox(path)
    token = receive(inbox, bot)
    original = inbox._commit
    def fail_after_processing(records):
        if records[token]['status'] == 'approved':
            raise OSError('disk full')
        original(records)
    monkeypatch.setattr(inbox, '_commit', fail_after_processing)
    assert 'QQ 已处理' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
    assert '正在处理' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
    assert len(action_calls(bot, 'set_friend_add_request')) == 1
    assert ReviewInbox(path).records[token]['status'] == 'uncertain'


def test_cancelled_approval_is_not_retried_as_a_pending_request(tmp_path):
    async def scenario():
        inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
        await inbox.receive(request_payload(raw_request(), '12345'), SCOPE, bot, ADMIN)
        token = next(iter(inbox.records))
        started = asyncio.Event()
        async def pending_api(name, **kwargs):
            started.set()
            await asyncio.Future()
        bot.call_action.side_effect = pending_api
        task = asyncio.create_task(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert inbox.records[token]['status'] == 'uncertain'
        count = bot.call_action.await_count
        assert '核实' in await inbox.decide(SCOPE, bot, ADMIN, True, token=token)
        assert bot.call_action.await_count == count
    asyncio.run(scenario())


@pytest.mark.parametrize('approve', [True, False])
@pytest.mark.parametrize('legacy', [True, False])
def test_changed_quote_id_resolves_exact_bot_notice_after_restart(tmp_path, approve, legacy):
    path, bot = tmp_path / 'requests.json', make_bot()
    inbox = ReviewInbox(path)
    first = receive(inbox, bot, 'group', flag='slreq:1:765:30003:2:0')
    other = receive(inbox, bot, 'group', flag='slreq:1:766:30003:2:0')
    notice = action_calls(bot, 'send_private_msg')[0].kwargs['message']
    if legacy:
        inbox.records[first].pop('notification_text')
        inbox._commit(inbox.records)
    inbox = ReviewInbox(path)
    original = bot.call_action.side_effect
    async def resolve_quote(name, **kwargs):
        if name == 'get_msg':
            assert kwargs['message_id'] == -456 and kwargs['self_id'] == 12345
            return {'status': 'ok', 'retcode': 0, 'data': {
                'message_id': -456, 'sender': {'user_id': 12345},
                'message_type': 'private', 'target_id': int(ADMIN), 'message': notice}}
        return await original(name, **kwargs)
    bot.call_action.side_effect = resolve_quote
    result = asyncio.run(inbox.decide(SCOPE, bot, ADMIN, approve, reply_id='-456'))
    assert ('已同意' if approve else '已拒绝') in result
    assert inbox.records[first]['status'] == ('approved' if approve else 'rejected')
    assert inbox.records[other]['status'] == 'pending'
    assert action_calls(bot, 'set_group_add_request')[0].kwargs['flag'] == 'slreq:1:765:30003:2:0'


def test_framework_quote_body_can_resolve_without_get_msg(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot, 'group')
    message = {'sender_id': SCOPE[1], 'message_str': inbox.records[token]['notification_text']}
    result = asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, reply_id='123', reply_message=message))
    assert '已同意' in result
    assert not action_calls(bot, 'get_msg')


@pytest.mark.parametrize('patch', [
    {'sender': {'user_id': int(ADMIN)}}, {'target_id': 99999}, {'message_type': 'group'},
    {'message': [{'type': 'text', 'data': {'text': '📩 入群邀请 FAKE1234'}}]},
])
def test_quote_fallback_rejects_wrong_sender_recipient_group_or_content(tmp_path, patch):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot, 'group')
    notice = action_calls(bot, 'send_private_msg')[0].kwargs['message']
    bot.call_action.side_effect = None
    bot.call_action.return_value = {'sender': {'user_id': 12345}, 'target_id': int(ADMIN),
                                    'message_type': 'private', 'message': notice, **patch}
    result = asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, reply_id='123'))
    assert '无法识别引用' in result
    assert inbox.records[token]['status'] == 'pending'
    assert not action_calls(bot, 'set_group_add_request')


@pytest.mark.parametrize('wrapped', [True, False])
def test_platform_error_detail_is_visible_without_leaking_request_flag(tmp_path, wrapped):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    flag = 'slreq:1:765:30003:2:0'
    token = receive(inbox, bot, 'group', flag)
    result = {'status': 'failed', 'retcode': 1200,
              'wording': f'group request {flag} not found'}
    bot.call_action.side_effect = None
    if wrapped:
        bot.call_action.return_value = result
    else:
        class PlatformError(Exception):
            def __init__(self):
                self.result = result
        bot.call_action.side_effect = PlatformError()
    message = asyncio.run(inbox.decide(SCOPE, bot, ADMIN, False, token=token))
    assert '处理失败' in message and 'not found' in message and flag not in message
    assert inbox.records[token]['status'] == 'pending'


def test_real_aiocqhttp_group_action_response_and_network_timeout(tmp_path):
    # 使用 aiocqhttp 的真实响应解包和异常；不建立 QQ 连接。
    cq = pytest.importorskip('aiocqhttp')
    impl = pytest.importorskip('aiocqhttp.api_impl')
    errors = pytest.importorskip('aiocqhttp.exceptions')
    bot = cq.CQHttp(__name__)
    calls = []
    async def action(action, **params):
        calls.append((action, params))
        if action == 'send_private_msg':
            return impl._handle_api_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': 456}})
        if action.startswith('set_'):
            if params['approve']:
                return impl._handle_api_result({'status': 'ok', 'retcode': 0, 'data': None})
            raise errors.NetworkError('WebSocket API call timeout')
        return {}
    bot._api = SimpleNamespace(call_action=action)
    inbox = ReviewInbox(tmp_path / 'requests.json')
    first = receive(inbox, bot, 'group', 'slreq:1:765:30003:2:0')
    assert '已同意' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=first))
    second = receive(inbox, bot, 'group', 'slreq:1:766:30003:2:0')
    assert '核实' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, False, token=second))
    assert inbox.records[second]['status'] == 'uncertain'
    count = len(calls)
    asyncio.run(inbox.decide(SCOPE, bot, ADMIN, False, token=second))
    assert len(calls) == count
