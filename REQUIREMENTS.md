# 战队对战战报插件 —— 需求文档（REQUIREMENTS.md）

> 对应版本：`1.15.0`｜数据库 `SCHEMA_VERSION = 17`｜最后核对：2026-09-10
> 代码基准：本目录（`battle-report/`）当前工作区

---

## 0. 本文档怎么用（重要）

这份文档的目标不是"描述有哪些功能"，而是**在改动时能一眼看出「要一起改的还有哪些」**。

**三条铁律：**

1. **口径是全局的，功能是局部的。** 胜率、积分、0:0 占位、玩家名解析、战队归属这些口径被十几个功能共用。改口径 = 改一批功能，不是改一个命令。
2. **同一件事在代码里有多个实现。** 例如积分公式在 Python 和两处 SQL 里各写了一遍（见 §7.3）。改一处不改其余，数据会自相矛盾。
3. **数据历史不会自动重算。** 改了判定/口径后，`matches`、`duels` 里已有数据的口径是旧的。要么加 `SCHEMA_VERSION` 迁移回填，要么写 `scripts/` 下的一次性修复脚本。

**使用流程：**

| 场景 | 动作 |
|---|---|
| 要加/改功能 | ① 在 §6 找到相关功能编号 ② 读 §5 找出它依赖的横切口径 ③ **逐行过 §7 传导矩阵** ④ 按 §9 检查清单收尾 |
| 要改统计口径 | 直接跳 §7.3「同源重复实现」清单，那里列了所有必须同步的位置 |
| 排查线上数字对不上 | 先看 §10「已知不一致」，多数是已登记的口径冲突 |
| 交接/新人上手 | §1 → §4 → §5 → §6 → §8 |

---

## 1. 系统定位与形态

面向 QQ 群聊的**战队对战战报**插件（AstrBot 插件），把线下/群内约战的排表、比分、战报统一到线上 MySQL，并提供个人与战队的统计分析。

- **运行形态**：AstrBot 插件（`Star` 子类），单文件入口 [main.py](main.py)，全部命令由 QQ 群消息触发
- **存储**：线上 MySQL（`aiomysql` 异步连接池，`autocommit`），插件启动时自动建库建表并执行迁移
- **渲染产出**：文本表格（[stats.py](stats.py)）+ Pillow 图片（[chart.py](chart.py)）+ CSV/JSON 文件导出 + 合并转发（QQ 富消息）
- **代码分层**（刻意做的解耦，纯逻辑层零框架依赖、可单测）：

```
main.py                    命令入口 / 权限 / 平台消息收发 / 编排
  ├── lineup.py            排表、追加轮次、记录比分、战报文本还原  ← 纯 Python
  ├── battle_report_parser.py  战报文本解析、胜负判定、时间参数解析  ← 纯 Python
  ├── stats.py             积分/胜率/表格/帮助文案                ← 纯 Python
  ├── database.py          MySQL 建表迁移 + 全部 SQL 聚合
  └── chart.py             Pillow 绘图（趋势图 + 排行表格图）
```

**核心数据流：**

```
/排表 ──► 模板文本（发到群里，不入库 duels）
             │
             ▼  群成员在群里填比分
/第N轮 ─┐
/记录  ─┴─► 修改群聊中"最近一条战报"的文本（不入库）
             │
             ▼
/发送 ──► 按首行分流（`战队: …` = 友谊 / `A踢馆B` = 踢馆）──► 各自的解析器
             │
             ▼
        校验（含本队/对局数/胜负已定/内容未重复）──► 入库 matches + duels + player_ids
             │
             ▼
/排行 /战绩 /趋势 /导出 /我的战绩 ──► 按 home_team 跨群聚合，且 kind = 'friendly'
                                       （踢馆被排除在上述友谊口径之外）
/踢馆                              ──► 按 team_a/team_b 对称取数（M-02 的例外）
```

> **关键认识**：`/排表`、`/第N轮`、`/记录` 全程**不碰数据库的 duels 表**，它们只是在群聊消息里搬运文本。数据只有一道入库口：`/发送`。因此"修改战报格式"这件事会同时波及制表侧和解析侧两套代码。

---

## 2. 术语表

| 术语 | 含义 |
|---|---|
| **主体战队 / home_team** | 一个群所归属的战队。群内所有上传与统计都以此战队为视角 |
| **参赛ID / player_name** | 选手在战报里出现的原始名字，如 `红莲`。仅从已上传战报中提取 |
| **角色 / user** | 一个 QQ 对应的身份，可挂多个参赛ID（如改名前后、小号）。统计时按角色聚合 |
| **对局 / duel** | 一次 1v1 交锋：`玩家A 比分A:比分B 玩家B` |
| **比赛 / match** | 一份战报 = 一场比赛，含多轮多场对局 |
| **轮次 / round_no** | 比赛内的轮次，第一轮由 `/排表` 生成，后续由 `/第N轮` 追加 |
| **占位对局** | 比分 `0:0` 的对局，表示"还没打"，不是平局 |
| **替补 / sub** | 第一轮没出场、后续轮次才出场的选手（也可用 `(替)` 显式标记） |
| **判罚落败 / ruled** | 选手触犯规则被直接判负，用 `(规则)` 标记；判罚方比分必然更低 |
| **友谊次数** | 该选手参与过的**比赛场次**数（去重，不含只有占位对局的比赛） |
| **无双次数** | 一人击败对面全部选手、且己方队友全部阵亡的场次（每场至多一人） |
| **踢馆 / raid** | 一队去另一队的馆 1v5：踢馆方要打穿全部防守者，守馆方只要终结踢馆者一次即胜。`matches.kind = 'raid'`，与友谊赛互不干扰（§5-M13） |
| **馆主** | 守馆方的一名特殊防守者，在战报里于名字后写 `(馆主)`。作为**第 5 个**防守者出场并获胜 = SHUT DOWN（+5 分） |
| **`规则` 占位行** | 踢馆报里防守方名字恰为 `规则` 的对局行（比分 `0:0`）= 守馆方没派这个人。全占位 = 无人守馆 → 踢馆方胜、0 分、发徽章 |
| **友谊积分** | 排行里的「**友谊积分**」（v1.14.0 前叫「积分」）= 胜场 × 胜率，只算友谊赛 |
| **踢馆积分 / 总积分** | 踢馆积分独立于友谊积分；**总积分 = 友谊积分 + 踢馆积分**，排行按总积分排序，列序为 `总积分｜友谊积分｜…｜踢馆积分` |
| **守馆成功 / 守馆首轮 / 踢馆成功** | 排行末三列的**次数**（不是分数）：分别指守馆方终结踢馆者的次数、其中属于首轮的次数、踢馆方踢破成功且含馆主的次数 |

---

## 3. 角色与权限模型

### 3.1 四级身份

| 身份 | 判定函数 | 能力 |
|---|---|---|
| **超级管理员** | `_is_super_admin()`（配置 `super_admin` 的 QQ） | 禁用/启用群、群列表、通告、导出群成员 |
| **管理 / 群主** | `_is_manager()` = AstrBot 管理员 ∨ 群主 ∨ 群管理 | 绑定战队、群聊属性、删除战报、管理ID、解绑他人ID |
| **提交者本人** | `get_last_match_by_submitter()` | 撤销自己最近一条战报 |
| **普通成员** | 默认 | 排表、追加轮次、记录比分、提交、查询、绑定自己的ID |

> `_is_manager` **包含** `event.is_admin()`（AstrBot 全局管理员），所以全局管理员在任何群都有管理权。

### 3.2 前置校验（每个命令入口的组合）

| 校验函数 | 检查内容 | 使用者 |
|---|---|---|
| `_check_db()` | 数据库连接就绪 | `/战队列表`、`_admin_check` 的第一半 |
| `_check_enabled()` | 本群未被超管禁用 | `/第N轮`、`/记录`、**`/帮助`** |
| `_group_check()` | `_check_db()` + `_check_enabled()` | 绝大多数命令 |
| `_admin_check()` | `_check_db()` + 超管身份 | `/禁群`、`/启群`、`/查群`、`/群列表`、`/通告` |
| `_require_home()` | `_group_check()` + 群号 + **本群已绑定战队** | 用户与身份类命令（见 §5-M02） |

**特例（改动时注意）：**

- `/帮助` **只做 `_check_enabled()`** —— 群被超管禁用时不回复（v1.13.0 补上，此前完全不校验）；但**不**检查数据库就绪、**不**要求绑定战队（有意为之：它是"命令怎么不灵"的自助排查入口，被未绑定拦住会自相矛盾）
- `/导出群成员` 只检查超管身份，**不检查**数据库（它只用 QQ 接口）
- `/第N轮`、`/记录` 只用 `_check_enabled()`、不用 `_check_db()` —— 它们纯做文本搬运，**完全不碰数据库**

### 3.3 群级开关

- `group_ban` 表：超管可整群禁用插件。所有走 `_group_check`/`_check_enabled` 的命令会被拦截
- `group_chat_type` 表：群属性（友谊群/战报群/主群），**仅影响 `/帮助` 显示哪些分类**（见 §5-M11）
- 配置 `submit_requires_admin`：开启后仅 AstrBot 管理员可提交战报（群管理**不在**此列，注意与 `_is_manager` 的区别）

---

## 4. 数据模型

### 4.1 表结构

| 表 | 关键字段 | 说明 |
|---|---|---|
| **matches** | `id`, `group_id`, `team_a`, `team_b`, `match_time`, `rule`, `location`, `submitted_by`, `submitted_name`, `created_at`, `winner`, `home_team`, `raw_text`, `fingerprint`, `kind` | 一份战报一条。`raw_text` 存原始文本用于逐字回放；`fingerprint` 是内容指纹（见 §5-M12）；`kind` 为 `'friendly'`（缺省）/ `'raid'`（踢馆，见 §5-M13） |
| **duels** | `id`, `match_id`, `round_no`, `player_a`, `score_a`, `player_b`, `score_b`, `player_a_team`, `player_b_team`, `result`, `seq`, `a_sub`, `b_sub`, `ruled`, `owner` | 对局。`result` 为 ENUM('A','B','DRAW')；`seq` 是提交时的原始顺序（还原导出用）；`owner` 表示本局**防守方（右侧）**是馆主（仅踢馆报有意义，见 §5-M13）；外键 CASCADE 到 matches |
| **teams** | `group_id`, `team_name`, `player_name` | `/排表` 写入的名单（按群覆盖式）。**不参与任何统计**，仅留档 |
| **group_home** | `group_id`, `home_team`, `created_at` | 群 → 主体战队绑定 |
| **group_ban** | `group_id`, `banned`, `created_at` | 群级功能开关 |
| **group_chat_type** | `group_id`, `chat_type` | 群属性，缺省友谊群 |
| **users** | `id`, `home_team`, `name`, `qq_id`, `created_at` | 角色。`UNIQUE(home_team, name)` |
| **player_ids** | `id`, `home_team`, `player_name`, `user_id`, `created_at` | 参赛ID池 + 绑定。`user_id` NULL = 未绑定。`UNIQUE(home_team, player_name)` |
| **settlements** | `id`, `home_team`, `month`, `player_name`, `rank_no`, `total_points`, `bonus`, `created_at` | 月度结算结果（见 §5-M14）。`month` 为 `'YYYY-MM'`；`player_name` 是**解析后的展示名**（M-03 口径，与 `get_player_ranking` 的 `player` 同源）；`UNIQUE(home_team, month, player_name)` 让重结算可以直接覆盖 |
| **schema_version** | `version` | 迁移游标，单行 |

**关系：** `matches 1─N duels`；`player_ids.user_id → users.id`；`player_ids` 同时充当"参赛ID池"和"绑定关系"两张脸的单一表。

### 4.2 迁移史（`database._init_schema`，按 `current < N` 顺序执行）

| 版本 | 内容 |
|---|---|
| v2 | matches 加 `winner` |
| v3 | matches 加 `home_team` |
| v6 | `team_players` 与 `player_ids` 合并为单表；从旧表或 duels 回填 ID 池 |
| v7 | 新建 `group_ban` |
| v8 | matches 加 `raw_text`；duels 加 `seq` 并回填 |
| v9 | duels 加 `a_sub` / `b_sub` |
| v10 | 主键/外键/QQ/时间戳 `INT → BIGINT`（先删外键再改类型再重建） |
| v11 | 新建 `group_chat_type` |
| v12 | duels 加 `a_ruled` / `b_ruled` |
| v13 | 合并 `a_ruled`/`b_ruled` → 单字段 `ruled` |
| v14 | 回填替补标记（首轮未出场却在后续轮次出场者标 `a_sub`/`b_sub`） |
| v15 | matches 加 `fingerprint CHAR(64) NULL` + 唯一键 `uk_matches_fingerprint`（重复战报去重，见 §5-M12）。历史行留 `NULL`，不受约束 |
| v16 | matches 加 `kind VARCHAR(16) NOT NULL DEFAULT 'friendly'`；duels 加 `owner TINYINT NOT NULL DEFAULT 0`（踢馆，见 §5-M13）。两份 ALTER 都先探 `information_schema.COLUMNS` 再加，可重放；历史行自动落到 `'friendly'` / `0` |
| v17 | 新建 `settlements`（月度结算，见 §5-M14）。**没有 `current < 17` 分支** —— 表用 `CREATE TABLE IF NOT EXISTS` 与其它基础表并列无条件建好，语句本身幂等，不需要 `information_schema` 探测（探测只对 `ALTER` 是必需的），也没有存量数据要迁；靠末尾那句统一的「写版本行」把版本推到 17 |

> ⚠️ **写迁移的坑**（已在 v14 踩过）：MySQL 不允许 `UPDATE duels` 的子查询里直接 `SELECT FROM duels`（错误 1093）。必须把子查询包成派生表 `(…) r1` 强制物化，否则 `initialize()` 抛异常 → 全插件报"数据库未连接"。
>
> ⚠️ **迁移必须可重放**：`_init_schema` **没有外层 try/except**，且只在**所有**分支跑完后才写 `schema_version`。所以任何一条 `ALTER` 在"已经加过该列"的库上失败一次，`initialize()` 就会抛异常 → `db_ready=False` → 整个插件不可用。加列/加索引前一律先用 `information_schema.COLUMNS` / `information_schema.STATISTICS` 探测（v15 就是这么写的）。

**新增字段的标准动作：** 建表语句 + `SCHEMA_VERSION` +1 + `_init_schema` 追加 `current < N` 分支 + 历史数据回填（或 `scripts/` 脚本）+ 所有 SELECT 补列（§7.2）。

---

## 5. 全局口径（横切机制）★核心章节

以下 14 条被多个功能共用。**改动其中任何一条，都必须按 §7 矩阵检查全部使用方。**

---

### M-01 群隔离键 `group_id`

- **来源**：群聊 → `event.get_group_id()`；私聊 → 战报的 `地点:` 字段（需配置 `allow_private_chat=true`）
- **落库范围**：`matches.group_id`、`teams.group_id`、`group_home`/`group_ban`/`group_chat_type` 的 `group_id`
- **真正生效的地方只有两处**：
  1. **删除校验** —— `delete_match()` 要求 `group_id` 匹配，防止跨群删
  2. **撤销** —— `get_last_match_by_submitter()` 按 `group_id + submitted_by` 找最近一条
- **⚠️ 统计查询完全不按 `group_id` 过滤！** 所有聚合 SQL 都是 `WHERE m.home_team = %s`，即**按战队跨群聚合**。

> 这是最容易误判的一条：群隔离**只在写入/删除**层面成立。同一个战队有 3 个群 → 排行/战绩/趋势/导出会把 3 个群的战报**合并统计**。这是设计意图（同一战队多群），但如果你以为改群隔离就是改个 `WHERE`，会把跨群聚合整个改坏。
>
> ⚠️ 这句话曾被写成"数据按群隔离"（`main.py` 模块 docstring 与旧版 README 都是这个措辞），已订正为准确说法。**新增的文档/注释不要再简写成"按群隔离"** —— 那句话只对写入路径成立。

---

### M-02 战队归属 `home_team`

**写入路径（唯一）：** `/发送` 要求本群已绑定战队，否则拒收；`matches.home_team = 群绑定战队`。并且校验**战报的 `战队:` 行必须包含本战队**，否则阻止（防误归队 / 脏队标入库）。

**读取路径（唯一）：** `_require_home()` —— **仅群绑定，无配置兜底**。未绑定的群一律拒绝，提示 `/绑定战队`。使用者：`/排行`、`/战绩`、`/趋势`、`/导出`、`/踢馆`、`/我的ID`、`/查ID`、`/绑定ID`、`/解绑ID`、`/管理ID`、`/我的战绩`、`/改名`。

