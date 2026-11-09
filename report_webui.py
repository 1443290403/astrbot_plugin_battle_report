"""AstrBot 后台战报核对、独立云端归档及QQ手动批量发送。"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

try:
    from .report_site import ComparisonError, ComparisonQuery, SiteClient, previous_month
    from .report_comparison import build_comparison
    from .web_accounts import WebAuthError
    from .friendly_audit import daily_friendly_limits
except ImportError:
    from report_site import ComparisonError, ComparisonQuery, SiteClient, previous_month
    from report_comparison import build_comparison
    from web_accounts import WebAuthError
    from friendly_audit import daily_friendly_limits

PLUGIN_NAME = 'astrbot_plugin_battle_report'


class PermissionDenied(ComparisonError):
    pass


def team_key(value):
    return str(value or '').strip().upper()


def validate_outcome(value):
    if value not in ('all', 'win', 'loss'):
        raise ComparisonError('胜负筛选无效。')
    return value


def outcome_matches(record, team, outcome, *, site=False):
    """以查询战队看整场胜负；不把未知胜负或未参与的记录算作负场。"""
    team = str(team or '').upper()
    winner = str(record.get('winner') or '').upper()
    sides = {str(record.get(key) or '').upper() for key in ('team_a', 'team_b')} - {''}
    loser = str(record.get('loser') or '').upper() if site else ''
    if site and loser:
        return bool(team and winner and loser != winner and
                    (winner == team if outcome == 'win' else loser == team))
    if team not in sides or winner not in sides:
        return False
    return winner == team if outcome == 'win' else winner != team


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.comparison-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ComparisonJobs:
    def __init__(self, path, load_reports, *, client=None, log=None, archive=None):
        self.path = Path(path)
        self.load_reports = load_reports
        self.client = client or SiteClient()
        self.log = log
        self.archive = archive
        self.jobs = {}
        self.task = None
        self.active_id = None
        self.closed = False
        self.lock = asyncio.Lock()
        for file in sorted(self.path.glob('*.job.json')):
            if not re.fullmatch(r'[0-9a-f]{32}\.job\.json', file.name):
                continue
            try:
                job = json.loads(file.read_text('utf-8'))
                if (not isinstance(job, dict) or not isinstance(job.get('query'), dict)
                        or not isinstance(job.get('created_at'), str)
                        or job.get('status') not in ('pending', 'running', 'completed', 'failed', 'cancelled', 'interrupted')
                        or job['id'] + '.job.json' != file.name):
                    continue
                ComparisonQuery.from_payload(job['query'])
                if job['status'] in ('pending', 'running'):
                    job.update(status='interrupted', message='重启中断了上次对比，请重新开始。', finished_at=timestamp())
                    atomic_json(file, job)
                self.jobs[job['id']] = job
            except (ValueError, KeyError, OSError):
                if log:
                    log.warning('忽略无效的战报对比任务存档: %s', file.name)

    def result_path(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id) or job_id not in self.jobs:
            raise ComparisonError('找不到这次对比。')
        return self.path / (job_id + '.json')

    def save(self, job):
        atomic_json(self.path / (job['id'] + '.job.json'), job)

    def history(self):
        return sorted(self.jobs.values(), key=lambda j: j['created_at'], reverse=True)[:20]

    async def start(self, payload, *, owner=None):
        query = ComparisonQuery.from_payload(payload)
        async with self.lock:
            if self.closed:
                raise ComparisonError('插件正在停止，请重载后再试。')
            if self.task and not self.task.done():
                raise ComparisonError('已有对比正在运行，请等待完成或取消。')
            job_id = uuid4().hex
            job = dict(id=job_id, status='pending', created_at=timestamp(), query=asdict(query),
                       page=0, pages=0, records=0, message='准备抓取网站战报。')
            if owner is not None:job['owner']=owner
            self.save(job)
            self.jobs[job_id] = job
            # 仅保留最近20次本功能的结果，避免无限积累整月原文。
            previous = [old for old in self.history_all() if old['id'] != job_id and old.get('owner')==owner]
            for old in previous[19:]:
                self.result_path(old['id']).unlink(missing_ok=True)
                (self.path / (old['id'] + '.job.json')).unlink(missing_ok=True)
                self.jobs.pop(old['id'])
            self.active_id = job_id
            self.task = asyncio.create_task(self.run(job, query))
            return dict(job)

    def history_all(self):
        return sorted(self.jobs.values(), key=lambda j: j['created_at'], reverse=True)

    async def run(self, job, query):
        try:
            async with asyncio.timeout(300):
                job.update(status='running', message='正在抓取网站战报。')
                self.save(job)
                async def progress(page, pages, count):
                    job.update(page=page, pages=pages, records=count,
                               message=f'正在抓取第 {page}/{pages} 页，已读取 {count} 条。')
                    self.save(job)
                site = await self.client.crawl(query, progress)
                if self.archive:
                    job['message'] = '正在保存云端战报。'
                    self.save(job)
                    job['archive'] = await self.archive(site, query)
                job['message'] = '正在读取本地战报并核对。'
                self.save(job)
                reports = await self.load_reports(query)
                if len(reports) > 10000:
                    raise ComparisonError('本地记录超过10000条，请缩短查询日期。')
                meta = dict(asdict(query), source_url=query.url(), finished_at=timestamp(), archive=job.get('archive'),
                            scope_note='网站按发布时间查询，本地按正文比赛日期查询。')
                result = await asyncio.to_thread(build_comparison, site, reports, meta)
                await asyncio.to_thread(atomic_json, self.result_path(job['id']), result)
                job.update(status='completed', message='对比完成。', summary=result['summary'],
                           categories=[{k: c[k] for k in ('key', 'label', 'count', 'note')} for c in result['categories']],
                           finished_at=timestamp())
        except asyncio.CancelledError:
            job.update(status='cancelled', message='对比已取消，未生成差异结果。', finished_at=timestamp())
            self.result_path(job['id']).unlink(missing_ok=True)
            raise
        except TimeoutError:
            job.update(status='failed', message='本次查询超过5分钟，请缩短日期范围后重试。', finished_at=timestamp())
        except Exception as exc:
            job.update(status='failed', message=str(exc) if isinstance(exc, ComparisonError)
                       else '对比未完成，请查看 AstrBot 日志后重试。', finished_at=timestamp())
            if self.log:
                self.log.exception('战报站对比失败，未发布差异结果')
        finally:
            try:
                self.save(job)
            finally:
                if self.active_id == job['id']:
                    self.active_id = None

    async def cancel(self, job_id):
        self.result_path(job_id)
        if self.active_id != job_id or not self.task or self.task.done():
            raise ComparisonError('这次对比已经结束。')
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        job = self.jobs[job_id]
        if job['status'] in ('pending', 'running'):
            job.update(status='cancelled', message='对比已取消，未生成差异结果。', finished_at=timestamp())
            self.save(job)
        self.active_id = None
        return dict(self.jobs[job_id])

    async def close(self):
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def get(self, job_id, category='matched', offset=0, limit=30, *, outcome='all'):
        validate_outcome(outcome)
        path = self.result_path(job_id)
        job = dict(self.jobs[job_id])
        output = dict(job=job, records=[], total=0, offset=offset, limit=limit)
        if job['status'] == 'completed':
            try:
                result = await asyncio.to_thread(lambda: json.loads(path.read_text('utf-8')))
            except (OSError, ValueError) as exc:
                raise ComparisonError('这次对比结果无法读取，请重新运行。') from exc
            selected = next((c for c in result['categories'] if c['key'] == category), None)
            if selected is None:
                raise ComparisonError('未知的差异类型。')
            records = selected['records']
            if outcome != 'all':
                records = [row for row in records if any(
                    outcome_matches(record, job['query']['site_team'] if side == 'site' else job['query']['team'],
                                    outcome, site=side == 'site')
                    for side in ('site', 'local') for record in row[side])]
            output.update(meta=result['meta'], notes=result['notes'], outcome=outcome,
                          total=selected['count'] if outcome == 'all' else len(records),
                          records=records[offset:offset + limit])
        return output


class ComparisonWebUI:
    def __init__(self, plugin, log):
        self.plugin = plugin
        self.team_actor = ContextVar('battle_report_team_actor', default=None)
        self.jobs = ComparisonJobs(plugin.data_dir / 'report_comparisons', self.load_reports, log=log,
                                   archive=self.archive_cloud)
        self.log = log
        try:
            from .report_dispatch import QQReportDispatch
        except ImportError:
            from report_dispatch import QQReportDispatch
        self.dispatch = QQReportDispatch(plugin.data_dir / 'report_dispatches', plugin.context,
                                         self.resolve_reports, self.admin_ids, log=log,authorize=self.authorize_dispatch)
        self.auth = None
        try:
            try:from .web_accounts import WebAccounts
            except ImportError:from web_accounts import WebAccounts
            self.auth = WebAccounts(plugin.data_dir/'web_accounts.json',self.admin_teams)
        except Exception:
            self.log.exception('战队管理员登录初始化失败，原后台登录继续可用')
        try:from .web_management import WebManagement
        except ImportError:from web_management import WebManagement
        self.management=WebManagement(self)

    @contextmanager
    def bind_team(self, actor):
        token=self.team_actor.set(actor)
        try:yield
        finally:self.team_actor.reset(token)

    async def admin_teams(self, qq):
        if not self.plugin.db_ready or not self.plugin.db:
            try:from .web_accounts import WebAuthError
            except ImportError:from web_accounts import WebAuthError
            raise WebAuthError('插件数据库尚未连接。',503)
        return await self.plugin.db.get_admin_teams(qq)

    async def sync_accounts(self, qqs=None):
        self.require_db()
        if qqs is None:qqs=await self.plugin.db.get_team_admin_qqs()
        return await self.require_auth().ensure_accounts(qqs)

    async def scope(self):
        actor=self.team_actor.get()
        if actor is None:return None
        teams=await self.admin_teams(actor['qq'])
        if not teams:raise PermissionDenied('当前QQ没有战队管理员权限。')
        return {team_key(t) for t in teams}

    async def require_team(self, team):
        teams=await self.scope()
        if teams is not None and team_key(team) not in teams:
            raise PermissionDenied('没有该战队的访问权限。')

    async def require_query(self, query):
        teams=await self.scope()
        if teams is not None and not {team_key(query.team),team_key(query.site_team)}.issubset(teams):
            raise PermissionDenied('没有该战队的访问权限。')

    async def require_job(self, job_id):
        self.jobs.result_path(job_id)
        job=self.jobs.jobs[job_id]
        query=ComparisonQuery.from_payload(job['query'])
        await self.require_query(query)
        return job

    @staticmethod
    def related_teams(row):
        return {team_key(row.get(k)) for k in ('home_team','team_a','team_b','winner_site','loser_site','query_team')
                if row.get(k)}

    async def authorize_dispatch(self, plan):
        qq=plan.get('team_qq')
        if not qq:return
        teams={team_key(t) for t in await self.admin_teams(qq)}
        if not teams:raise PermissionDenied('当前QQ没有战队管理员权限。')
        for report in plan['reports']:
            allowed=({team_key(report.get('home_team'))} if report['source']=='local'
                     else {team_key(t) for t in report.get('access_teams',[])})
            if not allowed.intersection(teams):raise PermissionDenied('该批次包含无权访问的战报。')
        if plan['target_mode']=='admins' and team_key(plan.get('admin_team')) not in teams:
            raise PermissionDenied('没有该战队管理员名单的访问权限。')

    async def require_plan(self, plan):
        actor=self.team_actor.get()
        if actor is not None:
            if plan.get('team_qq')!=actor['qq']:raise PermissionDenied('没有该批次的访问权限。')
            await self.authorize_dispatch(plan)

    def require_master(self):
        if self.team_actor.get() is not None:raise PermissionDenied('此操作需要 AstrBot 后台管理员登录。')

    def require_auth(self):
        if self.auth is None:raise ComparisonError('战队管理员登录暂不可用，请查看日志。')
        return self.auth

    @staticmethod
    def secure_passwords(request):
        try:
            try:from .standalone_web import StandaloneFrontend
            except ImportError:from standalone_web import StandaloneFrontend
            raw=getattr(request,'_request',None) or getattr(request,'raw',None)
            if raw is None:return False
            StandaloneFrontend.require_secure(raw)
            return True
        except WebAuthError:return False

    async def load_reports(self, query):
        if not self.plugin.db_ready or not self.plugin.db:
            raise ComparisonError('插件数据库尚未连接。')
        await self.require_query(query)
        return await self.plugin.db.get_comparison_reports(query.team, query.start, query.end)

    def require_db(self):
        if not self.plugin.db_ready or not self.plugin.db:
            raise ComparisonError('插件数据库尚未连接。')

    async def archive_cloud(self, records, query):
        self.require_db()
        await self.require_query(query)
        return await self.plugin.db.archive_cloud_reports(records, query.site_team, query.url())

    async def admin_ids(self, team):
        self.require_db()
        await self.require_team(team)
        return await self.plugin.db.get_team_admins(team)

    @staticmethod
    def local_record(report):
        try:
            from .lineup import report_to_text
        except ImportError:
            from lineup import report_to_text
        return dict(source='local', id=str(report['match_id']), home_team=report['home_team'],
                    team_a=report['team_a'], team_b=report['team_b'], match_time=report['match_time'],
                    rule=report['rule'], location=report['location'], winner=report['winner'],
                    kind=report['kind'], publisher=report['submitted_name'],
                    uploaded_at=report.get('created_at'), export_sent=report['export_sent'], body=report_to_text(report))

    @staticmethod
    def cloud_record(row):
        try:
            data = json.loads(row['parsed_json'])
        except (TypeError, ValueError):
            data = {}
        return dict(source='cloud', id=str(row['cloud_id']), home_team='', team_a=row['team_a'], team_b=row['team_b'],
                    match_time=str(row['match_time'] or ''), publish_date=str(row['publish_date'] or ''),
                    rule=data.get('rule', ''), location=data.get('location', ''), winner=row['winner_site'],
                    kind='friendly' if data.get('parse_ok') else 'unknown', publisher=row['publisher'],
                    uploaded_at=int(row['last_seen_at']), body=row['body'], source_url=row['source_url'],
                    errors=data.get('parse_errors', []), warnings=data.get('parse_warnings', []))

    async def resolve_reports(self, refs):
        self.require_db()
        teams=await self.scope()
        keys = []
        local_groups = {}
        clouds = []
        for ref in refs:
            if not isinstance(ref, dict) or ref.get('source') not in ('local', 'cloud'):
                raise ComparisonError('战报来源无效。')
            raw = str(ref.get('id', ''))
            maximum = 2**64 if ref['source'] == 'cloud' else 2**63
            if not re.fullmatch(r'[1-9][0-9]{0,19}', raw) or not 0 < int(raw) < maximum:
                raise ComparisonError('战报编号无效。')
            source, rid = ref['source'], int(raw)
            team = str(ref.get('home_team') or '').strip() if source == 'local' else ''
            if source == 'local' and (not team or len(team) > 64):
                raise ComparisonError('本地战报缺少所属战队。')
            if source=='local' and teams is not None and team_key(team) not in teams:
                raise PermissionDenied('没有该战队的访问权限。')
            key = (source, rid, team)
            if key in keys:
                continue
            keys.append(key)
            if source == 'local':
                local_groups.setdefault(team, []).append(rid)
            else:
                clouds.append(rid)
        found = {}
        for team, ids in local_groups.items():
            for row in await self.plugin.db.get_webui_local_reports_by_ids(team, ids):
                found[('local', int(row['match_id']), team)] = self.local_record(row)
        for row in await self.plugin.db.get_cloud_reports_by_ids(clouds):
            allowed=self.related_teams(row)
            if teams is not None and not allowed.intersection(teams):
                raise PermissionDenied('所选云端战报不属于当前授权战队。')
            record=self.cloud_record(row)
            if teams is not None:record['access_teams']=sorted(allowed.intersection(teams))
            found[('cloud', int(row['cloud_id']), '')] = record
        if any(key not in found for key in keys):
            raise ComparisonError('部分战报已不存在，或云端记录尚未归档；请刷新列表／重新拉取后再选。')
        return [found[key] for key in keys]

    def endpoints(self):
        return [('options', self.options, ['GET']), ('jobs', self.start, ['POST']),
                     ('jobs/<job_id>', self.get, ['GET']), ('jobs/<job_id>/cancel', self.cancel, ['POST']),
                     ('jobs/<job_id>/download', self.download, ['GET']),
                     ('local-reports', self.local_reports, ['GET']), ('cloud-reports', self.cloud_reports, ['GET']),
                     ('friendly-limits', self.friendly_limits, ['GET']),
                     ('friendly-limits/reports', self.friendly_limit_reports, ['GET']),
                     ('dispatch/options', self.dispatch_options, ['GET']), ('dispatch/admins', self.dispatch_admins, ['GET']),
                     ('dispatch/prepare', self.dispatch_prepare, ['POST']), ('dispatch/<batch_id>', self.dispatch_get, ['GET']),
                     ('dispatch/<batch_id>/send', self.dispatch_send, ['POST']),
                     ('dispatch/<batch_id>/cancel', self.dispatch_cancel, ['POST']),
                     ('accounts',self.accounts,['GET']),('accounts',self.account_sync,['POST']),
                     ('accounts/<account_id>',self.account_update,['POST']),
                     ('management/teams',self.management_teams,['GET']),('management/teams',self.management_team_save,['POST']),
                     ('management/members',self.management_members,['GET']),('management/members',self.management_member_save,['POST']),
                     ('management/players',self.management_players,['GET']),('management/players',self.management_player_save,['POST'])]

    def register(self):
        for suffix, handler, methods in self.endpoints():
            self.plugin.context.register_web_api(f'/{PLUGIN_NAME}/comparison/{suffix}', handler, methods, '战报管理、核对与QQ发送')

    async def close(self):
        if self.auth:self.auth.close()
        try:
            await self.dispatch.close()
        finally:
            try:
                await self.jobs.close()
            finally:
                registered = getattr(self.plugin.context, 'registered_web_apis', None)
                if isinstance(registered, list):
                    registered[:] = [entry for entry in registered if getattr(entry[1], '__self__', None) is not self]

    async def respond(self, handler):
        from astrbot.api.web import request, json_response, error_response
        if not request.username:
            return error_response('请先登录 AstrBot 后台。', status_code=401)
        try:
            return json_response(await handler(request))
        except PermissionDenied as exc:
            return error_response(str(exc),status_code=403)
        except WebAuthError as exc:
            return error_response(str(exc),status_code=exc.status)
        except ComparisonError as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            self.log.exception('战报对比 WebUI 请求失败')
            return error_response('暂时无法完成请求，请查看 AstrBot 日志。', status_code=500)

    async def options(self):
        async def handle(request):
            ready = bool(self.plugin.db_ready and self.plugin.db)
            teams = []
            if ready:
                try:
                    actor=self.team_actor.get()
                    teams = await self.admin_teams(actor['qq']) if actor else await self.plugin.db.get_all_teams()
                except Exception:
                    self.log.exception('战报对比无法读取战队列表，保留历史结果入口')
                    ready = False
            allowed={team_key(t) for t in teams}
            history=[]
            for job in self.jobs.history_all():
                query=ComparisonQuery.from_payload(job['query'])
                if not self.team_actor.get() or (team_key(query.team) in allowed and team_key(query.site_team) in allowed):
                    history.append(job)
                if len(history)==20:break
            return dict(teams=teams, month=previous_month(), ready=ready,
                        current_month=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m'),
                        source='https://rep.ygobbs2.com/', history=history,
                        role='team' if self.team_actor.get() else 'master',
                        secure_passwords=self.secure_passwords(request))
        return await self.respond(handle)

    async def start(self):
        async def handle(request):
            if not self.plugin.db_ready:
                raise ComparisonError('插件数据库尚未连接。')
            payload=await request.json(default={})
            await self.require_query(ComparisonQuery.from_payload(payload))
            actor=self.team_actor.get()
            return await self.jobs.start(payload,owner=actor['owner'] if actor else None)
        return await self.respond(handle)

    async def get(self, job_id):
        async def handle(request):
            await self.require_job(job_id)
            try:
                offset, limit = int(request.query.get('offset', '0')), int(request.query.get('limit', '30'))
            except (TypeError, ValueError) as exc:
                raise ComparisonError('分页参数无效。') from exc
            if offset < 0 or not 1 <= limit <= 100:
                raise ComparisonError('分页参数无效。')
            return await self.jobs.get(job_id, request.query.get('category', 'matched'), offset, limit,
                                       outcome=validate_outcome(request.query.get('outcome', 'all')))
        return await self.respond(handle)

    async def cancel(self, job_id):
        async def handle(request):
            await self.require_job(job_id)
            return await self.jobs.cancel(job_id)
        return await self.respond(handle)

    async def download(self, job_id):
        from astrbot.api.web import request, file_response, error_response
        if not request.username:
            return error_response('请先登录 AstrBot 后台。', status_code=401)
        try:
            await self.require_job(job_id)
            path = self.jobs.result_path(job_id)
            if self.jobs.jobs[job_id]['status'] != 'completed' or not path.is_file():
                raise ComparisonError('这次对比尚无可下载结果。')
            return file_response(path, filename=f'battle-report-comparison-{job_id}.json', content_type='application/json')
        except PermissionDenied as exc:
            return error_response(str(exc),status_code=403)
        except WebAuthError as exc:
            return error_response(str(exc),status_code=exc.status)
        except ComparisonError as exc:
            return error_response(str(exc), status_code=400)

    @staticmethod
    def page_params(request):
        try:
            offset, limit = int(request.query.get('offset', '0')), int(request.query.get('limit', '30'))
        except (ValueError, TypeError) as exc:
            raise ComparisonError('分页参数无效。') from exc
        if offset < 0 or not 1 <= limit <= 100:
            raise ComparisonError('分页参数无效。')
        query = ComparisonQuery.from_payload(dict(team=request.query.get('team', ''),
                    month=request.query.get('month', ''), site_team=request.query.get('site_team', '')))
        return query, offset, limit

    async def local_reports(self):
        async def handle(request):
            self.require_db()
            query, offset, limit = self.page_params(request)
            await self.require_query(query)
            kind = request.query.get('kind', 'all')
            if kind not in ('all', 'friendly', 'raid'):
                raise ComparisonError('战报类型无效。')
            outcome = validate_outcome(request.query.get('outcome', 'all'))
            result = await self.plugin.db.get_webui_local_reports(query.team, query.start, query.end, kind, offset, limit, outcome=outcome)
            return dict(total=result['total'], records=[self.local_record(r) for r in result['records']],
                        offset=offset, limit=limit, query=asdict(query))
        return await self.respond(handle)

    async def friendly_limits(self):
        async def handle(request):
            self.require_db()
            query, offset, limit = self.page_params(request)
            await self.require_query(query)
            only = request.query.get('only_exceeded', '1')
            if only not in ('0', '1'):
                raise ComparisonError('超限筛选无效。')
            rows = await self.plugin.db.get_daily_friendly_counts(query.team, query.start, query.end)
            result = daily_friendly_limits(rows, query.team, only_exceeded=only == '1', offset=offset, limit=limit)
            return dict(**result, query=asdict(query))
        return await self.respond(handle)

    async def friendly_limit_reports(self):
        async def handle(request):
            self.require_db()
            query, offset, limit = self.page_params(request)
            await self.require_query(query)
            day = request.query.get('day', '')
            opponent = str(request.query.get('opponent') or '').strip()
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
                    raise ValueError
                date.fromisoformat(day)
                if not query.start <= day <= query.end:
                    raise ValueError
            except (ValueError, TypeError):
                raise ComparisonError('比赛日期无效或不在所选月份内。')
            if (not opponent or len(opponent) > 64 or team_key(opponent) == team_key(query.team)
                    or any(ord(c) < 32 for c in opponent)):
                raise ComparisonError('对手战队无效。')
            result = await self.plugin.db.get_daily_friendly_reports(query.team, day, opponent, offset, limit)
            return dict(total=result['total'], records=[self.local_record(r) for r in result['records']],
                        offset=offset, limit=limit, query=asdict(query), day=day, opponent=opponent)
        return await self.respond(handle)

    async def cloud_reports(self):
        async def handle(request):
            self.require_db()
            query, offset, limit = self.page_params(request)
            await self.require_query(query)
            outcome = validate_outcome(request.query.get('outcome', 'all'))
            result = await self.plugin.db.get_cloud_reports(query.start, query.end, query.site_team, offset, limit, outcome=outcome)
            return dict(total=result['total'], records=[self.cloud_record(r) for r in result['records']],
                        offset=offset, limit=limit, query=asdict(query))
        return await self.respond(handle)

    async def dispatch_options(self):
        async def handle(request):
            actor=self.team_actor.get()
            teams = (await self.admin_teams(actor['qq']) if actor else await self.plugin.db.get_dispatch_admin_teams()) if self.plugin.db_ready and self.plugin.db else []
            plans=[p for p in self.dispatch.plans.values() if p['owner']==request.username]
            visible=[]
            for plan in sorted(plans,key=lambda p:p['created_at'],reverse=True):
                try:await self.require_plan(plan)
                except PermissionDenied:continue
                visible.append(self.dispatch.public(plan))
                if len(visible)==20:break
            return dict(connections=self.dispatch.connections(), admin_teams=teams,
                        history=visible)
        return await self.respond(handle)

    async def dispatch_admins(self):
        async def handle(request):
            team = str(request.query.get('team', '')).strip()
            if not team or len(team) > 64:
                raise ComparisonError('请选择目标战队。')
            return dict(team=team, admins=await self.admin_ids(team))
        return await self.respond(handle)

    async def dispatch_prepare(self):
        async def handle(request):
            actor=self.team_actor.get()
            return await self.dispatch.prepare(await request.json(default={}), request.username,team_qq=actor['qq'] if actor else None)
        return await self.respond(handle)

    async def dispatch_get(self, batch_id):
        async def handle(request):
            plan = self.dispatch.get(batch_id, request.username)
            await self.require_plan(plan)
            return self.dispatch.public(plan, bodies=plan['status'] == 'prepared' or request.query.get('preview') == '1')
        return await self.respond(handle)

    async def dispatch_send(self, batch_id):
        async def handle(request):
            await self.require_plan(self.dispatch.get(batch_id,request.username))
            payload = await request.json(default={})
            if not isinstance(payload, dict) or type(payload.get('retry', False)) is not bool:
                raise ComparisonError('发送参数无效。')
            return await self.dispatch.confirm(batch_id, request.username, retry=payload.get('retry', False))
        return await self.respond(handle)

    async def dispatch_cancel(self, batch_id):
        async def handle(request):
            await self.require_plan(self.dispatch.get(batch_id,request.username))
            return await self.dispatch.cancel(batch_id,request.username)
        return await self.respond(handle)

    async def accounts(self):
        async def handle(request):
            self.require_master()
            return dict(accounts=self.require_auth().list_accounts())
        return await self.respond(handle)

    async def management_teams(self):return await self.respond(self.management.teams)
    async def management_team_save(self):return await self.respond(self.management.team_save)
    async def management_members(self):return await self.respond(self.management.members)
    async def management_member_save(self):return await self.respond(self.management.member_save)
    async def management_players(self):return await self.respond(self.management.players)
    async def management_player_save(self):return await self.respond(self.management.player_save)

    async def account_sync(self):
        async def handle(request):
            self.require_master()
            payload=await request.json(default={})
            if not isinstance(payload,dict) or payload!={'action':'sync'}:
                raise ComparisonError('账号随战队管理员自动创建，请使用同步操作。')
            created=await self.sync_accounts()
            return dict(created=len(created),accounts=self.require_auth().list_accounts())
        return await self.respond(handle)

    async def account_update(self,account_id):
        async def handle(request):
            self.require_master()
            if not self.secure_passwords(request):raise WebAuthError('请通过HTTPS查看或修改账号密码。',403)
            payload=await request.json(default={})
            if not isinstance(payload,dict):raise ComparisonError('账号参数无效。')
            if payload.get('action')=='initial':
                auth=self.require_auth()
                account=auth.accounts.get(account_id)
                if account is None:raise ComparisonError('找不到该密码账号。')
                return await auth.credentials(account['qq'])
            if payload.get('action')=='delete':
                raise ComparisonError('账号随管理员名单自动创建，请停用密码登录或撤销战队管理员授权。')
            await self.require_auth().update(account_id,payload)
            return dict(updated=True)
        return await self.respond(handle)
