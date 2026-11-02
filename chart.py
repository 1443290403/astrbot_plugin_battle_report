"""Pillow 胜率走势图生成（含中文字体多路径兜底）。

图表内容：左轴按胜率范围自动缩放，主曲线为累计胜率，次曲线（虚线，右轴）为
累计场次，x 轴为日期（抽样显示 MM-DD）。
"""

import os
from math import floor
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


def _win_rate_ticks(rates: list[float]) -> list[int]:
    """上界取下一个 5 的整数倍，最多高于最高胜率 5 个百分点。"""
    low, high = min(rates), max(rates)
    span = min(100.0, max(10.0, (high - low) * 1.2))
    # 碰到 0/100 边界时整体平移窗口，保持单点、恒定胜率也有足够跨度。
    lower = max(0.0, min((low + high - span) / 2, 100.0 - span))
    # 上界限制在最终取整后执行，避免 60% 的数据又被刻度扩到 70%。
    stop = min(100, (floor(high / 5) + 1) * 5)
    # 根据范围选择步长，并让上界落在均匀刻度上。
    target_step = next(s for s in (5, 10, 20) if s >= span / 5)
    step = max(s for s in (5, 10, 20) if s <= target_step and stop % s == 0)
    # 尽量维持 10 个百分点跨度；接近 0% 时优先满足上界限制。
    lower = max(0, min(lower, stop - 10))
    start = floor(lower / step) * step
    return list(range(start, stop + 1, step))


