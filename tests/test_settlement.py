"""月度结算（奖金）单元测试：封顶表、名次口径、月份解析、表格奖金列。"""

from datetime import datetime

import pytest
import stats
from battle_report_parser import month_range, settle_month_range
from stats import (
    MAX_REWARD_RANKS,
    RANK_BONUS_CAPS,
    build_ranking_cells,
    format_settlement,
    format_settlement_repeat,
    format_settlement_view,
    rank_aligns,
    rank_bonus,
    ranks_for,
    settlement_announcement,
    settlement_awards_changed,
)


# ---------- 奖金公式与封顶表 ----------

def test_bonus_is_points_times_three():
    assert rank_bonus(1, 10.0) == 30.0
    # 12.5 × 3 = 37.5 → **向下取整** 37（不是四舍五入的 38、也不是 37.5）
    assert rank_bonus(4, 12.5) == 37.0


def test_bonus_floors_to_integer():
    """奖金一律向下取整：只舍不入，小数尾巴抹掉。

    总积分是小数（胜场²/场次、踢馆加点都可能带小数），×3 之后几乎必定带尾巴。
    这条以前是 `round(..., 2)`，会发出 34.17 这种带分的金额。
    """
    assert rank_bonus(5, 11.39) == 34.0     # 34.17 → 34
    assert rank_bonus(5, 11.99) == 35.0     # 35.97 → 35（不四舍五入成 36）
    assert rank_bonus(1, 42.99) == 128.0    # 128.97 → 128
    assert rank_bonus(2, 33.34) == 100.0    # 100.02 → 封顶 100（触顶路径也是整数）
    assert rank_bonus(1, 43.99) == 130.0    # 131.97 → 封顶 130，floor 对上限无影响
    assert rank_bonus(3, 25.0) == 75.0      # 整数不受影响
    # 结果恒为整数值（浮点表示但无小数部分）
    for rank, pts in ((1, 7.77), (4, 12.5), (9, 4.2)):
        assert rank_bonus(rank, pts) % 1 == 0


def test_bonus_caps_by_rank():
    """封顶按名次：总分够高时正好拿该名次的上限。"""
    assert rank_bonus(1, 1000.0) == 130
    assert rank_bonus(2, 1000.0) == 100
    assert rank_bonus(3, 1000.0) == 80
    assert rank_bonus(4, 1000.0) == 50
    assert rank_bonus(5, 1000.0) == 40
    assert rank_bonus(8, 1000.0) == 25
    # 9~12 共用 15 的上限
    assert [rank_bonus(r, 1000.0) for r in (9, 10, 11, 12)] == [15, 15, 15, 15]


def test_bonus_cap_boundary():
    """刚好卡在封顶线上 / 差一点 —— ×3 之后与上限取小。"""
    assert rank_bonus(1, 130 / 3) == 130.0
    assert rank_bonus(1, 43) == 129.0
    assert rank_bonus(1, 44) == 130.0   # 132 → 封顶


def test_bonus_out_of_table_is_zero():
    """封顶表里没有的名次一律 0 分。"""
    for rank in (0, -1, 13, 99):
        assert rank_bonus(rank, 1000.0) == 0.0


def test_bonus_zero_points():
    assert rank_bonus(1, 0) == 0.0
    assert rank_bonus(1, None) == 0.0


def test_bonus_does_not_mutate_points():
    """奖金是派生的展示值，不参与 total_points（结算不改变任何排行数字）。"""
    rows = [{"player": "A", "points": 10.0, "wins": 2, "losses": 0,
             "draws": 0, "total": 2, "total_points": 10.0, "raid_points": 0}]
    before = dict(rows[0])
    rank_bonus(1, rows[0]["total_points"])
    assert rows[0] == before


def test_caps_table_covers_1_to_12():
    assert MAX_REWARD_RANKS == 12
    assert sorted(RANK_BONUS_CAPS) == list(range(1, 13))


# ---------- 名次口径 ----------

def _r(name, total_points, wins=1, total=1):
    return {"player": name, "points": total_points, "wins": wins, "losses": 0,
            "draws": 0, "total": total, "total_points": total_points,
            "raid_points": 0}


def test_ranks_for_unique():
    assert ranks_for([_r("A", 9), _r("B", 5), _r("C", 1)]) == [1, 2, 3]


