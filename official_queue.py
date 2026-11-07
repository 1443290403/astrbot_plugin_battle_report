"""QQ 官方机器人友谊等车：状态持久化、独立计时、按钮和可靠通知。"""
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

try:
    from .command_args import parse_command_args, single_name
except ImportError:
    from command_args import parse_command_args, single_name

REFRESH = 240
GATHER_TIMEOUT = READY_TIMEOUT = 600
CALL_ALL_COOLDOWN = 1800
PLATFORMS = {'qq_official', 'qq_official_webhook'}
COMMANDS = ('友谊等车帮助', '友谊等车', '进队等车', '发送群号', '解散队伍', '离队', '解散', '人齐了发车', '跑路了再见',
            '我也要等车', '再开一车', '等车帮助', '等车ID', '等车', '蹲车', '呼叫全体')
_LEGACY_GROUP_NUMBER_RE = re.compile(
    r'^(?P<heading>[0-9]+号车发车啦！)群号：<qqbot-cmd-input text="(?P<number>[1-9][0-9]{4,11})" '
    r'show="(?P=number)" reference="false" />\n')
HELP = ('友谊等车流程喵～\n/友谊等车：开车或查看所有队伍\n/再开一车：新增队伍\n'
        '/进队等车 [车号]：加入\n/离队：退出当前队伍\n'
        '/发送群号 群号：通知同车成员并结束该车\n/解散：解散当前队伍\n'
        '/呼叫全体：@全体成员摇人友谊，同群冷却30分钟\n'
        '入队后自动定位你的队伍，不用填写车号。\n'
        '/等车ID 角色或参赛ID：关联原账号，首次用原 QQ /确认关联 确认码\n'
        '每4分钟统一广播；建队10分钟清理，首次满3人重新计10分钟并提醒接友谊。\n'
        '之后进出不延时，一人同时只在一队；车次号按北京时间每日重新从1编号喵。')


def parse_command(text):
    if re.fullmatch(r'\s*/?(?:帮助|战报帮助)\s+(?:友谊等车|等车)\s*', text):
        return '等车帮助', ''
    match = re.fullmatch(r'\s*/?(' + '|'.join(COMMANDS) + r')(?:\s+(.*))?\s*', text, re.S)
    if not match:
        return None
    action = {'友谊等车': '等车', '友谊等车帮助': '等车帮助',
              '进队等车': '我也要等车', '发送群号': '人齐了发车',
              '离队': '跑路了再见', '解散': '解散队伍'}.get(match[1], match[1])
    return action, (match[2] or '').strip()


def day_key(now):
    return datetime.fromtimestamp(now, timezone(timedelta(hours=8))).strftime('%Y-%m-%d')


def input_text(text, show):
    # QQ 的参数指令是填入输入框，不自动发送；两个属性都须 URL 编码。
    return f'<qqbot-cmd-input text="{quote(str(text)[:100], safe="")}" show="{quote(str(show)[:100], safe="")}" reference="false" />'


