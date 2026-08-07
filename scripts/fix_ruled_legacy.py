"""一次性数据修复脚本：清理历史战报中的 (规则) 判罚标记残留。

背景：v1.12.2 判罚落败标记功能上线前提交的战报，解析器不识别 `(规则)`/`（规则）`，
把标记原文存进了玩家名字段，且 duels.ruled 为 0。本脚本：
1. 剥离 duels 玩家名中的标记，并按语义置 ruled=1
2. 清理 player_ids 参赛ID池中带标记的脏名字（有干净名则删脏行，无则改名）

规则语义（用户确认）：`(规则)` 标记不管挂在哪个 ID 上，被规则的一方都是比分低
的一侧（判罚方必为败方）。代码层已按此实现，本脚本只修数据。

用法：
    python scripts/fix_ruled_legacy.py            # dry-run：打印将变更的明细
    python scripts/fix_ruled_legacy.py --apply    # 执行修复
    python scripts/fix_ruled_legacy.py --apply --backup-dir <dir>   # 备份到指定目录

备份：默认写到脚本旁的 backups/ 目录（JSON），含受影响行修复前快照，供回滚。
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

try:
    import aiomysql
except ImportError:
    print("缺少 aiomysql，请先安装：pip install aiomysql")
    sys.exit(1)

# 让脚本可以从插件根目录以包方式导入解析器
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from battle_report_parser import _clean_player_name
except ImportError:
    try:
        from .battle_report_parser import _clean_player_name
    except ImportError:
        print("无法导入 battle_report_parser，请检查脚本路径")
        sys.exit(1)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3306
DEFAULT_USER = "root"
DEFAULT_PASSWORD = os.environ.get("ASTRBOT_MYSQL_PASSWORD", "")
DEFAULT_DB = "astrbot_battle_report"

# 匹配带规则标记的玩家名（半角/全角括号）
_MARKED_SQL = (
    "player_a LIKE '%(规则)%' OR player_a LIKE '%（规则）%' "
    "OR player_b LIKE '%(规则)%' OR player_b LIKE '%（规则）%'"
)


def _snapshot_rows(rows: list[dict]) -> list[dict]:
    """把查询结果转为 JSON 可序列化的快照（date 等转 str）。"""
    out = []
    for r in rows:
        item = {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in r.items()}
        out.append(item)
    return out


async def _fetch_affected(pool) -> tuple[list[dict], list[dict], list[dict]]:
    """取受影响 duels、脏 player_ids、以及全部 player_ids（用于干净名存在性判定）。"""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"SELECT id, match_id, round_no, player_a, score_a, player_b, score_b, ruled, seq "
                f"FROM duels WHERE {_MARKED_SQL} ORDER BY id"
            )
            duels = await cur.fetchall()
            await cur.execute(
                "SELECT id, home_team, player_name, user_id FROM player_ids "
                "WHERE player_name LIKE '%(规则)%' OR player_name LIKE '%（规则）%' ORDER BY id"
            )
            player_ids = await cur.fetchall()
            await cur.execute("SELECT id, home_team, player_name FROM player_ids")
            all_player_ids = await cur.fetchall()
    return list(duels), list(player_ids), list(all_player_ids)


def _plan_changes(duels: list[dict], player_ids: list[dict], all_player_ids: list[dict]) -> dict:
    """计算变更计划（dry-run 输出用）。返回 {duels: [...], player_renames: [...], player_deletes: [...]}。"""
    duel_changes = []
    for d in duels:
        pa, _, pa_ruled = _clean_player_name(d["player_a"])
        pb, _, pb_ruled = _clean_player_name(d["player_b"])
        ruled = 1 if (pa_ruled or pb_ruled) else 0
        duel_changes.append({
            "id": d["id"],
            "match_id": d["match_id"],
            "old_a": d["player_a"],
            "old_b": d["player_b"],
            "new_a": pa,
            "new_b": pb,
            "score_a": d["score_a"],
            "score_b": d["score_b"],
            "old_ruled": d["ruled"],
            "new_ruled": ruled,
        })

    dirty_ids = {row["id"] for row in player_ids}
    # 现有干净名集合：(home_team, name) → id。只收集非脏行（已是干净名的行），
    # 避免把脏行自身的干净名当作"已存在的干净名"而误删。
    existing: dict[tuple[str, str], int] = {}
    for row in all_player_ids:
        if row["id"] in dirty_ids:
            continue
        existing[(row["home_team"], row["player_name"])] = row["id"]

    player_renames = []   # (id, home_team, 脏名, 干净名)
    player_deletes = []   # (id, home_team, 脏名, 原因)
    for row in player_ids:
        clean, _, _ = _clean_player_name(row["player_name"])
        if clean == row["player_name"]:
            continue  # 理论上不会出现（查询已过滤带标记）
        dup = existing.get((row["home_team"], clean))
        if dup is not None:
            player_deletes.append({
                "id": row["id"], "home_team": row["home_team"],
                "name": row["player_name"], "reason": f"干净名 {clean} 已存在(pid={dup})，保留干净行",
            })
        else:
            player_renames.append({
                "id": row["id"], "home_team": row["home_team"],
                "old": row["player_name"], "new": clean,
            })

    return {
        "duels": duel_changes,
        "player_renames": player_renames,
        "player_deletes": player_deletes,
    }


def _print_plan(plan: dict) -> None:
    print("=" * 60)
    print(f"duels 修复（{len(plan['duels'])} 行）→ 剥离标记 + ruled 置位")
    for c in plan["duels"]:
        print(f"  id={c['id']} match={c['match_id']}")
        print(f"    {c['old_a']} {c['score_a']}:{c['score_b']} {c['old_b']}  ruled={c['old_ruled']}")
        print(f"    {c['new_a']} {c['score_a']}:{c['score_b']} {c['new_b']}  ruled={c['new_ruled']}")
    print(f"player_ids 改名（{len(plan['player_renames'])} 行）")
    for c in plan["player_renames"]:
        print(f"  id={c['id']} [{c['home_team']}] {c['old']} → {c['new']}")
    print(f"player_ids 删除（{len(plan['player_deletes'])} 行）")
    for c in plan["player_deletes"]:
        print(f"  id={c['id']} [{c['home_team']}] {c['name']}  （{c['reason']}）")
    print("=" * 60)


def _write_backup(backup_dir: Path, duels_snap: list[dict], player_ids_snap: list[dict]) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = backup_dir / f"ruled_fix_{ts}.json"
    payload = {
        "note": "带 (规则) 标记的历史战报修复前快照，用于回滚",
        "time": ts,
        "duels": duels_snap,
        "player_ids": player_ids_snap,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def _apply(pool, plan: dict) -> None:
    """执行修复（单事务）。"""
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                for c in plan["duels"]:
                    await cur.execute(
                        "UPDATE duels SET player_a=%s, player_b=%s, ruled=%s WHERE id=%s",
                        (c["new_a"], c["new_b"], c["new_ruled"], c["id"]),
                    )
                for c in plan["player_renames"]:
                    await cur.execute(
                        "UPDATE player_ids SET player_name=%s WHERE id=%s",
                        (c["new"], c["id"]),
                    )
                for c in plan["player_deletes"]:
                    await cur.execute("DELETE FROM player_ids WHERE id=%s", (c["id"],))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def _verify(pool) -> None:
    """修复后校验：无带标记名字残留。"""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(f"SELECT COUNT(*) AS c FROM duels WHERE {_MARKED_SQL}")
            d = (await cur.fetchone())["c"]
            await cur.execute(
                "SELECT COUNT(*) AS c FROM player_ids "
                "WHERE player_name LIKE '%(规则)%' OR player_name LIKE '%（规则）%'"
            )
            p = (await cur.fetchone())["c"]
            await cur.execute("SELECT COUNT(*) AS c FROM duels WHERE ruled = 1")
            r = (await cur.fetchone())["c"]
    print(f"校验：duels 带标记={d}（应0）、player_ids 带标记={p}（应0）、ruled=1 记录={r}（应≥5）")
    if d or p:
        print("❌ 校验未通过：仍有标记残留！")
        sys.exit(2)


async def main() -> None:
    parser = argparse.ArgumentParser(description="修复历史战报中的 (规则) 标记残留")
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
        duels_snap, player_ids_snap, all_player_ids = await _fetch_affected(pool)
        if not duels_snap and not player_ids_snap:
            print("未发现带 (规则) 标记的残留数据，无需修复。")
            return
        plan = _plan_changes(duels_snap, player_ids_snap, all_player_ids)
        _print_plan(plan)

        if not args.apply:
            print("dry-run：以上为将执行的变更。确认无误后加 --apply 执行。")
            return

        backup_dir = Path(args.backup_dir) if args.backup_dir else Path(__file__).resolve().parent / "backups"
        backup_path = _write_backup(backup_dir, duels_snap, player_ids_snap)
        print(f"备份已写入：{backup_path}")

        await _apply(pool, plan)
        print("修复执行完成。")
        await _verify(pool)
    finally:
        pool.close()
        await pool.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
