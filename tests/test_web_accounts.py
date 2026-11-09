"""独立凭据、QQ取码、会话撤销与落盘失败；不接触真实QQ或生产配置。"""
import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from web_accounts import WebAccounts,WebAuthError,password_matches,SESSION_TTL


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


def test_short_codes_do_not_collide_or_cross_qq_accounts(auth,monkeypatch):
    import web_accounts
    service,clock,_=auth
    choices=iter('ABCDEFGH23456789ABCD2345ABCD2345EFGH6789ABCD2345EFGH6789JKLM2345')
    monkeypatch.setattr(web_accounts.secrets,'choice',lambda _:next(choices))
    async def run():
        first=await service.issue_code('20000001')
        second=await service.issue_code('20000002')
        assert first!=second and len(service.codes)==2
        clock[0]+=31
        replacement=await service.issue_code('20000001')
        assert replacement not in (first,second) and len(service.codes)==2
        with pytest.raises(WebAuthError):await service.login_code(first,'old')
        one=await service.login_code(replacement,'one')
        two=await service.login_code(second,'two')
        assert (await service.authenticate(one))['qq']=='20000001'
        assert (await service.authenticate(two))['qq']=='20000002'
    asyncio.run(run())


def test_short_code_generation_failure_keeps_previous_code_without_starting_cooldown(auth,monkeypatch):
    import web_accounts
    service,clock,_=auth
    async def run():
        previous=await service.issue_code('20000001')
        before=service.codes.copy()
        clock[0]+=31
        monkeypatch.setattr(web_accounts.secrets,'choice',lambda _:'A')
        with pytest.raises(WebAuthError) as error:await service.issue_code('20000001')
        assert error.value.status==503
        assert service.codes==before and '20000001' not in service.code_issued_at
        token=await service.login_code(previous,'one')
        assert (await service.authenticate(token))['qq']=='20000001'
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
        assert re.fullmatch(r'[A-HJ-NP-Z2-9]{8}',code)
        assert any(c.isdigit() for c in code) and any(c.isalpha() for c in code)
        with pytest.raises(WebAuthError):await service.login_code(old,'one')
        attempts=await asyncio.gather(service.login_code(' '+code.lower()+'\n','two'),service.login_code(code,'three'),return_exceptions=True)
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
        clock[0]+=SESSION_TTL
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


@pytest.mark.parametrize('automatic',[False,True])
def test_unload_prevents_inflight_password_work_from_writing_or_issuing_sessions(auth,monkeypatch,automatic):
    service,_,_=auth
    async def run():
        import web_accounts,threading
        entered,release=threading.Event(),threading.Event()
        original=web_accounts.password_hash
        def blocked(password):
            entered.set();assert release.wait(5)
            return original(password)
        monkeypatch.setattr(web_accounts,'password_hash',blocked)
        creation=asyncio.create_task(service.ensure_accounts(['20000001']) if automatic else service.create('valid','InitialPass8','20000001'))
        assert await asyncio.to_thread(entered.wait,5)
        service.close();release.set()
        with pytest.raises(WebAuthError) as error:await creation
        assert error.value.status==503
        assert not service.accounts and not service.path.exists() and not service.sessions
        with pytest.raises(WebAuthError):await service.issue_code('20000001')
    asyncio.run(run())


def test_auto_provision_persists_distinct_initial_passwords_and_preserves_changes(auth):
    service,_,teams=auth
    async def run():
        created=await service.ensure_accounts(['20000001','20000002','20000001'])
        assert len(created)==2
        first=await service.credentials('20000001');second=await service.credentials('20000002')
        assert first['username']=='20000001' and first['password']!=second['password']
        assert len(first['password'])>=20 and first['password'] not in service.path.read_text()
        assert service.path.stat().st_mode & 0o777==0o600
        assert 'password' not in created[0] and 'initial_key' not in repr(service.list_accounts())
        restored=WebAccounts(service.path,teams)
        assert await restored.credentials('20000001')==first
        token=await service.login(first['username'],first['password'],'one')
        await service.change_password(token,first['password'],'MyChangedPass9')
        await service.update(created[1]['id'],dict(action='disable'))
        before=service.path.read_bytes()
        teams.return_value=['b队','c队']
        assert await service.ensure_accounts(['20000001','20000002'])==[]
        assert service.path.read_bytes()==before
        assert (await service.credentials('20000001'))['password'] is None
        assert (await service.credentials('20000002'))['enabled'] is False
        assert await service.login('20000001','MyChangedPass9','two')
        with pytest.raises(WebAuthError):await service.login('20000002',second['password'],'three')
        teams.return_value=[]
        with pytest.raises(WebAuthError):await service.credentials('20000001')
        assert await service.ensure_accounts(['20000003'])==[]
    asyncio.run(run())


def test_auto_provision_legacy_accounts_collisions_and_atomic_failure(auth,monkeypatch):
    service,_,teams=auth
    async def run():
        legacy=await service.create('20000002','LegacyPass8','20000001')
        data=json.loads(service.path.read_text());data.pop('initial_key')
        service.path.write_text(json.dumps(data))
        restored=WebAccounts(service.path,teams)
        assert await restored.login('20000002','LegacyPass8','legacy')
        made=await restored.ensure_accounts(['20000001','20000002','7'])
        assert {a['username'] for a in made}=={'qq_20000002','qq_7'}
        assert restored.accounts[legacy['id']]['hash']==service.accounts[legacy['id']]['hash']
        before=restored.path.read_bytes();accounts=restored.list_accounts()
        def fail():raise OSError('disk full')
        monkeypatch.setattr(restored,'save',fail)
        with pytest.raises(OSError):await restored.ensure_accounts(['20000003','20000004'])
        assert restored.path.read_bytes()==before and restored.list_accounts()==accounts
        with pytest.raises(WebAuthError):await restored.ensure_accounts(['bad'])
        restored.close()
        with pytest.raises(WebAuthError):await restored.ensure_accounts(['20000005'])
    asyncio.run(run())


