"""比对线上战报系统导出的数据与插件 MySQL 库，输出差异报告。

配合 scripts/crawl_battle_reports.py 使用：先爬取导出 JSON，再用本脚本连库比对。
**全程只读 SELECT，不写库。**

匹配键（网站 ↔ 库）：
    主键 (比赛时间, 战队集合(无序), 地点)
    回退 (比赛时间, 战队集合(无序))      —— 兼容库内 location 为空的历史数据

输出四类差异：
    1. 库里缺失      网站有、库中没有（多为对手方发布、本群未提交的场次）
    2. 只在库里有    库中有、区间内网站没有（疑似误报/重复入库）
    3. 胜负不一致    两边都匹配上，但库记录的胜方与网站『胜方』列不符
    4. 明细不一致    时间/战队/地点都匹配，但对局序列不同（同场次存在两份不同版本）

用法：
    python scripts/diff_battle_report_db.py
    python scripts/diff_battle_report_db.py --export scripts/output/battle_report_KC_....json
    python scripts/diff_battle_report_db.py --dry-run      # 只打印，不写报告文件

连库参数默认从环境变量取（ASTRBOT_MYSQL_HOST/PORT/USER/PASSWORD/DB），与
scripts/fix_ruled_legacy.py 保持一致；线上库凭据需自行提供。
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

try:
    import aiomysql
except ImportError:
    print("缺少 aiomysql，请先安装：pip install aiomysql")
    sys.exit(1)

# 让脚本可以从插件根目录以包方式导入解析器
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from battle_report_parser import BattleReport, Duel, determine_match_winner
except ImportError:
    print("无法导入 battle_report_parser，请检查脚本路径")
    sys.exit(1)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

DEFAULT_HOST = os.environ.get("ASTRBOT_MYSQL_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("ASTRBOT_MYSQL_PORT", "3306"))
DEFAULT_USER = os.environ.get("ASTRBOT_MYSQL_USER", "root")
DEFAULT_PASSWORD = os.environ.get("ASTRBOT_MYSQL_PASSWORD", "")
DEFAULT_DB = os.environ.get("ASTRBOT_MYSQL_DB", "astrbot_battle_report")


# ---------- 匹配键与归一化 ----------

def _key(match_time: str, teams, location: str) -> tuple:
    teams = tuple(sorted(t.upper() for t in teams if t))
    return (match_time, teams, (location or "").strip())


def _slug(match_time: str, teams) -> tuple:
    return _key(match_time, teams, "")[:2]


def _winner_from_duels(duels: list[dict], match: dict) -> str | None:
    """库内 winner 为空时，用 duels 复算胜方。

    战队以 duels 自带的 player_a_team/player_b_team 为准，避免 matches.team_a 与
    对局归属不一致时判错边。
    """
    team_a, team_b = match["team_a"], match["team_b"]
    if duels:
        pa_team = (duels[0].get("player_a_team") or "").upper()
        pb_team = (duels[0].get("player_b_team") or "").upper()
        if pa_team and pb_team:
            team_a, team_b = pa_team, pb_team
    rep = BattleReport(
        team_a=team_a,
        team_b=team_b,
        rule=match.get("rule") or "",
        match_time=match.get("match_time") or "",
    )
    rep.duels = [
        Duel(d["round_no"], d["player_a"], d["score_a"], d["player_b"], d["score_b"])
        for d in duels
    ]
    return determine_match_winner(rep)


def _canon_duel_list(rows: list[tuple], team_a: str, team_b: str) -> list[tuple]:
    """把对局列表规范化成与战队书写顺序无关的形式，并丢弃 0:0 未打的占位。

    rows 每项为 (round_no, player_a, score_a, player_b, score_b)，player_a 属 team_a。
    """
    t1, _t2 = sorted([team_a.upper(), team_b.upper()])
    out = []
    for round_no, pa, sa, pb, sb in rows:
        if sa == 0 and sb == 0:
            continue
        if team_a.upper() == t1:
            out.append((round_no, pa, sa, pb, sb))
        else:
            out.append((round_no, pb, sb, pa, sa))
    return sorted(out)


def _site_duels(record: dict) -> list[tuple]:
    rows = [
        (d["round_no"], d["player_a"], d["score_a"], d["player_b"], d["score_b"])
        for d in record.get("duels", [])
    ]
    return _canon_duel_list(rows, record["team_a"], record["team_b"])


def _db_duels(duels: list[dict], match: dict) -> list[tuple]:
    rows = [
        (d["round_no"], d["player_a"], d["score_a"], d["player_b"], d["score_b"])
        for d in duels
    ]
    # player_a 属 player_a_team，须与该场的 team_a 对齐后再规范化
    t_a = match["team_a"]
    if duels and (duels[0].get("player_a_team") or "").upper() != t_a.upper():
        rows = [(r, pb, sb, pa, sa) for r, pa, sa, pb, sb in rows]
    return _canon_duel_list(rows, t_a, match["team_b"])


# ---------- 取数与比对 ----------

async def fetch_db(pool, start: str, end: str, home_team: str | None) -> tuple[list[dict], dict]:
    """只读拉取区间内的 matches 与其 duels。"""
    where = "match_time BETWEEN %s AND %s"
    params: list = [start, end]
    if home_team:
        where += " AND home_team = %s"
        params.append(home_team)

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, group_id, home_team, team_a, team_b, match_time, rule, "
                "location, winner, submitted_by, submitted_name, created_at "
                f"FROM matches WHERE {where} ORDER BY id",
                params,
            )
            matches = list(await cur.fetchall())

            duels: dict[int, list[dict]] = defaultdict(list)
            ids = [m["id"] for m in matches]
            for i in range(0, len(ids), 500):  # 分批，避免 IN 过长
                batch = ids[i : i + 500]
                if not batch:
                    continue
                marks = ",".join(["%s"] * len(batch))
                await cur.execute(
                    "SELECT match_id, round_no, player_a, score_a, player_b, score_b, "
                    "player_a_team, player_b_team, result "
                    f"FROM duels WHERE match_id IN ({marks}) ORDER BY match_id, round_no, id",
                    batch,
                )
                for d in await cur.fetchall():
                    duels[d["match_id"]].append(d)

    for m in matches:
        if hasattr(m["match_time"], "isoformat"):
            m["match_time"] = m["match_time"].isoformat()
    return matches, duels


def compare(site_records: list[dict], db_matches: list[dict], db_duels: dict) -> dict:
    """产出四类差异 + 库内重复场次。"""
    site = [r for r in site_records if r.get("parse_ok") and r.get("match_time_in_range")]
    out_of_range = [r for r in site_records if r.get("parse_ok") and not r.get("match_time_in_range")]
    parse_failed = [r for r in site_records if not r.get("parse_ok")]

    by_primary: dict[tuple, list[dict]] = defaultdict(list)
    by_slug: dict[tuple, list[dict]] = defaultdict(list)
    for m in db_matches:
        p = _key(m["match_time"], [m["team_a"], m["team_b"]], m["location"])
        by_primary[p].append(m)
        by_slug[p[:2]].append(m)

    consumed: set = set()
    missing: list[dict] = []
    only_dupe_extra: list[tuple] = []
    winner_mismatch: list[tuple] = []
    duel_mismatch: list[tuple] = []
    matched = 0

    for r in site:
        p = _key(r["match_time"], [r["team_a"], r["team_b"]], r["location"])
        free = [m for m in by_primary.get(p, []) if m["id"] not in consumed]
        if not free:
            free = [m for m in by_slug.get(p[:2], []) if m["id"] not in consumed]
        if not free:
            missing.append(r)
            continue

        m = free[0]
        consumed.add(m["id"])
        matched += 1
        if len(free) > 1:  # 同一场比赛在库里有多条记录
            only_dupe_extra.append((r, [x["id"] for x in free]))

        db_winner = (m["winner"] or "").upper() or _winner_from_duels(db_duels.get(m["id"], []), m)
        if db_winner and db_winner != r["winner_site"].upper():
            winner_mismatch.append((r, m, db_winner))

        if _site_duels(r) != _db_duels(db_duels.get(m["id"], []), m):
            duel_mismatch.append((r, m))

    only_in_db = [m for m in db_matches if m["id"] not in consumed]
    db_dupes = {k: v for k, v in by_primary.items() if len(v) > 1}

    return {
        "site_total": len(site_records),
        "site_in_range": len(site),
        "db_total": len(db_matches),
        "matched": matched,
        "missing_in_db": missing,
        "only_in_db": only_in_db,
        "winner_mismatch": winner_mismatch,
        "duel_mismatch": duel_mismatch,
        "multi_match_in_db": only_dupe_extra,
        "db_duplicate_keys": db_dupes,
        "site_out_of_range": out_of_range,
        "site_parse_failed": parse_failed,
    }


# ---------- 输出 ----------

def _site_line(r: dict) -> str:
    return (f"`{r['id']}` {r['match_time']} {r['team_a']} VS {r['team_b']}"
            f" @ {r['location'] or '?'} ｜网站判 {r['winner_site']} 胜 {r['loser_site']} 负"
            f" ｜发布 {r['publisher']}")


def _db_line(m: dict) -> str:
    return (f"match#{m['id']} {m['match_time']} {m['team_a']} VS {m['team_b']}"
            f" @ {m['location'] or '?'} ｜库判 {(m['winner'] or '(空)')} 胜"
            f" ｜home={m['home_team']} group={m['group_id']}")


def build_report(res: dict, meta: dict, limit: int) -> str:
    L: list[str] = []
    add = L.append
    add("# 战报比对报告")
    add("")
    add(f"- 战队：{meta['group']}　区间：{meta['start']} ~ {meta['end']}")
    add(f"- 网站抓取：**{res['site_total']}** 条（区间内可解析 {res['site_in_range']} 条）")
    add(f"- 数据库 matches：**{res['db_total']}** 条（home_team={meta['group']}）")
    add(f"- 成功匹配：**{res['matched']}** 条")
    add("")
    add("## 差异汇总")
    add("")
    add("| 类别 | 条数 | 说明 |")
    add("|---|---|---|")
    add(f"| 库里缺失 | {len(res['missing_in_db'])} | 网站有、库中没有 |")
    add(f"| 只在库里有 | {len(res['only_in_db'])} | 库中有、网站区间内没有 |")
    add(f"| 胜负不一致 | {len(res['winner_mismatch'])} | 匹配上但胜方判定不同 |")
    add(f"| 明细不一致 | {len(res['duel_mismatch'])} | 匹配上但对局序列不同 |")
    add(f"| 库内同场多条 | {len(res['multi_match_in_db'])} | 同一场比赛库中存在多条记录 |")
    add(f"| 库内重复键 | {len(res['db_duplicate_keys'])} | 库中键冲突场次数 |")
    add("")

    def section(title: str, rows: list[str], note: str = "") -> None:
        add(f"## {title}（{len(rows)}）")
        add("")
        if note:
            add(note)
            add("")
        if not rows:
            add("无。")
            add("")
            return
        for line in rows[:limit]:
            add(f"- {line}")
        if len(rows) > limit:
            add(f"- …另有 {len(rows) - limit} 条，见同名 .json")
        add("")

    section("库里缺失", [_site_line(r) for r in res["missing_in_db"]],
            "这些场次网站有记录但库里没有——多为对手方发布、本群未提交。")
    section("只在库里有", [_db_line(m) for m in res["only_in_db"]],
            "库中有但网站该区间内没有，疑似误报或重复入库。")
    section("胜负不一致",
            [f"{_site_line(r)}\n  - 库：{_db_line(m)}（库判 {w}）"
             for r, m, w in res["winner_mismatch"]])
    section("明细不一致",
            [f"{_site_line(r)}\n  - 库：{_db_line(m)}" for r, m in res["duel_mismatch"]],
            "对局序列不同，可能是同场次的两份不同版本战报。")
    section("库内同场多条",
            [f"库中 match#{ids} 同时匹配 {_site_line(r)}" for r, ids in res["multi_match_in_db"]],
            "同一场比赛在库中有多条记录，会让统计重复计数。")

    add("## 其它")
    add("")
    add(f"- 网站记录比赛时间超出区间（未参与比对）：{len(res['site_out_of_range'])} 条"
        + (f" → {[r['id'] for r in res['site_out_of_range']]}" if res["site_out_of_range"] else ""))
    add(f"- 网站记录正文解析失败：{len(res['site_parse_failed'])} 条"
        + (f" → {[r['id'] for r in res['site_parse_failed']]}" if res["site_parse_failed"] else ""))
    add("")
    return "\n".join(L)


def print_summary(res: dict) -> None:
    print()
    print("=" * 68)
    print(f"网站 {res['site_total']} 条（区间内 {res['site_in_range']}）  "
          f"库 {res['db_total']} 条  匹配 {res['matched']} 条")
    print(f"  库里缺失      {len(res['missing_in_db'])}")
    print(f"  只在库里有    {len(res['only_in_db'])}")
    print(f"  胜负不一致    {len(res['winner_mismatch'])}")
    print(f"  明细不一致    {len(res['duel_mismatch'])}")
    print(f"  库内同场多条  {len(res['multi_match_in_db'])}")
    if res["site_out_of_range"]:
        print(f"  (比赛时间越界未比对 {len(res['site_out_of_range'])} 条)")
    print("=" * 68)


async def main() -> int:
    ap = argparse.ArgumentParser(description="比对线上战报导出数据与插件 MySQL 库（只读）")
    ap.add_argument("--export", default=None, help="爬取导出的 JSON 路径")
    ap.add_argument("--group", default="KC", help="战队名（默认 KC）")
    ap.add_argument("--start", default="2026-09-01")
    ap.add_argument("--end", default="2026-09-30")
    ap.add_argument("--home-team", default=None,
                    help="只比对该 home_team 的库记录（默认取 --group；传空串比对全部）")
    ap.add_argument("--out-dir", default=None, help="报告输出目录（默认 scripts/output）")
    ap.add_argument("--limit", type=int, default=30, help="每类在 Markdown 中最多列出的条数")
    ap.add_argument("--dry-run", action="store_true", help="只打印摘要，不写报告文件")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    out_dir = Path(args.out_dir) if args.out_dir else script_dir / "output"
    export_path = (
        Path(args.export) if args.export
        else out_dir / f"battle_report_{args.group}_{args.start}_{args.end}.json"
    )
    if not export_path.exists():
        print(f"✗ 找不到导出文件：{export_path}")
        print("  请先运行：python scripts/crawl_battle_reports.py")
        return 1

    payload = json.loads(export_path.read_text(encoding="utf-8"))
    site_records = payload["records"]
    print(f"已载入网站数据 {len(site_records)} 条 ← {export_path}")

    home_team = args.group if args.home_team is None else (args.home_team or None)

    try:
        pool = await aiomysql.create_pool(
            host=args.host, port=args.port, user=args.user, password=args.password,
            db=args.db, charset="utf8mb4", autocommit=True, minsize=1, maxsize=3,
        )
    except (aiomysql.Error, OSError) as e:
        print(f"✗ 连接数据库失败：{e}")
        print("  用 --host/--port/--user/--password/--db 指定，"
              "或设置 ASTRBOT_MYSQL_HOST/PORT/USER/PASSWORD/DB 环境变量。")
        return 1

    try:
        try:
            db_matches, db_duels = await fetch_db(pool, args.start, args.end, home_team)
        except (aiomysql.Error, OSError) as e:
            print(f"✗ 查询数据库失败：{e}")
            return 1
    finally:
        pool.close()
        await pool.wait_closed()

    print(f"已载入库 matches {len(db_matches)} 条"
          + (f"（home_team={home_team}）" if home_team else "（全部战队）"))
    res = compare(site_records, db_matches, db_duels)
    print_summary(res)

    if args.dry_run:
        print("dry-run：未写报告文件。")
        return 0

    meta = {"group": args.group, "start": args.start, "end": args.end}
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"diff_{args.group}_{args.start}_{args.end}"
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"
    md_path.write_text(build_report(res, meta, args.limit), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "meta": meta,
                "summary": {k: (len(v) if isinstance(v, (list, dict)) else v)
                            for k, v in res.items()},
                "missing_in_db": [r["id"] for r in res["missing_in_db"]],
                "only_in_db": [m["id"] for m in res["only_in_db"]],
                "winner_mismatch": [
                    {"site_id": r["id"], "db_id": m["id"], "site": r["winner_site"], "db": w}
                    for r, m, w in res["winner_mismatch"]
                ],
                "duel_mismatch": [
                    {"site_id": r["id"], "db_id": m["id"]} for r, m in res["duel_mismatch"]
                ],
                "multi_match_in_db": [
                    {"site_id": r["id"], "db_ids": ids} for r, ids in res["multi_match_in_db"]
                ],
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已导出：\n  {md_path}\n  {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
