"""MySQL 存储层（aiomysql 异步驱动 + 连接池）。

负责建库、建表、迁移，以及战报/名单的 CRUD 与聚合查询。所有方法均为异步，
通过连接池在 asyncio 事件循环中执行，不阻塞事件循环。
"""

import re
import time
from typing import Any

import aiomysql

try:
    from . import stats
except ImportError:  # 单元测试以顶层模块方式导入 database
    import stats

SCHEMA_VERSION = 13

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
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        group_id VARCHAR(64) NOT NULL,
                        team_a VARCHAR(64) NOT NULL,
                        team_b VARCHAR(64) NOT NULL,
                        match_time DATE NOT NULL,
                        rule VARCHAR(128) DEFAULT '',
                        location VARCHAR(64) DEFAULT '',
                        submitted_by VARCHAR(64) DEFAULT '',
                        submitted_name VARCHAR(128) DEFAULT '',
                        created_at BIGINT NOT NULL,
                        INDEX idx_matches_group_time (group_id, match_time),
                        INDEX idx_matches_group (group_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS duels (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        match_id BIGINT NOT NULL,
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
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
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
                        created_at BIGINT NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS users (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        home_team VARCHAR(64) NOT NULL,
                        name VARCHAR(64) NOT NULL,
                        qq_id VARCHAR(64) DEFAULT '',
                        created_at BIGINT NOT NULL,
                        UNIQUE KEY uk_team_name (home_team, name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS player_ids (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        home_team VARCHAR(64) NOT NULL,
                        player_name VARCHAR(64) NOT NULL,
                        user_id BIGINT NULL,
                        created_at BIGINT NOT NULL,
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
                if current < 7:
                    # v7：群禁用表（超级管理员控制群级功能开关）
                    await cur.execute(
                        """CREATE TABLE IF NOT EXISTS group_ban (
                            group_id VARCHAR(64) PRIMARY KEY,
                            banned INT NOT NULL,
                            created_at BIGINT NOT NULL
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
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
                if current < 8:
                    # v8：matches 记录原始战报文本（逐字回放导出）；duels 记录对阵顺序
                    await cur.execute("ALTER TABLE matches ADD COLUMN raw_text MEDIUMTEXT")
                    await cur.execute("ALTER TABLE duels ADD COLUMN seq INT NOT NULL DEFAULT 0")
                    # 回填旧数据 seq：按 match 内 id 顺序编号
                    await cur.execute(
                        """UPDATE duels d JOIN (
                               SELECT id, ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY id) rn
                               FROM duels
                           ) x ON d.id = x.id SET d.seq = x.rn"""
                    )
                if current < 9:
                    # v9：duels 记录替补标识（a_sub / b_sub，1=替补）
                    await cur.execute("ALTER TABLE duels ADD COLUMN a_sub TINYINT NOT NULL DEFAULT 0")
                    await cur.execute("ALTER TABLE duels ADD COLUMN b_sub TINYINT NOT NULL DEFAULT 0")
                if current < 10:
                    # v10：自增主键/外键/QQ用户ID/时间戳从 INT 扩容到 BIGINT，
                    # 防止数据量达十位后自增溢出、QQ号溢出、以及 2038 年时间戳溢出。
                    await cur.execute(
                        "SELECT COUNT(*) AS n FROM information_schema.TABLE_CONSTRAINTS "
                        "WHERE constraint_schema = DATABASE() AND table_name = 'duels' "
                        "AND constraint_name = 'fk_duels_match'"
                    )
                    fk_exists = (await cur.fetchone())[0] > 0
                    if fk_exists:
                        # 外键会阻止 match_id 类型变更，先删后加
                        await cur.execute("ALTER TABLE duels DROP FOREIGN KEY fk_duels_match")
                    for sql in (
                        "ALTER TABLE matches MODIFY id BIGINT NOT NULL AUTO_INCREMENT",
                        "ALTER TABLE matches MODIFY created_at BIGINT NOT NULL",
                        "ALTER TABLE duels MODIFY id BIGINT NOT NULL AUTO_INCREMENT",
                        "ALTER TABLE duels MODIFY match_id BIGINT NOT NULL",
                        "ALTER TABLE teams MODIFY id BIGINT NOT NULL AUTO_INCREMENT",
                        "ALTER TABLE users MODIFY id BIGINT NOT NULL AUTO_INCREMENT",
                        "ALTER TABLE users MODIFY created_at BIGINT NOT NULL",
                        "ALTER TABLE player_ids MODIFY id BIGINT NOT NULL AUTO_INCREMENT",
                        "ALTER TABLE player_ids MODIFY user_id BIGINT NULL",
                        "ALTER TABLE player_ids MODIFY created_at BIGINT NOT NULL",
                        "ALTER TABLE group_home MODIFY created_at BIGINT NOT NULL",
                        "ALTER TABLE group_ban MODIFY created_at BIGINT NOT NULL",
                    ):
                        await cur.execute(sql)
                    if fk_exists:
                        await cur.execute(
                            "ALTER TABLE duels ADD CONSTRAINT fk_duels_match FOREIGN KEY (match_id) "
                            "REFERENCES matches(id) ON DELETE CASCADE"
                        )
                if current < 11:
                    # v11：群属性表（友谊群/战报群/主群，缺省友谊群）
                    await cur.execute(
                        """CREATE TABLE IF NOT EXISTS group_chat_type (
                            group_id VARCHAR(64) PRIMARY KEY,
                            chat_type VARCHAR(16) NOT NULL
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                    )
                if current < 12:
                    # v12：duels 记录判罚落败标记（a_ruled / b_ruled，1=判罚落败）
                    await cur.execute("ALTER TABLE duels ADD COLUMN a_ruled TINYINT NOT NULL DEFAULT 0")
                    await cur.execute("ALTER TABLE duels ADD COLUMN b_ruled TINYINT NOT NULL DEFAULT 0")
                if current < 13:
                    # v13：判罚标记合并为单字段 ruled（1=本场对局被规则）；判罚方比分更低即败方
                    await cur.execute("ALTER TABLE duels ADD COLUMN ruled TINYINT NOT NULL DEFAULT 0")
                    await cur.execute(
                        "UPDATE duels SET ruled = 1 WHERE a_ruled = 1 OR b_ruled = 1"
                    )
                    await cur.execute("ALTER TABLE duels DROP COLUMN a_ruled")
                    await cur.execute("ALTER TABLE duels DROP COLUMN b_ruled")
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

    async def insert_report(self, report, winner: str = "", home_team: str = "", raw_text: str = "") -> int:
        """插入一份战报（match + duels），返回 match_id。winner 为胜者，home_team 为上传方主体战队，raw_text 为原始战报文本（逐字回放导出用）。"""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """INSERT INTO matches
                           (group_id, team_a, team_b, match_time, rule, location,
                            submitted_by, submitted_name, created_at, winner, home_team, raw_text)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                            raw_text or "",
                        ),
                    )
                    match_id = cur.lastrowid
                    now = int(time.time())
                    for seq, duel in enumerate(report.duels):
                        await cur.execute(
                            """INSERT INTO duels
                               (match_id, round_no, player_a, score_a, player_b, score_b,
                                player_a_team, player_b_team, result, seq, a_sub, b_sub, ruled)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                                seq,
                                1 if getattr(duel, "a_sub", False) else 0,
                                1 if getattr(duel, "b_sub", False) else 0,
                                1 if getattr(duel, "ruled", False) else 0,
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
        """删除指定群、指定 ID 的战报及其关联对局（校验群归属），返回是否删除成功。

        先按群归属校验后删除 duels 关联数据，再删除 matches 本身。若外键
        ON DELETE CASCADE 未生效（旧表结构），duels 不会残留。
        """
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """DELETE FROM duels
                           WHERE match_id IN (SELECT id FROM matches
                                              WHERE id = %s AND group_id = %s)""",
                        (match_id, group_id),
                    )
                    await cur.execute(
                        "DELETE FROM matches WHERE id = %s AND group_id = %s",
                        (match_id, group_id),
                    )
                    deleted = cur.rowcount
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return deleted > 0

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

    async def set_group_chat_type(self, group_id: str, chat_type: str) -> None:
        """设置群属性（友谊群/战报群/主群）。"""
        await self._execute(
            """INSERT INTO group_chat_type (group_id, chat_type)
               VALUES (%s, %s) AS new
               ON DUPLICATE KEY UPDATE chat_type = new.chat_type""",
            (group_id, chat_type),
        )

    async def get_group_chat_type(self, group_id: str) -> str:
        """获取群属性；未绑定缺省为友谊群。"""
        rows = await self._query(
            "SELECT chat_type FROM group_chat_type WHERE group_id = %s",
            (group_id,),
        )
        return rows[0]["chat_type"] if rows else "友谊群"

    async def backfill_group_home(self, group_id: str, home_team: str) -> None:
        """把该群已有战报的 home_team 回填为当前主体。"""
        await self._execute(
            "UPDATE matches SET home_team = %s WHERE group_id = %s AND home_team = ''",
            (home_team, group_id),
        )

    # ---------- 群禁用 ----------

    async def set_group_ban(self, group_id: str, banned: bool) -> None:
        """设置群禁用状态（超级管理员控制）。"""
        await self._execute(
            """INSERT INTO group_ban (group_id, banned, created_at)
               VALUES (%s, %s, %s) AS new
               ON DUPLICATE KEY UPDATE banned = new.banned""",
            (group_id, 1 if banned else 0, int(time.time())),
        )

    async def get_group_ban(self, group_id: str) -> bool:
        """群是否被禁用。"""
        rows = await self._query(
            "SELECT banned FROM group_ban WHERE group_id = %s",
            (group_id,),
        )
        return bool(rows and rows[0]["banned"])

    async def get_all_teams(self) -> list[str]:
        """全部战队（来自绑定、参赛ID、战报对阵）。"""
        rows = await self._query(
            """SELECT DISTINCT t.team FROM (
                   SELECT home_team AS team FROM group_home WHERE home_team != ''
                   UNION SELECT home_team FROM player_ids WHERE home_team != ''
                   UNION SELECT team_a FROM matches WHERE team_a != ''
                   UNION SELECT team_b FROM matches WHERE team_b != ''
               ) t ORDER BY t.team"""
        )
        return [r["team"] for r in rows]

    async def get_all_groups(self, home_team: str | None = None) -> list[dict]:
        """全部群及其绑定战队与禁用状态；可按战队过滤。"""
        rows = await self._query(
            """SELECT t.group_id, t.home_team, t.banned FROM (
                   SELECT g.group_id, g.home_team, COALESCE(b.banned, 0) AS banned
                   FROM group_home g LEFT JOIN group_ban b ON g.group_id = b.group_id
                   UNION
                   SELECT b.group_id, '', b.banned FROM group_ban b
                   LEFT JOIN group_home g ON b.group_id = g.group_id
                   WHERE g.group_id IS NULL
               ) t ORDER BY t.group_id"""
        )
        if home_team:
            rows = [r for r in rows if r["home_team"] == home_team]
        return rows

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

    async def rename_user(self, home_team: str, user_id: int, new_name: str) -> str:
        """修改用户角色名（战队内唯一）。返回状态：'ok' / 'conflict'。"""
        rows = await self._query(
            "SELECT id FROM users WHERE home_team = %s AND name = %s AND id != %s",
            (home_team, new_name, user_id),
        )
        if rows:
            return "conflict"
        await self._execute(
            "UPDATE users SET name = %s WHERE id = %s",
            (new_name, user_id),
        )
        return "ok"

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

    async def unbind_player(self, home_team: str, player_name: str) -> None:
        """解除参赛ID绑定（user_id 置 NULL）。"""
        await self._execute(
            "UPDATE player_ids SET user_id = NULL WHERE home_team = %s AND player_name = %s",
            (home_team, player_name),
        )

    async def get_user_players(self, home_team: str, user_id: int) -> list[str]:
        """某用户绑定的参赛ID列表。"""
        rows = await self._query(
            "SELECT player_name FROM player_ids WHERE home_team = %s AND user_id = %s ORDER BY id",
            (home_team, user_id),
        )
        return [r["player_name"] for r in rows]

    async def resolve_role(self, home_team: str, name: str) -> dict | None:
        """把名字解析为角色：name 可以是已绑定的参赛ID 或 角色名。

        命中返回 {"user_name": 角色名, "players": [该角色全部参赛ID]}；无绑定或角色无参赛ID返回 None。
        """
        rows = await self._query(
            """SELECT u.id AS user_id, u.name AS user_name
               FROM player_ids pi JOIN users u ON u.home_team = pi.home_team AND u.id = pi.user_id
               WHERE pi.home_team = %s AND pi.player_name = %s LIMIT 1""",
            (home_team, name),
        )
        if not rows:
            rows = await self._query(
                "SELECT id AS user_id, name AS user_name FROM users WHERE home_team = %s AND name = %s LIMIT 1",
                (home_team, name),
            )
        if not rows:
            return None
        players = await self.get_user_players(home_team, rows[0]["user_id"])
        if not players:
            return None
        return {"user_name": rows[0]["user_name"], "players": players}

    async def get_players_aggregate(
        self,
        home_team: str,
        players: list[str],
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """多个参赛ID合并战绩统计（按战队跨群）。"""
        total = {"wins": 0, "losses": 0, "draws": 0, "total": 0}
        for p in players:
            rec = await self.get_player_record(home_team, p, date_from, date_to)
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
        home_team: str,
        date_from: str | None = None,
        date_to: str | None = None,
        min_games: int = 1,
        limit: int = 10,
        team: str | None = None,
    ) -> list[dict]:
        """个人排行（积分 = 胜场 × 胜率，胜率用小数，保留两位），按战队跨群统计。

        team 非空时只统计该战队选手。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        params: list = [home_team, d1, d2]
        params += [home_team, d1, d2]
        team_clause = ""
        if team:
            team_clause = "WHERE sides.team = %s "
            params.append(team)
        params.append(min_games)
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT %s"
            params.append(limit)
        rows = await self._query(
            f"""WITH sides AS (
                   SELECT COALESCE(u.name, d.player_a) AS player, d.player_a_team AS team,
                          CASE d.result WHEN 'A' THEN 1 ELSE 0 END AS win,
                          CASE d.result WHEN 'B' THEN 1 ELSE 0 END AS loss,
                          CASE WHEN d.result = 'DRAW' AND NOT (d.score_a = 0 AND d.score_b = 0)
                               THEN 1 ELSE 0 END AS draw
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   LEFT JOIN player_ids pi ON pi.home_team = d.player_a_team AND pi.player_name = d.player_a
                   LEFT JOIN users u ON u.home_team = pi.home_team AND u.id = pi.user_id
                   WHERE m.home_team = %s AND m.match_time >= %s AND m.match_time <= %s
                   UNION ALL
                   SELECT COALESCE(u.name, d.player_b), d.player_b_team,
                          CASE d.result WHEN 'B' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'A' THEN 1 ELSE 0 END,
                          CASE WHEN d.result = 'DRAW' AND NOT (d.score_a = 0 AND d.score_b = 0)
                               THEN 1 ELSE 0 END
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   LEFT JOIN player_ids pi ON pi.home_team = d.player_b_team AND pi.player_name = d.player_b
                   LEFT JOIN users u ON u.home_team = pi.home_team AND u.id = pi.user_id
                   WHERE m.home_team = %s AND m.match_time >= %s AND m.match_time <= %s
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
        # 合并每人的比赛级统计（友谊次数/无双次数）
        match_stats = await self.get_player_match_stats(home_team, date_from, date_to)
        for r in rows:
            st = match_stats.get(r["player"], {"friendship": 0, "wushuang": 0})
            r["friendship"] = st["friendship"]
            r["wushuang"] = st["wushuang"]
        return rows

    async def get_player_match_stats(
        self,
        home_team: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, dict]:
        """统计区间内每人参与的比赛场次（友谊次数）与无双次数。

        友谊次数 = 参与的去重比赛场次数（有 ≥1 场非 0:0 对局）。
        无双次数 = 场次级判定（见 stats.compute_match_stats），每场至多一人。
        返回 {已解析玩家名: {"friendship": int, "wushuang": int}}。
        名字解析与 get_player_ranking 的 sides CTE 一致（COALESCE(u.name, 参赛ID)）。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        rows = await self._query(
            """SELECT m.id AS match_id, m.winner,
                      d.seq, d.score_a, d.score_b,
                      d.player_a_team, d.player_b_team,
                      COALESCE(ua.name, d.player_a) AS resolved_a,
                      COALESCE(ub.name, d.player_b) AS resolved_b
               FROM matches m
               LEFT JOIN duels d ON d.match_id = m.id
               LEFT JOIN player_ids pia
                      ON pia.home_team = d.player_a_team AND pia.player_name = d.player_a
               LEFT JOIN users ua ON ua.home_team = pia.home_team AND ua.id = pia.user_id
               LEFT JOIN player_ids pib
                      ON pib.home_team = d.player_b_team AND pib.player_name = d.player_b
               LEFT JOIN users ub ON ub.home_team = pib.home_team AND ub.id = pib.user_id
               WHERE m.home_team = %s AND m.match_time >= %s AND m.match_time <= %s
               ORDER BY m.id, d.seq, d.id""",
            (home_team, d1, d2),
        )
        totals: dict[str, dict] = {}
        cur: list[dict] = []
        cur_id: int | None = None
        cur_winner = ""

        def flush() -> None:
            if not cur:
                return
            for name, st in stats.compute_match_stats(cur, cur_winner).items():
                t = totals.setdefault(name, {"friendship": 0, "wushuang": 0})
                t["friendship"] += st["friendship"]
                t["wushuang"] += st["wushuang"]

        for r in rows:
            if r["match_id"] != cur_id:
                flush()
                cur, cur_id, cur_winner = [], r["match_id"], r["winner"] or ""
            if r["resolved_a"] is None:  # LEFT JOIN：该比赛无对局
                continue
            cur.append({
                "seq": int(r["seq"] or 0),
                "score_a": r["score_a"],
                "score_b": r["score_b"],
                "player_a_team": r["player_a_team"],
                "player_b_team": r["player_b_team"],
                "resolved_a": r["resolved_a"],
                "resolved_b": r["resolved_b"],
            })
        flush()
        return totals

    async def get_player_record(
        self,
        home_team: str,
        player: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """单个玩家的战绩汇总，按战队跨群统计。

        同时限定该选手属于该战队（避免同名不同队选手混入）。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        params: list = [player, d1, d2, home_team, home_team]
        params += [player, d1, d2, home_team, home_team]
        params.append(player)
        rows = await self._query(
            f"""WITH sides AS (
                   SELECT CASE d.result WHEN 'A' THEN 1 ELSE 0 END AS win,
                          CASE d.result WHEN 'B' THEN 1 ELSE 0 END AS loss,
                          CASE WHEN d.result = 'DRAW' AND NOT (d.score_a = 0 AND d.score_b = 0)
                               THEN 1 ELSE 0 END AS draw
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE d.player_a = %s
                     AND m.match_time >= %s AND m.match_time <= %s
                     AND m.home_team = %s AND d.player_a_team = %s
                   UNION ALL
                   SELECT CASE d.result WHEN 'B' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'A' THEN 1 ELSE 0 END,
                          CASE WHEN d.result = 'DRAW' AND NOT (d.score_a = 0 AND d.score_b = 0)
                               THEN 1 ELSE 0 END
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE d.player_b = %s
                     AND m.match_time >= %s AND m.match_time <= %s
                     AND m.home_team = %s AND d.player_b_team = %s
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
        home_team: str,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """队伍战绩排行（一场比赛胜者是该场对局赢得更多的一方），按战队跨群统计。"""
        d1, d2 = self._date_bounds(date_from, date_to)
        params = [home_team, d1, d2]
        params += [home_team, d1, d2]
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
                   WHERE m.home_team = %s AND m.match_time >= %s AND m.match_time <= %s
                   UNION ALL
                   SELECT m.team_b,
                          CASE WHEN s.b_wins > s.a_wins THEN 1 ELSE 0 END,
                          CASE WHEN s.b_wins < s.a_wins THEN 1 ELSE 0 END,
                          CASE WHEN s.a_wins = s.b_wins THEN 1 ELSE 0 END
                   FROM matches m JOIN match_scores s ON m.id = s.match_id
                   WHERE m.home_team = %s AND m.match_time >= %s AND m.match_time <= %s
               )
               SELECT team, SUM(win) wins, SUM(loss) losses, SUM(draw) draws, COUNT(*) total,
                      ROUND(SUM(win)*SUM(win)/NULLIF(SUM(win)+SUM(loss),0),2) AS points
               FROM team_sides GROUP BY team
               ORDER BY points DESC, wins DESC, total DESC, team ASC
               LIMIT %s""",
            tuple(params),
        )

    async def get_home_team_vs_opponents(
        self,
        home_team: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """主体战队对战各对手的胜负记录（按战队跨群），返回 [{opponent, wins, losses, total, win_rate}]。"""
        d1, d2 = self._date_bounds(date_from, date_to)
        rows = await self._query(
            """SELECT opponent,
                      SUM(CASE WHEN winner = %s THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN winner != %s THEN 1 ELSE 0 END) AS losses,
                      COUNT(*) AS total
               FROM (
                   SELECT CASE WHEN m.team_a = %s THEN m.team_b ELSE m.team_a END AS opponent,
                          m.winner
                   FROM matches m
                   WHERE m.home_team = %s AND (m.team_a = %s OR m.team_b = %s)
                     AND m.winner != '' AND m.match_time >= %s AND m.match_time <= %s
               ) t
               GROUP BY opponent
               ORDER BY total DESC, wins DESC""",
            (home_team, home_team, home_team, home_team, home_team, home_team, d1, d2),
        )
        result = []
        for r in rows:
            w, l = int(r["wins"] or 0), int(r["losses"] or 0)
            total = int(r["total"] or 0)
            wr = round(w * 100.0 / (w + l), 1) if (w + l) else 0.0
            result.append({
                "opponent": r["opponent"], "wins": w, "losses": l,
                "total": total, "win_rate": wr,
            })
        return result

    async def get_home_team_record(
        self,
        home_team: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """主体战队总体战绩（按胜者判定，按战队跨群），返回 {wins, losses, draws, total, win_rate}。"""
        d1, d2 = self._date_bounds(date_from, date_to)
        rows = await self._query(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN winner = %s THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN winner != %s THEN 1 ELSE 0 END) AS losses
               FROM matches m
               WHERE m.home_team = %s AND m.winner != ''
                 AND m.match_time >= %s AND m.match_time <= %s""",
            (home_team, home_team, home_team, d1, d2),
        )
        r = rows[0] if rows else {}
        total = int(r.get("total") or 0)
        wins = int(r.get("wins") or 0)
        losses = int(r.get("losses") or 0)
        wr = round(wins * 100.0 / (wins + losses), 1) if (wins + losses) else 0.0
        return {"wins": wins, "losses": losses, "draws": 0, "total": total, "win_rate": wr}

    async def get_player_trend(
        self,
        home_team: str,
        player: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[tuple[str, int, int]]:
        """个人按日期的胜/负场次（按战队跨群），返回 [(date, wins, losses)]。"""
        d1, d2 = self._date_bounds(date_from, date_to)
        date_clause = " AND m.match_time <= %s" if date_to else ""
        params = [home_team, player, d1]
        if date_to:
            params.append(d2)
        params += [home_team, player, d1]
        if date_to:
            params.append(d2)
        rows = await self._query(
            f"""SELECT m.match_time AS date,
                      SUM(CASE WHEN d.result='A' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN d.result='B' THEN 1 ELSE 0 END) AS losses
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.home_team = %s AND d.player_a = %s AND m.match_time >= %s{date_clause}
               GROUP BY m.match_time
               UNION ALL
               SELECT m.match_time,
                      SUM(CASE WHEN d.result='B' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN d.result='A' THEN 1 ELSE 0 END)
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.home_team = %s AND d.player_b = %s AND m.match_time >= %s{date_clause}
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

    async def get_players_trend(
        self,
        home_team: str,
        players: list[str],
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[tuple[str, int, int]]:
        """多个参赛ID合并按日期的胜/负场次（按战队跨群，限定战队避免同名混淆），返回 [(date, wins, losses)]。"""
        if not players:
            return []
        d1, d2 = self._date_bounds(date_from, date_to)
        ph = ",".join(["%s"] * len(players))
        date_clause = " AND m.match_time <= %s" if date_to else ""
        params = [home_team, d1, home_team]
        if date_to:
            params.append(d2)
        params += list(players)
        params += [home_team, d1, home_team]
        if date_to:
            params.append(d2)
        params += list(players)
        rows = await self._query(
            f"""SELECT m.match_time AS date,
                      SUM(CASE WHEN d.result='A' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN d.result='B' THEN 1 ELSE 0 END) AS losses
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.home_team = %s AND m.match_time >= %s
                 AND d.player_a_team = %s AND d.player_a IN ({ph}){date_clause}
               GROUP BY m.match_time
               UNION ALL
               SELECT m.match_time,
                      SUM(CASE WHEN d.result='B' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN d.result='A' THEN 1 ELSE 0 END)
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.home_team = %s AND m.match_time >= %s
                 AND d.player_b_team = %s AND d.player_b IN ({ph}){date_clause}
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
        home_team: str,
        team: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[tuple[str, int, int]]:
        """队伍按日期的胜/负场次（按战队跨群），返回 [(date, wins, losses)]。"""
        d1, d2 = self._date_bounds(date_from, date_to)
        date_clause = " AND m.match_time <= %s" if date_to else ""
        params = [home_team, team, d1]
        if date_to:
            params.append(d2)
        params += [home_team, team, d1]
        if date_to:
            params.append(d2)
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
                   WHERE m.home_team = %s AND m.team_a = %s AND m.match_time >= %s{date_clause}
                   UNION ALL
                   SELECT m.match_time, m.team_b,
                          CASE WHEN s.b_wins > s.a_wins THEN 1 ELSE 0 END,
                          CASE WHEN s.b_wins < s.a_wins THEN 1 ELSE 0 END
                   FROM matches m JOIN match_scores s ON m.id = s.match_id
                   WHERE m.home_team = %s AND m.team_b = %s AND m.match_time >= %s{date_clause}
               )
               SELECT date, SUM(win) wins, SUM(loss) losses
               FROM team_sides GROUP BY date ORDER BY date""",
            tuple(params),
        )
        return [(str(r["date"]), int(r["wins"] or 0), int(r["losses"] or 0)) for r in rows]

    async def get_export_rows(self, home_team: str, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        """导出某战队战报（matches + duels 联表），按战队跨群，可按日期过滤。"""
        d1, d2 = self._date_bounds(date_from, date_to)
        date_clause = " AND m.match_time >= %s AND m.match_time <= %s" if (date_from or date_to) else ""
        params = [home_team]
        if date_from or date_to:
            params += [d1, d2]
        return await self._query(
            f"""SELECT m.id AS match_id, m.group_id, m.team_a, m.team_b,
                      m.match_time, m.rule, m.location,
                      d.round_no, d.player_a, d.score_a, d.player_b, d.score_b, d.result,
                      d.a_sub, d.b_sub, d.ruled
               FROM matches m LEFT JOIN duels d ON d.match_id = m.id
               WHERE m.home_team = %s{date_clause}
               ORDER BY m.id, d.round_no, d.seq, d.id""",
            tuple(params),
        )

    async def get_reports_for_export(self, home_team: str, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        """按战报聚合返回某战队战报（含原始文本与按 seq 排序的对局），供合并转发导出，按战队跨群、可按日期过滤。

        每份战报一个 dict：match_id / team_a / team_b / match_time / rule / location /
        winner / home_team / raw_text / submitted_by / submitted_name / duels。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        date_clause = " AND m.match_time >= %s AND m.match_time <= %s" if (date_from or date_to) else ""
        params = [home_team]
        if date_from or date_to:
            params += [d1, d2]
        rows = await self._query(
            f"""SELECT m.id AS match_id, m.team_a, m.team_b, m.match_time, m.rule, m.location,
                      m.winner, m.home_team, m.raw_text, m.submitted_by, m.submitted_name,
                      d.seq, d.round_no, d.player_a, d.score_a, d.player_b, d.score_b,
                      d.a_sub, d.b_sub, d.ruled
               FROM matches m LEFT JOIN duels d ON d.match_id = m.id
               WHERE m.home_team = %s{date_clause}
               ORDER BY m.id, d.seq, d.id""",
            tuple(params),
        )
        reports: list[dict] = []
        current: dict | None = None
        for r in rows:
            if current is None or current["match_id"] != r["match_id"]:
                current = {
                    "match_id": r["match_id"],
                    "team_a": r["team_a"],
                    "team_b": r["team_b"],
                    "match_time": str(r["match_time"]),
                    "rule": r["rule"],
                    "location": r["location"],
                    "winner": r["winner"] or "",
                    "home_team": r["home_team"] or "",
                    "raw_text": r["raw_text"] or "",
                    "submitted_by": r["submitted_by"] or "",
                    "submitted_name": r["submitted_name"] or "",
                    "duels": [],
                }
                reports.append(current)
            if r["player_a"] is not None:
                current["duels"].append({
                    "seq": r["seq"] or 0,
                    "round_no": r["round_no"],
                    "player_a": r["player_a"],
                    "score_a": r["score_a"],
                    "player_b": r["player_b"],
                    "score_b": r["score_b"],
                    "a_sub": bool(r["a_sub"]),
                    "b_sub": bool(r["b_sub"]),
                    "ruled": bool(r["ruled"]),
                })
        return reports
