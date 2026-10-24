"""OneBot 申请审批：转发、持久化、精确选单及重复/失败处理。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from request_review import ReviewInbox, invite_card_payload, parse_decision, request_payload

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
    assert ('已被同意' if approve else '已被拒绝') in again and '无法再' in again
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
    assert '已被拒绝' in asyncio.run(ReviewInbox(path).decide(SCOPE, bot, ADMIN, True, token=token))
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


@pytest.mark.parametrize('kind', ['friend', 'group'])
def test_failure_to_save_success_does_not_allow_a_second_action(tmp_path, monkeypatch, kind):
    path, bot = tmp_path / 'requests.json', make_bot()
    inbox = ReviewInbox(path)
    token = receive(inbox, bot, kind)
    original = inbox._commit
    def fail_after_processing(records):
        if records[token]['status'] == 'approved':
            raise OSError('disk full')
        original(records)
    monkeypatch.setattr(inbox, '_commit', fail_after_processing)
    assert 'QQ 已处理' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
    assert '正在处理' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
    assert len(action_calls(bot, 'set_friend_add_request' if kind == 'friend' else 'set_group_add_request')) == 1
    assert len(action_calls(bot, 'send_private_msg')) == 1
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


@pytest.mark.parametrize('approve', [True, False])
@pytest.mark.parametrize('wrapped', [True, False])
def test_qq_already_approved_is_saved_as_approved_even_when_user_rejects(tmp_path, approve, wrapped):
    path, bot = tmp_path / 'requests.json', make_bot()
    inbox = ReviewInbox(path)
    token = receive(inbox, bot, 'group')
    failure = {'status': 'failed', 'retcode': 100, 'data': None,
               'wording': 'OIDB error 120162004 on 0x10c8_1: already agree msg by self'}
    original = bot.call_action.side_effect
    if not wrapped:
        errors = pytest.importorskip('aiocqhttp.exceptions')
    async def action(name, **params):
        if name == 'set_group_add_request':
            if wrapped:
                return failure
            raise errors.ActionFailed(failure)
        return await original(name, **params)
    bot.call_action.side_effect = action
    result = asyncio.run(inbox.decide(SCOPE, bot, ADMIN, approve, token=token))
    assert '已被同意' in result and '处理失败' not in result and '已拒绝' not in result
    assert ('无法再拒绝' in result) == (not approve)
    assert inbox.records[token]['status'] == 'approved'
    notice = action_calls(bot, 'send_private_msg')[-1].kwargs
    assert notice['user_id'] == 20002 and notice['self_id'] == 12345
    assert notice['message'] == [{'type': 'text', 'data': {'text': '你的入群邀请已被同意喵。\n群号：30003'}}]
    restarted = ReviewInbox(path)
    assert token not in asyncio.run(restarted.list_requests(SCOPE))
    count = bot.call_action.await_count
    asyncio.run(restarted.decide(SCOPE, bot, ADMIN, False, token=token))
    assert bot.call_action.await_count == count


@pytest.mark.parametrize('kind,wording', [
    ('group', 'OIDB error 120161001 on 0x10c8_1: handle async message fail'),
    ('group', 'OIDB error 120162004 on 0x10c8_1: request invalid'),
    ('group', 'OIDB error 120162004 on 0x9999_1: already agree msg by self'),
    ('group', 'unknown flag: OIDB error 120162004 on 0x10c8_1: already agree msg by self'),
    ('friend', 'OIDB error 120162004 on 0x10c8_1: already agree msg by self'),
])
def test_other_errors_never_mark_a_request_approved(tmp_path, kind, wording):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot, kind)
    bot.call_action.side_effect = None
    bot.call_action.return_value = {'status': 'failed', 'retcode': 100, 'wording': wording}
    assert '处理失败' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
    assert inbox.records[token]['status'] == 'pending'


@pytest.mark.parametrize('app', ['com.tencent.qun.invite', 'com.tencent.tuwen.lua'])
def test_native_card_uses_approval_sequence_not_message_id(qq_invite_card, app):
    payload = invite_card_payload(qq_invite_card(app=app, message_id=99999), SCOPE[1])
    assert payload == {'kind': 'group', 'user_id': '20002', 'group_id': '30003',
                       'flag': 'slreq:1:765:30003:2:0', 'comment': '',
                       'source': 'snowluma_invite_card', 'request_time': 100}


@pytest.mark.parametrize('patch', [
    {'post_type': 'message_sent'}, {'message_type': 'group'}, {'sub_type': 'group'},
    {'group_id': 30003}, {'self_id': 99999}, {'user_id': 12345}, {'user_id': 0},
    {'time': 0}, {'time': True}, {'message': []}, {'message': '[CQ:json,data=fake]'},
])
def test_only_live_private_invite_cards_are_parsed(qq_invite_card, patch):
    assert invite_card_payload(qq_invite_card(**patch), SCOPE[1]) is None


@pytest.mark.parametrize('json_text', ['{bad', 'null', '[]', '123',
    '{"app":"com.tencent.qun.invite"}', '{"app":"other","meta":{"news":{}}}',
    '{"app":"com.tencent.qun.invite","meta":null}', '{"app":"com.tencent.qun.invite","meta":{"news":[]}}'])
def test_malformed_and_unrelated_cards_are_ignored(qq_invite_card, json_text):
    assert invite_card_payload(qq_invite_card(json_text=json_text), SCOPE[1]) is None


@pytest.mark.parametrize('group,sequence', [('0', '1'), ('1', '-1'), ('1', '1.2'),
    ('1', '9007199254740992'), ('1', '1&msgseq=2'), ('1', ''), ('bad', '1')])
def test_invite_tuple_must_be_unambiguous_positive_safe_integers(qq_invite_card, group, sequence):
    assert invite_card_payload(qq_invite_card(group=group, sequence=sequence), SCOPE[1]) is None


def test_new_card_replaces_stale_legacy_request_without_reusing_its_flag(tmp_path, qq_invite_card):
    path, bot = tmp_path / 'requests.json', make_bot()
    inbox = ReviewInbox(path)
    old = receive(inbox, bot, 'group', 'invite:30003:u_example')
    friend = receive(inbox, bot, 'friend', 'unrelated')
    payload = invite_card_payload(qq_invite_card(), SCOPE[1])
    asyncio.run(inbox.receive(payload, SCOPE, bot, ADMIN))
    current = next(t for t, r in inbox.records.items() if r['flag'] == payload['flag'])
    assert inbox.records[old]['status'] == 'superseded'
    assert inbox.records[old]['flag'] == 'invite:30003:u_example'
    assert inbox.records[friend]['status'] == 'pending'
    restarted = ReviewInbox(path)
    listing = asyncio.run(restarted.list_requests(SCOPE))
    assert current in listing and friend in listing and old not in listing
    assert '最新通知' in asyncio.run(restarted.decide(SCOPE, bot, ADMIN, True, token=old))
    assert not action_calls(bot, 'set_group_add_request')
    assert '已同意' in asyncio.run(restarted.decide(SCOPE, bot, ADMIN, True, token=current))
    assert action_calls(bot, 'set_group_add_request')[0].kwargs['flag'] == payload['flag']


def test_same_group_new_sequence_notifies_once_and_delayed_duplicates_keep_latest(tmp_path, qq_invite_card):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    first = invite_card_payload(qq_invite_card(), SCOPE[1])
    second = invite_card_payload(qq_invite_card(sequence=766), SCOPE[1])
    async def scenario():
        await inbox.receive(first, SCOPE, bot, ADMIN)
        await inbox.receive(second, SCOPE, bot, ADMIN)
        await asyncio.gather(inbox.receive(first, SCOPE, bot, ADMIN), inbox.receive(second, SCOPE, bot, ADMIN))
    asyncio.run(scenario())
    assert len(action_calls(bot, 'send_private_msg')) == 2
    latest = next(t for t, r in inbox.records.items() if r['flag'] == second['flag'])
    assert inbox.records[latest]['status'] == 'pending'
    assert len([r for r in inbox.records.values() if r['status'] == 'pending']) == 1


def test_older_card_cannot_supersede_a_newer_card_or_request(tmp_path, qq_invite_card):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    latest = invite_card_payload(qq_invite_card(sequence=766, when=102), SCOPE[1])
    asyncio.run(inbox.receive(latest, SCOPE, bot, ADMIN))
    asyncio.run(inbox.receive(invite_card_payload(qq_invite_card(when=101), SCOPE[1]), SCOPE, bot, ADMIN))
    assert len(inbox.records) == 1 and next(iter(inbox.records.values()))['status'] == 'pending'
    assert len(action_calls(bot, 'send_private_msg')) == 1


def test_card_and_backend_request_with_same_handle_do_not_duplicate_notices(tmp_path, qq_invite_card):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    card = invite_card_payload(qq_invite_card(), SCOPE[1])
    asyncio.run(inbox.receive(card, SCOPE, bot, ADMIN))
    asyncio.run(inbox.receive(request_payload(raw_request('group', card['flag'], time=100), SCOPE[1]), SCOPE, bot, ADMIN))
    asyncio.run(inbox.receive(request_payload(raw_request('group', 'invite:30003:u_example', time=100), SCOPE[1]), SCOPE, bot, ADMIN))
    assert len(inbox.records) == 1 and len(action_calls(bot, 'send_private_msg')) == 1


def test_card_refresh_is_isolated_from_other_bots_groups_and_inviters(tmp_path, qq_invite_card):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    for scope, group, actor in [(('other', '12345'), 30003, 20002), (SCOPE, 30004, 20002), (SCOPE, 30003, 20003)]:
        payload = request_payload(raw_request('group', f'flag:{group}:{actor}:{scope[0]}', group_id=group, user_id=actor), SCOPE[1])
        asyncio.run(inbox.receive(payload, scope, bot, ADMIN))
    asyncio.run(inbox.receive(invite_card_payload(qq_invite_card(), SCOPE[1]), SCOPE, bot, ADMIN))
    assert all(r['status'] == 'pending' for r in inbox.records.values())


@pytest.mark.parametrize('jump', [
    'https://group/invite_join?groupcode=30003&msgseq=765',
    'mqqapi://other/invite_join?groupcode=30003&msgseq=765',
    'mqqapi://group/other?groupcode=30003&msgseq=765',
])
def test_ordinary_share_links_do_not_trigger_request_forwarding(qq_invite_card, jump):
    text = json.dumps({'app': 'com.tencent.qun.invite', 'meta': {'news': {'jumpUrl': jump}}})
    assert invite_card_payload(qq_invite_card(json_text=text), SCOPE[1]) is None


def test_first_native_card_refreshes_an_existing_pending_notice_only_once(tmp_path, qq_invite_card):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    payload = invite_card_payload(qq_invite_card(), SCOPE[1])
    token = receive(inbox, bot, 'group', payload['flag'])
    asyncio.run(inbox.receive(payload, SCOPE, bot, ADMIN))
    asyncio.run(inbox.receive(payload, SCOPE, bot, ADMIN))
    assert len(inbox.records) == 1 and inbox.records[token]['source'] == 'snowluma_invite_card'
    assert len(action_calls(bot, 'send_private_msg')) == 2


@pytest.mark.parametrize('status', ['approved', 'uncertain'])
def test_reinvite_preserves_previously_confirmed_or_unknown_outcomes(tmp_path, qq_invite_card, status):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    old = receive(inbox, bot, 'group', 'older')
    inbox._update(old, status=status)
    asyncio.run(inbox.receive(invite_card_payload(qq_invite_card(), SCOPE[1]), SCOPE, bot, ADMIN))
    assert inbox.records[old]['status'] == status
    assert len([r for r in inbox.records.values() if r['status'] == 'pending']) == 1


def test_already_approved_save_failure_cannot_repeat_the_platform_action(tmp_path, monkeypatch):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot, 'group')
    bot.call_action.side_effect = None
    bot.call_action.return_value = {'status': 'failed', 'retcode': 100,
        'wording': 'OIDB error 120162004 on 0x10c8_1: already agree msg by self'}
    original = inbox._commit
    def fail_save(records):
        if records[token]['status'] == 'approved':
            raise OSError('disk full')
        original(records)
    monkeypatch.setattr(inbox, '_commit', fail_save)
    assert '记录保存失败' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, False, token=token))
    assert '正在处理' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, False, token=token))
    assert len(action_calls(bot, 'set_group_add_request')) == 1


def test_slow_profile_lookup_cannot_send_an_obsolete_notice_after_the_new_card(tmp_path, qq_invite_card):
    async def scenario():
        inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
        waiting, release = asyncio.Event(), asyncio.Event()
        original = bot.call_action.side_effect
        async def slow_first_lookup(name, **params):
            if name == 'get_stranger_info' and not waiting.is_set():
                waiting.set()
                await release.wait()
            return await original(name, **params)
        bot.call_action.side_effect = slow_first_lookup
        old = asyncio.create_task(inbox.receive(request_payload(raw_request('group', 'invite:30003:u_example'), SCOPE[1]), SCOPE, bot, ADMIN))
        await waiting.wait()
        card = invite_card_payload(qq_invite_card(), SCOPE[1])
        await inbox.receive(card, SCOPE, bot, ADMIN)
        release.set()
        await old
        token = next(t for t, r in inbox.records.items() if r['flag'] == card['flag'])
        notices = action_calls(bot, 'send_private_msg')
        assert len(notices) == 1 and token in notices[0].kwargs['message'][0]['data']['text']
    asyncio.run(scenario())


def history_bot(messages):
    bot = make_bot()
    original = bot.call_action.side_effect
    async def action(name, **params):
        if name == 'get_version_info':
            return {'app_name': 'SnowLuma', 'app_version': '1.14.11-node'}
        if name == 'get_friend_msg_history':
            return {'messages': messages}
        return await original(name, **params)
    bot.call_action.side_effect = action
    return bot


@pytest.mark.parametrize('approve', [True, False])
def test_refresh_restores_real_card_for_old_type_one_request_then_requires_new_approval(tmp_path, qq_invite_card, approve):
    path = tmp_path / 'requests.json'
    # SnowLuma 的真实历史接口会省去 post_type 和 self_id。
    card = {k: v for k, v in qq_invite_card().items() if k not in ('post_type', 'self_id')}
    inbox, bot = ReviewInbox(path), history_bot([card])
    old_flag = 'slreq:1:1790000000000010:30003:1:0'
    old = receive(inbox, bot, 'group', old_flag)
    inbox = ReviewInbox(path)  # 旧版记录没有 source/request_time，重启后仍可恢复。
    assert '已刷新 1 条' in asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN, token=old))
    new = next(t for t, r in inbox.records.items() if r.get('source') == 'snowluma_invite_card')
    assert old != new and inbox.records[old]['flag'] == old_flag
    assert inbox.records[old]['status'] == 'superseded'
    assert inbox.records[new]['flag'] == 'slreq:1:765:30003:2:0'
    assert inbox.records[new]['status'] == 'pending'
    assert not action_calls(bot, 'set_group_add_request')
    history = action_calls(bot, 'get_friend_msg_history')[0]
    assert history.kwargs == {'self_id': 12345, 'user_id': 20002, 'message_id': 0, 'count': 100}
    assert '最新通知' in asyncio.run(inbox.decide(SCOPE, bot, ADMIN, approve, token=old))
    inbox = ReviewInbox(path)
    assert ('已同意' if approve else '已拒绝') in asyncio.run(inbox.decide(
        SCOPE, bot, ADMIN, approve, reply_id=inbox.records[new]['notification_id']))
    calls = action_calls(bot, 'set_group_add_request')
    assert len(calls) == 1 and calls[0].kwargs['flag'] == 'slreq:1:765:30003:2:0'


def test_refresh_only_uses_latest_card_from_same_bot_sender_and_pending_group(tmp_path, qq_invite_card):
    messages = [qq_invite_card(sequence=700, when=90), qq_invite_card(sequence=766, when=101),
                qq_invite_card(sequence=765, when=100), qq_invite_card(group=30004, when=102),
                qq_invite_card(user_id=20003, when=103), qq_invite_card(self_id=99999, when=104),
                qq_invite_card(post_type='message_sent', when=105),
                {k: v for k, v in qq_invite_card(user_id=12345, when=106).items()
                 if k not in ('post_type', 'self_id')},
                {'message': [{'type': 'node', 'data': {'content': qq_invite_card()}}]}]
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot(messages)
    receive(inbox, bot, 'group', 'old')
    assert '已刷新 1 条' in asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN))
    cards = [r for r in inbox.records.values() if r.get('source') == 'snowluma_invite_card']
    assert len(cards) == 1 and cards[0]['flag'] == 'slreq:1:766:30003:2:0'
    assert len(action_calls(bot, 'send_private_msg')) == 2
    assert not action_calls(bot, 'set_group_add_request')


@pytest.mark.parametrize('newer', [False, True])
def test_refresh_does_not_guess_between_distinct_cards_with_equal_timestamps(tmp_path, qq_invite_card, newer):
    cards = [qq_invite_card(sequence=765), qq_invite_card(sequence=766)]
    if newer:
        cards.append(qq_invite_card(sequence=767, when=101))
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot(cards)
    receive(inbox, bot, 'group', 'old')
    response = asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN))
    assert ('已刷新' in response) == newer
    restored = [r for r in inbox.records.values() if r.get('source') == 'snowluma_invite_card']
    assert len(restored) == int(newer)
    if newer:
        assert restored[0]['flag'] == 'slreq:1:767:30003:2:0'


@pytest.mark.parametrize('scope', [SCOPE, ('other', '12345'), ('onebot-a', '99999')])
def test_refresh_exact_token_never_scans_unrelated_requests(tmp_path, qq_invite_card, scope):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot([qq_invite_card()])
    first = receive(inbox, bot, 'group', 'first')
    second_payload = request_payload(raw_request('group', 'second', user_id=20003), SCOPE[1])
    asyncio.run(inbox.receive(second_payload, SCOPE, bot, ADMIN))
    asyncio.run(inbox.refresh_requests(scope, bot, ADMIN, token=first))
    calls = action_calls(bot, 'get_friend_msg_history')
    assert [c.kwargs['user_id'] for c in calls] == ([20002] if scope == SCOPE else [])


@pytest.mark.parametrize('status', ['approved', 'rejected', 'uncertain', 'processing', 'superseded'])
def test_refresh_leaves_handled_or_unknown_outcomes_unchanged(tmp_path, qq_invite_card, status):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot([qq_invite_card()])
    old = receive(inbox, bot, 'group', 'old')
    inbox._update(old, status=status)
    bot.call_action.reset_mock()
    assert '没有待刷新' in asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN))
    bot.call_action.assert_not_awaited()
    assert inbox.records[old]['status'] == status and len(inbox.records) == 1


def test_refresh_never_converts_friend_requests_or_uses_another_backend(tmp_path, qq_invite_card):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot([qq_invite_card()])
    receive(inbox, bot)
    bot.call_action.reset_mock()
    assert '没有待刷新' in asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN))
    bot.call_action.assert_not_awaited()
    old = receive(inbox, bot, 'group', 'old')
    bot.call_action.side_effect = None
    bot.call_action.return_value = {'app_name': 'Other'}
    assert '不支持' in asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN))
    assert not action_calls(bot, 'get_friend_msg_history')
    assert inbox.records[old]['status'] == 'pending'


@pytest.mark.parametrize('response', [{}, {'messages': None}, {'messages': 'bad'},
    {'status': 'failed', 'retcode': 100}, TimeoutError()])
def test_refresh_read_failure_keeps_original_request_and_never_approves(tmp_path, response):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot([])
    old = receive(inbox, bot, 'group', 'old')
    original = bot.call_action.side_effect
    async def action(name, **params):
        if name == 'get_friend_msg_history':
            if isinstance(response, Exception):
                raise response
            return response
        return await original(name, **params)
    bot.call_action.side_effect = action
    assert '刷新邀请失败' in asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN))
    assert inbox.records[old]['status'] == 'pending' and len(inbox.records) == 1
    assert not action_calls(bot, 'set_group_add_request')


def test_refresh_resends_existing_pending_card_but_does_not_duplicate_records(tmp_path, qq_invite_card):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot([qq_invite_card()])
    asyncio.run(inbox.receive(invite_card_payload(qq_invite_card(), SCOPE[1]), SCOPE, bot, ADMIN))
    assert '已刷新 1 条' in asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN))
    assert len(inbox.records) == 1 and len(action_calls(bot, 'send_private_msg')) == 2
    assert not action_calls(bot, 'set_group_add_request')


@pytest.mark.parametrize('status', ['approved', 'rejected', 'uncertain', 'processing', 'superseded'])
def test_refresh_cannot_replace_pending_record_with_card_already_having_an_outcome(tmp_path, qq_invite_card, status):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot([qq_invite_card()])
    asyncio.run(inbox.receive(invite_card_payload(qq_invite_card(), SCOPE[1]), SCOPE, bot, ADMIN))
    card = next(iter(inbox.records))
    inbox._update(card, status=status)
    old = receive(inbox, bot, 'group', 'old')
    count = len(action_calls(bot, 'send_private_msg'))
    asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN, token=old))
    assert len(inbox.records) == 2 and inbox.records[old]['status'] == 'pending'
    assert inbox.records[card]['status'] == status
    assert len(action_calls(bot, 'send_private_msg')) == count
    assert not action_calls(bot, 'set_group_add_request')


def test_refresh_reports_missing_card_without_inventing_a_flag(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot([])
    old = receive(inbox, bot, 'group', 'slreq:1:123:30003:1:0')
    assert '未找到' in asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN))
    assert len(inbox.records) == 1 and inbox.records[old]['status'] == 'pending'


def test_request_handled_during_history_lookup_cannot_be_restored(tmp_path, qq_invite_card):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot([qq_invite_card()])
    old = receive(inbox, bot, 'group', 'old')
    original = bot.call_action.side_effect
    async def action(name, **params):
        if name == 'get_friend_msg_history':
            inbox._update(old, status='approved')
        return await original(name, **params)
    bot.call_action.side_effect = action
    asyncio.run(inbox.refresh_requests(SCOPE, bot, ADMIN))
    assert len(inbox.records) == 1 and inbox.records[old]['status'] == 'approved'
    assert len(action_calls(bot, 'send_private_msg')) == 1


def test_concurrent_refresh_shares_guard_and_notification_failure_remains_recoverable(tmp_path, qq_invite_card):
    async def scenario():
        inbox, bot = ReviewInbox(tmp_path / 'requests.json'), history_bot([qq_invite_card()])
        receive_payload = request_payload(raw_request('group', 'old'), SCOPE[1])
        await inbox.receive(receive_payload, SCOPE, bot, ADMIN)
        waiting, release = asyncio.Event(), asyncio.Event()
        original = bot.call_action.side_effect
        async def action(name, **params):
            if name == 'get_friend_msg_history':
                waiting.set()
                await release.wait()
            if name == 'send_private_msg':
                raise OSError('send unavailable')
            return await original(name, **params)
        bot.call_action.side_effect = action
        refresh = asyncio.create_task(inbox.refresh_requests(SCOPE, bot, ADMIN))
        await waiting.wait()
        assert '正在刷新' in await inbox.refresh_requests(SCOPE, bot, ADMIN)
        release.set()
        assert '刷新邀请失败' in await refresh
        assert len(action_calls(bot, 'get_friend_msg_history')) == 1
        card = next(r for r in inbox.records.values() if r.get('source') == 'snowluma_invite_card')
        assert card['status'] == 'pending' and card['notified_to'] == ''
        assert '待处理' in await inbox.list_requests(SCOPE)
        assert not action_calls(bot, 'set_group_add_request')
    asyncio.run(scenario())


def result_notices(bot):
    return [call for call in action_calls(bot, 'send_private_msg')
            if call.kwargs['message'][0]['data']['text'].startswith('你的入群邀请')]


@pytest.mark.parametrize('approve', [True, False])
@pytest.mark.parametrize('native_card', [True, False])
def test_inviter_receives_result_only_after_confirmed_outcome_is_saved(tmp_path, qq_invite_card, approve, native_card):
    path, bot = tmp_path / 'requests.json', make_bot()
    inbox = ReviewInbox(path)
    payload = (invite_card_payload(qq_invite_card(), SCOPE[1]) if native_card else
               request_payload(raw_request('group'), SCOPE[1]))
    asyncio.run(inbox.receive(payload, SCOPE, bot, ADMIN))
    token = next(iter(inbox.records))
    outcome = 'approved' if approve else 'rejected'
    original = bot.call_action.side_effect
    order = []
    async def action(name, **params):
        stored = json.loads(path.read_text())['requests'][token]
        if name == 'set_group_add_request':
            assert stored['status'] == 'processing'
            order.append('approval')
        if name == 'send_private_msg':
            assert stored['status'] == outcome
            order.append('notice')
        return await original(name, **params)
    bot.call_action.side_effect = action
    result = asyncio.run(inbox.decide(SCOPE, bot, ADMIN, approve, token=token))
    assert ('已同意' if approve else '已拒绝') in result and '通知未确认' not in result
    assert order == ['approval', 'notice']
    notice = result_notices(bot)[0].kwargs
    assert notice == {'self_id': 12345, 'user_id': 20002, 'message': [
        {'type': 'text', 'data': {'text': f"你的入群邀请已被{'同意' if approve else '拒绝'}喵。\n群号：30003"}}]}
    assert payload['flag'] not in str(notice['message'])
    asyncio.run(inbox.decide(SCOPE, bot, ADMIN, not approve, token=token))
    asyncio.run(ReviewInbox(path).decide(SCOPE, bot, ADMIN, approve, token=token))
    assert len(action_calls(bot, 'set_group_add_request')) == 1 and len(result_notices(bot)) == 1


@pytest.mark.parametrize('approve', [True, False])
def test_friend_reviews_do_not_send_group_invite_results(tmp_path, approve):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot)
    asyncio.run(inbox.decide(SCOPE, bot, ADMIN, approve, token=token))
    assert not result_notices(bot) and len(action_calls(bot, 'send_private_msg')) == 1


@pytest.mark.parametrize('failure', [{'status': 'failed', 'retcode': 100}, TimeoutError(), OSError('disconnected')])
def test_failed_or_unknown_approval_does_not_notify_inviter_with_a_result(tmp_path, failure):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    token = receive(inbox, bot, 'group')
    original = bot.call_action.side_effect
    async def action(name, **params):
        if name == 'set_group_add_request':
            if isinstance(failure, Exception):
                raise failure
            return failure
        return await original(name, **params)
    bot.call_action.side_effect = action
    asyncio.run(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
    assert inbox.records[token]['status'] in ('pending', 'uncertain')
    assert not result_notices(bot) and len(action_calls(bot, 'send_private_msg')) == 1


@pytest.mark.parametrize('approve', [True, False])
@pytest.mark.parametrize('failure', [{'status': 'failed', 'retcode': 100},
    TimeoutError(), OSError('disconnected'), ValueError('unavailable')])
def test_failed_result_notice_preserves_approval_and_warns_admin_without_retrying(tmp_path, approve, failure):
    path, bot = tmp_path / 'requests.json', make_bot()
    inbox = ReviewInbox(path)
    token = receive(inbox, bot, 'group')
    original = bot.call_action.side_effect
    async def action(name, **params):
        if name == 'send_private_msg':
            if isinstance(failure, Exception):
                raise failure
            return failure
        return await original(name, **params)
    bot.call_action.side_effect = action
    result = asyncio.run(inbox.decide(SCOPE, bot, ADMIN, approve, token=token))
    assert ('已同意' if approve else '已拒绝') in result
    assert '邀请人通知未确认' in result
    restarted = ReviewInbox(path)
    assert restarted.records[token]['status'] == ('approved' if approve else 'rejected')
    count = bot.call_action.await_count
    asyncio.run(restarted.decide(SCOPE, bot, ADMIN, approve, token=token))
    assert bot.call_action.await_count == count


@pytest.mark.parametrize('approve', [True, False])
def test_concurrent_decisions_during_result_notice_cannot_repeat_action_or_notice(tmp_path, approve):
    async def scenario():
        inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
        await inbox.receive(request_payload(raw_request('group'), SCOPE[1]), SCOPE, bot, ADMIN)
        token = next(iter(inbox.records))
        waiting, release = asyncio.Event(), asyncio.Event()
        original = bot.call_action.side_effect
        async def action(name, **params):
            if name == 'send_private_msg':
                waiting.set()
                await release.wait()
            return await original(name, **params)
        bot.call_action.side_effect = action
        first = asyncio.create_task(inbox.decide(SCOPE, bot, ADMIN, approve, token=token))
        await waiting.wait()
        second = await inbox.decide(SCOPE, bot, ADMIN, not approve, token=token)
        assert ('已被同意' if approve else '已被拒绝') in second
        release.set()
        await first
        assert len(action_calls(bot, 'set_group_add_request')) == 1 and len(result_notices(bot)) == 1
    asyncio.run(scenario())


def test_cancelled_result_notice_keeps_confirmed_approval_after_restart(tmp_path):
    async def scenario():
        path, bot = tmp_path / 'requests.json', make_bot()
        inbox = ReviewInbox(path)
        await inbox.receive(request_payload(raw_request('group'), SCOPE[1]), SCOPE, bot, ADMIN)
        token = next(iter(inbox.records))
        waiting = asyncio.Event()
        original = bot.call_action.side_effect
        async def action(name, **params):
            if name == 'send_private_msg':
                waiting.set()
                await asyncio.Future()
            return await original(name, **params)
        bot.call_action.side_effect = action
        task = asyncio.create_task(inbox.decide(SCOPE, bot, ADMIN, True, token=token))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        restarted = ReviewInbox(path)
        assert restarted.records[token]['status'] == 'approved'
        count = bot.call_action.await_count
        assert '已被同意' in await restarted.decide(SCOPE, bot, ADMIN, True, token=token)
        assert bot.call_action.await_count == count
    asyncio.run(scenario())


def test_result_targets_selected_inviter_and_bot_even_with_other_pending_invites(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    other = receive(inbox, bot, 'group', 'other')
    scope = ('onebot-b', '99999')
    payload = request_payload(raw_request('group', 'selected', self_id=99999,
                                         user_id=20003, group_id=30004), scope[1])
    asyncio.run(inbox.receive(payload, scope, bot, ADMIN))
    selected = next(t for t, r in inbox.records.items() if r['flag'] == 'selected')
    asyncio.run(inbox.decide(scope, bot, ADMIN, False, token=selected))
    notice = result_notices(bot)[0].kwargs
    assert notice['self_id'] == 99999 and notice['user_id'] == 20003
    assert '30004' in str(notice['message']) and '30003' not in str(notice['message'])
    assert inbox.records[other]['status'] == 'pending'


def test_inviter_who_is_super_admin_also_gets_one_result_notice(tmp_path):
    inbox, bot = ReviewInbox(tmp_path / 'requests.json'), make_bot()
    asyncio.run(inbox.receive(request_payload(raw_request('group'), SCOPE[1]), SCOPE, bot, '20002'))
    token = next(iter(inbox.records))
    asyncio.run(inbox.decide(SCOPE, bot, '20002', True, token=token))
    assert len(result_notices(bot)) == 1
    assert result_notices(bot)[0].kwargs['user_id'] == 20002
