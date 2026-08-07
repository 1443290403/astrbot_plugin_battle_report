"""统计逻辑与文本格式化（纯 Python，便于单测）。

胜率定义：胜场 / (胜场 + 负场)，平局不计入分母只计入总场次。
"""

import unicodedata


def compute_cumulative(points: list[tuple[str, int, int]]) -> list[dict]:
    """把按日期的 [(date, wins, losses)] 累计为逐日累计胜率/场次。

    Returns:
        list[dict]: [{date, win_rate, total, wins, losses}]，按日期顺序。
    """
    result: list[dict] = []
    cum_w = cum_l = 0
    for date, w, l in points:
        cum_w += int(w)
        cum_l += int(l)
        total = cum_w + cum_l
        wr = round(cum_w * 100.0 / total, 1) if total else 0.0
        result.append(
            {"date": date, "win_rate": wr, "total": total, "wins": cum_w, "losses": cum_l}
        )
    return result


def compute_match_stats(match_duels: list[dict], winner: str = "") -> dict[str, dict]:
    """单场比赛的友谊次数与无双统计（纯逻辑，不依赖数据库）。

    match_duels: 本场全部对局 dict，每项须含
        seq, score_a, score_b, player_a_team, player_b_team,
        resolved_a, resolved_b（已解析玩家名）。
    winner: 比赛胜方战队；非空时仅给胜方成员记无双（保证每场至多一人）；
        空串则关闭守卫（严格字面规则，兼容旧数据/未决比赛）。

    无双：队友（≥1）最后一场有效对局均负/平（按 seq 最大），P 本人未阵亡
    （最后一场为胜），且 P 对对面每个选手都有胜局。0:0 占位对局完全忽略。
    返回 {已解析名: {"friendship": 0|1, "wushuang": 0|1}}。
    """
    if not match_duels:
        return {}

    appeared: set[str] = set()                # 有有效对局的玩家
    last: dict[str, tuple[int, bool]] = {}    # 玩家 → (seq, 是否胜) 最后一场
    beaten: dict[str, set[str]] = {}          # 胜者 → 击败的对手集合
    side: dict[str, str] = {}                 # 玩家 → 所属队伍

    for d in match_duels:
        a, b = d["resolved_a"], d["resolved_b"]
        sa, sb = int(d["score_a"]), int(d["score_b"])
        if sa == 0 and sb == 0:
            continue  # 占位未打，忽略
        seq = int(d["seq"])
        appeared.update((a, b))
        side[a] = d["player_a_team"]
        side[b] = d["player_b_team"]
        # 只保留 seq 最大的有效对局（seq 单调递增）
        if last.get(a, (-1, False))[0] < seq:
            last[a] = (seq, sa > sb)
        if last.get(b, (-1, False))[0] < seq:
            last[b] = (seq, sb > sa)
        if sa > sb:
            beaten.setdefault(a, set()).add(b)
        elif sb > sa:
            beaten.setdefault(b, set()).add(a)

    result = {name: {"friendship": 1, "wushuang": 0} for name in appeared}

    for p in appeared:
        teammates = {q for q in appeared if q != p and side[q] == side[p]}
        opponents = {q for q in appeared if side[q] != side[p]}
        is_ace = (
            len(teammates) >= 1                # 1v1 无队友不算
            and bool(opponents)
            and last[p][1]                     # P 未阵亡
            and all(not last[t][1] for t in teammates)  # 队友均阵亡（负或非零平）
            and beaten.get(p, set()) >= opponents      # 击败对面每个选手
            and (not winner or side[p] == winner)
        )
        if is_ace:
            result[p]["wushuang"] = 1

    return result


