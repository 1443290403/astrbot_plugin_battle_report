"""帮助分类与渲染单元测试。"""

from stats import (
    ALL_SECTIONS,
    CHAT_TYPE_SECTIONS,
    HELP_SECTIONS,
    build_team_panel,
    raid_defense_points,
    render_help,
    total_points,
)


def test_chat_type_sections():
    assert CHAT_TYPE_SECTIONS["友谊群"] == ["排表", "追加轮次", "记录比分"]
    assert CHAT_TYPE_SECTIONS["战报群"] == ["提交战报", "管理", "查询"]
    assert CHAT_TYPE_SECTIONS["主群"] == ["查询", "用户与参赛ID"]


def test_all_sections_excludes_super():
    assert "超级管理" not in ALL_SECTIONS
    assert set(ALL_SECTIONS) == set(HELP_SECTIONS) - {"超级管理"}


def test_render_help_group_sections():
    text = render_help(["排表", "查询"])
    assert "▎排表" in text
    assert "▎查询" in text
    assert "默认本月" in text  # 查询栏目说明
    assert "群属性：/群聊属性" in text
    assert "/帮助 全部" in text
    assert "▎超级管理" not in text


def test_render_help_head_and_tail_are_catgirl():
    """抬头与收尾带猫娘语气；**命令字面量逐字不变**（§5-M10）。

    语气只进句子：栏目名、命令语法、`群属性：/群聊属性`、`/帮助 全部` 都是用户要
    照着敲的，一个字都不能动。
    """
    text = render_help(["排表", "查询"])
    head, *_, tail = text.strip().splitlines()
    assert "喵" in head
    assert "喵" in tail
    for literal in ("▎排表", "群属性：/群聊属性", "/帮助 全部", "默认本月"):
        assert literal in text


def test_render_help_super():
    text = render_help(["超级管理"])
    assert "▎超级管理" in text
    assert "群聊属性" in text
    assert "▎排表" not in text


# ---------- 排行图片「战队战绩」面板 ----------

def _panel_record(**kw):
    """get_home_team_record 返回值的最小替身。"""
    rec = {
        "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_points": 0.0,
        "hold": 0, "first_round": 0, "attack_points": 0,
    }
    rec.update(kw)
    return rec


def test_build_team_panel_labels_in_order():
    """8 行、左标签逐字且顺序固定（用户指定：总场数在最上、积分在最下）。"""
    panel = build_team_panel(_panel_record())
    assert [lb for lb, _ in panel] == [
        "总场数", "守馆首轮", "守馆", "踢馆", "胜场", "负场", "胜率", "积分",
    ]
    assert len(panel) == 8


def test_build_team_panel_hold_is_ceil_not_floor():
    """hold=2 / first_round=2 → 各得 1 分（ceil），不是 0（floor）。

    `_ceil_div3` 写成 `n // 3` 就会在这里得 0 —— 这正是 v1.14 用户报过的 bug。
    """
    panel = dict(build_team_panel(_panel_record(hold=2, first_round=2)))
    assert panel["守馆"] == "1"
    assert panel["守馆首轮"] == "1"


def test_build_team_panel_ignores_monthly_cap():
    """三行守馆/踢馆显示**各自赚了多少**，走的是原始分量，不套 10 分月度封顶。

    hold=33→11、first_round=6→2、attack_points=7，三行和 20；
    而实得的踢馆总积分受封顶只有 10+7=17，所以 20 > 17。
    这是既定口径（封顶只体现在「积分」行），不是 bug。
    """
    rec = _panel_record(
        hold=33, first_round=6, shutdown=0, attack_points=7,
        wins=10, losses=5, total=15, win_rate=66.7,
        total_points=total_points(6.0, 7 + raid_defense_points(33, 6, 0)),
    )
    panel = dict(build_team_panel(rec))
    assert panel["守馆"] == "11"
    assert panel["守馆首轮"] == "2"
    assert panel["踢馆"] == "7"

    raw_sum = 11 + 2 + 7
    earned = float(panel["积分"]) - 6.0  # 积分行 − 友谊积分
    assert raw_sum > earned  # 封顶吃掉的部分不体现在面板上
    assert panel["积分"] == "23"


def test_build_team_panel_empty_record():
    """缺字段 / 空 dict 不炸，退回 0。"""
    panel = dict(build_team_panel({}))
    assert panel["总场数"] == "0"
    assert panel["胜场"] == "0"
    assert panel["负场"] == "0"
    assert panel["胜率"] == "0.0%"   # 带百分号
    assert panel["积分"] == "0"


def test_build_team_panel_integral_points_drop_decimals():
    """「积分」用 :g，与 build_ranking_cells 的「总积分」列同格式。

    total_points 是 float（63.0），渲染成 "63" 而不是 "63.0"，
    否则面板和右侧表格会出现两种小数位。
    """
    panel = dict(build_team_panel(_panel_record(total_points=63.0)))
    assert panel["积分"] == "63"
    assert dict(build_team_panel(_panel_record(total_points=110.56)))["积分"] == "110.56"


def test_build_team_panel_win_rate_one_decimal():
    panel = dict(build_team_panel(_panel_record(win_rate=59.848)))
    assert panel["胜率"] == "59.8%"