> **v1.13.0 起已收敛为一条路径。** 此前存在 `_get_effective_home()`（群绑定 → 配置 `home_team` 兜底），导致"未绑定的群 `/排行` 能出 KC 的数据、`/查ID` 却报未绑定"。配置项 `home_team` 已随之删除。
>
> ⚠️ 新增命令若需要战队，只能用 `_require_home()`；**不要再引入任何形式的配置兜底** —— 那会让未绑定群的数据挂到别的战队名下。

**绑定后回填：** `/绑定战队` 会调 `backfill_group_home()`，把该群 `home_team = ''` 的旧战报补上。**只补空值**，不会改已有归属。

**SQL 侧约定：** 几乎每条聚合 SQL 都带 `m.home_team = %s`。新增查询漏了它 → 跨战队串数据。

> ⚠️ **唯一的例外是踢馆（§5-M13）**：踢馆的战队/个人统计按 `(m.team_a = %s OR m.team_b = %s)` **对称取数**，不看 `home_team`。因为踢馆报的 `home_team` 只是提交方（通常是踢馆方），照 M-02 取数的话**守馆方永远拿不到守馆积分**。踢馆报的 `home_team` 仍然照常写入（写入路径不变），只是踢馆查询不用它。

---

### M-03 玩家名解析口径（角色聚合）

**定义：** 显示名 = `COALESCE(users.name, 参赛ID)`。即：参赛ID 若被绑定到某角色，就用**角色名**展示并聚合该角色下**全部**参赛ID；未绑定则直接用参赛ID。

**两套实现必须一致：**

- **SQL 侧**（`get_player_ranking` 的 `sides` CTE ×2、`get_player_match_stats` ×2）：
  ```sql
  LEFT JOIN player_ids pi ON pi.home_team = <队> AND pi.player_name = <参赛ID>
  LEFT JOIN users u ON u.home_team = pi.home_team AND u.id = pi.user_id
  -- COALESCE(u.name, d.player_a)
  ```
- **Python 侧**（`resolve_role()`）：先按参赛ID命中 → 再按角色名命中 → 取该角色全部参赛ID。供 `/战绩`、`/趋势`、`/导出` 把"名字"翻译成"参赛ID集合"。

**跨队同名处理（三处，缺一不可）：**

1. `get_player_match_stats(team=...)`：只统计该玩家在指定战队一侧出场的比赛
2. `get_player_record()`：SQL 里带 `d.player_a_team = home_team`
3. `filter_report_outcome(member_team=...)`：导出时排除他在对手一侧的场次

**⚠️ 新增任何统计/查询/导出，必须复用这套解析**，否则同一个人会出现两个名字、数字对不上。

---

### M-04 占位对局 `0:0` 口径 ★最易出错

**语义：** `0:0` = **还没打**，不是平局。平局是 `1:1` 这类非零平分。

**已正确处理的地方：**

| 位置 | 处理 |
|---|---|
| `_winner_headcount` / `_winner_kof` | 跳过，不参与胜负 |
| `get_player_ranking` / `get_player_record` 的 `draw` | `result='DRAW' AND NOT(0:0)` 才算平 |
| 友谊次数 SQL | `AND NOT (d.score_a = 0 AND d.score_b = 0)` |
| `compute_match_stats`（无双） | 完全忽略该对局 |
| `_player_has_duel_result`（导出胜负过滤） | 不算胜也不算负 |
| KOF 容量 | 计入"可出战人数"，但不计"落败" |

**注意两个 total 不是同一个数：** SQL 侧的 `total` / `COUNT(*)` 含 `0:0`，而文字/图片排行表用的是 `played_total = wins + losses + draws`（不含 `0:0`）。受影响的判定有三处：

1. `min_games` 用含 `0:0` 的 `total`（`HAVING total >= %s`）
2. 排名并列判定 key = `(points, wins, total)`
3. `/战绩` 显示的"总N" = 胜 + 负 + 平 + 占位场次

> **实际上不构成问题**：带未填比分的战报会被 `/发送` 的胜负判定直接拒收（`determine_match_winner` 返回 `None`），根本进不了库。理论上唯一漏网场景是"占位行落在**胜方**、且对方已全员落败"——此时胜负可判、战报会带着 `0:0` 入库。§10-2 留了一条只读查证 SQL 可确认库里有没有这种行。
>
> **新增统计时必须显式决定：算不算 `0:0`。** 不要默认沿用旁边那行的写法 —— 旁边可能就是这两种口径之一。

**踢馆侧多一种等价写法（§5-M13）：** 无人守馆的踢馆报里，防守方名字写占位符 `规则`，比分一律 `0:0`。所以踢馆的「没打」判定是**两个条件的并集** —— 防守方是 `规则` **或** 比分 `0:0`（`stats._unplayed`）。只判其中一条都会漏。

> ⚠️ **占位行照常入库**（不是解析期丢弃）。丢掉整行会让"无人守馆"的战报里连踢馆者的名字都查不到，徽章也就发不出去。占位行走全库通用的 `0:0` 口径自动被排除在胜负与人数之外；**唯一需要额外处理的是建参赛ID**：`insert_report` 跳过 `name == RAID_PLACEHOLDER` 的两侧，否则 ID 池里会多出一个叫「规则」的假人（`player_ids.UNIQUE(home_team, player_name)` 会让它固化下来）。

---

### M-05 比分、结果与判罚 `ruled`

- `duels.result` 在**插入时**由比分比较算好落库（A / B / DRAW），后续统计全部读它，**不回读比分**
- `ruled`（单字段，v13 合并自 a_ruled/b_ruled）：本对局有判罚落败
  - **判罚方比分必然更低** → 必为败方 → `result` 自然正确，无需特殊处理
  - **双方不会被同时判罚**（保证单一字段够用）
  - `_clean_player_name()` 循环剥离 `(规则)` 与 `(替)` 标记，可叠加
- **还原时补标记**：`lineup.format_duels_block()` 给败方 ID 补 `(规则)`；`format_duel_results()`（提交后核对）**不显示**替补/判罚标记，只显示干净 ID

> 若要新增"判罚类型/裁判/申诉"等字段：单字段 `ruled` 的表达力就到此为止了，需要改表 + 解析器（`_RULED_RE`）+ 还原（`format_duels_block`）+ CSV 列。

---

### M-06 替补标记 `sub`

**两个来源，会叠加：**

1. **显式标记**：ID 后 `(替)`、`（替补）` 等（`_SUB_RE`），提交时剥离，只存干净 ID
2. **隐式推断**（v14）：`round_no > 1` 且该侧**第一轮名单中没有此人** → 视为替补。即使文本没写 `(替)`

**生效范围：**

| 受影响 | 不受影响 |
|---|---|
| KOF 可出战容量（key 含 sub，替补不扩容） | 无双判定（用首轮名单 `roster`，不看 sub 标记） |
| 导出/回放（`format_duels_block` 补 `(替)`） | 排行、友谊次数、积分 |
| 提交后核对（**不显示**，见上） | 胜负判定的人头赛分支 |

**⚠️ 关键不变量：** 同一场比赛里，同一个名字可以同时以"首发"和"替补"两种身份存在（KOF 的 last 字典 key 是 `(玩家, 是否替补)`）。改动前先读 `test_kof_same_name_regular_and_sub_distinct`。

**领域规则（用户确认，勿擅改）：** 替补是**替换**首轮选手、**不扩容**总出战人数；替补可能真的没被淘汰。

---

### M-07 比赛胜负判定 `determine_match_winner`

**入口：** 规则行含「人头」→ `_winner_headcount`；否则按 KOF `_winner_kof`。

| 规则 | 判定 |
|---|---|
| 人头赛 | 获胜对局数多者胜；平局返回 None |
| 2/3【KOF】 | 某队"不可出战"人数 ≥ 该队**第一轮出场人数** → 该队负 |

**返回 `None` = 胜负未定 → `/发送` 直接拒录**（不落库，并列出未填比分的对局）。这是**有意为之**：宁少一条，不记错一条。

**下游依赖 `matches.winner` 的功能（改判定逻辑等于同时改这些）：**

1. 战队总战绩 `get_home_team_record` —— `winner` 是唯一依据
2. 战队对战记录 `get_home_team_vs_opponents`
3. 导出 胜场/负场 过滤（未指定玩家时：`winner == home_team` 为胜）
4. 无双的 `winner` 守卫（非空时仅给胜方成员记无双，保证每场至多一人）

> ⚠️ **历史数据不会重算。** 改判定逻辑后，旧 `matches.winner` 仍是旧口径，需要迁移或脚本回填。
> **无双兜底已按规则门控（v1.13.0）**：`compute_match_stats` 里"排除败方首轮名单却最后一场为胜的选手"这条兜底**只在 KOF 成立**，现在由 `is_kof(rule)` 把关，人头赛不套用。判定 `rule` 是否 KOF 的**唯一实现**是 `battle_report_parser.is_kof()` —— `determine_match_winner` 与 `compute_match_stats` 共用它，不要再各写一份 `"人头" in rule`。

**踢馆赛不走上面任何一条（v1.14.0）：** `determine_match_winner` **开头**按 `report.kind` 分流到 `determine_raid_winner` —— 踢馆**看最后一场**，与 KOF/人头赛无关（踢馆报的 `规则:` 行照样要写，但不参与判定）。分流点刻意放在唯一入口内部（M-07 不变），调用方（`/发送`、导出过滤、无双守卫）一律不需要知道有两种判定。详见 §5-M13。

---

### M-08 时间范围口径

**三种形态：** `X月` / `最近N天` / 默认本月

| 参数 | 解析函数 | 说明 |
|---|---|---|
| `七月`、`7月`、`时间=7月` | `_parse_month_filter` | **纯数字不带"月"不识别**（避免与"最近7天"的裸数字冲突） |
| `最近7天`、`7天` | `_DAYS_RE` / `_TOKEN_DAYS_RE` | 仅 `/趋势`、`/导出` |
| `csv` / `json` | `parse_export_payload` | 仅 `/导出`，**顺序无关** |

- 月份区间：`month_range(month)` → 按**当前年份**算当月 1 号 ~ 月末
- `最近N天`：`_date_from(d)` → 只有起点，**无终点**
- **优先级：月份 > 最近N天 > 默认本月**（`/趋势`、`/导出`）
- 使用者：`/排行`、`/战绩`、`/趋势`、`/导出`、`/我的战绩`

> 配置项 `default_days`（"默认统计时间范围"）曾是**死配置、代码从未读取**，已于 v1.13.0 删除。实际默认恒为"本月"。

---

### M-09 输出与渲染

| 输出形态 | 实现 | 触发条件 |
|---|---|---|
| 文本表格 | `stats.format_player_ranking` / `_format_table`（按显示宽度对齐，CJK 算 2） | `ranking_image=false` 或图片生成失败 |
| 排行图片 | `chart.make_ranking_image` | `ranking_image=true`（默认），失败自动回退文本 |
| 趋势图 | `chart.make_trend_chart` | `/趋势` 恒为图片 |
| 合并转发 | `_send_responses`（≥2 条时封装 Node） | 提交结果、导出 |
| 文件 | `File` 组件 + `data_dir/exports/` | `/导出 csv/json`、`/导出群成员` |

**★ 文本表格与图片表格必须同源：** 两者共用 `stats.build_ranking_cells()`（表头 + 单元格值 + 并列名次规则全在这）。**新增排行列只能改这一个函数**，改 `format_player_ranking` 或 `make_ranking_image` 单独加列会导致两种输出不一致。

**列对齐只有一份（v1.13.0 收敛，v1.15.0 改为函数）：** `stats.rank_aligns(ncols) -> ["center"] * ncols`。v1.13.0 时它是个常量 `RANK_ALIGNS = ["right", "left"] + ["right"] * (len(_RANK_HEADERS) - 2)`；v1.15.0 改成**按列数生成的函数**，因为「仅已结算月份显示奖金列」让列数在 **14/15** 之间浮动，定长常量跟不动。语义也一并从「队员左对齐 + 其余右对齐」改为**全部居中**。`main.py` 的 `ranking()`、`format_player_ranking` 都引用它；测试里也不再手写字面量（`_aligns(cells)` 辅助）。加列时它自动跟随 —— 但**数据行是手写的定长列表，必须手动同步**（`build_ranking_cells` 的 docstring 里有提醒）。

---

### M-10 平台消息收发（QQ 适配器口径）

战报文本在群里"流浪"，读写消息是本插件的一大块隐式依赖：

| 工具 | 用途 | 关键点 |
|---|---|---|
| `_read_latest_report()` | 读"最近一条战报" | `get_group_msg_history(count=50)`，取 `time` 最大且**行首为 `战队:`** 的消息 |
| `_extract_reply_reports()` | 从引用消息提取 | 支持引用**合并转发**（`get_forward_msg` 逐条提取） |
| `_extract_msg_text()` | 提取纯文本 | message 段拼接 vs `raw_message` **取较长者**（防平台截断） |
| `_read_msg_by_id_from_history()` | 截断兜底 | `get_msg` 结果解析失败时，按 ID 回群历史重取 |
| `_send_responses()` | 分段回复 | **1 条** → `plain_result` 逐条发；**≥2 条** → `Node` 合并转发。注意调用处的注释写着"≤3 逐条"，与实现不符（以代码为准） |
| 导出分批 | 合并转发节点上限 | `max_nodes = 100`（QQ 限制） |
| `split_reports()` | 拆分多份战报 | 同样以**行首 `战队:`** 为分隔标志 |

> **"行首 `战队:`" 是战报的识别标志，出现在 3 个地方**（读最近战报、拆多份、引用转发过滤）。改战报头部格式（比如把 `战队: KC VS DYG` 改成 `队伍：KC vs DYG`）会同时打断这三处，且**旧消息里的战报会全部读不到**。

**★ 文案口吻（v1.15.0）：改「句子」，不改「数据」。** 所有面向用户的**叙述性**回复统一为猫娘口吻，中等浓度：句尾「喵」/「喵～」，关键回执配**一个**颜文字（取固定小集合 `(=^･ω･^=)` / `(；・∀・)` / `(๑•̀ㅂ•́)و` / `🐾`，与既有结算公告一致；不是每句都配，长回复只在结尾）。绝对**不**自称「人家」、**不**称用户「主人」。

**不在此列**（一字不动，改了一处就会连锁出 bug 或被测试钉死）：

- 命令名与参数（`/绑定战队 <战队名>`、`/结算 [月份] [名次数]`）
- 表格表头 `_RANK_HEADERS` / `_BONUS_HEADER`、图片面板标签 `_TEAM_PANEL_LABELS`、CSV 表头
- `lineup.generate_template` 的战报模板与 `main._FORMAT_EXAMPLE` —— **对局行的双空格是 `battle_report_parser` 的解析契约**
- `lineup.format_duel_results` / `format_raid_results` 的**逐场数据行**
- `/查ID` `/管理ID` `/群列表` `/战队列表` 的**列表项行**、数值与小数位
- `render_help` 的正文（只改抬头与收尾；`HELP_SECTIONS` 的栏目名与命令语法一字不动）

> **「结算完成」/「结算查询」的措辞是判据，不是文案**（§5-M14、§7.1）—— 这两个词分别承担「写过库」与「只读查询」的区分，且 CLAUDE.md 要求两个回执**刻意不同形**，不许当文案顺手改得一致。
>
> **「改句子不改数据」这条线的守卫在测试里**：叙述句只断言「含语气词且关键信息仍在」，数据行则断言**逐字不变**（`test_match_stats.py` 的 `format_player_record` 第二行、`test_settlement.py` 的 `_settlement_lines` 行体）。见 §10.4-31。

---

### M-11 群属性与帮助分类

- `CHAT_TYPE_SECTIONS`（[stats.py](stats.py)）：友谊群 → 排表/追加轮次/记录比分；战报群 → 提交/管理/查询；主群 → 查询/用户与参赛ID
- `ALL_SECTIONS` = `HELP_SECTIONS` 中除"超级管理"外的全部键（自动推导，新增 section 自动纳入）
- `/帮助` 无参数按本群属性展示；`/帮助 全部` 展示除超管外全部；`/帮助 超管` 仅超管段

> **新增命令必须同步三处：** `HELP_SECTIONS` 加段、`CHAT_TYPE_SECTIONS` 归类、命令别名（带 `/` 与不带 `/` 两种写法）。

