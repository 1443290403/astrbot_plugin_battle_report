"""SnowLuma / OneBot 呼叫全体：成功回执后冷却，失败不补发。"""
import asyncio
import copy
import json
import math
import os
import tempfile
import time
from pathlib import Path

try:
    from .official_queue import CALL_ALL_COOLDOWN, QueueSendError, call_all_failure
except ImportError:
    from official_queue import CALL_ALL_COOLDOWN, QueueSendError, call_all_failure


def cooldown_message(remaining):
    if remaining > 0:
        return f'呼叫全体冷却中，还需{remaining // 60}分{remaining % 60:02}秒喵。'
    return '呼叫全体冷却已结束，可以再次点击按钮喵。'


class OneBotCallAll:
    def __init__(self, path, *, clock=time.time, log=None):
        self.path = Path(path)
        self.clock, self.log = clock, log
        self.locks = {}
        self.state = {'version': 1, 'rooms': {}}
        if self.path.exists():
            state = json.loads(self.path.read_text('utf-8'))
            if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('rooms'), dict):
                raise ValueError('OneBot 呼叫全体存档错误')
            for key, room in state['rooms'].items():
                if (not isinstance(room, dict) or not isinstance(room.get('scope'), list)
                        or len(room['scope']) != 3 or not all(isinstance(v, str) and v for v in room['scope'])
                        or self.key(room['scope']) != key
                        or type(room.get('until')) not in (int, float)
                        or not math.isfinite(room['until']) or room['until'] < 0
                        or not isinstance(room.get('seen'), list)
                        or any(not isinstance(v, str) for v in room['seen'])):
                    raise ValueError('OneBot 呼叫全体存档错误')
            self.state = state

    @staticmethod
    def key(scope):
        return json.dumps(list(scope), ensure_ascii=False, separators=(',', ':'))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self.state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remaining(self, scope):
        room = self.state['rooms'].get(self.key(scope), {})
        return max(0, math.ceil(room.get('until', 0) - self.clock()))

    async def execute(self, scope, message_id, send, *, status_only=False):
        """返回提示或None；status_only只查询状态，send必须返回OneBot消息回执。"""
        key = self.key(scope)
        async with self.locks.setdefault(key, asyncio.Lock()):
            room = self.state['rooms'].setdefault(key, {'scope': list(scope), 'until': 0, 'seen': []})
            if not message_id:
                return '呼叫全体缺少消息编号，请重新发送指令喵。'
            if message_id in room['seen']:
                return None
            previous = copy.deepcopy(room)
            room['seen'] = (room['seen'] + [message_id])[-200:]
            try:
                self.save()
            except BaseException:
                self.state['rooms'][key] = previous
                raise
            remaining = math.ceil(room['until'] - self.clock())
            if remaining > 0 or status_only:
                return cooldown_message(max(0, remaining))
            try:
                result = await send()
                if (not isinstance(result, dict)
                        or result.get('status') == 'failed' or result.get('retcode', 0) != 0):
                    raise QueueSendError(result)
                data = result.get('data', result)
                receipt = data.get('message_id') if isinstance(data, dict) else None
                if (isinstance(receipt, bool) or not isinstance(receipt, (int, str))
                        or not str(receipt).lstrip('-').isascii() or not str(receipt).lstrip('-').isdigit()
                        or int(receipt) == 0):
                    raise QueueSendError(result)
            except Exception as error:
                if self.log:
                    self.log.exception('OneBot 呼叫全体发送失败，未进入冷却且不自动重试')
                return call_all_failure(error)
            # 确认发送后即保留运行期冷却，磁盘失败也不得再次发送。
            room['until'] = self.clock() + CALL_ALL_COOLDOWN
            try:
                self.save()
            except Exception:
                if self.log:
                    self.log.exception('OneBot 呼叫全体已发送，冷却保存失败，当前进程保留冷却')
            return None
