"""面向群成员的操作流程与开通引导；不依赖平台和数据库。"""
import json

FRIENDLY_GROUP = "友谊群"
REPORT_GROUP = "战报群"
MAIN_GROUP = "主群"
GROUP_TYPES = (FRIENDLY_GROUP, REPORT_GROUP, MAIN_GROUP)


def normalize_chat_type(chat_type: str | None) -> str | None:
    """None 表示还没有明确配置有效群属性；群名称保持原有约定。"""
    value = (chat_type or "").strip()
    return value if value in GROUP_TYPES else None


def _team_arg(home_team: str | None) -> str:
    name = home_team or "KC"
    return json.dumps(name, ensure_ascii=False) if any(c.isspace() or c in '\"\'' for c in name) else name


def render_setup_guide(home_team: str | None = None, chat_type: str | None = None,
                       *, qq_id: str | None = None, required_type: str | None = None) -> str:
    """展示已完成与缺失的步骤；不替用户执行授权或改群属性。"""
    role = normalize_chat_type(chat_type)
    target = normalize_chat_type(required_type)
    team = _team_arg(home_team)
    qq = str(qq_id or "")
    admin_qq = qq if qq.isascii() and qq.isdigit() else "<管理员QQ>"
    status = f"战队：{home_team or '未绑定'}；群属性：{role or '友谊群（默认，尚未明确设置）'}"
    parts = ["🧭 本群开通流程喵～", status]
    if not home_team or not role:
        parts.append("战队查询、ID 绑定和提交等功能需要先完成下面的设置；默认友谊群可先排表、记录比分。")
    parts.append(
        "① 先确认谁来设置（已有授权可跳过）\n"
        "仅超管可以指定负责本队设置的管理员，普通群员不需要成为管理员。下列 QQ 请换成实际负责人的 QQ：\n"
        f"/设置战队管理员 {team} {admin_qq}\n"
        "超管由机器人维护者在插件配置 super_admin 中指定；普通成员请联系超管或本队管理员，QQ 群主/群管身份本身不能代替战队授权。"
    )
    parts.append(
        (f"② 战队已绑定为 {home_team}，这一步已完成喵。" if home_team else
         "② 由超管或该战队管理员在本群绑定战队\n"
         f"/绑定战队 {team}\n"
         "这里以 KC 为例，请替换成自己的真实战队名。")
    )
    if target:
        parts.append(
            "③ 由超管或本队管理员设置提交用途\n"
            f"/群聊属性 {target}\n"
            "若本群另有用途，请到本战队已设置好的战报群提交，不必改动本群。"
        )
    elif role:
        parts.append(f"③ 群属性已设为 {role}，这一步已完成喵。需要换用途时仍由超管或本队管理员设置。")
    else:
        parts.append(
            "③ 按本群用途选择一条，由超管或本队管理员执行\n"
            "/群聊属性 友谊群  → 排表、打比赛、记录比分\n"
            "/群聊属性 战报群  → 提交、改错、删除战报\n"
            "/群聊属性 主群  → 绑定 ID、查战绩、看排行和导出"
        )
    parts.append("设置完成会立即发送对应群的操作流程；之后随时发 /帮助 查看。超管可设置任意群喵～")
    return "\n\n".join(parts)


