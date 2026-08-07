"""一次性数据修复脚本：修正战报队标误加「队」后缀的问题。

背景：某场比赛队标输入错误（如 KC 误录为「KC队」），导致 matches.team_a /
duels.player_*_team / player_ids.home_team 混入脏队标。按 home_team / player_*_team
过滤的统计（排行/战绩/导出）查不到该场数据。本脚本：
1. 检测全部队标中形如「<已知队>队」的脏标签（如 KC队 → KC）
2. matches.team_a/team_b、duels.player_a_team/player_b_team 改为正确队名
3. matches.raw_text 头部「战队: <脏标>」同步修正（导出逐字回放保真）
4. player_ids 中 home_team=脏标 的行：同队同名已有干净行则删除（有绑定则先迁移 user_id），
   否则改名 home_team 为正确队名

用法：
    python scripts/fix_team_label_legacy.py            # dry-run：打印将变更的明细
    python scripts/fix_team_label_legacy.py --apply    # 执行修复
    python scripts/fix_team_label_legacy.py --apply --backup-dir <dir>   # 备份到指定目录

备份：默认写到脚本旁的 backups/ 目录（JSON，含受影响行修复前快照，供回滚）。
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import aiomysql
except ImportError:
    print("缺少 aiomysql，请先安装：pip install aiomysql")
    sys.exit(1)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3306
DEFAULT_USER = "root"
DEFAULT_PASSWORD = os.environ.get("ASTRBOT_MYSQL_PASSWORD", "")
DEFAULT_DB = "astrbot_battle_report"

# 脏标签形如「<base>队」，base 是已知队名才判定为误加后缀（避免误伤真正的「XX队」队名）
_LABEL_RE = re.compile(r"^(?P<base>.+)队$")

_HEADER_RE = r"(战队\s*[:：=＝]\s*)"


def _snapshot_rows(rows: list[dict]) -> list[dict]:
    """把查询结果转为 JSON 可序列化的快照（date 等转 str）。"""
    return [
        {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in r.items()}
        for r in rows
    ]


async def _fetch_affected(pool) -> tuple[dict, list[dict], list[dict], list[dict], list[dict]]:
    """取脏标签映射，以及受影响的 matches/duels/脏 player_ids、全部 player_ids。"""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # 已知队标集合：matches + duels 中出现过的全部队名
            known: set[str] = set()
            for col in ("team_a", "team_b"):
                await cur.execute(f"SELECT DISTINCT {col} AS v FROM matches WHERE {col} <> ''")
                known |= {r["v"] for r in await cur.fetchall()}
            for col in ("player_a_team", "player_b_team"):
                await cur.execute(f"SELECT DISTINCT {col} AS v FROM duels WHERE {col} <> ''")
                known |= {r["v"] for r in await cur.fetchall()}

            # 脏标签映射：KC队 → KC
            labels: dict[str, str] = {}
            for k in sorted(known):
                m = _LABEL_RE.match(k)
                if m and m.group("base") in known:
                    labels[k] = m.group("base")
            bad_labels = sorted(labels)
            if not bad_labels:
                return labels, [], [], [], []

            ph = ",".join(["%s"] * len(bad_labels))
            await cur.execute(
                f"SELECT id, team_a, team_b, home_team, raw_text FROM matches "
                f"WHERE team_a IN ({ph}) OR team_b IN ({ph}) ORDER BY id",
                bad_labels + bad_labels,
            )
            matches = await cur.fetchall()
            await cur.execute(
                f"SELECT id, match_id, player_a, player_b, player_a_team, player_b_team "
                f"FROM duels WHERE player_a_team IN ({ph}) OR player_b_team IN ({ph}) ORDER BY id",
                bad_labels + bad_labels,
            )
            duels = await cur.fetchall()
            await cur.execute(
                f"SELECT id, home_team, player_name, user_id FROM player_ids "
                f"WHERE home_team IN ({ph}) ORDER BY id",
                bad_labels,
            )
            dirty_ids = await cur.fetchall()
            await cur.execute("SELECT id, home_team, player_name, user_id FROM player_ids")
            all_ids = await cur.fetchall()
    return labels, list(matches), list(duels), list(dirty_ids), list(all_ids)


def _plan_changes(
    labels: dict[str, str],
    matches: list[dict],
    duels: list[dict],
    dirty_ids: list[dict],
    all_ids: list[dict],
) -> dict:
    """计算变更计划（dry-run 输出用）。返回 {matches, duels, player_renames, player_merges, player_deletes}。"""
    match_changes = []
    for r in matches:
        new_a = labels.get(r["team_a"], r["team_a"])
        new_b = labels.get(r["team_b"], r["team_b"])
        old_raw = r["raw_text"] or ""
        new_raw = old_raw
        if new_a != r["team_a"]:
            new_raw = re.sub(_HEADER_RE + re.escape(r["team_a"]), lambda m: m.group(1) + new_a, new_raw, count=1)
        if new_b != r["team_b"]:
            new_raw = re.sub(_HEADER_RE + re.escape(r["team_b"]), lambda m: m.group(1) + new_b, new_raw, count=1)
        match_changes.append({
            "id": r["id"], "old_a": r["team_a"], "new_a": new_a,
            "old_b": r["team_b"], "new_b": new_b,
            "old_raw_head": old_raw.split("\n", 1)[0][:40],
            "raw_text_changed": new_raw != old_raw,
        })

    duel_changes = []
    for d in duels:
        duel_changes.append({
            "id": d["id"], "match_id": d["match_id"],
            "old_a_team": d["player_a_team"], "new_a_team": labels.get(d["player_a_team"], d["player_a_team"]),
            "old_b_team": d["player_b_team"], "new_b_team": labels.get(d["player_b_team"], d["player_b_team"]),
            "player": d["player_a"] if d["player_a_team"] in labels else d["player_b"],
        })

    dirty_ids_set = {row["id"] for row in dirty_ids}
    # 干净名集合：(home_team, name) → 行（同队同名取其一，优先有 user_id 的）
    clean_map: dict[tuple[str, str], dict] = {}
    for row in all_ids:
        if row["id"] in dirty_ids_set or row["home_team"] in labels:
            continue
        key = (row["home_team"], row["player_name"])
        cur = clean_map.get(key)
        if cur is None or (row["user_id"] and not cur.get("user_id")):
            clean_map[key] = row

    player_renames: list[dict] = []
    player_merges: list[dict] = []
    player_deletes: list[dict] = []
    for row in dirty_ids:
        base = labels[row["home_team"]]
        clean = clean_map.get((base, row["player_name"]))
        if clean is None:
            player_renames.append({
                "id": row["id"], "home_team": row["home_team"], "name": row["player_name"],
                "new_home_team": base,
            })
        elif row["user_id"] and not clean["user_id"]:
            player_merges.append({
                "id": row["id"], "home_team": row["home_team"], "name": row["player_name"],
                "clean_id": clean["id"], "user_id": row["user_id"],
            })
        else:
            reason = "已绑定到用户，需人工确认" if row["user_id"] else f"干净行(pid={clean['id']})已存在，保留干净行"
            player_deletes.append({
                "id": row["id"], "home_team": row["home_team"], "name": row["player_name"],
                "reason": reason,
            })

    return {
        "labels": labels,
        "matches": match_changes,
        "duels": duel_changes,
        "player_renames": player_renames,
        "player_merges": player_merges,
        "player_deletes": player_deletes,
    }


def _print_plan(plan: dict) -> None:
    print("=" * 60)
    print(f"脏队标映射：{plan['labels']}")
    print(f"matches 修复（{len(plan['matches'])} 场）")
    for c in plan["matches"]:
        print(f"  id={c['id']} {c['old_a']} VS {c['old_b']} → {c['new_a']} VS {c['new_b']}"
              + ("  [raw_text 已同步]" if c["raw_text_changed"] else ""))
    print(f"duels 修复（{len(plan['duels'])} 行）")
    for c in plan["duels"]:
        print(f"  id={c['id']} match={c['match_id']} {c['player']}: "
              f"{c['old_a_team']}→{c['new_a_team']} / {c['old_b_team']}→{c['new_b_team']}")
    print(f"player_ids 改名（{len(plan['player_renames'])} 行）")
    for c in plan["player_renames"]:
        print(f"  id={c['id']} [{c['home_team']}] {c['name']} → [{c['new_home_team']}]")
    print(f"player_ids 迁移绑定（{len(plan['player_merges'])} 行）")
    for c in plan["player_merges"]:
        print(f"  id={c['id']} [{c['home_team']}] {c['name']} user_id={c['user_id']} → 合并到干净行 pid={c['clean_id']}")
    print(f"player_ids 删除（{len(plan['player_deletes'])} 行）")
    for c in plan["player_deletes"]:
        print(f"  id={c['id']} [{c['home_team']}] {c['name']}  （{c['reason']}）")
    print("=" * 60)


def _write_backup(backup_dir: Path, plan: dict, matches_snap: list[dict], duels_snap: list[dict], ids_snap: list[dict]) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = backup_dir / f"team_label_fix_{ts}.json"
    payload = {
        "note": "队标误加「队」后缀修复前快照，用于回滚",
        "time": ts,
        "labels": plan["labels"],
        "matches": matches_snap,
        "duels": duels_snap,
        "player_ids": ids_snap,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def _apply(pool, plan: dict) -> None:
    """执行修复（单事务）。"""
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                for c in plan["matches"]:
                    if c["old_a"] != c["new_a"] or c["old_b"] != c["new_b"]:
                        await cur.execute(
                            "UPDATE matches SET team_a=%s, team_b=%s WHERE id=%s",
                            (c["new_a"], c["new_b"], c["id"]),
                        )
                    if c.get("raw_text_changed"):
                        # raw_text 头部已随计划替换，这里按最终队名重写头部
                        await cur.execute("SELECT raw_text FROM matches WHERE id=%s", (c["id"],))
                        row = await cur.fetchone()
                        if row and row["raw_text"]:
                            raw = row["raw_text"]
                            raw = re.sub(_HEADER_RE + re.escape(c["old_a"]), lambda m: m.group(1) + c["new_a"], raw, count=1)
                            raw = re.sub(_HEADER_RE + re.escape(c["old_b"]), lambda m: m.group(1) + c["new_b"], raw, count=1)
                            await cur.execute("UPDATE matches SET raw_text=%s WHERE id=%s", (raw, c["id"]))
                for c in plan["duels"]:
                    await cur.execute(
                        "UPDATE duels SET player_a_team=%s, player_b_team=%s WHERE id=%s",
                        (c["new_a_team"], c["new_b_team"], c["id"]),
                    )
                for c in plan["player_renames"]:
                    await cur.execute(
                        "UPDATE player_ids SET home_team=%s WHERE id=%s",
                        (c["new_home_team"], c["id"]),
                    )
                for c in plan["player_merges"]:
                    await cur.execute(
                        "UPDATE player_ids SET user_id=%s WHERE id=%s",
                        (c["user_id"], c["clean_id"]),
                    )
                    await cur.execute("DELETE FROM player_ids WHERE id=%s", (c["id"],))
                for c in plan["player_deletes"]:
                    await cur.execute("DELETE FROM player_ids WHERE id=%s", (c["id"],))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def _verify(pool, labels: dict[str, str]) -> None:
    """修复后校验：无脏队标残留。"""
    bad = sorted(labels)
    ph = ",".join(["%s"] * len(bad)) if bad else ""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            if not ph:
                print("校验：无脏队标，无需处理。")
                return
            await cur.execute(
                f"SELECT COUNT(*) AS c FROM matches WHERE team_a IN ({ph}) OR team_b IN ({ph})", bad + bad)
            m = (await cur.fetchone())["c"]
            await cur.execute(
                f"SELECT COUNT(*) AS c FROM duels WHERE player_a_team IN ({ph}) OR player_b_team IN ({ph})", bad + bad)
            d = (await cur.fetchone())["c"]
            await cur.execute(
                f"SELECT COUNT(*) AS c FROM player_ids WHERE home_team IN ({ph})", bad)
            p = (await cur.fetchone())["c"]
    print(f"校验：matches 脏队标={m}（应0）、duels 脏队标={d}（应0）、player_ids 脏队标={p}（应0）")
    if m or d or p:
        print("❌ 校验未通过：仍有脏队标残留！")
        sys.exit(2)


async def main() -> None:
    parser = argparse.ArgumentParser(description="修正战报队标误加「队」后缀的数据")
    parser.add_argument("--apply", action="store_true", help="实际执行修复（默认 dry-run）")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--backup-dir", default=None, help="备份目录（默认 scripts/backups/）")
    args = parser.parse_args()

    pool = await aiomysql.create_pool(
        host=args.host, port=args.port, user=args.user, password=args.password,
        db=args.db, charset="utf8mb4", autocommit=False,
        minsize=1, maxsize=3,
    )
    try:
        labels, matches_snap, duels_snap, ids_snap, all_ids = await _fetch_affected(pool)
        if not matches_snap and not duels_snap and not ids_snap:
            print("未发现队标误加「队」后缀的数据，无需修复。")
            return
        plan = _plan_changes(labels, matches_snap, duels_snap, ids_snap, all_ids)
        _print_plan(plan)

        if not args.apply:
            print("dry-run：以上为将执行的变更。确认无误后加 --apply 执行。")
            return

        backup_dir = Path(args.backup_dir) if args.backup_dir else Path(__file__).resolve().parent / "backups"
        backup_path = _write_backup(backup_dir, plan, matches_snap, duels_snap, ids_snap)
        print(f"备份已写入：{backup_path}")

        await _apply(pool, plan)
        print("修复执行完成。")
        await _verify(pool, labels)
    finally:
        pool.close()
        await pool.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
