"""排行表格图片渲染单元测试。"""

from PIL import Image

import chart
import pytest
from stats import build_ranking_cells, rank_aligns


def _rows():
    return [
        {"player": "老千", "points": 6.0, "wins": 6, "losses": 2, "draws": 1,
         "total": 9, "friendship": 3, "wushuang": 2},
        {"player": "牌大", "points": 2.5, "wins": 5, "losses": 5, "draws": 0,
         "total": 10, "friendship": 3, "wushuang": 1},
        {"player": "一吻便杀狗", "points": 1.33, "wins": 4, "losses": 5, "draws": 2,
         "total": 11, "friendship": 2, "wushuang": 0},
    ]


def _aligns(cells):
    return rank_aligns(len(cells[0]))


def test_make_ranking_image_basic(tmp_path):
    cells = build_ranking_cells(_rows())
    out = tmp_path / "rank.png"
    p = chart.make_ranking_image(cells, _aligns(cells), "个人积分榜（测试）", out)
    assert p.exists() and p == out.resolve()
    img = Image.open(p)
    assert img.format == "PNG"
    assert img.width > 400 and img.height > 150


def test_make_ranking_image_rows_scale_height(tmp_path):
    rows = _rows() * 12  # 36 行，触发截断
    cells = build_ranking_cells(rows)
    aligns = _aligns(cells)
    h30 = Image.open(chart.make_ranking_image(cells, aligns, "t", tmp_path / "a.png", max_rows=30)).height
    h10 = Image.open(chart.make_ranking_image(cells, aligns, "t", tmp_path / "b.png", max_rows=10)).height
    h1 = Image.open(chart.make_ranking_image(cells, aligns, "t", tmp_path / "c.png", max_rows=1)).height
    assert h30 > h10 > h1
    # max_rows=0 视为 1，不崩溃
    Image.open(chart.make_ranking_image(cells, aligns, "t", tmp_path / "d.png", max_rows=0)).close()


def test_make_ranking_image_long_player_name(tmp_path):
    cells = build_ranking_cells([
        {"player": "这是一个特别特别特别特别特别长的队员名字一吻便杀狗", "points": 1.0,
         "wins": 1, "losses": 0, "draws": 0, "total": 1, "friendship": 1, "wushuang": 0},
    ])
    img = Image.open(
        chart.make_ranking_image(cells, _aligns(cells), "t", tmp_path / "l.png")
    )
    assert img.width > 800


def test_make_ranking_image_no_truncation_renders_all(tmp_path):
    cells = build_ranking_cells(_rows() * 40)  # 120 行
    aligns = _aligns(cells)
    h_all = Image.open(chart.make_ranking_image(cells, aligns, "t", tmp_path / "a.png", max_rows=None)).height
    h_exact = Image.open(chart.make_ranking_image(cells, aligns, "t", tmp_path / "b.png", max_rows=120)).height
    h30 = Image.open(chart.make_ranking_image(cells, aligns, "t", tmp_path / "c.png", max_rows=30)).height
    # max_rows=None 与 恰好不截断 时等高（无省略行、全部渲染）
    assert h_all == h_exact
    assert h_all > h30


def test_make_ranking_image_empty_raises(tmp_path):
    with pytest.raises(ValueError):
        chart.make_ranking_image([["a"]], ["left"], "t", tmp_path / "x.png")
    with pytest.raises(ValueError):
        chart.make_ranking_image([], ["left"], "t", tmp_path / "y.png")


def test_make_ranking_image_with_bonus_column(tmp_path):
    """带奖金列（15 列）也要能渲染 —— 列数与 14 列时不同。"""
    rows = _rows()
    for r in rows:
        r["bonus"] = 130
    cells = build_ranking_cells(rows)
    assert len(cells[0]) == 15
    img = Image.open(
        chart.make_ranking_image(cells, _aligns(cells), "t", tmp_path / "b.png")
    )
    assert img.format == "PNG"


