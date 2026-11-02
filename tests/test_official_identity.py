"""SQLite 事务适配执行真实身份 SQL；不连接生产数据库或 QQ。"""
import asyncio
import hashlib
import sqlite3
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import database


@pytest.fixture
def identity_db(monkeypatch):
    raw = sqlite3.connect(':memory:', isolation_level=None)
    raw.row_factory = sqlite3.Row
    raw.executescript('''
      CREATE TABLE users(id INTEGER PRIMARY KEY, home_team TEXT, name TEXT, qq_id TEXT, created_at INT);
      CREATE TABLE player_ids(id INTEGER PRIMARY KEY, home_team TEXT, player_name TEXT, user_id INT);
      CREATE TABLE official_user_accounts(appid TEXT, openid TEXT, user_id INT, qq_id TEXT, created_at INT,
        PRIMARY KEY(appid,openid), UNIQUE(appid,qq_id));
      CREATE TABLE official_link_requests(token_hash TEXT PRIMARY KEY, home_team TEXT, appid TEXT,
        openid TEXT, user_id INT, owner_qq TEXT, expires_at INT, created_at INT);
      INSERT INTO users VALUES(1,'a队','云玩家','10001',0),(2,'a队','萌新','20002',0),
        (3,'b队','云玩家','10001',0),(4,'a队','遗老','',0);
      INSERT INTO player_ids VALUES(1,'a队','示例 玩家',1),(2,'a队','测试选手',2),(3,'a队','未归属ID',NULL);
    ''')
    clock = [1000]
    monkeypatch.setattr(database.time, 'time', lambda: clock[0])
    db = database.Database('localhost', 3306, 'test', '', 'test')
    class Cursor:
        async def execute(self, sql, params=()):
            self.cursor = raw.execute(sql.replace('%s', '?').replace(' FOR UPDATE', ''), params)
            self.rowcount = self.cursor.rowcount
        async def fetchone(self):
            row = self.cursor.fetchone()
            return dict(row) if row else None
        async def fetchall(self):
            return [dict(row) for row in self.cursor.fetchall()]
    @asynccontextmanager
    async def transaction(home):
        raw.execute('BEGIN')
        try:
            yield Cursor()
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
    async def query(sql, params=()):
        return [dict(row) for row in raw.execute(sql.replace('%s', '?'), params).fetchall()]
    db._identity_cursor = transaction
    db._query = query
    yield db, raw, clock
    raw.close()


def test_request_only_stores_hashed_code_and_confirmation_preserves_original_user(identity_db):
    db, raw, _ = identity_db
    async def scenario():
        before = [tuple(row) for row in raw.execute('SELECT * FROM users')]
        request = await db.request_official_link('a队', 'app', 'OpenID', '示例 玩家')
        assert not request['linked']
        assert await db.get_official_identity('app', 'OpenID') is None
        stored = dict(raw.execute('SELECT * FROM official_link_requests').fetchone())
        assert stored['token_hash'] == hashlib.sha256(request['code'].encode()).hexdigest()
        assert request['code'] not in str(stored)
        assert await db.confirm_official_link(request['code'], '10001') == 'ok'
        assert await db.confirm_official_link(request['code'], '10001') == 'invalid'
        identity = await db.get_official_identity('app', 'OpenID')
        assert identity['qq_id'] == '10001' and identity['user_id'] == 1
        assert await db.get_user_by_qq('a队', identity['qq_id']) == await db.get_user_by_qq('a队', '10001')
        assert [tuple(row) for row in raw.execute('SELECT * FROM users')] == before
        assert await db.request_official_link('a队', 'app', 'OpenID', '云玩家') == dict(linked=True, user_id=1, name='云玩家', qq_id='10001')
    asyncio.run(scenario())


def test_another_qq_cannot_confirm_even_with_exact_code(identity_db):
    db, raw, _ = identity_db
    async def scenario():
        request = await db.request_official_link('a队', 'app', 'OpenID', '云玩家')
        assert await db.confirm_official_link(request['code'], '20002') == 'denied'
        assert not raw.execute('SELECT * FROM official_user_accounts').fetchall()
        assert await db.confirm_official_link(request['code'], '10001') == 'ok'
    asyncio.run(scenario())


def test_expiry_and_reissued_codes_cannot_grant_identity(identity_db):
    db, _, clock = identity_db
    async def scenario():
        old = await db.request_official_link('a队', 'app', 'OpenID', '云玩家')
        new = await db.request_official_link('a队', 'app', 'OpenID', '云玩家')
        assert await db.confirm_official_link(old['code'], '10001') == 'invalid'
        clock[0] = 1600
        assert await db.confirm_official_link(new['code'], '10001') == 'invalid'
        assert await db.get_official_identity('app', 'OpenID') is None
    asyncio.run(scenario())


