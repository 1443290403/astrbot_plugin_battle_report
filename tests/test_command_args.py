"""含空格 ID 的统一输入契约，不依赖 AstrBot。"""
import pytest

from command_args import parse_command_args, parse_managed_ids, single_name
from battle_report_parser import parse_export_payload
from lineup import parse_lineup, generate_template, build_next_round, record_from_info


@pytest.mark.parametrize('text', ['"ice boundary"', "'ice boundary'", '“ice boundary”', '‘ice boundary’'])
def test_quote_forms_preserve_full_name(text):
    assert single_name(text) == 'ice boundary'
    assert [a.value for a in parse_command_args(text+' 红莲')] == ['ice boundary', '红莲']


def test_lexer_preserves_apostrophe_backslash_and_internal_spaces():
    assert [a.value for a in parse_command_args(r'''O'Connor A\B "ice  boundary"''')] == ["O'Connor", r'A\B', 'ice  boundary']
    assert single_name('ice  boundary') == 'ice  boundary'
    assert single_name(r'''"ice \"boundary\""''') == 'ice "boundary"'


@pytest.mark.parametrize('text', ['"ice boundary', '“ice boundary', '"ice"boundary', '""'])
def test_invalid_quotes_are_rejected(text):
    with pytest.raises(ValueError):
        parse_command_args(text)


def test_comma_in_quoted_id_is_not_list_separator():
    assert [a.value for a in parse_command_args('"ice,boundary",红莲，蓝大、悠悠球', split_commas=True)] == [
        'ice,boundary', '红莲', '蓝大', '悠悠球']


@pytest.mark.parametrize('text', ['"ice boundary",红莲 "角色 名"', 'ice boundary,红莲 "角色 名"'])
def test_manage_ids_preserves_legacy_and_quoted_names(text):
    assert parse_managed_ids(text) == (['ice boundary', '红莲'], '角色 名')


def test_keywords_inside_quotes_are_literal_ids():
    for name in ('csv', '胜场', '全部', '7月', '最近7天', '踢馆'):
        args = parse_export_payload('"'+name+'" 七月 json')
        assert args['player'] == name
        assert args['outcome'] == '全部' and args['fmt'] == 'json' and args['month'] == 7


@pytest.mark.parametrize('text,outcome', [('踢馆', '全部'), ('踢馆 胜场', '胜场'), ('负场 踢馆', '负场')])
def test_export_raid_is_a_type_instead_of_a_player(text, outcome):
    args = parse_export_payload(text)
    assert args['player'] is None
    assert args['kind'] == 'raid' and args['outcome'] == outcome


def test_export_raid_combines_with_quoted_player_dates_and_format():
    args = parse_export_payload('json 七月 "示例 玩家" 踢馆 胜场 最近7天')
    assert args == {'kind': 'raid', 'player': '示例 玩家', 'outcome': '胜场',
                    'month': 7, 'days': 7, 'fmt': 'json'}
    literal = parse_export_payload('"踢馆" json')
    assert literal['player'] == '踢馆' and literal['kind'] is None


@pytest.mark.parametrize('text', ['"ice boundary" 胜场 最近7天 csv', 'csv 胜场 "ice boundary" 最近7天'])
def test_export_quote_order_is_independent(text):
    assert parse_export_payload(text) == {
        'kind': None, 'player': 'ice boundary', 'outcome': '胜场', 'days': 7, 'month': None, 'fmt': 'csv'}


def test_template_record_round_and_reparse_keep_original_space_id():
    from battle_report_parser import parse_battle_report
    roster = parse_lineup('KC:"ice boundary" 红莲\nDYG:"opponent one" 蓝大', 'KOF')
    assert not roster.errors
    assert roster.teams[0][1] == ['ice boundary', '红莲']
    template = generate_template('KC', ['ice boundary'], 'DYG', ['opponent one'], '2026-08-01', 'KOF', '100').template
    recorded = record_from_info(template, '"ice boundary" 20 "opponent one"')
    assert recorded.ok
    parsed = parse_battle_report(recorded.new_text)
    assert not parsed.errors
    assert parsed.report.duels[0].player_a == 'ice boundary'
    assert parsed.report.duels[0].player_b == 'opponent one'
    for line in ['"ice boundary" "opponent one"', '"ice boundary" 20 "opponent one"', '"ice boundary" 2:0 "opponent one"']:
        appended = build_next_round(recorded.new_text, 2, [line])
        assert appended.ok
        assert 'ice boundary  ' in appended.new_text
        assert '"ice boundary"' not in appended.new_text


def test_malformed_quoted_lineup_and_record_fail_cleanly():
    roster = parse_lineup('KC:"ice boundary\nDYG:乙', 'KOF')
    assert roster.errors
    recorded = record_from_info('anything', '"ice boundary 20')
    assert not recorded.ok and '引号' in recorded.errors[0]
