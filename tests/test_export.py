"""导出功能单元测试：format_report / report_to_text 还原 + filter_report_outcome 过滤。"""

from lineup import filter_report_outcome, format_report, report_to_text


SAMPLE_DUELS = [
    {"seq": 0, "round_no": 1, "player_a": "红莲", "score_a": 2, "player_b": "牌大", "score_b": 1},
    {"seq": 1, "round_no": 1, "player_a": "凯撒亮", "score_a": 2, "player_b": "蓝大", "score_b": 1},
    {"seq": 2, "round_no": 1, "player_a": "悠悠球", "score_a": 1, "player_b": "老千", "score_b": 2},
    {"seq": 3, "round_no": 2, "player_a": "凯撒亮", "score_a": 1, "player_b": "老千", "score_b": 2},
    {"seq": 4, "round_no": 2, "player_a": "红莲", "score_a": 1, "player_b": "老千", "score_b": 2},
]


def test_format_report_basic():
    """兜底还原：头部 + 轮次分隔 + 对局行双空格。"""
    text = format_report("KC", "DYG", "2026-08-01", "2/3【KOF】", "435823386", SAMPLE_DUELS)
    assert text == (
        "战队: KC VS DYG\n"
        "时间: 2026-08-01\n"
        "规则: 2/3【KOF】\n"
        "地点: 435823386\n"
        "------第一轮------\n"
        "红莲  2:1  牌大\n"
        "凯撒亮  2:1  蓝大\n"
        "悠悠球  1:2  老千\n"
        "------第二轮------\n"
        "凯撒亮  1:2  老千\n"
        "红莲  1:2  老千"
    )


def test_format_report_round_headers_chinese():
    """轮次中文数字（含十位）。"""
    duels = [
        {"seq": 0, "round_no": 12, "player_a": "A", "score_a": 2, "player_b": "B", "score_b": 0},
        {"seq": 1, "round_no": 23, "player_a": "C", "score_a": 0, "player_b": "D", "score_b": 2},
    ]
    text = format_report("X", "Y", "2026-08-01", "人头赛", "1", duels)
    assert "------第十二轮------" in text
    assert "------第二十三轮------" in text


def test_format_duels_block_sub_marker():
    """对局段重建时替补玩家名后标 (替)。"""
    from lineup import format_duels_block

    duels = [
        {"seq": 0, "round_no": 1, "player_a": "耗子", "score_a": 2, "player_b": "蓝大", "score_b": 0, "a_sub": True, "b_sub": True},
        {"seq": 1, "round_no": 1, "player_a": "红莲", "score_a": 2, "player_b": "秋风", "score_b": 1, "a_sub": False, "b_sub": False},
    ]
    text = format_duels_block(duels)
    assert "耗子(替)  2:0  蓝大(替)" in text
    assert "红莲  2:1  秋风" in text


def test_format_duels_block_ruled_marker():
    """对局段重建时：ruled 字段为真则给败方（比分低的一侧）补 (规则)。"""
    from lineup import format_duels_block

    duels = [
        {"seq": 0, "round_no": 1, "player_a": "红莲", "score_a": 1, "player_b": "老千", "score_b": 2,
         "a_sub": False, "b_sub": False, "ruled": True},
        {"seq": 1, "round_no": 1, "player_a": "凯撒亮", "score_a": 2, "player_b": "蓝大", "score_b": 1,
         "a_sub": True, "b_sub": False, "ruled": True},
    ]
    text = format_duels_block(duels)
    assert "红莲(规则)  1:2  老千" in text       # 败方（A，比分低）补 (规则)
    assert "凯撒亮(替)  2:1  蓝大(规则)" in text  # 替补补 (替)、败方（B）补 (规则)
    # 未规则的对局不补
    duels[0]["ruled"] = False
    assert "红莲  1:2  老千" in format_duels_block(duels)


def test_format_report_round_order():
    """乱序对局按轮次分组后输出顺序正确。"""
    duels = [
        {"seq": 0, "round_no": 2, "player_a": "C", "score_a": 1, "player_b": "D", "score_b": 2},
        {"seq": 1, "round_no": 1, "player_a": "A", "score_a": 2, "player_b": "B", "score_b": 0},
    ]
    text = format_report("X", "Y", "2026-08-01", "人头赛", "1", duels)
    assert text.index("第一轮") < text.index("第二轮")
    assert text.index("第二轮") < text.index("C  1:2  D")


def test_report_to_text_raw_header_preserved_duels_two_spaces():
    """raw_text 头部逐字保留（含点号日期），单空格对局行被重建为双空格。"""
    raw = (
        "战队: KC VS DYG\n"
        "时间: 2026.08.01\n"
        "规则: 2/3【KOF】\n"
        "地点: 435823386\n"
        "------第一轮------\n"
        "红莲 2:1 牌大\n"
        "------第二轮------"
    )
    r = {"raw_text": raw, "duels": SAMPLE_DUELS}
    text = report_to_text(r)
    assert text.startswith(
        "战队: KC VS DYG\n时间: 2026.08.01\n规则: 2/3【KOF】\n地点: 435823386\n"
    )
    assert "红莲  2:1  牌大" in text
    assert "红莲  1:2  老千" in text
    # 原始单空格行被双空格替代
    assert "红莲 2:1 牌大" not in text


def test_report_to_text_no_raw():
    """raw_text 缺失时全量结构化兜底，对局行仍双空格。"""
    r = {
        "raw_text": "",
        "team_a": "KC", "team_b": "DYG", "match_time": "2026-08-01",
        "rule": "2/3【KOF】", "location": "435823386",
        "duels": SAMPLE_DUELS,
    }
    text = report_to_text(r)
    assert text.startswith("战队: KC VS DYG\n时间: 2026-08-01\n")
    assert "红莲  2:1  牌大" in text
    assert "红莲  1:2  老千" in text


def _report(mid, team_a, team_b, winner, home_team):
    return {
        "match_id": mid, "team_a": team_a, "team_b": team_b,
        "winner": winner, "home_team": home_team, "duels": [],
    }


def test_filter_all():
    reports = [
        _report(1, "KC", "DYG", "KC", "KC"),
        _report(2, "KC", "DYG", "DYG", "KC"),
    ]
    assert len(filter_report_outcome(reports, "全部")) == 2
    assert len(filter_report_outcome(reports, "其他")) == 2  # 未知范围按全部


def test_filter_win():
    reports = [
        _report(1, "KC", "DYG", "KC", "KC"),   # 主体获胜
        _report(2, "KC", "DYG", "DYG", "KC"),  # 主体战败
        _report(3, "KC", "DYG", "", "KC"),     # 胜负未定
        _report(4, "KC", "DYG", "KC", "ZZZ"),  # 主体不在对阵内
    ]
    assert [r["match_id"] for r in filter_report_outcome(reports, "胜场")] == [1]


def test_filter_loss():
    reports = [
        _report(1, "KC", "DYG", "KC", "KC"),
        _report(2, "KC", "DYG", "DYG", "KC"),
        _report(3, "KC", "DYG", "", "KC"),
    ]
    assert [r["match_id"] for r in filter_report_outcome(reports, "负场")] == [2]


def test_filter_home_empty():
    reports = [_report(1, "KC", "DYG", "KC", "")]
    assert filter_report_outcome(reports, "胜场") == []
    assert filter_report_outcome(reports, "负场") == []