def _panel():
    return [
        ("总场数", "264"), ("守馆首轮", "1"), ("守馆", "2"), ("踢馆", "13"),
        ("胜场", "158"), ("负场", "106"), ("胜率", "59.8%"), ("积分", "110.56"),
    ]


def test_panel_taller_than_table_is_not_clipped(tmp_path):
    """面板 8 行(338px) 比「只有 1 名队员」的表格(86px) 高 —— 图必须长高容纳它。

    这是 `body_h = max(table_h, panel_h)` 的回归护栏：去掉 max() 后画布
    按表格的高度开，Pillow 画到画布外**不报错**，面板后 5 行被静默裁掉，
    两种写法都不抛异常 —— 只有 assert 高度变大才拦得住。
    """
    cells = build_ranking_cells(_rows()[:1])
    h_panel = Image.open(
        chart.make_ranking_image(cells, _aligns(cells), "t", tmp_path / "p.png", 30, _panel())
    ).height
    h_none = Image.open(
        chart.make_ranking_image(cells, _aligns(cells), "t", tmp_path / "n.png")
    ).height
    assert h_panel > h_none


def test_panel_does_not_shrink_tall_table(tmp_path):
    """表格比面板高时高度不受影响（从另一侧钉住 max()，避免写成 `panel_h`）。"""
    cells = build_ranking_cells(_rows() * 12)  # 36 行 → 截到 30 行
    h_panel = Image.open(
        chart.make_ranking_image(cells, _aligns(cells), "t", tmp_path / "p.png", 30, _panel())
    ).height
    h_none = Image.open(
        chart.make_ranking_image(cells, _aligns(cells), "t", tmp_path / "n.png", 30)
    ).height
    assert h_panel == h_none


def test_panel_none_matches_omitted(tmp_path):
    """`panel=None` / 不传 / `[]` 三者尺寸逐像素相同 —— 「默认退化成今天」的契约。

    main.py 里 `/排行 全部` 走的正是 `panel = None` 这条路。
    """
    cells = build_ranking_cells(_rows())
    a = Image.open(chart.make_ranking_image(cells, _aligns(cells), "t", tmp_path / "a.png"))
    b = Image.open(
        chart.make_ranking_image(cells, _aligns(cells), "t", tmp_path / "b.png", 30, None)
    )
    c = Image.open(
        chart.make_ranking_image(cells, _aligns(cells), "t", tmp_path / "c.png", 30, [])
    )
    assert (a.width, a.height) == (b.width, b.height) == (c.width, c.height)


def test_panel_widens_image_and_coexists_with_note(tmp_path):
    """面板加宽图片；`max_rows=1` 触发省略行时面板仍能渲染（note_h 与面板共存）。"""
    cells = build_ranking_cells(_rows() * 12)
    aligns = _aligns(cells)
    w_none = Image.open(
        chart.make_ranking_image(cells, aligns, "t", tmp_path / "n.png", 1)
    ).width
    img = Image.open(
        chart.make_ranking_image(cells, aligns, "t", tmp_path / "p.png", 1, _panel())
    )
    assert img.width > w_none
    assert img.format == "PNG"


def test_rank_aligns_matches_header_columns():
    """rank_aligns(ncols) 必须与表头列数一致 —— 加列时忘了给它传对列数会在这里炸。

    这正是 §10-11 的回归护栏：对齐列表以前在 main.py / stats.py /
    本文件里各写了一遍，加列时 8 处一起错位，测试还跟着一起错。
    v1.15.0 起全部居中，所以额外钉死「不再有 left/right 混排」。
    """
    header = build_ranking_cells(_rows())[0]
    aligns = rank_aligns(len(header))
    assert len(aligns) == len(header)
    assert set(aligns) == {"center"}

    rows = _rows()
    for r in rows:
        r["bonus"] = 10
    header15 = build_ranking_cells(rows)[0]
    assert len(rank_aligns(len(header15))) == len(header15) == 15
