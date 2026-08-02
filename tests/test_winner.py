"""比赛胜负判定（人头赛 / KOF）单元测试。"""

from battle_report_parser import determine_match_winner, parse_battle_report


def _report(text: str):
    r = parse_battle_report(text)
    assert not r.errors, r.errors
    return r.report


def test_kof_all_defeated():
    # 老千（DYG）连续击败悠悠球、凯撒亮、红莲 → KC 全员败北 → DYG 胜
    text = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲 2:1 牌大
凯撒亮 2:1 蓝大
悠悠球 1:2 老千
------第二轮------
凯撒亮 1:2 老千
红莲 1:2 老千"""
    assert determine_match_winner(_report(text)) == "DYG"


def test_kof_undecided():
    # 双方都未全员败北 → None
    text = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲 2:0 老千
凯撒亮 1:2 蓝大"""
    assert determine_match_winner(_report(text)) is None


def test_kof_b_side_all_defeated():
    # DYG 全员败北 → KC 胜
    text = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲 2:0 老千
凯撒亮 2:0 蓝大
悠悠球 2:0 牌大"""
    assert determine_match_winner(_report(text)) == "KC"


def test_headcount_more_wins():
    text = """战队: KC VS DYG
时间: 2026.08.01
规则: 人头赛
地点: 123
------第一轮------
红莲 2:0 牌大
凯撒亮 1:2 蓝大
悠悠球 2:1 老千"""
    assert determine_match_winner(_report(text)) == "KC"


def test_headcount_tie():
    text = """战队: KC VS DYG
时间: 2026.08.01
规则: 人头赛
地点: 123
------第一轮------
红莲 2:0 牌大
凯撒亮 1:2 蓝大"""
    assert determine_match_winner(_report(text)) is None


def test_empty_duels():
    # 无对局的战报无法判定胜负
    from battle_report_parser import BattleReport

    report = BattleReport(
        team_a="KC", team_b="DYG", match_time="2026-08-01", rule="2/3【KOF】"
    )
    assert determine_match_winner(report) is None
