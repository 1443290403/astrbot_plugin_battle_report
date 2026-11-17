"""QQ 可信客户端互动回调接入；不增加公开路由，不生成用户消息。"""
import asyncio
import inspect
import re
import time

try:
    from .official_queue import CALL_ALL_CALLBACK, PLATFORMS
except ImportError:
    from official_queue import CALL_ALL_CALLBACK, PLATFORMS


def field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def owns(interaction):
    resolved = field(field(interaction, 'data'), 'resolved')
    return field(resolved, 'button_data') == CALL_ALL_CALLBACK


def callback_identity(platform, interaction):
    """只接受本应用的群内消息按钮；从d.id取互动ID，不用网关event_id。"""
    resolved = field(field(interaction, 'data'), 'resolved')
    if (not owns(interaction) or field(resolved, 'button_id') not in (None, 'call_all')
            or field(interaction, 'type') != 11
            or field(interaction, 'scene') not in (None, 'group')
            or field(interaction, 'chat_type') not in (None, 1)
            or (field(interaction, 'scene') != 'group' and field(interaction, 'chat_type') != 1)):
        raise ValueError('不是本插件的群内呼叫按钮')
    appid = str(platform.appid)
    received_appid = field(interaction, 'application_id')
    if received_appid is not None and str(received_appid) != appid:
        raise ValueError('按钮回调AppID与原连接不一致')
    values = [field(interaction, key) for key in ('id', 'group_openid', 'group_member_openid')]
    if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', v) for v in values):
        raise ValueError('按钮回调缺少有效的互动ID、群OpenID或成员OpenID')
    return values


def mapped_group(config, platform_id, group_openid):
    mappings = config.get('official_queue_call_all_routes', [])
    if not isinstance(mappings, list):
        raise ValueError('呼叫全体群映射配置格式错误')
    matches = [row for row in mappings if isinstance(row, dict) and row.get('enabled', True) is True
               and row.get('official_platform_id') == platform_id
               and row.get('official_group_openid') == group_openid]
    if len(matches) != 1:
        raise ValueError('请在呼叫全体群映射中为当前官方连接和群配置唯一的SnowLuma目标')
    row = matches[0]
    target = row.get('onebot_platform_id')
    if not isinstance(target, str) or not target.strip():
        raise ValueError('呼叫全体群映射缺少SnowLuma连接ID')
    for key in ('onebot_self_id', 'onebot_group_id'):
        if not isinstance(row.get(key), str) or not re.fullmatch(r'[1-9][0-9]{4,19}', row[key]):
            raise ValueError('呼叫全体群映射的机器人QQ和QQ群号须填写纯数字')
    return target, row['onebot_self_id'], row['onebot_group_id']


class QueueCallbackHooks:
    """按客户端实例挂接SDK回调，其他按钮继续交给原处理器。"""
    def __init__(self, context, handler, log):
        self.context, self.handler, self.log = context, handler, log
        self.bindings, self.tasks, self.acknowledged = {}, set(), {}
        self.closed = False

    def sync(self):
        if self.closed:
            return
        current = set()
        for platform in getattr(getattr(self.context, 'platform_manager', None), 'platform_insts', []):
            if platform.meta().name not in PLATFORMS:
                continue
            client = getattr(platform, 'client', None)
            if client is None:
                continue
            key = id(client)
            current.add(key)
            if key in self.bindings:
                continue
            previous = getattr(client, 'on_interaction_create', None)
            wrapper = self.listener(platform, previous)
            self.bindings[key] = (client, previous, wrapper)
            client.on_interaction_create = wrapper
            intents = getattr(client, 'intents', None)
            if platform.meta().name == 'qq_official' and type(intents) is int and not intents & (1 << 26):
                client.intents = intents | (1 << 26)
                if getattr(client, '_connection', None) is not None:
                    self.log.warning('呼叫全体已挂接互动回调；请重启QQ官方连接或AstrBot，使INTERACTION订阅生效')
        for key in set(self.bindings) - current:
            self.restore(key)

    def listener(self, platform, previous):
        async def callback(interaction):
            if not owns(interaction):
                if callable(previous):
                    result = previous(interaction)
                    if inspect.isawaitable(result):
                        await result
                return
            if self.closed:
                return
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                await self.handler(platform, interaction)
            except Exception:
                self.log.exception('QQ等车按钮回调处理失败，未自动重试')
            finally:
                self.tasks.discard(task)
        return callback

    async def acknowledge(self, platform, interaction_id, code):
        # QQ规定同一互动只回应一次；失败也不在重复投递中自动重试。
        now = time.monotonic()
        self.acknowledged = {k: t for k, t in self.acknowledged.items() if now - t < 600}
        key = (str(platform.appid), interaction_id)
        if key in self.acknowledged:
            return False
        self.acknowledged[key] = now
        result = await asyncio.wait_for(platform.client.api.on_interaction_result(
            interaction_id=interaction_id, code=code), timeout=3)
        if (result is not None and not isinstance(result, dict)) or (isinstance(result, dict) and (
                result.get('status') == 'failed'
                or any(result.get(k, 0) != 0 for k in ('code', 'err_code', 'retcode')))):
            raise RuntimeError('QQ互动回应未确认')
        return True

    def restore(self, key):
        client, previous, wrapper = self.bindings.pop(key)
        if getattr(client, 'on_interaction_create', None) is wrapper:
            if previous is None:
                del client.on_interaction_create
            else:
                client.on_interaction_create = previous

    async def close(self):
        self.closed = True
        for key in list(self.bindings):
            self.restore(key)
        tasks = [t for t in self.tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
