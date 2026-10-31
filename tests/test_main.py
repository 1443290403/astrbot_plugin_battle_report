"""消息边界回归：模拟 AstrBot 组件和 OneBot 返回值，直接调用真实插件 handler。

不启动 QQ 登录；装饰器桩只记录命令注册参数，数据库集成在 test_database_stats 验证。
"""
import asyncio
import csv
import importlib
import json
import logging
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def plugin_module(monkeypatch):
    def module(name, **values):
        obj = types.ModuleType(name)
        obj.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, obj)
        return obj

    def command(name, alias=None):
        def decorate(fn):
            fn.command_name = name
            fn.aliases = alias or set()
            return fn
        return decorate

    class Component:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.__dict__.update(kwargs)

        @classmethod
        def fromFileSystem(cls, path):
            return cls(file=path)

    module('astrbot')
    module('astrbot.api', AstrBotConfig=dict, logger=logging.getLogger('test_plugin'))
    module('astrbot.api.event', AstrMessageEvent=object, filter=SimpleNamespace(
        command=command, custom_filter=lambda *a, **kw: lambda fn: fn))
    module('astrbot.api.event.filter', CustomFilter=object)
    module('astrbot.api.message_components', **{
        name: type(name, (Component,), {}) for name in ['At', 'File', 'Image', 'Node', 'Nodes', 'Plain', 'Reply']})
    module('astrbot.api.star', Star=object, StarTools=object, register=lambda *a: lambda cls: cls)
    package = module('_review_plugin')
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    loaded = importlib.import_module('_review_plugin.main')
    yield loaded
    for name in list(sys.modules):
        if name.startswith('_review_plugin.'):
            monkeypatch.delitem(sys.modules, name, raising=False)


def event(text='', group='100', admin=False, messages=None):
    return SimpleNamespace(
        get_group_id=lambda: group,
        get_message_str=lambda: text,
        get_sender_id=lambda: '10001',
        get_sender_name=lambda: '提交者',
        is_admin=lambda: admin,
        get_group=AsyncMock(return_value=SimpleNamespace(group_owner='20002', group_admins=['10001'])),
        get_messages=lambda: messages or [],
        plain_result=lambda text: text,
        chain_result=lambda chain: chain,
        bot=SimpleNamespace(call_action=AsyncMock()),
        send=AsyncMock(),
        stop_event=Mock(),
        is_at_or_wake_command=True,
    )


def plugin(mod, tmp_path):
    obj = object.__new__(mod.BattleReportPlugin)
    obj.config = {'min_duels': 1}
    obj.data_dir = tmp_path
    obj.db_ready = True
    obj.db = SimpleNamespace(
        get_group_ban=AsyncMock(return_value=False),
        get_group_home=AsyncMock(return_value='KC'),
        get_group_chat_type=AsyncMock(return_value='战报群'),
        get_group_chat_type_setting=AsyncMock(return_value='战报群'),
        is_team_admin=AsyncMock(return_value=False),
        insert_report=AsyncMock(return_value=123),
    )
    return obj


async def collect(generator):
    return [value async for value in generator]


def ranking_with_llm(mod, tmp_path, text='/排行 七月'):
    obj = plugin(mod, tmp_path)
    obj.config['ranking_image'] = False
    obj.config['ai_persona_id'] = 'plugin-persona'
    obj.db.get_group_home.return_value = 'a队'
    obj.db.get_player_ranking = AsyncMock(return_value=[{
        'player': '云玩家', 'wins': 8, 'losses': 2, 'draws': 1, 'total': 11,
        'points': 6.4, 'raid_points': 4, 'total_points': 10.4,
        'friendship': 3, 'wushuang': 1, 'hold': 3, 'first_round': 0, 'raid_success': 1,
        'qq_id': 'PRIVATE_QQ', 'internal_id': 'PRIVATE_ROW',
    }])
    obj.db.get_settlement = AsyncMock(return_value={})
    obj.db.get_home_team_record = AsyncMock(return_value={
        'wins': 8, 'losses': 2, 'total': 11, 'win_rate': 80, 'total_points': 10.4})
    obj.db.get_home_team_vs_opponents = AsyncMock(return_value=[{
        'opponent': 'b队', 'wins': 3, 'losses': 1, 'total': 4, 'win_rate': 75,
        'raid_points': 1, 'internal_id': 'PRIVATE_ROW'}])
    obj.context = SimpleNamespace(
        get_current_chat_provider_id=AsyncMock(return_value='chat-selected'),
        llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='云玩家表现稳健，继续积累对局喵。')),
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value='conversation-a'),
            get_conversation=AsyncMock(return_value=SimpleNamespace(
                persona_id='persona-a', history='PRIVATE_HISTORY'))),
        persona_manager=SimpleNamespace(
            get_persona_v3_by_id=Mock(return_value={'prompt': '你是插件统一的解说员。'}),
            resolve_selected_persona=AsyncMock()),
        get_config=Mock(return_value={'provider_settings': {'default_personality': 'persona-default'}}),
    )
    ev = event(text)
    ev.unified_msg_origin = 'onebot-a:GroupMessage:100'
    ev.get_platform_name = lambda: 'aiocqhttp'
    return obj, ev


@pytest.mark.parametrize('text,scope,query', [
    ('/排行 七月', '本队个人榜', 'player'), ('/战报排行 七月', '本队个人榜', 'player'),
    ('/排行 全部 七月', '全战队个人榜', 'player'), ('/排行 所有 七月', '全战队个人榜', 'player'),
    ('/排行 队伍 七月', '队伍对战记录', 'team'), ('/排行 战队 七月', '队伍对战记录', 'team'),
    ('/排行 队 七月', '队伍对战记录', 'team')])
def test_ranking_sends_primary_result_before_commentary_using_same_scope_and_month(plugin_module, tmp_path, text, scope, query):
    obj, ev = ranking_with_llm(plugin_module, tmp_path, text)
    async def scenario():
        handler = obj.ranking(ev)
        first = await anext(handler)
        assert '排行点评' not in first
        assert '云玩家' in first if query == 'player' else 'b队' in first
        obj.context.llm_generate.assert_not_awaited()
        second = await anext(handler)
        assert second == '云玩家表现稳健，继续积累对局喵。'
        with pytest.raises(StopAsyncIteration):
            await anext(handler)
    asyncio.run(scenario())
    call = obj.context.llm_generate.await_args.kwargs
    data = json.loads(call['prompt'].split('\n', 1)[1])
    assert data['范围'] == scope and data['战队'] == 'a队'
    assert (data['统计开始日期'], data['统计结束日期']) == ('2026-07-01', '2026-07-31')
    assert call['chat_provider_id'] == 'chat-selected' and call['contexts'] == []
    assert 'tools' not in call and 'PRIVATE_' not in str(call)
    assert call['system_prompt'] == '你是插件统一的解说员。'
    assert call['prompt'].split('\n', 1)[0] == '请自由点评以下排行，100字以内，用一段话表达，不加标题、不分段：'
    obj.context.get_current_chat_provider_id.assert_awaited_once_with(umo=ev.unified_msg_origin)
    obj.context.persona_manager.get_persona_v3_by_id.assert_called_once_with('plugin-persona')
    obj.context.get_config.assert_not_called()
    obj.context.persona_manager.resolve_selected_persona.assert_not_awaited()
    obj.context.conversation_manager.get_curr_conversation_id.assert_not_awaited()
    if scope == '本队个人榜':
        assert data['榜单'] == plugin_module.stats.build_ranking_cells(obj.db.get_player_ranking.return_value)
        assert data['战队战绩面板'] == [list(item) for item in
            plugin_module.stats.build_team_panel(obj.db.get_home_team_record.return_value)]
        obj.db.get_player_ranking.assert_awaited_once_with('a队', '2026-07-01', '2026-07-31', 1, None, team='a队')
    elif scope == '全战队个人榜':
        assert data['战队战绩面板'] is None
        obj.db.get_settlement.assert_not_awaited()
        obj.db.get_player_ranking.assert_awaited_once_with('a队', '2026-07-01', '2026-07-31', 1, 10, team=None)
    else:
        assert data['榜单'] == plugin_module.stats.format_home_team_vs('a队', obj.db.get_home_team_vs_opponents.return_value)
        obj.db.get_player_ranking.assert_not_awaited()
        obj.db.get_home_team_vs_opponents.assert_awaited_once_with('a队', '2026-07-01', '2026-07-31')


@pytest.mark.parametrize('image_works', [True, False])
def test_image_ranking_and_text_fallback_each_append_one_commentary(plugin_module, tmp_path, monkeypatch, image_works):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    obj.config['ranking_image'] = True
    render = Mock(return_value=tmp_path / 'rank.png', side_effect=None if image_works else OSError('render failed'))
    monkeypatch.setattr(plugin_module.chart, 'make_ranking_image', render)
    results = asyncio.run(collect(obj.ranking(ev)))
    assert len(results) == 2 and results[1] == '云玩家表现稳健，继续积累对局喵。'
    assert isinstance(results[0], list) == image_works
    if image_works:
        assert isinstance(results[0][1], plugin_module.Image)
    else:
        assert '云玩家' in results[0]
    obj.context.llm_generate.assert_awaited_once()
    obj.db.get_player_ranking.assert_awaited_once()


def test_commentary_covers_all_visible_rows_for_image_and_text(plugin_module, tmp_path, monkeypatch):
    obj, ev = ranking_with_llm(plugin_module, tmp_path, '/排行 全部')
    base = obj.db.get_player_ranking.return_value[0]
    rows = [dict(base, player=f'示例选手{i}') for i in range(50)]
    obj.db.get_player_ranking.return_value = rows
    obj.config.update(ranking_image=True, ranking_limit=50, ranking_image_max_rows=2)
    monkeypatch.setattr(plugin_module.chart, 'make_ranking_image', Mock(return_value=tmp_path / 'rank.png'))
    asyncio.run(collect(obj.ranking(ev)))
    data = json.loads(obj.context.llm_generate.await_args.kwargs['prompt'].split('\n', 1)[1])
    assert data['本次榜单条目数'] == 2
    assert len(data['榜单']) == 3 and '示例选手2' not in str(data)
    obj.config['ranking_image'] = False
    obj.context.llm_generate.reset_mock()
    results = asyncio.run(collect(obj.ranking(ev)))
    data = json.loads(obj.context.llm_generate.await_args.kwargs['prompt'].split('\n', 1)[1])
    assert '示例选手49' in results[0]
    assert data['本次榜单条目数'] == 50
    assert len(data['榜单']) == 51 and '示例选手49' in str(data)


