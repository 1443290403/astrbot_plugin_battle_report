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


def format_player_record(player: str, agg: dict) -> str:
    """单个玩家战绩文本。agg 含 wins/losses/draws/total。"""
    wins = int(agg.get("wins", 0))
    losses = int(agg.get("losses", 0))
    draws = int(agg.get("draws", 0))
    total = int(agg.get("total", wins + losses + draws))
    wr = round(wins * 100.0 / (wins + losses), 1) if (wins + losses) else 0.0
    return (
        f"📊 {player} 战绩：胜{wins} 负{losses} 平{draws}  "
        f"总{total}  胜率{wr}%"
    )