def test_owner_changed_during_confirmation_and_after_link_immediately_loses_identity(identity_db):
    db, raw, _ = identity_db
    async def scenario():
        request = await db.request_official_link('a队', 'app', 'OpenID', '云玩家')
        raw.execute('UPDATE users SET qq_id=? WHERE id=1', ('30003',))
        assert await db.confirm_official_link(request['code'], '10001') == 'denied'
        assert await db.confirm_official_link(request['code'], '30003') == 'denied'
        raw.execute('UPDATE users SET qq_id=? WHERE id=1', ('10001',))
        assert await db.confirm_official_link(request['code'], '10001') == 'ok'
        raw.execute('UPDATE users SET qq_id=? WHERE id=1', ('30003',))
        assert await db.get_official_identity('app', 'OpenID') is None
        with pytest.raises(ValueError):
            await db.request_official_link('b队', 'app', 'OpenID', '云玩家')
    asyncio.run(scenario())


def test_appid_and_case_isolation_and_same_qq_across_teams(identity_db):
    db, _, _ = identity_db
    async def scenario():
        request = await db.request_official_link('a队', 'app', 'OpenID', '云玩家')
        await db.confirm_official_link(request['code'], '10001')
        assert await db.get_official_identity('other-app', 'OpenID') is None
        assert await db.get_official_identity('app', 'openid') is None
        identity = await db.get_official_identity('app', 'OpenID')
        other_team = await db.get_user_by_qq('b队', identity['qq_id'])
        assert other_team['id'] == 3
        assert (await db.request_official_link('b队', 'app', 'OpenID', '云玩家'))['linked']
    asyncio.run(scenario())


def test_existing_links_cannot_be_overwritten_by_a_different_qq_or_openid(identity_db):
    db, _, _ = identity_db
    async def scenario():
        request = await db.request_official_link('a队', 'app', 'OpenID', '云玩家')
        await db.confirm_official_link(request['code'], '10001')
        with pytest.raises(ValueError, match='其他账号'):
            await db.request_official_link('a队', 'app', 'OpenID', '萌新')
        with pytest.raises(ValueError, match='另一个官方账号'):
            await db.request_official_link('a队', 'app', 'other-openid', '云玩家')
        assert (await db.get_official_identity('app', 'OpenID'))['qq_id'] == '10001'
    asyncio.run(scenario())


def test_revoke_removes_active_identity_and_pending_requests_without_deleting_users(identity_db):
    db, raw, _ = identity_db
    async def scenario():
        request = await db.request_official_link('a队', 'app', 'OpenID', '云玩家')
        await db.confirm_official_link(request['code'], '10001')
        pending = await db.request_official_link('a队', 'other-app', 'other-openid', '云玩家')
        assert await db.revoke_official_links('10001') == 1
        assert await db.get_official_identity('app', 'OpenID') is None
        assert await db.confirm_official_link(pending['code'], '10001') == 'invalid'
        assert len(raw.execute('SELECT * FROM users').fetchall()) == 4
        assert len(raw.execute('SELECT * FROM player_ids').fetchall()) == 3
    asyncio.run(scenario())


@pytest.mark.parametrize('name', ['未归属ID', '不存在', '遗老'])
def test_unknown_or_unclaimed_role_cannot_grant_permissions(identity_db, name):
    db, raw, _ = identity_db
    with pytest.raises(ValueError):
        asyncio.run(db.request_official_link('a队', 'app', 'OpenID', name))
    assert not raw.execute('SELECT * FROM official_user_accounts').fetchall()


def test_identity_insert_failure_rolls_back_and_keeps_confirmation_request(identity_db):
    db, raw, _ = identity_db
    async def scenario():
        request = await db.request_official_link('a队', 'app', 'OpenID', '云玩家')
        raw.execute("CREATE TRIGGER fail_link BEFORE INSERT ON official_user_accounts BEGIN SELECT RAISE(ABORT,'insert failed'); END")
        with pytest.raises(sqlite3.IntegrityError, match='insert failed'):
            await db.confirm_official_link(request['code'], '10001')
        assert not raw.execute('SELECT * FROM official_user_accounts').fetchall()
        assert raw.execute('SELECT * FROM official_link_requests').fetchone()
        raw.execute('DROP TRIGGER fail_link')
        assert await db.confirm_official_link(request['code'], '10001') == 'ok'
    asyncio.run(scenario())


def test_unconfigured_official_group_can_locate_one_original_account_but_not_an_ambiguous_name(identity_db):
    db, raw, _ = identity_db
    async def scenario():
        assert await db.find_official_link_home('云玩家') == 'a队'  # 两队角色均属于同一原 QQ。
        assert await db.find_official_link_home('示例 玩家') == 'a队'
        raw.execute("INSERT INTO users VALUES(5,'c队','云玩家','30003',0)")
        with pytest.raises(ValueError, match='多个原账号'):
            await db.find_official_link_home('云玩家')
    asyncio.run(scenario())


def test_role_rename_does_not_change_the_account_being_linked(identity_db):
    db, raw, _ = identity_db
    async def scenario():
        request = await db.request_official_link('a队', 'app', 'OpenID', '示例 玩家')
        raw.execute("UPDATE users SET name='新名字' WHERE id=1")
        assert await db.confirm_official_link(request['code'], '10001') == 'ok'
        identity = await db.get_official_identity('app', 'OpenID')
        assert identity['user_id'] == 1 and identity['name'] == '新名字'
        raw.execute("UPDATE users SET name='再次改名' WHERE id=1")
        assert (await db.get_official_identity('app', 'OpenID'))['name'] == '再次改名'
    asyncio.run(scenario())
