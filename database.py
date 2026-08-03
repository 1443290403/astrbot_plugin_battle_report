"""MySQL 存储层（aiomysql 异步驱动 + 连接池）。

负责建库、建表、迁移，以及战报/名单的 CRUD 与聚合查询。所有方法均为异步，
通过连接池在 asyncio 事件循环中执行，不阻塞事件循环。
"""

import re
import time
from typing import Any

import aiomysql

SCHEMA_VERSION = 6

_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _sanitize_db_name(db: str) -> str:
    """仅允许字母数字下划线，防止注入。"""
    if not _DB_NAME_RE.match(db):
        raise ValueError(f"非法数据库名: {db}")
    return db


class Database:
    """战报插件数据库访问层。"""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        db: str,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.db = _sanitize_db_name(db or "astrbot_battle_report")
        self.pool: aiomysql.Pool | None = None

    async def initialize(self) -> None:
        """建库、建连接池、初始化表结构与迁移。"""
        await self._ensure_database()
        self.pool = await aiomysql.create_pool(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            db=self.db,
            charset="utf8mb4",
            autocommit=True,
            minsize=1,
            maxsize=10,
        )
        await self._init_schema()

    async def _ensure_database(self) -> None:
        """以无库连接执行 CREATE DATABASE IF NOT EXISTS。"""
        db = self.db
        conn = await aiomysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            charset="utf8mb4",
            autocommit=True,
        )
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
        finally:
            conn.close()

    async def _init_schema(self) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 建表（幂等）
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS matches (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        group_id VARCHAR(64) NOT NULL,
                        team_a VARCHAR(64) NOT NULL,
                        team_b VARCHAR(64) NOT NULL,
                        match_time DATE NOT NULL,
                        rule VARCHAR(128) DEFAULT '',
                        location VARCHAR(64) DEFAULT '',
                        submitted_by VARCHAR(64) DEFAULT '',
                        submitted_name VARCHAR(128) DEFAULT '',
                        created_at INT NOT NULL,
                        INDEX idx_matches_group_time (group_id, match_time),
                        INDEX idx_matches_group (group_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS duels (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        match_id INT NOT NULL,
                        round_no INT NOT NULL,
                        player_a VARCHAR(64) NOT NULL,
                        score_a INT NOT NULL,
                        player_b VARCHAR(64) NOT NULL,
                        score_b INT NOT NULL,
                        player_a_team VARCHAR(64) NOT NULL,
                        player_b_team VARCHAR(64) NOT NULL,
                        result ENUM('A','B','DRAW') NOT NULL,
                        INDEX idx_duels_match (match_id),
                        INDEX idx_duels_pa (player_a),
                        INDEX idx_duels_pb (player_b),
                        CONSTRAINT fk_duels_match FOREIGN KEY (match_id)
                            REFERENCES matches(id) ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS teams (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        group_id VARCHAR(64) NOT NULL,
                        team_name VARCHAR(64) NOT NULL,
                        player_name VARCHAR(64) NOT NULL,
                        UNIQUE KEY uk_group_team_player (group_id, team_name, player_name),
                        INDEX idx_teams_group (group_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await cur.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version (version INT NOT NULL)"
                )
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS group_home (
                        group_id VARCHAR(64) PRIMARY KEY,
                        home_team VARCHAR(64) NOT NULL,
                        created_at INT NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS users (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        home_team VARCHAR(64) NOT NULL,
                        name VARCHAR(64) NOT NULL,
                        qq_id VARCHAR(64) DEFAULT '',
                        created_at INT NOT NULL,
                        UNIQUE KEY uk_team_name (home_team, name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS player_ids (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        home_team VARCHAR(64) NOT NULL,
                        player_name VARCHAR(64) NOT NULL,
                        user_id INT NULL,
                        created_at INT NOT NULL,
                        UNIQUE KEY uk_team_player (home_team, player_name),
                        INDEX idx_team_user (home_team, user_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                # 迁移
                await cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
                row = await cur.fetchone()
                current = row[0] if row else 0
                if current < 2:
                    # v2：matches 增加 winner 列（记录胜者战队）
                    await cur.execute(
                        "ALTER TABLE matches ADD COLUMN winner VARCHAR(64) DEFAULT ''"
                    )
                if current < 3:
                    # v3：matches 增加 home_team 列（记录上传方主体战队）
                    await cur.execute(
                        "ALTER TABLE matches ADD COLUMN home_team VARCHAR(64) DEFAULT ''"
                    )
                if current < 6:
                    # v6：单表存储参赛ID池与绑定（player_ids.user_id 可空，NULL=未绑定）
                    await cur.execute("ALTER TABLE player_ids MODIFY user_id INT NULL")
                    now = int(time.time())
                    await cur.execute(
                        "SELECT COUNT(*) AS c FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND table_name = 'team_players'"
                    )
                    has_tp = (await cur.fetchone())[0] > 0
                    if has_tp:
                        # 从旧 team_players 迁移池数据（未绑定），再删表
                        await cur.execute(
                            """INSERT IGNORE INTO player_ids (home_team, player_name, user_id, created_at)
                               SELECT home_team, player_name, NULL, %s FROM team_players""",
                            (now,),
                        )
                        await cur.execute("DROP TABLE team_players")
                    else:
                        # 从已有战报回填参赛ID池（按队伍去重）
                        await cur.execute(
                            """INSERT IGNORE INTO player_ids (home_team, player_name, user_id, created_at)
                               SELECT d.player_a_team, d.player_a, NULL, %s FROM duels d
                               WHERE d.player_a_team != ''
                               UNION
                               SELECT d.player_b_team, d.player_b, NULL, %s FROM duels d
                               WHERE d.player_b_team != ''""",
                            (now, now),
                        )
                if current < SCHEMA_VERSION:
                    await cur.execute("INSERT INTO schema_version (version) VALUES (%s)", (SCHEMA_VERSION,))

    async def close(self) -> None:
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None

    # ---------- 基础查询封装 ----------

    async def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        return rows

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        """执行单条写语句，返回受影响行数。"""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return cur.rowcount

    # ---------- 战报写入 / 删除 ----------

    async def insert_report(self, report, winner: str = "", home_team: str = "") -> int:
        """插入一份战报（match + duels），返回 match_id。winner 为胜者，home_team 为上传方主体战队。"""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """INSERT INTO matches
                           (group_id, team_a, team_b, match_time, rule, location,
                            submitted_by, submitted_name, created_at, winner, home_team)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            report.group_id,
                            report.team_a,
                            report.team_b,
                            report.match_time,
                            report.rule,
                            report.location,
                            report.submitted_by,
                            report.submitted_name,
                            int(report.created_at) if report.created_at else 0,
                            winner or "",
                            home_team or "",
                        ),
                    )
                    match_id = cur.lastrowid
                    now = int(time.time())
                    for duel in report.duels:
                        await cur.execute(
                            """INSERT INTO duels
                               (match_id, round_no, player_a, score_a, player_b, score_b,
                                player_a_team, player_b_team, result)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                            (
                                match_id,
                                duel.round_no,
                                duel.player_a,
                                duel.score_a,
                                duel.player_b,
                                duel.score_b,
                                report.team_a,
                                report.team_b,
                                "A" if duel.score_a > duel.score_b
                                else ("B" if duel.score_a < duel.score_b else "DRAW"),
                            ),
                        )
                        # 参赛ID按队伍去重入库（发送战报时处理，保留已有绑定）
                        await cur.execute(
                            """INSERT INTO player_ids (home_team, player_name, user_id, created_at)
                               VALUES (%s, %s, NULL, %s) AS new
                               ON DUPLICATE KEY UPDATE player_name = new.player_name""",
                            (report.team_a, duel.player_a, now),
                        )
                        await cur.execute(
                            """INSERT INTO player_ids (home_team, player_name, user_id, created_at)
                               VALUES (%s, %s, NULL, %s) AS new
                               ON DUPLICATE KEY UPDATE player_name = new.player_name""",
                            (report.team_b, duel.player_b, now),
                        )
                await conn.commit()
                return match_id
            except Exception:
                await conn.rollback()
                raise

    async def delete_match(self, group_id: str, match_id: int) -> bool:
        """删除指定群、指定 ID 的战报（校验群归属），返回是否删除成功。"""
        cur = await self._execute(
            "DELETE FROM matches WHERE id = %s AND group_id = %s",
            (match_id, group_id),
        )
        return cur > 0

    async def get_last_match_by_submitter(self, group_id: str, submitter: str) -> int | None:
        """查询某提交者在该群最近一条战报 ID。"""
        rows = await self._query(
            """SELECT id FROM matches WHERE group_id = %s AND submitted_by = %s
               ORDER BY id DESC LIMIT 1""",
            (group_id, submitter),
        )
        return rows[0]["id"] if rows else None

    # ---------- 战队名单 ----------

    async def replace_teams(self, group_id: str, teams: list[tuple[str, list[str]]]) -> None:
        """覆盖写入某群的战队名单（先清空再写入）。"""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM teams WHERE group_id = %s", (group_id,))
                    for team_name, players in teams:
                        for player in players:
                            await cur.execute(
                                """INSERT IGNORE INTO teams (group_id, team_name, player_name)
                                   VALUES (%s, %s, %s)""",
                                (group_id, team_name, player),
                            )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def get_teams(self, group_id: str) -> list[dict]:
        """获取某群的战队名单。"""
        return await self._query(
            "SELECT team_name, player_name FROM teams WHERE group_id = %s ORDER BY id",
            (group_id,),
        )

    # ---------- 群主体绑定 ----------

    async def set_group_home(self, group_id: str, home_team: str) -> None:
        """绑定群的主体战队（覆盖写入）。"""
        await self._execute(
            """INSERT INTO group_home (group_id, home_team, created_at)
               VALUES (%s, %s, %s) AS new
               ON DUPLICATE KEY UPDATE home_team = new.home_team""",
            (group_id, home_team, int(time.time())),
        )

    async def get_group_home(self, group_id: str) -> str | None:
        """获取群绑定的主体战队；未绑定返回 None。"""
        rows = await self._query(
            "SELECT home_team FROM group_home WHERE group_id = %s",
            (group_id,),
        )
        return rows[0]["home_team"] if rows else None

    async def backfill_group_home(self, group_id: str, home_team: str) -> None:
        """把该群已有战报的 home_team 回填为当前主体。"""
        await self._execute(
            "UPDATE matches SET home_team = %s WHERE group_id = %s AND home_team = ''",
            (home_team, group_id),
        )

    # ---------- 用户与参赛ID ----------

    async def find_or_create_user(self, home_team: str, name: str, qq_id: str = "") -> int:
        """按 战队+名字 查找用户（角色）；不存在则创建。

        用户本身是队员角色：若该 QQ 已有角色则复用（一个 QQ 一个角色），
        否则按名字创建新角色。
        """
        rows = await self._query(
            "SELECT id FROM users WHERE home_team = %s AND name = %s",
            (home_team, name),
        )
        if rows:
            return rows[0]["id"]
        if qq_id:
            rows = await self._query(
                "SELECT id FROM users WHERE home_team = %s AND qq_id = %s",
                (home_team, qq_id),
            )
            if rows:
                return rows[0]["id"]
        await self._execute(
            "INSERT INTO users (home_team, name, qq_id, created_at) VALUES (%s, %s, %s, %s)",
            (home_team, name, qq_id, int(time.time())),
        )
        rows = await self._query(
            "SELECT id FROM users WHERE home_team = %s AND name = %s",
            (home_team, name),
        )
        return rows[0]["id"]

    async def get_user_by_qq(self, home_team: str, qq_id: str) -> dict | None:
        """按 战队+QQ 查找用户。"""
        rows = await self._query(
            "SELECT id, name, qq_id FROM users WHERE home_team = %s AND qq_id = %s",
            (home_team, qq_id),
        )
        return rows[0] if rows else None

    async def get_user_by_id(self, user_id: int) -> dict | None:
        rows = await self._query(
            "SELECT id, home_team, name, qq_id FROM users WHERE id = %s",
            (user_id,),
        )
        return rows[0] if rows else None

    async def claim_user_by_name(self, home_team: str, name: str, qq_id: str) -> tuple[str, int]:
        """把 QQ 认领到指定名字的用户。

        Returns:
            (状态, user_id)：状态为 'ok'（认领成功/已是本人）、'claimed_else'（已被他人认领）、
            'not_found'（用户不存在，user_id 为 0）。
        """
        rows = await self._query(
            "SELECT id, qq_id FROM users WHERE home_team = %s AND name = %s",
            (home_team, name),
        )
        if not rows:
            return "not_found", 0
        uid = rows[0]["id"]
        cur = rows[0]["qq_id"] or ""
        if cur and str(cur) != str(qq_id):
            return "claimed_else", uid
        if not cur:
            await self._execute(
                "UPDATE users SET qq_id = %s WHERE id = %s",
                (qq_id, uid),
            )
        return "ok", uid

    async def get_player_pool(self, home_team: str, keyword: str | None = None, limit: int = 50) -> list[str]:
        """该战队已入库的参赛ID（发送战报时写入 player_ids），可选模糊匹配。"""
        like = f"%{keyword}%" if keyword else "%"
        rows = await self._query(
            "SELECT player_name FROM player_ids "
            "WHERE home_team = %s AND player_name LIKE %s ORDER BY player_name LIMIT %s",
            (home_team, like, limit),
        )
        return [r["player_name"] for r in rows]

    async def get_pool_status(self, home_team: str, keyword: str | None = None, limit: int = 20) -> list[dict]:
        """参赛ID池及绑定状态（含所属用户）。"""
        pool = await self.get_player_pool(home_team, keyword, limit)
        if not pool:
            return []
        placeholders = ",".join(["%s"] * len(pool))
        rows = await self._query(
            f"""SELECT p.player_name, u.name AS user_name, u.qq_id
                FROM player_ids p LEFT JOIN users u ON p.user_id = u.id
                WHERE p.home_team = %s AND p.player_name IN ({placeholders})""",
            (home_team, *pool),
        )
        bound = {r["player_name"]: r for r in rows}
        result = []
        for p in pool:
            b = bound.get(p)
            result.append({
                "player": p,
                "user_name": b["user_name"] if b else "",
                "qq_id": b["qq_id"] if b else "",
            })
        return result

    async def get_player_binding(self, home_team: str, player_name: str) -> dict | None:
        """查询某个参赛ID的绑定情况。"""
        rows = await self._query(
            """SELECT p.id, p.player_name, p.user_id, u.name AS user_name, u.qq_id
               FROM player_ids p LEFT JOIN users u ON p.user_id = u.id
               WHERE p.home_team = %s AND p.player_name = %s""",
            (home_team, player_name),
        )
        return rows[0] if rows else None

    async def bind_player_to_user(self, home_team: str, player_name: str, user_id: int) -> None:
        """把参赛ID绑定到用户（覆盖）。"""
        await self._execute(
            """INSERT INTO player_ids (home_team, player_name, user_id, created_at)
               VALUES (%s, %s, %s, %s) AS new
               ON DUPLICATE KEY UPDATE user_id = new.user_id""",
            (home_team, player_name, user_id, int(time.time())),
        )

    async def get_user_players(self, home_team: str, user_id: int) -> list[str]:
        """某用户绑定的参赛ID列表。"""
        rows = await self._query(
            "SELECT player_name FROM player_ids WHERE home_team = %s AND user_id = %s ORDER BY id",
            (home_team, user_id),
        )
        return [r["player_name"] for r in rows]

    async def get_players_aggregate(
        self,
        group_id: str | None,
        players: list[str],
        date_from: str | None = None,
        date_to: str | None = None,
        home_team: str | None = None,
    ) -> dict:
        """多个参赛ID合并战绩统计。group_id 为 None 时跨该战队全部群。"""
        total = {"wins": 0, "losses": 0, "draws": 0, "total": 0}
        for p in players:
            rec = await self.get_player_record(group_id, p, date_from, date_to, home_team)
            total["wins"] += int(rec.get("wins", 0))
            total["losses"] += int(rec.get("losses", 0))
            total["draws"] += int(rec.get("draws", 0))
            total["total"] += int(rec.get("total", 0))
        return total

    # ---------- 聚合统计 ----------

    @staticmethod
    def _date_bounds(date_from: str | None, date_to: str | None) -> tuple[str, str]:
        return (date_from or "1000-01-01", date_to or "9999-12-31")

    async def get_player_ranking(
        self,
        group_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
        min_games: int = 1,
        limit: int = 10,
        team: str | None = None,
        home_team: str | None = None,
    ) -> list[dict]:
        """个人排行（积分 = 胜场 × 胜率，胜率用小数，保留两位）。

        team 非空时只统计该战队选手；home_team 非空时只统计该主体上传的战报。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        home_clause = " AND m.home_team = %s" if home_team else ""
        params: list = [group_id, d1, d2]
        if home_team:
            params.append(home_team)
        params += [group_id, d1, d2]
        if home_team:
            params.append(home_team)
        team_clause = ""
        if team:
            team_clause = "WHERE sides.team = %s "
            params.append(team)
        params.append(min_games)
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT %s"
            params.append(limit)
        return await self._query(
            f"""WITH sides AS (
                   SELECT d.player_a AS player, d.player_a_team AS team,
                          CASE d.result WHEN 'A' THEN 1 ELSE 0 END AS win,
                          CASE d.result WHEN 'B' THEN 1 ELSE 0 END AS loss,
                          CASE d.result WHEN 'DRAW' THEN 1 ELSE 0 END AS draw
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE m.group_id = %s AND m.match_time >= %s AND m.match_time <= %s{home_clause}
                   UNION ALL
                   SELECT d.player_b, d.player_b_team,
                          CASE d.result WHEN 'B' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'A' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'DRAW' THEN 1 ELSE 0 END
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE m.group_id = %s AND m.match_time >= %s AND m.match_time <= %s{home_clause}
               )
               SELECT player, SUM(win) wins, SUM(loss) losses, SUM(draw) draws,
                      COUNT(*) total,
                      ROUND(SUM(win) * SUM(win) / NULLIF(SUM(win)+SUM(loss), 0), 2) AS points
               FROM sides
               {team_clause}
               GROUP BY player HAVING total >= %s
               ORDER BY points DESC, wins DESC, total DESC, player ASC
               {limit_clause}""",
            tuple(params),
        )

    async def get_player_record(
        self,
        group_id: str | None,
        player: str,
        date_from: str | None = None,
        date_to: str | None = None,
        home_team: str | None = None,
    ) -> dict:
        """单个玩家的战绩汇总。group_id 为 None 时不按群过滤（跨该战队全部群）。

        home_team 非空时同时限制该选手属于该战队（避免同名不同队选手混入）。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        group_clause = " AND m.group_id = %s" if group_id else ""
        home_match = " AND m.home_team = %s" if home_team else ""
        team_clause_a = " AND d.player_a_team = %s" if home_team else ""
        team_clause_b = " AND d.player_b_team = %s" if home_team else ""
        params: list = [player, d1, d2]
        if group_id:
            params.append(group_id)
        if home_team:
            params.append(home_team)  # m.home_team
            params.append(home_team)  # player_a_team
        params += [player, d1, d2]
        if group_id:
            params.append(group_id)
        if home_team:
            params.append(home_team)  # m.home_team
            params.append(home_team)  # player_b_team
        params.append(player)
        rows = await self._query(
            f"""WITH sides AS (
                   SELECT CASE d.result WHEN 'A' THEN 1 ELSE 0 END AS win,
                          CASE d.result WHEN 'B' THEN 1 ELSE 0 END AS loss,
                          CASE d.result WHEN 'DRAW' THEN 1 ELSE 0 END AS draw
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE d.player_a = %s
                     AND m.match_time >= %s AND m.match_time <= %s{group_clause}{home_match}{team_clause_a}
                   UNION ALL
                   SELECT CASE d.result WHEN 'B' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'A' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'DRAW' THEN 1 ELSE 0 END
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE d.player_b = %s
                     AND m.match_time >= %s AND m.match_time <= %s{group_clause}{home_match}{team_clause_b}
               )
               SELECT player_name AS player,
                      SUM(win) wins, SUM(loss) losses, SUM(draw) draws, COUNT(*) total
               FROM (SELECT %s AS player_name, win, loss, draw FROM sides) t
               GROUP BY player_name""",
            tuple(params),
        )
        return rows[0] if rows else {"player": player, "wins": 0, "losses": 0, "draws": 0, "total": 0}

    async def get_team_ranking(
        self,
        group_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
        home_team: str | None = None,
    ) -> list[dict]:
        """队伍战绩排行（一场比赛胜者是该场对局赢得更多的一方）。"""
        d1, d2 = self._date_bounds(date_from, date_to)
        home_clause = " AND m.home_team = %s" if home_team else ""
        params = [group_id, d1, d2]
        if home_team:
            params.append(home_team)
        params += [group_id, d1, d2]
        if home_team:
            params.append(home_team)
        params.append(limit)
        return await self._query(
            f"""WITH match_scores AS (
                   SELECT d.match_id,
                          SUM(CASE d.result WHEN 'A' THEN 1 ELSE 0 END) a_wins,
                          SUM(CASE d.result WHEN 'B' THEN 1 ELSE 0 END) b_wins
                   FROM duels d GROUP BY d.match_id
               ),
               team_sides AS (
                   SELECT m.team_a AS team,
                          CASE WHEN s.a_wins > s.b_wins THEN 1 ELSE 0 END win,
                          CASE WHEN s.a_wins < s.b_wins THEN 1 ELSE 0 END loss,
                          CASE WHEN s.a_wins = s.b_wins THEN 1 ELSE 0 END draw
                   FROM matches m JOIN match_scores s ON m.id = s.match_id
                   WHERE m.group_id = %s AND m.match_time >= %s AND m.match_time <= %s{home_clause}
                   UNION ALL
                   SELECT m.team_b,
                          CASE WHEN s.b_wins > s.a_wins THEN 1 ELSE 0 END,
                          CASE WHEN s.b_wins < s.a_wins THEN 1 ELSE 0 END,
                          CASE WHEN s.a_wins = s.b_wins THEN 1 ELSE 0 END
                   FROM matches m JOIN match_scores s ON m.id = s.match_id
                   WHERE m.group_id = %s AND m.match_time >= %s AND m.match_time <= %s{home_clause}
               )
               SELECT team, SUM(win) wins, SUM(loss) losses, SUM(draw) draws, COUNT(*) total,
                      ROUND(SUM(win)*SUM(win)/NULLIF(SUM(win)+SUM(loss),0),2) AS points
               FROM team_sides GROUP BY team
               ORDER BY points DESC, wins DESC, total DESC, team ASC
               LIMIT %s""",
            tuple(params),
        )

    async def get_player_trend(
        self,
        group_id: str,
        player: str,
        date_from: str | None = None,
        home_team: str | None = None,
    ) -> list[tuple[str, int, int]]:
        """个人按日期的胜/负场次，返回 [(date, wins, losses)]。"""
        d1, _ = self._date_bounds(date_from, None)
        home_clause = " AND m.home_team = %s" if home_team else ""
        params = [group_id, player, d1]
        if home_team:
            params.append(home_team)
        params += [group_id, player, d1]
        if home_team:
            params.append(home_team)
        rows = await self._query(
            f"""SELECT m.match_time AS date,
                      SUM(CASE WHEN d.result='A' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN d.result='B' THEN 1 ELSE 0 END) AS losses
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.group_id = %s AND d.player_a = %s AND m.match_time >= %s{home_clause}
               GROUP BY m.match_time
               UNION ALL
               SELECT m.match_time,
                      SUM(CASE WHEN d.result='B' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN d.result='A' THEN 1 ELSE 0 END)
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.group_id = %s AND d.player_b = %s AND m.match_time >= %s{home_clause}
               GROUP BY m.match_time""",
            tuple(params),
        )
        merged: dict[str, list[int]] = {}
        for row in rows:
            key = str(row["date"])
            if key not in merged:
                merged[key] = [0, 0]
            merged[key][0] += int(row["wins"] or 0)
            merged[key][1] += int(row["losses"] or 0)
        return [(d, w, l) for d, (w, l) in sorted(merged.items())]

    async def get_team_trend(
        self,
        group_id: str,
        team: str,
        date_from: str | None = None,
        home_team: str | None = None,
    ) -> list[tuple[str, int, int]]:
        """队伍按日期的胜/负场次，返回 [(date, wins, losses)]。"""
        d1, _ = self._date_bounds(date_from, None)
        home_clause = " AND m.home_team = %s" if home_team else ""
        params = [group_id, team, d1]
        if home_team:
            params.append(home_team)
        params += [group_id, team, d1]
        if home_team:
            params.append(home_team)
        rows = await self._query(
            f"""WITH match_scores AS (
                   SELECT d.match_id,
                          SUM(CASE d.result WHEN 'A' THEN 1 ELSE 0 END) a_wins,
                          SUM(CASE d.result WHEN 'B' THEN 1 ELSE 0 END) b_wins
                   FROM duels d GROUP BY d.match_id
               ),
               team_sides AS (
                   SELECT m.match_time AS date, m.team_a AS team,
                          CASE WHEN s.a_wins > s.b_wins THEN 1 ELSE 0 END win,
                          CASE WHEN s.a_wins < s.b_wins THEN 1 ELSE 0 END loss
                   FROM matches m JOIN match_scores s ON m.id = s.match_id
                   WHERE m.group_id = %s AND m.team_a = %s AND m.match_time >= %s{home_clause}
                   UNION ALL
                   SELECT m.match_time, m.team_b,
                          CASE WHEN s.b_wins > s.a_wins THEN 1 ELSE 0 END,
                          CASE WHEN s.b_wins < s.a_wins THEN 1 ELSE 0 END
                   FROM matches m JOIN match_scores s ON m.id = s.match_id
                   WHERE m.group_id = %s AND m.team_b = %s AND m.match_time >= %s{home_clause}
               )
               SELECT date, SUM(win) wins, SUM(loss) losses
               FROM team_sides GROUP BY date ORDER BY date""",
            tuple(params),
        )
        return [(str(r["date"]), int(r["wins"] or 0), int(r["losses"] or 0)) for r in rows]

    async def get_export_rows(self, group_id: str) -> list[dict]:
        """导出某群全部战报（matches + duels 联表）。"""
        return await self._query(
            """SELECT m.id AS match_id, m.group_id, m.team_a, m.team_b,
                      m.match_time, m.rule, m.location,
                      d.round_no, d.player_a, d.score_a, d.player_b, d.score_b, d.result
               FROM matches m LEFT JOIN duels d ON d.match_id = m.id
               WHERE m.group_id = %s
               ORDER BY m.id, d.round_no, d.id""",
            (group_id,),
        )
