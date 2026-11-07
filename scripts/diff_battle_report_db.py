"""比对线上战报系统导出的数据与插件 MySQL 库，输出差异报告。

配合 scripts/crawl_battle_reports.py 使用：先爬取导出 JSON，再用本脚本连库比对。
**全程只读 SELECT，不写库。**

匹配键（网站 ↔ 库）：**内容指纹** = 比赛时间 + 战队集合(无序) + 地点 + 规范化对局序列。
不能用 (比赛时间, 战队集合, 地点) —— 地点是固定房间号，同一天同一房间会打好几场，
该键根本不唯一。

只比对**友谊战报**（`matches.kind = 'friendly'`）：网站只收录友谊赛，踢馆报进来
只会全落进「只在库里有」。`--include-raid` 可关掉这个过滤。

输出差异：
    1. 库里缺失        网站有、库中没有（多为对手方发布、本群未提交的场次）
    2. 只在库里有      库中有、区间内网站没有
    3. 库内重复        同内容多条 —— 会让统计直接翻倍
    4. 网站侧重复      网站同一份战报有多条 ID
    5. 胜负不一致      内容一致，但库判胜方与网站『胜方』列不符
    6. 同场次多版      同日期+战队+地点但对局内容不同（双方各报一版，指纹拦不住）
       └ 疑似重复计数  其中的子集：**库内条数多于网站同场次条数**，多出来的需人工核对

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
    from battle_report_parser import (
        KIND_FRIENDLY, BattleReport, Duel, determine_match_winner,
    )
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


from report_comparison import (
    _key, _slug, _winner_from_duels, _canon_duel_list, _site_duels, _db_duels,
    split_by_kind, _site_key, _db_key, _session_key, _db_winner, compare,
)


async def fetch_db(pool, start: str, end: str, home_team: str | None,
                   *, include_raid: bool = False) -> tuple[list[dict], dict, dict]:
    """只读拉取区间内的 matches 与其 duels。

    默认**只取友谊战报**（`matches.kind = 'friendly'`）—— 线上战报站
    rep.ygobbs2.com 只收录友谊赛，踢馆报拿来比对只会全部落进「只在库里有」，
    把结果污染成"库里多算"。插件自己的统计查询（database.py）也是这么过滤的。

    返回 `(matches, duels, excluded)`，`excluded` 是被排除的 kind 计数。
    """
    where = "match_time BETWEEN %s AND %s"
    params: list = [start, end]
    if home_team:
        where += " AND home_team = %s"
        params.append(home_team)

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, group_id, home_team, team_a, team_b, match_time, rule, "
                "location, winner, submitted_by, submitted_name, created_at, kind "
                f"FROM matches WHERE {where} ORDER BY id",
                params,
            )
            rows = list(await cur.fetchall())

            matches, excluded = split_by_kind(rows, include_raid=include_raid)

            duels: dict[int, list[dict]] = defaultdict(list)
            ids = [m["id"] for m in matches]
            for i in range(0, len(ids), 500):  # 分批，避免 IN 过长
                batch = ids[i : i + 500]
                if not batch:
                    continue
                marks = ",".join(["%s"] * len(batch))
                await cur.execute(
                    "SELECT match_id, round_no, player_a, score_a, player_b, score_b, "
                    "player_a_team, player_b_team, result, seq, a_sub, b_sub, ruled "
                    f"FROM duels WHERE match_id IN ({marks}) ORDER BY match_id, seq, id",
                    batch,
                )
                for d in await cur.fetchall():
                    duels[d["match_id"]].append(d)

    for m in matches:
        if hasattr(m["match_time"], "isoformat"):
            m["match_time"] = m["match_time"].isoformat()
    return matches, duels, dict(excluded)

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
    excluded = meta.get("excluded") or {}
    if excluded:
        detail = "、".join(f"{k} {v} 条" for k, v in sorted(excluded.items()))
        add(f"- 已排除：{detail}（网站只收录友谊赛，**不参与比对**）")
    add("")
    add("## 差异汇总")
    add("")
    add("| 类别 | 条数 | 说明 |")
    add("|---|---|---|")
    add(f"| 库里缺失 | {len(res['missing_in_db'])} | 网站有、本地未匹配（待核） |")
    add(f"| 只在库里有 | {len(res['only_in_db'])} | 库中有、网站本次发布区间未检出（待核） |")
    add(f"| **库内重复** | {len(res['db_duplicate_keys'])} 组 / "
        f"{sum(len(v) - 1 for v in res['db_duplicate_keys'].values())} 条冗余 | "
        f"同一份战报在库中存了多条，统计直接翻倍 |")
    add(f"| 网站侧重复 | {len(res['site_duplicate_keys'])} 组 | 网站同一份战报有多条 ID |")
    add(f"| 胜负不一致 | {len(res['winner_mismatch'])} | 内容一致但胜方判定不同 |")
    over = [g for g in res["db_session_groups"] if g["db_exceeds_site"]]
    over_n = sum(len(g["matches"]) - g["site_count"] for g in over)
    add(f"| 同场次多版（待核） | {len(res['db_session_groups'])} 组 | "
        f"同日期+同战队+同地点但对局内容不同，指纹去重拦不住 |")
    add(f"| **其中疑似重复计数** | {len(over)} 组 / {over_n} 条 | "
        f"库内条数**多于**网站同场次条数，仅为条数差异，需结合发布时间和原文核对 |")
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
            "网站有记录但库里没有——多为对手方发布、本群未提交的场次，会让统计偏少。")
    section("只在库里有", [_db_line(m) for m in res["only_in_db"]],
            "库中有但网站该区间内没有，会让统计偏多。")

    dup_rows = []
    for k, rows in res["db_duplicate_keys"].items():
        ids = [m["id"] for m in rows]
        head = rows[0]
        dup_rows.append(
            f"match#{ids}  {head['match_time']} {head['team_a']} VS {head['team_b']}"
            f" @ {head['location'] or '?'}（{len(ids)} 条同内容）"
        )
    section("库内重复（同内容多条）", dup_rows,
            "同一份战报在库中存在多条记录，排行/战绩会对该场重复计数。")

    session_rows = []
    for g in sorted(res["db_session_groups"],
                    key=lambda g: (not g["db_exceeds_site"], g["match_time"])):
        flag = (f"　⚠ 网站同场次只有 {g['site_count']} 条，库内多 "
                f"{len(g['matches']) - g['site_count']} 条") if g["db_exceeds_site"] else ""
        session_rows.append(
            f"{g['match_time']} {' VS '.join(g['teams'])} @ {g['location'] or '?'}"
            f"（库 {len(g['matches'])} 条不同内容）{flag}"
            + "".join(
                f"\n  - match#{m['db_id']} 库判 {m['winner'] or '(空)'} 胜"
                f" ｜{m['duel_count']} 对局 ｜提交 {m['submitted_name'] or '-'}"
                for m in g["matches"]
            )
        )
    section("同场次多版（同日期+战队+地点，内容不同）", session_rows,
            "双方各报一版时，对局只包含自己那半场，内容指纹拦不住 → 库里存成两条。\n"
            "网站本次发布范围内的条数仅作参考，不代表当天实际总场次：\n"
            "- ⚠ 标出的组库里多于网站 → 仅为条数差异，需结合发布时间和原文核对；\n"
            "- 未标出的组两边条数一致，也不能仅凭条数认定是否重复；\n"
            "- 两边胜方相反**不构成**重复的证据，不要只凭它删记录。")

    section("胜负不一致",
            [f"{_site_line(r)}\n  - 库：{_db_line(m)}（库判 {w}）"
             for r, m, w in res["winner_mismatch"]])

    site_dup_rows = [
        f"网站 ID {[r['id'] for r in rows]}  {rows[0]['match_time']} "
        f"{rows[0]['team_a']} VS {rows[0]['team_b']}（{len(rows)} 条同内容）"
        for rows in res["site_duplicate_keys"].values()
    ]
    section("网站侧重复", site_dup_rows, "同一份战报在网站上被提交了多次。")

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
    extra = sum(len(v) - 1 for v in res["db_duplicate_keys"].values())
    print(f"  库内重复      {len(res['db_duplicate_keys'])} 组 / {extra} 条冗余")
    print(f"  网站侧重复    {len(res['site_duplicate_keys'])} 组")
    print(f"  胜负不一致    {len(res['winner_mismatch'])}")
    over = [g for g in res["db_session_groups"] if g["db_exceeds_site"]]
    over_n = sum(len(g["matches"]) - g["site_count"] for g in over)
    print(f"  同场次多版    {len(res['db_session_groups'])} 组"
          f"（其中疑似重复计数 {len(over)} 组 / {over_n} 条）")
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
    ap.add_argument("--include-raid", action="store_true",
                    help="连踢馆战报一起比对（默认排除：网站只收录友谊赛）")
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
            db_matches, db_duels, excluded = await fetch_db(
                pool, args.start, args.end, home_team, include_raid=args.include_raid,
            )
        except (aiomysql.Error, OSError) as e:
            print(f"✗ 查询数据库失败：{e}")
            return 1
    finally:
        pool.close()
        await pool.wait_closed()

    print(f"已载入库 matches {len(db_matches)} 条"
          + (f"（home_team={home_team}）" if home_team else "（全部战队）"))
    if excluded:
        detail = "、".join(f"{k} {v} 条" for k, v in sorted(excluded.items()))
        print(f"  已排除 {detail}（网站只收录友谊赛，不参与比对）")
    res = compare(site_records, db_matches, db_duels)
    print_summary(res)

    if args.dry_run:
        print("dry-run：未写报告文件。")
        return 0

    meta = {"group": args.group, "start": args.start, "end": args.end,
            "excluded": excluded, "include_raid": args.include_raid}
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
                "missing_in_db": [
                    {"site_id": r["id"], "match_time": r["match_time"],
                     "teams": [r["team_a"], r["team_b"]], "location": r["location"],
                     "publisher": r["publisher"]}
                    for r in res["missing_in_db"]
                ],
                "only_in_db": [
                    {"db_id": m["id"], "match_time": m["match_time"],
                     "teams": [m["team_a"], m["team_b"]], "location": m["location"],
                     "group_id": m["group_id"], "submitted_name": m["submitted_name"]}
                    for m in res["only_in_db"]
                ],
                "db_duplicates": [
                    {"db_ids": [m["id"] for m in rows], "match_time": rows[0]["match_time"],
                     "teams": [rows[0]["team_a"], rows[0]["team_b"]],
                     "location": rows[0]["location"]}
                    for rows in res["db_duplicate_keys"].values()
                ],
                "site_duplicates": [
                    {"site_ids": [r["id"] for r in rows], "match_time": rows[0]["match_time"],
                     "teams": [rows[0]["team_a"], rows[0]["team_b"]]}
                    for rows in res["site_duplicate_keys"].values()
                ],
                "winner_mismatch": [
                    {"site_id": r["id"], "db_id": m["id"], "site": r["winner_site"], "db": w}
                    for r, m, w in res["winner_mismatch"]
                ],
                "db_session_groups": res["db_session_groups"],
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已导出：\n  {md_path}\n  {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
