"""追加轮次（/第N轮）逻辑单元测试。"""

import pytest

from lineup import build_next_round, parse_round_no, record_from_info

DRAFT = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲 2:0 老千
凯撒亮 2:0 蓝大
悠悠球 0:2 红大
------第二轮------"""

PARTIAL_ROUND = """战队: a队 VS b队
时间: 2026.10.20
规则: 2/3【KOF】
地点: 123
------第一轮------
A  2:0  SA
ASDA  0:2  DD
ASD  0:2  QW
CC  2:0  FF
------第二轮------
A  0:0  QW"""


def _round_section(text: str, round_no: int) -> list[str]:
    from battle_report_parser import ROUND_RE, _cn_to_int

    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        m = ROUND_RE.match(ln)
        if m and _cn_to_int(m.group(1)) == round_no:
            start = i
            break
    if start is None:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if ROUND_RE.match(lines[i]):
            end = i
            break
    return lines[start + 1:end]


def test_parse_round_no():
    assert parse_round_no("二") == 2
    assert parse_round_no("2") == 2
    assert parse_round_no("三") == 3
    assert parse_round_no("一") is None
    assert parse_round_no("1") is None


def test_random_match_prev_winners():
    # 第一轮胜者：A 侧 红莲/凯撒亮，B 侧 红大 → 只配对 1 场，不补 TK
    r = build_next_round(DRAFT, 2, [], seed="test")
    assert r.ok
    section = _round_section(r.new_text, 2)
    assert len(section) == 1  # min(2 A胜, 1 B胜)
    assert all("TK" not in ln for ln in section)
    assert any("红莲" in ln or "凯撒亮" in ln for ln in section)
    assert all("红大" in ln for ln in section)  # B 侧唯一胜者必定出场
    # 败者不进入
    assert all("悠悠球" not in ln for ln in section)
    assert all("老千" not in ln for ln in section)
    assert all("蓝大" not in ln for ln in section)


@pytest.mark.parametrize("seed", [None, "partial", "another-seed"])
def test_auto_round_only_pairs_survivors_not_already_in_unfinished_matches(seed):
    draft = PARTIAL_ROUND
    result = build_next_round(draft, 2, [], seed=seed)
    assert result.ok
    assert result.added_lines == ["CC  0:0  DD"]
    assert result.new_text.splitlines() == draft.splitlines() + ["CC  0:0  DD"]


def test_repeating_auto_round_does_not_add_more_matches():
    first = build_next_round(PARTIAL_ROUND, 2, [], seed="repeat")
    assert first.ok
    second = build_next_round(first.new_text, 2, [], seed="repeat-again")
    assert not second.ok
    assert second.new_text == first.new_text and second.added_lines == []
    assert any("未结束" in error for error in second.errors)


@pytest.mark.parametrize("score,busy_match,expected", [
    ("2:0", "CC  0:0  替补甲", "A  0:0  DD"),
    ("0:2", "替补乙  0:0  DD", "CC  0:0  QW"),
])
def test_finished_round_winner_can_fight_again_in_same_round(score, busy_match, expected):
    draft = PARTIAL_ROUND.replace("A  0:0  QW", f"A  {score}  QW\n{busy_match}")
    result = build_next_round(draft, 2, [], seed="winner-returns")
    assert result.ok and result.added_lines == [expected]


def test_finished_round_draw_eliminates_both_players():
    draft = PARTIAL_ROUND.replace("A  0:0  QW", "A  1:1  QW")
    result = build_next_round(draft, 2, [])
    assert result.ok and result.added_lines == ["CC  0:0  DD"]


def test_round_winner_who_later_loses_does_not_return():
    draft = PARTIAL_ROUND.replace("A  0:0  QW", "A  2:0  QW\nA  0:2  DD")
    result = build_next_round(draft, 2, [])
    assert result.ok and result.added_lines == ["CC  0:0  DD"]


def test_recording_both_matches_returns_their_winners_to_same_round_pool():
    paired = build_next_round(PARTIAL_ROUND, 2, [], seed="fill")
    left_wins = record_from_info(paired.new_text, "A 20")
    assert left_wins.ok
    right_wins = record_from_info(left_wins.new_text, "CC 02")
    assert right_wins.ok
    result = build_next_round(right_wins.new_text, 2, [])
    assert result.ok and result.added_lines == ["A  0:0  DD"]


def test_pending_players_are_scoped_to_their_own_team():
    draft = PARTIAL_ROUND.replace("\nA  ", "\n同名  ").replace("DD", "同名")
    result = build_next_round(draft, 2, [])
    assert result.ok and result.added_lines == ["CC  0:0  同名"]


def test_auto_round_stops_when_one_side_is_fully_scheduled():
    draft = PARTIAL_ROUND.replace("A  0:0  QW", "A  0:0  SA\nCC  0:0  FF")
    result = build_next_round(draft, 2, [])
    assert not result.ok
    assert result.new_text == draft and result.added_lines == []


def test_auto_later_round_also_excludes_existing_participants():
    draft = PARTIAL_ROUND.replace("第一轮", "第九轮").replace("第二轮", "第十轮")
    result = build_next_round(draft, 10, [], seed="later")
    assert result.ok and result.added_lines == ["CC  0:0  DD"]


def test_auto_round_does_not_duplicate_a_previous_winner():
    draft = PARTIAL_ROUND.split("------第二轮------")[0] + "A  2:0  FF\n------第二轮------"
    result = build_next_round(draft, 2, [], seed="unique")
    assert result.ok
    names = [line.split()[0] for line in result.added_lines]
    assert len(names) == len(set(names)) == 2


def test_random_match_no_pairable_fails():
    # 一侧无人可战（0 胜者 vs 2 胜者）→ 不补 TK，返回失败
    draft = DRAFT.replace("红莲 2:0 老千", "红莲 0:2 老千") \
                 .replace("凯撒亮 2:0 蓝大", "凯撒亮 0:2 蓝大")
    r = build_next_round(draft, 2, [])
    assert not r.ok
    assert any("可配对" in e for e in r.errors)


def test_names_only():
    r = build_next_round(DRAFT, 2, ["红莲 蓝大"])
    assert r.ok
    assert "红莲  0:0  蓝大" in r.new_text


def test_names_with_scores():
    r = build_next_round(DRAFT, 2, ["红莲  2:0  蓝大"])
    assert r.ok
    assert "红莲  2:0  蓝大" in r.new_text


def test_compact_score_20_means_2_0():
    r = build_next_round(DRAFT, 2, ["红莲 20 蓝大"])
    assert r.ok
    assert "红莲  2:0  蓝大" in r.new_text


def test_compact_score_12_means_1_2():
    r = build_next_round(DRAFT, 2, ["凯撒亮 12 老千"])
    assert r.ok
    assert "凯撒亮  1:2  老千" in r.new_text


def test_append_multiple_lines():
    r1 = build_next_round(DRAFT, 2, ["红莲  2:0  蓝大"])
    assert r1.ok
    r2 = build_next_round(r1.new_text, 2, ["凯撒亮  1:2  老千"])
    assert r2.ok
    section = _round_section(r2.new_text, 2)
    assert "红莲  2:0  蓝大" in section
    assert "凯撒亮  1:2  老千" in section


def test_prev_round_all_zero_fails():
    draft0 = DRAFT.replace("红莲 2:0 老千", "红莲 0:0 老千") \
                 .replace("凯撒亮 2:0 蓝大", "凯撒亮 0:0 蓝大") \
                 .replace("悠悠球 0:2 红大", "悠悠球 0:0 红大")
    r = build_next_round(draft0, 2, [])
    assert not r.ok
    assert any("胜者" in e for e in r.errors)


TEAM_DRAFT = """战队: TEST1 VS TEST
时间: 2026.08.02
规则: 2/3【KOF】
地点: 123
------第一轮------
YE 2:0 红大
悠悠球 1:2 空枭
随便 0:0 黄大
------第二轮------"""


def test_round_align_team_order():
    # 空枭属 TEST（右），YE 属 TEST1（左）；输入顺序反了 → 对齐为 YE 1:2 空枭
    r = build_next_round(TEAM_DRAFT, 2, ["空枭 21 YE"])
    assert r.ok
    assert "YE  1:2  空枭" in r.new_text
    assert "空枭  2:1  YE" not in r.new_text


def test_round_align_team_order_names_only():
    r = build_next_round(TEAM_DRAFT, 2, ["空枭 YE"])
    assert r.ok
    assert "YE  0:0  空枭" in r.new_text


def test_format_duel_results():
    from battle_report_parser import parse_battle_report
    from lineup import format_duel_results

    r = parse_battle_report(TEAM_DRAFT)
    assert not r.errors
    # 主体战队 TEST1（左侧）
    text = format_duel_results(r.report, "TEST1")
    assert "第一轮 YE 2:0 vs 红大 ✅ 胜" in text
    assert "悠悠球 1:2 vs 空枭 ❌ 负" in text
    # 主体战队 TEST（右侧）→ 视角翻转
    text2 = format_duel_results(r.report, "TEST")
    assert "第一轮 红大 0:2 vs YE ❌ 负" in text2


def test_format_duel_results_sub_clean_id():
    # 替补在核对信息里只显示干净 ID（替补标志在库中，不在核对信息标出）
    from battle_report_parser import parse_battle_report
    from lineup import format_duel_results

    r = parse_battle_report("""战队: TEST1 VS TEST