---

### M-12 重复战报去重（内容指纹）★v1.13.0 新增

**定义：** `battle_report_parser.report_fingerprint(report, home_team)` → sha256 十六进制，覆盖「这份战报说了什么」：

| **进**指纹（改了就换一条记录） | **不进**指纹（改了仍是同一条） |
|---|---|
| `home_team`（strip + 大写）、`team_a`、`team_b`（**保持原序，不排序**）、`match_time`、`rule`（小写）、逐局的 `(round_no, player_a, score_a, player_b, score_b, a_sub, b_sub, ruled)`；踢馆报**另加**逐局的 `owner` | `group_id`、`location`、`submitted_by`、`submitted_name`、`raw_text`、`created_at` |

**`kind` 只在非友谊赛时才进指纹（v1.14.0）：**

```python
if kind and kind != "friendly":
    payload["kind"] = kind
```

这样**友谊赛的指纹与引入踢馆之前逐字节一致** —— 已有行的指纹仍然有效（改算法 = 破坏历史去重，见下面的警告），`tests/test_fingerprint.py` 里写死的锚点也仍然成立；踢馆报则自带类型区分，不会被当成"同内容的友谊报"。同理 `owner` 也只对踢馆报写入 payload —— 否则 `S4`（含馆主）与 `S5`（不含）会算出同一个指纹。

**为什么 `group_id` 不进指纹：** 统计按战队**跨群聚合**（M-01）。KC 群和 DYG 群各提交一次同一场比赛是同一件事；指纹若含 `group_id`，库里会留两条、统计直接翻倍 —— 那正是本机制要防的问题。

**落库与拒收：**

- `matches.fingerprint CHAR(64) NULL` + `UNIQUE KEY uk_matches_fingerprint`（v15）。**用 NULL 而不是 NOT NULL 是关键**：MySQL 唯一索引允许多个 NULL，历史行留 NULL 即不受约束 —— 无需回填、不会因存量重复导致迁移失败。
- `insert_report`：先 `SELECT` 预检 → 命中抛 `DuplicateReportError(match_id)`；INSERT 再捕获 MySQL **1062**（`ER.DUP_ENTRY`）作**并发兜底**（两个请求同时穿过预检时靠唯一索引拦住）。
- 1062 分支必须在**回滚后用新连接**回查已有 ID：InnoDB 唯一键冲突会阻塞到对方事务提交，同一事务内（REPEATABLE READ）重查看不到那一行。
- 注意：aiomysql **没有** `IntegrityError`，异常要从 `pymysql.err` 导入。

> ⚠️ **改指纹算法 = 破坏历史去重。** 已有行的指纹是按旧算法算的，改算法（哪怕只是字段顺序）会让同一份战报能再存一次。`tests/test_fingerprint.py` 里钉了一个写死的期望值作回归锚点。
>
> ⚠️ **只按"完全一样"判重。** 同一天同一对阵打多场是正常的（用户已确认），不做软警告、不做时间窗启发式 —— 由用户自己控制内容。

---

### M-13 踢馆赛 ★v1.14.0 新增

踢馆 = 一支战队去另一支战队的馆 1v5：**踢馆方**要从头打穿全部防守者，**守馆方**只要终结踢馆者一次就算守住。踢馆产生独立的「踢馆积分」，与友谊积分**合计**为「总积分」参与排名。

#### 格式与解析

```
KC踢馆BAR                     ← 首行：左=踢馆方(team_a)，右=守馆方(team_b)
规则：OCG.2026.7.1.MATCH      ← 与友谊赛同一个 RULE_RE；不参与胜负判定
2026.8.30 21:00               ← 裸时间行，无 `时间:` 前缀
踢馆开始！                     ← 标记行，静默跳过
雨落 2:1 念心                  ← 对局行，左恒为踢馆方
雨落 1:2 云猫(馆主)            ← `(馆主)` = 本局防守方是馆主
```

- **分流**：`main._is_report_text` / `_parse_chunk` 按首行认 `战队:` 或踢馆头（`battle_report_parser.is_raid_header` → `RAID_RE`）；`split_reports` 同样按这两种首行切分多份。友谊赛路径**零改动**（`parse_raid_report` 是独立实现）。
- **无轮次**：所有对局的 `round_no` 恒为 `0` → `format_duels_block` 对 `round_no == 0` 不输出轮次头。
- **`(馆主)` 只认右侧**（防守方），左侧出现时剥离但忽略 —— 馆主只可能是守馆方。
- **`2.1` 小数比分升级为错误**（v1.14.0 起）：`_DOT_SCORE_RE` 命中但 `SCORE_RE` 未命中 → **整份拒收**，不再静默丢行。**友谊赛同样生效**（此前是静默忽略该行）。

#### 占位与「没打」

- 防守方名字恰为 `规则`（`RAID_PLACEHOLDER`）的对局行 = **空位占位**（守馆方没派这个人），比分一律 `0:0`。
- 踢馆的「没打」= **`规则` 占位 ∪ 比分 `0:0`**（`stats._unplayed`）—— 见 §5-M04 的补充。
- 全占位 = **无人守馆** → 判踢馆方胜，**0 分**，只发徽章。
- **占位行照常入库**（不丢整行），只是**不建参赛ID**。

#### 胜负判定

**看最后一场**（跳过没打的）：最后一场踢馆者胜 → 踢馆成功；最后一场防守者胜 → 守馆成功；平局 → `None`（`/发送` 拒收，与 M-07 一致）。无人守馆（全是占位）→ `team_a` 胜。

#### 积分公式

**踢馆方（成功踢破才得分，按场）：**

| 条件 | 分 |
|---|---|
| 防守者 ≥ 4 人 | +3（规则原文「3 个防守者以上，不包括 3 个」） |
| 防守者 1–3 人 | +2 |
| 无人守馆 | +0（只发徽章） |
| 该馆有馆主守且被踢破 | **再 +2**（与人数档**叠加**，所以 4 人 + 馆主 = 5 分） |

**守馆方（月度累计）：** `ceil(守馆成功/3) + ceil(守馆首轮/3) + 5 × SHUT DOWN`，**上限 10 分**。

- ⚠️ **是 ceiling 不是 floor**（v1.14.0 修正）：规则原文「守馆总数除以 3 由上取整」。曾经误写成 `n // 3`（floor），导致**守馆成功 1~2 次时得 0 分** —— 用户实测 2 把首轮守馆（守馆成功 2、守馆首轮 2）积分仍显示 0 才暴露。`stats._ceil_div3(n) = -(-n // 3)` 是唯一实现，两项奖励**各自取整后再相加**（`(1,1)` → 1+1 = 2，而合并成 `ceil(2/3)` 会只得 1）。
- **首轮被终结同时计一次「守馆成功」和一次「守馆首轮」**（用户确认口径，两条奖励独立累积）
- **SHUT DOWN** = 馆主作为**第 5 个**防守者出场并击败踢馆者 → +5
- **上限 10 包含 SHUT DOWN 的 +5**（规则只写「守馆积分的上限为 10」，未排除 +5；用户确认按包含实现）

**月度降重（踢馆方）：** 当月**重复踢穿同一战队不累计**，按对手队取**最高分那场**再求和；踢不同队才累计。守馆侧没有这条，直接按次数进上面的公式（受上限约束）。

**个人与战队同一套规则：** 个人积分**不是**把场次直接相加 —— 同一套降重与封顶要在**合并后的集合上**重算（`get_raid_stats_for_players` 先按 `(match_id, 阵营)` 去重再 `aggregate_raid`），否则一个用户挂多个参赛ID时会重复计分。

#### 统计取数（M-02 的例外）

踢馆的战队/个人统计按 `(m.team_a = %s OR m.team_b = %s)` **对称取数**，**不看 `home_team`** —— 否则守馆方永远拿不到守馆积分。查询入口：`get_raid_matches` → `_raid_infos` → `get_raid_team_stats` / `get_raid_player_stats` / `get_raid_stats_for_players`。玩家名照 M-03 解析。

> **守馆积分摊不到单个对手头上**（它是月度封顶的总额），所以 `get_raid_points_by_opponent` 只含踢馆**加点**。

#### 与友谊统计的隔离 ★最易出错

**踢馆对局不进任何友谊口径**（否则「友谊积分」列的数值会变，与「积分只算友谊、另加总积分列」的展示口径自相矛盾）。

实现方式：`matches.kind = 'friendly'` 过滤。**这是一次全表扫描式的改动，漏一处就是静默的数据污染** —— 已加过滤的 18 处：

| 文件 | 位置 |
|---|---|
| `database.py` | `get_player_ranking`（2 个 sides CTE）、`get_player_match_stats`、`get_player_record`（2 个 CTE + 1 个友谊子查询）、`get_players_aggregate` 的友谊查询、`get_home_team_vs_opponents`、`get_home_team_record`、`get_player_trend`（2）、`get_players_trend`（2）、`get_team_trend` 的 team_sides（2） |

**刻意不加过滤的：** `get_export_rows` / `get_reports_for_export`（踢馆报也要能导出，`m.kind` 随行返回）、`delete_match` / `get_last_match_by_submitter` / `backfill_group_home`（按 group/id 定位，与类型无关）、`get_all_teams`（队名并集，踢馆队名应当收录）、`replace_teams`。

> **自查命令**：改完任何统计查询后跑 `grep -n "FROM duels\|FROM matches" database.py`，逐个确认新加的查询**要么**带 `AND m.kind = 'friendly'`，**要么**在上一段的例外清单里。验收标准见 §12 的 `test_raid_does_not_move_friendly_numbers`。

#### 展示口径

- 排行（14 列，v1.14.0 定稿顺序）：

  `排名｜队员｜总积分｜友谊积分｜胜场｜负场｜总场数｜友谊次数｜胜率｜无双次数｜踢馆积分｜守馆成功｜守馆首轮｜踢馆成功`

  **按总积分排序**，并列 key = `(total_points, wins, total)`。**该月已结算时末尾追加第 15 列「奖金」**（v1.15.0，见 §5-M14）。友谊的那一列叫「**友谊积分**」（v1.14.0 从「积分」改名）—— 它与「踢馆积分」同一量纲、都不含另一侧；「总积分」= 两者之和且排在**第 3 位最前**，因为它是实际排序依据。
- **末三列「守馆成功 / 守馆首轮 / 踢馆成功」是次数不是分数**（v1.14.0 新增），放在「无双次数」之后与其它的次数字段连成一片。踢馆方看末列、守馆方看前两列，同一场两个数可能都非零（首轮终结同时计守馆成功与守馆首轮）。
- 排序在 **Python** 侧完成（`stats.sort_ranking`）—— 踢馆积分是在 SQL 之外并进来的，`ORDER BY … LIMIT` 会在合并前把被踢馆顶上去的选手截掉，所以 `get_player_ranking` 的 SQL 只留 `ORDER BY`、不留 `LIMIT`。
- 4 个计数字段 = **踢馆成功 / 踢馆成功含馆主 / 守馆成功 / 守馆首轮**；另有 `shutdown`（SHUT DOWN 次数）与 `badge`（无人守馆次数）两个附加计数。**`/战绩`、`/我的战绩`、战队战绩、`/踢馆` 卡**都带上「踢馆成功 / 守馆成功 / 守馆首轮」三个数与排行末三列同源（`get_player_record(with_raid=True)` / `get_home_team_record` 现各多返回 `raid_success` / `hold` / `first_round` 三个键）。
- **回执措辞整段按提交方视角**（逐局 v1.14.0，**结论行 v1.15.0 补齐**）：`lineup.format_raid_results` 里 ✅/❌ 后缀是「进攻成功 / 进攻失败」还是「防守成功 / 防守失败」，由**本群 `home_team` 是 `team_a` 还是 `team_b`** 决定（不是由左侧恒为踢馆方决定）。对局行本身恒为 `踢馆方 分数 守馆方`，只有措辞跟随视角；平局/未完走 `➖ 未打`。

  **末尾那行结论也是同一个视角**，四种组合：

  | 视角 | 踢穿（`raider_success`） | 没踢穿（`hold`） |
  |---|---|---|
  | 踢馆方（`home_team == team_a`） | `⚔️ 踢馆成功（防守者N人[，含馆主]），踢馆积分+X` | `💀 踢馆失败（team_a），防守者N人[，含馆主]` |
  | 守馆方（`home_team == team_b`） | `💀 守馆失败（team_b），防守者N人[，含馆主]` | `🛡️ 守馆成功（team_b），防守者N人[，含馆主]` |

  ⚠️ 这行**曾经恒用守馆方措辞**（不论视角都说 `🛡️ 守馆成功（team_b）`），于是没踢穿的战队在自己群里看到的是**对手**的守馆成功、被踢穿的守馆方看到的却像自己赢了 —— 与同一函数里跟随视角的 ✅/❌ 自相矛盾。`tests/test_raid.py` 的 `test_format_raid_results_hold_verdict`（踢馆方）/ `_hold_verdict_defender_perspective` / `_breached_verdict_defender_perspective` 三种组合分别钉住。

#### 未覆盖 / 已决定的边界

- **多踢馆者**：格式上恒为 1 人。若左侧出现多个出场者，**每个出场者都拿到该场的踢馆方得分**（规则未覆盖，按此实现）。
- **只踢馆不打友谊的玩家**：不出现在 `/排行`（被 `min_games` 的 `HAVING` 卡在友谊场次上），但可用 `/踢馆` 和 `/战绩` 查到。
- **同一条踢馆记录可能被重复提交**：`kind` 进指纹后，踢馆报与同内容的友谊报不再互判重复；踢馆报之间仍按 M-12 判重。

### M-14 月度结算 / 奖金 ★v1.15.0 新增

管理员用 `/结算 <月份> [名次数]` 把某个月的榜单前 N 名连同奖金**快照**进 `settlements` 表。

v1.15.0 起同一个命令对**非管理/群主**开放为**只读查询**（查已结算的名单与金额），**不写库、不重算** —— 见下方「只读查询」小节。

#### 奖金公式（`stats.rank_bonus`，唯一实现）

```
个人奖金 = floor( min(总积分 × 3, 该名次上限) )
```

| 名次 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 上限 | 130 | 100 | 80 | 50 | 40 | 35 | 30 | 25 | 15 | 15 | 15 | 15 |

- 基数恒为**总积分**（= 友谊积分 + 踢馆积分，即排行榜的排序键，M-13 展示口径），**不是**友谊积分。
- 名次不在 1~12（含 0、负数、13+）→ **0 分**（`RANK_BONUS_CAPS.get(rank)` 返回 `None`）。
- **奖金是派生的展示值**：不写回 `total_points`、不参与排序、不影响任何其它统计。结算**不改变排行榜上的任何数字**。
- **向下取整**（v1.15.0 用户口径）：总积分是小数（`胜场²/场次`、踢馆加点都可能带小数），×3 后几乎必定带尾巴，奖金一律**只舍不入**抹成整数 —— `34.17 → 34`、`35.97 → 35`（**不是四舍五入的 36**）。上限表全是整数，`floor` 对触顶的那批无影响。
  > 此前是 `round(…, 2)`，会发出 `34.17` 这种带分的金额；`DECIMAL(8,2)` 的列宽保留着（够用且不用改表），只是值恒为整数。

#### 名次口径：`stats.ranks_for`（与排行表格**共用同一实现**）

并列 key = `(total_points, wins, total)`（与 §5-M13 展示口径的排序 key 完全一致），三者全同才并列，并列后名次跳到 `i`（`1,1,3`）。返回的是**每行的展示名次**，与表格「排名」列逐行相同。

> ⚠️ **这是 v1.15.0 的消重项**：这段逻辑此前只长在 `build_ranking_cells` 里。结算若另写一遍，一旦两处口径漂移就会出现「榜上显示第 1 名、奖金却按第 2 档发」。所以抽成 `ranks_for(rows)` 由两边共用，`test_ranks_for_matches_table_column` 钉住一致性。

#### 月份：缺省=上个月 + 必须已结束 + 跨年回退（`battle_report_parser.settle_month_range`）

```python
if month is None:                       # `/结算` `/重置结算` 不带月份
    month = 12 if now.month == 1 else now.month - 1   # 上个月
year = now.year - 1 if month > now.month else now.year
```

- **缺省 = 上个月**：没人会想结算一个还没过完的当月，「不写月份」的自然含义就是刚过完的那个月。1 月的上个月是 12 月，**由下面那句回退顺带处理**，不用另写一套跨年逻辑（`12 > 1` → 去年）。因此缺省目标**永远不会是当前月**，也就永远不会撞上「当月拒结」那道挡板。
- **`/重置结算` 用同一个函数**取 key —— 重置的目标月份必须与结算时算出的 `key` 一致，否则删不到东西。

