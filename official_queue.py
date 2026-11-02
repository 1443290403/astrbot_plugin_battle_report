"""QQ 官方机器人等车队列：状态持久化、独立计时、按钮和可靠通知。"""
import asyncio
import copy
import html
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

try:
    from .command_args import parse_command_args, single_name
except ImportError:
    from command_args import parse_command_args, single_name

REFRESH = 180
GATHER_TIMEOUT = 540
READY_TIMEOUT = 900
PLATFORMS = {'qq_official', 'qq_official_webhook'}
COMMANDS = ('人齐了发车', '跑路了再见', '我也要等车', '再开一车', '等车帮助', '等车ID', '等车', '蹲车')
HELP = ('等车流程喵～\n/等车：开车或查看队列\n/再开一车：新增独立队列\n'
        '/我也要等车 [车号]：加入\n/跑路了再见 [车号]：离开\n'
        '/人齐了发车 [车号] 群号：至少3人可发车\n/等车ID 角色或参赛ID：关联原账号\n'
        '首次关联请用原 QQ 向原机器人发送 /确认关联 确认码，10分钟有效。\n'
        '每3分钟刷新；不足3人等待9分钟解散，满3人后15分钟未发车解散。\n'
        '一人同时只等一辆车；点击发车按钮后补上群号再发送喵。')


def parse_command(text):
    match = re.fullmatch(r'\s*/?(' + '|'.join(COMMANDS) + r')(?:\s+(.*))?\s*', text, re.S)
    return (match[1], (match[2] or '').strip()) if match else None


def safe(text):
    text = html.escape(str(text).replace('\n', ' ').replace('\r', ' '), quote=True)
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', text)


def mention(uid):
    return f'<qqbot-at-user id="{html.escape(str(uid), quote=True)}" />'


def keyboard(car):
    rows = []
    for buttons in [
        [('join', '我也要等车', f'/我也要等车 {car["id"]}', True),
         ('leave', '跑路了再见', f'/跑路了再见 {car["id"]}', True)],
        [('go', '人齐了发车', f'/人齐了发车 {car["id"]} ', False),
         ('new', '再开一车', '/再开一车', True)],
        [('help', '等车帮助', '/等车帮助', True)],
    ]:
        rows.append({'buttons': [
            {'id': f'{car["id"]}_{key}',
             'render_data': {'label': label, 'visited_label': label, 'style': 1},
             'action': {'type': 2, 'permission': {'type': 2}, 'data': data,
                        'enter': enter, 'reply': True}}
            for key, label, data, enter in buttons]})
    return {'content': {'rows': rows}}


