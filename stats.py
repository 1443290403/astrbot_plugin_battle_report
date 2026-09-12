"""统计逻辑与文本格式化（纯 Python，便于单测）。

胜率定义：胜场 / (胜场 + 负场)，平局不计入分母只计入总场次。

另一种比赛类型是**踢馆赛**（`matches.kind = 'raid'`）：1 对多，踢馆方打穿守馆方
全部人才算赢，守馆方只要终结踢馆者一次就赢。踢馆有自己的「踢馆积分」，与友谊
积分独立计算，最终排名看两者之和（见 `total_points`）。口径细节见
REQUIREMENTS.md §5-M13。
"""

import math
import unicodedata

try:  # AstrBot 运行时按包导入
    from .battle_report_parser import KIND_RAID, RAID_PLACEHOLDER, is_kof
except ImportError:  # 测试把本模块当顶层模块导入
    from battle_report_parser import KIND_RAID, RAID_PLACEHOLDER, is_kof


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


def compute_match_stats(
    match_duels: list[dict], winner: str = "", rule: str = ""
) -> dict[str, dict]:
    """单场比赛的友谊次数与无双统计（纯逻辑，不依赖数据库）。

    match_duels: 本场全部对局 dict，每项须含
        seq, round_no, score_a, score_b, player_a_team, player_b_team,
        resolved_a, resolved_b（已解析玩家名）。
    winner: 比赛胜方战队；非空时仅给胜方成员记无双（保证每场至多一人）；
        空串则关闭守卫（严格字面规则，兼容旧数据/未决比赛）。
    rule: 本场规则行（`matches.rule`）。**只用于决定是否启用下面的 KOF 数据
        兜底** —— 人头赛没有"全员淘汰"这回事，不能套用（见下）。空串按 KOF
        处理，与 `battle_report_parser.determine_match_winner` 的分支一致。

    无双：队友（≥1）最后一场有效对局均负/平（按 seq 最大），P 本人未阵亡
    （最后一场为胜），且 P 对对面每个选手都有胜局。0:0 占位对局完全忽略。
    数据兜底（**仅 KOF**）：KOF 中败方首轮名单必然全员被淘汰；若败方某首轮选手
    最后一场为胜（未被击败），说明战报漏记其落败对局。此时不要求胜方唯一幸存者
    P 击败该选手，否则其无双会被误判为 0。替补（非首轮出场）不在此列——替补
    可能真的未淘汰，仍要求 P 击败。
    人头赛按获胜对局数定胜负，败方首轮选手完全可以赢下最后一场，套用该兜底会
    把合法对手误删、把无双误判成 1，因此用 `rule` 门控。
    返回 {已解析名: {"friendship": 0|1, "wushuang": 0|1}}。
    """
    if not match_duels:
        return {}

    appeared: set[str] = set()                # 有有效对局的玩家
    last: dict[str, tuple[int, bool]] = {}    # 玩家 → (seq, 是否胜) 最后一场
    beaten: dict[str, set[str]] = {}          # 胜者 → 击败的对手集合
    side: dict[str, str] = {}                 # 玩家 → 所属队伍
    roster: set[str] = set()                  # 首轮（round_no==1）有有效对局的玩家

    for d in match_duels:
        a, b = d["resolved_a"], d["resolved_b"]
        sa, sb = int(d["score_a"]), int(d["score_b"])
        if sa == 0 and sb == 0:
            continue  # 占位未打，忽略
        seq = int(d["seq"])
        appeared.update((a, b))
        side[a] = d["player_a_team"]
        side[b] = d["player_b_team"]
        if int(d.get("round_no", 1)) == 1:          # 首轮名单（数据兜底用）
            roster.update((a, b))
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
        if winner and is_kof(rule):
            # 数据兜底（仅 KOF，见 docstring）：KOF 中败方首轮名单必然全员被淘汰；
            # 若其中某选手最后一场为胜（未被击败），说明战报漏记其落败对局。此时
            # 不要求胜方唯一幸存者 P 击败该选手，否则其无双会被误判为 0。
            # 替补（非首轮出场）不在此列——替补可能真的未淘汰。
            opponents = {
                q for q in opponents
                if not (q in roster and last[q][1] and side[q] != winner)
            }
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


# ---------- 踢馆赛（raid）----------

RAID_DEFENSE_CAP = 10  # 守馆积分月度上限（含 SHUT DOWN 的 +5，见 REQUIREMENTS §5-M13）
RAID_OWNER_BONUS = 2   # 成功踢破有馆主守的馆，额外加点
RAID_SHUTDOWN_BONUS = 5  # 馆主作为第 5 个守馆者终结踢馆者（SHUT DOWN）