- **当月一律拒绝**：`settle_month_range` 只做回退、不做校验（它无法区分「本月」与「回退后仍是本月」），**调用方 `main.settle` 必须自己挡**：`if date_to >= today: 报错「X 月尚未结束」`。这同时挡住最危险的路径 —— 9 月里手滑跑 `/结算 9月`，若不挡会静默落到**去年 9 月**去结算。
- 回执（`stats.format_settlement`）**必须回显年份**（「2026年7月」）：`/结算 十二月` 在 9 月落的是去年 12 月，只写「12月」会误导。
- ⚠️ **跨年只在 `/结算` 生效**。`month_range` 仍然**没有年份参数**（恒取 `now.year`），所以 `/排行 十二月` 在 1 月查的是**今年** 12 月，看不到去年 12 月的奖金列。这是既有的 `month_range` 年份缺口，v1.15.0 **未扩大改动面**去修（见 §10）。

#### 覆盖重算

`database.set_settlement` **先 `DELETE` 该队该月全部行再 `INSERT`**（显式事务，仿 `insert_report` 的 `begin/commit/rollback`），配合唯一键 `uk_settle` 保证幂等：

- 重跑 `/结算 7月` = 重算，旧奖金作废，**行数不翻倍**。
- **无参数重跑一律回到前 4 名**（`_parse_settle_args` 的名次数缺省 4）—— 不记忆上次的名次数，所以**不需要 `reward_ranks` 列**。追加到 12 名后再跑一次 `/结算 7月`，5~12 名的行会被删掉。
- 结算属于**战队**不是群：只发本战队（`team=home_team`），`/排行 全部` 是跨队榜故**不显示**奖金列。

#### 结算公告：@ 获奖成员 ★v1.15.0

写路径成功后，除回执外再发一条**猫娘口吻**的领奖公告，`At` 到每位获奖成员（用户要求）。

**触发条件只有两种**（`stats.settlement_awards_changed`）：

| 场景 | 是否 @ |
|---|---|
| 该月**第一次**结算（库里查不到旧记录） | ✅ @ |
| 重跑，但 `(名次, 获奖人)` 列表变了（追加名次 / 缩回前 4 / 有人被反超） | ✅ @ |
| 重跑，名单与名次**一模一样** | ❌ 不 @，回执后附一句「名次和上次一样，就不打扰大家了」|
| 非管理/群主（只读查询） | ❌ 不 @（见下节）|

- **比对键是 `(rank_no, player)`，不含奖金**：奖金由总积分派生，事后补录战报让积分微调、名次却没动时不该再 @ 一次全群。这是刻意选的（见 §10.4）。
- **读旧记录必须在 `set_settlement` 之前**：写完再读拿到的是自己刚写进去的，永远比不出变化。
- **@ 的是 `users.name` 而不是参赛ID**：榜单的 `player` 是 `COALESCE(u.name, d.player_a)`（M-03），所以 `get_qq_ids_by_names` 按 `users.name` 查。
- **@ 不出来就退化成字面「@名字」**：没绑定角色、绑了角色但没存 QQ 的成员（`users.qq_id` 为空）没有可 @ 的目标，降级成纯文本 —— 一行字面 @ 好过整条公告不发。
- **只在 QQ 平台真弹提醒**：`At` 是消息组件，非 QQ 平台（如 WebChat）渲染成什么由平台适配器决定，插件不做平台判断。
- **文案在 `stats.settlement_announcement`**，返回 `[("text", …) / ("at", 名), …]` 的**分段**而不是成品字符串 —— 拼成字符串就只剩字面的「@名字」，必须让 `main.py` 翻译成 `At` 组件。`stats.py` 不依赖 AstrBot，故只描述「哪一段要 @ 谁」。

#### 只读查询：同一个命令的第二种模式 ★v1.15.0

`/结算` 对**非管理/群主**开放为**纯查询**，只读不写：

- 走的是**同一条命令的另一个分支**，判据是入口处的 `_is_manager(event)`（与 F-20 等管理命令同一个判定），**不是**新命令、不是新参数。
- **只读 `settlements` 表**（`database.get_settlement_entries`），**不重算、不覆盖**：查到的就是当初结算那一刻的快照，之后榜单怎么变都不影响。这是刻意的 —— 重算会绕过 `set_settlement` 的覆盖语义，等于把「发过就算数」变成「随时可被任何人改写」。
- **未结算的月份只回一句「该月尚未结算」**，**不预览**「如果现在结算会发多少」。预览要把 `get_player_ranking` → `ranks_for` → `rank_bonus` 整条写路径跑一遍，等于把发钱逻辑暴露给所有群员当查询用；用户明确选择不预览。
- 回执抬头是「📋 … 结算**查询**」并带尾注「（以上为已结算结果，本次未做任何修改）」，与写路径的「⚔️ … 结算**完成**」**刻意不同形** —— 一字不差的话非管理员会以为钱刚发出去。
- 名次数那行两边不同也是刻意的：写路径回显管理员要的 `reward_ranks`，读路径只有库里实际存了几行（`len(entries)`）。**行体与「合计发放」逐字相同**（共用 `stats._settlement_lines`），`test_format_settlement_view_rows_match_settlement_receipt` 钉住。
- **`settle_month_range` 两条路径共用**（它在分叉之前调用）：读路径必须算出**同一个 `key`**，否则查不到管理员刚写进去的那份。所以 1 月里非管理员跑 `/结算 十二月` 查的是**去年** 12 月 —— 与管理员结算的是同一份，这是对的。
- **只有「当月未结束」的校验是写路径独有**：读路径查本月不拦，直接回「尚未结算」—— 本月本就不可能结算过，挡它只会让人白跑一趟还看不懂为什么。

#### 重置结算（`/重置结算`，管理/群主）★v1.15.0

把某个月**打回未结算**：删掉该队该月在 `settlements` 里的全部行，别的什么都不动。

- **只删不补**。`set_settlement` 里那句 DELETE 是「重算前先清空」的中间步骤，`clear_settlement` 把它单独暴露出来当**终点**用。
- 效果是**三个口一起回到未结算的样子**：`get_settlement` 空 dict → `/排行` 少一列奖金（14 列）；`get_settlement_entries` 空 list → `/结算` 回「该月尚未结算」；`settlement_awards_changed([], entries)` 为真 → 重新结算会**重新 @ 一遍获奖成员**。
- **该月本来就没结算过**（返回 0 行）不是错误，只回一句「无需重置」—— 管理员连点两次、或对上个月跑一次，都是正常操作。
- **不受「当月未结束」校验**：重置一个还没结算过的月份本来就无事发生，多一道挡板只会让人白跑。这与写路径 `/结算` 的校验**刻意不同**。
- **权限是硬拦**：非管理/群主直接拒（不像 `/结算` 那样有只读分支）。它是纯破坏性写操作，没有「只读版」可言。
- **参数解析复用 `_parse_settle_args`**，取出的名次数直接丢掉 —— `/重置结算 7月 12` 与 `/重置结算 7月` 等价。`_parse_month_filter` 用不了（正则末尾锚定，见上）。

#### 接口

| 位置 | 签名 | 说明 |
|---|---|---|
| `stats` | `rank_bonus(rank, total_points) -> float` | 公式唯一实现 |
| `stats` | `ranks_for(rows) -> list[int]` | 展示名次，与表格共用 |
| `stats` | `_settlement_lines(entries) -> list[str]` | 两个回执**共用的行体 + 合计**（唯一实现） |
| `stats` | `format_settlement(home_team, year, month, entries, reward_ranks) -> str` | **写路径**回执（⚔️「结算完成」） |
| `stats` | `format_settlement_view(home_team, year, month, entries) -> str` | **读路径**回执（📋「结算查询」+ 只读尾注） |
| `stats` | `build_ranking_cells(rows)` | `any("bonus" in r for r in rows)` 为真才加第 15 列 |
| `database` | `set_settlement(home_team, month, entries)` | 覆盖写（先 DELETE 再 INSERT） |
| `database` | `clear_settlement(home_team, month) -> int` | 只 DELETE、不 INSERT，返回删掉的行数；0 = 本来就没结算过（`/重置结算`） |
| `database` | `get_settlement(home_team, month) -> dict[str, float]` | `{玩家名: 奖金}`；未结算 → **空 dict**（只服务 `/排行` 的奖金列） |
| `database` | `get_settlement_entries(home_team, month) -> list[dict]` | `[{rank_no, player, total_points, bonus}]`，`ORDER BY rank_no`；未结算 → **空 list**（服务只读 `/结算`，也充当结算公告的「旧记录」基准） |
| `database` | `get_qq_ids_by_names(home_team, names) -> dict[str, str]` | **展示名 → qq_id**，`qq_id` 为空或查不到的**不返回**；按 `users.name` 查（见上节） |
| `stats` | `settlement_awards_changed(old_entries, new_entries) -> bool` | 获奖名次变没变（公告 @ 不 @ 的唯一判据） |
| `stats` | `settlement_announcement(names) -> list[tuple[str, str]]` | 公告分段（`text` / `at`），由 `main.py` 翻译成 `At` |
| `stats` | `format_settlement_repeat() -> str` | 名单没变时的「不打扰」提示 |

> ⚠️ 两个 `get_settlement*` **不是重复**：前者是 `{名字: 奖金}` 的查表（给榜单贴第 15 列），后者是**有序明细**（给 `/结算` 回执逐行念）。合并任一种都会让另一边多绕一圈。注意后者的 `total_points` 取自 `settlements` 表（`DECIMAL(10,2)`，**已截断到 2 位**），与榜单的实时值可能有末位差 —— 这正是「快照」的应有之义。

> ⚠️ `get_settlement` 返回空 dict 时调用方**不要给 rows 加 `bonus` 键** —— 这正是「仅已结算月份显示该列」的开关。加了键就会给所有月份都渲染出一整列 0。`/排行 全部` 同理不加。

#### 未覆盖 / 已决定的边界

- **并列跨奖励边界时按行位置截断**：`/结算 7月 4` 遇到第 4、5 行同为展示名次 4 时，第 4 行拿满 50、第 5 行没有（截断依据是 `enumerate` 的行位置，不是名次）。见 §10。
- **`_parse_settle_args` 不按位置解析**：正则 `_TOKEN_MONTH_RE` 逐个 token 试，纯数字 token 当名次数 → `7月 12` 与 `12 7月` 都认。**不能复用 `_parse_month_filter`** —— 它的正则**末尾锚定**，`/结算 7月 12` 里月份不在末尾会解析不出。
- **`min_games` 门槛照旧生效**：只踢馆不打友谊的玩家不进榜（M-13 边界），自然也不参与结算。

---

## 6. 功能清单

> 表格中「依赖口径」列指向 §5 的横切机制 —— **改动该功能前必须先读那些口径。**

### 域 A：排表与制表（不落库 duels）

| 编号 | 命令 | 入口 | 行为 | 依赖口径 | 落库 |
|---|---|---|---|---|---|
| **F-01** | `/排表 [规则]` + 名单 | `lineup_cmd` / `lineup.parse_lineup` / `generate_template` | 解析 `队伍名: 成员1 成员2` 名单；首行非名单则视为规则；两队各自随机洗牌后按下标配对，生成第一轮 `0:0` 占位 + 空第二轮；人数少的一侧用 `TK` 占位 | M-01、M-02(校验群)、M-10(输出) | `teams`（覆盖式） |
| **F-02** | `/第N轮 [玩家A [比分] 玩家B]` | `round_cmd`（`RoundCommandFilter` 自定义过滤器）/ `lineup.build_next_round` | 读群聊最近一条战报，追加第 N 轮（N≥2，数字或中文）。无参数 → 从上一轮**胜者**中随机配对；有参数 → 指定对阵或直接记比分 | M-04(胜者需非 0:0)、M-10(读群历史)、队对齐(见下) | 无 |
| **F-03** | `/记录 玩家名 比分 [对手]` | `record_cmd` / `lineup.record_from_info` | 找该选手**最后一场未记录**的 `0:0` 对阵填入比分；指定对手且无未记录对阵时，在最新轮次插入新对阵。比分支持 `2:0` 或紧凑 `20` | M-04、M-10、队对齐(见下) | 无 |

**共同约束：**
- 三者都只**回发新文本**，用户需再 `/发送` 才入库
- 对阵行按队伍对齐：`team_a` 的选手一定在左侧（`_align_team_order`）
- 输入按行处理，**单行失败不影响其余行**

### 域 B：提交与入库（唯一写入口）

| 编号 | 命令 | 入口 | 行为 |
|---|---|---|---|
| **F-04** | `/发送`（别名 `/战报`） | `submit_report` | 解析并入库战报（**友谊赛与踢馆同一入口**，按首行分流） |

**流程与逐项校验（顺序即代码顺序，改动时勿插队）：**

1. `_group_check()` → DB 就绪 + 群未禁用
2. `submit_requires_admin` 配置 → 仅管理员
3. 无参数且无引用 → 直接提示用法
4. **优先从引用消息提取**（含合并转发），否则按 `战队:` **或踢馆头**拆分多份（`split_reports`）
5. **逐份解析**，失败不阻断其余（记录行号 + 原文 + 格式示例）。每份按**首行**分流：`_is_report_text` 认 `战队:` 或 `is_raid_header`，`_parse_chunk` 决定走 `parse_battle_report` 还是 `parse_raid_report`
6. 确定 `group_id`：群聊直接用；私聊则取战报 `地点:` 字段（需 `allow_private_chat`，且该字段 `strip()` 后**必须全是数字**才当群号 —— 它是自由文本，可能写着"上海"之类）
7. **必须已绑定战队**，否则拒收
8. **战报的两侧队标之一必须包含本群绑定战队**，否则阻止（v1.12.15 新增；踢馆报即 `home_team ∈ (team_a, team_b)`，同一段代码成立，无需分支）
9. **对局数 < `min_duels`（配置，默认 3）直接拒** —— **踢馆报豁免**（踢馆天然只有 1~5 局，`min_duels` 默认 3 会误杀）
10. **胜负未定（`winner is None`）拒录**，并列出未填比分的对局（踢馆报列出的对局不带轮次前缀）
11. **内容指纹已存在 → 拒收**（`DuplicateReportError`，提示已有 ID），见 §5-M12
12. 入库 `matches` + `duels` + `player_ids`（单事务）
13. 回复：ID、对阵、胜负、逐场结果；成功集与失败集**分开**回复（各 ≥2 条则合并转发）。踢馆报的回复走 `lineup.format_raid_results`（逐场 `踢馆方 分数 守馆方` + `✅/❌ 进攻(防守)成功/失败`，**措辞视角 = 提交方战队**，见 §5-M13 展示口径；另附一行结论：踢馆成功/守馆成功 + 防守者人数 + 是否含馆主 + 点数）

**依赖口径：** M-01、M-02、M-03、M-04、M-05、M-06、M-07、M-10、M-12、**M-13（踢馆）**

> 提交成功后回复 `ID`，这是 `/战报删除` 的唯一凭据。改动时不要动这个 ID 的位置。

### 域 C：查询与统计