def test_auto_initial_key_must_be_present_and_valid(auth):
    service,_,teams=auth
    asyncio.run(service.ensure_accounts(['20000001']))
    data=json.loads(service.path.read_text());data.pop('initial_key')
    service.path.write_text(json.dumps(data))
    with pytest.raises(ValueError):WebAccounts(service.path,teams)
    data['initial_key']='invalid';service.path.write_text(json.dumps(data))
    with pytest.raises(ValueError):WebAccounts(service.path,teams)


def test_week_sessions_survive_restart_without_persisting_tokens_or_unused_codes(auth):
    service,clock,teams=auth
    async def run():
        assert SESSION_TTL==7*24*3600
        await service.create('week_user','InitialPass8','20000001')
        password=await service.login('week_user','InitialPass8','one')
        used=await service.issue_code('20000001');code=await service.login_code(used,'two')
        clock[0]+=31;unused=await service.issue_code('20000001')
        raw=service.path.read_text()
        assert all(secret not in raw for secret in [password,code,used,unused,'InitialPass8'])
        assert len(json.loads(raw)['sessions'])==2 and service.path.stat().st_mode & 0o777==0o600
        service.close()
        restored=WebAccounts(service.path,teams,clock=lambda:clock[0])
        for token in [password,code]:assert (await restored.authenticate(token))['qq']=='20000001'
        with pytest.raises(WebAuthError):await restored.login_code(unused,'three')
        clock[0]=1000+8*3600+1
        assert (await restored.authenticate(password))['password_login']
        clock[0]=1000+SESSION_TTL-1
        assert await restored.authenticate(code)
        clock[0]+=1
        for token in [password,code]:
            with pytest.raises(WebAuthError):await restored.authenticate(token)
    asyncio.run(run())


def test_logout_reset_disable_and_observed_revocation_remain_revoked_after_restart(auth):
    service,clock,teams=auth
    def restored():return WebAccounts(service.path,teams,clock=lambda:clock[0])
    async def run():
        account=await service.create('week_user','InitialPass8','20000001')
        first=await service.login('week_user','InitialPass8','one')
        service.logout(first)
        with pytest.raises(WebAuthError):await restored().authenticate(first)
        password=await service.login('week_user','InitialPass8','two')
        qq=await service.login_code(await service.issue_code('20000001'),'three')
        await service.update(account['id'],dict(action='reset',password='ChangedPass9'))
        with pytest.raises(WebAuthError):await restored().authenticate(password)
        assert (await restored().authenticate(qq))['password_login'] is False
        password=await service.login('week_user','ChangedPass9','four')
        await service.update(account['id'],dict(action='disable'))
        with pytest.raises(WebAuthError):await restored().authenticate(password)
        teams.return_value=[]
        with pytest.raises(WebAuthError):await service.authenticate(qq)
        teams.return_value=['a队']
        with pytest.raises(WebAuthError):await restored().authenticate(qq)
    asyncio.run(run())


def test_session_store_failure_does_not_issue_unstored_token_or_fake_logout(auth,monkeypatch):
    service,clock,teams=auth
    async def run():
        await service.create('week_user','InitialPass8','20000001')
        token=await service.login('week_user','InitialPass8','one')
        before=service.path.read_bytes();sessions=service.sessions.copy()
        def fail():raise OSError('disk full')
        monkeypatch.setattr(service,'save',fail)
        with pytest.raises(OSError):await service.login('week_user','InitialPass8','two')
        assert service.sessions==sessions and service.path.read_bytes()==before
        with pytest.raises(OSError):service.logout(token)
        assert service.sessions==sessions and (await service.authenticate(token))['qq']=='20000001'
        assert await WebAccounts(service.path,teams,clock=lambda:clock[0]).authenticate(token)
    asyncio.run(run())


@pytest.mark.parametrize('session',[
    dict(qq='bad',account_id=None,revision=None,expires=1001),
    dict(qq='20000001',account_id=None,revision='bad',expires=1001),
    dict(qq='20000001',account_id=True,revision=None,expires=1001),
    dict(qq='20000001',account_id=None,revision=None,expires=True),
    dict(qq='20000001',account_id=None,revision=None,expires=float('nan')),
])
def test_corrupt_persisted_session_fails_closed(auth,session):
    service,clock,teams=auth
    service.save()
    data=json.loads(service.path.read_text());data['sessions']={'0'*64:session};service.path.write_text(json.dumps(data))
    with pytest.raises(ValueError):WebAccounts(service.path,teams,clock=lambda:clock[0])


def test_password_revoked_during_role_lookup_cannot_finish_authentication(auth):
    service,_,_=auth
    async def run():
        account=await service.create('week_user','InitialPass8','20000001')
        token=await service.login('week_user','InitialPass8','one')
        entered,release=asyncio.Event(),asyncio.Event()
        async def wait_for_teams(qq):entered.set();await release.wait();return ['a队']
        service.allowed_teams=wait_for_teams
        request=asyncio.create_task(service.authenticate(token));await entered.wait()
        await service.update(account['id'],dict(action='disable'));release.set()
        with pytest.raises(WebAuthError):await request
    asyncio.run(run())