def test_ranks_for_ties_share_rank():
    """三者全同才并列；并列后名次跳到 i（1,1,3）。"""
    rows = [_r("A", 7), _r("B", 7), _r("C", 1)]
    assert ranks_for(rows) == [1, 1, 3]


def test_ranks_for_tie_needs_all_three_keys():
    """total_points 相同但 wins/total 不同 → 不并列。"""
    rows = [_r("A", 7, wins=3, total=4), _r("B", 7, wins=2, total=5)]
    assert ranks_for(rows) == [1, 2]


def test_ranks_for_empty():
    assert ranks_for([]) == []


def test_ranks_for_matches_table_column():
    """ranks_for 与表格里「排名」列必须逐行一致 —— 否则榜上第 1 名会被按第 2 档发钱。"""
    rows = [_r("老千", 20), _r("牌大", 20), _r("红莲", 5)]
    cells = build_ranking_cells(rows)
    assert [c[0] for c in cells[1:]] == ranks_for(rows) == [1, 1, 3]


# ---------- 月份解析 ----------

def test_settle_month_range_past_month_this_year():
    """已结束的月份取本年。"""
    now = datetime.now()
    # 取一个一定早于当前月的月份（1 月时跳过，没有更早的月份可测）
    if now.month > 1:
        start, end, key = settle_month_range(1)
        assert start == f"{now.year}-01-01"
        assert key == f"{now.year}-01"


def test_settle_month_range_rolls_back_a_year():
    """本年尚未到来的月份回退一年（1 月结算去年 12 月靠这条）。"""
    now = datetime.now()
    if now.month < 12:
        start, end, key = settle_month_range(12)
        assert start == f"{now.year - 1}-12-01"
        assert end == f"{now.year - 1}-12-31"
        assert key == f"{now.year - 1}-12"


def test_settle_month_range_current_month_not_ended():
    """当前月落在本年（不回退），且其 end 是未来 —— 调用方据此挡住结算。"""
    now = datetime.now()
    _, end, key = settle_month_range(now.month)
    assert key == f"{now.year}-{now.month:02d}"
    assert end >= now.date().isoformat()


def test_settle_month_range_defaults_to_last_month():
    """不写月份 → 结的是**上个月**（`/结算` `/重置结算` 的默认目标）。

    没人想结算一个还没过完的当月，所以「不写月份」的自然含义就是刚过完的那个月。
    """
    now = datetime.now()
    if now.month == 1:
        pytest.skip("1 月的上个月落在去年，由下面那条跨年用例覆盖")
    _, _, key = settle_month_range()
    expect_year = now.year
    assert key == f"{expect_year}-{now.month - 1:02d}"


def test_settle_month_range_default_equals_explicit_last_month():
    """缺省与显式写上个月给出一致结果 —— 默认值不能是另一套逻辑。"""
    now = datetime.now()
    if now.month == 1:
        pytest.skip("1 月见下条")
    last = now.month - 1
    assert settle_month_range() == settle_month_range(last)


def test_settle_month_range_default_never_lands_on_current_month():
    """默认目标永远是**已结束**的月份 —— 否则会绕过「当月拒结」的挡板。"""
    _, end, key = settle_month_range()
    assert end < datetime.now().date().isoformat()
    assert int(key[5:7]) != datetime.now().month


def test_settle_month_range_no_duplicate_logic_with_month_range():
    """同一年同一月时两个函数给出一致的区间（settle 只多一个 key）。"""
    now = datetime.now()
    if now.month > 1:
        assert settle_month_range(1)[:2] == month_range(1)


# ---------- 表格奖金列 ----------

def test_cells_have_no_bonus_column_without_bonus_key():
    """未结算 / `/排行 全部` 时行里没有 bonus 键 → 仍 14 列。"""
    cells = build_ranking_cells([_r("A", 9)])
    assert len(cells[0]) == 14
    assert "奖金" not in cells[0]
    assert len(cells[1]) == 14


