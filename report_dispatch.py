"""WebUI 手动准备并确认 QQ 合并转发；逐接收人/分包记录确认结果。"""
import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from uuid import uuid4

try:
    from .report_site import ComparisonError
except ImportError:
    from report_site import ComparisonError

QQ_RE = re.compile(r'[1-9][0-9]{4,11}')


def qq_ids(value):
    values = re.split(r'[\s,，;；]+', value.strip()) if isinstance(value, str) else value
    if not isinstance(values, list) or not values or len(values) > 20:
        raise ComparisonError('请指定1至20个接收QQ。')
    result = []
    for value in values:
        value = str(value).strip()
        if not QQ_RE.fullmatch(value):
            raise ComparisonError('接收QQ号格式不正确。')
        if value not in result:
            result.append(value)
    return result


def action_data(value):
    if isinstance(value, dict) and (value.get('status') == 'failed' or value.get('retcode', 0) != 0):
        raise DefiniteSendFailure('平台拒绝了这次发送，请检查好友关系及连接状态。')
    return value.get('data', value) if isinstance(value, dict) else value


class DefiniteSendFailure(ComparisonError):
    pass


class QQReportDispatch:
    def __init__(self, path, context, load_reports, get_admins, *, log=None, delay=0.5, clock=time.time):
        self.path = Path(path)
        self.context, self.load_reports, self.get_admins = context, load_reports, get_admins
        self.log, self.delay, self.clock = log, delay, clock
        self.plans = {}
        self.lock = asyncio.Lock()
        self.task = None
        self.active_id = None
        self.closed = False
        for file in self.path.glob('*.json'):
            if not re.fullmatch(r'[0-9a-f]{32}\.json', file.name):
                continue
            try:
                plan = json.loads(file.read_text('utf-8'))
                if plan['id'] + '.json' != file.name or not isinstance(plan['entries'], list):
                    continue
                if plan['status'] == 'running':
                    for entry in plan['entries']:
                        if entry['status'] == 'sending':
                            entry.update(status='unknown', error='重启前发送结果未确认，请先在QQ核实。')
                        elif entry['status'] == 'pending':
                            entry.update(status='cancelled', error='重启中断，尚未发送。')
                    plan['status'] = 'interrupted'
                    self.save(plan)
                self.plans[plan['id']] = plan
            except (OSError, ValueError, KeyError, TypeError):
                if log:
                    log.warning('忽略无效的QQ战报发送记录: %s', file.name)

    def save(self, plan):
        try:
            from .report_webui import atomic_json
        except ImportError:
            from report_webui import atomic_json
        atomic_json(self.path / (plan['id'] + '.json'), plan)

    def connections(self):
        platforms = getattr(getattr(self.context, 'platform_manager', None), 'platform_insts', [])
        rows = []
        for platform in platforms:
            meta = platform.meta()
            if meta.name != 'aiocqhttp':
                continue
            client = platform.get_client()
            clients = getattr(client, '_wsr_api_clients', {})
            accounts = [str(k) for k in clients if QQ_RE.fullmatch(str(k))] if isinstance(clients, dict) else []
            rows.append(dict(id=str(meta.id), label=str(meta.id), accounts=accounts))
        return rows

    def platform(self, platform_id):
        platform = self.context.get_platform_inst(platform_id)
        if platform is None or platform.meta().name != 'aiocqhttp':
            raise ComparisonError('请选择 SnowLuma／OneBot 发送连接。')
        return platform

    async def login(self, platform_id, self_id=''):
        client = self.platform(platform_id).get_client()
        params = {'self_id': int(self_id)} if self_id else {}
        try:
            data = action_data(await asyncio.wait_for(client.call_action('get_login_info', **params), timeout=10))
            actual = str(data.get('user_id') or '') if isinstance(data, dict) else ''
            if not QQ_RE.fullmatch(actual) or (self_id and actual != self_id):
                raise ComparisonError('无法确认发送机器人的QQ账号。')
            return client, actual
        except ComparisonError:
            raise
        except Exception as exc:
            raise ComparisonError('发送连接不可用，请检查 SnowLuma 在线状态。') from exc

    def get(self, plan_id, owner):
        plan = self.plans.get(plan_id)
        if plan is None or plan['owner'] != owner:
            raise ComparisonError('找不到这次发送批次。')
        return plan

    def public(self, plan, *, bodies=False):
        counts = {state: sum(e['status'] == state for e in plan['entries'])
                  for state in ('pending', 'sending', 'sent', 'failed', 'unknown', 'cancelled')}
        reports = [dict(r) if bodies else {k: v for k, v in r.items() if k != 'body'} for r in plan['reports']]
        return dict(id=plan['id'], status=plan['status'], created_at=plan['created_at'],
                    platform_id=plan['platform_id'], self_id=plan['self_id'], target_mode=plan['target_mode'],
                    admin_team=plan.get('admin_team', ''), recipients=plan['recipients'], reports=reports,
                    report_count=len(reports), entries=plan['entries'], counts=counts)

    def history(self, owner):
        plans = sorted((p for p in self.plans.values() if p['owner'] == owner), key=lambda p: p['created_at'], reverse=True)
        return [self.public(p) for p in plans[:20]]

    async def prepare(self, payload, owner):
        if not isinstance(payload, dict):
            raise ComparisonError('发送参数格式不正确。')
        async with self.lock:
            if self.closed:
                raise ComparisonError('插件正在停止。')
            refs = payload.get('reports')
            if not isinstance(refs, list) or not 1 <= len(refs) <= 200:
                raise ComparisonError('请勾选1至200份战报。')
            mode = payload.get('target_mode')
            team = str(payload.get('admin_team') or '').strip()
            if mode == 'custom':
                recipients = qq_ids(payload.get('qq_ids'))
            elif mode == 'admins':
                if not team or len(team) > 64:
                    raise ComparisonError('请选择目标战队。')
                current = await self.get_admins(team)
                recipients = qq_ids(payload.get('admin_ids'))
                if not set(recipients).issubset(set(current)):
                    raise ComparisonError('所选QQ不在该战队管理员名单中，请刷新后重选。')
            else:
                raise ComparisonError('请选择接收方式。')
            reports = copy.deepcopy(await self.load_reports(refs))
            if not reports or sum(len(r['body'].encode('utf-8')) for r in reports) > 8 * 1024 * 1024:
                raise ComparisonError('战报内容过多，请减少勾选数量。')
            if any(not r['body'].strip() for r in reports):
                raise ComparisonError('所选战报原文为空，请检查后重选。')
            platform_id = str(payload.get('platform_id') or '')
            self_id = str(payload.get('self_id') or '')
            if self_id and not QQ_RE.fullmatch(self_id):
                raise ComparisonError('机器人QQ账号无效。')
            _, self_id = await self.login(platform_id, self_id)
            plan = dict(id=uuid4().hex, owner=owner, status='prepared', created_at=self.clock(),
                        platform_id=platform_id, self_id=self_id, target_mode=mode, admin_team=team,
                        recipients=recipients, reports=reports, entries=[])
            for recipient in recipients:
                for index in range(0, len(reports), 50):
                    plan['entries'].append(dict(qq=recipient, start=index, count=len(reports[index:index+50]), status='pending'))
            self.save(plan)
            self.plans[plan['id']] = plan
            previous = sorted((p for p in self.plans.values() if p['id'] != plan['id']),
                              key=lambda p: p['created_at'], reverse=True)
            for old in previous[19:]:
                if old['id'] == self.active_id:
                    continue
                (self.path / (old['id'] + '.json')).unlink(missing_ok=True)
                self.plans.pop(old['id'], None)
            return self.public(plan, bodies=True)

    async def confirm(self, plan_id, owner, *, retry=False):
        async with self.lock:
            if self.closed:
                raise ComparisonError('插件正在停止。')
            plan = self.get(plan_id, owner)
            if plan['status'] == 'running':
                return self.public(plan)
            if self.task and not self.task.done():
                raise ComparisonError('已有QQ发送批次正在运行。')
            if plan['status'] != 'prepared' and not retry:
                return self.public(plan)
            if self.clock() - plan['created_at'] > (86400 if retry else 1800):
                raise ComparisonError('发送预览已过期，请重新准备。')
            if plan['target_mode'] == 'admins':
                current = await self.get_admins(plan['admin_team'])
                if not set(plan['recipients']).issubset(set(current)):
                    raise ComparisonError('战队管理员名单已变化，请重新准备发送。')
            await self.login(plan['platform_id'], plan['self_id'])
            previous = copy.deepcopy(plan)
            if retry:
                for entry in plan['entries']:
                    if entry['status'] in ('failed', 'cancelled'):
                        entry.update(status='pending', error='')
            if not any(e['status'] == 'pending' for e in plan['entries']):
                return self.public(plan)
            plan['status'] = 'running'
            try:
                self.save(plan)
            except BaseException:
                plan.clear()
                plan.update(previous)
                raise
            self.active_id = plan_id
            self.task = asyncio.create_task(self.run(plan))
            return self.public(plan)

    async def run(self, plan):
        try:
            async with asyncio.timeout(900):
                for entry in plan['entries']:
                    if entry['status'] != 'pending':
                        continue
                    entry['status'] = 'sending'
                    self.save(plan)
                    try:
                        client = self.platform(plan['platform_id']).get_client()
                        nodes = [{'type': 'node', 'data': {'uin': int(plan['self_id']), 'name': '战报系统',
                                  'content': [{'type': 'text', 'data': {'text': r['body']}}]}}
                                 for r in plan['reports'][entry['start']:entry['start']+entry['count']]]
                        receipt = action_data(await asyncio.wait_for(client.call_action(
                            'send_private_forward_msg', self_id=int(plan['self_id']),
                            user_id=int(entry['qq']), messages=nodes), timeout=25))
                        if not isinstance(receipt, dict) or not receipt.get('message_id'):
                            raise RuntimeError('消息送达结果未确认')
                        entry.update(status='sent', message_id=str(receipt['message_id']))
                    except asyncio.CancelledError:
                        entry.update(status='unknown', error='发送中断，结果未确认，请先在QQ核实。')
                        raise
                    except Exception as exc:
                        result = getattr(exc, 'result', None)
                        definite = isinstance(exc, DefiniteSendFailure) or (isinstance(result, dict)
                                   and (result.get('status') == 'failed' or result.get('retcode', 0) != 0))
                        entry.update(status='failed' if definite else 'unknown', error=(
                            '平台拒绝发送，请检查好友关系或平台日志。' if definite else '结果未确认，请先在QQ核实，系统不会自动重发。'))
                        if self.log:
                            self.log.exception('WebUI QQ战报发送未完成')
                    self.save(plan)
                    await asyncio.sleep(self.delay)
                plan['status'] = 'completed' if all(e['status'] == 'sent' for e in plan['entries']) else 'partial'
        except (asyncio.CancelledError, TimeoutError) as exc:
            for entry in plan['entries']:
                if entry['status'] == 'pending':
                    entry.update(status='cancelled', error='尚未发送。')
            plan['status'] = 'interrupted'
            if isinstance(exc, asyncio.CancelledError):
                raise
        except Exception:
            plan['status'] = 'interrupted'
            # 落盘失败后停止发送，避免丢失成功回执后继续扩大未知范围。
            for entry in plan['entries']:
                if entry['status'] == 'sending':
                    entry.update(status='unknown', error='发送记录未能确认，请先在QQ核实。')
                elif entry['status'] == 'pending':
                    entry.update(status='cancelled', error='尚未发送。')
            if self.log:
                self.log.exception('QQ战报发送批次中断')
        finally:
            try:
                self.save(plan)
            finally:
                self.active_id = None

    async def cancel(self, plan_id, owner):
        plan = self.get(plan_id, owner)
        if self.active_id != plan_id or not self.task or self.task.done():
            raise ComparisonError('发送批次已结束。')
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        if plan['status'] == 'running':
            plan['status'] = 'interrupted'
            for entry in plan['entries']:
                if entry['status'] == 'pending':
                    entry.update(status='cancelled', error='尚未发送。')
            self.save(plan)
        self.active_id = None
        return self.public(plan)

    async def close(self):
        self.closed = True
        if self.active_id and self.task and not self.task.done():
            plan = self.plans[self.active_id]
            await self.cancel(plan['id'], plan['owner'])
