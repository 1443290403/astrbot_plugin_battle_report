import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import platform_policy as policy


def profile(ids, allowed=(), mode='仅开放勾选指令'):
    return {'__template_key': 'platform', 'name': '示例配置', 'platform_ids': ids,
            'mode': mode, 'allowed_commands': list(allowed)}


@pytest.mark.parametrize('platform,command,allowed', [
    ('qq_official', '排行', False), ('qq_official_webhook', '排表', False),
    ('qq_official', '友谊等车', True), ('qq_official_webhook', '等车帮助', True),
    ('aiocqhttp', '排行', True), ('aiocqhttp', '申请审批', True),
    ('aiocqhttp', '进队等车', False), ('webchat', '排表', True),
    ('webchat', '导出群成员', False), ('telegram', '友谊等车', False),
])
def test_unconfigured_connections_keep_existing_scope(platform, command, allowed):
    assert policy.command_allowed({}, platform, 'bot-A', command) is allowed


def test_different_connections_of_same_adapter_have_independent_profiles():
    config = {'platform_command_profiles': [profile(['bot-A'], ['排行']), profile(['bot-B'], ['排表'])]}
    assert policy.command_allowed(config, 'qq_official', 'bot-A', '排行')
    assert not policy.command_allowed(config, 'qq_official', 'bot-A', '排表')
    assert policy.command_allowed(config, 'qq_official', 'bot-B', '排表')
    assert not policy.command_allowed(config, 'qq_official', 'bot-B', '排行')
    assert not policy.command_allowed(config, 'qq_official', 'bot-C', '排行')


@pytest.mark.parametrize('mode,command,allowed', [
    ('全部开放', '排行', True), ('全部开放', '第N轮', True),
    ('全部屏蔽', '排行', False), ('全部屏蔽', '友谊等车', False),
    ('仅开放勾选指令', '排行', True), ('仅开放勾选指令', '排表', False),
    ('无效方式', '排行', False),
])
def test_platform_modes_are_explicit(mode, command, allowed):
    config = {'platform_command_profiles': [profile(['bot-A'], ['排行'], mode)]}
    assert policy.command_allowed(config, 'qq_official', 'bot-A', command) is allowed


def test_first_exact_profile_wins_and_empty_targets_do_not_match_everyone():
    config = {'platform_command_profiles': [profile([], mode='全部开放'),
              profile([' bot-A ', 'bot-B'], mode='全部屏蔽'), profile(['bot-A'], mode='全部开放')]}
    assert not policy.command_allowed(config, 'qq_official', 'bot-A', '排行')
    assert not policy.command_allowed(config, 'qq_official', 'bot-B', '排行')
    assert not policy.command_allowed(config, 'qq_official', 'bot', '排行')
    assert policy.command_allowed(config, 'aiocqhttp', 'other', '排行')


@pytest.mark.parametrize('platform,command', [
    ('aiocqhttp', '友谊等车'), ('telegram', '进队等车'),
    ('qq_official', '确认关联'), ('qq_official_webhook', '申请审批'),
    ('qq_official', '导出群成员'),
])
def test_allow_all_does_not_remove_existing_transport_requirements(platform, command):
    config = {'platform_command_profiles': [profile(['bot-A'], mode='全部开放')]}
    assert not policy.command_allowed(config, platform, 'bot-A', command)


def test_profile_changes_take_effect_without_a_stale_allow_cache():
    rule = profile(['bot-A'], ['排行'])
    config = {'platform_command_profiles': [rule]}
    assert policy.command_allowed(config, 'aiocqhttp', 'bot-A', '排行')
    rule['allowed_commands'].clear()
    assert not policy.command_allowed(config, 'aiocqhttp', 'bot-A', '排行')
    rule['allowed_commands'] = '排行'
    assert not policy.command_allowed(config, 'aiocqhttp', 'bot-A', '排行')


@pytest.mark.parametrize('text,key', [('/排行 七月', '排行'), ('战报排行', '排行'),
    ('/战报排行 七月', '排行'), ('/战报 文字', '发送'), ('排表队友', None)])
def test_existing_aliases_use_one_command_permission(text, key):
    assert policy.match_command(text) == key


def test_round_display_name_does_not_change_handler_permission():
    async def round_cmd():
        pass
    assert policy.handler_command(SimpleNamespace(handler=round_cmd, handler_name='/第N轮')) == '第N轮'


def test_all_legacy_queue_actions_have_one_configurable_permission():
    from official_queue import COMMANDS, parse_command
    for name in (*COMMANDS, '帮助 等车', '战报帮助 友谊等车'):
        parsed = parse_command('/' + name)
        assert parsed and policy.QUEUE_COMMANDS[parsed[0]] in policy.COMMANDS


def test_catalogue_and_configuration_cover_every_registered_command():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'main.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'BattleReportPlugin')
    expected = {}
    aliases = {}
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == 'command':
                key = ast.literal_eval(dec.args[0])
                expected[node.name] = key
                aliases[key] = key
                for kw in dec.keywords:
                    if kw.arg == 'alias':
                        aliases.update({a: key for a in ast.literal_eval(kw.value)})
    expected.update(round_cmd='第N轮', review_request='申请审批')
    assert policy.HANDLER_COMMANDS == expected
    assert policy.COMMAND_ALIASES == aliases
    schema = json.loads((root / '_conf_schema.json').read_text())['platform_command_profiles']
    assert schema['type'] == 'template_list' and schema['default'] == []
    fields = schema['templates']['platform']['items']
    assert fields['platform_ids']['default'] == []
    assert fields['allowed_commands']['options'] == list(policy.COMMANDS)
    assert len(fields['allowed_commands']['labels']) == len(policy.COMMANDS)
    assert fields['mode']['options'] == ['仅开放勾选指令', '全部开放', '全部屏蔽']