| 编号 | 命令 | 入口 | 行为与口径 |
|---|---|---|---|
| **F-05** | `/排行 [个人\|队伍\|全部] [X月]` | `ranking` | 见下方详解 |
| **F-06** | `/战绩 [玩家名] [X月]` | `record` | 无名字 → **战队总战绩**（`get_home_team_record`，按 `winner`；另附踢馆积分 / 总积分 / 踢馆成功 / 守馆成功 / 守馆首轮）；有名字 → `resolve_role` 命中则聚合该角色全部参赛ID，否则按单个参赛ID（同上一组字段，走 `get_player_record(with_raid=True)`）。三条次数与排行末三列同源，**多出一行**展示 |
| **F-07** | `/趋势 <玩家名\|队伍> [最近N天\|X月]` | `trend` | 三级回退：角色聚合 → 单个参赛ID → **按队名查**。默认无参数时展示**本战队** |
| **F-08** | `/导出 [玩家名] [胜场\|负场\|全部] [X月\|最近N天] [csv\|json]` | `export` | 见下方详解 |
| **F-09** | `/我的战绩 [X月]` | `my_record` | 按自己绑定的全部参赛ID汇总；多ID时额外列每个ID明细（含踢馆积分 / 总积分 / 踢馆成功 / 守馆成功 / 守馆首轮） |
| **F-27** | `/踢馆 [玩家名] [X月]` | `raid` | 无名字 → 本战队踢馆卡（踢馆成功 / 守馆成功 / 守馆首轮 / 踢馆成功含馆主 + 徽章 + 积分明细 + 合计）；有名字 → 该选手的踢馆卡。alias `/踢馆`、`/战报踢馆`。入口 `_require_home` → `get_raid_team_stats` / `get_raid_stats_for_players` → `stats.format_raid_record` |
| **F-28** | `/结算 <月份> [名次数]` | `settle` | **双模式**（v1.15.0）。入口统一 `_require_home`（含 `_group_check` + 必须已绑定战队）→ `_is_manager`，此后分叉：<br>**管理/群主 = 写** —— 给该月榜单前 N 名（默认 4，上限 12）按 §5-M14 发奖金并覆盖写入 `settlements`。顺序：`_parse_settle_args`（**不是** `_parse_month_filter`，见 §5-M14）→ `settle_month_range` → 当月未结束则拒 → `get_player_ranking(…, limit=None, team=home_team)` → `ranks_for` + `rank_bonus` → **`get_settlement_entries`（读旧记录，供公告判定）** → `set_settlement` → `format_settlement` → 获奖名次变了就 `get_qq_ids_by_names` + `At` 发公告（见 §5-M14「结算公告」）。<br>**其他人 = 只读查询** —— `get_settlement_entries` → 有记录 `format_settlement_view`、无记录回「该月尚未结算」。**不跑写路径的任何一步**，见 §5-M14「只读查询」。<br>**月份缺省 = 上个月**（`settle_month_range(None)`） |
| **F-29** | `/重置结算 [月份]` | `reset_settle` | **管理/群主**（非管理员硬拒）。把该月恢复成**未结算**：`_require_home` → `_is_manager` → `_parse_settle_args`（名次数丢掉）→ `settle_month_range` → `clear_settlement`。删掉 0 行回「无需重置」而非报错；**没有「当月未结束」校验**（那是写路径独有）。见 §5-M14「重置结算」 |

**F-05 `/排行` 详解：**

| 分支 | 数据源 | 展示 |
|---|---|---|
| `队伍`/`战队`/`队` | `get_home_team_vs_opponents` | 对战各对手记录，每个对手附踢馆加点（`get_raid_points_by_opponent`；「只打过踢馆」的对手也会出现）（`get_team_ranking` 曾是死代码，已于 v1.13.0 删除） |
| `全部`/`所有` | `get_player_ranking(team=None)`，`limit=ranking_limit`(10) | 全战队个人榜，图片按 `ranking_image_max_rows`(30) 截断 |
| 默认（个人） | `get_player_ranking(team=home_team)`，**不限条数** | 只统计本战队选手，**全部队员不截断**。该月已结算则多一列「奖金」（v1.15.0，见 §5-M14）。**图片左侧有「战队战绩」面板**（v1.15.0，见下） |

- **友谊积分 = 胜场 × 胜率(小数) = 胜场²/(胜场+负场)**，平局不计入分母。v1.14.0 前这一列叫「积分」，现改名为「友谊积分」，**只算友谊赛**
- **踢馆积分 / 总积分**（v1.14.0）：`总积分 = 友谊积分 + 踢馆积分`，**排序按总积分**
- 并列名次：`(total_points, wins, total)` 三者全同才并列 —— 唯一实现在 `stats.ranks_for`，`build_ranking_cells` 与 `/结算` 共用（v1.15.0 消重）
- 表格列（**14 列**，v1.14.0 定稿）：排名｜队员｜总积分｜友谊积分｜胜场｜负场｜总场数｜友谊次数｜胜率｜无双次数｜踢馆积分｜守馆成功｜守馆首轮｜踢馆成功
- **该月已结算**时末尾追加第 15 列「奖金」（v1.15.0）。开关是「行里有没有 `bonus` 键」（`any("bonus" in r …)`）：未结算的月份和 `/排行 全部` 都不加键 → 仍 14 列，行为逐字不变
- **加键与否的唯一实现是 `stats.attach_bonus(rows, bonus_map)`**：`bonus_map` 为空（= 未结算，`get_settlement` 返回 `{}`）时**一个键都不加**。⚠️ 这段以前直接写在 `main.py` 的 `/排行` 分支里且**无条件跑**，导致未结算月份照样长出第 15 列、整列 0；`build_ranking_cells` 侧的测试看不见这条接线，所以抽成纯函数由 `test_settlement.py` 钉住
- **所有列居中**（v1.15.0）：对齐方式来自 `stats.rank_aligns(ncols)` 函数而非定长常量（奖金列让列数在 14/15 之间浮动），文字表格与图片表格共用；`chart.draw_cell` 早有 `"center"` 分支，图片侧零改动
- `min_games` 过滤走 SQL `HAVING total >= %s`（**只看友谊场次** —— 只踢馆不打友谊的人不进榜，但可用 `/踢馆`、`/战绩` 查到）
- **LIMIT 在 Python 侧施加**：`get_player_ranking` 的 SQL 只留 `ORDER BY`、不留 `LIMIT`，合并踢馆后由 `stats.sort_ranking(rows, limit)` 截断 —— 否则 SQL 会在合并前把被踢馆积分顶上去的选手截掉

**F-05 图片左侧「战队战绩」面板（v1.15.0）：**

- **只有默认（个人）分支有**，`/排行 队伍`、`/排行 全部` 都不给 —— 面板属于**单个战队**，跨队榜上是无意义的数字。开关就是 `main.py` 里的 `panel = None` 初值 + 默认分支里的赋值
- **无表头行**：8 行直接开始，第 1 行与表格表头行**同 y 同高**，之后逐行与表格数据行对齐（面板第 i 行 ↔ 表格第 i-1 个数据行）。用 `head_h` + `row_h` 而不是统一 `row_h`，否则分隔线永远差 2px 且越往下越漂

| 左标签 | 右值 | 取自 `get_home_team_record` |
|---|---|---|
| 总场数 | 场数 | `total` |
| 守馆首轮 | 首轮积分 | `_ceil_div3(first_round)` |
| 守馆 | 守馆成功积分 | `_ceil_div3(hold)` |
| 踢馆 | 踢馆成功积分 | `attack_points` |
| 胜场 | 场数 | `wins` |
| 负场 | 场数 | `losses` |
| 胜率 | **1 位小数 + `%`** | `win_rate`（与右侧表格同源，用户示例写的 2 位已改为 1 位） |
| 积分 | 总积分 | `total_points`（`:`g，与表格「总积分」列同格式） |

- 唯一实现是 `stats.build_team_panel(record)`（纯函数，返回 `list[(label, value)]`）；`chart.make_ranking_image(..., panel=)` 只负责画，不懂语义 —— 与 `build_ranking_cells` → `cells` 的分工一致
- **面板不走 `build_ranking_cells`**：那 14/15 列的契约由它独占，掺进面板会破坏列数契约测试
- **`attack_points` 是复用来的，零额外查询**：`get_home_team_record` 内部本来就调了 `get_raid_team_stats`，只是把已有的 `raid["attack_points"]` 透传出来。在 `main.py` 里另调一次 `get_raid_team_stats` 会把整段 `_raid_infos` 重跑一遍
- **面板只在图片路径**：`ranking_image=false` 或图片渲染失败退回 `format_player_ranking` 时**没有面板**（见 §10.4）

**F-08 `/导出` 详解：**

| 维度 | 取值 |
|---|---|
| 格式 | 无 `csv`/`json` → **合并转发**（每份战报一个节点，逐字还原）；指定则输出文件 |
| 胜负 | 全部 / 胜场 / 负场 |
| 玩家 | 无 → 以 `home_team` 为参照（整场胜负）；有 → **以该玩家本人对局结果**判定（选手级口径，同一场可能同时命中胜/负） |
| 时间 | 月份 > 最近N天 > 本月 |

- 文件导出：先查 `get_export_rows`，再按比赛级过滤裁剪
- 合并转发：`get_reports_for_export` → `lineup.report_to_text`，**头部逐字保留**（`raw_text`），对局段按 `seq` 双空格重建
- 单条上限 100 节点，自动分批
- **踢馆报同样可导出**：两条导出 SQL **刻意不加 `kind` 过滤**（`m.kind` / `d.owner` 随行返回）。CSV 新增「战报类型」（`踢馆`/`友谊`）与「防守方馆主」（`是`/空）两列；还原时 `owner` 为真 → 给右侧补 `(馆主)`、`round_no == 0` → 不输出轮次头

### 域 D：用户与参赛身份

| 编号 | 命令 | 入口 | 权限 | 行为 |
|---|---|---|---|---|
| **F-10** | `/我的ID` | `auth` | 任意 | 查看自己的角色名与已绑参赛ID |
| **F-11** | `/查ID <关键词>` | `search_id` | 任意 | 模糊查询本战队参赛ID池及绑定状态 |
| **F-12** | `/绑定ID <ID> [ID...]` | `bind_id` | 任意 | 批量绑定到自己；若该ID已绑他人角色 → **认领该角色**（见下） |
| **F-13** | `/解绑ID <ID> [ID...]` | `unbind_id` | 本人 / 管理 | 批量解绑 |
| **F-14** | `/管理ID <ID[,ID...]> <用户名>` | `admin_id` | 管理/群主 | 无参数 → 列出全队ID；有参数 → 批量绑定到指定角色（不存在则创建） |
| **F-15** | `/改名 <新名字>` | `rename_me` | 本人 | 改角色名，战队内唯一，≤30 字 |

**绑定语义（`_bind_one_id`，最绕的一段逻辑）：**

```
参赛ID 未绑定          → 挂到我的角色（我没有角色则以该ID为初始角色名创建）
参赛ID 已绑 = 我的角色  → 提示"已在你名下"
参赛ID 已绑 ≠ 我的角色  → 若我无角色 → 认领该角色（连带其全部参赛ID）
                        若我有角色  → 拒绝（不得改绑）
```

**★ 不变量：一个 QQ 在一个战队下只有一个角色**（`find_or_create_user` 先按名字查、再按 QQ 查）。这条约束被 `/绑定ID`、`/管理ID`、`/改名`、`/我的战绩` 全部依赖。

**依赖口径：** M-02（`_require_home` 无配置兜底）、M-03（角色聚合）

### 域 E：战队与群管理

| 编号 | 命令 | 权限 | 行为 |
|---|---|---|---|
| **F-16** | `/绑定战队 <战队>` | 管理/群主 | 绑定本群战队 + `backfill_group_home` 回填空值；队标转大写 |
| **F-17** | `/查看战队` | 任意 | 查看本群绑定战队 |
| **F-18** | `/战队列表` | 任意 | 全部战队（来源：group_home ∪ player_ids ∪ matches 双方队标） |
| **F-19** | `/群聊属性 <友谊群\|战报群\|主群>` | 管理/群主 | 设置群属性（默认友谊群） |

### 域 F：超级管理员

| 编号 | 命令 | 行为 |
|---|---|---|
| **F-20** | `/禁群` `/启群` `/查群` `<群号>` | 群级功能开关 |
| **F-21** | `/群列表 [战队]` | 全部群 + 绑定战队 + 禁用状态；可按战队过滤 |
| **F-22** | `/通告 <内容>` / `/通告 <群号或QQ>\n<内容>` | 广播（带北京时间戳，群发间隔 0.2s 防风控）；首行纯数字则视为指定目标 |
| **F-23** | `/导出群成员 <群号>` | 导出群成员 CSV（QQ号/昵称/名片/角色/所在地/入群时间），群主→管理员→成员排序。**不依赖数据库**（与 `/帮助` 同为两个例外） |

### 域 G：战报治理

| 编号 | 命令 | 权限 | 行为 |
|---|---|---|---|
| **F-24** | `/战报删除 <战报ID>` | 管理/群主 | 按 ID 删除，**校验群归属**（`delete_match` 带 `group_id`） |
| **F-25** | `/战报撤销` | 提交者本人 | 撤销自己在本群最近一条 |

### 域 H：帮助

| 编号 | 命令 | 行为 |
|---|---|---|
| **F-26** | `/帮助 [全部\|超管]` | 无参数按群属性展示；`全部` = 除超管外全部；`超管` = 仅超管段。**只做 `_check_enabled`（群被禁用时不回复），不校验 DB 就绪、不要求绑定战队** |

---

## 7. 变更传导矩阵 ★本文档的核心

### 7.1 常见变更 → 必须一起改的地方

