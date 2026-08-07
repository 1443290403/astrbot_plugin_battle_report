"""排行表格图片渲染单元测试。"""

from PIL import Image

import chart
import pytest
from stats import build_ranking_cells


def _rows():
    return [
        {"player": "老千", "points": 6.0, "wins": 6, "losses": 2, "draws": 1,
         "total": 9, "friendship": 3, "wushuang": 2},
        {"player": "牌大", "points": 2.5, "wins": 5, "losses": 5, "draws": 0,
         "total": 10, "friendship": 3, "wushuang": 1},
        {"player": "一吻便杀狗", "points": 1.33, "wins": 4, "losses": 5, "draws": 2,
         "total": 11, "friendship": 2, "wushuang": 0},
    ]


def test_make_ranking_image_basic(tmp_path):
    cells = build_ranking_cells(_rows())
    aligns = ["right", "left"] + ["right"] * 7
    out = tmp_path / "rank.png"
    p = chart.make_ranking_image(cells, aligns, "个人积分榜（测试）", out)
    assert p.exists() and p == out.resolve()
    img = Image.open(p)
    assert img.format == "PNG"
    assert img.width > 400 and img.height > 150


def test_make_ranking_image_rows_scale_height(tmp_path):
    rows = _rows() * 12  # 36 行，触发截断
    cells = build_ranking_cells(rows)
    aligns = ["right", "left"] + ["right"] * 7
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
    aligns = ["right", "left"] + ["right"] * 7
    img = Image.open(chart.make_ranking_image(cells, aligns, "t", tmp_path / "l.png"))
    assert img.width > 800


def test_make_ranking_image_no_truncation_renders_all(tmp_path):
    cells = build_ranking_cells(_rows() * 40)  # 120 行
    aligns = ["right", "left"] + ["right"] * 7
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
