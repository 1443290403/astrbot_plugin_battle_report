"""授权在服务端覆盖所有页面API；SDK运输边界使用最小桩，真实SDK另做浏览器验证。"""
import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

pytest.importorskip('fastapi')
httpx=pytest.importorskip('httpx')
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse,FileResponse
from report_webui import ComparisonWebUI,atomic_json
from report_site import ComparisonQuery
from standalone_web import StandaloneFrontend
import test_report_comparison_webui as sample


@pytest.fixture
def site(tmp_path,monkeypatch):
    current=ContextVar('test_web_request')
    class PluginRequest:
        def __init__(self,request,username=None,**kwargs):
            self.raw=request;self.username=username;self.query=request.query_params
        async def json(self,default=None):
            try:return await self.raw.json()
            except ValueError:return default
    class Proxy:
        def __getattr__(self,key):return getattr(current.get(),key)
    @contextmanager
    def bind(req):
        token=current.set(req)
        try:yield
        finally:current.reset(token)
    sdk=types.ModuleType('astrbot.api.web')
    sdk.PluginRequest=PluginRequest;sdk.bind_request_context=bind;sdk.request=Proxy()
    sdk.json_response=lambda data:JSONResponse(data)
    sdk.error_response=lambda message,status_code=400:JSONResponse(dict(status='error',message=message),status_code=status_code)
    sdk.file_response=lambda path,filename,content_type:FileResponse(path,filename=filename,media_type=content_type)
    for name in ('astrbot','astrbot.api'):
        module=types.ModuleType(name);module.__path__=[];monkeypatch.setitem(sys.modules,name,module)
    monkeypatch.setitem(sys.modules,'astrbot.api.web',sdk)
    roles={'20000001':['a队'],'20000002':['c队'],'20000003':['a队','c队']}
    local={1:sample.report(),2:dict(sample.report(2),home_team='c队',raw_text=sample.BODY.replace('a队','c队').replace('b队','d队'))}
    def cloud(rid,team,other):
        record=sample.record(str(rid),sample.BODY.replace('a队',team).replace('b队',other))
        return dict(cloud_id=rid,team_a=team,team_b=other,winner_site=team,loser_site=other,query_team=team,
                    match_time='2026-10-10',publish_date='2026-10-11',last_seen_at=1,body=record['body'],
                    parsed_json=json.dumps(record),publisher='示例用户',source_url='https://rep.ygobbs2.com/')
    clouds={101:cloud(101,'a队','b队'),102:cloud(102,'c队','d队')}
    async def local_list(home,*args,**kwargs):
        rows=[copy.deepcopy(r) for r in local.values() if r['home_team'].upper()==home.upper()]
        return dict(total=len(rows),records=rows)
    async def local_ids(home,ids):return [copy.deepcopy(r) for rid,r in local.items() if rid in ids and r['home_team'].upper()==home.upper()]
    async def cloud_list(start,end,team,*args,**kwargs):
        rows=[r for r in clouds.values() if team.upper() in {r['team_a'].upper(),r['team_b'].upper()}]
        return dict(total=len(rows),records=copy.deepcopy(rows))
    db=SimpleNamespace(get_team_admin_qqs=AsyncMock(side_effect=lambda:list(roles)),get_admin_teams=AsyncMock(side_effect=lambda qq:list(roles.get(qq,[]))),get_all_teams=AsyncMock(return_value=['a队','c队']),
        get_dispatch_admin_teams=AsyncMock(return_value=['a队','c队']),get_team_admins=AsyncMock(return_value=['20000001','20000002']),
        get_webui_local_reports=AsyncMock(side_effect=local_list),get_webui_local_reports_by_ids=AsyncMock(side_effect=local_ids),
        get_cloud_reports=AsyncMock(side_effect=cloud_list),get_cloud_reports_by_ids=AsyncMock(side_effect=lambda ids:[copy.deepcopy(r) for rid,r in clouds.items() if rid in ids]),
        archive_cloud_reports=AsyncMock(return_value=dict(inserted=0,refreshed=1)),get_comparison_reports=AsyncMock(return_value=[sample.report()]))
    calls=[]
    async def action(name,**params):
        if name=='get_login_info':return {'user_id':10000001}
        assert name=='send_private_forward_msg';calls.append(params);return {'message_id':len(calls)}
    bot=SimpleNamespace(call_action=action,_wsr_api_clients={10000001:object()})
    platform=SimpleNamespace(meta=lambda:SimpleNamespace(name='aiocqhttp',id='onebot'),get_client=lambda:bot)
    registered=[]
    context=SimpleNamespace(register_web_api=lambda *r:registered.append(r),registered_web_apis=registered,
        platform_manager=SimpleNamespace(platform_insts=[platform]),get_platform_inst=lambda pid:platform if pid=='onebot' else None)
    plugin=SimpleNamespace(context=context,data_dir=tmp_path,db=db,db_ready=True)
    ui=ComparisonWebUI(plugin,logging.getLogger('team-test'));ui.register();ui.dispatch.delay=0
    ui.jobs.client=SimpleNamespace(crawl=AsyncMock(return_value=[sample.record()]))
    app=FastAPI();app.state.core_lifecycle=SimpleNamespace(star_context=context)
    frontend=StandaloneFrontend(context,logging.getLogger('team-test'),webui=ui)
    assert frontend.register(SimpleNamespace(_app=app))
    @app.api_route('/api/plug/{path:path}',methods=['GET','POST'])
    async def master(request:Request,path:str):
        if request.headers.get('authorization')!='Bearer test-master':return JSONResponse({},status_code=401)
        endpoint=path.removeprefix('astrbot_plugin_battle_report/')
        import re
        for pattern,fn,methods in ui.endpoints():
            match=re.fullmatch(re.sub(r'<([a-z_]+)>',r'(?P<\1>[^/]+)','comparison/'+pattern),endpoint)
            if match and request.method in methods:
                with bind(PluginRequest(request,username='master')):return await fn(**match.groupdict())
        return JSONResponse({},status_code=404)
    async def close():frontend.close();await ui.close()
    return SimpleNamespace(app=app,ui=ui,roles=roles,db=db,calls=calls,bot=bot,close=close)


