# v1.17.8 提示文案修改清单

本次整理 63 项，覆盖 7 个用户提示与配置文件。表中花括号表示运行时数据。

提示保留结果和必要下一步，移除重复原因及内部处理说明。战报 ID、月份和战队范围、权限、单位、颜文字、战报标记保留。

检查范围还包括数据库、命令参数解析、维护脚本、日志、测试和文档；技术诊断及格式契约按其原有用途保留。

## 排表与记录

| 位置 | 修改前 | 修改后 |
|---|---|---|
| [lineup.py](lineup.py) | 喵…第 {prev_round} 轮还没分出胜者喵（要先把上一轮的比分填进战报）。 | 喵…第 {prev_round} 轮还没分出胜者，请先填写比分。 |
| [lineup.py](lineup.py) | 喵…没有可配对的空闲存活选手呢（有一边没人可打）。 | 喵…暂时没有可配对的选手。 |
| [lineup.py](lineup.py) | （应为『玩家A 玩家B』或『玩家A 比分 玩家B』） |  / 用法：玩家A 玩家B，或 玩家A 比分 玩家B。 |
| [lineup.py](lineup.py) | ⚠️ 胜负未定（最后一场为平局），未计算积分 | ⚠️ 胜负未定，暂不计算积分 |
| [main.py](main.py) | 其他规则 第一行：(/排表+空格+规则名称) | 指定规则：/排表 规则名称 |
| [main.py](main.py) | ❌ 轮次要写 2 以上的数字喵（比如 /第二轮、/第三轮）。 | ❌ 请从 /第二轮 开始追加轮次喵。 |

## 战队与群设置

| 位置 | 修改前 | 修改后 |
|---|---|---|
| [main.py](main.py) | （已有战报已回填） | 删除 |
| [main.py](main.py) | （覆盖原名单） | 删除 |
| [main.py](main.py) | 用法喵：绑定战队 <战队名>（最多 64 字） | 用法喵：绑定战队 <战队名> / 战队名最多 64 字。 |

## 身份与查询

| 位置 | 修改前 | 修改后 |
|---|---|---|
| [main.py](main.py) | 🔍 战队 {home} 参赛ID喵（/绑定ID <参赛ID> 绑定）： | 🔍 战队 {home} 参赛ID喵： |
| [main.py](main.py) | （已绑 {s['user_name']} 绑定此ID将同时绑定角色） | （已绑 {s['user_name']}，可认领） |
| [main.py](main.py) | 参赛ID「{p}」绑在别的角色（{binding['user_name']}）身上喵，解不了。 | 参赛ID「{p}」属于角色「{binding['user_name']}」，你没有解绑权限喵。 |
| [main.py](main.py) | 还没有参赛ID呢（还没有战报记录）。 | 还没有参赛ID呢。 |
| [main.py](main.py) | 喵…名字太长了啦（最多 30 字）。 | 喵…名字最多 30 字。 |
| [main.py](main.py) | ⚠️ 这个群没有成员列表喵（可能机器人不在群里，或者适配器不支持）。 | ⚠️ 暂时无法获取本群成员列表喵。 |
| [stats.py](stats.py) | （用法：/战绩 <玩家名> 查个人战绩喵） | 删除这条附加说明 |
| [stats.py](stats.py) | （上限{RAID_DEFENSE_CAP}） | 删除 |
| [main.py](main.py) | 参赛ID「{p}」解绑好啦喵～（管理操作） | 参赛ID「{p}」解绑好啦喵～ |

## 战报提交与导出

| 位置 | 修改前 | 修改后 |
|---|---|---|
| [main.py](main.py) |  /  / （提示喵：引用的内容可能不全，可以重发那份战报，或者直接 /发送 粘贴全文） |  /  / 请用 /发送 粘贴完整战报重试喵。 |
| [main.py](main.py) | ❌ 第 {i} 份战报里的队伍（{report.team_a}、{report.team_b}） | ❌ 第 {i} 份战报 |
| [main.py](main.py) | 没有本群绑定的战队（{home_team}）喵，所以拦下来没发： | 未包含本群战队「{home_team}」喵： |
| [main.py](main.py) | ❌ 第 {i} 份战报还没打完喵 | ❌ 第 {i} 份战报对局不足喵 |
| [main.py](main.py) | （只有 {len(report.duels)} 场对阵，至少要 {min_duels} 场）： | ，需要至少 {min_duels} 场，当前 {len(report.duels)} 场： |
| [main.py](main.py) | （把比分填上再发一次喵） | 请填好比分后重新提交喵。 |
| [main.py](main.py) | 📄 我收到的战报原文： | 📄 战报原文： |

## 结算

| 位置 | 修改前 | 修改后 |
|---|---|---|
| [main.py](main.py) | （结算是超管、战队管理员或群管理/群主来做的：/结算 {mon}月） | 请联系本队管理员结算喵。 |
| [main.py](main.py) | 结算重置好啦喵～（删掉了 {removed} 条记录） / 这个月又变回「未结算」了喵：/排行 {mon}月 不再显示「奖金」列，重新 /结算 {mon}月 会当成第一次结算、重新 @ 获奖成员哦。 | 结算重置好啦喵～ / 重新结算：/结算 {mon}月 |
| [stats.py](stats.py) | （以上是已经结算好的结果喵，这次一个字都没改～ 🐾） | 删除尾注，保留「结算查询」标题、明细和合计 |
| [stats.py](stats.py) | 这次结算出来的获奖名次和上次一样，就不打扰大家了喵～ 🐾 | 获奖名次未变喵～ 🐾 |

## 命令帮助

