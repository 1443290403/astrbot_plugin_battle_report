"""战报站接入、只读任务和原文对比回归；所有网络/数据库均为隔离样本。"""
import asyncio
import copy
from dataclasses import asdict
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from report_site import ComparisonQuery, ComparisonError, SiteClient, enrich, parse_page
from report_comparison import build_comparison, compare
from report_webui import ComparisonJobs, ComparisonWebUI, atomic_json

BODY = '''战队: a队 VS b队
时间: 2026.10.10
规则: 2/3【KOF】
地点: 123456789
------第一轮------
云玩家  2:0  萌新'''


def record(rid='101', body=BODY):
    return enrich(dict(id=rid, winner_site='A队', loser_site='B队', publish_date='2026-10-11',
                       publisher='示例发布者', body=body), '2026-10-01', '2026-10-31')


def report(mid=1, body=BODY):
    rec = record(body=body)
    return dict(match_id=mid, team_a=rec['team_a'], team_b=rec['team_b'], match_time=rec['match_time'],
                rule=rec['rule'], location=rec['location'], winner=rec['winner_site'], home_team='a队',
                submitted_name='示例用户', submitted_by='PRIVATE_QQ', kind='friendly',
                raw_text=body, export_sent=False, duels=copy.deepcopy(rec['duels']))


def payload(**kwargs):
    return dict(team='a队', month='2026-10', **kwargs)


def page(ids=('101',), *, current=1, pages=1, body=BODY):
    from html import escape
    rows = ''.join(f'<TR class="r"><TD><a>{rid}</a></TD><TD><b>A队</b></TD><TD>B队</TD>'
                   f'<TD>26-10-11</TD><TD>示例用户</TD><TD><DIV class="panel panel-body">'
                   + escape(body).replace('\n', '<BR/>') + '</DIV></TD></TR>' for rid in ids)
    return f'<table><caption>战报列表</caption><th>胜方</th><th>负方</th>{rows}</table>当前为{current}页，共有{pages}页'


def test_user_url_derives_query_and_keeps_publication_distinct_from_match_dates():
    query = ComparisonQuery.from_payload(dict(source_url='https://rep.ygobbs2.com/?c=search&a=sub&username=&groupname=a%E9%98%9F&starttime=2026-10-01&endtime=2026-10-31&playername='))
    assert query.start == query.publish_start == '2026-10-01' and query.team == 'a队'
    query = ComparisonQuery.from_payload(payload(publish_start='2026-10-01', publish_end='2026-11-07'))
    assert query.end == '2026-10-31' and query.publish_end == '2026-11-07'
    assert 'endtime=2026-11-07' in query.url(2) and 'page=2' in query.url(2)


@pytest.mark.parametrize('url', [
    'https://evil.example/?c=search&a=sub', 'http://rep.ygobbs2.com/?c=search&a=sub',
    'https://rep.ygobbs2.com.evil/?c=search&a=sub', 'https://user:pass@rep.ygobbs2.com/?c=search&a=sub',
    'https://rep.ygobbs2.com:8080/?c=search&a=sub', 'https://rep.ygobbs2.com:bad/?c=search&a=sub',
    'https://rep.ygobbs2.com/admin?c=search&a=sub', 'https://rep.ygobbs2.com/?c=search&a=sub&playername=someone',
    'https://rep.ygobbs2.com/?c=search&a=sub&groupname=a&groupname=b',
])
def test_source_url_cannot_be_used_for_arbitrary_requests(url):
    with pytest.raises(ComparisonError):
        ComparisonQuery.from_payload(payload(source_url=url))


@pytest.mark.parametrize('changes', [dict(month='2026-13'), dict(month='2026-1'),
    dict(publish_start='2026-11-01', publish_end='2026-10-01'),
    dict(publish_start='2026-01-01', publish_end='2026-12-31'), dict(team=''), dict(team='a\n队')])
def test_invalid_scope_rejected(changes):
    data = payload(); data.update(changes)
    with pytest.raises(ComparisonError):
        ComparisonQuery.from_payload(data)


def test_page_attributes_nested_body_and_entities_are_text():
    html = page().replace('云玩家', '<div><strong>云玩家</strong></div>').replace('示例用户', '&lt;script&gt;示例&lt;/script&gt;')
    rows, pages = parse_page(html)
    assert pages == 1 and len(rows) == 1
    assert '云玩家' in rows[0]['body'] and '2:0' in rows[0]['body']
    assert rows[0]['publisher'] == '<script>示例</script>'
    assert rows[0]['winner_site'] == 'A队'