async def team_login(site,client,qq='20000001'):
    code=await site.ui.auth.issue_code(qq)
    response=await client.post('/battle-report/auth/code-login',json={'code':code})
    assert response.status_code==200,response.text
    return response.json()['data']['token']


def test_scoped_login_all_api_boundaries_and_account_management_are_server_enforced(site):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            master={'Authorization':'Bearer test-master'}
            response=await client.post('/api/plug/astrbot_plugin_battle_report/comparison/accounts',headers=master,
                json=dict(action='sync'))
            assert response.status_code==200,response.text
            assert response.json()['created']==3
            aid=next(a['id'] for a in response.json()['accounts'] if a['qq']=='20000001')
            initial=await client.post('/api/plug/astrbot_plugin_battle_report/comparison/accounts/'+aid,headers=master,json=dict(action='initial'))
            assert initial.status_code==200
            response=await client.post('/battle-report/auth/login',json=dict(username='20000001',password=initial.json()['password']))
            assert response.status_code==200
            token=response.json()['data']['token'];headers={'Authorization':'Bearer '+token}
            assert 'httponly' in response.headers['set-cookie'].lower() and 'path=/battle-report' in response.headers['set-cookie'].lower()
            assert (await client.get('/api/plug/astrbot_plugin_battle_report/comparison/options',headers=headers)).status_code==401
            base='/battle-report/api/comparison/'
            data=(await client.get(base+'options',headers=headers)).json()
            assert data['teams']==['a队'] and data['role']=='team'
            for path in ['local-reports','cloud-reports']:
                own=await client.get(base+path,headers=headers,params=dict(team='a队',month='2026-10'))
                assert own.status_code==200 and own.json()['total']==1
                assert (await client.get(base+path,headers=headers,params=dict(team='c队',month='2026-10'))).status_code==403
                assert (await client.get(base+path,headers=headers,params=dict(team='a队',site_team='c队',month='2026-10'))).status_code==403
            for method,path,body in [('GET','accounts',None),('POST','accounts',dict(username='evil',password='InitialPass8',qq='20000002')),
                                     ('POST','accounts/'+aid,dict(action='delete'))]:
                r=await client.request(method,base+path,headers=headers,json=body)
                assert r.status_code==403,r.text
            assert (await client.get(base+'dispatch/admins',headers=headers,params={'team':'c队'})).status_code==403
            assert (await client.get(base+'dispatch/options',headers=headers)).json()['admin_teams']==['a队']
            site.roles['20000001']=[]
            assert (await client.get(base+'options',headers=headers)).status_code==401
            site.roles['20000001']=['a队']
            assert (await client.get(base+'options',headers=headers)).status_code==401
        await site.close()
    asyncio.run(run())


