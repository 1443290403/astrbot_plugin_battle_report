"""战报平台指令权限：只管理本插件入口，不替代角色与群权限校验。"""

from functools import partial

HANDLER_COMMANDS = {
    "report_web_login": "战报登录",
    "confirm_identity": "确认关联",
    "revoke_identity": "解除官方关联",
    "request_list": "申请列表",
    "request_refresh": "刷新申请",
    "lineup_cmd": "排表",
    "bind_home": "绑定战队",
    "view_home": "查看战队",
    "set_team_admins": "设置战队管理员",
    "remove_team_admin": "移除战队管理员",
    "view_team_admins": "查看战队管理员",
    "ban_group": "禁群",
    "enable_group": "启群",
    "check_group": "查群",
    "list_teams": "战队列表",
    "list_groups": "群列表",
    "set_chat_type": "群聊属性",
    "broadcast": "通告",
    "auth": "我的ID",
    "search_id": "查ID",
    "bind_id": "绑定ID",
    "unbind_id": "解绑ID",
    "admin_id": "管理ID",
    "my_record": "我的战绩",
    "rename_me": "改名",
    "record_cmd": "记录",
    "submit_report": "发送",
    "ranking": "排行",
    "settle": "结算",
    "reset_settle": "重置结算",
    "record": "战绩",
    "raid": "踢馆",
    "trend": "趋势",
    "export": "导出",
    "mark_export_sent": "标记已发送",
    "mark_export_unsent": "标记未发送",
    "export_group_members": "导出群成员",
    "delete": "战报删除",
    "undo": "战报撤销",
    "help_cmd": "帮助",
    "round_cmd": "第N轮",
    "review_request": "申请审批"
}

COMMAND_ALIASES = {
    "战报登录": "战报登录",
    "/战报登录": "战报登录",
    "确认关联": "确认关联",
    "/确认关联": "确认关联",
    "解除官方关联": "解除官方关联",
    "/解除官方关联": "解除官方关联",
    "申请列表": "申请列表",
    "/申请列表": "申请列表",
    "刷新申请": "刷新申请",
    "/刷新申请": "刷新申请",
    "排表": "排表",
    "/排表": "排表",
    "绑定战队": "绑定战队",
    "/绑定战队": "绑定战队",
    "查看战队": "查看战队",
    "/查看战队": "查看战队",
    "设置战队管理员": "设置战队管理员",
    "/设置战队管理员": "设置战队管理员",
    "移除战队管理员": "移除战队管理员",
    "/移除战队管理员": "移除战队管理员",
    "查看战队管理员": "查看战队管理员",
    "/查看战队管理员": "查看战队管理员",
    "禁群": "禁群",
    "/禁群": "禁群",
    "启群": "启群",
    "/启群": "启群",
    "查群": "查群",
    "/查群": "查群",
    "战队列表": "战队列表",
    "/战队列表": "战队列表",
    "群列表": "群列表",
    "/群列表": "群列表",
    "群聊属性": "群聊属性",
    "/群聊属性": "群聊属性",
    "通告": "通告",
    "/通告": "通告",
    "我的ID": "我的ID",
    "/我的ID": "我的ID",
    "查ID": "查ID",
    "/查ID": "查ID",
    "绑定ID": "绑定ID",
    "/绑定ID": "绑定ID",
    "解绑ID": "解绑ID",
    "/解绑ID": "解绑ID",
    "管理ID": "管理ID",
    "/管理ID": "管理ID",
    "我的战绩": "我的战绩",
    "/我的战绩": "我的战绩",
    "改名": "改名",
    "/改名": "改名",
    "记录": "记录",
    "/记录": "记录",
    "发送": "发送",
    "/战报": "发送",
    "/发送": "发送",
    "战报": "发送",
    "排行": "排行",
    "战报排行": "排行",
    "/排行": "排行",
    "/战报排行": "排行",
    "结算": "结算",
    "/结算": "结算",
    "重置结算": "重置结算",
    "/重置结算": "重置结算",
    "战绩": "战绩",
    "/战绩": "战绩",
    "战报战绩": "战绩",
    "/战报战绩": "战绩",
    "踢馆": "踢馆",
    "战报踢馆": "踢馆",
    "/踢馆": "踢馆",
    "/战报踢馆": "踢馆",
    "趋势": "趋势",
    "/趋势": "趋势",
    "/战报趋势": "趋势",
    "战报趋势": "趋势",
    "导出": "导出",
    "/战报导出": "导出",
    "战报导出": "导出",
    "/导出": "导出",
    "标记已发送": "标记已发送",
    "/标记已发送": "标记已发送",
    "标记未发送": "标记未发送",
    "/标记未发送": "标记未发送",
    "导出群成员": "导出群成员",
    "/导出群成员": "导出群成员",
    "战报删除": "战报删除",
    "/战报删除": "战报删除",
    "战报撤销": "战报撤销",
    "/战报撤销": "战报撤销",
    "帮助": "帮助",
    "/战报帮助": "帮助",
    "战报帮助": "帮助",
    "/帮助": "帮助"
}

