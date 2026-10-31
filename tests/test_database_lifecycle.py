"""不依赖数据库服务的连接生命周期和唯一键冲突回归。"""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymysql.err import IntegrityError

from battle_report_parser import BattleReport, Duel
from database import Database, DuplicateReportError


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


def test_duplicate_conflict_does_not_borrow_second_connection():
    class Cursor:
        def __init__(self):
            self.lookup = 0

        async def execute(self, sql, params=()):
            if sql.startswith('SELECT id'):
                self.lookup += 1
            elif 'INSERT INTO matches' in sql:
                raise IntegrityError(1062, "Duplicate entry for key 'uk_matches_fingerprint'")

        async def fetchone(self):
            return None if self.lookup == 1 else (42,)

    cur = Cursor()
    conn = SimpleNamespace(cursor=lambda *a: Context(cur), begin=AsyncMock(), rollback=AsyncMock())

    class Pool:
        def __init__(self):
            self.borrowed = False

        def acquire(self):
            assert not self.borrowed, '连接池已满时不能继续借第二条连接'
            self.borrowed = True
            return Context(conn)

    db = Database('localhost', 3306, 'root', '', 'test')
    db.pool = Pool()
    report = BattleReport(team_a='A', team_b='B', match_time='2026-08-01', duels=[Duel(1, '甲', 2, '乙', 0)])
    with pytest.raises(DuplicateReportError) as raised:
        asyncio.run(db.insert_report(report, 'A', 'A'))
    assert raised.value.match_id == 42
    conn.rollback.assert_awaited_once()


def test_initialization_failure_closes_created_pool(monkeypatch):
    import database
    pool = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
    monkeypatch.setattr(database.aiomysql, 'create_pool', AsyncMock(return_value=pool))
    db = Database('localhost', 3306, 'root', '', 'test')
    db._ensure_database = AsyncMock()
    db._init_schema = AsyncMock(side_effect=RuntimeError('migration failed'))
    with pytest.raises(RuntimeError, match='migration failed'):
        asyncio.run(db.initialize())
    assert db.pool is None
    pool.wait_closed.assert_awaited_once()


@pytest.mark.parametrize('found', [(200,), None])
@pytest.mark.parametrize('sent', [True, False])
def test_export_mark_cutoff_transaction_scopes_team(found, sent):
    statements = []
    async def execute(sql, params=()):
        statements.append((sql, params))
    cur = SimpleNamespace(execute=execute, fetchone=AsyncMock(return_value=found), rowcount=170)
    conn = SimpleNamespace(cursor=lambda *a: Context(cur), begin=AsyncMock(),
                           commit=AsyncMock(), rollback=AsyncMock())
    db = Database('localhost', 3306, 'root', '', 'test')
    db.pool = SimpleNamespace(acquire=lambda: Context(conn))
    if found:
        assert asyncio.run(db.set_reports_export_sent_through('a队', 200, sent)) == 170
        conn.commit.assert_awaited_once()
        assert statements[1][1] == (int(sent), 'a队', 200)
        assert 'WHERE home_team = %s AND id <= %s' in statements[1][0]
    else:
        with pytest.raises(ValueError, match='不属于本战队'):
            asyncio.run(db.set_reports_export_sent_through('a队', 200, sent))
        conn.rollback.assert_awaited_once()
        conn.commit.assert_not_awaited()
        assert len(statements) == 1
    assert statements[0][1] == ('a队', 200)
    assert 'id = %s FOR UPDATE' in statements[0][0]



def test_export_mark_cancellation_rolls_back():
    cur = SimpleNamespace(execute=AsyncMock(side_effect=asyncio.CancelledError()))
    conn = SimpleNamespace(cursor=lambda *a: Context(cur), begin=AsyncMock(),
                           commit=AsyncMock(), rollback=AsyncMock())
    db = Database('localhost', 3306, 'root', '', 'test')
    db.pool = SimpleNamespace(acquire=lambda: Context(conn))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(db.set_reports_export_sent_through('a队', 1, False))
    conn.rollback.assert_awaited_once()
    conn.commit.assert_not_awaited()


