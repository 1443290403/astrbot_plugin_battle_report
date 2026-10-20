# 战报插件代码审查与修复记录

> 版本说明：本文保留 **v1.15.4 审查记录**。当前 v1.16.0 已按新需求禁止所有私聊提交，
> 新增战队管理员及带空格 ID 支持，数据库版本为 18。当前权限、升级步骤与命令以
> [README](README.md) 和 [REQUIREMENTS](REQUIREMENTS.md) 为准；下面的旧版实现和测试数字属于审查时快照。

审查日期：2026-10-20；修复版本：**1.15.4**；插件基线：`1cb6c14`。

本次审查覆盖插件入口、解析、排表、MySQL 持久化与迁移、统计、图片、导出及测试工具。
主要风险集中在权限入口、身份归属和模块之间的接线。原来的纯逻辑测试 277 项、
含 MySQL 测试 330 项均能通过，但没有覆盖下面多项实际故障。因此，不能只用已有测试是否通过评价代码可靠性。

## 上游对照依据

参考源码保存在工作区的 `reference/SnowLuma`、`reference/AstrBot`，没有修改上游项目。
本次核对的具体提交如下，结论仅覆盖这些接口契约，不代表所有历史版本均兼容。

- [SnowLuma db142db9](https://github.com/SnowLuma/SnowLuma/tree/db142db9e6398c779283eab9afc3552da1e2c361)
- [AstrBot fc6b326f](https://github.com/AstrBotDevs/AstrBot/tree/fc6b326fc9012873c4adc0183256c90d34a3d912)
- SnowLuma 的 [group-info.ts](https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/onebot/src/actions/group-info.ts)：
  `get_group_member_info` 未查到数据时可能返回带请求 `user_id`、`role='member'` 的占位对象。
  仅检查这个返回值不能证明用户在群内。修复采用真实 `get_group_member_list` 列表匹配。
- SnowLuma 的 [extended.ts](https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/onebot/src/actions/extended.ts)：
  `get_group_msg_history` 返回 `{messages}`，消息 ID 可能是负数；引用查询保留有符号 ID。
- AstrBot 的 [唤醒阶段](https://github.com/AstrBotDevs/AstrBot/blob/fc6b326fc9012873c4adc0183256c90d34a3d912/astrbot/core/pipeline/waking_check/stage.py)
  与 [命令过滤器](https://github.com/AstrBotDevs/AstrBot/blob/fc6b326fc9012873c4adc0183256c90d34a3d912/astrbot/core/star/filter/command.py)：
  `/` 可在匹配命令前被剥离，旧命令别名必须同时注册不带 `/` 的形式。
- AstrBot 的 [aiocqhttp_message_event.py](https://github.com/AstrBotDevs/AstrBot/blob/fc6b326fc9012873c4adc0183256c90d34a3d912/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py)：
  支持显式目标群的 `get_group(group_id)`；私聊检查管理员身份时必须传目标群。

## 已修复的问题

P1 表示可造成越权、错误归属或服务不可用；P2 表示会算错数据或在特定输入/并发条件下失败。
下表位置对应修复后的函数，便于继续维护。

| 编号 | 级别 | 原问题与触发条件 | 修复位置与行为 |
|---|---|---|---|
| R01 | P1 | 私聊没有事件群号，`_group_check` 不会检查战报地点所指群的禁用状态；只知道群号就可能向该群提交 | `main.submit_report` / `_is_target_member`：逐份检查目标群禁用、真实成员身份；无法确认身份则拒收。全局管理员可代交，但仍检查禁用 |
| R02 | P1 | 私聊一次提交多份时，整批都套用第一份的地点和绑定战队，写入错误群，删除/撤销归属也会错 | `main.submit_report`：每份独立取地点、战队和权限，某份失败不阻断其他份 |
| R03 | P1 | `find_or_create_user` 先查角色名再查 QQ，可能返回别人的同名角色；解绑后旧角色仍被认领时尤其容易触发 | `database._find_or_create_user` / `bind_player_to_self`：先复用本人 QQ 角色，再检查同名角色归属，禁止加入他人角色 |
| R04 | P1 | 认领、创建角色、绑定分别查询和写入，两个请求可同时通过检查，后写覆盖前写；解绑也有检查后归属变化的竞争 | `database._identity_cursor`：同战队身份操作使用数据库命名锁串行执行，校验和绑定单事务；普通解绑 UPDATE 同时匹配当前 `user_id`，管理员绑定检查角色所属战队 |
| R05 | P1 | 默认 `super_admin` 写死作者 QQ，其他人安装后会给该账号通告、群开关及成员导出的权限 | `_conf_schema.json` / `main._is_super_admin`：默认空，未显式配置就不授予超管。旧配置不自动重写，升级时需检查 |
| R06 | P1 | v2/v3/v8/v9/v12/v13 多处 ALTER 不探测现有列，DDL 已提交但版本号尚未写入时，重启后重复 ALTER 失败；v10 在删外键后中断也无法恢复外键 | `database._init_schema`：ADD/DROP 前检查列是否存在，合并判罚标记支持部分完成状态，v10 完成后恢复外键 |
| R07 | P1 | 数据库测试继承插件配置，普通运行测试就可能连接配置里的线上 MySQL 并创建/删除测试库；不同测试进程还会互删同名库 | `tests/conftest.py` / `test_database_stats.py`：独立测试连接参数，默认不连接；显式启用才执行，库名带进程号 |
| R08 | P2 | 配置提示允许群管理/群主提交，但 `submit_requires_admin` 只检查 AstrBot 全局管理员 | `main.submit_report`：群聊复用 `_is_manager`，私聊检查目标群管理身份 |
| R09 | P2 | `get_players_trend` 的 SQL 是 ID 集合在日期上限前，参数列表却把日期放到 ID 前；默认月份查询角色趋势也会出错或查不到 | `database.get_players_trend`：修正两侧参数顺序，真实 MySQL 验证单/多 ID 和日期范围 |
| R10 | P2 | 队伍趋势另按对局胜场多数判胜，KOF 下可能和已入库胜者、战绩、导出相反 | `database.get_team_trend`：统一使用 `matches.winner`，每场比赛计一次 |
| R11 | P2 | 未解析成角色的原始 ID 趋势同时查两队，双方同名会混入对手结果 | `database.get_player_trend` / `main.trend`：入口传 `side_team=home_team`；底层保留可选参数以兼容已有调用 |
| R12 | P2 | 解析器支持 `战队：`，最近战报、引用识别、批量拆分却只认 `战队:`；引用消息内多份战报没有再次拆分 | `TEAM_RE`、`split_reports`、消息读取与引用：统一中英文冒号识别，节点内部继续拆分多报，忽略纯前导空行 |
| R13 | P2 | 旧命令仅注册带 `/` 别名，AstrBot 剥离唤醒前缀后匹配不到；轮次自定义过滤器不兼容保留 `/` 的消息；短命令前缀可先截掉长别名 | `main` 命令装饰器、`_ROUND_CMD_RE`、`_strip_command`：补齐别名、允许可选 `/`、按最长命令优先剥离 |
| R14 | P2 | 日期只检查日不大于 31，2 月 30 日也算解析成功；胜方/双方判罚被折叠成一个标记，导出时可能换到败方；同队对阵无法可靠区分胜负 | `battle_report_parser`：校验公历与 MySQL DATE 年份范围，拒绝胜方/双方判罚和双方同队 |
| R15 | P2 | `/结算 13月` 在异常处理前报错；未知参数被忽略后，结算/重置可能静默作用于缺省月份 | `main._parse_settle_args`、两个 handler、`settle_month_range`：非法月份和未知参数明确拒绝，任何写库前完成校验 |
| R16 | P2 | 排行/趋势按秒命名，同一秒请求可能互相覆盖；导出按群固定文件名，同群不同筛选的文件可能串内容 | `main.ranking` / `trend` / `export` / `export_group_members`：所有产物使用独立 UUID 文件名 |
| R17 | P2 | 重复提交唯一键冲突时，仍占着当前连接又借第二条连接回查；连接池满时可能相互等待 | `database.insert_report`：回滚结束旧快照后，直接使用原连接回查已有 ID |
| R18 | P2 | 初始化迁移失败后未关闭已创建连接池；`BattleReport` 未声明数据库方法必读的 `created_at`，解析结果直接入库会 AttributeError | `database.initialize`：失败时关闭池；`BattleReport.created_at` 声明默认值 |
| R19 | P2 | 绑定 ID 用最多 50 条的 LIKE 查询确认存在，管理绑定也只取前 500 个 ID，真实存在的 ID 可能被误判不存在 | `database.bind_player_to_self` / `main.admin_id`：精确查询 ID，不受模糊查询与列表展示上限影响 |
| R20 | P2 | 批量提交回执没有按合并转发节点上限分批，超过 100 个节点时可能发送失败 | `main._send_responses`：每批最多 100 个节点 |

另外，排行和趋势的 Pillow 同步绘制已放到 `asyncio.to_thread`，减少长图阻塞机器人事件循环的时间。
README、配置说明、开发要求和三个插件版本标记已同步。

## 仍需后续处理的不足

以下事项没有通过修改既有计分规则或自动改写历史数据来处理：

1. **跨年查询缺少年份参数**：`/结算 十二月` 可能指去年，而 `/排行 十二月` 恒取当前年。
   需求文档 §10.4 已明确保留此边界。建议统一支持 `2025年12月`，覆盖所有查询与结算入口。
2. **并列奖金跨截断边界按行截断**：同为第 4 名的两人可能只有一人获奖。
   已有测试明确固定此政策（§10.4），若要改成同名次全奖，需要先统一奖金预算规则。
3. **多人踢馆与角色合并的业务含义未完全定义**：目前按既有规则给各出场者记分（§10.3）。
   多个 ID 是否表示同一人，不能仅从文本推断。本次保持现有规则。
4. **历史身份脏数据仍可能存在**：新操作已串行化，但 `users` 仍无 QQ 唯一约束。
   旧版本产生的同 QQ 多角色、同展示名与未绑定 ID 碰撞不会自动清理；需要基于真实数据备份、核对后迁移。
   `/排行 全部` 仍按展示名聚合，不同战队同名选手可能合并，需要进一步定义跨队榜身份键。
5. **结算计算、读取旧快照、写入与发送公告不是一个完整并发单元**：写快照本身有事务，
   但两个管理同时结算仍可能重复通知或其中一次写入失败。建议后续按战队和月份串行结算/重置。
6. **大数据量与产物管理**：统计以 `home_team` 和日期过滤，但现有主要索引按群建立；
   需要结合真实数据量用 EXPLAIN 验证索引。导出仍全量加载，生成文件尚无保留期限，后续应分页并清理过期文件。
7. **时间边界与解析策略**：多数日期默认值依赖服务端时区，查询尚不支持统一显式业务时区；
   部分无法识别的对局行仍按警告忽略，可能漏统计。扩大拒收范围前应结合真实历史格式定义允许的备注行。
8. **维护成本**：入口 `main.py` 与 SQL 文件仍较大，多个 SQL/Python 路径重复表达积分和显示名逻辑。
   后续宜按提交、身份、结算、统计分模块，并继续补接口契约测试。

## 验证与范围

- 完整回归：**376 passed**（含隔离 MySQL 8.4 集成测试），相比原有 330 项新增 46 项。
- 默认不连接数据库的回归：**313 passed, 1 skipped**（跳过整个数据库模块）。
- 原有指纹固定值测试继续通过，未改变历史去重算法。
- 保留按战队跨群统计、0:0 占位、友谊/踢馆分流、替补不扩容、结算快照和普通成员只读结算等既有口径。
- `test_main.py` 用框架组件桩调用真实 handler；`test_database_stats.py` 在本机隔离 MySQL 8.4 中执行真实 SQL，
  覆盖并发认领、归属冲突、趋势参数及迁移恢复；生命周期测试模拟唯一键冲突时连接池已满的场景。
- 没有登录真实 QQ，也没有启动完整 AstrBot/SnowLuma 服务链路；平台联调仍需在部署环境确认。
- `compileall` 与 `git diff --check` 通过。MySQL 幂等建表、INSERT IGNORE 和测试库清理产生的 Warning 不作为失败。

普通验证：

```bash
pip install -r requirements.txt pytest
pytest tests/ -q
```

含数据库验证（使用专用测试实例，不读取插件配置）：

```bash
ASTRBOT_TEST_MYSQL_ENABLE=1 \
ASTRBOT_TEST_MYSQL_HOST=127.0.0.1 \
ASTRBOT_TEST_MYSQL_PORT=3306 \
ASTRBOT_TEST_MYSQL_USER=root \
ASTRBOT_TEST_MYSQL_PASSWORD='<测试实例密码>' \
pytest tests/ -q
```

测试会建删 `astrbot_battle_report_test_<进程号>`。独立参数和显式开关是测试工具的行为，
日常插件连接仍使用原来的 `mysql_*` 配置。

## 升级说明

将修复版覆盖到原插件目录并重启 AstrBot。数据库结构版本仍为 17，无新增业务表或列。
检查 `super_admin` 是否为自己的 QQ：新安装默认空，旧配置保存的作者 QQ 不会自动替换。
开启仅管理提交的群会按配置说明允许群主/管理提交；私聊现在必须能确认目标群成员身份，
平台无法提供成员列表时可以直接在目标群提交。