def _trend_image(
    points: list[dict],
    title: str,
    width: int = 960,
    height: int = 480,
    scale: float = 1,
):
    """生成内存中的趋势图，坐标轴由独立趋势图与战绩卡共用。"""
    from PIL import Image, ImageDraw

    if not points:
        raise ValueError("无可绘制的数据点")

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    font_title = _load_font(round(20 * scale))
    font_small = _load_font(round(12 * scale))

    margin_left, margin_right = round(80 * scale), round(90 * scale)
    margin_top, margin_bottom = round(50 * scale), round(55 * scale)
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    draw.text((width / 2, round(22 * scale)), title, font=font_title, fill="black", anchor="mm")

    n = len(points)
    ticks = _win_rate_ticks([p["win_rate"] for p in points])
    y_min, y_max = ticks[0], ticks[-1]

    def px(i: int) -> float:
        return margin_left + (i / max(n - 1, 1)) * plot_w

    def py(v: float) -> float:
        return margin_top + plot_h * (1 - (v - y_min) / (y_max - y_min))

    # 网格、刻度与曲线共用同一映射；累计场次仍使用独立右轴。
    for v in ticks:
        y = py(v)
        draw.line([(margin_left, y), (width - margin_right, y)], fill="#DDDDDD", width=1)
        draw.text((margin_left - 8, y), f"{v}%", font=font_small, fill="gray", anchor="rm")

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

    # 点位标注使用实际胜率；白色描边保证与灰色场次线交叉时仍可辨认。
    label_boxes = []
    for point, (x, y) in zip(points, coords):
        text = f"{point['win_rate']:g}%"
        label_y = y - 8
        if scale != 1:
            # 嵌入手机卡片时，密集/恒定胜率的标签上下错开，仍保留每个点的百分比。
            offsets = [0] + [delta * direction for delta in (24, 48, 72, 96) for direction in (-1, 1)]
            for offset in offsets:
                candidate = y - 8 + offset
                box = draw.textbbox((x, candidate), text, font=font_small, anchor="ms", stroke_width=2)
                if box[1] < margin_top - 20 or box[3] > height - margin_bottom:
                    continue
                if not any(box[0] < b[2] + 3 and box[2] > b[0] - 3
                           and box[1] < b[3] + 3 and box[3] > b[1] - 3 for b in label_boxes):
                    label_y = candidate
                    label_boxes.append(box)
                    break
            if label_y != y - 8:
                draw.line([(x, y), (x, box[3] + 2 if label_y < y else box[1] - 2)],
                          fill="#bad0fa", width=1)
        draw.text((x, label_y), text, font=font_small,
                  fill="#2f6fed", anchor="ms", stroke_width=2, stroke_fill="white")

    # x 轴日期标签（抽样，显示 MM-DD）
    step = max(1, n // 10)
    if scale != 1:
        slots = max(2, int(plot_w / (draw.textlength("00-00", font=font_small) + 16)))
        step = max(1, (n + slots - 1) // slots)
    for i in range(0, n, step):
        x = px(i)
        draw.text((x, height - margin_bottom + 8), _short_date(points[i]["date"]),
                  font=font_small, fill="black", anchor="ma")

    # 图例放在绘图区外，标明缩放范围，避免遮住放大后的曲线。
    draw.text((margin_left, height - 14), f"胜率轴：{y_min}%–{y_max}%",
              font=font_small, fill="gray", anchor="lm")
    draw.text((width - margin_right - 100, height - 14), "■ 累计胜率",
              font=font_small, fill="#2f6fed", anchor="rm")
    draw.text((width - margin_right, height - 14), "-- 累计场次",
              font=font_small, fill="#999999", anchor="rm")

    return img


def make_trend_chart(points: list[dict], title: str, out_path: Path,
                     width: int = 960, height: int = 480) -> Path:
    """保存独立趋势图；与战绩卡片复用同一绘图和坐标轴逻辑。"""
    img = _trend_image(points, title, width, height)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    img.close()
    return out_path.resolve()


# ---------- 排行表格图片 ----------

def _wrap_card_text(text: str, font, width: int) -> list[str]:
    """按实际字宽换行，保留名字中的空格，不截断长 ID。"""
    lines = []
    for paragraph in str(text).split("\n"):
        current = ""
        for char in paragraph:
            if current and font.getlength(current + char) > width:
                lines.append(current)
                current = ""
            current += char
        lines.append(current)
    return lines


def make_record_images(title: str, home_team: str, period: str,
                       metrics: list[tuple[str, str]], points: list[dict], out_path: Path,
                       *, team: bool = False, players: list[str] | None = None,
                       details: list[tuple[str, list[tuple[str, str]]]] | None = None,
                       trend_note: str | None = None) -> list[Path]:
    """手机竖版战绩卡：指标与趋势同图，多 ID 明细每页最多三个，不丢条目。"""
    from PIL import Image, ImageDraw

    width, pad, gap = 900, 32, 16
    content_w = width - pad * 2
    title_font, body_font = _load_font(40, True), _load_font(24)
    small_font, value_font = _load_font(22), _load_font(40, True)
    blue, ink, muted = "#2f6fed", "#17243b", "#63718a"
    players, details = players or [], details or []
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def header_lines(detail_page=None):
        scope = "战队战绩" if team else "个人战绩"
        result = [(scope if detail_page is None else f"参赛 ID 明细 · {detail_page}", body_font, blue)]
        result += [(line, title_font, ink) for line in _wrap_card_text(title, title_font, content_w)]
        for text in (f"战队：{home_team}", period):
            result += [(line, small_font, muted) for line in _wrap_card_text(text, small_font, content_w)]
        if players and detail_page is None:
            text = "参赛 ID：" + "、".join(players[:4])
            if len(players) > 4:
                text += f" 等 {len(players)} 个"
                if details:
                    text += "，完整列表见明细"
            result += [(line, small_font, muted) for line in _wrap_card_text(text, small_font, content_w)]
        return result

    def text_height(lines):
        return sum(font.size + 14 for _, font, _ in lines)

    def draw_header(draw, lines):
        y = pad
        for text, font, color in lines:
            draw.text((pad, y), text, font=font, fill=color)
            y += font.size + 14
        return y + 12

    def draw_metrics(draw, data, y, *, compact=False):
        cell_w = (content_w - gap) // 2
        cell_h = 96 if compact else 108
        label_font = small_font if compact else body_font
        number_font = _load_font(32, True) if compact else value_font
        for i, (label, value) in enumerate(data):
            x = pad + (i % 2) * (cell_w + gap)
            top = y + (i // 2) * cell_h
            draw.rounded_rectangle((x, top, x + cell_w, top + cell_h - gap), radius=14,
                                   fill="#eaf1ff" if i < 2 and not compact else "white")
            draw.text((x + 18, top + 8), label, font=label_font, fill=muted)
            # 极端大数也不会越过单元格；只缩小数值字号，不修改数值文本。
            fitting = number_font
            while fitting.getlength(value) > cell_w - 36 and fitting.size > 12:
                fitting = _load_font(fitting.size - 2, True)
            draw.text((x + 18, top + (35 if compact else 40)), value, font=fitting,
                      fill=blue if label in ("胜率", "总积分") else ink)
        return y + ((len(data) + 1) // 2) * cell_h

    lines = header_lines()
    chart_h = 450 if points and not trend_note else 200
    header_h = pad + text_height(lines) + 12
    metrics_h = ((len(metrics) + 1) // 2) * 108
    height = header_h + metrics_h + chart_h + 76
    img = Image.new("RGB", (width, height), "#f3f6fc")
    draw = ImageDraw.Draw(img)
    y = draw_metrics(draw, metrics, draw_header(draw, lines)) + 8
    if points and not trend_note:
        trend = _trend_image(points, "友谊赛整场累计胜率" if team else "友谊对局累计胜率",
                             content_w, chart_h, scale=1.4)
        img.paste(trend, (pad, y))
        trend.close()
    else:
        draw.rounded_rectangle((pad, y, width - pad, y + chart_h), radius=16, fill="white")
        draw.text((width // 2, y + chart_h // 2), trend_note or "本期暂无友谊胜负记录",
                  font=body_font, fill=muted, anchor="mm")
    draw.text((pad, height - 40), "平局不计胜率 · 踢馆积分单独统计", font=small_font, fill=muted)
    img.save(out_path)
    img.close()
    paths = [out_path.resolve()]

    # 不把所有 ID 塞进宽表；按三张竖向明细块分页，长名字完整换行。
    pages = (len(details) + 2) // 3
    detail_labels = {"胜率", "总积分", "胜场", "负场", "平场", "总场次", "友谊积分", "踢馆积分", "友谊次数"}
    for page in range(pages):
        lines = header_lines(f"{page + 1}/{pages}")
        blocks = []
        for name, data in details[page * 3:page * 3 + 3]:
            names = _wrap_card_text(name, body_font, content_w)
            cells = [(label, value) for label, value in data if label in detail_labels]
            blocks.append((names, cells))
        height = pad * 2 + text_height(lines) + 12 + sum(
            len(names) * 38 + ((len(cells) + 1) // 2) * 96 + 24 for names, cells in blocks)
        img = Image.new("RGB", (width, height), "#f3f6fc")
        draw = ImageDraw.Draw(img)
        y = draw_header(draw, lines)
        for names, cells in blocks:
            for name in names:
                draw.text((pad, y), name, font=body_font, fill=blue)
                y += 38
            y = draw_metrics(draw, cells, y, compact=True) + 24
        target = out_path.with_name(f"{out_path.stem}_ids_{page + 1}.png")
        img.save(target)
        img.close()
        paths.append(target.resolve())
    return paths

# 浅色简洁配色
_HEADER_BG = "#2f6fed"     # 表头蓝
_HEADER_FG = "#ffffff"
_ROW_STRIPE = "#f2f6ff"    # 隔行浅条纹
_ROW_LINE = "#dce4f0"      # 行间分隔线
_BORDER = "#c9d4e8"        # 外边框
_MEDAL = {1: "#b8860b", 2: "#808080", 3: "#a0522d"}  # 金银铜
_PANEL_GAP = 24          # 左侧「战队战绩」面板与排行表格之间的间距
_PANEL_LABEL_FILL = "#666666"   # 面板标签用灰字，值用正文黑，形成一档层次
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
    panel: list[tuple[str, str]] | None = None,
) -> Path:
    """把排行单元格网格渲染成 PNG 表格图片（浅色简洁风格）。

    Args:
        cells: stats.build_ranking_cells 的输出（首行=表头，其后为数据行）。
        aligns: 各列对齐方式（"left"/"right"/"center"），表头行同样适用。
        title: 图片顶部标题。
        out_path: PNG 保存路径（目录自动创建）。
        max_rows: 最多渲染的数据行数，超出部分以省略行提示；传 None 则显示全部。
        panel: 可选，左侧「战队战绩」面板的 `(左标签, 右值)` 列表，由
            `stats.build_team_panel` 生成。**无表头行** —— 第一行与表格表头行
            同 y 同高。字符串应当是短的定长标签/数值，本函数不做截断。

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

    # 左侧「战队战绩」面板（可选）。None 与 [] 都当「没有面板」—— 用真值判断，
    # 因为 max(... for ... in []) 会抛 ValueError。所有下面用到面板的地方
    # 都必须判真值，不能判 `is not None`。
    panel = list(panel) if panel else []
    if panel:
        # 量宽用的字号必须与真正绘制时一致，否则文本会溢出格子
        p_col_w = [
            int(max(font_data.getlength(lb) for lb, _ in panel)),   # 标签：常规
            int(max(font_head.getlength(v) for _, v in panel)),     # 值：粗体
        ]
        p_xs = [margin]
        for w in p_col_w:
            p_xs.append(p_xs[-1] + w + 2 * _CELL_PAD_X)
        panel_w = p_xs[-1] - margin
        # 首行占表头高、其余占行高 —— 这样面板的分隔线与表格逐行对齐
        panel_h = head_h + row_h * (len(panel) - 1)
    else:
        p_col_w, p_xs, panel_w, panel_h = [], [], 0, 0

    table_h = head_h + row_h * len(shown) + note_h
    # ⚠️ max() 不能省：面板 8 行比「只有 1~2 名队员」的表格高得多（338 vs 86）。
    # Pillow 画到画布外**不报错**，只会把面板后几行静默裁掉 —— 没有异常、
    # 没有测试失败。note_h 只归 table_h，省略行仍只画在表格宽度内。
    body_h = max(table_h, panel_h)
    img_w = int(sum(col_w) + 2 * _CELL_PAD_X * ncols) + 2 * margin + panel_w
    img_w += _PANEL_GAP if panel else 0
    img_h = int(margin + title_h + body_h + margin)

    img = Image.new("RGB", (img_w, img_h), "white")
    draw = ImageDraw.Draw(img)

    # 标题。按整张图居中（img_w 已含面板），不是相对表格居中 ——
    # 相对表格居中的话，加了面板标题就会明显偏右。
    draw.text((img_w / 2, margin + title_h / 2), title, font=font_title,
              fill="#1a1a1a", anchor="mm")

    # 表格列边界（有面板时整张表向右让位）
    x0 = margin + (panel_w + _PANEL_GAP if panel else 0)
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

    # 左侧「战队战绩」面板。**无表头行**：第一行与表格表头行同 y 同高，
    # 之后逐行与表格数据行对齐（面板第 i 行 ↔ 表格第 i-1 个数据行）。
    # 用独立的 py 游标，不碰上面表格的 y 流。
    if panel:
        py = margin + title_h
        for i, (label, value) in enumerate(panel):
            rh = head_h if i == 0 else row_h
            # 条纹相位：表格条纹的是**奇数数据行**，而面板第 i 行对应表格第
            # i-1 个数据行，所以面板要条纹**偶数行**。写成 i % 2 == 1 会让
            # 两块相隔 24px 的条带完全错开。
            if i and i % 2 == 0:
                draw.rectangle([p_xs[0], py, p_xs[-1], py + rh], fill=_ROW_STRIPE)
            draw_cell(p_xs[0], py, p_col_w[0], rh, label, font_data,
                      _PANEL_LABEL_FILL, "left")
            draw_cell(p_xs[1], py, p_col_w[1], rh, value, font_head,
                      "#1a1a1a", "right")
            draw.line([(p_xs[0], py + rh), (p_xs[-1], py + rh)],
                      fill=_ROW_LINE, width=1)
            py += rh
        draw.rectangle([p_xs[0], margin + title_h, p_xs[-1], py],
                       outline=_BORDER, width=2)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    return out_path.resolve()
