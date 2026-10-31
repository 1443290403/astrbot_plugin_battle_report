"""面向群成员的操作流程与开通引导；不依赖平台和数据库。"""
FRIENDLY_GROUP = "友谊群"
REPORT_GROUP = "战报群"
MAIN_GROUP = "主群"
GROUP_TYPES = (FRIENDLY_GROUP, REPORT_GROUP, MAIN_GROUP)


def normalize_chat_type(chat_type: str | None) -> str | None:
    """None 表示还没有明确配置有效群属性；群名称保持原有约定。"""
    value = (chat_type or "").strip()
    return value if value in GROUP_TYPES else None


def render_setup_guide(home_team: str | None = None, chat_type: str | None = None,
                       *, required_type: str | None = None) -> str:
    """只提示需要补充的群设置，不展示超管的配置或授权操作。"""
    role = normalize_chat_type(chat_type)
    target = normalize_chat_type(required_type)
    steps = []
    if not home_team:
        steps.append("绑定本群战队\n/绑定战队 <战队名>")
    if target and role != target:
        steps.append(f"设置为{target}\n/群聊属性 {target}\n"
                     "若本群另有用途，请到本战队已设置好的战报群提交。")
    elif not role:
        steps.append(
            "按本群用途选择一条\n"
            "/群聊属性 友谊群  → 排表、记录比分\n"
            "/群聊属性 战报群  → 提交、修改和删除战报\n"
            "/群聊属性 主群  → 绑定 ID、查询战绩"
        )
    if not steps:
        return f"本群已绑定战队 {home_team}，群属性为{role}。需要调整请联系超管或本队战队管理员喵。"
    parts = ["🧭 本群设置喵～", "请联系超管或本队战队管理员完成以下设置："]
    if home_team:
        parts.append(f"本群战队：{home_team}")
    parts.extend(f"{number}. {step}" for number, step in enumerate(steps, 1))
    parts.append("设置好后，发 /帮助 查看本群流程。")
    return "\n\n".join(parts)


WORKFLOWS = {
    FRIENDLY_GROUP: (
        "① 比赛开始前：提供双方名单\n"
        "换成自己的队名和选手，整段发送：\n"
        "/排表\n"
        "a队:云玩家 萌新 遗老\n"
        "b队:复读机 鸽子 柠檬\n"
        "默认按 KOF 排表；人头赛首行写 /排表 人头赛。机器人会生成第一轮，0:0 是尚未比赛的占位。\n\n"
        "② 第一轮打完一局：填比分\n"
        "/记录 云玩家 20\n"
        "20 表示云玩家 2:0 胜；也可以写 /记录 云玩家 2:1。机器人会发出更新后的完整战报。\n"
        "名字带空格时：/记录 \"示例 玩家\" 20\n\n"
        "③ 需要下一轮：先填完上一轮，再追加\n"
        "/第二轮\n"
        "自动匹配上一轮胜者和本轮空闲的存活选手，跳过还在 0:0 对局中的双方。先用 /记录 填完比分，再发 /第二轮，本轮胜者就能继续匹配。\n"
        "指定对阵可写 /第二轮 云玩家 鸽子；需要新增轮次时用 /第三轮、/第四轮。\n"
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
        "指定删除请联系本队战队管理员或群管理/群主；普通成员不能删别人的记录。\n"
        "删除会影响实时战绩；已结算奖金是快照，需管理人员另行重跑对应月份的结算喵。"
    ),
    MAIN_GROUP: (
        "① 先找自己的参赛 ID\n"
        "/查ID 云玩家\n"
        "也可用名字的一部分模糊查找；带空格的完整 ID 用 /查ID \"示例 玩家\"。\n"
        "查不到时，先确认战队是否正确，并请战报群录入包含该 ID 的战报；ID 不是靠 /绑定ID 凭空创建的。\n\n"
        "② 绑定到自己的 QQ，并检查身份\n"
        "/绑定ID \"示例 玩家\"\n"
        "/我的ID\n"
        "多个 ID 可写 /绑定ID \"示例 玩家\" 云玩家，绑定后合并为自己的角色战绩。\n"
        "绑错自己的 ID 用 /解绑ID \"示例 玩家\"；ID 已属于别人时联系管理员处理。可用 /改名 \"角色名字\" 修改显示名。\n\n"
        "③ 查看个人或战队战绩\n"
        "/我的战绩  → 汇总自己绑定的 ID\n"
        "/战绩 \"示例 玩家\"  → 查指定玩家\n"
        "/战绩  → 本战队总战绩\n"
        "默认本月，命令末尾加 七月/7月 可查该月，例如 /我的战绩 七月。\n\n"
        "④ 看排行、走势与踢馆成绩\n"
        "/排行  → 本队个人榜；/排行 队伍  → 对战各队记录\n"
        "/趋势 \"示例 玩家\" 最近7天\n"
        "/踢馆 \"示例 玩家\" 七月\n\n"
        "⑤ 查战报原文或导出\n"
        "/导出 \"示例 玩家\" 全部 七月  → 查看该玩家该月战报原文\n"
        "/导出 胜场 七月  → 查看本队该月胜场战报\n"
        "/导出 踢馆 胜场  → 查看本队本月踢馆胜场战报\n"
        "/导出 \"示例 玩家\" 七月 csv  → 下载表格；把 csv 换成 json 可导出 JSON。\n\n"
        "⑥ 查看月度奖金\n"
        "普通成员用 /结算 七月 查看已结算结果；管理人员执行同一个命令会重新结算，操作含义不同喵。\n"
        "本群可以查询；提交、改错和删除流程请看 /帮助 战报群。"
    ),
}


def render_group_help(chat_type: str | None = None, home_team: str | None = None,
                      *, preview: bool = False) -> str:
    """普通帮助只展示操作流程；缺少设置时由相关命令单独提示。"""
    role = normalize_chat_type(chat_type) or FRIENDLY_GROUP
    title = f"📋 {role}操作流程{'预览' if preview else ''}喵～"
    parts = [title]
    if home_team and not preview:
        parts.append(f"本群战队：{home_team}")
    parts.append(WORKFLOWS[role])
    parts.append("/帮助 全部 查看三类流程｜/帮助 命令 查看命令索引 🐾")
    return "\n\n".join(parts)
