"""统计逻辑与文本格式化（纯 Python，便于单测）。

胜率定义：胜场 / (胜场 + 负场)，平局不计入分母只计入总场次。
"""


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


def format_player_ranking(rows: list[dict], limit: int | None = 10, min_games: int = 1) -> str:
    """个人积分排行文本。limit 为 None 时显示全部。"""
    if not rows:
        return "暂无战报数据。"
    title = "🏆 个人积分榜（全部）" if limit is None else f"🏆 个人积分榜（前 {limit}）"
    return "\n".join(_rank_lines(rows, "player", f"{title}\n（仅统计 ≥{min_games} 局的玩家）"))


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
        f"（用法：/战报战绩 <玩家名> 查个人战绩）"
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
        "▎查询（默认本月，末尾可加 时间=X月 查其他月份）\n"
        "/战报排行 [个人|队伍] [时间=X月]  排行榜\n"
        "/战报战绩 [玩家名] [时间=X月]    战绩（无玩家名=本战队）\n"
        "/战报趋势 <玩家名|队伍> [最近N天|时间=X月]  胜率走势图\n"
        "/战报导出 [全部|胜场|负场|csv|json] [时间=X月]  导出（本月）\n"
        "/我的战绩 [时间=X月]   我的总战绩"
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
