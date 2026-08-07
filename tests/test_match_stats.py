"""比赛级统计（无双/友谊次数）与排行表格格式化单元测试。"""

from stats import (
    _disp_width,
    build_ranking_cells,
    compute_match_stats,
    format_player_ranking,
    format_player_record,
)


def _d(seq, a, sa, sb, b, team_a="KC", team_b="DYG"):
    """构造一场对局 dict（字段与 database.get_player_match_stats 传入的一致）。"""
    return {
        "seq": seq,
        "score_a": sa,
        "score_b": sb,
        "player_a_team": team_a,
        "player_b_team": team_b,
        "resolved_a": a,
        "resolved_b": b,
    }


SAMPLE = [
    _d(0, "红莲", 2, 1, "牌大"),
    _d(1, "凯撒亮", 2, 1, "蓝大"),
    _d(2, "悠悠球", 1, 2, "老千"),
    _d(3, "凯撒亮", 1, 2, "老千"),
    _d(4, "红莲", 1, 2, "老千"),
]


def test_format_player_record_with_friendship():
    """个人战绩文本包含友谊次数，缺省按 0。"""
    out = format_player_record("红莲", {"wins": 3, "losses": 1, "draws": 0, "total": 4, "friendship": 3})
    assert out == "📊 红莲 战绩：胜3 负1 平0  总4  友谊3  积分2.25  胜率75.0%"
    # 缺 friendship 字段 → 按 0
    out2 = format_player_record("红莲", {"wins": 1, "losses": 0, "draws": 0, "total": 1})
    assert "友谊0" in out2


def test_ace_hit():
    # 老千队友(牌大/蓝大)均阵亡，老千击败 KC 全部三人
    st = compute_match_stats(SAMPLE, winner="DYG")
    assert st["老千"]["wushuang"] == 1
    assert st["老千"]["friendship"] == 1
    assert len(st) == 6
    for k, v in st.items():
        if k != "老千":
            assert v["wushuang"] == 0, k


def test_ace_no_teammate_1v1():
    st = compute_match_stats([_d(0, "红莲", 2, 1, "老千")], winner="KC")
    assert st["红莲"]["wushuang"] == 0  # 无队友不算无双
    assert st["红莲"]["friendship"] == 1


def test_ace_teammate_not_all_dead():
    # 凯撒亮最后一场是胜 → 红莲非唯一幸存者
    duels = [
        _d(0, "红莲", 2, 0, "老千"),
        _d(1, "凯撒亮", 2, 0, "牌大"),
        _d(2, "红莲", 2, 0, "牌大"),
    ]
    st = compute_match_stats(duels, winner="KC")
    assert st["红莲"]["wushuang"] == 0
    assert st["凯撒亮"]["wushuang"] == 0


def test_ace_not_beat_all_opponents():
    # 红莲只击败老千，未击败牌大 → 非无双；牌大击败全部 KC → 无双
    duels = [
        _d(0, "红莲", 2, 0, "老千"),       # KC 红莲 胜 DYG 老千
        _d(1, "凯撒亮", 0, 2, "牌大"),     # KC 凯撒亮 负 DYG 牌大
        _d(2, "红莲", 0, 2, "牌大"),       # KC 红莲 负 DYG 牌大
    ]
    st = compute_match_stats(duels, winner="DYG")
    assert st["红莲"]["wushuang"] == 0
    assert st["牌大"]["wushuang"] == 1


def test_ace_zero_zero_excluded():
    # 0:0 占位被忽略；0:0-only 玩家不出现在结果中，不影响老千无双
    duels = SAMPLE + [
        _d(5, "悠悠球", 0, 0, "蓝大"),
        _d(6, "战神", 0, 0, "牌大"),
    ]
    st = compute_match_stats(duels, winner="DYG")
    assert st["老千"]["wushuang"] == 1
    assert "战神" not in st


def test_ace_draw_eliminates_teammate():
    # 队友凯撒亮非零平局 → 视为阵亡，红莲仍无双
    duels = [
        _d(0, "红莲", 2, 0, "老千"),
        _d(1, "凯撒亮", 1, 1, "牌大"),
        _d(2, "红莲", 2, 0, "牌大"),
    ]
    st = compute_match_stats(duels, winner="KC")
    assert st["红莲"]["wushuang"] == 1


def test_ace_winner_guard():
    # 守卫：仅给胜方记无双；winner 为空时关闭守卫
    assert compute_match_stats(SAMPLE, winner="KC")["老千"]["wushuang"] == 0
    assert compute_match_stats(SAMPLE, winner="DYG")["老千"]["wushuang"] == 1
    assert compute_match_stats(SAMPLE, winner="")["老千"]["wushuang"] == 1