def _unplayed(d: dict) -> bool:
    """该对局是否「没打」：比分 0:0（全库通用口径），或防守方是 `规则` 空位。"""
    if str(d.get("player_b", "")) == RAID_PLACEHOLDER:
        return True
    return int(d["score_a"]) == 0 and int(d["score_b"]) == 0


def raid_attack_points(defender_count: int, has_owner: bool) -> int:
    """踢馆方单场加点。

    - 踢破 ≥4 名防守者守的馆 → +3（规则原文「3 个防守者以上（不包括 3 个）」）
    - 踢破 1–3 名防守者守的馆 → +2
    - 无人守馆 → +0（只发徽章）
    - 该馆有馆主守且被踢破 → **再 +2**（与人数档叠加，4 人 + 馆主 = 5 分）
    """
    if defender_count >= 4:
        base = 3
    elif defender_count >= 1:
        base = 2
    else:
        base = 0
    return base + (RAID_OWNER_BONUS if has_owner else 0)


def _ceil_div3(n: int) -> int:
    """n/3 **向上取整** —— 规则原文「守馆总数除以3由上取整」。

    ⚠️ 这里必须是 ceiling 不是 floor：守馆成功 2 次要得 1 分（ceil(2/3)），
    写成 `n // 3` 会得 0 分。`-(-n // 3)` 是 Python 里对非负数取 ceiling 的惯用写法。
    """
    return -(-n // 3)


def raid_defense_points(hold: int, first_round: int, shutdown: int) -> int:
    """守馆方月度加点（含上限）。

    `ceil(守馆成功/3) + ceil(守馆首轮/3) + 5 × SHUT DOWN`，**封顶 10**。
    首轮终结同时计入守馆成功与守馆首轮（两条奖励独立累积）。
    """
    raw = _ceil_div3(hold) + _ceil_div3(first_round) + RAID_SHUTDOWN_BONUS * shutdown
    return min(RAID_DEFENSE_CAP, raw)


def compute_raid_match(
    match_duels: list[dict], winner: str, raider_team: str, defender_team: str
) -> dict:
    """一场踢馆报的判定与点数 —— **踢馆侧唯一的判定实现**（对应友谊侧的 compute_match_stats）。

    Args:
        match_duels: 本场对局 dict（seq 升序），须含 score_a/score_b/player_b/
            owner/resolved_a/resolved_b。
        winner: `matches.winner`（踢馆方=team_a 或 守馆方=team_b；未定为空串）。
        raider_team: 踢馆方队标（= 战报左侧，`matches.team_a`）。
        defender_team: 守馆方队标（= 战报右侧，`matches.team_b`）。

    胜负本身由 `battle_report_parser.determine_raid_winner` 判定（看最后一场）；
    这里只把「谁赢的、赢了几个人、有没有馆主」翻译成计数与点数。

    Returns:
        dict，字段见下方各 `info[...]` 赋值；`raiders` 是踢馆方出场过的选手名。
    """
    played = [d for d in match_duels if not _unplayed(d)]
    defender_count = len(played)
    has_owner = any(d.get("owner") for d in played)
    # 踢馆方出场者（正常只有 1 人；规则未覆盖多人，按「出场者各记一份」处理）。
    # 无人守馆时 played 为空 —— 此时回退到全部行：占位行的左侧仍是踢馆者的名字，
    # 徽章要记在他头上（这正是解析层保留占位行、不丢整行的原因）。
    raider_rows = played or match_duels
    info = {
        # 由调用方（database._raid_infos）回填真实战报 ID，供「同一场只算一次」去重
        "match_id": 0,
        "raider_team": raider_team,
        "defender_team": defender_team,
        "defender_count": defender_count,
        "has_owner": bool(has_owner),
        "raiders": sorted({str(d["resolved_a"]) for d in raider_rows if d.get("resolved_a")}),
        "raider_success": False,
        "raid_success_owner": False,
        "attack_points": 0,
        "hold": 0,
        "first_round": 0,
        "shutdown": 0,
        "ender": "",       # 终结踢馆者的防守者
        "owner_name": "",  # 该场出场的馆主（仅当是最后一场的防守者时就是 ender）
    }
    if winner == raider_team:
        info["raider_success"] = True
        info["raid_success_owner"] = bool(has_owner)
        info["attack_points"] = raid_attack_points(defender_count, bool(has_owner))
        return info
    if winner == defender_team and played:
        last = played[-1]
        info["hold"] = 1
        info["ender"] = str(last.get("resolved_b") or last.get("player_b") or "")
        # 踢馆者的第一场就被终结 = 守馆首轮
        if int(played[0]["score_b"]) > int(played[0]["score_a"]):
            info["first_round"] = 1
        if last.get("owner"):
            info["owner_name"] = info["ender"]
            # SHUT DOWN：馆主恰好是第 5 个守馆者，且赢下了这最后一场
            if defender_count == 5:
                info["shutdown"] = 1
    return info


def raid_team_view(info: dict, team: str) -> dict | None:
    """把一场踢馆的判定结果换算成「以 team 为视角」的记录；team 未参战返回 None。"""
    if team == info["raider_team"]:
        return {
            "match_id": info["match_id"],
            "opponent": info["defender_team"],
            "defender_count": info["defender_count"],
            "has_owner": info["has_owner"],
            "raid_success": 1 if info["raider_success"] else 0,
            "raid_success_owner": 1 if info["raid_success_owner"] else 0,
            "badge": 1 if (info["raider_success"] and info["defender_count"] == 0) else 0,
            "attack_points": info["attack_points"],
            "hold": 0,
            "first_round": 0,
            "shutdown": 0,
        }
    if team == info["defender_team"]:
        return {
            "match_id": info["match_id"],
            "opponent": info["raider_team"],
            "defender_count": info["defender_count"],
            "has_owner": info["has_owner"],
            "raid_success": 0,
            "raid_success_owner": 0,
            "badge": 0,
            "attack_points": 0,
            "hold": info["hold"],
            "first_round": info["first_round"],
            "shutdown": info["shutdown"],
        }
    return None


def raid_player_views(info: dict) -> list[tuple[str, dict]]:
    """把一场踢馆的判定结果拆分到**个人**：返回 [(玩家名, 视角记录), ...]。

    归谁：

    - 踢馆方：该场出场的每个踢馆者都记一份（正常只有 1 人）
    - 守馆成功 / 守馆首轮 / SHUT DOWN：都归**终结踢馆者的那名防守者**
      （踢馆赛里防守方只可能有一人赢下比赛，所以这三项天然是同一个人）

    无人守馆时踢馆者照样记「踢馆成功 + 徽章」（0 分），与战队侧口径一致 ——
    规则只说「不增加积分」，没说这次踢馆不算数。

    Each view 带 `team` 字段（该玩家所属战队）—— 调用方据此只取自己这一侧的
    记录，避免把对手的防守者也算进本队选手（跨队同名时尤其重要）。
    """
    out: list[tuple[str, dict]] = []
    if info["raider_success"]:
        base = {
            "match_id": info["match_id"],
            "team": info["raider_team"],
            "opponent": info["defender_team"],
            "defender_count": info["defender_count"],
            "has_owner": info["has_owner"],
            "raid_success": 1,
            "raid_success_owner": 1 if info["raid_success_owner"] else 0,
            "badge": 1 if info["defender_count"] == 0 else 0,
            "attack_points": info["attack_points"],
            "hold": 0,
            "first_round": 0,
            "shutdown": 0,
        }
        for name in info["raiders"]:
            out.append((name, dict(base)))
    if info["hold"] and info["ender"]:
        out.append((info["ender"], {
            "match_id": info["match_id"],
            "team": info["defender_team"],
            "opponent": info["raider_team"],
            "defender_count": info["defender_count"],
            "has_owner": info["has_owner"],
            "raid_success": 0,
            "raid_success_owner": 0,
            "badge": 0,
            "attack_points": 0,
            "hold": info["hold"],
            "first_round": info["first_round"],
            "shutdown": info["shutdown"],
        }))
    return out


def aggregate_raid(views: list[dict]) -> dict:
    """把同一归属（某战队 / 某人）的踢馆视角记录汇总为本月计数与积分。

    踢馆方：**当月重复踢穿同一战队不累计，取最高分那场**（按对手队分组取
    MAX 再求和）；不同对手队的成功踢馆累计。
    守馆方：`raid_defense_points`（含封顶 10）。
    """
    best_by_opponent: dict[str, int] = {}
    raid_success = raid_success_owner = badge = 0
    hold = first_round = shutdown = 0
    for v in views:
        raid_success += int(v.get("raid_success", 0))
        raid_success_owner += int(v.get("raid_success_owner", 0))
        badge += int(v.get("badge", 0))
        hold += int(v.get("hold", 0))
        first_round += int(v.get("first_round", 0))
        shutdown += int(v.get("shutdown", 0))
        if v.get("raid_success"):
            opp = str(v.get("opponent", ""))
            best_by_opponent[opp] = max(
                best_by_opponent.get(opp, 0), int(v.get("attack_points", 0))
            )
    attack_points = sum(best_by_opponent.values())
    defense_points = raid_defense_points(hold, first_round, shutdown)
    return {
        "raid_success": raid_success,
        "raid_success_owner": raid_success_owner,
        "badge": badge,
        "hold": hold,
        "first_round": first_round,
        "shutdown": shutdown,
        "attack_points": attack_points,
        "defense_points": defense_points,
        "raid_points": attack_points + defense_points,
    }


def total_points(friendly_points: float, raid_points: int) -> float:
    """总积分 = 友谊积分 + 踢馆积分（最终排名口径）。"""
    return round((friendly_points or 0) + (raid_points or 0), 2)


def merge_raid_into_ranking(rows: list[dict], raid_by_player: dict[str, dict]) -> None:
    """就地把踢馆字段并入排行行，并算出 total_points。

    新增字段：raid_points / total_points / raid_success / raid_success_owner /
    hold / first_round / shutdown / badge。
    """
    for r in rows:
        raid = raid_by_player.get(r["player"], {})
        r["raid_points"] = int(raid.get("raid_points", 0))
        r["raid_success"] = int(raid.get("raid_success", 0))
        r["raid_success_owner"] = int(raid.get("raid_success_owner", 0))
        r["hold"] = int(raid.get("hold", 0))
        r["first_round"] = int(raid.get("first_round", 0))
        r["shutdown"] = int(raid.get("shutdown", 0))
        r["badge"] = int(raid.get("badge", 0))
        r["total_points"] = total_points(r.get("points", 0) or 0, r["raid_points"])


def sort_ranking(rows: list[dict], limit: int | None = None) -> list[dict]:
    """按总积分排序并截断（最终名次口径）。

    `get_player_ranking` 的 SQL 只按友谊积分排；踢馆积分是 Python 侧并进来的，
    所以**最终顺序必须在 Python 侧定**，否则 DB 的 LIMIT 会先按友谊截断、
    把靠踢馆冲上来的选手挡在榜外。并列规则与 build_ranking_cells 一致。
    """
    rows.sort(
        key=lambda r: (
            -(r.get("total_points") or 0),
            -int(r.get("wins", 0)),
            -int(r.get("total", 0)),
            str(r.get("player", "")),
        )
    )
    return rows if limit is None else rows[: max(int(limit), 0)]


# ---------- 月度结算（settlement，v1.15.0）----------

BONUS_MULTIPLIER = 3  # 奖金 = 总积分 × 3，再按名次封顶

# 名次 → 奖金上限。1~4 是默认奖励范围（/结算 不写名次数就奖励到这），
# 5~8 与 9~12 需要管理员显式 `/结算 7月 8` / `/结算 7月 12` 才追加。
RANK_BONUS_CAPS = {
    1: 130, 2: 100, 3: 80, 4: 50,
    5: 40, 6: 35, 7: 30, 8: 25,
    9: 15, 10: 15, 11: 15, 12: 15,
}
MAX_REWARD_RANKS = max(RANK_BONUS_CAPS)  # 12：`/结算` 的名次数上限


def rank_bonus(rank: int, total_points: float) -> float:
    """个人奖金 = `floor(min(总积分 × 3, 该名次上限))`。

    基数是**总积分**（友谊积分 + 踢馆积分），即排行榜的排序依据。
    名次不在 1~12 内（含 0、负数、13+）一律 0 分 —— 封顶表里没有的名次不发钱。

    **向下取整**（v1.15.0 起，此前是四舍五入到 2 位小数）：总积分是小数，
    ×3 之后几乎必定带小数尾巴，奖金一律抹掉成整数，**只舍不入** ——
    34.17 → 34、133.92（且未触顶时）→ 133。触顶的那批本来就是整数（上限表全是
    整数），`floor` 对它们无影响。

    ⚠️ 奖金是**从总积分派生出来的展示值**，不参与 `total_points`、不影响名次。
    结算不改变任何排行数字。
    """
    cap = RANK_BONUS_CAPS.get(rank)
    if cap is None:
        return 0.0
    return float(math.floor(min((total_points or 0) * BONUS_MULTIPLIER, cap)))


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


_RANK_HEADERS = [
    "排名", "队员", "总积分", "友谊积分",
    "胜场", "负场", "总场数", "友谊次数", "胜率", "无双次数",
    "踢馆积分", "守馆成功", "守馆首轮", "踢馆成功",
]

# 奖金列的表头。只在**该月已结算**时才追加到 _RANK_HEADERS 后面（v1.15.0），
# 所以列数是 14 或 15 —— 对齐方式改用 rank_aligns() 函数而不是定长常量。
_BONUS_HEADER = "奖金"


def rank_aligns(ncols: int) -> list[str]:
    """排行各列的对齐方式：**全部居中**（v1.15.0）。

    此前是「队员列左对齐 + 其余右对齐」，两种对齐混排看着不齐。改成函数而不是
    常量，是因为奖金列让列数在 14/15 之间浮动，定长列表跟不动。
    文字表格（`_pad` 的 `"center"` 分支）与图片表格（`chart.draw_cell`）共用。
    """
    return ["center"] * ncols


def ranks_for(rows: list[dict]) -> list[int]:
    """每行的**展示名次**（1 起，并列同名次），与表格里那一列完全一致。

    并列规则：`(total_points, wins, total)` 三者全同则同名次。
    `build_ranking_cells` 与本文件的 `rank_bonus` 调用方（`main.settle`）共用同
    一份 —— 否则会出现「榜上显示第 1 名、奖金却按第 2 档的上限发」这种自相矛盾。
    """
    ranks: list[int] = []
    prev = None
    rank = 0
    for i, r in enumerate(rows, 1):
        total_pts = r.get("total_points")
        if total_pts is None:  # 未合并踢馆字段（旧调用方）时按纯友谊处理
            total_pts = r.get("points", 0) or 0
        key = (total_pts, r.get("wins", 0), r.get("total", 0))
        if key != prev:
            rank = i
            prev = key
        ranks.append(rank)
    return ranks


def build_ranking_cells(rows: list[dict]) -> list[list[object]]:
    """排行行 → 展示单元格（首行=表头）。

    列序（v1.14.0 起）：总积分 / 友谊积分 前置，末四列为踢馆口径
    （踢馆积分、守馆成功、守馆首轮、踢馆成功）。「友谊积分」即原「积分」，
    纯友谊口径（保留原义），「总积分」= 友谊积分 + 踢馆积分。
    名次按 **总积分** 排（与 `sort_ranking` 一致）。
    文字表格（format_player_ranking）与图片表格（chart.make_ranking_image）
    共用本函数，保证两处名次与单元格值一致。

    并列名次规则见 `ranks_for`。

    **奖金列（v1.15.0）**：行里带 `bonus` 键时才追加，即**只有已结算的月份**
    才有这一列。调用方不加这个键（未结算 / `/排行 全部`）就还是 14 列。

    ⚠️ 加列时必须同时改 `_RANK_HEADERS` 和下面数据行的构造 —— 两者都是手写的
    定长列表，对齐数组由 `rank_aligns(ncols)` 跟着列数走，但数据行不会。
    """
    with_bonus = any("bonus" in r for r in rows)
    headers = _RANK_HEADERS + ([_BONUS_HEADER] if with_bonus else [])
    cells = [list(headers)]
    for rank, r in zip(ranks_for(rows), rows):
        pts = r.get("points", 0) or 0
        raid_pts = int(r.get("raid_points", 0))
        total_pts = r.get("total_points")
        if total_pts is None:  # 未合并踢馆字段（旧调用方）时按纯友谊处理
            total_pts = pts
        wins = int(r["wins"])
        losses = int(r["losses"])
        draws = int(r.get("draws", 0))
        played_total = wins + losses + draws  # 总场数不含 0:0 占位
        wr = round(wins * 100.0 / (wins + losses), 1) if (wins + losses) else 0.0
        row = [
            rank, r["player"], f"{total_pts:g}", f"{pts:g}",
            wins, losses, played_total,
            int(r.get("friendship", 0)), f"{wr:.1f}", int(r.get("wushuang", 0)),
            raid_pts, int(r.get("hold", 0)),
            int(r.get("first_round", 0)), int(r.get("raid_success", 0)),
        ]
        if with_bonus:
            row.append(f"{float(r.get('bonus', 0) or 0):g}")
        cells.append(row)
    return cells


def attach_bonus(rows: list[dict], bonus_map: dict[str, float]) -> list[dict]:
    """把 `/结算` 的奖金并进排行行，**仅供已结算的月份**。

    `bonus_map` 是 `database.get_settlement` 的返回值；**未结算的月份它返回空
    dict**，此时**一个 `bonus` 键都不加** —— `build_ranking_cells` 的开关正是
    「行里有没有 `bonus` 键」（`any("bonus" in r …)`）。

    ⚠️ 别把这里写成无条件赋值。抽成函数就是因为它以前长在 `main.py` 的
    `/排行` 分支里，被无条件跑了一遍：`bonus_map` 为空也照样补键 →
    `any(...)` 恒为真 → **未结算的月份也长出第 15 列**，整列 0。
    纯逻辑套件的测试全在 `build_ranking_cells` 那一侧，看不到这条接线。
    """
    if not bonus_map:
        return rows
    for r in rows:
        r["bonus"] = float(bonus_map.get(r["player"], 0.0))
    return rows


# 面板行序（左标签）。v1.15.0 加入，顺序由用户指定：总场数在最上、积分在最下。
_TEAM_PANEL_LABELS = ["总场数", "守馆首轮", "守馆", "踢馆", "胜场", "负场", "胜率", "积分"]


def build_team_panel(record: dict) -> list[tuple[str, str]]:
    """排行图片左侧「战队战绩」面板的 8 行 `(左标签, 右值)`。

    `record` 是 `database.get_home_team_record` 的返回值。

    ⚠️ **守馆首轮 / 守馆 / 踢馆 三行是「各自赚了多少」的原始分量**：
    `ceil(次数/3)` 与 `attack_points`，**刻意不走 `raid_defense_points`** ——
    那个函数带 10 分月度封顶。封顶只体现在「积分」行（取自 `total_points`）。
    所以守馆多的月份里，三行之和会**大于**「积分 − 友谊积分」（被封顶吃掉的部分
    不体现），且面板上看不到 SHUT DOWN 的痕迹。这是既定口径，不是 bug
    —— 见 REQUIREMENTS §5-M14 与 §10 的登记条目。
    「积分」行与排行表格的「总积分」列同源，所以面板和表格永远不矛盾。

    文字表格（`format_player_ranking`）**没有**这个面板：那 14/15 列的契约由
    `build_ranking_cells` 独占，面板是图片独有的（见 REQUIREMENTS §10）。
    """
    return [
        ("总场数", str(int(record.get("total", 0) or 0))),
        # 每个计数都先 int() —— `_ceil_div3` 的 `-(-n // 3)` 遇到 float 会返回
        # float，渲染出来是 "2.0"。
        ("守馆首轮", str(_ceil_div3(int(record.get("first_round", 0) or 0)))),
        ("守馆", str(_ceil_div3(int(record.get("hold", 0) or 0)))),
        ("踢馆", str(int(record.get("attack_points", 0) or 0))),
        ("胜场", str(int(record.get("wins", 0) or 0))),
        ("负场", str(int(record.get("losses", 0) or 0))),
        # win_rate 已经是 round(..., 1)，:.1f 是幂等的
        ("胜率", f"{float(record.get('win_rate', 0) or 0):.1f}%"),
        # :g 与 build_ranking_cells 的 f"{total_pts:g}" 一致，两处小数位不打架
        ("积分", f"{float(record.get('total_points', 0) or 0):g}"),
    ]


def format_player_ranking(rows: list[dict], limit: int | None = 10) -> str:
    """个人积分排行：Excel 风格表格（表头一行，数据行按列对齐）。

    入榜门槛（min_games）不在这里过滤 —— 它在 `get_player_ranking` 的 SQL
    `HAVING total >= %s` 里，传进来的 rows 已经过筛。
    """
    if not rows:
        return "喵～ 还没有战报数据喵。 (=；ω；=)"
    title = (
        "🏆 个人积分榜（全部）喵～"
        if limit is None
        else f"🏆 个人积分榜（前 {limit}）喵～"
    )
    cells = build_ranking_cells(rows)
    return title + "\n" + _format_table(cells, rank_aligns(len(cells[0])))


def _points(wins: int, losses: int) -> float:
    """友谊积分 = 胜场 × 胜率(小数) = 胜场² / (胜场+负场)。"""
    return round(wins * wins / (wins + losses), 2) if (wins + losses) else 0.0


def format_team_record(home_team: str, record: dict, suffix: str = "") -> str:
    """主体战队总体战绩文本（胜/负/平/总/友谊积分/胜率 + 踢馆积分/总积分 + 踢馆三次数）。

    `record` 的踢馆字段由 `database.get_home_team_record` 一并取出；缺失时按 0 处理
    （即该月没有踢馆记录）。友谊口径的胜/负/总**不含**踢馆对局。
    """
    w = int(record.get("wins", 0))
    l = int(record.get("losses", 0))
    d = int(record.get("draws", 0))
    t = int(record.get("total", 0))
    wr = record.get("win_rate", 0)
    pts = _points(w, l)
    raid_pts = int(record.get("raid_points", 0) or 0)
    return (
        f"🏆 {home_team} 总战绩{suffix}喵～\n"
        f"胜{w} 负{l} 平{d}  总{t}  友谊积分{pts}  胜率{wr}%\n"
        f"踢馆积分{raid_pts}  总积分{total_points(pts, raid_pts)}\n"
        f"踢馆成功{int(record.get('raid_success', 0) or 0)}  "
        f"守馆成功{int(record.get('hold', 0) or 0)}  "
        f"守馆首轮{int(record.get('first_round', 0) or 0)}\n"
        f"（用法：/战绩 <玩家名> 查个人战绩喵）"
    )


def format_home_team_vs(home_team: str, rows: list[dict]) -> str:
    """主体战队对战各对手的记录文本（胜/负/总场/胜率，有踢馆则附踢馆得分）。

    踢馆积分按对手队展示；`raid_points` 缺失（该对手没有踢馆）时不显示该段。
    """
    if not rows:
        return f"喵～ {home_team} 还没有对战记录喵。 (=；ω；=)"
    lines = [f"🏆 {home_team} 对战记录喵～"]
    for r in rows:
        line = (
            f"vs {r['opponent']}  胜{r['wins']} 负{r['losses']}  "
            f"总{r['total']}  胜率{r['win_rate']}%"
        )
        if r.get("raid_points"):
            line += f"  踢馆{r['raid_points']}分"
        lines.append(line)
    return "\n".join(lines)


def format_player_record(player: str, agg: dict) -> str:
    """单个玩家战绩文本。

    agg 含 wins/losses/draws/total/friendship（友谊口径），以及踢馆口径的
    raid_points / raid_success / hold / first_round（来自
    `database.get_raid_player_stats`）。
    """
    wins = int(agg.get("wins", 0))
    losses = int(agg.get("losses", 0))
    draws = int(agg.get("draws", 0))
    total = int(agg.get("total", wins + losses + draws))
    friendship = int(agg.get("friendship", 0))
    wr = round(wins * 100.0 / (wins + losses), 1) if (wins + losses) else 0.0
    pts = _points(wins, losses)
    raid_pts = int(agg.get("raid_points", 0) or 0)
    return (
        f"📊 {player} 战绩喵～ 胜{wins} 负{losses} 平{draws}  "
        f"总{total}  友谊{friendship}  友谊积分{pts}  踢馆积分{raid_pts}  "
        f"总积分{total_points(pts, raid_pts)}  胜率{wr}%\n"
        f"踢馆成功{int(agg.get('raid_success', 0) or 0)}  "
        f"守馆成功{int(agg.get('hold', 0) or 0)}  "
        f"守馆首轮{int(agg.get('first_round', 0) or 0)}"
    )


def format_raid_record(title: str, agg: dict) -> str:
    """踢馆卡文本（战队或个人的月度踢馆汇总）。

    `agg` 是 `aggregate_raid` 的返回值。计数字段对齐「守馆成功 / 守馆首轮 /
    踢馆成功 / 踢馆成功含馆主」四项，外加积分明细与合计。
    """
    return (
        f"⚔️ {title} 踢馆（本月）喵～\n"
        f"踢馆成功{agg['raid_success']}  踢馆成功含馆主{agg['raid_success_owner']}"
        f"  无人守馆徽章{agg['badge']}\n"
        f"守馆成功{agg['hold']}  守馆首轮{agg['first_round']}"
        f"  SHUT DOWN{agg['shutdown']}\n"
        f"踢馆加点{agg['attack_points']}  守馆加点{agg['defense_points']}"
        f"（上限{RAID_DEFENSE_CAP}）  踢馆积分{agg['raid_points']}"
    )


def _settlement_lines(entries: list[dict]) -> list[str]:
    """结算明细行 + 合计（`format_settlement` 与 `format_settlement_view` 共用）。

    `entries` 每项含 rank_no / player / total_points / bonus。
    抽出来是为了让「发奖金的回执」与「查已发结果的回执」行体逐字相同 ——
    不然两处各写一遍，改了一边另一边就悄悄不一致。
    """
    lines = [
        f"第{e['rank_no']}名  {e['player']}  总积分{e['total_points']:g}"
        f"  奖金{e['bonus']:g}"
        for e in entries
    ]
    lines.append(f"合计发放 {sum(float(e['bonus'] or 0) for e in entries):g}")
    return lines


def format_settlement(
    home_team: str, year: int, month: int, entries: list[dict], reward_ranks: int
) -> str:
    """结算回执（**管理/群主**跑 `/结算`，刚写完库）：明细 + 合计。

    **年份一定要回显**：`/结算 十二月` 在 9 月会落到去年度（见 `settle_month_range`），
    只写「12月」会让管理员以为结的是今年 —— 那一年根本还没到。
    """
    lines = [f"⚔️ {home_team} {year}年{month}月 结算完成（奖励前 {reward_ranks} 名）"]
    lines += _settlement_lines(entries)
    return "\n".join(lines)


def format_settlement_view(
    home_team: str, year: int, month: int, entries: list[dict]
) -> str:
    """已结算结果的**只读**回执（**非管理/群主**跑 `/结算`）：明细 + 合计。

    与 `format_settlement` 只差抬头：那个是「结算完成」（刚写完库），这个是
    「结算查询」（什么都没改）—— 措辞必须能一眼区分，否则会有人以为钱刚发出去。

    名次数取 `len(entries)`：库里存的就是这次实际发出去的那几行（`set_settlement`
    先 DELETE 再 INSERT，缩回前 4 名时多余行会被删掉）。
    """
    lines = [f"📋 {home_team} {year}年{month}月 结算查询（奖励前 {len(entries)} 名）"]
    lines += _settlement_lines(entries)
    lines.append("（以上是已经结算好的结果喵，这次一个字都没改～ 🐾）")
    return "\n".join(lines)


# ---------- 结算公告（@ 获奖成员，v1.15.0）----------

def settlement_awards_changed(
    old_entries: list[dict], new_entries: list[dict]
) -> bool:
    """获奖名次是否变了 —— 决定本次结算要不要 @ 全群。

    只比 `(名次, 获奖人)`，**不比奖金**：奖金是从总积分派生的，事后补录几场
    战报让积分微调、名次却没动时，不该为那点零头再 @ 一次所有人。
    这是既定口径（见 REQUIREMENTS §10.4）。

    首次结算时 `old_entries` 为空 → 一定为真 → 一定 @（该 @ 的就是第一次）。
    """
    def awards(entries: list[dict]) -> list[tuple]:
        return [(e["rank_no"], e["player"]) for e in entries]

    return awards(old_entries) != awards(new_entries)


def settlement_announcement(names: list[str]) -> list[tuple[str, str]]:
    """结算公告的**分段**内容：`("text", 文本)` 或 `("at", 成员名)`。

    返回分段而不是成品字符串，是因为 @ 必须是真正的 `At` 消息组件（QQ 上才会
    真弹出提醒），拼进字符串就只剩一个字面的「@名字」。`stats.py` 不依赖
    AstrBot，所以这里只描述「哪一段要 @ 谁」，由 `main.py` 翻译成组件。

    猫娘口吻 —— 文案改动不影响任何数值，随便改。
    """
    segments: list[tuple[str, str]] = [
        ("text", "喵～ 本月的奖金已经结算好啦！(=^･ω･^=)\n"
                 "这几位小伙伴快来领奖喵："),
    ]
    for i, name in enumerate(names):
        if i:
            segments.append(("text", " "))
        segments.append(("at", name))
    segments.append(
        ("text", "\n快去找 战队管理员 领取喵～ 记得收好小钱钱喵！🐾")
    )
    return segments


def format_settlement_repeat() -> str:
    """重跑结算、但获奖名次和上次一模一样时的提示（**不 @ 任何人**）。

    附在回执末尾，解释为什么这次没有 @ 全群 —— 否则管理员会以为公告发失败了。
    """
    return "\n\n喵～ 这次结算出来的获奖名次和上次一样，就不打扰大家了喵～ 🐾"


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
        "踢馆战报同样用 /发送，格式：\n"
        "KC踢馆FH\n"
        "规则：OCG.2026.7.1.MATCH\n"
        "2026.9.4 20:00\n"
        "踢馆开始！\n"
        "雨落 2:1 云猫(馆主)\n"
        "（/战报 仍可用）"
    ),
    "管理": (
        "▎管理\n"
        "/绑定战队 <战队>        绑定本群战队（管理/群主）\n"
        "/查看战队              查看本群战队\n"
        "/战队列表               查看全部战队\n"
        "/战报删除 <战报ID>      仅管理/群主\n"
        "/战报撤销              撤销自己最近一条\n"
        "/结算 [月份] [名次数]   月度结算（默认上个月）：管理/群主发奖金（默认前 4 名）\n"
        "                        并 @ 获奖成员；其他人查询已结算结果（不会改动数据）\n"
        "/重置结算 [月份]        把该月恢复成未结算（管理/群主，默认上个月）"
    ),
    "查询": (
        "▎查询（默认本月，末尾可加 X月 查其他月份，如 七月/7月）\n"
        "/排行 [个人|队伍] [X月]  排行榜\n"
        "/战绩 [玩家名] [X月]    战绩（无玩家名=本战队）\n"
        "/趋势 <玩家名|队伍> [最近N天|X月]  胜率走势图\n"
        "/导出 [玩家名] [胜场|负场|全部] [X月|最近N天] [csv|json]  导出（默认本月）\n"
        "/我的战绩 [X月]   我的总战绩\n"
        "/踢馆 [玩家名] [X月]  踢馆成绩（无玩家名=本战队）\n"
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
    parts = ["📋 战队对战战报插件喵～ (=^･ω･^=)", "━━━━━━━━━━━━"]
    for k in section_keys:
        parts.append(HELP_SECTIONS[k])
    parts.append("群属性：/群聊属性 <友谊群|战报群|主群>（管理/群主）")
    parts.append("/帮助 全部 查看全部 | /帮助 超管 查看超管指令")
    parts.append("🐾 有不懂的地方随时喊我喵～")
    return "\n\n".join(parts)
