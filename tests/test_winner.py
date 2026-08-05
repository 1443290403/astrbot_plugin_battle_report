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


def test_kof_substitute_does_not_increase_roster():
    # 替补：FH 第一轮出战 5 人，咸鱼(替) 替补不增加可出战人数；
    # 累计落败（各选手最后一场为负）达 5 人 → FH 无人可战 → KC 胜
    text = """战队: KC VS FH
时间: 2026.07.30
规则: 2/3【KOF】
地点: 435823386
------第一轮------
桐人  1:2  灯球
生命  2:0  小宝
幻梦  0:2  寒烟柔
tsuki 0:2  笑君
知更  2:1  秋风
------第二轮------
生命  2:0  笑君
生命  1:2  寒烟柔
知更  2:1  咸鱼(替)
知更  2:1  寒烟柔"""
    assert determine_match_winner(_report(text)) == "KC"


def test_kof_same_name_regular_and_sub_distinct():
    # 正选 红莲 与替补 红莲(替) 是不同的出战名额：正选红莲落败计入，替补获胜不抵消
    text = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲 0:2 老千
凯撒亮 0:2 蓝大
悠悠球 0:2 牌大
------第二轮------
红莲(替) 2:0 老千"""
    # KC 第一轮 3 人全部落败（3 >= 3）→ 无人可战；DYG 老千落败仅 1 人 → DYG 胜
    assert determine_match_winner(_report(text)) == "DYG"


def test_kof_zero_zero_unfinished_undetermined():
    # 0:0 = 未完结：双方该对局选手仍可出战 → 双方都有存活选手 → 胜负未定（战报不可提交）
    text = """战队: KC VS DYG
时间: 2026.08.04
规则: 2/3【KOF】
地点: 1060889761
------第一轮------
你好  0:0  阿斯顿
TSUKI  2:0  宏大
战神  0:2  自大
红莲(替)  2:0  黄大
------第二轮------
红莲(替)  2:0  黄色(替)"""
    assert determine_match_winner(_report(text)) is None


def test_kof_tk_placeholder_zero_zero_undetermined():
    # TK 占位 0:0 同样视为未打：KC 与 DYG 都有存活选手 → 胜负未定
    text = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲  0:2  老千
凯撒亮  0:0  TK
------第二轮------
红莲  2:0  老千"""
    assert determine_match_winner(_report(text)) is None


def test_kof_draw_counts_as_out():
    # 1:1 非零平分 = 已经打完的平局 → 不可出战（计入落败）；蓝大 1:1 计入 → DYG 灭 → KC 胜
    text = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲  2:0  老千
凯撒亮  1:1  蓝大"""
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


def test_kof_ruled_report_determines():
    # 含判罚(规则)标记的战报：判罚方比分更低、视为落败 → KC 全员不可出战 → DYG 胜
    text = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲(规则) 0:2 老千
凯撒亮 0:2 蓝大
悠悠球 0:2 牌大"""
    r = _report(text)
    assert r.duels[0].ruled is True  # 规则标记已记录
    assert determine_match_winner(r) == "DYG"


def test_headcount_ruled_report_determines():
    # 人头赛：含判罚(规则)标记，判罚方比分更低 → 按比分判定 → DYG 胜
    text = """战队: KC VS DYG
时间: 2026.08.01
规则: 人头赛
地点: 123
------第一轮------
红莲(规则) 1:2 老千
凯撒亮 0:2 蓝大
悠悠球 2:0 牌大"""
    r = _report(text)
    assert r.duels[0].ruled is True
    assert determine_match_winner(r) == "DYG"


def test_empty_duels():
    # 无对局的战报无法判定胜负
    from battle_report_parser import BattleReport

    report = BattleReport(
        team_a="KC", team_b="DYG", match_time="2026-08-01", rule="2/3【KOF】"
    )
    assert determine_match_winner(report) is None
