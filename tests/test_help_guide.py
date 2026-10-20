"""流程帮助的行为契约：正确顺序、权限边界与历史群属性兼容。"""
import ast
from pathlib import Path
import re

import pytest

from help_guide import GROUP_TYPES, normalize_chat_type, render_group_help, render_setup_guide
from battle_report_parser import parse_battle_report
from lineup import parse_lineup, generate_template, record_from_info


@pytest.mark.parametrize('raw,expected', [(None,None), ('',None), ('未知类型',None),
    ('比赛群',None), (' 友谊群 ','友谊群'), ('战报群','战报群'), ('主群','主群')])
def test_chat_type_normalization(raw, expected):
    assert normalize_chat_type(raw) == expected


def test_default_help_guides_match_before_handoff():
    text = render_group_help(None, 'a队')
    assert '友谊群操作流程' in text
    assert '尚未明确设置' not in text and '开通' not in text
    assert '/绑定战队' not in text and '/群聊属性' not in text
    assert text.index('① 比赛开始前') < text.index('② 第一轮') < text.index('③ 需要下一轮') < text.index('④ 分出胜负')
    for command in ['/排表', '/记录 云玩家 20', '/第二轮', '/第三轮', '/帮助 战报群']:
        assert command in text
    assert '不会自动入库' in text and '复制最终完整战报到本战队的战报群' in text


def test_report_workflow_explains_delete_before_resubmission_and_ownership():
    text = render_group_help('战报群', 'a队')
    assert '开通流程' not in text
    assert text.index('② 提交') < text.index('③ 看回执') < text.index('④ 已入库') < text.index('⑤ 只想删除')
    assert '先删旧记录，再提交修正版' in text
    assert '/战报撤销' in text and '/战报删除 <战报ID>' in text
    assert '必须回到原提交群操作' in text and '普通成员不能删别人的记录' in text
    assert '目前没有原地修改命令' in text and '可能重复统计' in text
    assert '失败的先按提示修正再发' in text


def test_main_workflow_guides_identity_before_query_and_export():
    text = render_group_help('主群', 'a队')
    assert text.index('① 先找') < text.index('② 绑定') < text.index('③ 查看个人') < text.index('⑤ 查战报')
    for command in ['/查ID', '/绑定ID "示例 玩家"', '/我的ID', '/我的战绩', '/战绩', '/排行', '/趋势', '/踢馆', '/导出']:
        assert command in text
    assert 'ID 不是靠 /绑定ID 凭空创建的' in text
    assert '普通成员用 /结算' in text and '管理人员执行同一个命令会重新结算' in text


def test_setup_lists_only_needed_group_settings():
    text = render_setup_guide(None, None)
    assert '请联系超管或本队战队管理员' in text
    assert text.index('/绑定战队 <战队名>') < text.index('/群聊属性')
    for role in GROUP_TYPES:
        assert '/群聊属性 '+role in text
    for internal in ['/设置战队管理员', 'super_admin', '机器人维护者', '超管可设置任意群', '尚未明确设置']:
        assert internal not in text
    partial = render_setup_guide('a队', None)
    assert '本群战队：a队' in partial and '/绑定战队' not in partial
    partial = render_setup_guide(None, '主群')
    assert '/绑定战队 <战队名>' in partial and '/群聊属性' not in partial


def test_setup_for_submission_targets_report_group_without_repurposing_others():
    text = render_setup_guide('a队', None, required_type='战报群')
    assert '/群聊属性 战报群' in text
    assert '/群聊属性 友谊群' not in text
    assert '请到本战队已设置好的战报群提交' in text


def test_preview_never_claims_to_change_group_settings():
    text = render_group_help('主群', preview=True)
    assert '主群操作流程预览' in text
    assert '本群：主群' not in text
    assert '本群开通流程' not in text


def test_setup_preserves_bound_team_name_with_spaces():
    text = render_setup_guide('ICE TEAM', None)
    assert '本群战队：ICE TEAM' in text and '/绑定战队' not in text


def test_match_example_generates_and_records_a_real_report():
    text = render_group_help('友谊群','a队')
    roster_text = re.search(r'/排表\n(a队:[^\n]+\nb队:[^\n]+)',text).group(1)
    roster = parse_lineup(roster_text, 'KOF')
    assert not roster.errors and len(roster.teams[0][1]) == 3
    draft = generate_template(*roster.teams[0], *roster.teams[1], '2026-10-20', 'KOF', '100').template
    result = record_from_info(draft, '云玩家 20')
    assert result.ok and not parse_battle_report(result.new_text).errors


def test_guides_use_real_registered_commands():
    tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text())
    commands = {d.args[0].value for node in ast.walk(tree) if isinstance(node,(ast.AsyncFunctionDef,ast.FunctionDef))
        for d in node.decorator_list if isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute)
        and d.func.attr == 'command' and d.args and isinstance(d.args[0],ast.Constant)}
    for role in GROUP_TYPES:
        text = render_group_help(role)
        for command in re.findall(r'(?m)^/([^\s，。、：:|]+)',text):
            assert command in commands or re.fullmatch(r'第[一二三四五六七八九十\d]+轮', command), command
