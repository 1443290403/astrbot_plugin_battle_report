"""战报解析器单元测试。"""

from battle_report_parser import _cn_to_int, _normalize_date, parse_battle_report, split_reports

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
