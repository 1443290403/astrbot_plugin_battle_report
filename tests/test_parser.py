"""战报解析器单元测试。"""

from battle_report_parser import (
    _cn_to_int,
    _normalize_date,
    _parse_month_filter,
    month_range,
    parse_battle_report,
    split_reports,
)

SAMPLE = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 435823386
------第一轮------
红莲 2:1 牌大
凯撒亮 2:1 蓝大
悠悠球 1:2 老千
------第二轮------
凯撒亮 1:2 老千
红莲 1:2 老千"""


def test_parse_ok():
    r = parse_battle_report(SAMPLE)
    assert not r.errors
    assert (r.report.team_a, r.report.team_b) == ("KC", "DYG")
    assert r.report.match_time == "2026-08-01"
    assert r.report.rule == "2/3【KOF】"
    assert r.report.location == "435823386"
    assert len(r.report.duels) == 5
    assert r.report.duels[0].round_no == 1
    assert r.report.duels[-1].player_b == "老千"


def test_fullwidth_colon():
    r = parse_battle_report("战队：KC VS DYG\n时间：2026.08.01\n------第一轮------\n红莲 2：1 牌大")
    assert not r.errors
    assert r.report.team_a == "KC"
    assert r.report.duels[0].score_a == 2


def test_missing_team_and_time():
    r = parse_battle_report("红莲 2:1 牌大")
    assert r.errors
    assert r.report is None


def test_duel_before_round():
    r = parse_battle_report("战队: A VS B\n时间: 2026.01.01\n红莲 2:1 牌大")
    assert any("轮次" in e for e in r.errors)


def test_cn_round_number():
    r = parse_battle_report("战队: A VS B\n时间: 2026.01.01\n------第二轮------\n红莲 2:1 牌大")
    assert r.report.duels[0].round_no == 2


def test_multi_score_error():
    r = parse_battle_report("战队: A VS B\n时间: 2026.01.01\n------第一轮------\n红莲 2:1 2:1 牌大")
    assert any("多个比分" in e for e in r.errors)


def test_empty():
    r = parse_battle_report("  \n  ")
    assert r.errors
    assert "为空" in r.errors[0]


def test_normalize_date_variants():
    assert _normalize_date("2026.08.01") == "2026-08-01"
    assert _normalize_date("2026-8-1") == "2026-08-01"
    assert _normalize_date("2026/08/01") == "2026-08-01"
    assert _normalize_date("2026年8月1日") == "2026-08-01"


def test_cn_to_int():
    assert _cn_to_int("一") == 1
    assert _cn_to_int("十二") == 12
    assert _cn_to_int("二十") == 20
    assert _cn_to_int("3") == 3


def test_sub_marker_stripped():
    # 替补标记剥离：只记录 ID，并标记替补
    r = parse_battle_report("战队: A VS B\n时间: 2026.01.01\n------第一轮------\n红莲(替) 2:1 蓝大（替）")
    assert not r.errors, r.errors
    d = r.report.duels[0]
    assert d.player_a == "红莲" and d.a_sub is True
    assert d.player_b == "蓝大" and d.b_sub is True


def test_sub_marker_variants():
    # 各种写法：半角/全角/前后空格/替补
    cases = [
        "红莲(替)",
        "红莲（替）",
        "红莲 （替）",
        "红莲(替补)",
        "红莲 （替补）",
        "红莲（ 替 ）",
    ]
    for name in cases:
        r = parse_battle_report(f"战队: A VS B\n时间: 2026.01.01\n------第一轮------\n{name} 2:0 蓝大")
        assert not r.errors, f"{name}: {r.errors}"
        d = r.report.duels[0]
        assert d.player_a == "红莲", f"{name} → {d.player_a!r}"
        assert d.a_sub is True, name


def test_ruled_marker_stripped():
    # 判罚落败标记剥离：只记录 ID，并用单字段 ruled 标记本场被规则
    r = parse_battle_report("战队: A VS B\n时间: 2026.01.01\n------第一轮------\n红莲(规则) 1:2 蓝大（规则）")
    assert not r.errors, r.errors
    d = r.report.duels[0]
    assert d.player_a == "红莲" and d.player_b == "蓝大"
    assert d.ruled is True
    assert d.a_sub is False and d.b_sub is False


def test_ruled_marker_variants():
    cases = ["红莲(规则)", "红莲（规则）", "红莲 （规则）", "红莲（ 规则 ）"]
    for name in cases:
        r = parse_battle_report(f"战队: A VS B\n时间: 2026.01.01\n------第一轮------\n{name} 1:2 蓝大")
        assert not r.errors, f"{name}: {r.errors}"
        d = r.report.duels[0]
        assert d.player_a == "红莲", f"{name} → {d.player_a!r}"
        assert d.ruled is True, name


def test_ruled_and_sub_together():
    # 替补+判罚同时存在：两个标记都剥离
    r = parse_battle_report("战队: A VS B\n时间: 2026.01.01\n------第一轮------\n红莲（替）（规则） 1:2 蓝大")
    assert not r.errors, r.errors
    d = r.report.duels[0]
    assert d.player_a == "红莲" and d.a_sub is True and d.ruled is True


def test_sub_both_sides():
    # 双方都是替补：耗子 （替） 2:0 蓝大（替）
    r = parse_battle_report("战队: A VS B\n时间: 2026.01.01\n------第一轮------\n耗子 （替） 2:0 蓝大（替）")
    assert not r.errors, r.errors
    d = r.report.duels[0]
    assert d.player_a == "耗子" and d.a_sub is True
    assert d.player_b == "蓝大" and d.b_sub is True


def test_no_marker_not_sub():
    # 无标记不误判
    r = parse_battle_report("战队: A VS B\n时间: 2026.01.01\n------第一轮------\n红莲 2:0 蓝大")
    assert not r.errors
    d = r.report.duels[0]
    assert d.player_a == "红莲" and d.a_sub is False
    assert d.player_b == "蓝大" and d.b_sub is False


def test_parse_month_filter():
    import datetime

    assert _parse_month_filter("队伍") == ("队伍", None)
    # 新写法：末尾直接写 X月
    assert _parse_month_filter("队伍 七月") == ("队伍", 7)
    assert _parse_month_filter("红莲 7月") == ("红莲", 7)
    assert _parse_month_filter("红莲 十二月") == ("红莲", 12)
    assert _parse_month_filter("红莲 12月") == ("红莲", 12)
    # 纯数字（不带 月）不识别为月份（避免与趋势 最近N天 冲突）
    assert _parse_month_filter("红莲 7") == ("红莲 7", None)
    assert _parse_month_filter("红莲 最近7天") == ("红莲 最近7天", None)
    # 旧写法：时间=X月（含全角等号 ＝）仍兼容
    assert _parse_month_filter("队伍 时间=7月") == ("队伍", 7)
    assert _parse_month_filter("队伍 时间：七月") == ("队伍", 7)
    assert _parse_month_filter("队伍 时间=7") == ("队伍", 7)
    assert _parse_month_filter("红莲 时间 = 7月") == ("红莲", 7)
    assert _parse_month_filter("红莲 时间＝十二月") == ("红莲", 12)
    assert _parse_month_filter("红莲 30 时间=12月") == ("红莲 30", 12)
    assert _parse_month_filter("") == ("", None)
    # 时间参数出现在中间而非末尾时不提取
    assert _parse_month_filter("时间=7月 队伍") == ("时间=7月 队伍", None)
    assert _parse_month_filter("七月 队伍") == ("七月 队伍", None)


def test_month_range():
    import datetime

    now = datetime.datetime.now()
    s, e = month_range(7)
    assert s == f"{now.year}-07-01" and e == f"{now.year}-07-31"
    s2, e2 = month_range(12)
    assert s2 == f"{now.year}-12-01" and e2 == f"{now.year}-12-31"
    # 非法/缺省 → 当前月
    s3, e3 = month_range(None)
    assert s3 == f"{now.year}-{now.month:02d}-01"
    s4, _ = month_range(0)
    assert s4 == f"{now.year}-{now.month:02d}-01"


def test_parse_uppercase_team():
    # 战队名含字母时统一转大写
    r = parse_battle_report("战队: dyg VS kc\n时间: 2026.01.01\n------第一轮------\n红莲 2:0 老千")
    assert not r.errors
    assert r.report.team_a == "DYG"
    assert r.report.team_b == "KC"


def test_split_reports():
    text = (
        "战队: A VS B\n时间: 2026.01.01\n规则: 人头赛\n地点: 1\n------第一轮------\n红莲 2:0 蓝大\n"
        "战队: C VS D\n时间: 2026.01.02\n规则: 2/3【KOF】\n地点: 1\n------第一轮------\n凯撒亮 1:2 老千"
    )
    chunks = split_reports(text)
    assert len(chunks) == 2
    assert chunks[0].startswith("战队: A VS B")
    assert chunks[1].startswith("战队: C VS D")
    # 单份
    assert len(split_reports("战队: A VS B\n时间: 2026.01.01\n红莲 2:0 蓝大")) == 1
    assert split_reports("") == []
