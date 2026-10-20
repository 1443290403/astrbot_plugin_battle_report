"""不依赖数据库服务的连接生命周期和唯一键冲突回归。"""
import asyncio
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