def test_complete_pagination_and_legitimate_empty_result():
    async def scenario():
        query = ComparisonQuery.from_payload(payload())
        loader = AsyncMock(side_effect=[page(('101','102'), pages=2), page(('103',),current=2,pages=2)])
        progress = AsyncMock()
        records = await SiteClient(loader,delay=0).crawl(query,progress)
        assert [r['id'] for r in records] == ['101','102','103']
        assert progress.await_args.args == (2,2,3)
        empty = await SiteClient(AsyncMock(return_value=page((),pages=0)),delay=0).crawl(query,progress)
        assert empty == []
    asyncio.run(scenario())


@pytest.mark.parametrize('second', [page(('101',),current=2,pages=2), page(('102',),current=1,pages=2),
    page((),current=2,pages=2), page(('102',),current=2,pages=3), '<h1>暂时出错</h1>'])
def test_partial_or_inconsistent_site_fetch_is_never_accepted(second):
    async def scenario():
        loader = AsyncMock(side_effect=[page(pages=2),second,second])
        with pytest.raises(ComparisonError):
            await SiteClient(loader,delay=0).crawl(ComparisonQuery.from_payload(payload()),AsyncMock())
    asyncio.run(scenario())


def test_rule_and_penalty_changes_are_content_differences_not_matches():
    for change in ('rule','ruled','a_sub','score_a'):
        original = report()
        if change == 'rule': original['rule'] = '人头赛'
        elif change == 'score_a': original['duels'][0]['score_a'] = 1
        else: original['duels'][0][change] = True
        result = build_comparison([record()], [original], asdict(ComparisonQuery.from_payload(payload())))
        groups = {c['key']:c for c in result['categories']}
        assert result['summary']['matched_groups'] == 0
        assert groups['content_differences']['count'] == 1
        assert groups['only_in_db']['count'] == groups['missing_in_db']['count'] == 1


def test_round_order_and_id_internal_spaces_preserved():
    rec = record(body=BODY + '\n云玩家  0:2  遗老')
    db = report(body=rec['body'])
    db['duels'].reverse()
    result = build_comparison([rec], [db], asdict(ComparisonQuery.from_payload(payload())))
    assert result['summary']['matched_groups'] == 0
    spaced = record(body=BODY.replace('云玩家','示例  玩家'))
    db = report(body=spaced['body']); db['duels'][0]['player_a'] = '示例 玩家'
    assert build_comparison([spaced],[db],asdict(ComparisonQuery.from_payload(payload())))['summary']['matched_groups'] == 0


def test_comparison_is_read_only_and_excludes_raid_other_team_and_outside_month():
    records = [record(), record('102',BODY.replace('2026.10.10','2026.09.30')),record('103',BODY.replace('a队','其他队'))]
    reports = [report(),dict(report(2),kind='raid')]
    before = copy.deepcopy((records,reports))
    result = build_comparison(records,reports,asdict(ComparisonQuery.from_payload(payload())))
    assert (records,reports) == before
    assert result['summary']['db_total'] == result['summary']['matched_groups'] == 1
    assert result['summary']['excluded'] == {'raid':1}
    groups={c['key']:c for c in result['categories']}
    assert groups['out_of_range']['count'] == groups['other_team']['count'] == 1
    assert 'PRIVATE_QQ' not in json.dumps(result,ensure_ascii=False)


def test_winner_mismatch_and_duplicates_retained_with_ids():
    records = [record(),record('102')]
    reports = [dict(report(5),winner='B队'), report(6)]
    result = build_comparison(records,reports,asdict(ComparisonQuery.from_payload(payload())))
    groups={c['key']:c for c in result['categories']}
    assert groups['site_duplicates']['count'] == groups['db_duplicates']['count'] == 1
    assert groups['winner_mismatch']['count'] == 1
    assert groups['db_duplicates']['records'][0]['local'][0]['id'] == '5'


