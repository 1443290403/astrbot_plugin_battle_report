"""MySQL 存储层（aiomysql 异步驱动 + 连接池）。

负责建库、建表、迁移，以及战报/名单的 CRUD 与聚合查询。所有方法均为异步，
通过连接池在 asyncio 事件循环中执行，不阻塞事件循环。
"""

import re
import time
from typing import Any

import aiomysql
from pymysql.err import IntegrityError

try:
    from . import stats
    from .battle_report_parser import (
        KIND_FRIENDLY, KIND_RAID, RAID_PLACEHOLDER, report_fingerprint,
    )
except ImportError:  # 单元测试以顶层模块方式导入 database
    import stats
    from battle_report_parser import (
        KIND_FRIENDLY, KIND_RAID, RAID_PLACEHOLDER, report_fingerprint,
    )

SCHEMA_VERSION = 17

_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

# MySQL 唯一键冲突（ER.DUP_ENTRY）
_DUP_ENTRY = 1062


class DuplicateReportError(Exception):
    """同一份战报重复提交时抛出（内容指纹相同）。

    match_id: 已存在的那条 `matches.id`。并发兜底路径下若回查不到（对方刚被
    删掉），为 None，调用方需按无 ID 的文案降级提示。
    """

    def __init__(self, match_id: int | None = None):
        self.match_id = match_id
        super().__init__(
            f"该战报已存在（ID {match_id}）" if match_id else "该战报已存在"
        )


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
                        fingerprint CHAR(64) NULL DEFAULT NULL,
                        kind VARCHAR(16) NOT NULL DEFAULT 'friendly',
                        INDEX idx_matches_group_time (group_id, match_time),
                        INDEX idx_matches_group (group_id),
                        UNIQUE KEY uk_matches_fingerprint (fingerprint)
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
                        owner TINYINT NOT NULL DEFAULT 0,
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
                # 新增表用 CREATE TABLE IF NOT EXISTS 就够，**不需要 information_schema
                # 探测** —— 探测是给 ALTER 用的（重放时 ADD COLUMN 会报 1060）。建表语句
                # 本身幂等，所以这里跟 group_home / users 一样无条件建。
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS settlements (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        home_team VARCHAR(64) NOT NULL,
                        month CHAR(7) NOT NULL,
                        player_name VARCHAR(64) NOT NULL,
                        rank_no INT NOT NULL,
                        total_points DECIMAL(10,2) NOT NULL DEFAULT 0,
                        bonus DECIMAL(8,2) NOT NULL DEFAULT 0,
                        created_at BIGINT NOT NULL,
                        UNIQUE KEY uk_settle (home_team, month, player_name),
                        INDEX idx_settle_team_month (home_team, month)
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
                if current < 14:
                    # v14：回填替补标记——某选手同场首轮（round_no==1）未出场、却在后续轮次
                    # 出场，即视为替补（a_sub/b_sub=1）。领域规则：替补替换首轮选手、不扩容
                    # 总出战人数。首轮名单只认 player_a 侧（player_b 对称）；含 0:0 占位。
                    # 注意：MySQL 不允许 UPDATE 目标表在子查询里直接 SELECT 同一张表（错误
                    # 1093），须把子查询包成派生表 (r1) 强制物化快照。
                    await cur.execute(
                        """UPDATE duels d
                           SET d.a_sub = 1
                           WHERE d.round_no > 1 AND d.a_sub = 0
                             AND NOT EXISTS (
                                 SELECT 1 FROM (
                                     SELECT match_id, player_a FROM duels WHERE round_no = 1
                                 ) r1
                                 WHERE r1.match_id = d.match_id
                                   AND r1.player_a = d.player_a
                             )"""
                    )
                    await cur.execute(
                        """UPDATE duels d
                           SET d.b_sub = 1
                           WHERE d.round_no > 1 AND d.b_sub = 0
                             AND NOT EXISTS (
                                 SELECT 1 FROM (
                                     SELECT match_id, player_b FROM duels WHERE round_no = 1
                                 ) r1
                                 WHERE r1.match_id = d.match_id
                                   AND r1.player_b = d.player_b
                             )"""
                    )
                if current < 15:
                    # v15：matches 增加内容指纹列，用于拒绝「完全相同的战报」重复入库
                    # （同一份战报提交两次会让统计翻倍）。指纹由 report_fingerprint()
                    # 计算，不含 group_id/提交人/地点 —— 同一战队的另一个群再提交一次
                    # 也算重复。历史行留 NULL：MySQL 唯一索引不对 NULL 去重，所以既
                    # 不需要回填，也不会因存量重复数据而迁移失败。
                    #
                    # ⚠️ 必须先探测再执行：_init_schema 没有外层 try/except，而版本号是
                    # 所有分支跑完后才写的。若「ALTER 成功、写版本号前进程被杀」导致本
                    # 分支重放，ADD COLUMN 会报 1060、ADD UNIQUE 会报 1061 → initialize()
                    # 抛错 → db_ready=False → 整个插件永久起不来。
                    await cur.execute(
                        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                        "WHERE table_schema = DATABASE() AND table_name = 'matches' "
                        "AND column_name = 'fingerprint'"
                    )
                    if (await cur.fetchone())[0] == 0:
                        await cur.execute(
                            "ALTER TABLE matches ADD COLUMN fingerprint CHAR(64) NULL DEFAULT NULL"
                        )
                    await cur.execute(
                        "SELECT COUNT(*) AS n FROM information_schema.STATISTICS "
                        "WHERE table_schema = DATABASE() AND table_name = 'matches' "
                        "AND index_name = 'uk_matches_fingerprint'"
                    )
                    if (await cur.fetchone())[0] == 0:
                        await cur.execute(
                            "ALTER TABLE matches "
                            "ADD UNIQUE KEY uk_matches_fingerprint (fingerprint)"
                        )
                if current < 16:
                    # v16：踢馆战报（matches.kind='raid'）+ 馆主标记（duels.owner）。
                    # 复用 matches/duels 而不是新开一对表：写入/删除/撤销/指纹去重/
                    # 导出/参赛ID池全部现成。代价是**所有友谊赛聚合必须显式加
                    # `AND m.kind = 'friendly'`** —— 否则踢馆对局会混进友谊胜负与
                    # 友谊次数。新增查询时务必照做（§7.1 有传导行）。
                    #
                    # owner 只认**防守方（右侧）**，与 ruled 的单字段风格一致。
                    # 同样先探测再 ALTER：_init_schema 没有外层 try/except，重放时
                    # ADD COLUMN 报 1060 会让整个插件起不来（见 v15 注释）。
                    await cur.execute(
                        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                        "WHERE table_schema = DATABASE() AND table_name = 'matches' "
                        "AND column_name = 'kind'"
                    )
                    if (await cur.fetchone())[0] == 0:
                        await cur.execute(
                            "ALTER TABLE matches ADD COLUMN kind VARCHAR(16) "
                            "NOT NULL DEFAULT 'friendly'"
                        )
                    await cur.execute(
                        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                        "WHERE table_schema = DATABASE() AND table_name = 'duels' "
                        "AND column_name = 'owner'"
                    )
                    if (await cur.fetchone())[0] == 0:
                        await cur.execute(
                            "ALTER TABLE duels ADD COLUMN owner TINYINT NOT NULL DEFAULT 0"
                        )
                # v17：月度结算（settlements 表）。表本身在上面已经无条件建好，这里
                # 没有需要迁移的存量数据 —— 所以**没有 current < 17 的分支**，靠末尾
                # 那句统一的「写版本行」把版本推到 17 即可。
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
        # aiomysql 的 DictCursor.fetchall() 在**没有行**时返回空 tuple、有行时返回
        # list —— 空结果集的上游是 `tuple(self._rows)`，非空走的是 `list`。调用方
        # 会就地改这些行（`sort_ranking` 排序、`get_home_team_vs_opponents` 追加
        # 只踢过馆的对手），拿到 tuple 就会 AttributeError 崩在"查无记录"这条路上。
        # 在这里统一成 list，兑现 `-> list[dict]` 的签名。
        return list(rows)

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        """执行单条写语句，返回受影响行数。"""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return cur.rowcount

    # ---------- 战报写入 / 删除 ----------

    async def _find_by_fingerprint(self, fingerprint: str) -> int | None:
        """按内容指纹查既有战报 id；不存在返回 None。"""
        rows = await self._query(
            "SELECT id FROM matches WHERE fingerprint = %s LIMIT 1", (fingerprint,)
        )
        return int(rows[0]["id"]) if rows else None

    async def insert_report(self, report, winner: str = "", home_team: str = "", raw_text: str = "") -> int:
        """插入一份战报（match + duels），返回 match_id。winner 为胜者，home_team 为上传方主体战队，raw_text 为原始战报文本（逐字回放导出用）。

        内容指纹与既有战报完全相同时抛 `DuplicateReportError`（带已存在的
        match_id），且不写入任何数据 —— 同一份战报提交两次会让统计翻倍。
        """
        assert self.pool is not None
        fingerprint = report_fingerprint(report, home_team)
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    # 预检：用本事务内的 cursor，而不是 self._query()（后者会另借一条
                    # 连接、脱离本事务）。这只是为了拿到友好提示所需的旧 id；并发场景
                    # 由下面的唯一索引兜住，所以此处不需要加锁。
                    await cur.execute(
                        "SELECT id FROM matches WHERE fingerprint = %s LIMIT 1",
                        (fingerprint,),
                    )
                    row = await cur.fetchone()
                    if row:
                        raise DuplicateReportError(int(row[0]))
                    await cur.execute(
                        """INSERT INTO matches
                           (group_id, team_a, team_b, match_time, rule, location,
                            submitted_by, submitted_name, created_at, winner, home_team,
                            raw_text, fingerprint, kind)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                            fingerprint,
                            getattr(report, "kind", KIND_FRIENDLY) or KIND_FRIENDLY,
                        ),
                    )
                    match_id = cur.lastrowid
                    now = int(time.time())
                    for seq, duel in enumerate(report.duels):
                        await cur.execute(
                            """INSERT INTO duels
                               (match_id, round_no, player_a, score_a, player_b, score_b,
                                player_a_team, player_b_team, result, seq, a_sub, b_sub, ruled,
                                owner)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                                1 if getattr(duel, "owner", False) else 0,
                            ),
                        )
                        # 参赛ID按队伍去重入库（发送战报时处理，保留已有绑定）。
                        # 踢馆报里防守方是 `规则` 的空位行不进 ID 池：那不是一个选手，
                        # 建进去会让战队花名册里多出一个叫「规则」的假人。
                        for team, name in (
                            (report.team_a, duel.player_a),
                            (report.team_b, duel.player_b),
                        ):
                            if name == RAID_PLACEHOLDER:
                                continue
                            await cur.execute(
                                """INSERT INTO player_ids (home_team, player_name, user_id, created_at)
                                   VALUES (%s, %s, NULL, %s) AS new
                                   ON DUPLICATE KEY UPDATE player_name = new.player_name""",
                                (team, name, now),
                            )
                await conn.commit()
                return match_id
            except DuplicateReportError:
                await conn.rollback()
                raise
            except IntegrityError as e:
                # 并发兜底：两个请求同时通过上面的预检时，唯一索引会拦住后到的那个。
                # InnoDB 会让后到的插入等待前一个事务结束，所以拿到 1062 时对方
                # 一定已提交 —— 回滚后用新连接回查能得到它的 id（在本事务里查是
                # REPEATABLE READ 的旧快照，查不到）。回查不到（对方刚被删）则给 None。
                await conn.rollback()
                if e.args and e.args[0] == _DUP_ENTRY and "uk_matches_fingerprint" in str(e):
                    raise DuplicateReportError(
                        await self._find_by_fingerprint(fingerprint)
                    ) from None
                raise
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

    async def get_qq_ids_by_names(
        self, home_team: str, names: list[str]
    ) -> dict[str, str]:
        """展示名 → qq_id（**只含 qq_id 非空的**），用于结算公告里的 @。

        按 `users.name` 查，**不是**按 `player_ids.player_name`：排行榜上的
        `player` 是 `COALESCE(u.name, d.player_a)`（§5-M03 的展示名），绑定了角色
        的选手在榜上显示的是**角色名**，用参赛ID去查会一个都匹配不上。

        查不到的（没绑定 / 绑了但没存 QQ / 纯参赛ID）**不出现在返回值里**，
        调用方按「@ 不出来」退化处理，见 `main.settle`。
        """
        if not names:
            return {}
        placeholders = ", ".join(["%s"] * len(names))
        rows = await self._query(
            f"""SELECT name, qq_id FROM users
                WHERE home_team = %s AND name IN ({placeholders}) AND qq_id <> ''""",
            (home_team, *names),
        )
        return {r["name"]: r["qq_id"] for r in rows}

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
            rec = await self.get_player_record(
                home_team, p, date_from, date_to, with_raid=False
            )
            total["wins"] += int(rec.get("wins", 0))
            total["losses"] += int(rec.get("losses", 0))
            total["draws"] += int(rec.get("draws", 0))
            total["total"] += int(rec.get("total", 0))
        # 友谊次数 = 集合内任一玩家在本战队一侧出场的去重比赛场次数
        # （合并多ID时按比赛去重，避免同一场出现该用户多个ID被重复计）
        total["friendship"] = 0
        if players:
            d1, d2 = self._date_bounds(date_from, date_to)
            ph = ",".join(["%s"] * len(players))
            params = [home_team, d1, d2] + players + [home_team] + players + [home_team]
            frows = await self._query(
                f"""SELECT COUNT(DISTINCT m.id) AS friendship
                    FROM duels d JOIN matches m ON d.match_id = m.id
                    WHERE m.home_team = %s AND m.match_time >= %s AND m.match_time <= %s
                      AND m.kind = 'friendly'
                      AND NOT (d.score_a = 0 AND d.score_b = 0)
                      AND ((d.player_a IN ({ph}) AND d.player_a_team = %s)
                           OR (d.player_b IN ({ph}) AND d.player_b_team = %s))""",
                tuple(params),
            )
            total["friendship"] = int(frows[0]["friendship"] or 0)
        # 踢馆积分在**合并后的ID集合**上按同一套规则重算（不是各ID相加）
        total["raid_points"] = int((
            await self.get_raid_stats_for_players(home_team, players, date_from, date_to)
        )["raid_points"])
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

        **「积分」列只算友谊赛**；踢馆积分由本方法另行并入 `raid_points`/
        `total_points`，名次按**总积分**排。因此最终排序与截断在 Python 侧做
        （`stats.sort_ranking`）—— SQL 的 `LIMIT` 会先按友谊积分截断，把靠踢馆
        冲上来的选手直接挡在榜外，所以这里**不能**带 LIMIT。

        只在踢馆里出场、没有任何友谊对局的选手不在此榜（`HAVING total >=
        min_games` 的门槛按友谊场次算），但 `/踢馆` 与 `/战绩` 仍能查到他们。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        params: list = [home_team, d1, d2]
        params += [home_team, d1, d2]
        team_clause = ""
        if team:
            team_clause = "WHERE sides.team = %s "
            params.append(team)
        params.append(min_games)
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
                     AND m.kind = 'friendly'
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
                     AND m.kind = 'friendly'
               )
               SELECT player, SUM(win) wins, SUM(loss) losses, SUM(draw) draws,
                      COUNT(*) total,
                      ROUND(SUM(win) * SUM(win) / NULLIF(SUM(win)+SUM(loss), 0), 2) AS points
               FROM sides
               {team_clause}
               GROUP BY player HAVING total >= %s
               ORDER BY points DESC, wins DESC, total DESC, player ASC""",
            tuple(params),
        )
        # 合并每人的比赛级统计（友谊次数/无双次数）；team 过滤与 total 列口径一致
        match_stats = await self.get_player_match_stats(home_team, date_from, date_to, team=team)
        for r in rows:
            st = match_stats.get(r["player"], {"friendship": 0, "wushuang": 0})
            r["friendship"] = st["friendship"]
            r["wushuang"] = st["wushuang"]
        # 并入踢馆积分并算出总积分，按总积分重排 + 截断（见 docstring）。
        # 这里的 team 是「只看这一侧的选手」过滤器 → 踢馆侧对应 side_team。
        raid_by_player = await self.get_raid_player_stats(
            home_team, date_from, date_to, side_team=team
        )
        stats.merge_raid_into_ranking(rows, raid_by_player)
        return stats.sort_ranking(rows, limit)

    async def get_player_match_stats(
        self,
        home_team: str,
        date_from: str | None = None,
        date_to: str | None = None,
        team: str | None = None,
    ) -> dict[str, dict]:
        """统计区间内每人参与的比赛场次（友谊次数）与无双次数。

        友谊次数 = 参与的去重比赛场次数（有 ≥1 场非 0:0 对局）。
        无双次数 = 场次级判定（见 stats.compute_match_stats），每场至多一人。
        返回 {已解析玩家名: {"friendship": int, "wushuang": int}}。
        名字解析与 get_player_ranking 的 sides CTE 一致（COALESCE(u.name, 参赛ID)）。

        team 非空时只统计该玩家在 team 一侧出场的比赛（跨队同名碰撞排除，
        与 get_player_ranking 的 team_clause / total 列口径一致）。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        rows = await self._query(
            """SELECT m.id AS match_id, m.winner, m.rule,
                      d.seq, d.round_no, d.score_a, d.score_b,
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
                 AND m.kind = 'friendly'
               ORDER BY m.id, d.seq, d.id""",
            (home_team, d1, d2),
        )
        totals: dict[str, dict] = {}
        cur: list[dict] = []
        cur_id: int | None = None
        cur_winner = ""
        cur_rule = ""

        def flush() -> None:
            if not cur:
                return
            # rule 只影响无双的数据兜底是否启用（仅 KOF 适用），见 compute_match_stats
            st = stats.compute_match_stats(cur, cur_winner, cur_rule)
            if team:
                # 只统计在该战队一侧出场的玩家：跨队同名（如两队都有 知更）不计入本队
                on_team = set()
                for d in cur:
                    if d["player_a_team"] == team:
                        on_team.add(d["resolved_a"])
                    if d["player_b_team"] == team:
                        on_team.add(d["resolved_b"])
                for name in list(st):
                    if name not in on_team:
                        del st[name]
            for name, v in st.items():
                t = totals.setdefault(name, {"friendship": 0, "wushuang": 0})
                t["friendship"] += v["friendship"]
                t["wushuang"] += v["wushuang"]

        for r in rows:
            if r["match_id"] != cur_id:
                flush()
                # ⚠️ 三者必须一起重置：flush() 是闭包，漏掉任何一个都会把上一场比赛
                # 的值静默带进下一场（不报错，只是算错）。
                cur, cur_id, cur_winner, cur_rule = (
                    [], r["match_id"], r["winner"] or "", r["rule"] or "",
                )
            if r["resolved_a"] is None:  # LEFT JOIN：该比赛无对局
                continue
            cur.append({
                "seq": int(r["seq"] or 0),
                "round_no": int(r["round_no"] or 1),
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
        with_raid: bool = True,
    ) -> dict:
        """单个玩家的战绩汇总，按战队跨群统计。

        同时限定该选手属于该战队（避免同名不同队选手混入）。
        友谊次数 = 该玩家在本战队一侧出场的去重比赛场次数（有 ≥1 场非 0:0 对局）。
        with_raid=False 时跳过踢馆积分（`get_players_aggregate` 自己合并多ID时用，
        避免每个ID各扫一遍踢馆表）。
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
                     AND m.kind = 'friendly'
                   UNION ALL
                   SELECT CASE d.result WHEN 'B' THEN 1 ELSE 0 END,
                          CASE d.result WHEN 'A' THEN 1 ELSE 0 END,
                          CASE WHEN d.result = 'DRAW' AND NOT (d.score_a = 0 AND d.score_b = 0)
                               THEN 1 ELSE 0 END
                   FROM duels d JOIN matches m ON d.match_id = m.id
                   WHERE d.player_b = %s
                     AND m.match_time >= %s AND m.match_time <= %s
                     AND m.home_team = %s AND d.player_b_team = %s
                     AND m.kind = 'friendly'
               )
               SELECT player_name AS player,
                      SUM(win) wins, SUM(loss) losses, SUM(draw) draws, COUNT(*) total
               FROM (SELECT %s AS player_name, win, loss, draw FROM sides) t
               GROUP BY player_name""",
            tuple(params),
        )
        result = rows[0] if rows else {"player": player, "wins": 0, "losses": 0, "draws": 0, "total": 0}
        # 友谊次数：该玩家在本战队一侧出场的去重比赛场次数（有 ≥1 场非 0:0 对局）
        frows = await self._query(
            """SELECT COUNT(DISTINCT m.id) AS friendship
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.home_team = %s AND m.match_time >= %s AND m.match_time <= %s
                 AND m.kind = 'friendly'
                 AND NOT (d.score_a = 0 AND d.score_b = 0)
                 AND ((d.player_a = %s AND d.player_a_team = %s)
                      OR (d.player_b = %s AND d.player_b_team = %s))""",
            (home_team, d1, d2, player, home_team, player, home_team),
        )
        result["friendship"] = int(frows[0]["friendship"] or 0)
        result["raid_points"] = 0
        if with_raid:
            # 踢馆积分（独立于友谊口径的胜负；按「同一套规则按人重算」得出）。
            # 传进来的是参赛ID，踢馆侧按已解析名聚合，先按 M-03 解析一次。
            resolved = (await self._resolve_player_names(home_team, [player]))[player]
            raid = await self.get_raid_player_stats(
                home_team, date_from, date_to, side_team=home_team
            )
            mine = raid.get(resolved, {})
            result["raid_points"] = int(mine.get("raid_points", 0))
            # 三个计数字段供 `/战绩` 展示（与排行末三列同源，取不到时按 0）
            result["raid_success"] = int(mine.get("raid_success", 0))
            result["hold"] = int(mine.get("hold", 0))
            result["first_round"] = int(mine.get("first_round", 0))
        return result

    async def get_home_team_vs_opponents(
        self,
        home_team: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """主体战队对战各对手的胜负记录（按战队跨群），返回 [{opponent, wins, losses, total, win_rate, raid_points}]。

        `raid_points` 是该对手的交手踢馆积分（踢馆方按对手队取最高那场，与
        `stats.aggregate_raid` 的月度降重口径一致），没有踢馆记录时为 0。
        胜负列**不含**踢馆（那走 raid_points）。
        """
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
                     AND m.kind = 'friendly'
               ) t
               GROUP BY opponent
               ORDER BY total DESC, wins DESC""",
            (home_team, home_team, home_team, home_team, home_team, home_team, d1, d2),
        )
        raid = await self.get_raid_points_by_opponent(home_team, date_from, date_to)
        result = []
        for r in rows:
            w, l = int(r["wins"] or 0), int(r["losses"] or 0)
            total = int(r["total"] or 0)
            wr = round(w * 100.0 / (w + l), 1) if (w + l) else 0.0
            result.append({
                "opponent": r["opponent"], "wins": w, "losses": l,
                "total": total, "win_rate": wr,
                "raid_points": int(raid.get(r["opponent"], 0)),
            })
        # 只在友谊赛里交手过、但有过踢馆的对手：补进列表（否则踢馆分无处显示）
        for opp, pts in raid.items():
            if pts and opp not in {x["opponent"] for x in result}:
                result.append({
                    "opponent": opp, "wins": 0, "losses": 0,
                    "total": 0, "win_rate": 0.0, "raid_points": int(pts),
                })
        return result

    async def get_home_team_record(
        self,
        home_team: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """主体战队总体战绩（按胜者判定，按战队跨群），返回 {wins, losses, draws, total, win_rate, raid_points, total_points, attack_points, raid_success, hold, first_round}。

        胜/负/总**只统计友谊赛**（踢馆有自己的积分，见 `raid_points`）。
        踢馆口径与 get_raid_team_stats 一致：按 `(team_a = T OR team_b = T)`
        对称取数 —— 只有这样才能让守馆方也拿到分（M-02 的唯一例外）。
        末三个计数字段供 `/战队战绩` 展示，`attack_points` 供 `/排行` 图片左侧的
        战队面板（`stats.build_team_panel`）—— 都直接透传 `get_raid_team_stats`。

        ⚠️ `raid_points` 是**封顶后**的踢馆积分（= attack + min(10, 守馆加点)），
        `attack_points` 是**未封顶**的踢馆加点。面板要的是后者，别拿错。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        rows = await self._query(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN winner = %s THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN winner != %s THEN 1 ELSE 0 END) AS losses
               FROM matches m
               WHERE m.home_team = %s AND m.winner != ''
                 AND m.match_time >= %s AND m.match_time <= %s
                 AND m.kind = 'friendly'""",
            (home_team, home_team, home_team, d1, d2),
        )
        r = rows[0] if rows else {}
        total = int(r.get("total") or 0)
        wins = int(r.get("wins") or 0)
        losses = int(r.get("losses") or 0)
        wr = round(wins * 100.0 / (wins + losses), 1) if (wins + losses) else 0.0
        raid = await self.get_raid_team_stats(home_team, date_from, date_to)
        return {
            "wins": wins, "losses": losses, "draws": 0, "total": total, "win_rate": wr,
            "raid_points": raid["raid_points"],
            "total_points": stats.total_points(stats._points(wins, losses), raid["raid_points"]),
            "attack_points": raid["attack_points"],
            "raid_success": raid["raid_success"],
            "hold": raid["hold"],
            "first_round": raid["first_round"],
        }

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
               WHERE m.home_team = %s AND d.player_a = %s AND m.match_time >= %s
                 AND m.kind = 'friendly'{date_clause}
               GROUP BY m.match_time
               UNION ALL
               SELECT m.match_time,
                      SUM(CASE WHEN d.result='B' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN d.result='A' THEN 1 ELSE 0 END)
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.home_team = %s AND d.player_b = %s AND m.match_time >= %s
                 AND m.kind = 'friendly'{date_clause}
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
                 AND m.kind = 'friendly'
                 AND d.player_a_team = %s AND d.player_a IN ({ph}){date_clause}
               GROUP BY m.match_time
               UNION ALL
               SELECT m.match_time,
                      SUM(CASE WHEN d.result='B' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN d.result='A' THEN 1 ELSE 0 END)
               FROM duels d JOIN matches m ON d.match_id = m.id
               WHERE m.home_team = %s AND m.match_time >= %s
                 AND m.kind = 'friendly'
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
                   WHERE m.home_team = %s AND m.team_a = %s AND m.match_time >= %s
                     AND m.kind = 'friendly'{date_clause}
                   UNION ALL
                   SELECT m.match_time, m.team_b,
                          CASE WHEN s.b_wins > s.a_wins THEN 1 ELSE 0 END,
                          CASE WHEN s.b_wins < s.a_wins THEN 1 ELSE 0 END
                   FROM matches m JOIN match_scores s ON m.id = s.match_id
                   WHERE m.home_team = %s AND m.team_b = %s AND m.match_time >= %s
                     AND m.kind = 'friendly'{date_clause}
               )
               SELECT date, SUM(win) wins, SUM(loss) losses
               FROM team_sides GROUP BY date ORDER BY date""",
            tuple(params),
        )
        return [(str(r["date"]), int(r["wins"] or 0), int(r["losses"] or 0)) for r in rows]

    # ---------- 月度结算（settlement） ----------

    async def set_settlement(
        self, home_team: str, month: str, entries: list[dict]
    ) -> None:
        """覆盖写入某队某月的结算结果。

        `month` 是 `'YYYY-MM'` 键（见 `battle_report_parser.settle_month_range`），
        `entries` 每项含 rank_no / player / total_points / bonus。

        **先 DELETE 再 INSERT**：`/结算` 重跑一次就是重算，旧奖金必须作废（用户
        确认的口径）。只 DELETE 本队本月，不动别队别月。
        """
        assert self.pool is not None
        now = int(time.time())
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM settlements WHERE home_team = %s AND month = %s",
                        (home_team, month),
                    )
                    for e in entries:
                        await cur.execute(
                            """INSERT INTO settlements
                               (home_team, month, player_name, rank_no,
                                total_points, bonus, created_at)
                               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                            (
                                home_team,
                                month,
                                e["player"],
                                int(e["rank_no"]),
                                e.get("total_points", 0) or 0,
                                e.get("bonus", 0) or 0,
                                now,
                            ),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def clear_settlement(self, home_team: str, month: str) -> int:
        """删掉某队某月的结算记录，把该月恢复成**未结算**。返回删掉的行数。

        `set_settlement` 里那句 DELETE 是「重算前先清空」的中间步骤；这个方法是
        把它单独暴露出来当**终点**用（`/重置结算`）：删完不补 INSERT，于是
        `/排行` 回到 14 列、`/结算` 又可以被当成「该月第一次结算」重新发公告。

        返回 0 = 该月本来就没结算过，调用方据此回「无需重置」（不报错）。
        只删本队本月，别队别月不受影响。
        """
        return await self._execute(
            "DELETE FROM settlements WHERE home_team = %s AND month = %s",
            (home_team, month),
        )

    async def get_settlement(self, home_team: str, month: str) -> dict[str, float]:
        """某队某月的结算结果 `{玩家名: 奖金}`。

        没结算过时返回**空 dict** —— 调用方（`/排行`）据此决定要不要显示「奖金」
        列：空 = 这个月还没结算 = 不加那一列。
        """
        rows = await self._query(
            """SELECT player_name, bonus FROM settlements
               WHERE home_team = %s AND month = %s""",
            (home_team, month),
        )
        return {r["player_name"]: float(r["bonus"] or 0) for r in rows}

    async def get_settlement_entries(self, home_team: str, month: str) -> list[dict]:
        """某队某月已结算结果的**明细**（按名次升序），供只读查询用。

        没结算过时返回**空列表**。

        与 `get_settlement` 的区别：那个只给 `{玩家名: 奖金}`（`/排行` 拿来判断
        要不要加「奖金」列），这个给的是完整回执所需的
        rank_no / player / total_points / bonus 四个字段。

        ⚠️ `total_points` 落库时被 `DECIMAL(10,2)` 截到 2 位小数，所以回显值
        可能与实时榜单的 `:g` 值差在小数点后第 3 位 —— 这是**快照**，以库里的为准。
        """
        rows = await self._query(
            """SELECT rank_no, player_name, total_points, bonus FROM settlements
               WHERE home_team = %s AND month = %s ORDER BY rank_no""",
            (home_team, month),
        )
        return [
            {
                "rank_no": int(r["rank_no"]),
                "player": r["player_name"],
                "total_points": float(r["total_points"] or 0),
                "bonus": float(r["bonus"] or 0),
            }
            for r in rows
        ]

    # ---------- 踢馆赛（raid） ----------

    async def get_raid_matches(
        self, team: str, date_from: str | None = None, date_to: str | None = None
    ) -> list[dict]:
        """某战队参与的踢馆报（含对局），返回按战报聚合的 dict 列表。

        ⚠️ **按 `team_a`/`team_b` 对称取数，不看 `home_team`** —— 这是 M-02
        「统计按 `home_team` 跨群聚合」的**唯一例外**。踢馆报的 `home_team` 只是
        提交方（通常是踢馆方），照 M-02 取数的话守馆方永远拿不到守馆积分。
        跨群的同一支战队因此能看到彼此的踢馆记录，与友谊赛一致。

        玩家名按 M-03 解析（`COALESCE(u.name, 参赛ID)`）。占位行（防守方为
        `规则`）照常返回，由 `stats.compute_raid_match` 排除。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        rows = await self._query(
            """SELECT m.id AS match_id, m.team_a, m.team_b, m.winner, m.rule,
                      m.match_time,
                      d.seq, d.round_no, d.score_a, d.score_b,
                      d.player_a, d.player_b, d.owner,
                      COALESCE(ua.name, d.player_a) AS resolved_a,
                      COALESCE(ub.name, d.player_b) AS resolved_b
               FROM matches m
               LEFT JOIN duels d ON d.match_id = m.id
               LEFT JOIN player_ids pia
                      ON pia.home_team = m.team_a AND pia.player_name = d.player_a
               LEFT JOIN users ua ON ua.home_team = pia.home_team AND ua.id = pia.user_id
               LEFT JOIN player_ids pib
                      ON pib.home_team = m.team_b AND pib.player_name = d.player_b
               LEFT JOIN users ub ON ub.home_team = pib.home_team AND ub.id = pib.user_id
               WHERE m.kind = %s AND (m.team_a = %s OR m.team_b = %s)
                 AND m.match_time >= %s AND m.match_time <= %s
               ORDER BY m.id, d.seq, d.id""",
            (KIND_RAID, team, team, d1, d2),
        )
        matches: list[dict] = []
        current: dict | None = None
        for r in rows:
            if current is None or current["match_id"] != r["match_id"]:
                current = {
                    "match_id": r["match_id"],
                    "team_a": r["team_a"],
                    "team_b": r["team_b"],
                    "winner": r["winner"] or "",
                    "rule": r["rule"] or "",
                    "match_time": str(r["match_time"]),
                    "duels": [],
                }
                matches.append(current)
            if r["player_a"] is None:  # LEFT JOIN：该战报没有对局
                continue
            current["duels"].append({
                "seq": int(r["seq"] or 0),
                "round_no": int(r["round_no"] or 0),
                "score_a": int(r["score_a"]),
                "score_b": int(r["score_b"]),
                "player_a": r["player_a"],
                "player_b": r["player_b"],
                "owner": bool(r["owner"]),
                "resolved_a": r["resolved_a"],
                "resolved_b": r["resolved_b"],
            })
        return matches

    async def _raid_infos(
        self, team: str, date_from: str | None = None, date_to: str | None = None
    ) -> list[dict]:
        """区间内某战队参与的全部踢馆的判定结果（`stats.compute_raid_match` 输出）。

        回填 `match_id`，供个人侧「同一场只算一次」去重（一名用户的多个参赛ID
        可能同时出现在同一场踢馆里）。
        """
        infos = []
        for m in await self.get_raid_matches(team, date_from, date_to):
            info = stats.compute_raid_match(m["duels"], m["winner"], m["team_a"], m["team_b"])
            info["match_id"] = m["match_id"]
            infos.append(info)
        return infos

    async def _raid_views_by_player(
        self, team: str, date_from: str | None, date_to: str | None, side_team: str | None
    ) -> dict[str, list[dict]]:
        """{已解析玩家名: [视角记录]}。

        「取哪些战报」由 `team` 决定（对称取数，见 `get_raid_matches`）；
        「取哪一侧的人」由 `side_team` 决定，与前者**互相独立**：

        - `side_team=None` → **不按阵营过滤**，两侧的人都收
          （对应 `get_player_ranking(team=None)` 的「不按队伍过滤」口径）
        - `side_team="KC"` → 只收 KC 自己这一侧（踢馆方记踢馆成功、终结踢馆者的
          那名防守者记守馆成功），避免跨队同名互相串分

        ⚠️ 别把 `side_team` 缺省写成 `team` —— 那会让 `/排行 全部` 悄悄退化成
        「只看本队」，与它同一次查询里友谊口径的选手集合对不上。
        """
        per_player: dict[str, list[dict]] = {}
        for info in await self._raid_infos(team, date_from, date_to):
            for name, view in stats.raid_player_views(info):
                if side_team is not None and view.get("team") != side_team:
                    continue
                per_player.setdefault(name, []).append(view)
        return per_player

    async def get_raid_team_stats(
        self, team: str, date_from: str | None = None, date_to: str | None = None
    ) -> dict:
        """战队级踢馆汇总（`stats.aggregate_raid` 的返回值：4 个计数 + 徽章 + 积分）。

        4 个计数字段 = 踢馆成功 / 踢馆成功含馆主 / 守馆成功 / 守馆首轮，
        另有 `shutdown`（SHUT DOWN 次数）与 `badge`（无人守馆次数）。
        """
        views = []
        for info in await self._raid_infos(team, date_from, date_to):
            v = stats.raid_team_view(info, team)
            if v:
                views.append(v)
        return stats.aggregate_raid(views)

    async def get_raid_player_stats(
        self,
        team: str,
        date_from: str | None = None,
        date_to: str | None = None,
        side_team: str | None = None,
    ) -> dict[str, dict]:
        """个人级踢馆汇总，返回 {已解析玩家名: aggregate_raid 结果}。

        **同一套规则按人重算**（含当月重复踢同一队取最高、守馆封顶 10），不是
        把场次直接相加 —— 既定口径。

        战报集合是 `team` 参与过的全部踢馆（对称取数）；`side_team` 再按阵营
        过滤出场者：踢馆方记踢馆成功与加点，终结踢馆者的那名防守者记守馆
        成功/首轮/SHUT DOWN。**`side_team=None` 表示不按阵营过滤**（两侧都收，
        与 `get_player_ranking(team=None)` 同口径）；要只看某一侧就显式传进来。
        """
        per_player = await self._raid_views_by_player(team, date_from, date_to, side_team)
        return {name: stats.aggregate_raid(views) for name, views in per_player.items()}

    async def get_raid_stats_for_players(
        self,
        team: str,
        players: list[str],
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """把一名用户绑定的多个参赛ID合并为**一份**踢馆汇总（`aggregate_raid` 结果）。

        合并后再套规则，而不是把各ID的汇总相加：同一个人挂多个ID时，「当月重复
        踢穿同一战队取最高」「守馆封顶 10」都要在合并后的集合上算，否则会重复
        计分。同一场踢馆里出现该用户的多个ID时按 `(match_id, 阵营)` 去重。
        """
        if not players:
            return stats.aggregate_raid([])
        per_player = await self._raid_views_by_player(team, date_from, date_to, team)
        resolved = await self._resolve_player_names(team, players)
        views: list[dict] = []
        seen: set[tuple] = set()
        for p in players:
            for v in per_player.get(resolved[p], []):
                key = (v.get("match_id"), v.get("team"))
                if key in seen:
                    continue
                seen.add(key)
                views.append(v)
        return stats.aggregate_raid(views)

    async def _resolve_player_names(self, home_team: str, players: list[str]) -> dict[str, str]:
        """参赛ID → 已解析名（M-03：`COALESCE(u.name, 参赛ID)`），未收录的原样返回。

        踢馆侧按已解析名聚合（与排行行同名），而调用方拿到的是参赛ID，查分前
        必须先走这一步，否则用户改过名就对不上号。
        """
        if not players:
            return {}
        ph = ",".join(["%s"] * len(players))
        rows = await self._query(
            f"""SELECT p.player_name, COALESCE(u.name, p.player_name) AS resolved
                FROM player_ids p LEFT JOIN users u ON u.id = p.user_id
                WHERE p.home_team = %s AND p.player_name IN ({ph})""",
            (home_team, *players),
        )
        mapping = {r["player_name"]: r["resolved"] for r in rows}
        return {p: mapping.get(p, p) for p in players}

    async def get_raid_points_by_opponent(
        self, team: str, date_from: str | None = None, date_to: str | None = None
    ) -> dict[str, int]:
        """对战各对手的**踢馆加点**（踢馆方视角，按对手队取最高那场），供 `/排行 队伍` 展示。

        只含踢馆加点，不含守馆加点 —— 守馆是月度封顶的总额，摊不到单个对手头上。
        """
        best: dict[str, int] = {}
        for info in await self._raid_infos(team, date_from, date_to):
            v = stats.raid_team_view(info, team)
            if v and v["raid_success"]:
                opp = str(v["opponent"])
                best[opp] = max(best.get(opp, 0), int(v["attack_points"]))
        return best

    async def get_export_rows(self, home_team: str, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        """导出某战队战报（matches + duels 联表），按战队跨群，可按日期过滤。

        **不按 `kind` 过滤**：踢馆战报同样要能导出（`m.kind` 随行返回，
        调用方按需区分）。
        """
        d1, d2 = self._date_bounds(date_from, date_to)
        date_clause = " AND m.match_time >= %s AND m.match_time <= %s" if (date_from or date_to) else ""
        params = [home_team]
        if date_from or date_to:
            params += [d1, d2]
        return await self._query(
            f"""SELECT m.id AS match_id, m.group_id, m.team_a, m.team_b,
                      m.match_time, m.rule, m.location, m.kind,
                      d.round_no, d.player_a, d.score_a, d.player_b, d.score_b, d.result,
                      d.a_sub, d.b_sub, d.ruled, d.owner
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
                      m.kind,
                      d.seq, d.round_no, d.player_a, d.score_a, d.player_b, d.score_b,
                      d.a_sub, d.b_sub, d.ruled, d.owner
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
                    "kind": r["kind"] or KIND_FRIENDLY,
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
                    "owner": bool(r["owner"]),
                })
        return reports
