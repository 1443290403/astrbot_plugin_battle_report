"""Pillow 胜率走势图生成（含中文字体多路径兜底）。

图表内容：y 轴固定 0-100（胜率%），主曲线为累计胜率，次曲线（虚线，右轴）为
累计场次，x 轴为日期（抽样显示 MM-DD）。
"""

import os
from pathlib import Path

# 中文字体候选（Windows / Linux）
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",        # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",      # 黑体
    r"C:\Windows\Fonts\simsun.ttc",      # 宋体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]

# 粗体候选：优先微软雅黑粗体/黑体/Noto Bold，缺则回退到常规字体列表
_FONT_CANDIDATES_BOLD = [
    r"C:\Windows\Fonts\msyhbd.ttc",        # 微软雅黑 粗体
    r"C:\Windows\Fonts\simhei.ttf",        # 黑体（近似粗）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
] + _FONT_CANDIDATES

_font_cache: dict[tuple, object] = {}
_warned = False


def _load_font(size: int, bold: bool = False):
    """加载指定字号的中文字体，带缓存；找不到则退回默认字体并告警一次。"""
    global _warned
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    from PIL import ImageFont

    font = None
    for p in (_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES):
        if os.path.exists(p):
            try:
                font = ImageFont.truetype(p, size)
                break
            except Exception:
                continue
    if font is None:
        if not _warned:
            _warned = True
            print("[battle_report] 未找到中文字体，图表中文可能显示为方块。"
                  "请安装 微软雅黑 / Noto Sans CJK。")
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _dashed_line(draw, start, end, fill="#999999", dash=6, gap=4, width=1):
    """在两点间绘制虚线。"""
    import math

    x1, y1 = start
    x2, y2 = end
    total = math.hypot(x2 - x1, y2 - y1)
    if total == 0:
        return
    steps = int(total // (dash + gap)) + 1
    for i in range(steps):
        t0 = i * (dash + gap) / total
        t1 = min((i * (dash + gap) + dash) / total, 1.0)
        draw.line(
            [(x1 + (x2 - x1) * t0, y1 + (y2 - y1) * t0),
             (x1 + (x2 - x1) * t1, y1 + (y2 - y1) * t1)],
            fill=fill,
            width=width,
        )


def _short_date(date: str) -> str:
    """"2026-08-01" -> "08-01"。"""
    parts = str(date).split("-")
    return "-".join(parts[1:]) if len(parts) == 3 else str(date)


def make_trend_chart(
    points: list[dict],
    title: str,
    out_path: Path,
    width: int = 960,
    height: int = 480,
) -> Path:
    """绘制胜率走势图并保存。

    Args:
        points: compute_cumulative 的输出，含 date/win_rate/total。
        out_path: PNG 保存路径（目录自动创建）。

    Returns:
        保存后的绝对路径。
    """
    from PIL import Image, ImageDraw

    if not points:
        raise ValueError("无可绘制的数据点")

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    font_title = _load_font(20)
    font_small = _load_font(12)

    margin_left, margin_right = 80, 90
    margin_top, margin_bottom = 50, 55
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    draw.text((width / 2, 22), title, font=font_title, fill="black", anchor="mm")

    # y 轴网格与刻度（胜率 0-100）
    for v in (0, 25, 50, 75, 100):
        y = margin_top + plot_h * (1 - v / 100)
        draw.line([(margin_left, y), (width - margin_right, y)], fill="#DDDDDD", width=1)
        draw.text((margin_left - 8, y), f"{v}%", font=font_small, fill="gray", anchor="rm")

    n = len(points)

    def px(i: int) -> float:
        return margin_left + (i / max(n - 1, 1)) * plot_w

    def py(v: float) -> float:
        return margin_top + plot_h * (1 - v / 100)

    # 主曲线：累计胜率
    coords = [(px(i), py(p["win_rate"])) for i, p in enumerate(points)]
    if n == 1:
        c = coords[0]
        draw.ellipse([c[0] - 4, c[1] - 4, c[0] + 4, c[1] + 4], fill="#2f6fed")
    else:
        draw.line(coords, fill="#2f6fed", width=2)
        for c in coords:
            draw.ellipse([c[0] - 3, c[1] - 3, c[0] + 3, c[1] + 3], fill="#2f6fed")

    # 次曲线（虚线，右轴）：累计场次
    max_games = max(p["total"] for p in points)
    if max_games > 0:
        draw.text((width - margin_right + 8, margin_top), str(max_games),
                  font=font_small, fill="gray", anchor="lm")
        draw.text((width - margin_right + 8, margin_top + plot_h), "0",
                  font=font_small, fill="gray", anchor="lm")
        gcoords = [(px(i), margin_top + plot_h * (1 - p["total"] / max_games)) for i, p in enumerate(points)]
        if n > 1:
            for i in range(n - 1):
                _dashed_line(draw, gcoords[i], gcoords[i + 1])
        else:
            draw.ellipse([gcoords[0][0] - 3, gcoords[0][1] - 3, gcoords[0][0] + 3, gcoords[0][1] + 3],
                         fill="#999999")

    # x 轴日期标签（抽样，显示 MM-DD）
    step = max(1, n // 10)
    for i in range(0, n, step):
        x = px(i)
        draw.text((x, height - margin_bottom + 8), _short_date(points[i]["date"]),
                  font=font_small, fill="black", anchor="ma")

    # 图例
    draw.text((width - margin_right - 150, margin_top + 4),
              "■ 胜率   -- 累计场次", font=font_small, fill="#2f6fed")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    return out_path.resolve()


# ---------- 排行表格图片 ----------

# 浅色简洁配色
_HEADER_BG = "#2f6fed"     # 表头蓝
_HEADER_FG = "#ffffff"
_ROW_STRIPE = "#f2f6ff"    # 隔行浅条纹
_ROW_LINE = "#dce4f0"      # 行间分隔线
_BORDER = "#c9d4e8"        # 外边框
_MEDAL = {1: "#b8860b", 2: "#808080", 3: "#a0522d"}  # 金银铜
_CELL_PAD_X = 16
_TITLE_SIZE = 22
_HEADER_FONT_SIZE = 15
_DATA_FONT_SIZE = 15
_NOTE_FONT_SIZE = 13


def make_ranking_image(
    cells: list[list[object]],
    aligns: list[str],
    title: str,
    out_path: Path,
    max_rows: int = 30,
) -> Path:
    """把排行单元格网格渲染成 PNG 表格图片（浅色简洁风格）。

    Args:
        cells: stats.build_ranking_cells 的输出（首行=表头，其后为数据行）。
        aligns: 各列对齐方式（"left"/"right"/"center"），表头行同样适用。
        title: 图片顶部标题。
        out_path: PNG 保存路径（目录自动创建）。
        max_rows: 最多渲染的数据行数，超出部分以省略行提示；传 None 则显示全部。

    Returns:
        保存后的绝对路径。
    """
    from PIL import Image, ImageDraw

    if not cells or len(cells) < 2:
        raise ValueError("无可渲染的排行数据")
    ncols = len(cells[0])
    data_rows = cells[1:]
    if max_rows is None:
        truncated = 0  # 显示全部，不截断
    else:
        max_rows = max(int(max_rows), 1)
        truncated = len(data_rows) - max_rows
    shown = data_rows if truncated <= 0 else data_rows[:max_rows]

    font_title = _load_font(_TITLE_SIZE, bold=True)
    font_head = _load_font(_HEADER_FONT_SIZE, bold=True)
    font_data = _load_font(_DATA_FONT_SIZE)
    font_note = _load_font(_NOTE_FONT_SIZE)
    note_text = f"… 其余 {truncated} 人未显示" if truncated > 0 else None

    # 列宽 = 该列表头与数据单元格像素宽度最大值
    col_w = []
    for c in range(ncols):
        w = max(font_head.getlength(str(cells[0][c])),
                max(font_data.getlength(str(r[c])) for r in shown))
        col_w.append(w)

    margin = 28
    title_h = 58
    head_h = 44
    row_h = 42
    note_h = 38 if note_text else 0
    img_w = int(sum(col_w) + 2 * _CELL_PAD_X * ncols) + 2 * margin
    img_h = int(margin + title_h + head_h + row_h * len(shown) + note_h + margin)

    img = Image.new("RGB", (img_w, img_h), "white")
    draw = ImageDraw.Draw(img)

    # 标题
    draw.text((img_w / 2, margin + title_h / 2), title, font=font_title,
              fill="#1a1a1a", anchor="mm")

    # 表格列边界
    x0 = margin
    xs = [x0]
    for w in col_w:
        xs.append(xs[-1] + w + 2 * _CELL_PAD_X)

    def draw_cell(x, y, w, h, text, font, fill, align):
        t = str(text)
        if align == "left":
            tx = x + _CELL_PAD_X
        elif align == "center":
            tx = x + _CELL_PAD_X + w / 2 - font.getlength(t) / 2
        else:  # right
            tx = x + _CELL_PAD_X + w - font.getlength(t)
        draw.text((tx, y + h / 2), t, font=font, fill=fill, anchor="lm")

    # 表头
    y = margin + title_h
    draw.rectangle([x0, y, xs[-1], y + head_h], fill=_HEADER_BG)
    for c in range(ncols):
        draw_cell(xs[c], y, col_w[c], head_h, cells[0][c], font_head, _HEADER_FG, aligns[c])
    y += head_h

    # 数据行
    for i, r in enumerate(shown):
        if i % 2 == 1:
            draw.rectangle([x0, y, xs[-1], y + row_h], fill=_ROW_STRIPE)
        for c in range(ncols):
            fill = _MEDAL.get(r[0], "#1a1a1a") if c == 0 else "#1a1a1a"
            draw_cell(xs[c], y, col_w[c], row_h, r[c], font_data, fill, aligns[c])
        draw.line([(x0, y + row_h), (xs[-1], y + row_h)], fill=_ROW_LINE, width=1)
        y += row_h

    # 省略行
    if note_text:
        draw.line([(x0, y), (xs[-1], y)], fill=_ROW_LINE, width=1)
        draw.text((x0 + _CELL_PAD_X, y + note_h / 2), note_text,
                  font=font_note, fill="#888888", anchor="lm")
        y += note_h

    # 外边框
    draw.rectangle([x0, margin + title_h, xs[-1], y], outline=_BORDER, width=2)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    return out_path.resolve()
