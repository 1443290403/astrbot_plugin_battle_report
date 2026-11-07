"""AstrBot 后台战报核对、独立云端归档及QQ手动批量发送。"""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

try:
    from .report_site import ComparisonError, ComparisonQuery, SiteClient, previous_month
    from .report_comparison import build_comparison
except ImportError:
    from report_site import ComparisonError, ComparisonQuery, SiteClient, previous_month
    from report_comparison import build_comparison

PLUGIN_NAME = 'astrbot_plugin_battle_report'


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

    async def start(self, payload):
        query = ComparisonQuery.from_payload(payload)
        async with self.lock:
            if self.closed:
                raise ComparisonError('插件正在停止，请重载后再试。')
            if self.task and not self.task.done():
                raise ComparisonError('已有对比正在运行，请等待完成或取消。')
            job_id = uuid4().hex
            job = dict(id=job_id, status='pending', created_at=timestamp(), query=asdict(query),
                       page=0, pages=0, records=0, message='准备抓取网站战报。')
            self.save(job)
            self.jobs[job_id] = job
            # 仅保留最近20次本功能的结果，避免无限积累整月原文。
            previous = [old for old in self.history_all() if old['id'] != job_id]
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

    async def get(self, job_id, category='matched', offset=0, limit=30):
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
            output.update(meta=result['meta'], notes=result['notes'], total=selected['count'],
                          records=selected['records'][offset:offset + limit])
        return output


class ComparisonWebUI:
    def __init__(self, plugin, log):
        self.plugin = plugin
        self.jobs = ComparisonJobs(plugin.data_dir / 'report_comparisons', self.load_reports, log=log,
                                   archive=self.archive_cloud)
        self.log = log
        try:
            from .report_dispatch import QQReportDispatch
        except ImportError:
            from report_dispatch import QQReportDispatch
        self.dispatch = QQReportDispatch(plugin.data_dir / 'report_dispatches', plugin.context,
                                         self.resolve_reports, self.admin_ids, log=log)

    async def load_reports(self, query):
        if not self.plugin.db_ready or not self.plugin.db:
            raise ComparisonError('插件数据库尚未连接。')
        return await self.plugin.db.get_comparison_reports(query.team, query.start, query.end)

    def require_db(self):
        if not self.plugin.db_ready or not self.plugin.db:
            raise ComparisonError('插件数据库尚未连接。')

    async def archive_cloud(self, records, query):
        self.require_db()
        return await self.plugin.db.archive_cloud_reports(records, query.site_team, query.url())

    async def admin_ids(self, team):
        self.require_db()
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
            found[('cloud', int(row['cloud_id']), '')] = self.cloud_record(row)
        if any(key not in found for key in keys):
            raise ComparisonError('部分战报已不存在，或云端记录尚未归档；请刷新列表／重新拉取后再选。')
        return [found[key] for key in keys]

    def register(self):
        endpoints = [('options', self.options, ['GET']), ('jobs', self.start, ['POST']),
                     ('jobs/<job_id>', self.get, ['GET']), ('jobs/<job_id>/cancel', self.cancel, ['POST']),
                     ('jobs/<job_id>/download', self.download, ['GET']),
                     ('local-reports', self.local_reports, ['GET']), ('cloud-reports', self.cloud_reports, ['GET']),
                     ('dispatch/options', self.dispatch_options, ['GET']), ('dispatch/admins', self.dispatch_admins, ['GET']),
                     ('dispatch/prepare', self.dispatch_prepare, ['POST']), ('dispatch/<batch_id>', self.dispatch_get, ['GET']),
                     ('dispatch/<batch_id>/send', self.dispatch_send, ['POST']),
                     ('dispatch/<batch_id>/cancel', self.dispatch_cancel, ['POST'])]
        for suffix, handler, methods in endpoints:
            self.plugin.context.register_web_api(f'/{PLUGIN_NAME}/comparison/{suffix}', handler, methods, '战报核对、归档与QQ发送')

    async def close(self):
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
                    teams = await self.plugin.db.get_all_teams()
                except Exception:
                    self.log.exception('战报对比无法读取战队列表，保留历史结果入口')
                    ready = False
            return dict(teams=teams, month=previous_month(), ready=ready,
                        source='https://rep.ygobbs2.com/', history=self.jobs.history())
        return await self.respond(handle)

    async def start(self):
        async def handle(request):
            if not self.plugin.db_ready:
                raise ComparisonError('插件数据库尚未连接。')
            return await self.jobs.start(await request.json(default={}))
        return await self.respond(handle)

    async def get(self, job_id):
        async def handle(request):
            try:
                offset, limit = int(request.query.get('offset', '0')), int(request.query.get('limit', '30'))
            except (TypeError, ValueError) as exc:
                raise ComparisonError('分页参数无效。') from exc
            if offset < 0 or not 1 <= limit <= 100:
                raise ComparisonError('分页参数无效。')
            return await self.jobs.get(job_id, request.query.get('category', 'matched'), offset, limit)
        return await self.respond(handle)

    async def cancel(self, job_id):
        return await self.respond(lambda request: self.jobs.cancel(job_id))

    async def download(self, job_id):
        from astrbot.api.web import request, file_response, error_response
        if not request.username:
            return error_response('请先登录 AstrBot 后台。', status_code=401)
        try:
            path = self.jobs.result_path(job_id)
            if self.jobs.jobs[job_id]['status'] != 'completed' or not path.is_file():
                raise ComparisonError('这次对比尚无可下载结果。')
            return file_response(path, filename=f'battle-report-comparison-{job_id}.json', content_type='application/json')
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
            kind = request.query.get('kind', 'all')
            if kind not in ('all', 'friendly', 'raid'):
                raise ComparisonError('战报类型无效。')
            result = await self.plugin.db.get_webui_local_reports(query.team, query.start, query.end, kind, offset, limit)
            return dict(total=result['total'], records=[self.local_record(r) for r in result['records']],
                        offset=offset, limit=limit, query=asdict(query))
        return await self.respond(handle)

    async def cloud_reports(self):
        async def handle(request):
            self.require_db()
            query, offset, limit = self.page_params(request)
            result = await self.plugin.db.get_cloud_reports(query.start, query.end, query.site_team, offset, limit)
            return dict(total=result['total'], records=[self.cloud_record(r) for r in result['records']],
                        offset=offset, limit=limit, query=asdict(query))
        return await self.respond(handle)

    async def dispatch_options(self):
        async def handle(request):
            teams = await self.plugin.db.get_dispatch_admin_teams() if self.plugin.db_ready and self.plugin.db else []
            return dict(connections=self.dispatch.connections(), admin_teams=teams,
                        history=self.dispatch.history(request.username))
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
            return await self.dispatch.prepare(await request.json(default={}), request.username)
        return await self.respond(handle)

    async def dispatch_get(self, batch_id):
        async def handle(request):
            plan = self.dispatch.get(batch_id, request.username)
            return self.dispatch.public(plan, bodies=plan['status'] == 'prepared' or request.query.get('preview') == '1')
        return await self.respond(handle)

    async def dispatch_send(self, batch_id):
        async def handle(request):
            payload = await request.json(default={})
            if not isinstance(payload, dict) or type(payload.get('retry', False)) is not bool:
                raise ComparisonError('发送参数无效。')
            return await self.dispatch.confirm(batch_id, request.username, retry=payload.get('retry', False))
        return await self.respond(handle)

    async def dispatch_cancel(self, batch_id):
        return await self.respond(lambda request: self.dispatch.cancel(batch_id, request.username))