| 位置 | 修改前 | 修改后 |
|---|---|---|
| [stats.py](stats.py) | 如：/第二轮 云玩家 2:0 鸽子（记录比分） | 如：/第二轮 云玩家 2:0 鸽子 |
| [stats.py](stats.py) | 如：/记录 云玩家 20（填入云玩家最后一场未记录对阵） | 填分：/记录 云玩家 20 |
| [stats.py](stats.py) | 如：/记录 云玩家 20 鸽子（无未记录对阵时插入最新轮次） | 指定对手：/记录 云玩家 20 鸽子 |
| [stats.py](stats.py) | /发送 + 粘贴排表模板（填入实际比分） | /发送 + 粘贴已填好比分的完整战报 |
| [stats.py](stats.py) | （仅战报群可提交，禁止私聊；/战报 仍可用） | 仅战报群可提交，禁止私聊 |
| [stats.py](stats.py) | ▎战队设置（超管或对应战队管理员） | ▎战队设置 / 由超管或本队管理员操作 |
| [stats.py](stats.py) | 月度结算（默认上个月）：超管/战队管理员/群管理/群主发奖金（默认前 4 名） | 管理人员结算发奖金；普通成员查看结果 |
| [stats.py](stats.py) |                         并 @ 获奖成员；其他人查询已结算结果（不会改动数据） |                         默认上个月、前 4 名 |
| [stats.py](stats.py) | 把该月恢复成未结算（超管/战队管理员/群管理/群主，默认上个月） | 管理人员重置月度结算，默认上个月 |
| [stats.py](stats.py) | ▎查询（默认本月，末尾可加 X月 查其他月份，如 七月/7月） | ▎查询 / 默认本月，末尾可加 七月/7月 等月份 |
| [stats.py](stats.py) | 战绩（无玩家名=本战队） | 个人或本队战绩 |
| [stats.py](stats.py) | 导出（默认本月） | 导出战报 |
| [stats.py](stats.py) | 踢馆成绩（无玩家名=本战队） | 个人或本队踢馆成绩 |
| [stats.py](stats.py) | 批量解除参赛ID绑定（管理可解任意） | 解除绑定；管理人员可操作他人 ID |
| [stats.py](stats.py) | ▎超级管理（仅超管） | ▎超级管理 |
| [stats.py](stats.py) | 查看全部群（可按战队过滤） | 查看群列表 |
| [stats.py](stats.py) | （超管或本队管理员） | 删除 |

## 流程帮助

| 位置 | 修改前 | 修改后 |
|---|---|---|
| [help_guide.py](help_guide.py) | 整段发送（示例队名和选手请替换）： | 换成自己的队名和选手，整段发送： |
| [help_guide.py](help_guide.py) | （预览） | 预览 |

## 图表

| 位置 | 修改前 | 修改后 |
|---|---|---|
| [chart.py](chart.py) | （自动缩放） | 删除 |

## 格式校验

| 位置 | 修改前 | 修改后 |
|---|---|---|
| [battle_report_parser.py](battle_report_parser.py) | 比分用了小数点（如 2.1），应为冒号（如 2:1） | 比分不能用小数点，请改为冒号格式，如 2:1 |
| [battle_report_parser.py](battle_report_parser.py) | 时间格式无法识别（应为如 2026.08.01），原因：{e} | 时间格式或日期无效，请按 2026.08.01 的格式填写。 |
| [battle_report_parser.py](battle_report_parser.py) | 缺少时间信息，需写一行日期（如 2026.8.30 21:00）。 | 缺少时间信息，请填写日期，如 2026.8.30 21:00。 |

## 配置面板

| 位置 | 修改前 | 修改后 |
|---|---|---|
| [_conf_schema.json](_conf_schema.json) | ranking_image.description: 排行是否以图片展示（关闭则退回文字表格） | ranking_image.description: 排行图片 |
| [_conf_schema.json](_conf_schema.json) | ranking_image.hint:  | ranking_image.hint: 关闭后显示文字表格 |
| [_conf_schema.json](_conf_schema.json) | ranking_image_max_rows.description: 排行图片最多展示行数（仅 /排行 全部 生效；/排行 默认显示全部队员不截断） | ranking_image_max_rows.description: 全战队排行图片行数 |
| [_conf_schema.json](_conf_schema.json) | ranking_image_max_rows.hint:  | ranking_image_max_rows.hint: 仅影响 /排行 全部；本队排行显示全部队员 |
| [_conf_schema.json](_conf_schema.json) | min_duels.description: 单份战报最少对局数（低于此数视为未完成的战报，拒绝入库） | min_duels.description: 战报最少对局数 |
| [_conf_schema.json](_conf_schema.json) | min_duels.hint: 2 人赛制的战队可调小；这是提交前置条件，与排行的 min_games 无关 | min_duels.hint: 对局不足时不能提交；2 人赛制可调小 |
| [_conf_schema.json](_conf_schema.json) | super_admin.description: 超级管理员QQ（可禁用/开启群功能） | super_admin.description: 超级管理员 QQ |
| [_conf_schema.json](_conf_schema.json) | super_admin.hint: 请填写你自己的 QQ；留空时超级管理员命令不可用，不预授予作者账号权限 | super_admin.hint: 留空则不开启超管命令 |
| [_conf_schema.json](_conf_schema.json) | pairing_seed.description: 排表随机种子（留空则每次随机；填写则固定以便复现） | pairing_seed.description: 排表随机种子 |
| [_conf_schema.json](_conf_schema.json) | pairing_seed.hint:  | pairing_seed.hint: 留空随机，填写后固定配对 |
| [_conf_schema.json](_conf_schema.json) | mysql_db.description: MySQL 数据库名（插件会自动建库建表） | mysql_db.description: MySQL 数据库名 |