def test_other_team_jobs_download_cancel_history_and_source_url_cannot_bypass_scope(site):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            token=await team_login(site,client);headers={'Authorization':'Bearer '+token};base='/battle-report/api/comparison/'
            for team in ['a队','c队']:
                job=await site.ui.jobs.start(dict(team=team,month='2026-10'));await site.ui.jobs.task
                if team=='c队':foreign=job['id']
            opts=(await client.get(base+'options',headers=headers)).json()
            assert len(opts['history'])==1 and opts['history'][0]['query']['team']=='a队'
            for method,suffix in [('GET',''),('GET','/download'),('POST','/cancel')]:
                response=await client.request(method,base+'jobs/'+foreign+suffix,headers=headers,json={} if method=='POST' else None)
                assert response.status_code==403,response.text
            response=await client.post(base+'jobs',headers=headers,json=dict(team='a队',month='2026-10',source_url='https://rep.ygobbs2.com/?c=search&a=sub&groupname=c%E9%98%9F&starttime=2026-10-01&endtime=2026-10-31'))
            assert response.status_code==403
            response=await client.post(base+'jobs',headers=headers,json=dict(team='a队',month='2026-10'))
            assert response.status_code==200;await site.ui.jobs.task
            jid=response.json()['id']
            assert (await client.get(base+'jobs/'+jid+'/download',headers=headers)).status_code==200
        await site.close()
    asyncio.run(run())


def test_dispatch_rejects_forged_references_and_revocation_stops_between_recipients(site):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            token=await team_login(site,client);headers={'Authorization':'Bearer '+token};base='/battle-report/api/comparison/'
            payload=dict(target_mode='custom',qq_ids='20000010,20000011',platform_id='onebot',self_id='10000001')
            for ref in [dict(source='local',id='2',home_team='c队'),dict(source='cloud',id='102')]:
                response=await client.post(base+'dispatch/prepare',headers=headers,json={**payload,'reports':[ref]})
                assert response.status_code==403,response.text
            own=dict(source='local',id='1',home_team='a队',body='forged')
            response=await client.post(base+'dispatch/prepare',headers=headers,json={**payload,'reports':[own]})
            assert response.status_code==200,response.text
            plan=response.json();assert plan['reports'][0]['body']!='forged'
            original=site.bot.call_action
            async def revoke(name,**params):
                result=await original(name,**params)
                if name=='send_private_forward_msg':site.roles['20000001']=[]
                return result
            site.bot.call_action=revoke
            sent=await client.post(base+'dispatch/'+plan['id']+'/send',headers=headers,json={})
            assert sent.status_code==200,sent.text
            await site.ui.dispatch.task
            assert len(site.calls)==1
            stored=site.ui.dispatch.plans[plan['id']]
            assert stored['entries'][0]['status']=='sent' and stored['entries'][1]['status']=='cancelled'
            assert stored['status']=='interrupted'
        await site.close()
    asyncio.run(run())