| 你要做的事 | 必须同步修改 | 容易漏的点 |
|---|---|---|
| **新增一条命令** | ① `main.py` 加 handler + `@filter.command(name, alias={"/"+name})` ② `stats.HELP_SECTIONS` 加段 ③ `stats.CHAT_TYPE_SECTIONS` 归类 ④ 前置校验（选 `_group_check`/`_require_home`/`_admin_check`）⑤ `tests/` | 别名必须**同时注册带 `/` 与不带 `/`** 两种；`ALL_SECTIONS` 会自动纳入，不用改 |
| **战报格式加一个头字段**（如 `裁判:`） | ① `parser` 加正则 + `BattleReport` 字段 ② `matches` 表加列 + `SCHEMA_VERSION` + 迁移 ③ `insert_report` ④ `lineup.generate_template` ⑤ 导出还原 `report_to_text`/`format_report` ⑥ CSV 表头 ⑦ **`_header_block` 的头部提取** | M-10：`战队:` 是 3 处的识别标志，别动它 |
| **对局加一个属性**（如 出场序号/时长） | ① `Duel` dataclass ② `parse_battle_report` ③ `duels` 表 + 迁移 ④ `insert_report` ⑤ **所有读出 duels 的 SELECT**：`get_export_rows`、`get_reports_for_export`、`get_player_match_stats` ⑥ dict 组装处（`get_reports_for_export` 逐字段手写）⑦ `format_duels_block` 还原 ⑧ CSV 列 | ⑤⑥ 是"逐字段罗列"的写法，**漏一个字段就是静默丢数据** |
| **改胜负判定** | ① `determine_match_winner` + 两个 winner 函数 ② 历史 `matches.winner` 回填脚本 ③ 战队战绩/对战记录 ④ 导出胜负过滤 ⑤ `compute_match_stats` 的 winner 守卫 ⑥ 提交时的拒录分支 | M-07：**历史数据不会自动重算** |
| **改积分/胜率公式** | 见 §7.3「同源重复实现」第 1、2 条 —— 共 **5 处** | 漏 SQL 侧 → Python 表格与 DB 排序不一致 |
| **新增排行列** | ① `stats._RANK_HEADERS` ② `stats.build_ranking_cells` 的**数据行**（文字与图片同源，手写定长列表）③ `chart.make_ranking_image` 布局宽高自适应（列宽已自动算）④ 测试里别再手写 `aligns` 字面量 | ①③④ 由 `rank_aligns(ncols)` 自动跟随（**没有定长常量了**，v1.15.0）；**② 不会**，漏了就是行列数对不上。`tests/test_chart.py::test_rank_aligns_matches_header_columns` 会拦住 ① 与 ② 不一致 |
| **改结算/奖金**（封顶表、名次截断、月份回退） | ① `stats.RANK_BONUS_CAPS` / `rank_bonus`（**唯一**公式实现）② `stats.ranks_for`（**唯一**名次实现，与排行表格共用）③ `battle_report_parser.settle_month_range`（月份缺省=上个月也在这里，**`/重置结算` 共用**）④ `main.settle` 的「当月拒结」校验 ⑤ `stats.format_settlement` 的年份回显 ⑥ `stats.attach_bonus`（bonus 键加不加的唯一实现，「未结算不长第 15 列」靠它）⑦ `tests/test_settlement.py` | **③④ 必须成对**：`settle_month_range` 只回退、不校验，少了 ④ 就会静默结算去年同月。改封顶表别顺手改排序 key —— 奖金是派生值，**不影响名次**。改 ③ 的返回形态（如把年份/月也一并返回）要同时看 `main.settle` 与 `main.reset_settle` 两个调用点 |
| **改 `/重置结算`** | ① `database.clear_settlement`（**只 DELETE、不 INSERT** —— 补上 INSERT 就退化成「重算」，不再是重置）② `main.reset_settle` 的 `_is_manager` 硬拦 ③ 与 `/结算` **共用** `settle_month_range` 取 key（月份缺省=上个月的口径必须一致，否则删不到结算时写的那一行）④ 回执里「重新结算会重新 @」的说法依赖 `settlement_awards_changed([], …) == True` ⑤ `tests/test_database_stats.py` / `tests/test_settlement.py` | 重置把该月打回「从没结算过」，于是重新结算必然被判成**第一次结算**、**重新 @ 一遍全群**。想「只改金额不 @ 人」的话不能走重置 |
| **改 `/结算` 的读/写分叉** | ① `main.settle` 里 `_is_manager` 之后的那次分叉（**写路径的每一步都必须在分支之内**）② 读路径的 `get_settlement_entries`（只读 `settlements`，**不能改成跑 `get_player_ranking` 重算**）③ 只读回执 `stats.format_settlement_view` 的抬头/尾注（必须与写路径「结算完成」不同形）④ 两边共用的 `stats._settlement_lines`（改行体只改这一处，两个回执同时变）⑤ `tests/test_settlement.py` | **新加写操作时必须放进 `is_admin` 分支内** —— 放到分叉之前会让非管理员的查询也写库。回执文案两个函数刻意不同形，是为了避免非管理员误以为钱已发出 |
| **改结算公告（@ 获奖成员）** | ① `stats.settlement_awards_changed` 的比对键（改口径 = 改「什么时候 @ 全群」）② `main.settle` 里**「读旧记录」必须仍在 `set_settlement` 之前**（挪到后面就永远比不出变化、每次重跑都 @ 全群）③ `stats.settlement_announcement` 的分段（`("at", 名)` 不能改成把名字拼进文本）④ `database.get_qq_ids_by_names`（按 `users.name` 查，**不是** `player_ids.player_name`）⑤ `stats.format_settlement_repeat` ⑥ `tests/test_settlement.py` | ②挪错位置是最隐蔽的一种：功能「看起来正常」（首次结算照样 @），只在重跑时静默退化成每次都 @。③拼成字符串会让 QQ 上只剩字面的「@名字」，不弹提醒也不亮蓝 |
| **改「战队战绩」面板**（加行/改口径/换取值） | ① `stats._TEAM_PANEL_LABELS`（若加行）② `stats.build_team_panel` 的**返回值列表是手写的**，与 ① 顺序必须逐字一致 ③ `get_home_team_record` 是否已透传所需键（踢馆相关的键**只透传 `attack_points` 一个**，见 §7.3 第 19 条）④ `tests/test_stats.py` 的标签顺序用例 | 面板不走 `build_ranking_cells`，**改排行列的那套清单对它一个都不适用**；反过来把面板塞进 `build_ranking_cells` 会破坏 14/15 列契约 |
| **改玩家名解析口径** | ① `get_player_ranking` 的 2 处 `COALESCE` ② `get_player_match_stats` 的 2 处 ③ `resolve_role` ④ 跨队同名三处（§5-M03） | 漏一处 → 同一个人出现两个名字 |
| **改战队归属解析** | ① `_require_home`（**唯一**解析路径）② 所有聚合 SQL 的 `m.home_team = %s` ③ `backfill_group_home` ④ `/发送` 的绑定校验 | M-02：v1.13.0 起只有群绑定一条路径，别再引入配置兜底 |
| **新增配置项** | ① `_conf_schema.json` 加定义 ② `main.py` 读取 ③ `README.md` 配置表 ④ 本文件 §11 | `tests/conftest.py` 从 schema 自动加载默认值，**无需改测试**；`tests/test_config_schema.py` 会校验格式 |
| **删除配置项** | 同上四处**一起删** | 只从 schema 删会留下"界面没有、代码在读"的隐式默认值；只从代码删会留下界面上的死开关（`home_team`、`default_days` 就是这么来的，§10-3/§10-4） |
| **新增群级开关** | ① 建表（仿 `group_ban`/`group_chat_type`：`group_id` 主键）② 迁移 ③ 读/写函数 ④ 在**每个**命令入口串进校验链 | 漏了某个命令的入口 → 该开关对它无效 |
| **改导出** | ① `get_export_rows`（CSV/JSON 行）vs `get_reports_for_export`（合并转发）——**两条独立链路** ② `report_to_text` ③ `filter_report_outcome` ④ main 里的 CSV 表头 ⑤ 100 节点分批 | 两条链路常被当成一条改，结果"文件导出变了、转发没变" |
| **改删除/撤销** | ① `delete_match` 的群归属校验 ② `get_last_match_by_submitter` ③ 外键 `ON DELETE CASCADE`（旧表可能没有，代码显式删 duels 兜底） | 删除是**物理删除**，无软删/回收站 |
| **改用户/身份** | ① `users`/`player_ids` 表 ② `find_or_create_user` 的"一QQ一角色" ③ `claim_user_by_name` ④ `bind_player_to_user` / `unbind_player` ⑤ `resolve_role` ⑥ **所有依赖 COALESCE 的 SQL** | "一QQ一角色"是不变量，改绑定语义会连锁影响 6 处 |
| **新增一个战报类型**（如「擂台」「车轮战」） | ① `parser` 加 `KIND_*` + 独立 `parse_xxx_report`（**不要改造 `parse_battle_report`**，友谊赛路径零改动）② `report_fingerprint` 把新 kind 写进 payload（友谊赛不进，见 §5-M12）③ `determine_match_winner` 开头按 `report.kind` 分流 ④ `stats` 加该类型唯一的判定/计分实现 ⑤ `database` 加该类型的取数函数（**注意按哪一侧取数**，踢馆就是 M-02 的例外）⑥ `main._is_report_text` / `_parse_chunk` / `split_reports` 的**首行识别标志**扩到新类型 ⑦ `lineup` 的还原与回执 ⑧ **`grep -n "FROM duels\|FROM matches" database.py`：每个既有的聚合查询都要决定**「要不要把这一类型算进去」，要排除就补 `AND m.kind = 'friendly'` ⑨ 导出两条链路 ⑩ 文档四处（本文件 §5 加 M-xx / §6 加 F-xx / README 功能与格式示例） | **⑧ 是本次最大的静默损坏风险点**（踢馆一次要动 18 处）。漏一处 → 新类型的数据会渗进友谊统计，"积分"列数值凭空调高，且**不会报错**。验收标准是"插入一份新类型的战报后，所有友谊查询逐字段不变"（§12） |

### 7.2 新增数据库字段的固定动作

```
1. _init_schema 的 CREATE TABLE 里加列（新库）
2. SCHEMA_VERSION += 1
3. _init_schema 末尾加 if current < N: ALTER TABLE ...
4. 历史数据回填（SQL 迁移 或 scripts/ 一次性脚本）
5. 写入侧：insert_report
6. 读取侧：列出所有 SELECT 该表的地方，逐个补列 ← 最容易漏
7. 测试：tests/test_database_stats.py 的迁移与读写用例
```

> **读取侧自查命令：** `grep -n "FROM duels\|FROM matches" database.py` —— 每个命中点都要评估。

### 7.3 同源重复实现清单（改一处必须改全部）★★

这是本代码库最大的维护风险来源。以下逻辑在多个地方各写了一遍：

| # | 逻辑 | 出现位置 | 漏改的后果 |
|---|---|---|---|
| **1** | **积分公式** `wins²/(wins+losses)` | `stats._points`（Python）<br>`get_player_ranking` SQL 的 `ROUND(SUM(win)*SUM(win)/...)`<br>~~`get_team_ranking` SQL~~（v1.13.0 已随死代码删除） | 表格显示与 DB 排序不一致、名次错乱 |
| **2** | **胜率公式** `w*100/(w+l)` round 1 位 | `stats.build_ranking_cells`、`stats.format_player_record`、`stats.format_team_record` 调用处、`main.my_record`、`get_home_team_vs_opponents`、`get_home_team_record` | 同一胜率在不同页面显示不同值 |
| **3** | **玩家名解析** `COALESCE(u.name, 参赛ID)` | `get_player_ranking` ×2、`get_player_match_stats` ×2、`resolve_role` | 同人两名、数据对不上 |
| **4** | **0:0 排除** | SQL `NOT (d.score_a = 0 AND d.score_b = 0)` **6 处**（`database.py:858,902,912,1043,1052,1071`：友谊计数 2 处 + draw 的 CASE 4 处）、`compute_match_stats`、`_player_has_duel_result`、`_winner_*` | 平局数虚高 / 无双误判 |
| **5** | **中文数字 ↔ 整数** | `parser._cn_to_int`（lineup 从 parser 导入）、`lineup._int_to_cn`（**另写一份**） | 轮次显示/解析不一致 |
| **6** | **队标转大写** | `parser.parse_battle_report`、`lineup.parse_lineup`、`main.bind_home`、`main.list_groups` | 同一战队出现 `KC` 与 `kc` 两条记录 |
| **7** | **时间范围解析** | `_parse_month_filter`、`parse_export_payload`、`main._date_from` | `/导出 7月` 与 `/排行 7月` 走不同分支 |
| **8** | **战报文本还原** | `lineup.format_duels_block`、`format_report`、`report_to_text`、`format_duel_results`（**唯一不补标记的**） | 导出丢标记 / 格式与原始不符 |
| **9** | **跨队同名过滤** | `get_player_match_stats(team=)`、`get_player_record` 的 SQL、`filter_report_outcome(member_team=)` | 对方战队同名选手的成绩算进本队 |
| **10** | **规则是否 KOF** | ✅ **已收敛**：唯一实现是 `battle_report_parser.is_kof()`，`determine_match_winner` 与 `stats.compute_match_stats` 共用（v1.13.0） | 别重新各写一份 `"人头" in rule` —— 人头赛会误用 KOF 的无双兜底，无双判错（§10-7） |
| **11** | **内容指纹字段集** | ✅ **单一实现**：`battle_report_parser.report_fingerprint()`。见 §5-M12 的进/不进指纹对照表 | 改动会同时影响"该拒的没拒"和"该收的拒了"，且历史行指纹作废 |
| **12** | **踢馆积分公式** `3/2/0 + 2(馆主)` 与 `min(10, ceil(hold/3) + ceil(first/3) + 5×shutdown)` | ✅ **单一实现**：`stats.raid_attack_points` / `raid_defense_points`（取整走 `_ceil_div3`）。战队侧（`aggregate_raid(team_view)`）与个人侧（`aggregate_raid(player_views)`）**共用同一对函数与同一个 `aggregate_raid`** —— 个人不是另写一套公式，而是把视角记录换成个人的再喂给同一段代码 | 别在 `database.py` 里再算一遍积分（那里只做"取数 + 调 stats"）。另：**月度降重必须在合并后算**（一个用户的多个参赛ID先并成一个集合再取 MAX / 封顶），把各ID的汇总相加会重复计分。**取整方向别抄错成 `//`** —— 写成 floor 时 1~2 次守馆得 0 分且不报错（§5-M13） |
| **13** | **踢馆「没打」判定** | `stats._unplayed`（`规则` 占位 ∪ `0:0`）。被 `compute_raid_match`、`raid_player_views` 共用 | 只判 `0:0` → 占位行被当成"打过但 0:0"计入防守者人数；只判 `规则` → 真 `0:0` 占位行串进人数 |
| **14** | **踢馆首行识别** | `battle_report_parser.is_raid_header`（`RAID_RE` + 排除 `战队:`/含「规则」），被 `split_reports`、`main._is_report_text` 共用 | 各处各写一份正则 → 有的命令认得踢馆报、有的不认 |
| **15** | **`friendly` 过滤** | `AND m.kind = 'friendly'` 在 `database.py` 里手写 18 处（清单见 §5-M13）。**没有共用的常量或视图** | 新增聚合查询时容易整体漏写。见 §7.1「新增一个战报类型」与 §7.2 的自查命令 |
| **16** | **排行列布局（14/15 列）与三个踢馆次数** | `stats._RANK_HEADERS`（表头，`rank_aligns()` 与 `chart.py` 的列数都从它自动推导）<br>`stats.build_ranking_cells` 的**数据行是手写定长列表**，列序/列数必须逐个手工对齐<br>`stats.format_player_record` / `format_team_record` 的「踢馆成功 / 守馆成功 / 守馆首轮」三个数与末三列同源（取自 `get_player_record` / `get_home_team_record` 返回的 `raid_success` / `hold` / `first_round`） | 图片表格少一列 / 与文本表列错位（`build_ranking_cells` 不跟着改就静默串列）；同一选手在排行与 `/战绩` 里看到不同的踢馆次数 |
| **17** | **并列名次 key** `(total_points, wins, total)` | ✅ **已收敛**：唯一实现是 `stats.ranks_for(rows)`（v1.15.0）。此前只长在 `build_ranking_cells` 里，`/结算` 发奖金时若另写一遍就会漂移 | 榜上显示第 1 名、奖金却按第 2 档发（**静默错发钱**）。`tests/test_settlement.py::test_ranks_for_matches_table_column` 钉住一致性 |
| **19** | **「战队战绩」面板是踢馆原始分量的第 4 个展示位** | `stats.build_team_panel` 直取 `_ceil_div3(hold)` / `_ceil_div3(first_round)` / `attack_points`，**刻意绕开带封顶的 `raid_defense_points`**；`get_home_team_record` 只透传 `raid["attack_points"]` 一个键，**不得**改为在 `main.py` 里另调一次 `get_raid_team_stats`（会重跑整段 `_raid_infos`） | 有人「顺手修一下」让面板改用 `raid_defense_points` → 面板数字突然比 `/战绩` 小；或在 `main.py` 里另算一遍 → 面板与表格不同源（每月多一次全表扫） |
| **18** | **排行对齐方式** | ✅ **已收敛**：唯一实现是 `stats.rank_aligns(ncols)`（v1.15.0），返回 `["center"] * ncols`。此前 `RANK_ALIGNS` 常量在 `stats.py` / `main.py` / 测试里各写一遍（共 8 处） | 加列时 8 处一起错位（§10-11 的历史 bug）。v1.15.0 顺带把定长常量换成按列数生成的函数，奖金列带来的 14/15 浮动也一并解决 |
| **20** | **结算回执的行体**（`第N名 名 总积分X 奖金Y` + 「合计发放 Z」） | ✅ **已收敛**：唯一实现是 `stats._settlement_lines(entries)`（v1.15.0），由写路径 `format_settlement` 与只读路径 `format_settlement_view` 共用。两者**只有抬头与尾注不同**（「结算完成」/「结算查询」、年份与名次数的取法） | 各写一份 → 同一次结算在管理员与群员眼里金额、小数位、合计对不上。`test_format_settlement_view_rows_match_settlement_receipt` 断言两者去掉抬头/尾注后逐行相同 |
| **21** | **「展示名 ↔ 角色/QQ」的反查**（与 #1~#3 的**正查**方向相反） | 榜单的 `player` 是 `COALESCE(u.name, d.player_a)`（展示名），所以**反查必须按 `users.name`**：`get_player_binding`（单个参赛ID）、`get_qq_ids_by_names`（批量展示名 → QQ，结算公告用）。而 `resolve_role` / `get_user_players` 走的是 `player_ids.player_name`（参赛ID），是另一个方向 | 拿**参赛ID**去查 QQ → 一个都匹配不上，结算公告静默 @ 不出任何人（**不报错**）；拿**展示名**去查 `player_ids` → 同样的静默失配。两个方向的键长得一模一样，是本库最容易混的一处 |
| **22** | **同一句用户文案的多个副本** | ✅ **已收敛**（v1.15.0）：`main.py` 里逐字重复 2~4 次的几组抽成模块常量 —— `_NEED_HOME` / `_NEED_GROUP` / `_ERR_QUERY`（4 处）/ `_ERR_EXPORT`（3 处）/ `_NEED_BIND`（3 处）/ `_NO_REPORT`（2 处）/ `_ADMIN_ONLY`（2 处）/ `_USAGE_MANAGE_ID`（2 处）/ `_USAGE_BROADCAST`（2 处）/ `_GROUP_DISABLED`（2 处，含群号故用 `.format(gid)`）。与既有的 `_HELP_HINT` 是同一套做法 | 改口吻或改措辞时要手改 20+ 处，**漏一处就出现两种说法**（一半 handler 说「请稍后重试」、一半说「稍后再试一次嘛」）—— 这类不一致没有任何测试会报错 |

### 7.4 改动影响面速查（反向索引：口径 → 功能）