def test_attach_bonus_skips_unsettled_month():
    """未结算月份 `get_settlement` 返回空 dict → 一个 bonus 键都不加 → 仍 14 列。

    这条以前长在 `main.py` 的 `/排行` 分支里、**被无条件跑了一遍**：空 map 也
    照样补键，`any("bonus" in r …)` 恒为真，未结算月份照样长出第 15 列（整列 0）。
    上面那些 `build_ranking_cells` 的测试全看不见这条接线，所以把它抽成纯函数。
    """
    rows = [_r("A", 9), _r("B", 3)]
    stats.attach_bonus(rows, {})
    assert not any("bonus" in r for r in rows)
    assert len(build_ranking_cells(rows)[0]) == 14


def test_attach_bonus_adds_key_for_every_row_when_settled():
    """已结算 → **每行**都加键（榜外的队员补 0），否则那一列会有半截空。"""
    rows = [_r("A", 9), _r("B", 3)]
    stats.attach_bonus(rows, {"A": 27.5})
    assert [r["bonus"] for r in rows] == [27.5, 0.0]
    cells = build_ranking_cells(rows)
    assert len(cells[0]) == 15
    assert [r[-1] for r in cells[1:]] == ["27.5", "0"]


def test_attach_bonus_mutates_in_place_and_returns_rows():
    rows = [_r("A", 9)]
    assert stats.attach_bonus(rows, {"A": 1}) is rows


def test_cells_add_bonus_column_when_settled():
    rows = [_r("A", 9), _r("B", 3)]
    for r in rows:
        r["bonus"] = 10
    cells = build_ranking_cells(rows)
    assert len(cells[0]) == 15
    assert cells[0][-1] == "奖金"
    assert cells[1][-1] == "10"
    assert cells[2][-1] == "10"


def test_cells_bonus_column_alignment():
    """奖金列也要有对齐项，数量与表头一致。"""
    rows = [_r("A", 9)]
    rows[0]["bonus"] = 27
    cells = build_ranking_cells(rows)
    assert len(rank_aligns(len(cells[0]))) == len(cells[0])


def test_cells_bonus_fractional_and_zero():
    rows = [_r("A", 9), _r("B", 1)]
    rows[0]["bonus"] = 37.5
    rows[1]["bonus"] = 0
    cells = build_ranking_cells(rows)
    assert cells[1][-1] == "37.5"
    assert cells[2][-1] == "0"


def test_cells_raises_nothing_when_only_some_rows_have_bonus():
    """只要有一行带 bonus 就出这一列，缺的行按 0（调用方会给所有行都补键）。"""
    rows = [_r("A", 9), _r("B", 1)]
    rows[0]["bonus"] = 5
    cells = build_ranking_cells(rows)
    assert len(cells[0]) == 15
    assert cells[2][-1] == "0"


# ---------- 结算回执 ----------

def test_format_settlement_text():
    entries = [
        {"rank_no": 1, "player": "雨落", "total_points": 20.0, "bonus": 60.0},
        {"rank_no": 2, "player": "云猫", "total_points": 3.0, "bonus": 9.0},
    ]
    out = format_settlement("KC", 2026, 7, entries, 4)
    assert out.splitlines() == [
        "⚔️ KC 2026年7月 结算完成（奖励前 4 名）",
        "第1名  雨落  总积分20  奖金60",
        "第2名  云猫  总积分3  奖金9",
        "合计发放 69",
    ]


def test_format_settlement_view_is_read_only_wording():
    """只读回执（非管理员的 `/结算`）抬头是「查询」，且明说没做修改。

    和 `format_settlement` 的「结算完成」长得很像 —— 这正是要区分的地方：
    一模一样的抬头会让非管理员以为钱刚发出去。
    """
    entries = [
        {"rank_no": 1, "player": "雨落", "total_points": 20.0, "bonus": 60.0},
        {"rank_no": 2, "player": "云猫", "total_points": 3.0, "bonus": 9.0},
    ]
    out = format_settlement_view("KC", 2026, 7, entries)
    assert out.splitlines() == [
        "📋 KC 2026年7月 结算查询（奖励前 2 名）",
        "第1名  雨落  总积分20  奖金60",
        "第2名  云猫  总积分3  奖金9",
        "合计发放 69",
        "（以上是已经结算好的结果喵，这次一个字都没改～ 🐾）",
    ]
    assert "结算完成" not in out


