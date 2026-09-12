# astrbot_plugin_battle_report

AstrBot 战队战报插件：QQ 群约战排表 + 战报解析入库 + 跨群统计。数据存线上 MySQL。

## 改代码前必读

[REQUIREMENTS.md](REQUIREMENTS.md) 的两节，不要跳过：

- **§5 全局口径** —— 14 条横切机制（M-01~M-14）：群隔离、战队归属、玩家名解析、`0:0` 占位、判罚、替补、胜者判定、时间范围、渲染、消息收发、群属性、去重指纹、踢馆赛、月度结算
- **§7.1 变更传导矩阵** —— 改 X 必须同时改哪些；§7.3 是「同一逻辑在多处各写一遍」的清单

## 文档规矩

**加/改功能时，两份文档必须一起改**（硬要求）：

| 文件 | 视角 | 改什么 |
|---|---|---|
| [README.md](README.md) | 使用者 | 核心功能条目、命令语法、示例、配置表、更新日志、版本徽章 |
| [REQUIREMENTS.md](REQUIREMENTS.md) | 开发者 | §6 功能清单加 F-xx；新口径改 §5；新联动改 §7；新技术债改 §10；新配置改 §11；新测试改 §12 |

逐项清单见 REQUIREMENTS.md **§9 新增功能检查清单**（含版本号三处、删配置项四处等容易漏的收尾动作）。

## 易踩的坑（详见 REQUIREMENTS.md §10）