def group_nickname(source, fallback=''):
    """群名片优先，未提供时按用户要求回退 QQ 昵称。"""
    def get(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    raw = get(source, 'raw_data') or source
    for obj, keys in [(get(raw, 'member'), ('nick', 'card', 'group_nickname')),
                      (get(source, 'member'), ('nick', 'card', 'group_nickname')),
                      (get(raw, 'author'), ('member_name', 'group_nickname', 'card')),
                      (get(source, 'author'), ('member_name', 'group_nickname', 'card')),
                      (raw, ('member_name', 'group_nickname', 'card'))]:
        for key in keys:
            value = get(obj, key)
            if isinstance(value, str) and value.strip():
                return value.strip().replace('\n', ' ').replace('\r', ' ')
    author = get(raw, 'author') or get(source, 'author')
    for value in (get(author, 'username'), get(author, 'nickname'), fallback):
        if isinstance(value, str) and value.strip():
            return value.strip().replace('\n', ' ').replace('\r', ' ')
    return ''


def safe(text):
    text = html.escape(str(text).replace('\n', ' ').replace('\r', ' '), quote=True)
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', text)


def mention(uid):
    return f'<qqbot-at-user id="{html.escape(str(uid), quote=True)}" />'


def avatar(appid, uid):
    """官方头像由同一应用的原始 OpenID 定位，不依赖 QQ 身份关联。"""
    url = f'https://q.qlogo.cn/qqapp/{quote(str(appid), safe="")}/{quote(str(uid), safe="")}/100'
    return f'![头像 #32px #32px]({url})'


def keyboard(car=None, *, show_call_all=True):
    # 所有队伍共用操作，每行至多三个；呼叫全体与车次无关。
    actions = [('join', '进队等车', '/进队等车', True),
               ('leave', '跑路了再见', '/离队', True),
               ('go', '发送群号', '/发送群号 ', False),
               ('new', '再开一车', '/再开一车', True),
               ('dissolve', '解散队伍', '/解散', True),
               ('help', '等车帮助', '/等车帮助', True)]
    if show_call_all:
        actions.append(('call_all', '呼叫全体', '/呼叫全体', True))
    buttons = [{'id': key, 'render_data': {'label': label, 'visited_label': label, 'style': 1},
                'action': {'type': 2, 'permission': {'type': 2}, 'data': data,
                           'enter': enter, 'reply': True, 'unsupport_tips': '请发送对应文字指令'}}
               for key, label, data, enter in actions]
    return {'content': {'rows': [{'buttons': buttons[i:i + 3]} for i in range(0, len(buttons), 3)]}}


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
        self.state = {'version': 2, 'rooms': {}}
        if self.path.exists():
            state = json.loads(self.path.read_text('utf-8'))
            if type(state.get('version')) is not int or state['version'] not in (1, 2) or not isinstance(state.get('rooms'), dict):
                raise ValueError('等车存档格式错误')
            for key, room in state['rooms'].items():
                if (not isinstance(room, dict) or not isinstance(room.get('scope'), list)
                        or len(room['scope']) != 3 or self.key(room['scope']) != key
                        or not isinstance(room.get('cars'), list) or not isinstance(room.get('outbox'), list)
                        or not isinstance(room.get('bindings'), dict) or not isinstance(room.get('seen'), list)
                        or type(room.get('next_number')) is not int or room['next_number'] < 1):
                    raise ValueError('等车存档格式错误')
                users = set()
                cooldown = room.get('call_all_until', 0)
                if type(cooldown) not in (int, float) or not math.isfinite(cooldown) or cooldown < 0:
                    raise ValueError('等车呼叫全体冷却格式错误')
                for car in room['cars']:
                    if (not isinstance(car.get('id'), str) or not car['id'].startswith('C')
                            or type(car.get('number')) is not int or car['number'] < 1
                            or not isinstance(car.get('members'), dict)
                            or type(car.get('ready')) is not bool):
                        raise ValueError('等车存档格式错误')
                    if any(not isinstance(member, dict) or not isinstance(member.get('name'), str)
                           for member in car['members'].values()):
                        raise ValueError('等车存档成员错误')
                    if users.intersection(car['members']):
                        raise ValueError('等车存档存在重复入队成员')
                    users.update(car['members'])
                    times = ([car.get('deadline')]
                             + ([car.get('next_refresh')] if state['version'] == 1 else [])
                             + [member.get('joined_at') for member in car['members'].values()])
                    for value in times:
                        if type(value) not in (int, float) or not math.isfinite(value):
                            raise ValueError('等车存档计时错误')
                for notice in room['outbox']:
                    if (not isinstance(notice, dict) or not isinstance(notice.get('id'), str)
                            or not isinstance(notice.get('text'), str) or not isinstance(notice.get('users'), list)
                            or not isinstance(notice.get('retry_at'), (int, float))
                            or not math.isfinite(notice['retry_at'])):
                        raise ValueError('等车待发提醒格式错误')
                    if ('names' in notice and (not isinstance(notice['names'], dict)
                            or any(not isinstance(name, str) for name in notice['names'].values()))):
                        raise ValueError('等车通知昵称格式错误')
                    if (notice.get('format', 'markdown') not in ('markdown', 'text')
                            or ('after' in notice and (not isinstance(notice['after'], str)
                                                       or notice['after'] == notice['id']))):
                        raise ValueError('等车通知发送格式错误')
                    if (notice.get('format') == 'text'
                            and (notice['users'] or not re.fullmatch(r'[1-9][0-9]{4,11}', notice['text']))):
                        raise ValueError('等车群号通知格式错误')
                if state['version'] == 2:
                    if (not isinstance(room.get('next_refresh'), (int, float))
                            or not math.isfinite(room['next_refresh'])
                            or not isinstance(room.get('number_day'), str)):
                        raise ValueError('等车广播或编号格式错误')
                    if any(not isinstance(car.get('day'), str) for car in room['cars']):
                        raise ValueError('等车车次日期错误')
            self.state = state
            if state['version'] == 1:
                with self.edit():
                    now = self.clock()
                    for room in state['rooms'].values():
                        room['number_day'] = day_key(now)
                        room['next_refresh'] = min([car['next_refresh'] for car in room['cars']] or [now + REFRESH])
                        for car in room['cars']:
                            start = min([m['joined_at'] for m in car['members'].values()] or [now])
                            car['day'] = day_key(start)
                            # 不复活已过期的旧车，不给旧车重新赠送一整段等待时间。
                            car['deadline'] = min(car['deadline'], now + GATHER_TIMEOUT)
                            car.pop('next_refresh', None)
                        room['next_number'] = max([c['number'] for c in room['cars'] if c['day'] == room['number_day']] or [0]) + 1
                    state['version'] = 2
            if any(_LEGACY_GROUP_NUMBER_RE.match(n['text']) for r in state['rooms'].values()
                   for n in r['outbox']):
                # 升级前尚未发出的群号也要去掉“填入指令”链接，不能补发旧格式。
                with self.edit():
                    for room in state['rooms'].values():
                        pending = []
                        for notice in room['outbox']:
                            pending.append(notice)
                            match = _LEGACY_GROUP_NUMBER_RE.match(notice['text'])
                            if match:
                                notice['text'] = match['heading'] + '\n' + notice['text'][match.end():]
                                pending.append({'id': uuid4().hex, 'text': match['number'], 'users': [],
                                                'names': {}, 'retry_at': notice['retry_at'],
                                                'format': 'text', 'after': notice['id']})
                        room['outbox'] = pending


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
    def notify(room, text, users, *, plain=False, after=None):
        notice = {'id': uuid4().hex, 'text': text,
                  'users': list(users), 'retry_at': 0,
                  'format': 'text' if plain else 'markdown',
                  'names': {uid: (member['name'] or f'车友{uid[-4:]}')
                            for uid, member in users.items()} if isinstance(users, dict) else {}}
        if after is not None:
            notice['after'] = after
        room['outbox'].append(notice)
        return notice['id']

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
        # ready 是“曾经满3人”，不能在离队后清零。
        if not car['ready'] and len(car['members']) >= 3:
            car['ready'] = True
            car['deadline'] = now + READY_TIMEOUT
            return True
        return False

    @staticmethod
    def select(room, target, uid, *, joining=False):
        if target:
            found = [q for q in room['cars'] if q['id'] == target or str(q['number']) == target]
        else:
            found = [q for q in room['cars'] if uid in q['members']]
            if not found and joining and room['cars']:
                found = [next((q for q in room['cars'] if len(q['members']) < 3), room['cars'][0])]
        if len(found) != 1:
            if target:
                raise ValueError('这辆车已经结束了喵，请 /友谊等车 查看。' if not found
                                 else '车次号有重复，请点面板选择队伍喵。')
            raise ValueError('你还没有加入队伍喵。')
        return found[0]

    async def emit(self, scope, room, text, buttons=None, *, plain=False):
        now = self.clock()
        reply = {}
        passive = room.get('reply_id') and now - room['reply_at'] < 290 and room['reply_seq'] < 4
        if not passive and not self.proactive:
            raise RuntimeError('当前没有可用的被动回复额度，请开通群主动消息权限')
        with self.edit():
            if passive:
                room['reply_seq'] += 1
                reply = {'msg_id': room['reply_id'], 'msg_seq': room['reply_seq']}
        # 不在模块内重试单次接口请求；已持久化提醒失败后留在 outbox。
        result = (await self.send(scope, text, buttons, reply, plain=True) if plain
                  else await self.send(scope, text, buttons, reply))
        if result is None or (isinstance(result, dict) and (result.get('code', 0) != 0
                               or result.get('retcode', 0) != 0 or result.get('status') == 'failed')):
            raise RuntimeError('QQ 官方消息发送未确认')

    async def flush(self, scope, room, force=False):
        for notice in list(room['outbox']):
            if any(n['id'] == notice.get('after') for n in room['outbox']):
                continue
            if not force and notice['retry_at'] > self.clock():
                continue
            plain = notice.get('format') == 'text'
            # 原生标签由 QQ 显示本群名称；不能再拼接缓存昵称或“填入指令”标签。
            labels = ' '.join(mention(uid) for uid in dict.fromkeys(notice['users']))
            text = (labels + '\n' if labels else '') + notice['text']
            try:
                await self.emit(scope, room, text, plain=plain)
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

    async def panel(self, scope, room, car=None, *, announcement=''):
        now = self.clock()
        sections = ([announcement] if announcement else []) + ['**友谊等车**']
        for car in room['cars']:
            remaining = max(0, int(car['deadline'] - now))
            cleanup = f'{remaining // 60}分{remaining % 60:02}秒'
            label = f'{car["number"]}号车' if car['day'] == day_key(now) else f'{car["day"][5:]}·{car["number"]}号车'
            title = f'**{label} · {len(car["members"])}人** ' + input_text('/进队等车 ' + car['id'], '进队等车')
            members = []
            for uid, member in car['members'].items():
                name = member['name'] or f'车友{uid[-4:]}'
                waited = max(0, int(now - member['joined_at']))
                members.append(f'{avatar(scope[1], uid)} {input_text("@" + name + " ", name)} '
                               f'{waited // 60}:{waited % 60:02}')
            sections.append(title + f'\n> 自动清理：{cleanup} ｜ 广播间隔：4分钟\n\n'
                            + '　'.join(members or ['暂无成员']))
        if not room['cars']:
            sections.append('当前没有队伍喵。')
        await self.emit(scope, room, '\n\n'.join(sections),
                        keyboard(show_call_all=now >= room.get('call_all_until', 0)))

    async def execute(self, scope, action, raw, uid, name, msg_id):
        async with self.lock(scope):
            key = self.key(scope)
            if key not in self.state['rooms']:
                with self.edit():
                    self.state['rooms'][key] = {'scope': list(scope), 'cars': [], 'next_number': 1,
                                               'number_day': day_key(self.clock()), 'next_refresh': self.clock() + REFRESH,
                                               'bindings': {}, 'seen': [], 'outbox': []}
            room = self.room(scope)
            if msg_id and msg_id in room['seen']:
                return
            with self.edit():
                if msg_id:
                    room.update(reply_id=msg_id, reply_at=self.clock(), reply_seq=0)
                    room['seen'] = (room['seen'] + [msg_id])[-200:]
                if day_key(self.clock()) != room['number_day']:
                    room['number_day'], room['next_number'] = day_key(self.clock()), 1
                if name:
                    for car in room['cars']:
                        if uid in car['members']:
                            car['members'][uid]['name'] = name
            self.expire(room, self.clock())
            await self.flush(scope, room, force=True)
            try:
                args = [arg.value for arg in parse_command_args(raw)]
                if action == '等车帮助':
                    await self.emit(scope, room, HELP)
                    return
                if action == '呼叫全体':
                    if args:
                        raise ValueError('用法喵：/呼叫全体')
                    remaining = math.ceil(room.get('call_all_until', 0) - self.clock())
                    if remaining > 0:
                        raise ValueError(f'呼叫全体冷却中，还需{remaining // 60}分{remaining % 60:02}秒喵。')
                    # 发送前落盘；即使超时、取消或重启，也不自动重试全体提醒。
                    # 与普通成员 outbox 分开，避免未知送达结果引起重复 @全体。
                    with self.edit():
                        room['call_all_until'] = self.clock() + CALL_ALL_COOLDOWN
                    try:
                        await self.panel(scope, room, announcement='<qqbot-at-everyone />\n'
                                         '来打友谊喵！发送 /友谊等车 查看或创建队伍，一起接友谊～')
                    except Exception:
                        if self.log:
                            self.log.exception('QQ 官方等车呼叫全体发送未确认，保留冷却且不自动重试')
                        await self.emit(scope, room, '呼叫全体发送未确认，请检查 @全体权限和平台额度喵。')
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
                    await self.emit(scope, room, f'账号已关联「{safe(query)}」啦喵。')
                    return
                with self.edit():
                    if action in ('等车', '蹲车', '再开一车'):
                        if args:
                            raise ValueError('用 /友谊等车 查看，或 /再开一车 新增队伍喵。')
                        if action == '再开一车' or not room['cars']:
                            own = any(uid in q['members'] for q in room['cars'])
                            now = self.clock()
                            car = dict(id=f'C{room["next_number"]}_' + uuid4().hex[:8].upper(), number=room['next_number'],
                                       day=day_key(now), owner=uid, members={} if own else {uid: dict(name=name, joined_at=now)},
                                       ready=False, deadline=now + GATHER_TIMEOUT)
                            if not room['cars']:
                                room['next_refresh'] = now + REFRESH
                            room['next_number'] += 1
                            room['cars'].append(car)
                    elif action in ('我也要等车', '跑路了再见', '解散队伍'):
                        if len(args) > 1:
                            raise ValueError('只需指定一个车号喵。')
                        car = self.select(room, args[0] if args else None, uid, joining=action == '我也要等车')
                        if action == '我也要等车':
                            if any(uid in q['members'] and q['id'] != car['id'] for q in room['cars']):
                                raise ValueError('你已经在另一队了喵，请先离队。')
                            if uid not in car['members']:
                                previous_members = dict(car['members'])
                                car['members'][uid] = dict(name=name, joined_at=self.clock())
                                if self.update_timer(car, self.clock()):
                                    self.notify(room, f'{car["number"]}号车人齐啦，快去接友谊喵！', car['members'])
                                elif previous_members:
                                    self.notify(room, f'有新成员加入了{car["number"]}号车喵。', previous_members)
                        elif action == '跑路了再见':
                            if uid not in car['members']:
                                raise ValueError('你没有在这队里喵。')
                            car['members'].pop(uid)
                            self.notify(room, f'有成员离开了{car["number"]}号车喵。', car['members'])
                            if not car['members']:
                                room['cars'].remove(car)
                        else:
                            if uid not in car['members'] and not (not car['members'] and car.get('owner') == uid):
                                raise ValueError('只有本车成员可以解散队伍喵。')
                            room['cars'].remove(car)
                            self.notify(room, f'{car["number"]}号车已解散喵。', car['members'])
                    elif action == '人齐了发车':
                        if len(args) not in (1, 2) or not re.fullmatch(r'[1-9][0-9]{4,11}', args[-1]):
                            raise ValueError('用法喵：/发送群号 群号')
                        car = self.select(room, args[0] if len(args) == 2 else None, uid)
                        if uid not in car['members']:
                            raise ValueError('只有本车成员可以发送群号喵。')
                        if len(car['members']) < 3:
                            raise ValueError('至少凑满3人才能发送群号喵。')
                        room['cars'].remove(car)
                        others = {user: member for user, member in car['members'].items() if user != uid}
                        notice_id = self.notify(room, f'{car["number"]}号车发车啦！\n'
                                               '快来集合喵～', others)
                        self.notify(room, args[-1], [], plain=True, after=notice_id)
                    else:
                        raise ValueError('请用 /等车帮助 查看指令喵。')
                room = self.room(scope)
                await self.flush(scope, room)
                await self.panel(scope, room)
            except ValueError as exc:
                await self.emit(scope, self.room(scope), str(exc))

    async def tick(self):
        async def tick_room(scope):
            async with self.lock(scope):
                room = self.room(scope)
                before = len(room['cars'])
                self.expire(room, self.clock())
                await self.flush(scope, room)
                due = room['next_refresh'] <= self.clock()
                if room['cars'] and (due or len(room['cars']) != before):
                    if due:
                        with self.edit():
                            room['next_refresh'] = self.clock() + REFRESH
                    try:
                        await self.panel(scope, room)
                    except Exception:
                        if self.log:
                            self.log.exception('QQ 官方等车面板刷新失败')
        results = await asyncio.gather(*(tick_room(tuple(room['scope']))
                                        for room in list(self.state['rooms'].values())), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and self.log:
                self.log.error('QQ 官方等车定时状态处理失败: %s', type(result).__name__)
