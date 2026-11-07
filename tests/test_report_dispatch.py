"""QQ发送只使用模拟OneBot，不连接或发送到真实QQ。"""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from report_dispatch import QQReportDispatch, qq_ids
from report_site import ComparisonError


@pytest.fixture
def dispatch_env(tmp_path):
    forwards=[]
    outcomes=[]
    async def action(name,**params):
        if name=='get_login_info': return {'user_id':10000001}
        assert name=='send_private_forward_msg'
        forwards.append(params)
        if outcomes:
            item=outcomes.pop(0)
            if isinstance(item,BaseException): raise item
            return item
        return {'message_id':len(forwards)}
    bot=SimpleNamespace(call_action=AsyncMock(side_effect=action),_wsr_api_clients={10000001:object()})
    platform=SimpleNamespace(meta=lambda:SimpleNamespace(name='aiocqhttp',id='onebot-a'),get_client=lambda:bot)
    context=SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[platform]),get_platform_inst=lambda pid:platform if pid=='onebot-a' else None)
    load=AsyncMock(return_value=[dict(source='local',id='1',home_team='a队',team_a='A队',team_b='B队',body='云玩家  2:0  萌新\n[CQ:at,qq=all]')])
    admins=AsyncMock(return_value=['20000002'])
    service=QQReportDispatch(tmp_path,context,load,admins,delay=0)
    body=dict(reports=[dict(source='local',id='1',home_team='a队')],target_mode='custom',qq_ids='20000001',platform_id='onebot-a',self_id='10000001')
    return service,bot,load,admins,body,forwards,outcomes,context


def test_prepare_does_not_send_and_confirm_is_idempotent(dispatch_env):
    service,bot,load,_,body,calls,_,_=dispatch_env
    async def scenario():
        plan=await service.prepare(body,'admin')
        assert calls==[] and plan['reports'][0]['body'].startswith('云玩家  2:0')
        await service.confirm(plan['id'],'admin');await service.task
        result=service.public(service.get(plan['id'],'admin'))
        assert result['counts']['sent']==1 and result['status']=='completed'
        assert calls[0]['user_id']==20000001 and calls[0]['self_id']==10000001
        node=calls[0]['messages'][0]
        assert node['data']['uin']==10000001 and node['data']['content']==[{'type':'text','data':{'text':load.return_value[0]['body']}}]
        await service.confirm(plan['id'],'admin')
        assert len(calls)==1
        with pytest.raises(ComparisonError):service.get(plan['id'],'other-admin')
    asyncio.run(scenario())


def test_chunks_and_recipients_each_have_separate_receipts(dispatch_env):
    service,_,load,_,body,calls,_,_=dispatch_env
    load.return_value=[dict(source='cloud',id=str(i),team_a='A队',team_b='B队',body=f'战报 {i}') for i in range(51)]
    body['qq_ids']='20000001,20000002,20000001'
    async def scenario():
        plan=await service.prepare(body,'admin');await service.confirm(plan['id'],'admin');await service.task
        assert len(calls)==4 and [len(c['messages']) for c in calls]==[50,1,50,1]
        assert service.public(service.get(plan['id'],'admin'))['counts']['sent']==4
    asyncio.run(scenario())


def test_retry_never_resends_successful_target(dispatch_env):
    service,_,_,_,body,calls,outcomes,_=dispatch_env
    body['qq_ids']='20000001 20000002'
    outcomes.extend([{'message_id':1},{'status':'failed','retcode':100}])
    async def scenario():
        plan=await service.prepare(body,'admin');await service.confirm(plan['id'],'admin');await service.task
        assert service.get(plan['id'],'admin')['entries'][0]['status']=='sent'
        await service.confirm(plan['id'],'admin',retry=True);await service.task
        assert [c['user_id'] for c in calls]==[20000001,20000002,20000002]
        assert service.get(plan['id'],'admin')['status']=='completed'
    asyncio.run(scenario())


def test_unknown_delivery_is_not_retried(dispatch_env):
    service,_,_,_,body,calls,outcomes,_=dispatch_env
    body['qq_ids']='20000001,20000002'
    outcomes.extend([{'status':'failed','retcode':100},TimeoutError('unknown')])
    async def scenario():
        plan=await service.prepare(body,'admin');await service.confirm(plan['id'],'admin');await service.task
        assert [e['status'] for e in service.get(plan['id'],'admin')['entries']]==['failed','unknown']
        await service.confirm(plan['id'],'admin',retry=True);await service.task
        assert [c['user_id'] for c in calls]==[20000001,20000002,20000001]
        await service.confirm(plan['id'],'admin',retry=True)
        assert len(calls)==3 and service.get(plan['id'],'admin')['entries'][1]['status']=='unknown'
    asyncio.run(scenario())