def test_empty():
    assert compute_match_stats([]) == {}


# ---------- 排行表格 ----------

def test_format_table_headers_and_no_tip():
    rows = [
        {"player": "老千", "points": 3.0, "wins": 3, "losses": 0, "draws": 0, "total": 3,
         "friendship": 1, "wushuang": 1},
        {"player": "红莲", "points": 1.0, "wins": 1, "losses": 1, "draws": 0, "total": 2,
         "friendship": 1, "wushuang": 0},
    ]
    out = format_player_ranking(rows, limit=None)
    lines = out.splitlines()
    assert lines[0] == "🏆 个人积分榜（全部）"
    header = lines[1]
    for h in ["排名", "队员", "积分", "胜场", "负场", "总场数", "友谊次数", "胜率", "无双次数"]:
        assert h in header
    assert "仅统计" not in out
    data = lines[2:]
    assert len(data) == 2
    # 每行 9 格，且各列展示宽度一致（对齐）
    for col in range(9):
        widths = {_disp_width(l.split(" | ")[col]) for l in lines[1:]}
        assert len(widths) == 1, f"列 {col} 未对齐: {widths}"
    # 首行是数据：老千 积分3 无双1
    first = [c.strip() for c in data[0].split(" | ")]
    assert first[1] == "老千" and first[2] == "3"
    assert first[6] == "1" and first[8] == "1"


def test_format_ranking_ties_share_rank():
    rows = [
        {"player": "A", "points": 2.0, "wins": 2, "losses": 0, "draws": 0, "total": 2,
         "friendship": 1, "wushuang": 0},
        {"player": "B", "points": 2.0, "wins": 2, "losses": 0, "draws": 0, "total": 2,
         "friendship": 1, "wushuang": 0},
    ]
    out = format_player_ranking(rows)
    data = out.splitlines()[2:]
    assert data[0].split(" | ")[0].strip() == "1"
    assert data[1].split(" | ")[0].strip() == "1"


def test_format_ranking_missing_fields_default_zero():
    rows = [
        {"player": "X", "points": 0.0, "wins": 0, "losses": 0, "draws": 0, "total": 1},
    ]
    out = format_player_ranking(rows)
    data = out.splitlines()[2:]
    cells = [c.strip() for c in data[0].split(" | ")]
    assert cells[6] == "0"  # 友谊次数
    assert cells[8] == "0"  # 无双次数


def test_format_ranking_empty():
    assert format_player_ranking([]) == "暂无战报数据。"


# ---------- 图片表格共用的单元格构建器 ----------

def test_build_ranking_cells_values():
    rows = [
        {"player": "老千", "points": 3.0, "wins": 3, "losses": 0, "draws": 0, "total": 3,
         "friendship": 1, "wushuang": 1},
        {"player": "红莲", "points": 1.0, "wins": 1, "losses": 1, "draws": 0, "total": 2,
         "friendship": 1, "wushuang": 0},
    ]
    cells = build_ranking_cells(rows)
    assert cells[0] == ["排名", "队员", "积分", "胜场", "负场", "总场数", "友谊次数", "胜率", "无双次数"]
    assert cells[1] == [1, "老千", "3", 3, 0, 3, 1, "100.0", 1]
    assert cells[2] == [2, "红莲", "1", 1, 1, 2, 1, "50.0", 0]


def test_build_ranking_cells_tie_rank():
    rows = [
        {"player": "A", "points": 2.0, "wins": 2, "losses": 0, "draws": 0, "total": 2,
         "friendship": 1, "wushuang": 0},
        {"player": "B", "points": 2.0, "wins": 2, "losses": 0, "draws": 0, "total": 2,
         "friendship": 1, "wushuang": 0},
        {"player": "C", "points": 1.0, "wins": 1, "losses": 1, "draws": 0, "total": 2,
         "friendship": 1, "wushuang": 0},
    ]
    cells = build_ranking_cells(rows)
    assert cells[1][0] == 1 and cells[2][0] == 1 and cells[3][0] == 3


def test_build_ranking_cells_missing_fields_default_zero():
    cells = build_ranking_cells([
        {"player": "X", "points": 0.0, "wins": 0, "losses": 0, "draws": 0, "total": 1},
    ])
    assert cells[1] == [1, "X", "0", 0, 0, 0, 0, "0.0", 0]
