"""仅在明确启用隔离测试MySQL时执行；绝不读取插件生产配置。"""
import asyncio
import os
from uuid import uuid4

import pytest
from conftest import DEFAULTS
from database import Database, SCHEMA_VERSION
from battle_report_parser import parse_battle_report
from report_site import enrich

pytestmark=pytest.mark.skipif(os.environ.get('ASTRBOT_TEST_MYSQL_ENABLE')!='1',reason='requires explicit isolated MySQL opt-in')
BODY='战队: a队 VS b队\n时间: 2026.10.10\n规则: 2/3【KOF】\n地点: 123456789\n------第一轮------\n云玩家  2:0  萌新'


def cloud(rid='1',body=BODY):
    return enrich(dict(id=rid,body=body,winner_site='A队',loser_site='B队',publish_date='2026-10-11',publisher='示例用户'),'2026-10-01','2026-10-31')


def run_db(check):
    async def run():
        name=f'astrbot_battle_report_test_{os.getpid()}_cloud_{uuid4().hex[:6]}'
        db=Database(host=DEFAULTS['mysql_host'],port=DEFAULTS['mysql_port'],user=DEFAULTS['mysql_user'],password=DEFAULTS['mysql_password'],db=name)
        await db.initialize()
        try:await check(db)
        finally:
            assert name.startswith('astrbot_battle_report_test_')
            await db._execute(f'DROP DATABASE `{name}`')
            await db.close()
    asyncio.run(run())


def test_archive_upsert_and_migration_replay_leave_local_tables_untouched():
    async def check(db):
        first=await db.archive_cloud_reports([cloud()],'a队','https://rep.ygobbs2.com/')
        assert first==dict(total=1,inserted=1,refreshed=0)
        second=await db.archive_cloud_reports([cloud(body=BODY.replace('2:0','2:1'))],'a队','https://rep.ygobbs2.com/')
        assert second['inserted']==0 and second['refreshed']==1
        rows=await db.get_cloud_reports_by_ids([1]);assert len(rows)==1 and '2:1' in rows[0]['body']
        assert (await db._query('SELECT COUNT(*) AS n FROM matches'))[0]['n']==0
        assert (await db._query('SELECT COUNT(*) AS n FROM duels'))[0]['n']==0
        await db._execute('UPDATE schema_version SET version=20')
        await db._init_schema();await db._init_schema()
        assert (await db._query('SELECT MAX(version) AS v FROM schema_version'))[0]['v']==SCHEMA_VERSION==21
        assert len(await db.get_cloud_reports_by_ids([1]))==1
    run_db(check)


def test_archive_batch_is_atomic_on_sql_failure():
    async def check(db):
        await db._execute("""CREATE TRIGGER reject_cloud BEFORE INSERT ON cloud_battle_reports FOR EACH ROW
                           BEGIN IF NEW.cloud_id=2 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='isolated rollback test'; END IF; END""")
        with pytest.raises(Exception,match='isolated rollback test'):
            await db.archive_cloud_reports([cloud('1'),cloud('2')],'a队','https://rep.ygobbs2.com/')
        assert (await db._query('SELECT COUNT(*) AS n FROM cloud_battle_reports'))[0]['n']==0
    run_db(check)


def test_concurrent_same_cloud_id_has_single_row():
    async def check(db):
        results=await asyncio.gather(*(db.archive_cloud_reports([cloud()],'a队','https://rep.ygobbs2.com/') for _ in range(3)))
        assert sum(r['inserted'] for r in results)==1
        assert (await db._query('SELECT COUNT(*) AS n FROM cloud_battle_reports'))[0]['n']==1
    run_db(check)