def test_jobs_success_pagination_history_and_restart(tmp_path):
    async def scenario():
        loader = AsyncMock(return_value=[report(i) for i in range(40)])
        client=SimpleNamespace(crawl=AsyncMock(return_value=[record()]))
        jobs=ComparisonJobs(tmp_path,loader,client=client)
        job=await jobs.start(payload()); await jobs.task
        assert jobs.jobs[job['id']]['status'] == 'completed'
        loader.assert_awaited_once()
        result=await jobs.get(job['id'],'db_duplicates',0,1)
        assert result['total'] == len(result['records']) == 1
        assert len(result['records'][0]['local']) == 40
        restored=ComparisonJobs(tmp_path,loader,client=client)
        assert restored.history()[0]['status'] == 'completed'
        assert (await restored.get(job['id']))['job']['summary']['db_total'] == 40
        assert restored.result_path(job['id']).is_file()
        with pytest.raises(ComparisonError): await restored.get('../secret')
    asyncio.run(scenario())


def test_failed_fetch_never_queries_database_or_publishes_diff(tmp_path):
    async def scenario():
        loader=AsyncMock()
        jobs=ComparisonJobs(tmp_path,loader,client=SimpleNamespace(crawl=AsyncMock(side_effect=ComparisonError('分页失败'))))
        job=await jobs.start(payload()); await jobs.task
        assert jobs.jobs[job['id']]['status'] == 'failed'
        loader.assert_not_awaited()
        assert not jobs.result_path(job['id']).exists()
        assert (await jobs.get(job['id']))['records'] == []
    asyncio.run(scenario())


def test_concurrent_jobs_rejected_and_pending_job_cancelled(tmp_path):
    async def scenario():
        jobs=ComparisonJobs(tmp_path,AsyncMock(),client=SimpleNamespace(crawl=AsyncMock()))
        job=await jobs.start(payload())
        with pytest.raises(ComparisonError,match='已有对比'): await jobs.start(payload())
        await jobs.cancel(job['id'])
        assert jobs.jobs[job['id']]['status'] == 'cancelled'
        assert jobs.active_id is None
    asyncio.run(scenario())


def test_interrupted_job_is_not_automatically_refetched(tmp_path):
    query=asdict(ComparisonQuery.from_payload(payload()))
    job=dict(id='a'*32,status='running',created_at='2026-11-01',query=query)
    atomic_json(tmp_path/(job['id']+'.job.json'),job)
    client=SimpleNamespace(crawl=AsyncMock())
    jobs=ComparisonJobs(tmp_path,AsyncMock(),client=client)
    assert jobs.history()[0]['status'] == 'interrupted'
    client.crawl.assert_not_awaited()


def test_webui_register_and_reload_cleanup_do_not_remove_new_instance(tmp_path):
    routes=[]
    def register(route,handler,methods,desc):
        routes[:]=[entry for entry in routes if not (entry[0] == route and entry[2] == methods)]
        routes.append((route,handler,methods,desc))
    plugin=SimpleNamespace(data_dir=tmp_path,context=SimpleNamespace(register_web_api=register,registered_web_apis=routes))
    first=ComparisonWebUI(plugin,logging.getLogger('test'));first.register()
    assert len(routes) == 22
    second=ComparisonWebUI(plugin,logging.getLogger('test'));second.register()
    asyncio.run(first.close())
    assert len(routes) == 22 and all(r[1].__self__ is second for r in routes)
    asyncio.run(second.close()); assert not routes


def test_snapshot_preserves_sides_without_changing_normal_export_fields():
    from database import Database
    async def scenario():
        db=object.__new__(Database)
        base=report()
        row={k:v for k,v in base.items() if k!='duels'}
        row.update(base['duels'][0],seq=1,owner=False,player_a_team='A队',player_b_team='B队')
        db._query=AsyncMock(return_value=[row])
        comparison=await db.get_comparison_reports('a队','2026-10-01','2026-10-31')
        assert comparison[0]['duels'][0]['player_a_team']=='A队'
        sql,args=db._query.await_args.args
        assert sql.lstrip().startswith('SELECT') and 'd.player_a_team' in sql
        assert args == ('a队','2026-10-01','2026-10-31')
        regular=await db.get_reports_for_export('a队','2026-10-01','2026-10-31')
        assert 'player_a_team' not in regular[0]['duels'][0]
        assert 'd.player_a_team' not in db._query.await_args.args[0]
    asyncio.run(scenario())


def test_local_duel_sides_aligned_before_matching_and_display():
    local=report()
    d=local['duels'][0]
    d['player_a'],d['player_b']=d['player_b'],d['player_a']
    d['score_a'],d['score_b']=d['score_b'],d['score_a']
    d.update(player_a_team='B队',player_b_team='A队')
    result=build_comparison([record()],[local],asdict(ComparisonQuery.from_payload(payload())))
    assert result['summary']['matched_groups']==1
    shown=result['categories'][0]['records'][0]['local'][0]['body']
    assert '云玩家  2:0  萌新' in shown