WORKFLOWS = {
    FRIENDLY_GROUP: (
        "① 比赛开始前：提供双方名单\n"
        "整段发送（示例队名和选手请替换）：\n"
        "/排表\n"
        "KC:红莲 凯撒亮 悠悠球\n"
        "DYG:老千 蓝大 红大\n"
        "默认按 KOF 排表；人头赛首行写 /排表 人头赛。机器人会生成第一轮，0:0 是尚未比赛的占位。\n\n"
        "② 第一轮打完一局：填比分\n"
        "/记录 红莲 20\n"
        "20 表示红莲 2:0 胜；也可以写 /记录 红莲 2:1。机器人会发出更新后的完整战报。\n"
        "名字带空格时：/记录 \"ice boundary\" 20\n\n"
        "③ 需要下一轮：先填完上一轮，再追加\n"
        "/第二轮\n"
        "自动从上一轮双方胜者中配对；指定对阵可写 /第二轮 红莲 蓝大，继续用 /记录 填分。后续用 /第三轮、/第四轮。\n"
        "若填错已记录比分，请手动修正完整战报并重新发到本群；/记录 主要填写尚为 0:0 的对局。\n\n"
        "④ 分出胜负：核对并交接最终战报\n"
        "确认日期、双方队名、全部轮次和比分正确，复制最终完整战报到本战队的战报群，用 /发送 提交。\n"
        "本群排表、记录和追加轮次都不会自动入库，不能只发一条比分当战报喵。\n\n"
        "下一步：/帮助 战报群 查看提交与改错流程。"
    ),
    REPORT_GROUP: (
        "① 提交前：拿到友谊群里的最终完整战报\n"
        "核对战队、日期、规则、地点、全部轮次和比分。战报必须包含本群绑定的战队，并已分出胜负。\n"
        "踢馆战报也使用同一个 /发送 入口，无需套用 KOF 排表流程。\n\n"
        "② 提交：下面两种方式选一种\n"
        "直接发送 /发送，换行粘贴完整战报；或引用本群中的战报消息，再发 /发送。\n"
        "支持一次提交多份、引用合并转发。只允许在战报群提交，私聊禁止；若开启仅管理提交，请让管理人员操作。\n\n"
        "③ 看回执：确认是否真的入库\n"
        "出现“战报记好啦”及战报 ID 才算成功，请保留 ID。批量提交要逐份查看回执，失败的先按提示修正再发。\n"
        "重复内容会拒收；核对逐局结果后，可用 /排行、/战绩 查看。\n\n"
        "④ 已入库但记错了：先删旧记录，再提交修正版\n"
        "如果是自己在本群最近提交的一份：/战报撤销\n"
        "如果不是最近一份，或是别人提交的：联系管理人员用 /战报删除 <战报ID>。\n"
        "确认撤销/删除成功后，修改原始战报，再 /发送 修正后的完整文本，并保留新的 ID。\n"
        "目前没有原地修改命令；直接再提交改过比分的战报会成为另一条记录，可能重复统计。\n\n"
        "⑤ 只想删除：删除后不用重发\n"
        "自己的最近一份用 /战报撤销；指定 ID 由管理人员 /战报删除 <战报ID>。必须回到原提交群操作。\n"
        "管理人员包括超管、本队战队管理员及具备插件管理权限的群管理/群主等；普通成员不能删别人的记录。\n"
        "删除会影响实时战绩；已结算奖金是快照，需管理人员另行重跑对应月份的结算喵。"
    ),
    MAIN_GROUP: (
        "① 先找自己的参赛 ID\n"
        "/查ID 红莲\n"
        "也可用名字的一部分模糊查找；带空格的完整 ID 用 /查ID \"ice boundary\"。\n"
        "查不到时，先确认战队是否正确，并请战报群录入包含该 ID 的战报；ID 不是靠 /绑定ID 凭空创建的。\n\n"
        "② 绑定到自己的 QQ，并检查身份\n"
        "/绑定ID \"ice boundary\"\n"
        "/我的ID\n"
        "多个 ID 可写 /绑定ID \"ice boundary\" 红莲，绑定后合并为自己的角色战绩。\n"
        "绑错自己的 ID 用 /解绑ID \"ice boundary\"；ID 已属于别人时联系管理员处理。可用 /改名 \"角色名字\" 修改显示名。\n\n"
        "③ 查看个人或战队战绩\n"
        "/我的战绩  → 汇总自己绑定的 ID\n"
        "/战绩 \"ice boundary\"  → 查指定玩家\n"
        "/战绩  → 本战队总战绩\n"
        "默认本月，命令末尾加 七月/7月 可查该月，例如 /我的战绩 七月。\n\n"
        "④ 看排行、走势与踢馆成绩\n"
        "/排行  → 本队个人榜；/排行 队伍  → 对战各队记录\n"
        "/趋势 \"ice boundary\" 最近7天\n"
        "/踢馆 \"ice boundary\" 七月\n\n"
        "⑤ 查战报原文或导出\n"
        "/导出 \"ice boundary\" 全部 七月  → 查看该玩家该月战报原文\n"
        "/导出 胜场 七月  → 查看本队该月胜场战报\n"
        "/导出 \"ice boundary\" 七月 csv  → 下载表格；把 csv 换成 json 可导出 JSON。\n\n"
        "⑥ 查看月度奖金\n"
        "普通成员用 /结算 七月 查看已结算结果；管理人员执行同一个命令会重新结算，操作含义不同喵。\n"
        "本群可以查询；提交、改错和删除流程请看 /帮助 战报群。"
    ),
}


def render_group_help(chat_type: str | None = None, home_team: str | None = None,
                      *, configured: bool = True, qq_id: str | None = None,
                      preview: bool = False) -> str:
    role = normalize_chat_type(chat_type) or FRIENDLY_GROUP
    title = f"📋 {role}操作流程{'（预览）' if preview else ''}喵～"
    parts = [title]
    if not preview:
        state = role if configured else f"{role}（默认，尚未明确设置）"
        parts.append(f"本群：{state}；战队：{home_team or '未绑定'}")
        if not home_team or not configured:
            parts.append(render_setup_guide(home_team, role if configured else None, qq_id=qq_id))
    parts.append(WORKFLOWS[role])
    parts.append("/帮助 设置 查看开通步骤｜/帮助 全部 查看三类流程｜/帮助 命令 查看命令索引 🐾")
    return "\n\n".join(parts)