def test_local_pagination_and_team_scoped_id_lookup():
    async def check(db):
        ids=[]
        for name,home in [('云玩家','a队'),('遗老','a队'),('复读机','b队')]:
            body=BODY.replace('云玩家',name)
            parsed=parse_battle_report(body).report;parsed.group_id='test-group';parsed.submitted_name='示例上传者';parsed.created_at=1
            ids.append(await db.insert_report(parsed,'A队',home,body))
        result=await db.get_webui_local_reports('a队','2026-10-01','2026-10-31','all',0,1)
        assert result['total']==2 and len(result['records'])==1 and result['records'][0]['match_id']==ids[1]
        assert result['records'][0]['created_at']==1
        assert not await db.get_webui_local_reports_by_ids('a队',[ids[2]])
        await db.archive_cloud_reports([cloud()],'a队','https://rep.ygobbs2.com/')
        assert (await db.get_webui_local_reports('a队','2026-10-01','2026-10-31','all',0,30))['total']==2
        assert (await db.get_cloud_reports('2026-10-01','2026-10-31','a队',0,30))['total']==1
    run_db(check)


def test_webui_outcome_filters_sql_count_and_pages_for_local_and_cloud():
    async def check(db):
        wins=[]
        for i,(winner,kind,other_team) in enumerate([
            ('A队','friendly',False),('B队','friendly',False),('', 'friendly',False),
            ('A队','raid',False),('无关队','friendly',False),('C队','friendly',True),
        ]):
            body=BODY.replace('云玩家',f'示例玩家{i}')
            if other_team:body=body.replace('a队 VS b队','c队 VS d队')
            parsed=parse_battle_report(body).report;parsed.group_id='isolated-group'
            mid=await db.insert_report(parsed,winner,'a队',body)
            await db._execute('UPDATE matches SET kind=%s WHERE id=%s',(kind,mid))
            if winner=='A队':wins.append(mid)
        first=await db.get_webui_local_reports('a队','2026-10-01','2026-10-31','all',0,1,outcome='win')
        second=await db.get_webui_local_reports('a队','2026-10-01','2026-10-31','all',1,1,outcome='win')
        assert first['total']==second['total']==2
        assert {first['records'][0]['match_id'],second['records'][0]['match_id']}==set(wins)
        assert (await db.get_webui_local_reports('a队','2026-10-01','2026-10-31','all',0,30,outcome='loss'))['total']==1
        assert (await db.get_webui_local_reports('a队','2026-10-01','2026-10-31','friendly',0,30,outcome='win'))['total']==1
        assert (await db.get_webui_local_reports('a队','2026-10-01','2026-10-31','raid',0,30,outcome='win'))['total']==1
        rows=[cloud(str(i)) for i in range(1,5)]
        rows[1].update(winner_site='B队',loser_site='A队')
        rows[2].update(winner_site='',loser_site='')
        rows[3].update(winner_site='C队',loser_site='D队')
        await db.archive_cloud_reports(rows,'a队','https://rep.ygobbs2.com/')
        assert (await db.get_cloud_reports('2026-10-01','2026-10-31','a队',0,30))['total']==4
        assert (await db.get_cloud_reports('2026-10-01','2026-10-31','a队',0,30,outcome='win'))['total']==1
        assert (await db.get_cloud_reports('2026-10-01','2026-10-31','a队',0,30,outcome='loss'))['total']==1
    run_db(check)


def test_web_admin_team_lookup_is_qq_scoped_and_follows_revocation():
    async def check(db):
        await db.set_team_admins('a队',['20000001','20000003'])
        await db.set_team_admins('c队',['20000002','20000003'])
        assert await db.get_team_admin_qqs()==['20000001','20000002','20000003']
        assert await db.get_admin_teams('20000001')==['a队']
        assert set(await db.get_admin_teams('20000003'))=={'a队','c队'}
        assert await db.get_admin_teams("20000001' OR 1=1 --")==[]
        await db.remove_team_admin('a队','20000003')
        assert await db.get_admin_teams('20000003')==['c队']
        await db.set_team_admins('c队',['20000002'])
        assert await db.get_admin_teams('20000003')==[]
        assert await db.get_team_admin_qqs()==['20000001','20000002']
    run_db(check)