def test_commentary_uses_displayed_ties_points_and_settlement_bonus(plugin_module, tmp_path):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    first = obj.db.get_player_ranking.return_value[0]
    obj.db.get_player_ranking.return_value.append(dict(first, player='萌新'))
    obj.db.get_settlement.return_value = {'云玩家': 30, '萌新': 30}
    results = asyncio.run(collect(obj.ranking(ev)))
    data = json.loads(obj.context.llm_generate.await_args.kwargs['prompt'].split('\n', 1)[1])
    cells = data['榜单']
    assert cells[0][-1] == '奖金' and cells[1][0] == cells[2][0] == 1
    assert cells[1][2:4] == ['10.4', '6.4'] and cells[1][-1] == '30'
    assert '奖金' in results[0]


@pytest.mark.parametrize('reason', ['disabled', 'empty_players', 'empty_opponents', 'query_error', 'unbound', 'banned'])
def test_unavailable_or_disabled_rankings_do_not_call_llm(plugin_module, tmp_path, reason):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    if reason == 'disabled':
        obj.config['ranking_ai_comment'] = False
    elif reason == 'empty_players':
        obj.db.get_player_ranking.return_value = []
    elif reason == 'empty_opponents':
        ev.get_message_str = lambda: '/排行 队伍'
        obj.db.get_home_team_vs_opponents.return_value = []
    elif reason == 'query_error':
        obj.db.get_player_ranking.side_effect = OSError('db unavailable')
    elif reason == 'unbound':
        obj.db.get_group_home.return_value = None
    else:
        obj.db.get_group_ban.return_value = True
    results = asyncio.run(collect(obj.ranking(ev)))
    assert len(results) == 1
    obj.context.get_current_chat_provider_id.assert_not_awaited()
    obj.context.llm_generate.assert_not_awaited()


@pytest.mark.parametrize('failure', ['provider', 'timeout', 'generate', 'blank', 'none'])
def test_llm_errors_do_not_turn_successful_ranking_into_query_failure(plugin_module, tmp_path, failure):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    if failure == 'provider':
        obj.context.get_current_chat_provider_id.side_effect = ValueError('Provider not found')
    elif failure == 'timeout':
        obj.context.llm_generate.side_effect = TimeoutError()
    elif failure == 'generate':
        obj.context.llm_generate.side_effect = RuntimeError('generation failed')
    else:
        obj.context.llm_generate.return_value.completion_text = '  ' if failure == 'blank' else None
    results = asyncio.run(collect(obj.ranking(ev)))
    assert len(results) == 2 and '云玩家' in results[0]
    assert '点评' in results[1] and all('查询出错' not in part for part in results)
    obj.db.get_player_ranking.assert_awaited_once()
    assert obj.context.llm_generate.await_count <= 1


def test_plugin_persona_does_not_read_global_or_session_overrides(plugin_module, tmp_path):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    obj.context.conversation_manager.get_curr_conversation_id.return_value = None
    obj.context.persona_manager.get_persona_v3_by_id.return_value = {'prompt': '你是默认解说员。'}
    asyncio.run(collect(obj.ranking(ev)))
    obj.context.conversation_manager.get_conversation.assert_not_awaited()
    obj.context.persona_manager.resolve_selected_persona.assert_not_awaited()
    obj.context.get_config.assert_not_called()
    obj.context.conversation_manager.get_curr_conversation_id.assert_not_awaited()
    assert '你是默认解说员。' in obj.context.llm_generate.await_args.kwargs['system_prompt']


def test_empty_persona_prompt_silently_skips_commentary(plugin_module, tmp_path):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    obj.context.persona_manager.get_persona_v3_by_id.return_value = {'prompt': ''}
    results = asyncio.run(collect(obj.ranking(ev)))
    assert len(results) == 1 and '云玩家' in results[0]
    obj.context.llm_generate.assert_not_awaited()
    obj.context.get_current_chat_provider_id.assert_not_awaited()


def test_cancelled_commentary_does_not_retry_or_repeat_ranking(plugin_module, tmp_path):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    started = asyncio.Event()
    async def pending(**kwargs):
        started.set()
        await asyncio.Future()
    obj.context.llm_generate.side_effect = pending
    async def scenario():
        handler = obj.ranking(ev)
        assert '云玩家' in await anext(handler)
        task = asyncio.create_task(anext(handler))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        obj.context.llm_generate.assert_awaited_once()
        obj.db.get_player_ranking.assert_awaited_once()
    asyncio.run(scenario())