class WaitingQueues:
    """send(scope, text, keyboard, reply) 与 profile/bind 回调均由插件注入。"""

    def __init__(self, path, send, profile, bind, *, proactive=True, clock=time.time, log=None):
        self.path = Path(path)
        self.send = send
        self.profile = profile
        self.bind = bind
        self.proactive = proactive
        self.clock = clock
        self.log = log
        self.locks = {}
        self.state = {'version': 1, 'rooms': {}}
        if self.path.exists():
            state = json.loads(self.path.read_text('utf-8'))
            if state.get('version') != 1 or not isinstance(state.get('rooms'), dict):
                raise ValueError('等车存档格式错误')
            for key, room in state['rooms'].items():
                if (not isinstance(room, dict) or not isinstance(room.get('scope'), list)
                        or len(room['scope']) != 3 or self.key(room['scope']) != key
                        or not isinstance(room.get('cars'), list) or not isinstance(room.get('outbox'), list)
                        or not isinstance(room.get('bindings'), dict) or not isinstance(room.get('seen'), list)
                        or type(room.get('next_number')) is not int or room['next_number'] < 1):
                    raise ValueError('等车存档格式错误')
                for car in room['cars']:
                    if (not isinstance(car.get('id'), str) or not car['id'].startswith('C')
                            or type(car.get('number')) is not int or not isinstance(car.get('members'), dict)
                            or type(car.get('ready')) is not bool):
                        raise ValueError('等车存档格式错误')
                    if any(not isinstance(member, dict) or not isinstance(member.get('name'), str)
                           for member in car['members'].values()):
                        raise ValueError('等车存档成员错误')
                    times = ([car.get('deadline'), car.get('next_refresh')]
                             + [member.get('joined_at') for member in car['members'].values()])
                    for value in times:
                        if not isinstance(value, (int, float)) or not math.isfinite(value):
                            raise ValueError('等车存档计时错误')
                for notice in room['outbox']:
                    if (not isinstance(notice, dict) or not isinstance(notice.get('id'), str)
                            or not isinstance(notice.get('text'), str) or not isinstance(notice.get('users'), list)
                            or not isinstance(notice.get('retry_at'), (int, float))
                            or not math.isfinite(notice['retry_at'])):
                        raise ValueError('等车待发提醒格式错误')
            self.state = state

    @staticmethod
    def key(scope):
        return json.dumps(list(scope), ensure_ascii=False, separators=(',', ':'))

    def lock(self, scope):
        return self.locks.setdefault(self.key(scope), asyncio.Lock())

    @contextmanager
    def edit(self):
        before = copy.deepcopy(self.state)
        try:
            yield
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, filename = tempfile.mkstemp(prefix='.waiting-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(self.state, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(filename, self.path)
            finally:
                if os.path.exists(filename):
                    os.unlink(filename)
        except BaseException:
            # edit 中没有 await；只还原实际变化的群，保留其他群正在发送时持有的引用。
            rooms = self.state['rooms']
            old_rooms = before['rooms']
            for key in set(rooms) | set(old_rooms):
                if key not in old_rooms:
                    rooms.pop(key, None)
                elif key not in rooms:
                    rooms[key] = old_rooms[key]
                elif rooms[key] != old_rooms[key]:
                    rooms[key].clear()
                    rooms[key].update(old_rooms[key])
            raise

    def room(self, scope):
        return self.state['rooms'][self.key(scope)]

    @staticmethod
    def notify(room, text, users):
        room['outbox'].append({'id': uuid4().hex, 'text': text,
                               'users': list(users), 'retry_at': 0})

    def expire(self, room, now):
        expired = [car for car in room['cars'] if now >= car['deadline']]
        if not expired:
            return
        with self.edit():
            for car in expired:
                room['cars'].remove(car)
                why = '满员后长时间未发车' if car['ready'] else '未凑满3人'
                self.notify(room, f'{car["number"]}号车{why}，已自动解散喵。', car['members'])

    @staticmethod
    def update_timer(car, now):
        ready = len(car['members']) >= 3
        if ready != car['ready']:
            car['ready'] = ready
            car['deadline'] = now + (READY_TIMEOUT if ready else GATHER_TIMEOUT)

    @staticmethod
    def select(room, target, uid):
        if target:
            found = [q for q in room['cars'] if q['id'] == target or str(q['number']) == target]
        else:
            found = [q for q in room['cars'] if uid in q['members']]
            if not found and len(room['cars']) == 1:
                found = room['cars']
        if len(found) != 1:
            raise ValueError('请 /等车 查看并选择车号喵。' if not target else '这辆车已经结束了喵，请 /等车 查看。')
        return found[0]

    async def emit(self, scope, room, text, buttons=None):
        now = self.clock()
        reply = {}
        passive = room.get('reply_id') and now - room['reply_at'] < 290 and room['reply_seq'] < 4
        if not passive and not self.proactive:
            raise RuntimeError('当前没有可用的被动回复额度，请开通群主动消息权限')
        with self.edit():
            if passive:
                room['reply_seq'] += 1
                reply = {'msg_id': room['reply_id'], 'msg_seq': room['reply_seq']}
        # 不在模块内重试单次接口请求；终态提醒失败后留在 outbox。
        result = await self.send(scope, text, buttons, reply)
        if result is None or (isinstance(result, dict) and (result.get('code', 0) != 0
                               or result.get('retcode', 0) != 0 or result.get('status') == 'failed')):
            raise RuntimeError('QQ 官方消息发送未确认')

    async def flush(self, scope, room, force=False):
        for notice in list(room['outbox']):
            if not force and notice['retry_at'] > self.clock():
                continue
            text = ' '.join(mention(uid) for uid in notice['users']) + '\n' + notice['text']
            try:
                await self.emit(scope, room, text)
            except Exception:
                with self.edit():
                    for live in room['outbox']:
                        if live['id'] == notice['id']:
                            live['retry_at'] = self.clock() + REFRESH
                if self.log:
                    self.log.exception('QQ 官方等车通知发送失败，保留待发提醒')
            else:
                with self.edit():
                    room['outbox'] = [n for n in room['outbox'] if n['id'] != notice['id']]

    async def panel(self, scope, room, car):
        now = self.clock()
        async def row(uid, member):
            try:
                display, rate = await asyncio.wait_for(
                    self.profile(scope, uid, room['bindings'].get(uid)), timeout=8)
            except Exception:
                display, rate = '', '暂不可用'
            name = display or member['name'] or f'车友{uid[-4:]}'
            waited = max(0, int(now - member['joined_at']))
            return f'**{safe(name)}** ｜ 等待{waited // 60}分{waited % 60:02}秒\n当月胜率：{safe(rate)}'
        rows = await asyncio.gather(*(row(uid, member) for uid, member in car['members'].items()))
        remaining = max(0, int(car['deadline'] - now))
        state = '等待发车' if car['ready'] else '等待凑满3人'
        text = (f'## {car["number"]}号车 · 当前等车列表 | {len(car["members"])}人\n'
                f'{state}，剩余{remaining // 60}分{remaining % 60:02}秒\n每3分钟刷新\n\n'
                + '\n\n'.join(rows or ['暂时还没有成员喵。']))
        await self.emit(scope, room, text, keyboard(car))

    async def execute(self, scope, action, raw, uid, name, msg_id):
        async with self.lock(scope):
            key = self.key(scope)
            if key not in self.state['rooms']:
                with self.edit():
                    self.state['rooms'][key] = {'scope': list(scope), 'cars': [], 'next_number': 1,
                                               'bindings': {}, 'seen': [], 'outbox': []}
            room = self.room(scope)
            if msg_id and msg_id in room['seen']:
                return
            with self.edit():
                if msg_id:
                    room.update(reply_id=msg_id, reply_at=self.clock(), reply_seq=0)
                    room['seen'] = (room['seen'] + [msg_id])[-200:]
            self.expire(room, self.clock())
            await self.flush(scope, room, force=True)
            try:
                args = [arg.value for arg in parse_command_args(raw)]
                if action == '等车帮助':
                    await self.emit(scope, room, HELP)
                    return
                if action == '等车ID':
                    query = single_name(raw)
                    if not query:
                        raise ValueError('用法喵：/等车ID 参赛ID')
                    binding = await self.bind(scope, uid, query)
                    if binding.get('pending'):
                        await self.emit(scope, room, binding['message'])
                        return
                    with self.edit():
                        room['bindings'][uid] = binding
                    await self.emit(scope, room, f'账号已关联「{safe(query)}」啦喵。' if binding.get('linked')
                                    else f'等车战绩设为「{safe(query)}」啦喵。')
                    return
                selected = None
                with self.edit():
                    if action in ('等车', '蹲车', '再开一车'):
                        if args:
                            raise ValueError('用 /等车 查看，或 /再开一车 新增队列喵。')
                        if action == '再开一车' or not room['cars']:
                            own = any(uid in q['members'] for q in room['cars'])
                            now = self.clock()
                            car = dict(id=f'C{room["next_number"]}_' + uuid4().hex[:8].upper(), number=room['next_number'],
                                       members={} if own else {uid: dict(name=name, joined_at=now)},
                                       ready=False, deadline=now + GATHER_TIMEOUT, next_refresh=now + REFRESH)
                            room['next_number'] += 1
                            room['cars'].append(car)
                            selected = [car['id']]
                    elif action in ('我也要等车', '跑路了再见'):
                        if len(args) > 1:
                            raise ValueError('只需指定一个车号喵。')
                        car = self.select(room, args[0] if args else None, uid)
                        if action == '我也要等车':
                            if any(uid in q['members'] and q['id'] != car['id'] for q in room['cars']):
                                raise ValueError('你已经在另一辆车里了喵，请先离队。')
                            if uid not in car['members']:
                                car['members'][uid] = dict(name=name, joined_at=self.clock())
                                self.update_timer(car, self.clock())
                        else:
                            if uid not in car['members']:
                                raise ValueError('你没有在这辆车里喵。')
                            del car['members'][uid]
                            if not car['members']:
                                room['cars'].remove(car)
                            else:
                                self.update_timer(car, self.clock())
                        selected = [car['id']]
                    elif action == '人齐了发车':
                        if len(args) not in (1, 2) or not re.fullmatch(r'[1-9][0-9]{4,11}', args[-1]):
                            raise ValueError('用法喵：/人齐了发车 [车号] 群号')
                        car = self.select(room, args[0] if len(args) == 2 else None, uid)
                        if uid not in car['members']:
                            raise ValueError('只有本车成员可以发车喵。')
                        if len(car['members']) < 3:
                            raise ValueError('至少凑满3人才能发车喵。')
                        room['cars'].remove(car)
                        self.notify(room, f'{car["number"]}号车发车啦！群号：{args[-1]}\n'
                                    f'发车者：{safe(car["members"][uid]["name"] or "本车成员")}，快来集合喵～',
                                    [user for user in car['members'] if user != uid])
                        selected = []
                    else:
                        raise ValueError('请用 /等车帮助 查看指令喵。')
                # 异常时对应群会还原；发送前重新取当前 room。
                room = self.room(scope)
                await self.flush(scope, room)
                cars = room['cars'] if selected is None else [q for q in room['cars'] if q['id'] in selected]
                for car in cars:
                    await self.panel(scope, room, car)
                if not cars and action == '跑路了再见':
                    await self.emit(scope, room, '已离队，本车已解散喵。')
            except ValueError as exc:
                await self.emit(scope, self.room(scope), str(exc))

    async def tick(self):
        # 不让一个群的网络等待延迟其他群的解散时刻。
        async def tick_room(scope):
            async with self.lock(scope):
                room = self.room(scope)
                self.expire(room, self.clock())
                await self.flush(scope, room)
                for car in list(room['cars']):
                    if car['next_refresh'] > self.clock():
                        continue
                    with self.edit():
                        car['next_refresh'] = self.clock() + REFRESH
                    try:
                        await self.panel(scope, room, car)
                    except Exception:
                        if self.log:
                            self.log.exception('QQ 官方等车面板刷新失败')
        results = await asyncio.gather(*(tick_room(tuple(room['scope']))
                                        for room in list(self.state['rooms'].values())), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and self.log:
                self.log.error('QQ 官方等车定时状态处理失败: %s', type(result).__name__)
