"""MySQL 存储层（aiomysql 异步驱动 + 连接池）。

负责建库、建表、迁移，以及战报/名单的 CRUD 与聚合查询。所有方法均为异步，
通过连接池在 asyncio 事件循环中执行，不阻塞事件循环。
"""

import re
from typing import Any

import aiomysql

SCHEMA_VERSION = 2

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
                # 迁移
                await cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
                row = await cur.fetchone()
                current = row[0] if row else 0
                if current < 2:
                    # v2：matches 增加 winner 列（记录胜者战队）
                    await cur.execute(
                        "ALTER TABLE matches ADD COLUMN winner VARCHAR(64) DEFAULT ''"
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

    async def insert_report(self, report, winner: str = "") -> int:
        """插入一份战报（match + duels），返回 match_id。winner 为胜者战队名。"""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """INSERT INTO matches
                           (group_id, team_a, team_b, match_time, rule, location,
                            submitted_by, submitted_name, created_at, winner)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                        ),
                    )
                    match_id = cur.lastrowid
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
    ) -> list[dict]:
        """个人胜率排行。"""
        d1, d2 = self._date_bounds(date_from, date_to)
        return await self._query(
            """WITH sides AS (
                   SELECT d.player_a AS player,
                          CASE d.result WHEN 'A' THEN 1 ELSE 0 END AS win,
                          CASE d.result WHEN 'B' THEN 1 ELSE 0 END AS loss,
                          CASE d.result WHEN 'DRAW' THEN 1 ELSE 0 END AS draw
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE m.group_id = %s AND m.match_time >= %s AND m.match_time <= %s
                   UNION ALL
                   SELECT d.player_b,
                          CASE d.result WHEN 'B' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'A' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'DRAW' THEN 1 ELSE 0 END
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE m.group_id = %s AND m.match_time >= %s AND m.match_time <= %s
               )
               SELECT player, SUM(win) wins, SUM(loss) losses, SUM(draw) draws,
                      COUNT(*) total,
                      ROUND(SUM(win) * 100.0 / NULLIF(SUM(win)+SUM(loss), 0), 1) AS win_rate
               FROM sides GROUP BY player HAVING total >= %s
               ORDER BY win_rate DESC, wins DESC, total DESC, player ASC
               LIMIT %s""",
            (group_id, d1, d2, group_id, d1, d2, min_games, limit),
        )

    async def get_player_record(
        self,
        group_id: str,
        player: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """单个玩家的战绩汇总。"""
        d1, d2 = self._date_bounds(date_from, date_to)
        rows = await self._query(
            """WITH sides AS (
                   SELECT CASE d.result WHEN 'A' THEN 1 ELSE 0 END AS win,
                          CASE d.result WHEN 'B' THEN 1 ELSE 0 END AS loss,
                          CASE d.result WHEN 'DRAW' THEN 1 ELSE 0 END AS draw
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE m.group_id = %s AND d.player_a = %s
                     AND m.match_time >= %s AND m.match_time <= %s
                   UNION ALL
                   SELECT CASE d.result WHEN 'B' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'A' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'DRAW' THEN 1 ELSE 0 END
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE m.group_id = %s AND d.player_b = %s
                     AND m.match_time >= %s AND m.match_time <= %s
               )
               SELECT player_name AS player,
                      SUM(win) wins, SUM(loss) losses, SUM(draw) draws, COUNT(*) total
               FROM (SELECT %s AS player_name, win, loss, draw FROM sides) t
               GROUP BY player_name""",
            (group_id, player, d1, d2, group_id, player, d1, d2, player),
        )
        return rows[0] if rows else {"player": player, "wins": 0, "losses": 0, "draws": 0, "total": 0}

    async def get_team_ranking(
        self,
        group_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """队伍战绩排行（一场比赛胜者是该场对局赢得更多的一方）。"""
        d1, d2 = self._date_bounds(date_from, date_to)
        return await self._query(
            """WITH match_scores AS (
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
                   WHERE m.group_id = %s AND m.match_time >= %s AND m.match_time <= %s
                   UNION ALL
                   SELECT m.team_b,
                          CASE WHEN s.b_wins > s.a_wins THEN 1 ELSE 0 END,
                          CASE WHEN s.b_wins < s.a_wins THEN 1 ELSE 0 END,
                          CASE WHEN s.a_wins = s.b_wins THEN 1 ELSE 0 END
                   FROM matches m JOIN match_scores s ON m.id = s.match_id
                   WHERE m.group_id = %s AND m.match_time >= %s AND m.match_time <= %s
               )
               SELECT team, SUM(win) wins, SUM(loss) losses, SUM(draw) draws, COUNT(*) total,
                      ROUND(SUM(win)*100.0/NULLIF(SUM(win)+SUM(loss),0),1) AS win_rate
               FROM team_sides GROUP BY team
               ORDER BY win_rate DESC, wins DESC, total DESC, team ASC
               LIMIT %s""",
            (group_id, d1, d2, group_id, d1, d2, limit),
        )

    async def get_player_trend(
        self,
        group_id: str,
        player: str,
        date_from: str | None = None,
    ) -> list[tuple[str, int, int]]:
        """个人按日期的胜/负场次，返回 [(date, wins, losses)]。"""
        d1, _ = self._date_bounds(date_from, None)
        rows = await self._query(
            """SELECT m.match_time AS date,
                      SUM(CASE WHEN d.result='A' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN d.result='B' THEN 1 ELSE 0 END) AS losses
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.group_id = %s AND d.player_a = %s AND m.match_time >= %s
               GROUP BY m.match_time
               UNION ALL
               SELECT m.match_time,
                      SUM(CASE WHEN d.result='B' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN d.result='A' THEN 1 ELSE 0 END)
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.group_id = %s AND d.player_b = %s AND m.match_time >= %s
               GROUP BY m.match_time""",
            (group_id, player, d1, group_id, player, d1),
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
    ) -> list[tuple[str, int, int]]:
        """队伍按日期的胜/负场次，返回 [(date, wins, losses)]。"""
        d1, _ = self._date_bounds(date_from, None)
        rows = await self._query(
            """WITH match_scores AS (
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
                   WHERE m.group_id = %s AND m.team_a = %s AND m.match_time >= %s
                   UNION ALL
                   SELECT m.match_time, m.team_b,
                          CASE WHEN s.b_wins > s.a_wins THEN 1 ELSE 0 END,
                          CASE WHEN s.b_wins < s.a_wins THEN 1 ELSE 0 END
                   FROM matches m JOIN match_scores s ON m.id = s.match_id
                   WHERE m.group_id = %s AND m.team_b = %s AND m.match_time >= %s
               )
               SELECT date, SUM(win) wins, SUM(loss) losses
               FROM team_sides GROUP BY date ORDER BY date""",
            (group_id, team, d1, group_id, team, d1),
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
