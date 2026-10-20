"""消息边界回归：模拟 AstrBot 组件和 OneBot 返回值，直接调用真实插件 handler。

不启动 QQ 登录；装饰器桩只记录命令注册参数，数据库集成在 test_database_stats 验证。
"""
import asyncio
import importlib
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


@pytest.mark.parametrize('role', ['友谊群','战报群','主群'])
def test_help_shows_group_workflow_instead_of_command_catalog(plugin_module, tmp_path, role):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_group_chat_type_setting.return_value = role
    response = asyncio.run(collect(obj.help_cmd(event('/帮助'))))
    assert len(response) == 1
    assert role+'操作流程' in response[0]
    assert '①' in response[0] and '②' in response[0]
    assert '本群开通流程' not in response[0]


def test_unconfigured_help_defaults_to_competition_and_shows_setup(plugin_module, tmp_path):
    obj = plugin(plugin_module, tmp_path)
    obj.db.get_group_home.return_value = None
    obj.db.get_group_chat_type_setting.return_value = None
    response = asyncio.run(collect(obj.help_cmd(event('/帮助'))))
    assert '友谊群操作流程' in response[0]
    assert '本群开通流程' in response[0]
    assert '/绑定战队 KC' in response[0] and '/群聊属性' in response[0]


@pytest.mark.parametrize('home,role', [(None,None), ('KC',None), (None,'主群')])
def test_require_home_returns_actionable_setup_for_incomplete_group(plugin_module,tmp_path,home,role):
    obj = plugin(plugin_module,tmp_path)
    obj.db.get_group_home.return_value = home
    obj.db.get_group_chat_type_setting.return_value = role
    obj.db.get_player_ranking = AsyncMock()
    response = asyncio.run(collect(obj.ranking(event('/排行'))))
    assert '开通流程' in response[0]
    assert '/设置战队管理员' in response[0]
    obj.db.get_player_ranking.assert_not_awaited()


def test_export_also_requires_explicit_group_configuration(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.db.get_group_chat_type_setting.return_value = None
    obj.db.get_export_rows = AsyncMock()
    response = asyncio.run(collect(obj.export(event('/导出 json'))))
    assert '开通流程' in response[0]
    assert '/群聊属性 主群' in response[0]
    obj.db.get_export_rows.assert_not_awaited()


def test_missing_submission_setup_explains_report_group_steps(plugin_module,tmp_path):
    obj = plugin(plugin_module,tmp_path)
    obj.db.get_group_chat_type_setting.return_value = None
    response = asyncio.run(collect(obj.submit_report(event('/发送\n'+report()))))
    assert '开通流程' in response[0]
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
        assert '开通流程' not in response[1]
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
    obj.db.get_group_chat_type_setting.return_value = '友谊群'
    obj.db.set_group_chat_type = AsyncMock()
    preview = asyncio.run(collect(obj.help_cmd(event('/帮助 主群'),'主群')))
    assert '主群操作流程（预览）' in preview[0]
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
    assert '友谊群操作流程（预览）' in response[1]
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
    assert '友谊群操作流程（预览）' in response[1]