QUEUE_COMMANDS = {
    '等车': '友谊等车', '蹲车': '友谊等车', '我也要等车': '进队等车', '跑路了再见': '离队',
    '人齐了发车': '发送群号', '再开一车': '再开一车', '解散队伍': '解散',
    '等车ID': '等车ID', '等车帮助': '等车帮助', '呼叫全体': '呼叫全体',
}
OFFICIAL_PLATFORMS = {'qq_official', 'qq_official_webhook'}
ONEBOT_ONLY = {'确认关联', '解除官方关联', '申请审批', '申请列表', '刷新申请', '导出群成员','战报登录'}
COMMANDS = tuple(dict.fromkeys([*HANDLER_COMMANDS.values(), *QUEUE_COMMANDS.values()]))


def handler_command(handler):
    # 注册全名在实例绑定和显示名修改后保持不变，优先使用它识别入口。
    module = getattr(handler, 'handler_module_path', None)
    full_name = getattr(handler, 'handler_full_name', None)
    if module and isinstance(full_name, str) and full_name.startswith(str(module) + '_'):
        command = HANDLER_COMMANDS.get(full_name[len(str(module)) + 1:])
        if command:
            return command
    # StarManager 用 partial(raw_handler, plugin_instance) 绑定处理器；
    # partial 没有 __name__，装饰器还可能再通过 __wrapped__ 包装一层。
    function = getattr(handler, 'handler', None)
    seen = set()
    while function is not None and id(function) not in seen:
        seen.add(id(function))
        if isinstance(function, partial):
            function = function.func
        elif getattr(function, '__wrapped__', None) is not None:
            function = function.__wrapped__
        else:
            break
    name = getattr(function, '__name__', None)
    return HANDLER_COMMANDS.get(name) or HANDLER_COMMANDS.get(getattr(handler, 'handler_name', None))


def match_command(text):
    for alias, command in COMMAND_ALIASES.items():
        if text == alias or text.startswith(alias + ' '):
            return command
    return None


def matched_profile(config, platform_id):
    profiles = config.get('platform_command_profiles', [])
    if not isinstance(profiles, list) or not platform_id:
        return None
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        ids = profile.get('platform_ids', [])
        if isinstance(ids, list) and platform_id in {str(v).strip() for v in ids}:
            return profile
    return None


def command_allowed(config, platform, platform_id, command):
    if command not in COMMANDS:
        return False
    if command in QUEUE_COMMANDS.values() and platform not in OFFICIAL_PLATFORMS:
        return False
    if command in ONEBOT_ONLY and platform != 'aiocqhttp':
        return False
    profile = matched_profile(config, platform_id)
    if profile is None:
        return platform not in OFFICIAL_PLATFORMS or command in QUEUE_COMMANDS.values()
    mode = profile.get('mode', '仅开放勾选指令')
    if mode == '全部开放':
        return True
    if mode != '仅开放勾选指令':
        return False
    allowed = profile.get('allowed_commands', [])
    return isinstance(allowed, list) and command in allowed