- **统计按战队跨群聚合**（`home_team`），不是按群隔离——改统计 SQL 时别顺手加 `group_id` 过滤。同理，指纹（§5-M12）**不含 `group_id`**：换个群提交同一份战报算重复
- **改内容指纹算法 = 破坏历史去重**。已有行的指纹按旧算法算，改了算法会让同一份战报能再存一次。`tests/test_fingerprint.py` 里钉了一个写死的期望值作锚点
- **写迁移必须可重放**：`_init_schema` 没有外层 try/except，任何一条 ALTER 失败 → `initialize()` 抛异常 → `db_ready=False` → 全插件不可用。加列/索引前一律先用 `information_schema` 探测
- **战队解析只有一条路径**：`_require_home()`（必须群绑定）。别再引入配置兜底（v1.13.0 刚删掉 `_get_effective_home` + `home_team` 配置）
- **加排行列**：`_RANK_HEADERS` 改完，`rank_aligns(ncols)` 自动跟随，但 `build_ranking_cells` 的**数据行是手写定长列表**，必须手动同步
- **排行图片左侧的「战队战绩」面板**（`stats.build_team_panel`）**不走 `build_ranking_cells`** —— 那 14/15 列的契约由它独占。面板是图片独有的展示层，文字表格（`format_player_ranking`）没有它。加行要同步 `_TEAM_PANEL_LABELS` 与返回值列表两处
- **改名次口径**：唯一实现是 `stats.ranks_for()`，排行榜表格与 `/结算` 发奖金共用——另写一份会让榜上第 1 名按第 2 档发钱
- **奖金列开关**：唯一实现是 `stats.attach_bonus(rows, bonus_map)`，`bonus_map` 空就**一个 `bonus` 键都不加**——`build_ranking_cells` 正是靠「行里有没有这个键」决定加不加第 15 列。写成无条件赋值会让未结算月份也长出整列 0
- **`/结算` 是双模式命令**（`main.settle`）：`_is_manager` 之前是共用的（`_require_home`、参数解析、`settle_month_range`），之后分叉 —— **写路径**（管理员）和**只读查询**（其他人）。**加任何写操作必须放进 `is_admin` 分支内**，放到分叉前会让群员的查询也写库。两个回执 `format_settlement` / `format_settlement_view` 的抬头**刻意不同形**（「结算完成」/「结算查询」），行体共用 `stats._settlement_lines`
- **结算公告的 @ 只在「该月首次结算 / 获奖名次变了」时发**：判据是 `stats.settlement_awards_changed`（比 `(rank_no, player)`，**不比奖金**）。⚠️ `main.settle` 里**读旧记录的 `get_settlement_entries` 必须在 `set_settlement` 之前** —— 挪到后面永远比不出变化，每次重跑都会 @ 全群。公告文案是 `stats.settlement_announcement` 的**分段**（`("at", 名)` 不能拼进字符串，否则 QQ 上只剩字面的「@名字」）；取 QQ 走 `database.get_qq_ids_by_names`，它按 **`users.name`（展示名）** 查而不是 `player_ids.player_name`（参赛ID）—— 榜上的 `player` 是 `COALESCE(u.name, d.player_a)`
- **`/重置结算` 只做一件事：`database.clear_settlement` 删本队本月那几行**（`DELETE`，不是 `UPDATE` 置标志位）。它是**纯管理命令**（`_is_manager` 硬拦，没有 `/结算` 那种群员只读分支），返回 0 行 = 该月本就没结算过 → 回「无需重置」而**不是报错**。`clear_settlement` 一旦被改成带附加写操作（连删用户、改积分），`/结算` 的双模式分叉就白设计了
- **月份缺省 = 上个月**：唯一实现是 `battle_report_parser.settle_month_range(month: int | None = None)`（`12 if now.month == 1 else now.month - 1`），`/结算` 与 `/重置结算` **共用**它——两边的 `key` 必须由同一个函数产出，否则「重置 8 月」会把 7 月的记录删掉。⚠️ 「当月未结束」的拦截只在 `/结算` 的**写路径**里，`/重置结算` **没有**这个检查（重置一个未过完的月无害）
- **面板的守馆/守馆首轮/踢馆三行是「各自赚了多少」的原始分量**（`_ceil_div3` + `attack_points`），**刻意绕开带 10 分封顶的 `raid_defense_points`**。别"顺手修"成 `raid_defense_points`——封顶只体现在面板最后一行「积分」上
- **`is_kof()` 是判定规则的唯一实现**（`battle_report_parser`），别在别处重写 `"人头" in rule`
- **文案风格：改「句子」，不改「数据」**。用户可见的叙述句统一猫娘口吻（中等浓度：句尾「喵」/「喵～」，关键回执配一个颜文字，不自称「人家」不称「主人」）。**绝不能动**的：表格表头 `_RANK_HEADERS`/`_BONUS_HEADER`、面板标签 `_TEAM_PANEL_LABELS`、命令名与参数、`lineup.generate_template` 的战报模板与 `main._FORMAT_EXAMPLE`（**对局行双空格是 `battle_report_parser` 的解析契约**）、`format_duel_results`/`format_raid_results` 的逐场数据行、CSV 表头、`/查ID`·`/管理ID`·`/群列表`·`/战队列表` 的列表项行、`render_help` 的正文（只改抬头与收尾）。⚠️ **「结算完成」/「结算查询」的措辞是判据不是文案** —— 它们承担「写过库」与「只读」的区分，别当文案改得同形（§5-M14）。测试的守卫方式：叙述句断言「含语气词且关键信息仍在」，数据行断言**逐字不变**
- 积分/胜率/玩家名解析在 Python 与多处 SQL 里各写一遍（§7.3），只改一处会让数据自相矛盾
- `min_games`（排名门槛，在 SQL 的 `HAVING` 里）与 `min_duels`（提交前置条件，在 `main.py`）是**两件事**，别混

## 测试

```bash
pytest tests/ --ignore=tests/test_database_stats.py   # 纯逻辑，无需 DB
ASTRBOT_TEST_MYSQL_PASSWORD=<密码> pytest tests/       # 含 DB 集成测试
```

⚠️ `test_database_stats.py` 会连 `_conf_schema.json` 里配的那台 MySQL，并 `DROP DATABASE IF EXISTS astrbot_battle_report_test`。默认指向本机，但**改过 `mysql_host` 就指向生产库了**——跑之前先确认。