| 口径 | 受影响功能 |
|---|---|
| **M-01 群隔离** | F-24、F-25（+ 私聊提交的 F-04） |
| **M-02 战队归属** | F-04、F-05、F-06、F-07、F-08、F-09、F-10~F-15、F-16、F-17 |
| **M-03 玩家名解析** | F-05、F-06、F-07、F-08、F-09、F-11、F-12、F-13、F-14 |
| **M-04 0:0 占位** | F-02、F-03、F-04、F-05、F-06、F-07、F-08 |
| **M-05 判罚 ruled** | F-04、F-08、F-05（胜负判定间接）、F-03/F-02（还原） |
| **M-06 替补 sub** | F-04、F-08、F-02（KOF 容量）、F-05（不直接） |
| **M-07 胜负判定** | F-04、F-05（队伍榜）、F-06（战队总战绩）、F-08（胜负过滤） |
| **M-08 时间范围** | F-05、F-06、F-07、F-08、F-09 |
| **M-09 输出渲染** | F-05、F-07、F-08、F-23 |
| **M-10 平台消息** | F-01、F-02、F-03、F-04、F-08、F-22、F-23 |
| **M-11 群属性帮助** | F-26、F-19 |
| **M-12 去重指纹** | F-04、F-24（删除后才能重提同一份） |
| **M-13 踢馆** | F-04（分流入库）、F-05（踢馆积分/总积分列、队伍榜的踢馆加点、**左侧「战队战绩」面板的守馆/踢馆三行**）、F-06、F-08（导出不过滤 kind）、F-09、F-27；以及**所有友谊聚合的反向约束** —— 改 M-04 / M-07 时也要回头确认踢馆侧是否同源（§7.3 第 12~16 条）。**改 M-14 的 10 分封顶时要回头确认面板三行仍取原始分量**（§7.3 第 19 条） |
| **M-14 月度结算（10 分封顶）** | F-28（`/结算` 的**写路径**：管理/群主结算发奖金 + **@ 获奖成员的结算公告**；**读路径**：其他人只读查询已结算结果）、**F-29（`/重置结算`：把该月打回未结算，与 F-28 共用 `settle_month_range` 的月份口径）**、F-05（个人榜的奖金列，仅已结算月份）、F-12 `/绑定ID`（**间接**：公告能不能真 @ 到人取决于有没有绑 QQ，见 §10.4-27）；依赖 M-03（玩家名必须与榜单同源，否则奖金对不上人、公告 @ 错人）与 M-13 展示口径的排序 key（名次共用 `ranks_for`）。**反向约束**：改排行排序 key / `ranks_for` 时要同步 `tests/test_settlement.py`；改 `settlements` 表结构或 `month` 的 `YYYY-MM` 形态时，**两条 `get_settlement*` 都要看**（一个是查表贴列、一个是明细回执，见 §5-M14 接口表）；改 `users.name` / `qq_id` 的写入路径时要回头看结算公告的 @ 是否还能取到 QQ（§7.3 第 21 条） |

> **用法：** 改了 M-04（比如决定把 `0:0` 从 `total` 里剔除），上表告诉你 **7 个功能**要一起验证，而不是只测你想到的那一个。

---

## 8. 代码地图

### 8.1 文件职责

| 文件 | 行数 | 职责 | 框架依赖 |
|---|---|---|---|
| [main.py](main.py) | ~1770 | 全部命令入口、权限、编排、平台消息收发、文本/文件输出 | AstrBot |
| [database.py](database.py) | ~1640 | 建库建表迁移 + 全部 SQL 聚合 + `settlements` 的两条读取路径（`get_settlement` 查表贴奖金列 / `get_settlement_entries` 明细回执）+ `get_qq_ids_by_names`（展示名 → QQ，结算公告用）+ `set_settlement` / `clear_settlement`（结算的写与撤销） | aiomysql |
| [lineup.py](lineup.py) | ~755 | 排表、追加轮次、记录比分、战报文本还原、导出过滤 | 无 |
| [battle_report_parser.py](battle_report_parser.py) | ~735 | 战报解析（友谊 + 踢馆）、胜负判定、`is_kof`、内容指纹、月份/参数解析 | 无 |
| [stats.py](stats.py) | ~700 | 积分/胜率/表格/帮助文案、`rank_aligns`、**踢馆判定与计分（踢馆侧唯一实现）**、**结算奖金公式与名次（`rank_bonus` / `ranks_for` / `attach_bonus`）**、**结算回执（写 `format_settlement` / 只读 `format_settlement_view`，行体共用 `_settlement_lines`）**、**结算公告（`settlement_awards_changed` 判要不要 @ / `settlement_announcement` 分段文案 / `format_settlement_repeat` 不 @ 时的提示）**、**排行图片左侧面板的 8 行（`build_team_panel`）** | 无 |
| [chart.py](chart.py) | ~300 | Pillow 趋势图 + 排行表格图（`make_ranking_image` 的 `panel` 参数只负责画，不懂语义） | Pillow |
| [tests/](tests/) | ~3440 | 13 个纯逻辑测试 + 1 个 DB 集成测试（见 §12） | pytest |
| [scripts/](scripts/) | — | 一次性数据修复脚本（见下） | — |

### 8.2 死代码（v1.13.0 已清理）

此前登记过四个"有定义、有测试、生产路径从不调用"的函数，**已全部删除**：

| 已删函数 | 原位置 | 取代者 |
|---|---|---|
| `get_team_ranking` | `database.py` | `get_home_team_vs_opponents` |
| `format_team_ranking` | `stats.py` | `format_home_team_vs` |
| `format_roster_display` | `lineup.py` | 配套的 `/看排表` 命令早已移除 |
| `get_teams` | `database.py` | 名单仅留档，需要时直接查 `teams` 表 |

> **`/排行 队伍` 走的是 `get_home_team_vs_opponents` + `format_home_team_vs`。** 想改队伍排行，就从这两个函数下手。
>
> 顺带删掉的死参数：`stats.format_player_ranking(rows, limit, min_games)` 的 `min_games` —— 入榜门槛真正的实现在 SQL 的 `HAVING total >= %s`，这个参数从来没被用过。
>
> **教训：** 死代码会持续误导（本文件的早期版本就把 `get_team_ranking` 当成队伍排行在讲）。发现即删，不要留着"以后可能用" —— 没有测试会因为你删了它们而变红（会变红的测试本身也是钉死代码的）。
>
> ⚠️ **删除任何函数前先查引用**：`grep -rn "函数名" *.py tests/ scripts/`。本轮就发现 §10 清单里"测试钉住了它们"的说法只对了三个，第四个（`format_team_ranking`）其实是零引用。

### 8.3 外部脚本

| 脚本 | 用途 |
|---|---|
| `scripts/fix_ruled_legacy.py` | 修复历史判罚标记数据 |
| `scripts/fix_team_label_legacy.py` | 修复历史脏队标（大小写/多余空格） |
| `scripts/backups/` | 上述脚本运行前的数据备份 |

---

## 9. 新增功能检查清单

任何新功能落地前，逐项过一遍：

- [ ] **命令注册**：`@filter.command` + `alias`（带 `/` 与不带 `/` 两种）
- [ ] **权限**：明确选 `_group_check` / `_require_home` / `_admin_check` / 无校验（并说明理由）
- [ ] **帮助**：`HELP_SECTIONS` 加段 + `CHAT_TYPE_SECTIONS` 归类
- [ ] **配置**：是否需新配置项？→ `_conf_schema.json` + 读取处 + README
- [ ] **口径核对**：过一遍 §5 的 14 条，逐条确认新代码是否遵守（尤其是 M-02 过滤、M-03 名解析、M-04 的 0:0、M-13 的 `kind` 隔离、M-14 的名次与月份）
- [ ] **`kind` 隔离**（若新功能要读 `matches`/`duels`）：跑 `grep -n "FROM duels\|FROM matches" database.py`，逐个决定「算不算友谊」，要排除就补 `AND m.kind = 'friendly'`（清单见 §5-M13，验收见 §12）
- [ ] **同源实现**：新逻辑是否与 §7.3 中某项重复？若重复，考虑先抽公用函数
- [ ] **传导矩阵**：逐行过 §7.1，确认没有连带漏改
- [ ] **数据迁移**：改了表结构？→ §7.2 七步
- [ ] **历史数据**：新口径对旧数据成立吗？→ 迁移回填 or `scripts/` 脚本
- [ ] **测试**：纯逻辑加到对应 `test_*.py`；涉及 DB 的加 `test_database_stats.py`（需 `ASTRBOT_TEST_MYSQL_PASSWORD`）
- [ ] **文档（两份必须一起改，缺一不可）**：
  - [ ] [README.md](README.md) —— 使用者视角：核心功能条目、命令语法、配置表、必要时「💡 几个概念」
  - [ ] **本文件（REQUIREMENTS.md）** —— 开发者视角：§6 加 F-xx 条目；若引入新口径改 §5；若产生新联动改 §7；若产生新技术债改 §10；若加配置项改 §11；若加测试改 §12
- [ ] **版本号**（**三处都要改**）：`metadata.yaml` + `main.py` 的 `@register` 版本串 + `README.md` 第 3 行的版本徽章

---

## 10. 已知不一致与技术债

> 原登记 12 条，**v1.13.0 一次性处置完毕**：9 条已修复（#1、#3~#8、#10、#11）、1 条订正为"不存在"（#9）、1 条按业务判断挂起（#2）、1 条移出债表（#12，本就不是债）。
>
> 这里保留"已修复"清单不是流水账 —— 每条的**修法**和**验证方式**都指向具体位置，日后相关代码再被动到时，能立刻知道当初为什么这么改。

### 10.1 已修复（v1.13.0）

| # | 原问题 | 处置 | 验证 |
|---|---|---|---|
| **1** | 死代码四处 | 全部删除（含钉住它们的 3 处测试、1 处零引用） | §8.2 |
| **3** | 战队解析双路径 | 统一为严格模式 `_require_home`；删 `_get_effective_home` + 配置 `home_team` | §5-M02 |
| **4** | `default_days` 死配置 | 删除 | §11 |
| **5** | README 严重落后 | 已重写（2026-09-10） | 两份文档分工见 §9 检查清单 |
| **6** | 对局数下限硬编码 3 | 提为配置 `min_duels`（默认 3，取值 `max(1, …)` 保护） | §11 |
| **7** | 无双兜底未按 `rule` 门控 | 抽出 `battle_report_parser.is_kof()`，`compute_match_stats` 按规则门控 | `test_match_stats.py::test_wushuang_fallback_only_for_kof` |
| **8** | 重复提交无幂等 | 内容指纹 + 唯一键 + 预检 + 1062 并发兜底 | §5-M12、`test_fingerprint.py`、`test_database_stats.py::test_duplicate_report_rejected` |
| **9** | 私聊 `group_id` 类型混用 | **订正：此问题不存在**（详见下） | — |
| **10** | `/帮助` 不校验 | 加 `_check_enabled`（仍**不**校验绑定，有意为之） | F-26 |
| **11** | 排行 `aligns` 硬编码 | 收敛为 `stats.RANK_ALIGNS`（生产 2 处 + 测试 6 处 → 1 处）。**v1.15.0 该常量进一步被函数 `stats.rank_aligns(ncols)` 取代**（列数在 14/15 浮动，定长常量跟不动；语义也改为全部居中，见 §7.3 第 18 条） | `test_chart.py::test_rank_aligns_matches_header_columns` |

**关于 #9（订正，不是修复）：** 调研结论是 `group_id` **全链路本就是 `str`**（适配器已做 `str()` 转换），"类型混用"是文档误记。但顺带发现了真实风险：私聊用 `地点:` 当群号时**完全不校验** —— 该字段是自由文本，可能写着"上海"。现已加固为 `strip()` 后必须 `isdigit()`，否则给出明确提示（而不是拿它去查库）。

**关于 #12（移出债表）：** `/排表` 名单表（`teams`）不参与统计是**有意设计** —— 名单仅留档，参赛ID池只从战报提取。不是债。将来若要做"名单 vs 实到"的对比，再从这里扩展。

### 10.2 挂起：`total` 含 `0:0`（业务判断，不改代码）

`get_player_ranking` / `get_player_record` 的 `COUNT(*)` 含 `0:0` 占位行，而排行表"总场数"列用 `played_total = wins + losses + draws`（不含）。两者不是同一个数，波及三处：`min_games`、并列 key、`/战绩` 的"总N"（详见 §5-M04）。

**不改的理由（用户判断）：** 带未填比分的战报会被 `/发送` 的胜负判定拒收（`winner is None` → 拒录），根本进不了库；现实中不存在带 `0:0` 的记录。

**理论上唯一的漏网场景：** 占位行落在**胜方**（胜方有人全程没上场、而对方已全员落败）—— 此时胜负可判，战报会带着 `0:0` 入库。用下面这条**只读** SQL 查证线上有没有这种行：

```sql
SELECT COUNT(*) FROM duels d JOIN matches m ON d.match_id = m.id
WHERE d.score_a = 0 AND d.score_b = 0 AND m.winner <> ''
  AND m.kind = 'friendly';   -- v1.14.0 起必须带：踢馆的无人守馆战报本来就全是 0:0 行
```

> ⚠️ **v1.14.0 补充：** 踢馆引入了**合法入库**的 `0:0` 行（无人守馆的 `规则` 占位行），所以上面这条查证 SQL **必须加 `m.kind = 'friendly'`**，否则会把踢馆的占位行当成"漏网"。它们不会影响任何友谊统计（§5-M13 的 `kind` 隔离）。

| 结果 | 处置 |
|---|---|
| **0** | 该问题彻底闭合，本节可删 |
| **> 0** | 再决定是否补一道独立门禁（入库前拦掉胜方的占位行）；**不要顺手改统计口径** —— 那会牵连 §7.4 里 7 个功能 |

### 10.3 v1.14.0 新登记的取舍（不是 bug，是已知边界）

| # | 事项 | 现状与理由 |
|---|---|---|
| **13** | **`friendly` 过滤是手写的 15 处，没有共用实现** | 18 个 `AND m.kind = 'friendly'` 散在 `database.py` 各处。可选方案：给 `matches` 建一个 `friendly_matches` 视图，或抽一个 `_friendly_clause()` 常量 —— 这次没做（会把改动面从"加过滤"扩大到"改所有 SQL 的拼法"），代价是**下次加战报类型时仍要靠人肉全表扫描**。§7.3 第 15 条已登记 |
| **14** | **`get_raid_points_by_opponent` 只有踢馆加点** | `/排行 队伍` 的「踢馆」列**不含守馆加点的分摊**。守馆积分是月度封顶的总额（`min(10, …)`），不能按对手拆开 —— 硬拆会让各对手之和 ≠ 总额。守馆方的完整分只能看 `/踢馆` |
| **15** | **多踢馆者按"各记一份"处理** | 规则只覆盖单人踢馆。若一场左侧出现多个出场者，**每人都拿到该场的踢馆方得分**（会让战队总分虚高）。选这个方向是因为"更可能"的是同一人挂多个ID，而不是真的多人踢 —— 真多人踢只是规则未覆盖，不是数据错 |
| **16** | **CSV/JSON 新增两列可能影响下游脚本** | 导出 CSV 加了「战报类型」「防守方馆主」，**列数变了**。接口文档没单独维护，靠 README 与 `main.py` 的表头列表对齐 |

### 10.4 v1.15.0 新登记的取舍（不是 bug，是已知边界）