@pytest.mark.parametrize('column_present', [False, True])
def test_export_sent_migration_can_replay_after_column_was_added(column_present):
    statements = []
    async def execute(sql, params=()):
        statements.append((sql, params))
    async def fetchone():
        return (18,) if 'MAX(version)' in statements[-1][0] else (int(column_present),)
    cur = SimpleNamespace(execute=execute, fetchone=fetchone)
    conn = SimpleNamespace(cursor=lambda *a: Context(cur))
    db = Database('localhost', 3306, 'root', '', 'test')
    db.pool = SimpleNamespace(acquire=lambda: Context(conn))
    asyncio.run(db._init_schema())
    alter = [sql for sql, _ in statements if 'ALTER TABLE matches ADD COLUMN export_sent' in sql]
    assert len(alter) == (0 if column_present else 1)
    assert statements[-1][1] == (19,)
    assert any('information_schema.columns' in sql.lower() and params == ('matches', 'export_sent')
               for sql, params in statements)


def test_export_queries_return_persisted_status_in_both_paths():
    db = Database('localhost', 3306, 'root', '', 'test')
    row = dict(match_id=1, team_a='a队', team_b='b队', match_time='2026-10-31',
               rule='人头赛', location='100', winner='a队', home_team='a队',
               raw_text='', submitted_by='10001', submitted_name='云玩家',
               kind='friendly', export_sent=1, player_a=None)
    db._query = AsyncMock(return_value=[row])
    assert asyncio.run(db.get_reports_for_export('a队'))[0]['export_sent'] is True
    assert asyncio.run(db.get_export_rows('a队'))[0]['export_sent'] == 1
    assert all('m.export_sent' in call.args[0] for call in db._query.await_args_list)


def test_each_teams_export_mark_is_independent_in_executed_sql():
    """实际执行标记 SQL，验证 A 队标记/恢复均不改 B 队。

    SQLite 只替换占位符并省略 MySQL 行锁；锁及迁移仍由专门测试覆盖。
    """
    raw = sqlite3.connect(':memory:')
    raw.execute('CREATE TABLE matches (id INTEGER PRIMARY KEY, home_team TEXT, export_sent INTEGER)')
    raw.executemany('INSERT INTO matches VALUES (?, ?, ?)', [(i, 'a队' if i % 2 else 'b队', 0) for i in range(1, 245)])
    raw.commit()
    fail_after_update = [False]

    class Cursor:
        async def execute(self, sql, params=()):
            self.cursor = raw.execute(sql.replace('%s', '?').replace(' FOR UPDATE', ''), params)
            self.rowcount = self.cursor.rowcount
            if sql.startswith('UPDATE') and fail_after_update[0]:
                fail_after_update[0] = False
                raise RuntimeError('模拟范围更新后的事务异常')

        async def fetchone(self):
            return self.cursor.fetchone()

    class Connection:
        def cursor(self):
            return Context(Cursor())

        async def begin(self):
            raw.execute('BEGIN')

        async def commit(self):
            raw.commit()

        async def rollback(self):
            raw.rollback()

    db = Database('localhost', 3306, 'root', '', 'test')
    db.pool = SimpleNamespace(acquire=lambda: Context(Connection()))

    def states():
        return dict(raw.execute('SELECT id, export_sent FROM matches').fetchall())

    async def run():
        # 超过 100 份，边界本身包含在内，较新战报和其他队均保持不变。
        await db.set_reports_export_sent_through('a队', 241, True)
        assert states() == {i: int(i % 2 == 1 and i <= 241) for i in range(1, 245)}
        with pytest.raises(ValueError):
            await db.set_reports_export_sent_through('b队', 241, True)
        with pytest.raises(ValueError):
            await db.set_reports_export_sent_through('a队', 999, False)
        assert states() == {i: int(i % 2 == 1 and i <= 241) for i in range(1, 245)}
        fail_after_update[0] = True
        with pytest.raises(RuntimeError):
            await db.set_reports_export_sent_through('a队', 121, False)
        assert states() == {i: int(i % 2 == 1 and i <= 241) for i in range(1, 245)}
        await db.set_reports_export_sent_through('b队', 242, True)
        await db.set_reports_export_sent_through('a队', 121, False)
        expected = {i: int((i % 2 == 0 and i <= 242) or (i % 2 == 1 and 121 < i <= 241))
                    for i in range(1, 245)}
        assert states() == expected
        await db.set_reports_export_sent_through('a队', 121, False)
        assert states() == expected

    try:
        asyncio.run(run())
    finally:
        raw.close()