def test_format_settlement_view_rows_match_settlement_receipt():
    """两个回执的**行体与合计逐字相同**，只有抬头与尾注不同（共用 _settlement_lines）。

    名次数那行不同是刻意的：写路径知道管理员要了几个名次（`reward_ranks`），
    读路径只有库里实际存了几行（`len(entries)`）。
    """
    entries = [
        {"rank_no": 1, "player": "雨落", "total_points": 20.5, "bonus": 61.0},
        {"rank_no": 2, "player": "云猫", "total_points": 3.33, "bonus": 9.0},
    ]
    write = format_settlement("KC", 2026, 7, entries, 4).splitlines()
    read = format_settlement_view("KC", 2026, 7, entries).splitlines()
    assert write[1:] == read[1:-1]  # 去掉各自独有的抬头/尾注后完全一致


def test_format_settlement_shows_year():
    """年份必须回显 —— 9 月跑 /结算 十二月 落的是去年，只写 12月 会误导。

    2025-12 在 2026 年 9 月是不可达的（settle_month_range 只在 month > now.month
    时回退），但函数本身按传入的 year 渲染，这里直接钉住输出形态。
    """
    out = format_settlement("KC", 2025, 12, [], 4)
    assert "2025年12月" in out


def test_format_settlement_empty_entries():
    out = format_settlement("KC", 2026, 7, [], 4)
    assert out.splitlines()[0] == "⚔️ KC 2026年7月 结算完成（奖励前 4 名）"
    assert out.splitlines()[-1] == "合计发放 0"


# ---------- 结算公告：什么时候 @ 人 ----------

def _e(rank_no, player, total_points=10.0, bonus=30.0):
    return {"rank_no": rank_no, "player": player,
            "total_points": total_points, "bonus": bonus}


def test_awards_changed_on_first_settlement():
    """该月第一次结算（库里还没有记录）→ 必 @ 人。"""
    assert settlement_awards_changed([], [_e(1, "桐人")]) is True


def test_awards_unchanged_when_rerun_identical():
    """重跑、名单一模一样 → 不 @（这是「不打扰全群」的主路径）。"""
    entries = [_e(1, "桐人"), _e(2, "红莲"), _e(3, "117"), _e(4, "别天")]
    assert settlement_awards_changed(entries, list(entries)) is False


def test_awards_changed_when_one_more_winner():
    """`/结算 8月` 之后 `/结算 8月 8`：追加了获奖人 → @。"""
    old = [_e(1, "桐人"), _e(2, "红莲")]
    new = old + [_e(3, "117"), _e(4, "别天")]
    assert settlement_awards_changed(old, new) is True


def test_awards_changed_when_shrunk_back_to_top4():
    """追加到 8 名后又跑回默认前 4 名 → 名单变了 → @。"""
    old = [_e(i, f"P{i}") for i in range(1, 9)]
    new = old[:4]
    assert settlement_awards_changed(old, new) is True


def test_awards_changed_when_rank_order_swapped():
    """名次没变但人对调了（补录战报让两人反超）→ @。"""
    old = [_e(1, "桐人"), _e(2, "红莲")]
    new = [_e(1, "红莲"), _e(2, "桐人")]
    assert settlement_awards_changed(old, new) is True


def test_awards_unchanged_ignores_bonus_drift():
    """名次、获奖人都没变，只是奖金尾数动了 → **不** @。

    口径是「获奖名次变了才 @」，不是「金额变了才 @」（见 §10.4）。
    事后补录战报常让总积分微调，为此 @ 一次全群太吵。
    """
    old = [_e(1, "桐人", total_points=47.76, bonus=130.0)]
    new = [_e(1, "桐人", total_points=47.81, bonus=130.0)]
    assert settlement_awards_changed(old, new) is False


def test_awards_unchanged_ignores_total_points_drift():
    """同上，总积分列动了也不算「名次变了」。"""
    old = [_e(1, "桐人", total_points=40.0, bonus=120.0)]
    new = [_e(1, "桐人", total_points=41.0, bonus=123.0)]
    assert settlement_awards_changed(old, new) is False


def test_awards_changed_when_same_names_different_rank():
    """同名在不同名次上 → @（名次是比对键的一半）。"""
    old = [_e(1, "桐人"), _e(2, "红莲")]
    new = [_e(1, "桐人"), _e(3, "红莲")]
    assert settlement_awards_changed(old, new) is True


# ---------- 结算公告：文案与分段 ----------