def test_running_job_cancel_does_not_load_database(tmp_path):
    async def scenario():
        started,release=asyncio.Event(),asyncio.Event()
        async def crawl(*args):
            started.set();await release.wait();return []
        load=AsyncMock()
        jobs=ComparisonJobs(tmp_path,load,client=SimpleNamespace(crawl=crawl))
        job=await jobs.start(payload());await started.wait()
        await jobs.cancel(job['id'])
        assert jobs.jobs[job['id']]['status']=='cancelled'
        load.assert_not_awaited()
        assert not jobs.result_path(job['id']).exists()
        await jobs.close()
        with pytest.raises(ComparisonError,match='正在停止'):await jobs.start(payload())
    asyncio.run(scenario())


def test_job_history_retention_is_bounded_to_twenty(tmp_path):
    async def scenario():
        jobs=ComparisonJobs(tmp_path,AsyncMock(return_value=[]),client=SimpleNamespace(crawl=AsyncMock(return_value=[])))
        oldest=None
        for n in range(21):
            job=await jobs.start(payload());await jobs.task
            if n==0:oldest=job['id']
        assert len(jobs.jobs)==len(jobs.history())==20
        assert not (tmp_path/(oldest+'.json')).exists()
        assert not (tmp_path/(oldest+'.job.json')).exists()
    asyncio.run(scenario())


def test_job_persistence_failure_does_not_start_a_crawl(tmp_path,monkeypatch):
    import report_webui
    async def scenario():
        crawl=AsyncMock()
        jobs=ComparisonJobs(tmp_path,AsyncMock(),client=SimpleNamespace(crawl=crawl))
        monkeypatch.setattr(report_webui.os,'replace',lambda *args: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):await jobs.start(payload())
        crawl.assert_not_awaited()
        assert not jobs.jobs and jobs.task is None
    asyncio.run(scenario())


def test_site_text_duplicate_key_does_not_merge_different_internal_space_ids():
    from report_site import norm_content
    assert norm_content(BODY.replace('云玩家','示例  玩家')) != norm_content(BODY.replace('云玩家','示例 玩家'))


def test_unrecognized_record_id_cannot_be_mistaken_for_empty_result():
    async def scenario():
        broken=page().replace('<a>101</a>','<a>101详情</a>')
        with pytest.raises(ComparisonError,match='部分网站记录'):
            await SiteClient(AsyncMock(return_value=broken),delay=0).crawl(ComparisonQuery.from_payload(payload()),AsyncMock())
    asyncio.run(scenario())


def test_explicit_form_fields_override_imported_url_defaults_consistently():
    query=ComparisonQuery.from_payload(payload(site_team='b队',publish_start='2026-10-02',publish_end='2026-11-01',
        source_url='https://rep.ygobbs2.com/?c=search&a=sub&groupname=a%E9%98%9F&starttime=2026-10-01&endtime=2026-10-31'))
    assert query.site_team=='b队' and query.publish_start=='2026-10-02' and query.publish_end=='2026-11-01'


def test_complete_cloud_archive_runs_before_comparison_and_archive_failure_stops_diff(tmp_path):
    async def scenario():
        events=[]
        async def archive(records,query):events.append('archive');return dict(total=1,inserted=1,refreshed=0)
        async def load(query):events.append('load');return []
        jobs=ComparisonJobs(tmp_path,load,archive=archive,client=SimpleNamespace(crawl=AsyncMock(return_value=[record()])))
        job=await jobs.start(payload());await jobs.task
        assert events==['archive','load'] and jobs.jobs[job['id']]['archive']['inserted']==1
        events.clear()
        jobs.archive=AsyncMock(side_effect=OSError('archive unavailable'))
        job=await jobs.start(payload());await jobs.task
        assert not events and jobs.jobs[job['id']]['status']=='failed'
        assert not jobs.result_path(job['id']).exists()
    asyncio.run(scenario())


