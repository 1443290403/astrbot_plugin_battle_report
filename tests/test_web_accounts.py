"""独立凭据、QQ取码、会话撤销与落盘失败；不接触真实QQ或生产配置。"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from web_accounts import WebAccounts,WebAuthError,password_matches


@pytest.fixture
def auth(tmp_path):
    clock=[1000.0]
    teams=AsyncMock(return_value=['a队'])
    return WebAccounts(tmp_path/'accounts.json',teams,clock=lambda:clock[0]),clock,teams


def test_password_storage_and_change_revoke_existing_password_sessions(auth):
    service,_,_=auth
    async def run():
        account=await service.create('team_a','InitialPass8','20000001')
        raw=service.path.read_text()
        assert 'InitialPass8' not in raw and service.path.stat().st_mode & 0o777==0o600
        assert not {'hash','revision'}.intersection(account)
        first=await service.login('TEAM_A','InitialPass8','client-1')
        second=await service.login('team_a','InitialPass8','client-2')
        assert first not in repr(service.sessions)
        assert (await service.authenticate(first))['teams']==['a队']
        with pytest.raises(WebAuthError,match='原密码'):await service.change_password(first,'wrong','ChangedPass9')
        changed=await service.change_password(first,'InitialPass8','ChangedPass9')
        assert (await service.authenticate(changed))['qq']=='20000001'
        for token in [first,second]:
            with pytest.raises(WebAuthError) as error:await service.authenticate(token)
            assert error.value.status==401
        with pytest.raises(WebAuthError):await service.login('team_a','InitialPass8','client-3')
        restored=WebAccounts(service.path,service.teams)
        assert await restored.login('team_a','ChangedPass9','client-4')
        with pytest.raises(WebAuthError):await restored.authenticate(changed)
    asyncio.run(run())


def test_code_is_single_use_latest_only_expires_and_does_not_need_password_account(auth):
    service,clock,_=auth
    async def run():
        old=await service.issue_code('20000001')
        assert old not in repr(service.codes) and not service.path.exists()
        with pytest.raises(WebAuthError) as error:await service.issue_code('20000001')
        assert error.value.status==429
        clock[0]+=31
        code=await service.issue_code('20000001')
        with pytest.raises(WebAuthError):await service.login_code(old,'one')
        attempts=await asyncio.gather(service.login_code(code,'two'),service.login_code(code,'three'),return_exceptions=True)
        assert sum(isinstance(x,str) for x in attempts)==1
        token=next(x for x in attempts if isinstance(x,str))
        assert (await service.authenticate(token))['password_login'] is False
        with pytest.raises(WebAuthError) as error:await service.issue_code('20000001')
        assert error.value.status==429
        clock[0]+=31
        expired=await service.issue_code('20000001');clock[0]+=300
        with pytest.raises(WebAuthError):await service.login_code(expired,'four')
        service.logout(token)
        with pytest.raises(WebAuthError):await service.authenticate(token)
    asyncio.run(run())


def test_password_disable_delete_and_membership_revocation_have_distinct_effects(auth):
    service,_,teams=auth
    async def run():
        account=await service.create('team_a','InitialPass8','20000001')
        password=await service.login('team_a','InitialPass8','one')
        code=await service.issue_code('20000001');qq=await service.login_code(code,'two')
        await service.update(account['id'],dict(action='disable'))
        with pytest.raises(WebAuthError):await service.authenticate(password)
        with pytest.raises(WebAuthError):await service.login('team_a','InitialPass8','three')
        assert (await service.authenticate(qq))['qq']=='20000001'
        teams.return_value=['b队']
        assert (await service.authenticate(qq))['teams']==['b队']
        teams.return_value=[]
        with pytest.raises(WebAuthError) as error:await service.authenticate(qq)
        assert error.value.status==401
        teams.return_value=['a队']
        with pytest.raises(WebAuthError):await service.authenticate(qq)
        await service.update(account['id'],dict(action='delete'))
        assert not service.list_accounts()
    asyncio.run(run())


def test_account_validation_rate_limit_and_expiry(auth):
    service,clock,teams=auth
    async def run():
        for args in [('bad name','InitialPass8','20000001'),('name','short','20000001'),('name','InitialPass8','bad')]:
            with pytest.raises(WebAuthError):await service.create(*args)
        await service.create('valid','InitialPass8','20000001')
        with pytest.raises(WebAuthError):await service.create('VALID','InitialPass8','20000002')
        with pytest.raises(WebAuthError):await service.create('other','InitialPass8','20000001')
        teams.return_value=[]
        with pytest.raises(WebAuthError):await service.create('nomember','InitialPass8','20000003')
        teams.return_value=['a队']
        token=await service.login('valid','InitialPass8','ok')
        clock[0]+=8*3600
        with pytest.raises(WebAuthError):await service.authenticate(token)
        for _ in range(20):
            with pytest.raises(WebAuthError):await service.login_code('invalid','spam')
        with pytest.raises(WebAuthError) as error:await service.login_code('invalid','spam')
        assert error.value.status==429
        clock[0]+=300
        with pytest.raises(WebAuthError) as error:await service.login_code('invalid','spam')
        assert error.value.status==401
    asyncio.run(run())


def test_store_failure_rolls_back_account_change_and_preserves_valid_session(auth,monkeypatch):
    service,_,_=auth
    async def run():
        account=await service.create('valid','InitialPass8','20000001')
        token=await service.login('valid','InitialPass8','one')
        before=service.path.read_bytes()
        import web_accounts
        def fail(*args):raise OSError('disk full')
        monkeypatch.setattr(web_accounts.os,'replace',fail)
        with pytest.raises(OSError):await service.update(account['id'],dict(action='reset',password='ChangedPass9'))
        assert service.path.read_bytes()==before
        assert (await service.authenticate(token))['qq']=='20000001'
        assert password_matches(service.accounts[account['id']]['hash'],'InitialPass8')
        with pytest.raises(OSError):await service.create('newuser','InitialPass8','20000002')
        assert len(service.accounts)==1
    asyncio.run(run())


def test_password_change_cannot_overwrite_a_concurrent_admin_reset(auth,monkeypatch):
    service,_,_=auth
    async def run():
        import web_accounts,threading
        account=await service.create('valid','InitialPass8','20000001')
        token=await service.login('valid','InitialPass8','one')
        entered,release=threading.Event(),threading.Event()
        original=web_accounts.password_hash
        def blocked(password):
            if password=='UserChange9':entered.set();assert release.wait(5)
            return original(password)
        monkeypatch.setattr(web_accounts,'password_hash',blocked)
        change=asyncio.create_task(service.change_password(token,'InitialPass8','UserChange9'))
        assert await asyncio.to_thread(entered.wait,5)
        await service.update(account['id'],dict(action='reset',password='AdminReset9'))
        release.set()
        with pytest.raises(WebAuthError) as error:await change
        assert error.value.status==401
        assert await service.login('valid','AdminReset9','two')
    asyncio.run(run())


def test_unload_prevents_inflight_password_work_from_writing_or_issuing_sessions(auth,monkeypatch):
    service,_,_=auth
    async def run():
        import web_accounts,threading
        entered,release=threading.Event(),threading.Event()
        original=web_accounts.password_hash
        def blocked(password):
            entered.set();assert release.wait(5)
            return original(password)
        monkeypatch.setattr(web_accounts,'password_hash',blocked)
        creation=asyncio.create_task(service.create('valid','InitialPass8','20000001'))
        assert await asyncio.to_thread(entered.wait,5)
        service.close();release.set()
        with pytest.raises(WebAuthError) as error:await creation
        assert error.value.status==503
        assert not service.accounts and not service.path.exists() and not service.sessions
        with pytest.raises(WebAuthError):await service.issue_code('20000001')
    asyncio.run(run())
