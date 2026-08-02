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

_font_cache: dict[int, object] = {}
_warned = False


def _load_font(size: int):
    """加载指定字号的中文字体，带缓存；找不到则退回默认字体并告警一次。"""
    global _warned
    if size in _font_cache:
        return _font_cache[size]
    from PIL import ImageFont

    font = None
    for p in _FONT_CANDIDATES:
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
    _font_cache[size] = font
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