def test_selection_is_resolved_from_database_and_wrong_local_team_is_rejected(tmp_path):
    local=report();local['created_at']=1
    source=record()
    cloud=dict(cloud_id=101,team_a=source['team_a'],team_b=source['team_b'],match_time=source['match_time'],
               publish_date=source['publish_date'],winner_site=source['winner_site'],publisher='示例用户',
               last_seen_at=1,body=source['body'],source_url='https://rep.ygobbs2.com/',parsed_json=json.dumps(source))
    db=SimpleNamespace(get_webui_local_reports_by_ids=AsyncMock(return_value=[local]),get_cloud_reports_by_ids=AsyncMock(return_value=[cloud]))
    plugin=SimpleNamespace(data_dir=tmp_path,context=SimpleNamespace(),db_ready=True,db=db)
    ui=ComparisonWebUI(plugin,logging.getLogger('test'))
    async def scenario():
        refs=[dict(source='local',id='1',home_team='a队',body='forged'),dict(source='cloud',id='101',body='forged')]
        rows=await ui.resolve_reports(refs)
        assert len(rows)==2 and all(r['body']!='forged' for r in rows)
        assert rows[0]['source']=='local' and rows[1]['source']=='cloud'
        db.get_webui_local_reports_by_ids.return_value=[]
        with pytest.raises(ComparisonError,match='不存在'):await ui.resolve_reports(refs)
    asyncio.run(scenario())


def test_dispatch_shutdown_failure_still_cancels_comparison_and_cleans_routes(tmp_path):
    routes=[]
    plugin=SimpleNamespace(data_dir=tmp_path,context=SimpleNamespace(register_web_api=lambda *entry:routes.append(entry),registered_web_apis=routes))
    ui=ComparisonWebUI(plugin,logging.getLogger('test'));ui.register()
    ui.dispatch.close=AsyncMock(side_effect=OSError('disk full'))
    with pytest.raises(OSError):asyncio.run(ui.close())
    assert ui.jobs.closed and not routes


@pytest.mark.parametrize('record,team,outcome,site,expected', [
    ({'team_a':'A队','team_b':'B队','winner':'A队'},'a队','win',False,True),
    ({'team_a':'A队','team_b':'B队','winner':'B队'},'a队','loss',False,True),
    ({'team_a':'A队','team_b':'B队','winner':''},'a队','loss',False,False),
    ({'team_a':'A队','team_b':'B队','winner':'无关队'},'a队','loss',False,False),
    ({'team_a':'A队','team_b':'B队','winner':'A队'},'其他队','loss',False,False),
    ({'winner':'A队','loser':'B队'},'a队','win',True,True),
    ({'winner':'A队','loser':'B队'},'b队','loss',True,True),
    ({'winner':'A队','loser':'A队'},'a队','loss',True,False),
    ({'team_a':'A队','team_b':'B队','winner':'B队'},'a队','loss',True,True),
])
def test_outcome_filter_uses_query_team_and_excludes_unknown(record,team,outcome,site,expected):
    from report_webui import outcome_matches
    assert outcome_matches(record,team,outcome,site=site) is expected


@pytest.mark.parametrize('value',['won',"win OR 1=1",None])
def test_invalid_outcome_is_rejected(value):
    from report_webui import validate_outcome
    with pytest.raises(ComparisonError,match='胜负'):
        validate_outcome(value)


def test_comparison_outcome_filters_before_paging_preserves_pairs_and_does_not_recrawl(tmp_path):
    async def scenario():
        site=dict(record(),winner_site='B队',loser_site='A队')
        reports=[report(1)]+[report(i,BODY.replace('云玩家',f'示例玩家{i}')) for i in (2,3,4)]
        reports[1]['winner']='B队'
        client=SimpleNamespace(crawl=AsyncMock(return_value=[site]))
        jobs=ComparisonJobs(tmp_path,AsyncMock(return_value=reports),client=client)
        job=await jobs.start(payload());await jobs.task
        first=await jobs.get(job['id'],'only_in_db',0,1,outcome='win')
        second=await jobs.get(job['id'],'only_in_db',1,1,outcome='win')
        assert first['total']==second['total']==2
        assert len(first['records'])==len(second['records'])==1
        assert first['records'][0]['local'][0]['id']!=second['records'][0]['local'][0]['id']
        for outcome in ('win','loss'):
            paired=await jobs.get(job['id'],'winner_mismatch',outcome=outcome)
            assert paired['total']==1
            assert paired['records'][0]['site'] and paired['records'][0]['local']
        assert first['job']['summary']['db_total']==4
        client.crawl.assert_awaited_once()
    asyncio.run(scenario())