def test_commentary_deadline_cancels_slow_model_after_ranking_is_already_available(plugin_module, tmp_path, monkeypatch):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    cancelled = asyncio.Event()
    async def pending(**kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    obj.context.llm_generate.side_effect = pending
    real_wait_for = asyncio.wait_for
    async def quick_deadline(awaitable, timeout):
        assert timeout == 30
        return await real_wait_for(awaitable, 0.01)
    monkeypatch.setattr(plugin_module.asyncio, 'wait_for', quick_deadline)
    results = asyncio.run(collect(obj.ranking(ev)))
    assert '云玩家' in results[0] and '点评这次没赶上' in results[1]
    assert cancelled.is_set()


def test_commentary_without_persona_api_silently_skips(plugin_module, tmp_path):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    del obj.context.persona_manager
    results = asyncio.run(collect(obj.ranking(ev)))
    assert len(results) == 1 and '云玩家' in results[0]
    obj.context.llm_generate.assert_not_awaited()
    obj.context.get_current_chat_provider_id.assert_not_awaited()


@pytest.mark.parametrize('configured', ['plugin-persona', '  plugin-persona  ', 'default'])
def test_commentary_uses_plugin_persona_and_current_chat_model(plugin_module, tmp_path, configured):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    obj.config['ai_persona_id'] = configured
    obj.context.get_config.side_effect = AssertionError('must use configured persona')
    results = asyncio.run(collect(obj.ranking(ev)))
    assert results[1] == '云玩家表现稳健，继续积累对局喵。'
    assert obj.context.llm_generate.await_args.kwargs['chat_provider_id'] == 'chat-selected'
    obj.context.get_current_chat_provider_id.assert_awaited_once_with(umo=ev.unified_msg_origin)
    obj.context.persona_manager.get_persona_v3_by_id.assert_called_once_with(configured.strip())
    assert obj.context.llm_generate.await_args.kwargs['system_prompt'] == '你是插件统一的解说员。'


@pytest.mark.parametrize('configured', ['', '  ', None])
def test_empty_plugin_persona_only_displays_ranking_without_any_ai_prompt(plugin_module, tmp_path, configured):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    obj.config['ai_persona_id'] = configured
    results = asyncio.run(collect(obj.ranking(ev)))
    assert len(results) == 1 and '云玩家' in results[0]
    obj.context.get_current_chat_provider_id.assert_not_awaited()
    obj.context.llm_generate.assert_not_awaited()
    obj.context.get_config.assert_not_called()
    obj.context.persona_manager.get_persona_v3_by_id.assert_not_called()


def test_invalid_plugin_persona_preserves_ranking_without_switching_personas(plugin_module, tmp_path):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    obj.config['ai_persona_id'] = 'removed-persona'
    obj.context.persona_manager.get_persona_v3_by_id.return_value = None
    results = asyncio.run(collect(obj.ranking(ev)))
    assert len(results) == 1 and '云玩家' in results[0]
    obj.context.get_config.assert_not_called()
    obj.context.llm_generate.assert_not_awaited()


@pytest.mark.parametrize('configured', ['', 'plugin-persona'])
def test_plugin_persona_is_shared_across_groups(plugin_module, tmp_path, configured):
    obj, first = ranking_with_llm(plugin_module, tmp_path)
    obj.config['ai_persona_id'] = configured
    second = event('/排行 七月', group='200')
    second.unified_msg_origin = 'onebot-b:GroupMessage:200'
    second.get_platform_name = lambda: 'aiocqhttp'
    obj.context.conversation_manager.get_curr_conversation_id.side_effect = AssertionError('no per-group persona')
    obj.context.persona_manager.resolve_selected_persona.side_effect = AssertionError('no per-group persona')
    first_result = asyncio.run(collect(obj.ranking(first)))
    second_result = asyncio.run(collect(obj.ranking(second)))
    calls = obj.context.llm_generate.await_args_list
    if not configured:
        assert len(first_result) == len(second_result) == 1
        assert not calls
        obj.context.get_current_chat_provider_id.assert_not_awaited()
        return
    assert len(calls) == 2
    assert calls[0].kwargs['system_prompt'] == calls[1].kwargs['system_prompt'] == '你是插件统一的解说员。'
    assert [c.kwargs['umo'] for c in obj.context.get_current_chat_provider_id.await_args_list] == [
        first.unified_msg_origin, second.unified_msg_origin]


def test_disabled_persona_silently_skips_commentary(plugin_module, tmp_path):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    obj.config['ai_persona_id'] = '[%None]'
    results = asyncio.run(collect(obj.ranking(ev)))
    assert len(results) == 1 and '云玩家' in results[0]
    obj.context.llm_generate.assert_not_awaited()
    obj.context.persona_manager.get_persona_v3_by_id.assert_not_called()


@pytest.mark.parametrize('text,expected', [
    ('第一句。\n\n第二句。\r\n第三句。', '第一句。 第二句。 第三句。'),
    ('💬 排行点评\n\n云玩家发挥稳健。', '云玩家发挥稳健。'),
    ('排行点评：\n云玩家发挥稳健。', '云玩家发挥稳健。'),
    ('云玩家发挥稳健。', '云玩家发挥稳健。')])
def test_commentary_has_no_title_or_paragraph_breaks(plugin_module, tmp_path, text, expected):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    obj.context.llm_generate.return_value.completion_text = text
    results = asyncio.run(collect(obj.ranking(ev)))
    assert results[1] == expected
    assert '\n' not in results[1] and '\r' not in results[1]


@pytest.mark.parametrize('text', ['稳' * 1000, '稳' * 90 + '。' + '继续积累对局。' * 50])
def test_long_commentary_stays_short_and_single_paragraph(plugin_module, tmp_path, text):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    obj.context.llm_generate.return_value.completion_text = text
    results = asyncio.run(collect(obj.ranking(ev)))
    assert 0 < len(results[1]) <= 160
    assert results[1].endswith(('。', '…'))
    assert '\n' not in results[1]


def test_missing_persona_needs_no_llm_context_and_sends_no_hint(plugin_module, tmp_path, monkeypatch):
    obj, ev = ranking_with_llm(plugin_module, tmp_path)
    obj.config.pop('ai_persona_id')
    del obj.context
    log = Mock()
    monkeypatch.setattr(plugin_module, 'logger', log)
    results = asyncio.run(collect(obj.ranking(ev)))
    assert len(results) == 1 and '云玩家' in results[0]
    log.exception.assert_not_called()


def review_event(raw, text='', sender='10001', group=''):
    ev = event(text, group=group, admin=True)
    ev.get_sender_id = lambda: sender
    ev.get_platform_name = lambda: 'aiocqhttp'
    ev.get_platform_id = lambda: 'onebot-a'
    ev.get_self_id = lambda: '12345'
    ev.message_obj = SimpleNamespace(raw_message=raw)
    ev.stop_event = Mock()
    ev.send = AsyncMock()
    return ev


def new_qq_request(kind='friend'):
    return {'post_type':'request', 'request_type':kind, 'self_id':12345,
            'user_id':20002, 'flag':'opaque-original', 'comment':'你好',
            **({'sub_type':'invite','group_id':30003,'invited_id':12345} if kind=='group' else {})}


@pytest.mark.parametrize('kind', ['friend', 'group'])
def test_qq_request_handler_notifies_super_admin_without_database(plugin_module, tmp_path, kind):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db_ready, obj.db = False, None
    ev = review_event(new_qq_request(kind), group='30003' if kind=='group' else '')
    ev.bot.call_action.return_value = {'message_id':90001}
    assert plugin_module.RequestReviewFilter().filter(ev, {})
    assert asyncio.run(obj.review_request(ev)) is None
    sent = [c for c in ev.bot.call_action.await_args_list if c.args[0]=='send_private_msg']
    assert len(sent) == 1 and sent[0].kwargs['user_id'] == 10001
    ev.stop_event.assert_called_once()
    reply = review_event({'post_type':'message','message_type':'private'}, text='同意')
    reply.bot = ev.bot
    reply.get_messages = lambda: [plugin_module.Reply(id='90001')]
    asyncio.run(obj.review_request(reply))
    assert '已同意' in reply.send.await_args.args[0]
    reply.stop_event.assert_called_once()
    approvals = [c for c in ev.bot.call_action.await_args_list if c.args[0].startswith('set_')]
    assert len(approvals) == 1
    assert approvals[0].kwargs['flag'] == 'opaque-original' and approvals[0].kwargs['self_id'] == 12345
    results = [c for c in ev.bot.call_action.await_args_list
               if c.args[0] == 'send_private_msg' and c.kwargs['user_id'] == 20002]
    assert len(results) == int(kind == 'group')
    if results:
        assert results[0].kwargs['message'] == [
            {'type': 'text', 'data': {'text': '你的入群邀请已被同意喵。\n群号：30003'}}]


@pytest.mark.parametrize('component_id', [None, 'changed-id'])
@pytest.mark.parametrize('approve', [True, False])
def test_review_uses_raw_quote_if_framework_drops_or_changes_reply(plugin_module, tmp_path, component_id, approve):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    request = review_event(new_qq_request('group'))
    request.bot.call_action.return_value = {'message_id': 90001}
    asyncio.run(obj.review_request(request))
    reply = review_event({'post_type': 'message', 'message_type': 'private',
                          'message': [{'type': 'reply', 'data': {'id': '90001'}}]},
                         text='同意' if approve else '拒绝')
    reply.bot = request.bot
    if component_id is not None:
        reply.get_messages = lambda: [plugin_module.Reply(id=component_id)]
    asyncio.run(obj.review_request(reply))
    assert ('已同意' if approve else '已拒绝') in reply.send.await_args.args[0]
    approvals = [c for c in request.bot.call_action.await_args_list if c.args[0].startswith('set_')]
    assert len(approvals) == 1 and approvals[0].kwargs['approve'] == approve


def test_unresolved_raw_quote_cannot_approve_the_only_pending_request(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    request = review_event(new_qq_request('group'))
    request.bot.call_action.return_value = {'message_id': 90001}
    asyncio.run(obj.review_request(request))
    reply = review_event({'post_type': 'message', 'message_type': 'private',
                          'message': [{'type': 'reply', 'data': {'id': 'unknown'}}]}, text='同意')
    reply.bot = request.bot
    asyncio.run(obj.review_request(reply))
    assert '无法识别引用' in reply.send.await_args.args[0]
    assert not any(c.args[0].startswith('set_') for c in request.bot.call_action.await_args_list)


@pytest.mark.parametrize('decision', ['同意', '拒绝'])
def test_numbered_review_shows_platform_error_and_uses_astrbot_logger(plugin_module, tmp_path, monkeypatch, decision):
    logger = Mock()
    monkeypatch.setattr(plugin_module, 'logger', logger)
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    request = review_event(new_qq_request('group'))
    asyncio.run(obj.review_request(request))
    token = next(iter(obj._review_inbox.records))
    reply = review_event({'post_type': 'message', 'message_type': 'private'}, text=f'{decision} {token}')
    reply.bot.call_action.return_value = {'status': 'failed', 'retcode': 1200,
                                          'wording': 'group request not found'}
    asyncio.run(obj.review_request(reply))
    assert 'group request not found' in reply.send.await_args.args[0]
    logger.exception.assert_called_once_with('QQ 申请处理失败')
    assert obj._review_inbox.records[token]['status'] == 'pending'


@pytest.mark.parametrize('approve', [True, False])
def test_snowluma_11411_old_request_failure_then_native_reinvite_uses_card_sequence(plugin_module, tmp_path, qq_invite_card, approve):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db_ready, obj.db = False, None
    bot = SimpleNamespace(call_action=AsyncMock())
    async def action(name, **params):
        if name == 'get_version_info':
            return {'app_name': 'SnowLuma', 'app_version': '1.14.11-node'}
        if name == 'send_private_msg':
            return {'message_id': 90000 + len([c for c in bot.call_action.await_args_list if c.args[0] == name])}
        if name == 'set_group_add_request':
            if params['flag'] == 'invite:30003:u_example':
                return {'status': 'failed', 'retcode': 100,
                        'wording': 'OIDB error 120161001 on 0x10c8_1: handle async message fail'}
            assert params == {'self_id': 12345, 'flag': 'slreq:1:765:30003:2:0',
                              'sub_type': 'invite', 'approve': approve}
        return {}
    bot.call_action.side_effect = action
    request = review_event(new_qq_request('group') | {'flag': 'invite:30003:u_example', 'time': 90})
    request.bot = bot
    asyncio.run(obj.review_request(request))
    old = next(iter(obj._review_inbox.records))
    failed = review_event({'post_type': 'message', 'message_type': 'private'}, f'同意 {old}')
    failed.bot = bot
    asyncio.run(obj.review_request(failed))
    assert '120161001' in failed.send.await_args.args[0]
    card = review_event(qq_invite_card())
    card.bot = bot
    assert plugin_module.RequestReviewFilter().filter(card, {})
    asyncio.run(obj.review_request(card))
    card.stop_event.assert_called_once()
    token = next(t for t, r in obj._review_inbox.records.items() if r.get('source') == 'snowluma_invite_card')
    assert obj._review_inbox.records[old]['status'] == 'superseded'
    notices = [c for c in bot.call_action.await_args_list if c.args[0] == 'send_private_msg']
    assert len(notices) == 2 and token in notices[-1].kwargs['message'][0]['data']['text']
    assert all(c.kwargs['user_id'] == 10001 for c in notices)
    assert len([c for c in bot.call_action.await_args_list if c.args[0].startswith('set_')]) == 1
    # 重新加载记录后，用新通知的引用审批；仍只回传卡片中的准确序号。
    obj._review_inbox = None
    reply = review_event({'post_type': 'message', 'message_type': 'private'}, '同意' if approve else '拒绝')
    reply.bot = bot
    reply.get_messages = lambda: [plugin_module.Reply(id='90002')]
    asyncio.run(obj.review_request(reply))
    assert ('已同意' if approve else '已拒绝') in reply.send.await_args.args[0]
    assert obj._review_inbox.records[token]['status'] == ('approved' if approve else 'rejected')


@pytest.mark.parametrize('backend', ['NapCat', '', None])
def test_native_card_is_not_converted_to_a_snowluma_handle_on_other_backends(plugin_module, tmp_path, qq_invite_card, backend):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    card = review_event(qq_invite_card())
    card.bot.call_action.return_value = {'app_name': backend}
    asyncio.run(obj.review_request(card))
    assert obj._review_inbox.records == {}
    card.stop_event.assert_not_called()
    card.send.assert_not_awaited()
    assert [c.args[0] for c in card.bot.call_action.await_args_list] == ['get_version_info']


@pytest.mark.parametrize('approve', [True, False])
def test_already_agreed_invite_is_not_reported_as_failure_or_rejection(plugin_module, tmp_path, monkeypatch, approve):
    logger = Mock()
    monkeypatch.setattr(plugin_module, 'logger', logger)
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    request = review_event(new_qq_request('group'))
    asyncio.run(obj.review_request(request))
    token = next(iter(obj._review_inbox.records))
    reply = review_event({'post_type': 'message', 'message_type': 'private'},
                         f'{"同意" if approve else "拒绝"} {token}')
    async def action(name, **params):
        if name == 'set_group_add_request':
            return {'status': 'failed', 'retcode': 100,
                    'wording': 'OIDB error 120162004 on 0x10c8_1: already agree msg by self'}
        return {'message_id': 90002}
    reply.bot.call_action.side_effect = action
    asyncio.run(obj.review_request(reply))
    assert '已被同意' in reply.send.await_args.args[0]
    assert ('无法再拒绝' in reply.send.await_args.args[0]) == (not approve)
    assert obj._review_inbox.records[token]['status'] == 'approved'
    logger.exception.assert_not_called()
    sent = [c for c in reply.bot.call_action.await_args_list if c.args[0] == 'send_private_msg']
    assert len(sent) == 1 and sent[0].kwargs['user_id'] == 20002
    assert '已被同意' in str(sent[0].kwargs['message']) and '拒绝' not in str(sent[0].kwargs['message'])


@pytest.mark.parametrize('approve', [True, False])
def test_inviter_notification_failure_returns_real_approval_result_to_admin(plugin_module, tmp_path, monkeypatch, approve):
    logger = Mock()
    monkeypatch.setattr(plugin_module, 'logger', logger)
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    request = review_event(new_qq_request('group'))
    asyncio.run(obj.review_request(request))
    token = next(iter(obj._review_inbox.records))
    reply = review_event({'post_type': 'message', 'message_type': 'private'},
                         f'{"同意" if approve else "拒绝"} {token}')
    async def action(name, **params):
        if name == 'send_private_msg':
            assert params['user_id'] == 20002
            return {'status': 'failed', 'retcode': 100, 'wording': 'message unavailable'}
        return {}
    reply.bot.call_action.side_effect = action
    asyncio.run(obj.review_request(reply))
    text = reply.send.await_args.args[0]
    assert ('已同意' if approve else '已拒绝') in text and '邀请人通知未确认' in text
    assert obj._review_inbox.records[token]['status'] == ('approved' if approve else 'rejected')
    logger.exception.assert_called_once_with('发送入群邀请结果通知失败')


@pytest.mark.parametrize('sender,group,message_type', [
    ('20002','','private'), ('10001','100','group'), ('10001','100','private'),
])
def test_only_super_admin_private_messages_can_review(plugin_module,tmp_path,sender,group,message_type):
    obj = plugin(plugin_module,tmp_path)
    obj.config['super_admin'] = '10001'
    ev = review_event({'post_type':'message','message_type':message_type}, '同意', sender, group)
    obj._get_review_inbox = Mock(side_effect=AssertionError('must not open inbox'))
    assert asyncio.run(obj.review_request(ev)) is None
    obj._get_review_inbox.assert_not_called()
    ev.bot.call_action.assert_not_awaited()
    ev.stop_event.assert_not_called()


def test_unconfigured_admin_and_unrelated_events_do_not_trigger_actions(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    ev = review_event(new_qq_request())
    assert asyncio.run(obj.review_request(ev)) is None
    ev.bot.call_action.assert_not_awaited()
    ordinary = review_event({'post_type':'message','message_type':'private'}, '我同意这个看法')
    assert not plugin_module.RequestReviewFilter().filter(ordinary,{})
    for raw in [new_qq_request('group') | {'sub_type':'add'},
                new_qq_request('group') | {'invited_id':54321},
                new_qq_request() | {'post_type':'notice'}]:
        ignored = review_event(raw)
        assert not plugin_module.RequestReviewFilter().filter(ignored,{})
        assert asyncio.run(obj.review_request(ignored)) is None
        ignored.bot.call_action.assert_not_awaited()


def test_private_request_list_works_after_restart_without_database(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db_ready = False
    request = review_event(new_qq_request())
    asyncio.run(obj.review_request(request))
    obj._review_inbox = None
    ev = review_event({'post_type':'message','message_type':'private'}, '/申请列表')
    asyncio.run(obj.request_list(ev))
    response = ev.send.await_args.args[0]
    assert '20002' in response and '同意 编号' in response
    ev.get_sender_id = lambda: '20002'
    assert asyncio.run(obj.request_list(ev)) is None


@pytest.mark.parametrize('approve', [True, False])
def test_super_admin_refreshes_saved_invite_then_reviews_new_notice_without_database(plugin_module, tmp_path, qq_invite_card, approve):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db_ready, obj.db = False, None
    bot = SimpleNamespace(call_action=AsyncMock())
    async def action(name, **params):
        if name == 'get_version_info':
            return {'app_name': 'SnowLuma', 'app_version': '1.14.11-node'}
        if name == 'get_friend_msg_history':
            return {'messages': [{k: v for k, v in qq_invite_card().items()
                                  if k not in ('post_type', 'self_id')}]}
        if name == 'send_private_msg':
            return {'message_id': 90000 + len([c for c in bot.call_action.await_args_list if c.args[0] == name])}
        return {}
    bot.call_action.side_effect = action
    request = review_event(new_qq_request('group') | {'flag': 'slreq:1:1790000000000010:30003:1:0'})
    request.bot = bot
    asyncio.run(obj.review_request(request))
    old = next(iter(obj._review_inbox.records))
    obj._review_inbox = None
    refresh = review_event({'post_type': 'message', 'message_type': 'private'}, f'/刷新申请 {old}')
    refresh.bot = bot
    asyncio.run(obj.request_refresh(refresh))
    refresh.stop_event.assert_called_once()
    assert '已刷新 1 条' in refresh.send.await_args.args[0]
    assert obj.request_refresh.command_name == '刷新申请'
    assert not any(c.args[0].startswith('set_') for c in bot.call_action.await_args_list)
    obj._review_inbox = None
    reply = review_event({'post_type': 'message', 'message_type': 'private',
                          'message': [{'type': 'reply', 'data': {'id': '90002'}}]}, '同意' if approve else '拒绝')
    reply.bot = bot
    asyncio.run(obj.review_request(reply))
    assert ('已同意' if approve else '已拒绝') in reply.send.await_args.args[0]
    calls = [c for c in bot.call_action.await_args_list if c.args[0] == 'set_group_add_request']
    assert len(calls) == 1 and calls[0].kwargs == {
        'self_id': 12345, 'flag': 'slreq:1:765:30003:2:0', 'sub_type': 'invite', 'approve': approve}


@pytest.mark.parametrize('sender,group,backend', [
    ('20002', '', 'aiocqhttp'), ('10001', '30003', 'aiocqhttp'), ('10001', '', 'other')])
def test_refresh_rejects_non_super_admin_group_and_other_platform(plugin_module, tmp_path, sender, group, backend):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    obj._get_review_inbox = Mock(side_effect=AssertionError('must not open inbox'))
    ev = review_event({'post_type': 'message', 'message_type': 'private'}, '/刷新申请', sender, group)
    ev.get_platform_name = lambda: backend
    asyncio.run(obj.request_refresh(ev))
    obj._get_review_inbox.assert_not_called()
    ev.bot.call_action.assert_not_awaited()


@pytest.mark.parametrize('argument', ['bad', 'ABCDEF01 extra', 'ABCDEF01\n同意'])
def test_refresh_invalid_arguments_do_not_read_history(plugin_module, tmp_path, argument):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    obj._get_review_inbox = Mock(side_effect=AssertionError('must not open inbox'))
    ev = review_event({'post_type': 'message', 'message_type': 'private'}, '/刷新申请 ' + argument)
    asyncio.run(obj.request_refresh(ev))
    assert '用法' in ev.send.await_args.args[0]
    obj._get_review_inbox.assert_not_called()


def test_round_command_fills_only_available_survivors(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    draft = """战队: a队 VS b队
时间: 2026.10.20
规则: 2/3【KOF】
地点: 100
------第一轮------
云玩家  2:0  复读机
萌新  0:2  鸽子
遗老  0:2  柠檬
路人甲  2:0  路人乙
------第二轮------
云玩家  0:0  柠檬"""
    obj._read_latest_report = AsyncMock(return_value=draft)
    ev = event('/第二轮')
    response = asyncio.run(collect(obj.round_cmd(ev)))
    assert response == [] and ev.send.await_count == 1
    assert ev.send.await_args.args[0].splitlines() == draft.splitlines() + ['路人甲  0:0  鸽子']
    obj._read_latest_report.assert_awaited_once()
    obj.db.insert_report.assert_not_awaited()


@pytest.mark.parametrize('handler_name', ['lineup_cmd', 'record_cmd', 'round_cmd'])
def test_report_output_sends_exact_double_spaces_without_decorating_pipeline(plugin_module, tmp_path, handler_name):
    obj = plugin(plugin_module, tmp_path)
    obj.config['pairing_seed'] = 'test'
    obj.db.replace_teams = AsyncMock()
    draft = ('战队: a队 VS b队\n时间: 2026.10.25\n规则: 2/3【KOF】\n地点: 100\n'
             '------第一轮------\n示例  玩家(替) 2:0 萌新  遗老(规则)\n'
             '云玩家 0:0 遗老\n------第二轮------')
    obj._read_latest_report = AsyncMock(return_value=draft)
    command = {'lineup_cmd': '/排表\na队: "示例  玩家"\nb队: "萌新  遗老"',
               'record_cmd': '/记录 云玩家 20',
               'round_cmd': '/第二轮 云玩家 0:0 遗老'}[handler_name]
    ev = event(command)
    sent = []
    async def send(text):
        assert ev.stop_event.called
        sent.append(text)
    ev.send.side_effect = send
    # 普通回复经过整理会把连续空格合并；战报应完全不进入该回复流。
    decorated = [re.sub(r' {2,}', ' ', result)
                 for result in asyncio.run(collect(getattr(obj, handler_name)(ev)))]
    assert decorated == [] and len(sent) == 1
    rows = [line for line in sent[0].splitlines() if '0:0' in line or '2:0' in line]
    assert rows
    assert all(re.fullmatch(r'.+  \d+:\d+  .+', line) for line in rows)
    assert '示例  玩家' in sent[0] and '萌新  遗老' in sent[0]
    if handler_name != 'lineup_cmd':
        assert sent[0].startswith('战队: a队 VS b队\n时间: 2026.10.25\n')
        assert '示例  玩家(替)  ' in sent[0]
    ev.stop_event.assert_called_once()
    obj.db.insert_report.assert_not_awaited()


@pytest.mark.parametrize('handler_name,command', [
    ('lineup_cmd', '/排表\na队: 云玩家\nb队: 萌新'),
    ('record_cmd', '/记录 云玩家 20'), ('round_cmd', '/第二轮')])
def test_forbidden_group_cannot_directly_send_a_report(plugin_module, tmp_path, handler_name, command):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_group_ban.return_value = True
    obj._read_latest_report = AsyncMock()
    obj.db.replace_teams = AsyncMock()
    ev = event(command)
    result = asyncio.run(collect(getattr(obj, handler_name)(ev)))
    assert len(result) == 1 and '禁用' in result[0]
    ev.send.assert_not_awaited()
    obj._read_latest_report.assert_not_awaited()
    obj.db.replace_teams.assert_not_awaited()


def test_partial_round_warning_is_delivered_after_exact_report(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj._read_latest_report = AsyncMock(return_value=(
        '战队: a队 VS b队\n时间: 2026.10.25\n规则: 2/3【KOF】\n地点: 100\n'
        '------第一轮------\n云玩家 2:0 萌新\n------第二轮------'))
    ev = event('/第二轮 云玩家 0:0 萌新\ninvalid')
    result = asyncio.run(collect(obj.round_cmd(ev)))
    assert result == [] and ev.send.await_count == 2
    report, warning = [call.args[0] for call in ev.send.await_args_list]
    assert '云玩家  2:0  萌新' in report and '云玩家  0:0  萌新' in report
    assert '有几行对局没看懂' in warning


def test_partial_record_warning_is_delivered_after_exact_report(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj._read_latest_report = AsyncMock(return_value=(
        '战队: a队 VS b队\n时间: 2026.10.25\n规则: 2/3【KOF】\n地点: 100\n'
        '------第一轮------\n云玩家 0:0 萌新\n------第二轮------'))
    ev = event('/记录 云玩家 20\ninvalid')
    result = asyncio.run(collect(obj.record_cmd(ev)))
    assert result == [] and ev.send.await_count == 2
    report, warning = [call.args[0] for call in ev.send.await_args_list]
    assert '云玩家  2:0  萌新' in report and '有几条没记上' in warning


def test_failed_direct_report_send_is_not_retried_through_normal_reply(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    ev = event('/排表')
    ev.send.side_effect = TimeoutError('result unknown')
    with pytest.raises(TimeoutError):
        asyncio.run(obj._send_report_text(ev, '------第一轮------\n云玩家 0:0 萌新'))
    ev.send.assert_awaited_once_with('------第一轮------\n云玩家  0:0  萌新')
    ev.stop_event.assert_called_once()


def report(group='100', team='DYG', colon=':'):
    return f'战队{colon} KC VS {team}\n时间: 2026.08.01\n规则: 人头赛\n地点: {group}\n------第一轮------\n甲 2:0 乙'


@pytest.mark.parametrize('super_admin', [False, True])
def test_private_submissions_are_always_blocked(plugin_module, tmp_path, super_admin):
    obj = plugin(plugin_module, tmp_path)
    obj.config['allow_private_chat'] = True  # 旧安装保存的开关不能恢复私聊入口。
    if super_admin:
        obj.config['super_admin'] = '10001'
    ev = event('/发送\n' + report(), group='', admin=True)
    response = asyncio.run(collect(obj.submit_report(ev)))
    assert '禁止私聊' in response[0]
    obj.db.insert_report.assert_not_awaited()
    ev.bot.call_action.assert_not_awaited()


@pytest.mark.parametrize('chat_type', ['友谊群', '主群'])
def test_only_report_groups_can_submit(plugin_module, tmp_path, chat_type):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db.get_group_chat_type.return_value = chat_type
    obj.db.get_group_chat_type_setting.return_value = chat_type
    ev = event('/发送\n' + report(), admin=True)
    response = asyncio.run(collect(obj.submit_report(ev)))
    assert '只有战报群' in response[0]
    obj.db.insert_report.assert_not_awaited()
    ev.bot.call_action.assert_not_awaited()


def test_report_group_accepts_submission(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    ev = event('/发送\n' + report())
    asyncio.run(collect(obj.submit_report(ev)))
    obj.db.insert_report.assert_awaited_once()
    assert obj.db.insert_report.await_args.args[0].group_id == '100'


def test_disabled_report_group_still_blocks_super_admin(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db.get_group_ban.return_value = True
    asyncio.run(collect(obj.submit_report(event('/发送\n' + report()))))
    obj.db.insert_report.assert_not_awaited()


def test_admin_only_submit_accepts_group_admin(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.config['submit_requires_admin'] = True
    ev = event('/发送\n' + report())
    asyncio.run(collect(obj.submit_report(ev)))
    obj.db.insert_report.assert_awaited_once()


def test_admin_only_submit_accepts_team_admin_without_qq_role(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.config['submit_requires_admin'] = True
    obj.db.is_team_admin.return_value = True
    ev = event('/发送\n' + report())
    ev.get_group.return_value = SimpleNamespace(group_owner='20002', group_admins=[])
    asyncio.run(collect(obj.submit_report(ev)))
    obj.db.insert_report.assert_awaited_once()
    ev.get_group.assert_not_awaited()


def test_no_implicit_author_super_admin(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    ev = event()
    ev.get_sender_id = lambda: '1443290403'
    assert obj._is_super_admin(ev) is False
    obj.config['super_admin'] = '1443290403'
    assert obj._is_super_admin(ev) is True


def test_fullwidth_quoted_batch_and_latest_report(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    ev = event(messages=[plugin_module.Reply(id='-100')])
    texts = report(colon='：') + '\n' + report(team='BAR', colon=' ：')
    ev.bot.call_action.return_value = {'message': [{'type': 'text', 'data': {'text': texts}}]}
    chunks = asyncio.run(obj._extract_reply_reports(ev))
    assert len(chunks) == 2
    assert all(plugin_module._is_report_text(chunk) for chunk in chunks)
    assert ev.bot.call_action.await_args.kwargs['message_id'] == -100
    ev.bot.call_action.return_value = {'messages': [{'time': 1, 'raw_message': report(colon='：')}]}
    assert asyncio.run(obj._read_latest_report(ev)) == report(colon='：')


def test_forward_nodes_may_contain_multiple_reports(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    ev = event(messages=[plugin_module.Reply(id='-100')])
    ev.bot.call_action.side_effect = [
        {'message': [{'type': 'forward', 'data': {'id': 'res1'}}]},
        {'messages': [{'content': [{'type': 'text', 'data': {'text': report(colon='：') + '\n' + report(team='BAR')}}]}]},
    ]
    assert len(asyncio.run(obj._extract_reply_reports(ev))) == 2


@pytest.mark.parametrize('handler,alias', [('submit_report','战报'), ('ranking','战报排行'),
    ('record','战报战绩'), ('raid','战报踢馆'), ('trend','战报趋势'), ('export','战报导出'), ('help_cmd','战报帮助')])
def test_legacy_alias_after_astrbot_strips_wake_prefix(plugin_module, handler, alias):
    assert alias in getattr(plugin_module.BattleReportPlugin, handler).aliases


def test_strip_command_prefers_complete_alias(plugin_module):
    assert plugin_module._strip_command('/战报排行 七月', ('战报', '/战报', '/战报排行')) == '七月'


@pytest.mark.parametrize('payload', ['0月', '13月', 'abc', '-1'])
def test_invalid_settlement_input_never_writes(plugin_module, tmp_path, payload):
    obj = plugin(plugin_module, tmp_path)
    obj.db.clear_settlement = AsyncMock()
    obj.db.get_player_ranking = AsyncMock()
    obj.db.set_settlement = AsyncMock()
    obj._require_home = AsyncMock(return_value=(None, 'KC'))
    ev = event('/重置结算 ' + payload)
    response = asyncio.run(collect(obj.reset_settle(ev)))
    assert response and '❌' in response[0]
    obj.db.clear_settlement.assert_not_awaited()
    ev.get_message_str = lambda: '/结算 ' + payload
    response = asyncio.run(collect(obj.settle(ev)))
    assert response and '❌' in response[0]
    obj.db.get_player_ranking.assert_not_awaited()
    obj.db.set_settlement.assert_not_awaited()


def test_nonadmin_settlement_is_read_only(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj._require_home = AsyncMock(return_value=(None, 'KC'))
    obj._is_manager = AsyncMock(return_value=False)
    obj.db.get_settlement_entries = AsyncMock(return_value=[])
    obj.db.set_settlement = AsyncMock()
    ev = event('/结算 7月')
    response = asyncio.run(collect(obj.settle(ev)))
    assert '还没结算' in response[0]
    obj.db.set_settlement.assert_not_awaited()


def test_round_command_with_slash_and_without(plugin_module):
    matcher = plugin_module.RoundCommandFilter()
    assert matcher.filter(event('/第二轮'), {})
    assert matcher.filter(event('第二轮'), {})
    assert not matcher.filter(event('第二轮比赛很精彩'), {})


def test_export_requests_never_share_output_file(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_export_rows = AsyncMock(return_value=[{'match_id': 1}])
    obj.db.get_reports_for_export = AsyncMock(return_value=[{
        'match_id': 1, 'kind': 'friendly', 'home_team': 'KC', 'team_a': 'KC',
        'team_b': 'b队', 'winner': 'KC'}])
    ev = event('/导出 json 七月')

    async def run():
        return await asyncio.gather(collect(obj.export(ev)), collect(obj.export(ev)))

    results = asyncio.run(run())
    files = [response[0][1].file for response in results]
    assert files[0] != files[1]
    assert all(Path(path).read_text().startswith('[') for path in files)


def test_batch_submission_responses_respect_onebot_node_limit(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    chunks = list(obj._send_responses(event(), ['回执'] * 201))
    assert [len(chunk[0].args[0]) for chunk in chunks] == [100, 100, 1]


def test_admin_binding_uses_exact_id_without_roster_limit(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj._require_home = AsyncMock(return_value=(None, 'KC'))
    obj.db.get_player_binding = AsyncMock(return_value={'user_id': None})
    obj.db.find_or_create_user = AsyncMock(return_value=9)
    obj.db.bind_player_to_user = AsyncMock()
    ev = event('/管理ID 最后一个ID 角色')
    response = asyncio.run(collect(obj.admin_id(ev)))
    assert '✅' in response[0]
    obj.db.get_player_binding.assert_awaited_once_with('KC', '最后一个ID')
    obj.db.bind_player_to_user.assert_awaited_once_with('KC', '最后一个ID', 9)


@pytest.mark.parametrize('global_admin', [False, True])
def test_group_owner_cannot_change_settings_without_team_grant(plugin_module, tmp_path, global_admin):
    obj = plugin(plugin_module, tmp_path)
    obj.db.set_group_home = AsyncMock()
    obj.db.set_group_chat_type = AsyncMock()
    ev = event('/绑定战队 KC', admin=global_admin)
    ev.get_group.return_value = SimpleNamespace(group_owner='10001', group_admins=['10001'])
    response = asyncio.run(collect(obj.bind_home(ev)))
    assert '战队管理员' in response[0]
    obj.db.set_group_home.assert_not_awaited()
    response = asyncio.run(collect(obj.set_chat_type(ev, '战报群')))
    assert '战队管理员' in response[0]
    obj.db.set_group_chat_type.assert_not_awaited()


def test_team_admin_can_bind_unbound_group_and_set_type(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_group_home.return_value = None
    obj.db.is_team_admin.side_effect = lambda home, qq: home == 'KC' and qq == '10001'
    obj.db.set_group_home = AsyncMock()
    obj.db.backfill_group_home = AsyncMock()
    obj.db.set_group_chat_type = AsyncMock()
    ev = event('/绑定战队 kc')
    response = asyncio.run(collect(obj.bind_home(ev)))
    assert '✅' in response[0]
    obj.db.set_group_home.assert_awaited_once_with('100', 'KC')
    obj.db.get_group_home.return_value = 'KC'
    response = asyncio.run(collect(obj.set_chat_type(ev, '战报群')))
    assert '✅' in response[0]
    obj.db.set_group_chat_type.assert_awaited_once_with('100', '战报群')


def test_team_admin_cannot_configure_other_team(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_group_home.return_value = 'BAR'
    obj.db.is_team_admin.side_effect = lambda home, qq: home == 'KC'
    obj.db.set_group_home = AsyncMock()
    obj.db.set_group_chat_type = AsyncMock()
    ev = event('/绑定战队 BAR')
    asyncio.run(collect(obj.bind_home(ev)))
    asyncio.run(collect(obj.set_chat_type(ev, '战报群')))
    obj.db.set_group_home.assert_not_awaited()
    obj.db.set_group_chat_type.assert_not_awaited()


@pytest.mark.parametrize('owns_old', [False, True])
def test_rebinding_requires_permission_on_both_teams(plugin_module, tmp_path, owns_old):
    obj = plugin(plugin_module, tmp_path)
    obj.db.is_team_admin.side_effect = lambda home, qq: home == 'BAR' or owns_old
    obj.db.set_group_home = AsyncMock()
    obj.db.backfill_group_home = AsyncMock()
    response = asyncio.run(collect(obj.bind_home(event('/绑定战队 BAR'))))
    assert obj.db.set_group_home.await_count == int(owns_old)
    assert ('✅' in response[0]) == owns_old


def test_super_admin_can_configure_any_group_without_team_grant(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db.get_group_ban.return_value = True
    obj.db.set_group_home = AsyncMock()
    obj.db.backfill_group_home = AsyncMock()
    obj.db.set_group_chat_type = AsyncMock()
    ev = event('/绑定战队 BAR')
    response = asyncio.run(collect(obj.bind_home(ev)))
    assert '✅' in response[0]
    obj.db.set_group_home.assert_awaited_once_with('100', 'BAR')
    obj.db.get_group_home.return_value = None
    response = asyncio.run(collect(obj.set_chat_type(ev, '主群')))
    assert '✅' in response[0]
    obj.db.set_group_chat_type.assert_awaited_once_with('100', '主群')
    obj.db.is_team_admin.assert_not_awaited()


@pytest.mark.parametrize('handler,text', [
    ('set_team_admins','/设置战队管理员 KC 12345'),
    ('remove_team_admin','/移除战队管理员 KC 12345'),
    ('view_team_admins','/查看战队管理员 KC'),
])
def test_only_super_admin_can_manage_team_grants(plugin_module, tmp_path, handler, text):
    obj = plugin(plugin_module, tmp_path)
    obj.db.is_team_admin.return_value = True
    obj.db.set_team_admins = AsyncMock()
    obj.db.remove_team_admin = AsyncMock()
    obj.db.get_team_admins = AsyncMock()
    response = asyncio.run(collect(getattr(obj, handler)(event(text, admin=True))))
    assert '超级管理员' in response[0]
    obj.db.set_team_admins.assert_not_awaited()
    obj.db.remove_team_admin.assert_not_awaited()
    obj.db.get_team_admins.assert_not_awaited()


def test_super_admin_grant_list_revoke_lifecycle(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db.set_team_admins = AsyncMock()
    obj.db.remove_team_admin = AsyncMock(return_value=1)
    obj.db.get_team_admins = AsyncMock(return_value=['12345', '67890'])
    ev = event('/设置战队管理员 kc 12345,67890')
    assert '✅' in asyncio.run(collect(obj.set_team_admins(ev)))[0]
    obj.db.set_team_admins.assert_awaited_once_with('KC', ['12345', '67890'])
    ev.get_message_str = lambda: '/查看战队管理员 KC'
    assert '12345' in asyncio.run(collect(obj.view_team_admins(ev)))[0]
    ev.get_message_str = lambda: '/移除战队管理员 KC 12345'
    assert '✅' in asyncio.run(collect(obj.remove_team_admin(ev)))[0]
    obj.db.remove_team_admin.assert_awaited_once_with('KC', '12345')


@pytest.mark.parametrize('qq', ['abc', '-1', '0123', '１２３', '123456789012345678901'])
def test_invalid_admin_qq_cannot_write(plugin_module, tmp_path, qq):
    obj = plugin(plugin_module, tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db.set_team_admins = AsyncMock()
    response = asyncio.run(collect(obj.set_team_admins(event('/设置战队管理员 KC '+qq))))
    assert '❌' in response[0]
    obj.db.set_team_admins.assert_not_awaited()


def test_team_admin_inherits_manager_only_for_current_team(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.db.is_team_admin.side_effect = lambda home, qq: home == 'KC'
    ev = event()
    ev.get_group.return_value = SimpleNamespace(group_owner='20002', group_admins=[])
    assert asyncio.run(obj._is_manager(ev))
    obj.db.get_group_home.return_value = 'BAR'
    assert not asyncio.run(obj._is_manager(ev))


@pytest.mark.parametrize('text', ['ice boundary', '"ice boundary"', '“ice boundary”'])
def test_binding_preserves_space_id(plugin_module, tmp_path, text):
    obj = plugin(plugin_module, tmp_path)
    obj._require_home = AsyncMock(return_value=(None, 'KC'))
    obj.db.get_player_binding = AsyncMock(return_value={'user_id': None})
    obj.db.get_user_by_qq = AsyncMock(return_value=None)
    obj.db.bind_player_to_self = AsyncMock(return_value=('ok', {'id': 9, 'name': 'ice boundary'}))
    response = asyncio.run(collect(obj.bind_id(event('/绑定ID '+text))))
    assert '✅' in response[0]
    obj.db.bind_player_to_self.assert_awaited_once_with('KC', 'ice boundary', '10001')


def test_binding_list_and_unbinding_quoted_id(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj._require_home = AsyncMock(return_value=(None, 'KC'))
    obj.db.get_user_by_qq = AsyncMock(return_value={'id': 9})
    obj.db.bind_player_to_self = AsyncMock(return_value=('ok', {'id': 9, 'name': '名字'}))
    asyncio.run(collect(obj.bind_id(event('/绑定ID "ice boundary",红莲'))))
    assert [c.args[1] for c in obj.db.bind_player_to_self.await_args_list] == ['ice boundary', '红莲']
    obj.db.get_player_binding = AsyncMock(return_value={'user_id': 9, 'user_name': '名字'})
    obj.db.unbind_player = AsyncMock(return_value=1)
    asyncio.run(collect(obj.unbind_id(event('/解绑ID "ice boundary"'))))
    assert obj.db.unbind_player.await_args.args[:2] == ('KC', 'ice boundary')


def test_manage_and_search_ids_preserve_quotes_and_spaces(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj._require_home = AsyncMock(return_value=(None, 'KC'))
    obj.db.get_player_binding = AsyncMock(return_value={'user_id': None})
    obj.db.find_or_create_user = AsyncMock(return_value=9)
    obj.db.bind_player_to_user = AsyncMock()
    asyncio.run(collect(obj.admin_id(event('/管理ID "ice boundary",红莲 "ice user"'))))
    obj.db.find_or_create_user.assert_awaited_once_with('KC', 'ice user')
    assert [c.args[1] for c in obj.db.bind_player_to_user.await_args_list] == ['ice boundary', '红莲']
    obj.db.get_pool_status = AsyncMock(return_value=[])
    asyncio.run(collect(obj.search_id(event('/查ID "ice boundary"'))))
    obj.db.get_pool_status.assert_awaited_once_with('KC', 'ice boundary', 20)


@pytest.mark.parametrize('handler,command', [('record','战绩'), ('raid','踢馆'), ('trend','趋势')])
def test_player_queries_keep_full_id(plugin_module, tmp_path, handler, command):
    obj = plugin(plugin_module, tmp_path)
    obj._require_home = AsyncMock(return_value=(None, 'KC'))
    obj.db.resolve_role = AsyncMock(return_value=None)
    obj.db.get_player_record = AsyncMock(return_value={})
    obj.db.get_raid_stats_for_players = AsyncMock(return_value={})
    obj.db.get_player_trend = AsyncMock(return_value=[])
    obj.db.get_team_trend = AsyncMock(return_value=[])
    asyncio.run(collect(getattr(obj, handler)(event('/'+command+' "ice boundary" 七月'))))
    obj.db.resolve_role.assert_awaited_once_with('KC', 'ice boundary')


def test_unclosed_quote_does_not_partially_bind(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj._require_home = AsyncMock(return_value=(None, 'KC'))
    obj.db.bind_player_to_self = AsyncMock()
    response = asyncio.run(collect(obj.bind_id(event('/绑定ID 红莲 "ice boundary'))))
    assert '引号' in response[0]
    obj.db.bind_player_to_self.assert_not_awaited()


def test_team_admin_can_delete_reports_and_reset_settlement_without_qq_role(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.db.is_team_admin.return_value = True
    obj.db.delete_match = AsyncMock(return_value=True)
    obj.db.clear_settlement = AsyncMock(return_value=1)
    ev = event('/战报删除 123')
    ev.get_group.return_value = SimpleNamespace(group_owner='20002', group_admins=[])
    response = asyncio.run(collect(obj.delete(ev, '123')))
    assert '✅' in response[0]
    obj.db.delete_match.assert_awaited_once_with('100', 123)
    ev.get_message_str = lambda: '/重置结算 7月'
    response = asyncio.run(collect(obj.reset_settle(ev)))
    assert '重置好' in response[0]
    obj.db.clear_settlement.assert_awaited_once()
    assert obj.db.clear_settlement.await_args.args[0] == 'KC'


def test_plain_batch_binding_remains_available_when_full_name_does_not_exist(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_player_binding = AsyncMock(return_value=None)
    assert asyncio.run(obj._parse_player_ids('KC', '红莲 蓝大')) == ['红莲', '蓝大']
    assert asyncio.run(obj._parse_player_ids('KC', '"ice boundary" 蓝大')) == ['ice boundary', '蓝大']


def test_export_quoted_space_id_uses_same_player_filter(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.db.resolve_role = AsyncMock(return_value=None)
    obj.db.get_export_rows = AsyncMock(return_value=[{'match_id': 1}])
    obj.db.get_reports_for_export = AsyncMock(return_value=[])
    asyncio.run(collect(obj.export(event('/导出 "ice boundary" 七月 json'))))
    obj.db.resolve_role.assert_awaited_once_with('KC', 'ice boundary')
    obj.db.get_export_rows.assert_awaited_once_with('KC', '2026-07-01', '2026-07-31')


def raid_export_data():
    reports, rows = [], []
    # 友谊胜、踢馆胜负、守馆胜负、无人守馆；所有记录均从本队 a队视角筛选。
    for mid, kind, team_a, team_b, winner, left, right, score_a, score_b in [
        (1, 'friendly', 'a队', 'b队', 'a队', '云玩家', '萌新', 2, 0),
        (2, 'raid', 'a队', 'b队', 'a队', '云玩家', '萌新', 2, 0),
        (3, 'raid', 'a队', 'b队', 'b队', '云玩家', '萌新', 0, 2),
        (4, 'raid', 'b队', 'a队', 'a队', '遗老', '云玩家', 0, 2),
        (5, 'raid', 'b队', 'a队', 'b队', '遗老', '云玩家', 2, 0),
        (6, 'raid', 'a队', 'b队', 'a队', None, None, None, None),
    ]:
        duel = dict(seq=0, round_no=(1 if kind == 'friendly' else 0) if left else None,
                    player_a=left, player_b=right, score_a=score_a, score_b=score_b,
                    a_sub=False, b_sub=False, ruled=False, owner=(kind == 'raid') if left else None)
        header = (f'战队: {team_a} VS {team_b}\n时间: 2026.07.01\n规则: 人头赛\n地点: 100'
                  if kind == 'friendly' else f'{team_a}踢馆{team_b}\n规则：OCG\n2026.07.01\n踢馆开始！')
        reports.append(dict(match_id=mid, kind=kind, home_team='a队', team_a=team_a,
                            team_b=team_b, winner=winner, match_time='2026-07-01',
                            rule='人头赛' if kind == 'friendly' else 'OCG', location='100',
                            raw_text=header, submitted_by='10001', submitted_name='提交者',
                            duels=[duel] if left else []))
        rows.append(dict(match_id=mid, group_id='100', kind=kind, team_a=team_a,
                         team_b=team_b, match_time='2026-07-01', rule='OCG', location='100',
                         result='A' if (score_a or 0) > (score_b or 0) else 'B', **duel))
    return reports, rows


@pytest.mark.parametrize('fmt', ['', 'csv', 'json'])
@pytest.mark.parametrize('suffix,expected', [('', [2, 3, 4, 5, 6]), (' 胜场', [2, 4, 6]), (' 负场', [3, 5])])
@pytest.mark.parametrize('player', ['', ' "示例 玩家"'])
def test_export_raid_type_and_outcome_in_all_formats(plugin_module, tmp_path, monkeypatch, fmt, suffix, expected, player):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_group_home.return_value = 'a队'
    reports, rows = raid_export_data()
    obj.db.resolve_role = AsyncMock(return_value={'players': ['云玩家'], 'user_name': '示例 玩家'})
    obj.db.get_export_rows = AsyncMock(return_value=rows)
    obj.db.get_reports_for_export = AsyncMock(return_value=reports)
    monkeypatch.setattr(plugin_module, 'month_range', lambda month: ('2026-07-01', '2026-07-31'))
    response = asyncio.run(collect(obj.export(event('/导出 踢馆' + suffix + player + (' '+fmt if fmt else '')))))
    if player:
        expected = [mid for mid in expected if mid != 6]  # 无人守馆没有个人对局。
        obj.db.resolve_role.assert_awaited_once_with('a队', '示例 玩家')
    else:
        obj.db.resolve_role.assert_not_awaited()
    obj.db.get_reports_for_export.assert_awaited_once_with('a队', '2026-07-01', '2026-07-31')
    if fmt:
        output = Path(response[0][1].file)
        if fmt == 'json':
            data = json.loads(output.read_text())
            assert [r['match_id'] for r in data] == expected
            assert all(r['kind'] == 'raid' for r in data)
        else:
            with output.open(encoding='utf-8-sig', newline='') as stream:
                data = list(csv.DictReader(stream))
            assert [int(r['战报ID']) for r in data] == expected
            assert all(r['战报类型'] == '踢馆' for r in data)
            assert all(r['防守方馆主'] == ('是' if r['玩家A'] else '') for r in data)
    else:
        nodes = response[0][0].args[0]
        texts = [node.content[0].args[0] for node in nodes]
        assert texts == [plugin_module.lineup.report_to_text(r) for r in reports if r['match_id'] in expected]
        assert '踢馆' in response[-1] and f'{len(expected)} 份' in response[-1]


@pytest.mark.parametrize('fmt', ['', 'json'])
def test_export_raid_with_only_friendly_data_returns_no_match(plugin_module, tmp_path, fmt):
    obj = plugin(plugin_module, tmp_path)
    reports, rows = raid_export_data()
    obj.db.get_export_rows = AsyncMock(return_value=rows[:1])
    obj.db.get_reports_for_export = AsyncMock(return_value=reports[:1])
    response = asyncio.run(collect(obj.export(event('/导出 踢馆 '+fmt))))
    assert len(response) == 1 and '没有符合' in response[0]
    assert not list(tmp_path.rglob('*.json'))


def test_export_without_type_only_includes_friendly(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    reports, _ = raid_export_data()
    obj.db.get_reports_for_export = AsyncMock(return_value=reports)
    response = asyncio.run(collect(obj.export(event('/导出'))))
    nodes = response[0][0].args[0]
    assert len(nodes) == 1
    assert nodes[0].content[0].args[0] == plugin_module.lineup.report_to_text(reports[0])
    assert '友谊' in response[-1]


@pytest.mark.parametrize('fmt', ['', 'csv', 'json'])
@pytest.mark.parametrize('outcome', ['', '全部', '胜场', '负场'])
def test_default_export_outcomes_never_include_raid(plugin_module, tmp_path, fmt, outcome):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_group_home.return_value = 'a队'
    reports, rows = raid_export_data()
    obj.db.get_reports_for_export = AsyncMock(return_value=reports)
    obj.db.get_export_rows = AsyncMock(return_value=rows)
    response = asyncio.run(collect(obj.export(event(f'/战报导出 {outcome} 七月 {fmt}'))))
    if outcome == '负场':
        assert len(response) == 1 and '没有符合' in response[0]
        assert not list(tmp_path.rglob('*.csv')) and not list(tmp_path.rglob('*.json'))
    elif fmt == 'json':
        data = json.loads(Path(response[0][1].file).read_text())
        assert [r['match_id'] for r in data] == [1]
        assert data[0]['kind'] == 'friendly'
    elif fmt == 'csv':
        with Path(response[0][1].file).open(encoding='utf-8-sig', newline='') as stream:
            data = list(csv.DictReader(stream))
        assert [r['战报ID'] for r in data] == ['1']
        assert data[0]['战报类型'] == '友谊'
    else:
        nodes = response[0][0].args[0]
        assert len(nodes) == 1
        assert nodes[0].content[0].args[0] == plugin_module.lineup.report_to_text(reports[0])


@pytest.mark.parametrize('fmt', ['', 'csv', 'json'])
def test_default_export_with_only_raid_returns_no_match(plugin_module, tmp_path, fmt):
    obj = plugin(plugin_module, tmp_path)
    reports, rows = raid_export_data()
    obj.db.get_reports_for_export = AsyncMock(return_value=reports[1:])
    obj.db.get_export_rows = AsyncMock(return_value=rows[1:])
    response = asyncio.run(collect(obj.export(event('/导出 '+fmt))))
    assert len(response) == 1 and '没有符合' in response[0]


@pytest.mark.parametrize('suffix', ['', ' 7'])
@pytest.mark.parametrize('name', ['踢馆', '已发送', '未发送'])
def test_trend_keeps_raid_word_as_player_name(plugin_module, tmp_path, suffix, name):
    obj = plugin(plugin_module, tmp_path)
    obj.db.resolve_role = AsyncMock(return_value=None)
    obj.db.get_player_trend = AsyncMock(return_value=[])
    obj.db.get_team_trend = AsyncMock(return_value=[])
    asyncio.run(collect(obj.trend(event('/趋势 '+name+suffix))))
    obj.db.resolve_role.assert_awaited_once_with('KC', name)


@pytest.mark.parametrize('fmt', ['', 'csv', 'json'])
@pytest.mark.parametrize('selector', ['', '已发送', '未发送'])
@pytest.mark.parametrize('kind', ['', '踢馆'])
def test_export_separates_sent_and_unsent_without_marking(plugin_module, tmp_path, fmt, selector, kind):
    obj = plugin(plugin_module, tmp_path)
    reports, rows = raid_export_data()
    reports.append({**reports[0], 'match_id': 7, 'match_time': '2026-07-02',
                    'raw_text': reports[0]['raw_text'].replace('2026.07.01', '2026.07.02')})
    rows.append({**rows[0], 'match_id': 7, 'match_time': '2026-07-02'})
    for r in reports + rows:
        r['export_sent'] = r['match_id'] in (2, 4, 7)
    obj.db.get_reports_for_export = AsyncMock(return_value=reports)
    obj.db.get_export_rows = AsyncMock(return_value=rows)
    obj.db.set_reports_export_sent_through = AsyncMock()
    response = asyncio.run(collect(obj.export(event(f'/导出 {kind} {selector} {fmt}'))))
    expected = [('未发送', [3, 5, 6]), ('已发送', [2, 4])] if kind else [('未发送', [1]), ('已发送', [7])]
    if selector:
        expected = [(label, ids) for label, ids in expected if label == selector]
    for i, (label, ids) in enumerate(expected):
        if fmt:
            assert label in response[i][0].args[0]
            path = Path(response[i][1].file)
            assert label in path.name
            if fmt == 'json':
                data = json.loads(path.read_text())
                assert [r['match_id'] for r in data] == ids
                assert all(bool(r['export_sent']) == (label == '已发送') for r in data)
            else:
                with path.open(encoding='utf-8-sig', newline='') as f:
                    data = list(csv.DictReader(f))
                assert [int(r['战报ID']) for r in data] == ids
                assert all(r['发送状态'] == label for r in data)
        else:
            nodes = response[i][0].args[0]
            assert [int(n.name.rsplit(' ', 1)[1]) for n in nodes] == ids
            assert all(f' · {label} · ' in n.name for n in nodes)
            assert [n.content[0].args[0] for n in nodes] == [
                plugin_module.lineup.report_to_text(r) for r in reports if r['match_id'] in ids]
    assert len(response) == len(expected) + (0 if fmt else 1)
    obj.db.set_reports_export_sent_through.assert_not_awaited()


def test_export_status_groups_each_obey_node_limit(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    reports, _ = raid_export_data()
    obj.db.get_reports_for_export = AsyncMock(return_value=[
        {**reports[0], 'match_id': i+1, 'export_sent': bool(i % 2)} for i in range(203)])
    response = asyncio.run(collect(obj.export(event('/导出'))))
    assert [len(part[0].args[0]) for part in response[:-1]] == [100, 2, 100, 1]
    for part in response[:-1]:
        assert len({n.name.split(' · ')[1] for n in part[0].args[0]}) == 1


@pytest.mark.parametrize('fmt', ['csv', 'json'])
def test_export_file_group_uses_same_status_snapshot_as_filter(plugin_module, tmp_path, fmt):
    obj = plugin(plugin_module, tmp_path)
    reports, rows = raid_export_data()
    reports[1]['export_sent'] = True  # 两次查询之间完成了标记。
    rows[1]['export_sent'] = False
    obj.db.get_reports_for_export = AsyncMock(return_value=reports)
    obj.db.get_export_rows = AsyncMock(return_value=rows)
    response = asyncio.run(collect(obj.export(event('/导出 已发送 踢馆 胜场 '+fmt))))
    assert len(response) == 1
    assert '已发送' in response[0][0].args[0]
    path = Path(response[0][1].file)
    if fmt == 'json':
        assert json.loads(path.read_text())[0]['export_sent'] is True
    else:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            assert list(csv.DictReader(stream))[0]['发送状态'] == '已发送'


@pytest.mark.parametrize('sent', [True, False])
@pytest.mark.parametrize('prefix', ['', '/'])
def test_mark_export_uses_one_cutoff_id_and_team_scope(plugin_module, tmp_path, sent, prefix):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_group_home.return_value = 'a队'
    obj.db.set_reports_export_sent_through = AsyncMock(return_value=240)
    obj.db.get_reports_for_export = AsyncMock()
    command = '标记已发送' if sent else '标记未发送'
    handler = obj.mark_export_sent if sent else obj.mark_export_unsent
    ev = event(prefix+command+' 345')
    response = asyncio.run(collect(handler(ev)))
    assert '✅' in response[0] and 'a队' in response[0] and '345 及之前' in response[0]
    obj.db.set_reports_export_sent_through.assert_awaited_once_with('a队', 345, sent)
    obj.db.get_reports_for_export.assert_not_awaited()
    obj.db.insert_report.assert_not_awaited()
    ev.bot.call_action.assert_not_awaited()


@pytest.mark.parametrize('payload', ['', '0', '-1', 'abc', str(2**63), '1 2', '1,2', '1，2', '1 '*101])
@pytest.mark.parametrize('sent', [True, False])
def test_mark_invalid_or_multiple_cutoffs_never_writes(plugin_module, tmp_path, payload, sent):
    obj = plugin(plugin_module, tmp_path)
    obj.db.set_reports_export_sent_through = AsyncMock()
    command = '标记已发送' if sent else '标记未发送'
    handler = obj.mark_export_sent if sent else obj.mark_export_unsent
    response = asyncio.run(collect(handler(event('/'+command+' '+payload))))
    assert '用法' in response[0] and '截止战报ID' in response[0]
    obj.db.set_reports_export_sent_through.assert_not_awaited()


@pytest.mark.parametrize('sent', [True, False])
def test_mark_quote_without_cutoff_only_shows_usage(plugin_module, tmp_path, sent):
    obj = plugin(plugin_module, tmp_path)
    obj.db.set_reports_export_sent_through = AsyncMock()
    obj._extract_reply_reports = AsyncMock()
    command = '标记已发送' if sent else '标记未发送'
    handler = obj.mark_export_sent if sent else obj.mark_export_unsent
    ev = event('/'+command, messages=[plugin_module.Reply(id='42')])
    response = asyncio.run(collect(handler(ev)))
    assert '用法' in response[0]
    obj.db.set_reports_export_sent_through.assert_not_awaited()
    obj._extract_reply_reports.assert_not_awaited()
    ev.bot.call_action.assert_not_awaited()


@pytest.mark.parametrize('failure', [ValueError('截止战报不存在或不属于本战队喵。'), RuntimeError('DB failed')])
def test_mark_cutoff_failure_does_not_claim_success(plugin_module, tmp_path, failure):
    obj = plugin(plugin_module, tmp_path)
    obj.db.set_reports_export_sent_through = AsyncMock(side_effect=failure)
    response = asyncio.run(collect(obj.mark_export_sent(event('/标记已发送 123'))))
    assert '❌' in response[0] and '✅' not in response[0]
    obj.db.set_reports_export_sent_through.assert_awaited_once_with('KC', 123, True)


@pytest.mark.parametrize('sent', [True, False])
def test_mark_cutoff_repeated_with_zero_changed_rows_is_success(plugin_module, tmp_path, sent):
    obj = plugin(plugin_module, tmp_path)
    obj.db.set_reports_export_sent_through = AsyncMock(return_value=0)
    handler = obj.mark_export_sent if sent else obj.mark_export_unsent
    command = '标记已发送' if sent else '标记未发送'
    response = asyncio.run(collect(handler(event('/'+command+' 123'))))
    assert '✅' in response[0] and '123 及之前' in response[0]


@pytest.mark.parametrize('case', ['non_admin', 'private', 'banned', 'unbound'])
def test_mark_export_rejects_unauthorized_or_unconfigured_context(plugin_module, tmp_path, case):
    obj = plugin(plugin_module, tmp_path)
    ev = event('/标记已发送 1', group='' if case == 'private' else '100')
    obj.db.set_reports_export_sent_through = AsyncMock()
    if case == 'non_admin':
        obj._is_manager = AsyncMock(return_value=False)
    elif case == 'banned':
        obj.db.get_group_ban.return_value = True
    elif case == 'unbound':
        obj.db.get_group_home.return_value = None
    response = asyncio.run(collect(obj.mark_export_sent(ev)))
    assert response
    obj.db.set_reports_export_sent_through.assert_not_awaited()


@pytest.mark.parametrize('role', ['友谊群','战报群','主群'])
def test_help_shows_group_workflow_instead_of_command_catalog(plugin_module, tmp_path, role):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_group_chat_type_setting.return_value = role
    response = asyncio.run(collect(obj.help_cmd(event('/帮助'))))
    assert len(response) == 1
    assert role+'操作流程' in response[0]
    assert '①' in response[0] and '②' in response[0]
    assert '本群设置' not in response[0]


@pytest.mark.parametrize('home', [None, 'KC'])
def test_unconfigured_help_only_shows_match_workflow(plugin_module, tmp_path, home):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_group_home.return_value = home
    obj.db.get_group_chat_type_setting.return_value = None
    response = asyncio.run(collect(obj.help_cmd(event('/帮助'))))
    assert '友谊群操作流程' in response[0]
    assert len(response) == 1
    for unwanted in ['本群设置', '/绑定战队', '/群聊属性', '/设置战队管理员', '尚未明确设置', '默认友谊群', '战队：未绑定']:
        assert unwanted not in response[0]


@pytest.mark.parametrize('home,role', [(None,None), ('KC',None), (None,'主群')])
def test_require_home_returns_actionable_setup_for_incomplete_group(plugin_module,tmp_path,home,role):
    obj = plugin(plugin_module,tmp_path)
    obj.db.get_group_home.return_value = home
    obj.db.get_group_chat_type_setting.return_value = role
    obj.db.get_player_ranking = AsyncMock()
    response = asyncio.run(collect(obj.ranking(event('/排行'))))
    assert '本群设置' in response[0]
    assert '请联系超管或本队战队管理员' in response[0]
    assert '/设置战队管理员' not in response[0]
    obj.db.get_player_ranking.assert_not_awaited()


def test_export_also_requires_explicit_group_configuration(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.db.get_group_chat_type_setting.return_value = None
    obj.db.get_export_rows = AsyncMock()
    response = asyncio.run(collect(obj.export(event('/导出 json'))))
    assert '本群设置' in response[0]
    assert '/群聊属性 主群' in response[0]
    obj.db.get_export_rows.assert_not_awaited()


def test_missing_submission_setup_explains_report_group_steps(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.db.get_group_chat_type_setting.return_value = None
    response = asyncio.run(collect(obj.submit_report(event('/发送\n'+report()))))
    assert '本群设置' in response[0]
    assert '/群聊属性 战报群' in response[0]
    obj.db.insert_report.assert_not_awaited()


def test_setting_type_immediately_displays_selected_workflow(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db.set_group_chat_type = AsyncMock()
    for role in ['友谊群','战报群','主群']:
        response = asyncio.run(collect(obj.set_chat_type(event('/群聊属性 '+role),role)))
        assert '✅' in response[0]
        assert role+'操作流程' in response[1]
        assert '本群设置' not in response[1]
    response = asyncio.run(collect(obj.set_chat_type(event('/群聊属性 友谊群'),'友谊群')))
    assert '友谊群操作流程' in response[1]
    assert obj.db.set_group_chat_type.await_args.args[1] == '友谊群'


def test_bind_team_displays_remaining_setup_and_default_match_workflow(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.config['super_admin'] = '10001'
    obj.db.get_group_home.return_value = None
    obj.db.get_group_chat_type_setting.return_value = None
    obj.db.set_group_home = AsyncMock()
    obj.db.backfill_group_home = AsyncMock()
    response = asyncio.run(collect(obj.bind_home(event('/绑定战队 KC'))))
    assert '✅' in response[0]
    assert '战队：KC' in response[1]
    assert '友谊群操作流程' in response[1]
    assert '/群聊属性' in response[1]


def test_help_preview_and_all_do_not_change_group_type(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.db.get_group_chat_type_setting.return_value = None
    obj.db.get_group_home.return_value = None
    obj.db.set_group_chat_type = AsyncMock()
    preview = asyncio.run(collect(obj.help_cmd(event('/帮助 主群'),'主群')))
    assert '主群操作流程预览' in preview[0]
    assert '本群：主群' not in preview[0]
    all_guides = asyncio.run(collect(obj.help_cmd(event('/帮助 全部'),'全部')))
    assert len(all_guides) == 3
    assert all('操作流程' in text for text in all_guides)
    obj.db.set_group_chat_type.assert_not_awaited()
    reference = asyncio.run(collect(obj.help_cmd(event('/帮助 命令'),'命令')))
    assert '▎排表' in reference[0] and '▎用户与参赛ID' in reference[0]


def test_help_still_works_when_database_is_unavailable(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.db_ready = False
    response = asyncio.run(collect(obj.help_cmd(event('/帮助'))))
    assert '暂时读不到本群设置' in response[0]
    assert '友谊群操作流程预览' in response[1]
    obj.db.get_group_home.assert_not_awaited()
    obj.db.get_group_chat_type_setting.assert_not_awaited()


def test_help_does_not_mistake_database_error_for_unbound_group(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.db.get_group_home.side_effect = RuntimeError('offline')
    response = asyncio.run(collect(obj.help_cmd(event('/帮助'))))
    assert '暂时读不到' in response[0]
    assert '战队：未绑定' not in '\n'.join(response)


def test_disabled_group_cannot_bypass_ban_through_help(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.db.get_group_ban.return_value = True
    response = asyncio.run(collect(obj.help_cmd(event('/帮助'))))
    assert len(response) == 1 and '禁用' in response[0]
    assert '操作流程' not in response[0]


def test_private_help_is_general_and_cannot_submit(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    response = asyncio.run(collect(obj.help_cmd(event('/帮助',group=''))))
    assert '请到' in response[0] and '私聊不能提交' in response[0]
    assert '友谊群操作流程预览' in response[1]