时间: 2026.08.02
规则: 2/3【KOF】
地点: 123
------第一轮------
YE 2:0 红大（替）
悠悠球 （替） 1:2 空枭""")
    assert not r.errors, r.errors
    assert r.report.duels[0].b_sub is True  # 替补标志已记录
    assert r.report.duels[1].a_sub is True
    text = format_duel_results(r.report, "TEST1")
    assert "YE 2:0 vs 红大 ✅ 胜" in text
    assert "悠悠球 1:2 vs 空枭 ❌ 负" in text
    assert "(替)" not in text


def test_round_partial_success():
    # 一行有效一行无效：有效照加，无效进 errors
    r = build_next_round(TEAM_DRAFT, 2, ["红莲 20 黄大", "这一行无法解析"])
    assert r.ok
    assert "红莲  2:0  黄大" in r.new_text
    assert r.errors and any("无法解析" in e for e in r.errors)


def test_no_prev_round_fails():
    # 第 3 轮但草稿只有第 1 轮 → 找不到第 2 轮前的……实际找最高 <3 的轮次即第 1 轮
    r = build_next_round(DRAFT, 3, [])
    assert r.ok  # 第 1 轮存在，可作为上一轮
    # 单独看：若没有第 1 轮之前的轮次
    draft_only_headers = "战队: A VS B\n时间: 2026.08.01\n规则: r\n地点: 1\n------第一轮------"
    r2 = build_next_round(draft_only_headers, 2, [])
    assert not r2.ok