def _disp_width(s: object) -> int:
    """按显示宽度计长：CJK 全角（W/F）算 2，其余算 1。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def _pad(s: object, width: int, align: str = "right") -> str:
    """按显示宽度补齐空格（right/left/center）。"""
    s = str(s)
    pad = width - _disp_width(s)
    if pad <= 0:
        return s
    if align == "left":
        return s + " " * pad
    if align == "center":
        return " " * (pad // 2) + s + " " * (pad - pad // 2)
    return " " * pad + s


def _format_table(cells: list[list[object]], aligns: list[str]) -> str:
    """把 [[表头...], [数据...], ...] 渲染为各列对齐的表格。"""
    widths = [max(_disp_width(r[col]) for r in cells) for col in range(len(cells[0]))]
    return "\n".join(
        " | ".join(_pad(r[col], widths[col], aligns[col]) for col in range(len(r)))
        for r in cells
    )


def _rank_lines(rows: list[dict], name_key: str, title: str) -> list[str]:
    """生成带并列名次的榜单文本行（按积分 points）。"""
    if not rows:
        return ["暂无战报数据。"]
    lines = [title]
    prev = None
    rank = 0
    for i, r in enumerate(rows, 1):
        pts = r.get("points", 0) or 0
        key = (pts, r["wins"], r["total"])
        if key != prev:
            rank = i
            prev = key
        lines.append(
            f"第{rank}名 {r[name_key]}  胜{r['wins']} 负{r['losses']} "
            f"总{r['total']}  积分{pts}"
        )
    return lines


_RANK_HEADERS = ["排名", "队员", "积分", "胜场", "负场", "总场数", "友谊次数", "胜率", "无双次数"]


def build_ranking_cells(rows: list[dict]) -> list[list[object]]:
    """排行行 → 展示单元格（首行=表头）。

    并列名次规则与 _rank_lines 一致：points/wins/total 相同则同名次。
    文字表格（format_player_ranking）与图片表格（chart.make_ranking_image）
    共用本函数，保证两处名次与单元格值一致。
    """
    cells = [list(_RANK_HEADERS)]
    prev = None
    rank = 0
    for i, r in enumerate(rows, 1):
        pts = r.get("points", 0) or 0
        key = (pts, r["wins"], r["total"])
        if key != prev:
            rank = i
            prev = key
        wins = int(r["wins"])
        losses = int(r["losses"])
        draws = int(r.get("draws", 0))
        played_total = wins + losses + draws  # 总场数不含 0:0 占位
        wr = round(wins * 100.0 / (wins + losses), 1) if (wins + losses) else 0.0
        cells.append([
            rank, r["player"], f"{pts:g}", wins, losses, played_total,
            int(r.get("friendship", 0)), f"{wr:.1f}", int(r.get("wushuang", 0)),
        ])
    return cells


def format_player_ranking(rows: list[dict], limit: int | None = 10, min_games: int = 1) -> str:
    """个人积分排行：Excel 风格表格（表头一行，数据行按列对齐）。"""
    if not rows:
        return "暂无战报数据。"
    title = "🏆 个人积分榜（全部）" if limit is None else f"🏆 个人积分榜（前 {limit}）"
    aligns = ["right", "left", "right", "right", "right", "right", "right", "right", "right"]
    return title + "\n" + _format_table(build_ranking_cells(rows), aligns)


def format_team_ranking(rows: list[dict], limit: int = 10) -> str:
    """队伍积分排行文本。"""
    if not rows:
        return "暂无队伍战报数据。"
    return "\n".join(_rank_lines(rows, "team", f"🏆 队伍积分榜（前 {limit}）"))


def _points(wins: int, losses: int) -> float:
    """积分 = 胜场 × 胜率(小数) = 胜场² / (胜场+负场)。"""
    return round(wins * wins / (wins + losses), 2) if (wins + losses) else 0.0


def format_team_record(home_team: str, record: dict, suffix: str = "") -> str:
    """主体战队总体战绩文本（胜/负/平/总/积分/胜率）。"""
    w = int(record.get("wins", 0))
    l = int(record.get("losses", 0))
    d = int(record.get("draws", 0))
    t = int(record.get("total", 0))
    wr = record.get("win_rate", 0)
    return (
        f"🏆 {home_team} 总战绩{suffix}：\n"
        f"胜{w} 负{l} 平{d}  总{t}  积分{_points(w, l)}  胜率{wr}%\n"
        f"（用法：/战绩 <玩家名> 查个人战绩）"
    )


def format_home_team_vs(home_team: str, rows: list[dict]) -> str:
    """主体战队对战各对手的记录文本（胜/负/总场/胜率）。"""
    if not rows:
        return f"{home_team} 暂无对战记录。"
    lines = [f"🏆 {home_team} 对战记录："]
    for r in rows:
        lines.append(
            f"vs {r['opponent']}  胜{r['wins']} 负{r['losses']}  "
            f"总{r['total']}  胜率{r['win_rate']}%"
        )
    return "\n".join(lines)


def format_player_record(player: str, agg: dict) -> str:
    """单个玩家战绩文本。agg 含 wins/losses/draws/total。"""
    wins = int(agg.get("wins", 0))
    losses = int(agg.get("losses", 0))
    draws = int(agg.get("draws", 0))
    total = int(agg.get("total", wins + losses + draws))
    wr = round(wins * 100.0 / (wins + losses), 1) if (wins + losses) else 0.0
    return (
        f"📊 {player} 战绩：胜{wins} 负{losses} 平{draws}  "
        f"总{total}  积分{_points(wins, losses)}  胜率{wr}%"
    )


# ---------- 帮助分类（按群属性展示） ----------

HELP_SECTIONS = {
    "排表": (
        "▎排表\n"
        "/排表 [规则]\n"
        "KC:红莲 凯撒亮 悠悠球\n"
        "DYG:老千 蓝大 红大\n"
        "→ 生成随机配对的第一轮战报模板"
    ),
    "追加轮次": (
        "▎追加轮次\n"
        "/第N轮 [玩家A [比分] 玩家B]\n"
        "无追加：随机匹配上一轮胜者\n"
        "如：/第二轮 红莲 2:0 蓝大（记录比分）\n"
        "读取群聊中最近一条战报并追加该轮"
    ),
    "记录比分": (
        "▎记录比分\n"
        "/记录 玩家名 比分 [对手]\n"
        "如：/记录 红莲 20（填入红莲最后一场未记录对阵）\n"
        "如：/记录 红莲 20 蓝大（无未记录对阵时插入最新轮次）\n"
        "比分支持 2:0 或紧凑 20"
    ),
    "提交战报": (
        "▎提交战报\n"
        "/发送 + 粘贴排表模板（填入实际比分）\n"
        "（/战报 仍可用）"
    ),
    "管理": (
        "▎管理\n"
        "/绑定战队 <战队>        绑定本群战队（管理/群主）\n"
        "/查看战队              查看本群战队\n"
        "/战队列表               查看全部战队\n"
        "/战报删除 <战报ID>      仅管理/群主\n"
        "/战报撤销              撤销自己最近一条"
    ),
    "查询": (
        "▎查询（默认本月，末尾可加 X月 查其他月份，如 七月/7月）\n"
        "/排行 [个人|队伍] [X月]  排行榜\n"
        "/战绩 [玩家名] [X月]    战绩（无玩家名=本战队）\n"
        "/趋势 <玩家名|队伍> [最近N天|X月]  胜率走势图\n"
        "/导出 [玩家名] [胜场|负场|全部] [X月|最近N天] [csv|json]  导出（默认本月）\n"
        "/我的战绩 [X月]   我的总战绩\n"
    ),
    "用户与参赛ID": (
        "▎用户与参赛ID\n"
        "/查ID <关键词>          模糊查询本战队参赛ID\n"
        "/绑定ID <参赛ID> [参赛ID...] 批量绑定参赛ID到自己的用户\n"
        "/解绑ID <参赛ID> [参赛ID...] 批量解除参赛ID绑定（管理可解任意）\n"
        "/改名 <新名字>          修改自己的用户名称\n"
        "/我的ID                查看/确认自己的身份\n"
        "/管理ID <参赛ID[,参赛ID...]> <用户名> 管理/群主查看/批量绑定参赛ID"
    ),
    "超级管理": (
        "▎超级管理（仅超管）\n"
        "/禁群 <群号>            禁用该群全部功能\n"
        "/启群 <群号>            开启该群全部功能\n"
        "/查群 <群号>            查询群禁用状态\n"
        "/群列表 [战队]          查看全部群（可按战队过滤）\n"
        "/群聊属性 <友谊群|战报群|主群>  设置群属性\n"
        "/通告 <内容>            向所有群发布通告\n"
        "/通告 <群号/QQ号>\\n<内容>  仅向指定群/人发布通告"
    ),
}

CHAT_TYPE_SECTIONS = {
    "友谊群": ["排表", "追加轮次", "记录比分"],
    "战报群": ["提交战报", "管理", "查询"],
    "主群": ["查询", "用户与参赛ID"],
}

ALL_SECTIONS = [k for k in HELP_SECTIONS if k != "超级管理"]


def render_help(section_keys: list[str]) -> str:
    """按分类渲染帮助文本。"""
    parts = ["📋 战队对战战报插件", "━━━━━━━━━━━━"]
    for k in section_keys:
        parts.append(HELP_SECTIONS[k])
    parts.append("群属性：/群聊属性 <友谊群|战报群|主群>（管理/群主）")
    parts.append("/帮助 全部 查看全部 | /帮助 超管 查看超管指令")
    return "\n\n".join(parts)
