"""追加轮次（/第N轮）逻辑单元测试。"""

from lineup import build_next_round, parse_round_no

DRAFT = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲 2:0 老千
凯撒亮 2:0 蓝大
悠悠球 0:2 红大
------第二轮------"""


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
    # 第一轮胜者：A 侧 红莲/凯撒亮，B 侧 红大
    r = build_next_round(DRAFT, 2, [], seed="test")
    assert r.ok
    section = _round_section(r.new_text, 2)
    assert len(section) == 2  # max(2 A胜, 1 B胜)
    assert any("TK" in ln for ln in section)  # B 侧只有 1 名胜者，补 TK
    assert any("红莲" in ln or "凯撒亮" in ln for ln in section)
    # 败者不进入
    assert all("悠悠球" not in ln for ln in section)
    assert all("老千" not in ln for ln in section)
    assert all("蓝大" not in ln for ln in section)


def test_names_only():
    r = build_next_round(DRAFT, 2, ["红莲 蓝大"])
    assert r.ok
    assert "红莲 0:0 蓝大" in r.new_text


def test_names_with_scores():
    r = build_next_round(DRAFT, 2, ["红莲 2:0 蓝大"])
    assert r.ok
    assert "红莲 2:0 蓝大" in r.new_text


def test_compact_score_20_means_2_0():
    r = build_next_round(DRAFT, 2, ["红莲 20 蓝大"])
    assert r.ok
    assert "红莲 2:0 蓝大" in r.new_text


def test_compact_score_12_means_1_2():
    r = build_next_round(DRAFT, 2, ["凯撒亮 12 老千"])
    assert r.ok
    assert "凯撒亮 1:2 老千" in r.new_text


def test_append_multiple_lines():
    r1 = build_next_round(DRAFT, 2, ["红莲 2:0 蓝大"])
    assert r1.ok
    r2 = build_next_round(r1.new_text, 2, ["凯撒亮 1:2 老千"])
    assert r2.ok
    section = _round_section(r2.new_text, 2)
    assert "红莲 2:0 蓝大" in section
    assert "凯撒亮 1:2 老千" in section


def test_prev_round_all_zero_fails():
    draft0 = DRAFT.replace("红莲 2:0 老千", "红莲 0:0 老千") \
                 .replace("凯撒亮 2:0 蓝大", "凯撒亮 0:0 蓝大") \
                 .replace("悠悠球 0:2 红大", "悠悠球 0:0 红大")
    r = build_next_round(draft0, 2, [])
    assert not r.ok
    assert any("胜者" in e for e in r.errors)


def test_no_prev_round_fails():
    # 第 3 轮但草稿只有第 1 轮 → 找不到第 2 轮前的……实际找最高 <3 的轮次即第 1 轮
    r = build_next_round(DRAFT, 3, [])
    assert r.ok  # 第 1 轮存在，可作为上一轮
    # 单独看：若没有第 1 轮之前的轮次
    draft_only_headers = "战队: A VS B\n时间: 2026.08.01\n规则: r\n地点: 1\n------第一轮------"
    r2 = build_next_round(draft_only_headers, 2, [])
    assert not r2.ok