def test_credentials_do_not_cross_users_and_logout_replay_limits_are_enforced(site):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            code=await site.ui.auth.issue_code('20000001')
            response=await client.post('/battle-report/auth/code-login',json=dict(code=code))
            token=response.json()['data']['token']
            assert (await client.post('/battle-report/auth/code-login',json=dict(code=code))).status_code==401
            assert (await client.post('/battle-report/auth/logout',headers={'Authorization':'Bearer '+token})).status_code==200
            assert (await client.get('/battle-report/auth/session',headers={'Authorization':'Bearer '+token})).status_code==401
            assert (await client.post('/battle-report/auth/login',content=b'x'*8193)).status_code==413
            assert (await client.post('/battle-report/auth/login',headers={'Sec-Fetch-Site':'cross-site'},json={})).status_code==403
            assert (await client.get('/battle-report/auth/login')).status_code==405
            assert (await client.get('/battle-report/auth/unknown')).status_code==404
        await site.close()
    asyncio.run(run())


def test_parallel_sessions_keep_distinct_team_context_and_do_not_change_master_scope(site):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            a=await team_login(site,client,'20000001');c=await team_login(site,client,'20000002')
            async def read(token,team):
                response=await client.get('/battle-report/api/comparison/local-reports',headers={'Authorization':'Bearer '+token},params=dict(team=team,month='2026-10'))
                assert response.status_code==200,response.text
                assert response.json()['records'][0]['home_team']==team
            await asyncio.gather(*(read(a,'a队') if i%2 else read(c,'c队') for i in range(20)))
            master=await client.get('/api/plug/astrbot_plugin_battle_report/comparison/options',headers={'Authorization':'Bearer test-master'})
            assert master.json()['teams']==['a队','c队'] and master.json()['role']=='master'
        await site.close()
    asyncio.run(run())


def test_https_reverse_proxy_session_cookie_remains_secure(site):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            code=await site.ui.auth.issue_code('20000001')
            response=await client.post('/battle-report/auth/code-login',headers={'X-Forwarded-Proto':'https'},json={'code':code})
            assert response.status_code==200
            assert 'secure' in response.headers['set-cookie'].lower()
        await site.close()
    asyncio.run(run())


def test_root_sync_preserves_credentials_and_initial_password_is_not_public(site):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app),base_url='http://test') as client:
            root='/api/plug/astrbot_plugin_battle_report/comparison/accounts';master={'Authorization':'Bearer test-master'}
            response=await client.post(root,headers=master,json=dict(username='manual',qq='20000001',password='InitialPass8'))
            assert response.status_code==400
            response=await client.post(root,headers=master,json=dict(action='sync'))
            account=next(a for a in response.json()['accounts'] if a['qq']=='20000001')
            initial=await client.post(root+'/'+account['id'],headers=master,json=dict(action='initial'))
            secret=initial.json()['password']
            assert secret and secret not in (await client.get(root,headers=master)).text
            token=await team_login(site,client)
            for path in ['/battle-report/api/comparison/accounts/'+account['id'],root+'/'+account['id']]:
                response=await client.post(path,headers={'Authorization':'Bearer '+token},json=dict(action='initial'))
                assert response.status_code in (401,403) and secret not in response.text
            assert (await client.post(root+'/'+account['id'],headers=master,json=dict(action='reset',password='ChangedPass9'))).status_code==200
            assert (await client.post(root,headers=master,json=dict(action='sync'))).json()['created']==0
            assert (await client.post(root+'/'+account['id'],headers=master,json=dict(action='initial'))).json()['password'] is None
            assert (await client.post(root+'/'+account['id'],headers=master,json=dict(action='delete'))).status_code==400
            assert await site.ui.auth.login('20000001','ChangedPass9','check')
        await site.close()
    asyncio.run(run())
