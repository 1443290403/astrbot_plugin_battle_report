"""管理API必须使用核心后台身份；不通过QQ发送或访问生产数据库。"""
import asyncio
from unittest.mock import AsyncMock
import pytest
from test_team_webui import site,team_login,httpx

BASE='/api/plug/astrbot_plugin_battle_report/comparison/management/'
TEAM='/battle-report/api/comparison/management/'
MASTER={'Authorization':'Bearer test-master'}


def prepare(site):
    site.db.get_webui_teams=AsyncMock(return_value=dict(total=1,records=[dict(team='a队',admins=['20000001'],groups=[],member_count=1,player_count=1)]))
    site.db.get_webui_members=AsyncMock(return_value=dict(total=1,records=[dict(id='1',name='云 玩家',qq_id='20000001',players=['示例 ID'])]))
    site.db.get_webui_players=AsyncMock(return_value=dict(total=1,records=[dict(player_name='示例 ID',user_id='1',user_name='云 玩家',qq_id='20000001')]))
    site.db.set_team_admins=AsyncMock()
    site.db.save_webui_member=AsyncMock(return_value='1')
    site.db.set_webui_player_owner=AsyncMock()


@pytest.mark.parametrize('method,suffix',[('GET','teams'),('POST','teams'),('GET','members'),('POST','members'),('GET','players'),('POST','players')])
def test_management_is_core_admin_only_even_for_same_team_and_invalid_payload(site,method,suffix):
    prepare(site)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            assert (await client.request(method,BASE+suffix,json={} if method=='POST' else None)).status_code==401
            token=await team_login(site,client)
            response=await client.request(method,TEAM+suffix,headers={'Authorization':'Bearer '+token},params={'team':'a队'},json={'role':'master','qq':'20000001'} if method=='POST' else None)
            assert response.status_code==403,response.text
            for name in ['get_webui_teams','get_webui_members','get_webui_players','set_team_admins','save_webui_member','set_webui_player_owner']:
                getattr(site.db,name).assert_not_awaited()
        await site.close()
    asyncio.run(run())


def test_core_management_reads_writes_and_automatic_password_accounts(site):
    prepare(site)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            for suffix in ['teams','members','players']:
                result=await client.get(BASE+suffix,headers=MASTER,params=dict(team='a队',keyword='示例 ID',offset=30,limit=30))
                assert result.status_code==200 and result.json()['total']==1
            site.db.get_webui_members.assert_awaited_once_with('A队','示例 ID',30,30)
            result=await client.post(BASE+'teams',headers=MASTER,json=dict(team='a队',qqs=['20000001','20000001'],expected=[]))
            assert result.status_code==200 and result.json()['accounts_ready']
            site.db.set_team_admins.assert_awaited_once_with('A队',['20000001'],expected=[])
            assert (await site.ui.auth.credentials('20000001'))['username']=='20000001'
            payload=dict(action='edit',team='a队',id='1',name='云 玩家',qq='',expected=dict(name='云玩家',qq_id='20000001'))
            assert (await client.post(BASE+'members',headers=MASTER,json=payload)).status_code==200
            site.db.save_webui_member.assert_awaited_once_with('A队','云 玩家','',user_id=1,expected={'name':'云玩家','qq_id':'20000001'})
            payload=dict(action='bind',team='a队',player='示例 ID',id='1',expected_owner=None)
            assert (await client.post(BASE+'players',headers=MASTER,json=payload)).status_code==200
            site.db.set_webui_player_owner.assert_awaited_once_with('A队','示例 ID',1,expected_owner=None)
            payload.update(action='unbind',expected_owner='1')
            assert (await client.post(BASE+'players',headers=MASTER,json=payload)).status_code==200
            assert site.db.set_webui_player_owner.await_args.args==('A队','示例 ID',None)
        await site.close()
    asyncio.run(run())


@pytest.mark.parametrize('suffix,payload',[
    ('teams',dict(team='a队',qqs='20000001',expected=[])),
    ('teams',dict(team='a队',qqs=['0123'],expected=[])),
    ('teams',dict(team='a队',qqs=[],expected=None)),
    ('teams',dict(team='x'*65,qqs=[],expected=[])),
    ('members',dict(team='a队',action='create',name='坏\n名称',qq='')),
    ('members',dict(team='a队',action='edit',id=True,name='云玩家',qq='')),
    ('members',dict(team='a队',action='edit',id='1',name='云玩家',qq='',expected=None)),
    ('members',dict(team='a队',action='edit',id='9223372036854775808',name='云玩家',qq='')),
    ('members',dict(team='a队',action='delete',name='云玩家',qq='')),
    ('players',dict(team='a队',action='bind',player='示例 ID',id='1')),
    ('players',dict(team='a队',action='bind',player='示例 ID',id='1',expected_owner=False)),
    ('players',dict(team='a队',action='delete',player='示例 ID',expected_owner=None)),
])
def test_invalid_management_payloads_never_write(site,suffix,payload):
    prepare(site)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            assert (await client.post(BASE+suffix,headers=MASTER,json=payload)).status_code==400
            for name in ['set_team_admins','save_webui_member','set_webui_player_owner']:getattr(site.db,name).assert_not_awaited()
        await site.close()
    asyncio.run(run())


@pytest.mark.parametrize('params',[{'offset':'-1'},{'limit':'0'},{'limit':'101'},{'offset':'1e3'},{'offset':'١'},{'keyword':'x'*65}])
def test_invalid_management_query_rejected_before_sql(site,params):
    prepare(site)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            assert (await client.get(BASE+'teams',headers=MASTER,params=params)).status_code==400
            site.db.get_webui_teams.assert_not_awaited()
        await site.close()
    asyncio.run(run())


def test_stale_edit_is_reported_and_account_store_failure_keeps_grant_success(site):
    prepare(site)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            site.db.save_webui_member.side_effect=ValueError('队员资料已变化，请刷新后重试。')
            result=await client.post(BASE+'members',headers=MASTER,json=dict(action='create',team='a队',name='云玩家',qq=''))
            assert result.status_code==400 and '已变化' in result.text
            site.ui.sync_accounts=AsyncMock(side_effect=OSError('disk full'))
            result=await client.post(BASE+'teams',headers=MASTER,json=dict(team='a队',qqs=['20000001'],expected=[]))
            assert result.status_code==200 and result.json()['updated'] and not result.json()['accounts_ready']
            site.db.set_team_admins.assert_awaited_once()
        await site.close()
    asyncio.run(run())