def test_admin_scope_is_checked_at_prepare_and_confirm(dispatch_env):
    service,_,_,admins,body,calls,_,_=dispatch_env
    body.update(target_mode='admins',admin_team='a队',admin_ids=['20000001'])
    async def scenario():
        with pytest.raises(ComparisonError,match='不在该战队'):await service.prepare(body,'admin')
        body['admin_ids']=['20000002'];plan=await service.prepare(body,'admin')
        admins.return_value=[]
        with pytest.raises(ComparisonError,match='名单已变化'):await service.confirm(plan['id'],'admin')
        assert not calls
    asyncio.run(scenario())


def test_official_and_mismatched_sender_accounts_cannot_send(dispatch_env):
    service,bot,_,_,body,calls,_,context=dispatch_env
    async def scenario():
        body['platform_id']='official'
        with pytest.raises(ComparisonError):await service.prepare(body,'admin')
        body['platform_id']='onebot-a';body['self_id']='10000002'
        with pytest.raises(ComparisonError,match='QQ账号'):await service.prepare(body,'admin')
        assert not calls
    asyncio.run(scenario())


def test_restart_does_not_replay_sent_or_inflight_messages(dispatch_env):
    service,_,load,admins,body,calls,_,context=dispatch_env
    body['qq_ids']='20000001,20000002,20000003'
    async def scenario():
        plan=await service.prepare(body,'admin');stored=service.get(plan['id'],'admin')
        stored['status']='running';stored['entries'][0]['status']='sent';stored['entries'][1]['status']='sending'
        service.save(stored)
        restored=QQReportDispatch(service.path,context,load,admins,delay=0)
        state=restored.get(plan['id'],'admin')
        assert [e['status'] for e in state['entries']]==['sent','unknown','cancelled'] and not calls
        await restored.confirm(plan['id'],'admin',retry=True);await restored.task
        assert len(calls)==1 and calls[0]['user_id']==20000003
    asyncio.run(scenario())


def test_prepared_preview_expires_and_content_is_frozen(dispatch_env):
    service,_,load,_,body,calls,_,_=dispatch_env
    clock=[10.0];service.clock=lambda:clock[0]
    async def scenario():
        plan=await service.prepare(body,'admin')
        load.return_value[0]['body']='changed source'
        assert service.get(plan['id'],'admin')['reports'][0]['body']!='changed source'
        clock[0]=1811
        with pytest.raises(ComparisonError,match='过期'):await service.confirm(plan['id'],'admin')
        assert not calls
    asyncio.run(scenario())


def test_failed_plan_persistence_does_not_send_or_leave_running_state(dispatch_env,monkeypatch):
    service,_,_,_,body,calls,_,_=dispatch_env
    async def scenario():
        plan=await service.prepare(body,'admin')
        monkeypatch.setattr(service,'save',lambda p:(_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):await service.confirm(plan['id'],'admin')
        assert not calls and service.task is None
        assert service.get(plan['id'],'admin')['status']=='prepared'
    asyncio.run(scenario())


@pytest.mark.parametrize('value',['',[],['bad'],['012345'],['20000;30000'],['20000001']*21])
def test_invalid_qq_targets_rejected(value):
    with pytest.raises(ComparisonError):qq_ids(value)


def test_cancel_pending_dispatch_does_not_send(dispatch_env):
    service,_,_,_,body,calls,_,_=dispatch_env
    async def scenario():
        plan=await service.prepare(body,'admin');await service.confirm(plan['id'],'admin');await service.cancel(plan['id'],'admin')
        assert not calls and service.get(plan['id'],'admin')['entries'][0]['status']=='cancelled'
        await service.close()
    asyncio.run(scenario())


def test_cancel_inflight_marks_unknown_and_leaves_unsent_targets_retryable(dispatch_env):
    service,bot,_,_,body,calls,_,_=dispatch_env
    body['qq_ids']='20000001,20000002'
    async def scenario():
        started=asyncio.Event()
        async def action(name,**params):
            if name=='get_login_info':return {'user_id':10000001}
            calls.append(params);started.set();await asyncio.Event().wait()
        bot.call_action.side_effect=action
        plan=await service.prepare(body,'admin');await service.confirm(plan['id'],'admin');await started.wait()
        await service.cancel(plan['id'],'admin')
        state=service.get(plan['id'],'admin')
        assert [e['status'] for e in state['entries']]==['unknown','cancelled'] and len(calls)==1
    asyncio.run(scenario())