| # | 事项 | 现状与理由 |
|---|---|---|
| **17** | **并列名次跨奖励边界时按行位置截断** | `/结算 7月 4` 遇到第 4、5 行同为展示名次 4（`ranks_for` 给 `1,2,3,4,4`）时，第 4 行拿满 50、**第 5 行没有** —— 截断依据是 `enumerate` 的行位置，不是名次。两个展示名次相同的人一个有一个没有。选这个方向是因为「按名次发」无法定义发几份（并列 10 个第 4 名难道发 10 份上限？），按行位置至少总额可控。`test_settle_tie_across_boundary_cuts_by_row_position` 钉住此行为 |
| **18** | **跨年只在 `/结算` 生效，`/排行` 仍有年份缺口** | `settle_month_range` 会在 `month > now.month` 时回退一年，但 `month_range` **仍然没有年份参数**（恒取 `now.year`）。所以 1 月里 `/结算 十二月` 结算的是**去年** 12 月（正确），而 `/排行 十二月` 查的是**今年** 12 月（空榜）—— 看不到刚结算出的奖金列。这是既有的 `month_range` 缺口，v1.15.0 **刻意没扩大改动面**去动它（`month_range` 被 `/排行` `/战绩` `/趋势` `/导出` `/踢馆` 共用，改它会一次性波及 5 个功能的默认月份行为）。回执里恒显年份（「2025年12月」）就是为了让人看出落到了哪一年 |
| **19** | **结算是快照，不随新战报自动重算** | 结算把那一刻的榜单前 N 名连同总积分、奖金**存下来**。之后补录战报让排名变了，已发的奖金**不会自动跟着改**，`/排行` 的「奖金」列显示的是结算时的结果，未必与当前名次一一对应。要更新只能重跑 `/结算`（覆盖重算，见 §5-M14）。选这个方向是因为"自动重算"意味着历史某月的表彰结果会被人事后改数据悄悄改写，反而不符合"发过就算数"的直觉 |
| **20** | **`/排行 全部` 不显示奖金列** | 结算记录属于**单个战队**（`home_team`），跨队榜上显示某一队的奖金字段没有意义。故该分支**不给 rows 加 `bonus` 键** |
| **21** | **面板的守馆三行之和 ≠ 积分 − 友谊积分** | 面板上「守馆首轮 / 守馆 / 踢馆」是**各自赚了多少**的原始分量，不套 10 分月度封顶；封顶只体现在最后一行「积分」上。守馆多的月份里三行之和会**大于**「积分 − 友谊积分」，被上限吃掉的部分在面板上看不到，**SHUT DOWN（+5）也因此没有独立的展示位**。这是用户明确选定的方向（「各自赚了多少」而不是「实得」），不是 bug。`tests/test_stats.py::test_build_team_panel_ignores_monthly_cap` 钉住此行为 |
| **22** | **面板只在图片路径** | `ranking_image=false`，或图片渲染失败退回 `format_player_ranking` 时，**没有面板** —— 那 14/15 列的契约由 `build_ranking_cells` 独占，把面板塞进文字表格会破坏列数契约（§7.3 第 16 条）。面板是**图片独有的展示层**，不是数据层的第 15/16 列 |
| **23** | **面板的胜/负/总场数是「比赛级」，右侧表格是「对局级」** | 面板取自 `get_home_team_record` → `matches.winner`（一场比赛算 1），右侧个人榜取自个人 duel 聚合（一个人一场里打了几局就算几局）。**同一个人的「总场数」「胜率」在左右两侧对不上是正常的**，不是数据错。两边真正同源的只有「积分」行 —— 它取 `total_points`，与表格「总积分」列一字不差（M-01 的求交仍在 `get_home_team_record` 里成立） |
| **24** | **非管理员的 `/结算` 看不到「尚未结算」月份的预览金额** | 查询命中未结算月份时**只回一句「该月尚未结算」**，不预告「如果现在结算会发多少」。要预览就得把 `get_player_ranking` → `ranks_for` → `rank_bonus` 整条发钱路径跑一遍，等于把结算逻辑变成对所有群员开放的查询接口。这是用户明确选定的方向（`/结算` 对群员是**查历史**，不是**试算**）。管理员若想看预览，等月份结束后正常跑一次 `/结算` 即可（本身就是覆盖重算，跑两次不留痕） |
| **25** | **非管理员的 `/结算` 不受「当月未结束」校验** | 「当月拒结」只挡写路径（挡的是「9 月里手滑结算 9 月」）。读路径查本月不拦，直接回「尚未结算」—— 本月本就不可能结算过，挡它只会让用户白跑一趟还看不懂原因。反过来，`settle_month_range` **两条路径共用**（在分叉之前调用）：`key` 必须一致，否则查不到管理员刚写进去的那份。副作用是 1 月里非管理员跑 `/结算 十二月` 查的是**去年** 12 月 —— 与管理员结算的正是同一份，符合直觉 |
| **26** | **结算公告的 @ 判据是「名次变了」，不是「金额变了」** | `/结算` 重跑时只有 `(名次, 获奖人)` 变了才 @ 全群；总积分/奖金因为事后补录战报而微调、名次却没动时**不 @**，只在回执后附一句「名次和上次一样」。选这个方向是因为补录是常态，为几分钱反复 @ 全群太吵。代价是：**金额悄悄变了但没人被提醒**（群里只看得到自己那份回执）。要强制重发公告，改个名次数重跑即可（能同时造出名单变化） |
| **27** | **没绑定 QQ 的获奖成员 @ 不出来** | 结算公告对绑定过 QQ 的成员发真的 `At`（会弹提醒），其余退化成字面的「@名字」纯文本 —— 不亮蓝、不提醒。**不是 bug**：`users.qq_id` 为空时插件没有任何可 @ 的目标。用 `/绑定ID` 补上后下次结算就会真 @ |
| **28** | **公告只保证在 QQ 平台上 @ 有效** | `At` 是 AstrBot 的消息组件，非 QQ 平台（如 WebChat）渲染成什么由平台适配器决定。插件**不做平台判断**（与全插件其它输出一致，那边也没有平台分支） |
| **29** | **重置后重新结算会重新 @ 一遍全群** | `/重置结算` 把该月打回「从没结算过」，于是下一次 `/结算` 必然被判成**该月第一次结算** → 一定发公告、一定 @ 所有获奖成员（§5-M14「结算公告」）。这是重置的语义决定的，不是 bug：重置的本意就是「当作没结算过，重来」。**代价**：管理员若只想悄悄改个金额，重置+重结算会让全群再收一次 @。想避免只能接受「名次没变就不 @」那条 —— 而重置恰好把「旧记录」抹了，比对基准没了 |
| **30** | **`/重置结算` 也只认月份，不认名次数** | 参数解析复用 `_parse_settle_args` 后把名次数丢掉，所以 `/重置结算 7月 12` 与 `/重置结算 7月` 等价。重置永远是**整月**清空（`settlements` 的粒度就是「队+月」），没有「只撤掉第 5~12 名」这种部分重置 —— 要做只能改完名次数重跑 `/结算`（那本来就是覆盖重算） |
| **31** | **语气只承担语气，不承担信息** | 猫娘语气词一律不进任何**需要被解析或复制**的内容 —— 战报模板、命令语法、表格行列、CSV 表头、逐场核对行都不含「喵」，用户可以直接复制粘贴回机器人（清单见 §5-M10「不在此列」）。代价是**分界只能靠位置判断，不能靠词形**：「积分」既是图片面板标签也是字段名，`（本月）` 既出现在叙述抬头也出现在数据行里，长得一样的两处处理方式相反。改文案前先确认**这一行会不会被 `parse_*` / `format_*` / 前端表格读到**，是则不动。守卫在测试里：叙述句只加「含语气词且关键信息仍在」的轻量锚点，数据行断言逐字不变（§12） |

---

## 11. 配置项（`_conf_schema.json`）

| 配置 | 默认 | 作用于 |
|---|---|---|
| `submit_requires_admin` | false | F-04（**仅 AstrBot 管理员**，不含群管理） |
| `allow_private_chat` | true | F-04 私聊提交（用 `地点:` 作群号） |
| `ranking_limit` | 10 | F-05 `全部` 分支的条数 |
| `ranking_image` | true | F-05 输出形态（关 → 文本表格） |
| `ranking_image_max_rows` | 30 | F-05 `全部` 分支图片截断；**默认个人榜不截断** |
| `min_games` | 1 | F-05 入榜最低场次（SQL `HAVING total >=`，口径见 §10-2） |
| `min_duels` | 3 | F-04 单份战报最少对局数，低于即拒收。**与 `min_games` 是两件事**：一个是提交前置条件，一个是排名门槛 |
| `trend_chart_width` / `height` | 960 / 480 | F-07 图片尺寸 |
| `default_rule` | `2/3【KOF】` | F-01 未指定规则时的默认值；**同时决定 M-07 走哪个判定分支** |
| `super_admin` | 1443290403 | F-20/F-21/F-22/F-23 权限 |
| `pairing_seed` | 空 | F-01/F-02 随机种子（填空则固定，便于复现） |
| `mysql_host/port/user/password/db` | — | 数据库连接；`db` 会做标识符白名单校验防注入 |

> **删配置项时**：旧配置文件里残留的键（如 `home_team`）是无害的 —— 代码不再读它。但**四处要一起删**（schema、`main.py` 读取处、README 配置表、本表），否则会留下界面上的死开关（§7.1）。
>
> `tests/test_config_schema.py` 钉住了格式与"死配置不许回来"两条。

---

## 12. 测试地图

| 测试文件 | 覆盖 |
|---|---|
| `test_parser.py` | 战报解析、标记剥离、**替补推断**、月份/导出参数解析 |
| `test_winner.py` | 人头赛 / KOF 胜负判定（含替补不扩容、同名首发与替补区分、TK 占位、判罚） |
| `test_rounds.py` | `/第N轮` 随机配对、指定对阵、紧凑比分、队对齐、部分成功 |
| `test_record.py` | `/记录` 找未记录对阵、右侧选手、插入最新轮次 |
| `test_lineup.py` | 名单解析、模板生成、TK 占位、种子可复现 |
| `test_match_stats.py` | 友谊/无双计算（含漏记兜底回归）、**14 列排行**的单元格值/列对齐/并列名次、`format_player_record` 的踢馆三次数行、**`is_kof` 真值表与规则门控**。**v1.15.0 未改动且继续通过** —— 这是「未结算路径仍是 14 列、行为逐字不变」的验收。另含**文案口吻锚点**：抬头行断言含「喵」（如 `lines[0] == "🏆 个人积分榜（全部）喵～"`），而 `format_player_record` 断言**第二行数据行逐字不变** —— 语气只进句子、不进字段（§5-M10、§10.4-31） |
| `test_settlement.py` | **月度结算**（§5-M14）：封顶表与边界（第 1 名 130、封顶线上下、名次 0/13 → 0）、奖金不写回 `total_points`、`ranks_for` 的并列口径（含 `1,1,3` 与「必须与表格名次列逐行一致」）、`settle_month_range` 的跨年回退、当月、**缺省=上个月**（且与显式写上个月等价、永不落在当前月）、`_parse_settle_args` 的两种参数顺序、**`attach_bonus` 空 map 一个键都不加（未结算月份不长第 15 列）与已结算时每行都补键**、`build_ranking_cells` 有/无 `bonus` 键时 15/14 列、`format_settlement` 文案与年份回显、并列跨边界按行截断、**只读回执 `format_settlement_view` 的抬头/尾注措辞与「与写回执行体逐字相同」**、**结算公告**：`settlement_awards_changed` 的全部分支（首次 / 重跑同名次 / 追加名次 / 缩回前 4 / 两人对调 / **只动奖金与总积分不算变**）、`settlement_announcement` 的 @ 段与单人无分隔、`format_settlement_repeat` 解释「为什么没 @ 人」 |
| `test_raid.py` | **踢馆**（§5-M13）：5 份真实样例的解析与判定、`(馆主)` 只认右侧、`规则` 占位、`2.1` 报错、首轮同时计守馆成功与守馆首轮、SHUT DOWN、馆主叠加（4人+馆主=5）、月度降重取最高、守馆上限 10、无人守馆的战队/个人口径一致、排行合并与按总积分排序、踢馆指纹、**守馆积分向上取整**（`_ceil_div3`：1 次 = 1 分、2 次 = 1 分，即用户报的 0 分 bug 的回归）、**回执逐局措辞的两种视角**（提交方为踢馆方 → 进攻；提交方为守馆方 → 防守） |
| `test_fingerprint.py` | **内容指纹**（§5-M12）：同内容同指纹、改比分/换战队变指纹、换提交人/群/地点不变、写死的算法锚点（**友谊赛锚点必须继续成立** —— 这是"`kind` 只在非友谊赛进指纹"的验收） |
| `test_config_schema.py` | `_conf_schema.json` 格式合法性、死配置不许回归、`min_duels` 存在性 |
| `test_export.py` | 文本还原（raw 头部保留、双空格、`(替)`/`(规则)`/`(馆主)`、`round_no == 0` 无轮次头）、导出过滤全分支 |
| `test_stats.py` | 帮助分类与渲染（`test_render_help_head_and_tail_are_catgirl`：抬头与末行含语气，而 **`▎排表` / `默认本月` / `群属性：/群聊属性` / `/帮助 全部` 等命令字面量逐字不变**，§5-M10）、**`build_team_panel` 的 8 行标签顺序 / ceil 取整回归 / 不套 10 分封顶 / 空 dict 兜底 / `:g` 与表格同格式 / 胜率 1 位小数** |
| `test_chart.py` | 排行图片生成、行数缩放、长名、不截断、**`rank_aligns()` 与表头列数一致且全为 `center`**、**15 列（带奖金列）渲染冒烟**、**「战队战绩」面板**：面板比表格高时图变高（`body_h = max()` 的裁切回归）、表格更高时高度不变、`panel=None`/不传/`[]` 三者尺寸逐像素相同、`max_rows=1` 的省略行与面板共存 |
| `test_database_stats.py` | **DB 集成**（排行/战绩/趋势/导出/迁移/用户绑定/群管理/**重复战报拒收**/**踢馆**/**结算**：写入后取回、重结算行数不翻倍、追加后回到前 4 名会删掉多余行、不同战队/月份互不干扰、`player_name` 与排行同源、小数往返、**`get_settlement_entries` 的明细与排序**（只读 `/结算` 的数据源）、**`clear_settlement` 的重置**（只删本队本月、返回行数、重复重置返回 0、重置后可重新结算且不残留旧行）、**`get_qq_ids_by_names`**（空名单不查库、只返回有 QQ 的、按 `users.name` 而非参赛ID、跨战队不串门）；**`get_home_team_record` 的 `attack_points` 与 `get_raid_team_stats` 等值**——面板「零额外查询」的护栏） |

**运行 DB 测试需 `ASTRBOT_TEST_MYSQL_PASSWORD` 环境变量**（`conftest.py` 从 `_conf_schema.json` 读默认值，密码走环境变量避免入库）。

**踢馆侧的 DB 验收标准（★必跑）：** `test_raid_does_not_move_friendly_numbers` —— 插入一份踢馆报后，**所有友谊查询的返回值必须逐字段不变**（比对时把 `raid_points`/`total_points` 等踢馆字段摘掉）。这是 §5-M13 那 18 处 `kind` 过滤的**唯一**验收手段：漏写一处不会报错，只会让数据静默变脏。另有 `test_raid_same_player_keeps_friendly_untouched`（同一个人既打友谊又踢馆时，友谊口径仍不变）。

> ⚠️ **`test_database_stats.py` 会连 `_conf_schema.json` 里配置的那台 MySQL**，并在开始时执行 `DROP DATABASE IF EXISTS astrbot_battle_report_test`。默认配置指向本机，但**若改了 `mysql_host` 指向生产库，它就会在生产库上建/删测试库**。跑之前先确认配置指向。
>
> ⚠️ **指纹唯一键改变了这些测试的前提**：同一测试内插入"内容一字不差"的两份战报现在会抛 `DuplicateReportError`。此前靠"只改 `group_id`/`submitted_by`"来造两份数据的用例，必须改成有真实的 `match_time` 差异（见各用例里的注释）。

---

## 13. 运维与版本

- **版本号有三处，必须同步改**：`metadata.yaml` 的 `version`、`main.py` 的 `@register(..., "1.15.0")`、`README.md` 第 3 行的版本徽章（实际漏改的就是这一处）
- **部署**：插件目录放到 `AstrBot/data/plugins/astrbot_plugin_battle_report`，管理面板启用（`requirements.txt` 自动装 `aiomysql`、`Pillow`）
- **启动即迁移**：`initialize()` 建库建表 + 跑 `_init_schema` 迁移；迁移异常会导致全插件"数据库未连接"
- **凭据**：MySQL 密码存于 `data/config/astrbot_plugin_battle_report_config.json`；本仓库是公开仓库，勿把生产凭据写入代码或提交

---

## 附：一句话总结每条铁律的落点

| 铁律 | 落点 |
|---|---|
| 口径是全局的 | §5 的 14 条 + §7.4 反向索引 |
| 同一件事有多个实现 | §7.3 的 19 条同源实现（第 10、11、12、17、18 条已收敛，留作"别再拆开"的警示；第 15 条 `friendly` 过滤**恰恰是反例** —— 它在 18 处手写，没有共用实现；第 16 条排行列布局**也是反例** —— 数据行是手写定长列表；第 19 条是 v1.15.0 新登记的「面板必须取原始分量」） |
| 历史数据不会重算 | §7.2 七步 + `SCHEMA_VERSION` 迁移 + §5-M12「改指纹算法 = 破坏历史去重」 |
| 加一个新类型会波及全部旧类型 | §7.1「新增一个战报类型」+ §5-M13 的 18 处 `kind` 过滤 + §12 的 `test_raid_does_not_move_friendly_numbers` |