def test_announcement_ats_each_winner():
    """每个获奖人一个 `at` 段，段之间用空格隔开，头尾各有文字。"""
    segs = settlement_announcement(["桐人", "红莲", "117"])
    assert [s for s in segs if s[0] == "at"] == [
        ("at", "桐人"), ("at", "红莲"), ("at", "117")
    ]
    kinds = [k for k, _ in segs]
    assert kinds[0] == "text" and kinds[-1] == "text"
    assert " " in [v for k, v in segs if k == "text"]


def test_announcement_is_cute_and_names_the_admin():
    """猫娘口吻，且明说找谁领（战队管理员）。"""
    text = "".join(v for k, v in settlement_announcement(["桐人"]) if k == "text")
    assert "喵" in text
    assert "结算" in text
    assert "战队管理员" in text


def test_announcement_single_winner_has_no_separator():
    """只有一个人时不该多出一个孤零零的分隔空格。"""
    segs = settlement_announcement(["桐人"])
    assert [k for k, _ in segs] == ["text", "at", "text"]


def test_settlement_repeat_says_why_nobody_was_atted():
    """重跑提示要解释「为什么这次没 @ 人」，否则管理员会以为公告没发出去。"""
    note = format_settlement_repeat()
    assert "名次" in note and "一样" in note
    assert "喵" in note
    assert "@" not in note          # 它自己不能再带 @
    assert "（" not in note and "）" not in note


def test_receipt_plus_repeat_still_starts_with_the_receipt():
    """重跑时回执一个字不动，只在后面追加一句提示。"""
    entries = [_e(1, "桐人", total_points=47.76, bonus=130.0)]
    out = format_settlement("KC", 2026, 8, entries, 4) + format_settlement_repeat()
    assert out.startswith("⚔️ KC 2026年8月 结算完成（奖励前 4 名）")
    assert "第1名  桐人  总积分47.76  奖金130" in out
    assert out.rstrip().endswith("就不打扰大家了喵～ 🐾")


# ---------- 端到端拼装（不碰 DB） ----------

def test_settle_pipeline_selects_top_n_and_caps():
    """把 main.settle 的拼装逻辑走一遍：按名次发钱、超出部分不发。"""
    rows = [
        _r("第一", 100), _r("第二", 50), _r("第三", 30), _r("第四", 20),
        _r("第五", 10), _r("第六", 1),
    ]
    ranks = ranks_for(rows)
    entries = [
        {"rank_no": ranks[i], "player": r["player"],
         "total_points": r["total_points"],
         "bonus": rank_bonus(ranks[i], r["total_points"])}
        for i, r in enumerate(rows[:4])
    ]
    assert [e["player"] for e in entries] == ["第一", "第二", "第三", "第四"]
    # 100×3=300→130；50×3=150→100；30×3=90→80；20×3=60→50
    assert [e["bonus"] for e in entries] == [130, 100, 80, 50]

    # 追加到 8 名：第五、第六 也拿到（第六积分 1 → 3 分，未触上限 40）
    entries12 = [
        {"rank_no": ranks[i], "player": r["player"],
         "total_points": r["total_points"],
         "bonus": rank_bonus(ranks[i], r["total_points"])}
        for i, r in enumerate(rows[:8])
    ]
    assert len(entries12) == 6          # 只有 6 行，取不到 8 个
    assert entries12[4]["bonus"] == 30  # 10×3 = 30，未触第 5 名的 40 上限
    assert entries12[5]["bonus"] == 3   # 1×3 = 3


def test_settle_tie_across_boundary_cuts_by_row_position():
    """并列跨奖励边界时按**行位置**截断：第 4 行拿满，第 5 行没有。

    两人展示名次都是 4，但只奖励前 4 行 —— 这是已知边界（见 REQUIREMENTS §10）。
    """
    rows = [_r("A", 100), _r("B", 80), _r("C", 60), _r("D", 40), _r("E", 40)]
    ranks = ranks_for(rows)
    assert ranks == [1, 2, 3, 4, 4]
    entries = [
        {"bonus": rank_bonus(ranks[i], rows[i]["total_points"])}
        for i in range(4)
    ]
    assert [e["bonus"] for e in entries] == [130, 100, 80, 50]
    # 第 5 行同为第 4 名，却拿不到
    assert "E" not in [r["player"] for r in rows[:4]]
