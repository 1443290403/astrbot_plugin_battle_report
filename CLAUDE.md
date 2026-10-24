# astrbot_plugin_battle_report

AstrBot 战队战报插件：QQ 群约战排表 + 战报解析入库 + 跨群统计。数据存线上 MySQL。

## 改代码前必读

[REQUIREMENTS.md](REQUIREMENTS.md) 的两节，不要跳过：

- **§5 全局口径** —— 15 条横切机制（M-01~M-15）：群隔离、战队归属、玩家名解析、`0:0` 占位、判罚、替补、胜者判定、时间范围、渲染、消息收发、群属性、去重指纹、踢馆赛、月度结算、QQ 申请审批
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
- **文案风格：改「句子」，不改「数据」**。用户可见的叙述句统一猫娘口吻（中等浓度：句尾「喵」/「喵～」，关键回执配一个颜文字，不自称「人家」不称「主人」）。**绝不能动**的：表格表头 `_RANK_HEADERS`/`_BONUS_HEADER`、面板标签 `_TEAM_PANEL_LABELS`、命令名与参数、`lineup.generate_template` 的战报模板与 `main._FORMAT_EXAMPLE`（**对局行双空格是 `battle_report_parser` 的解析契约**）、`format_duel_results`/`format_raid_results` 的逐场数据行、CSV 表头、`/查ID`·`/管理ID`·`/群列表`·`/战队列表` 中的 ID、角色归属与群号数据、命令索引中的命令语法。⚠️ **「结算完成」/「结算查询」的措辞是判据不是文案** —— 它们承担「写过库」与「只读」的区分，别当文案改得同形（§5-M14）。测试的守卫方式：叙述句断言「含语气词且关键信息仍在」，数据行断言**逐字不变**
- 积分/胜率/玩家名解析在 Python 与多处 SQL 里各写一遍（§7.3），只改一处会让数据自相矛盾
- `min_games`（排名门槛，在 SQL 的 `HAVING` 里）与 `min_duels`（提交前置条件，在 `main.py`）是**两件事**，别混

## v1.16.0 权限与参数约束

- `/绑定战队`、`/群聊属性`：超管可设置任意群；其他人必须是对应战队管理员，QQ 群主/群管理和 AstrBot 全局管理员身份本身不提供这两项权限。
- `_is_manager` 包含超管、AstrBot 全局管理员、本群绑定战队管理员和 QQ 群主/群管理；战队管理员仅在本队已绑定群内继承插件管理权限。
- `/发送` 先拒绝私聊，再检查群启用、属性为战报群及绑定。引用、批量、踢馆均共用此入口；不再读取 `allow_private_chat`。
- 参赛 ID 用 `command_args.py` 的统一解析，允许引号、保留 ID 内空格。别重新用 `payload.split()` 拆名字。
- `team_admins` 按 `(home_team, qq_id)` 授权，schema v18；设置名单是覆盖，必须保留撤销与查看入口。

## v1.17.0 流程帮助

- v1.17.2：用户可见的固定示例使用虚构队名 a队/b队、云玩家/萌新/遗老等 ID；空格 ID 使用“示例 玩家”。修改示例名称时保持命令语法、战报字段与对局分隔格式不变。

- 默认群名仍是友谊群，不重命名、不增加群名别名。默认用途不等于已经明确设置属性。
- v1.17.1：普通帮助和全部流程只展示操作流程，不夹带开通步骤或默认/未设置状态。需要群设置的命令缺项时或显式请求设置帮助时，再展示简短引导；不展示 super_admin 配置说明、授权示例或超管功能入口。
- 默认帮助及开通提示统一由 `help_guide.py` 生成；`stats.render_help` 仅保留为 `/帮助 命令` 的索引。
- 友谊群的排表/记录/追加轮次不新增绑定要求；依赖战队的 `_require_home` 和 `/发送` 检查战队及明确群属性，缺项返回设置流程。导出也必须走同一前置检查。
- `get_group_chat_type_setting` 用 None 区分尚未设置；`get_group_chat_type` 仍返回默认友谊群，不得为了判断是否设置而偷偷写入默认行。
- `/绑定战队` 和 `/群聊属性` 成功后发送对应流程；帮助预览不得写入设置。数据库不可用时帮助显示通用预览，不谎报本群未绑定。

## v1.18.0 QQ 申请审批

- `RequestReviewFilter` 只匹配 OneBot 原始请求、严格匹配的私聊 QQ 邀请卡与明确的同意/拒绝短回复；不要注册会唤醒所有消息的监听。
- 好友与入群邀请通知私聊发送给 `super_admin`。审批及 `/申请列表` 仅允许该 QQ 私聊操作，不复用战队/群管理员权限，也不依赖 MySQL。
- `request_review.py` 原样保存并回传 flag；使用平台 ID + 机器人 self_id 隔离申请，不能拿 QQ 号或申请编号替代 flag。
- `request_reviews.json` 先持久化处理中状态再调用平台，处理完成后持久化结果；引用消息必须匹配已发送通知。处理超时/重启中断转为需核实，不自动重复执行。
- v1.18.1：同时读取原始 reply 与 AstrBot Reply；ID 不匹配时核验机器人发给当前超管的原通知，不因组件丢失而退回唯一 pending。由 main 注入 AstrBot logger；aiocqhttp NetworkError 属于 OSError，需归类为结果未确认。
- v1.18.2：确认 SnowLuma 后才从实时原始邀请卡的 groupcode/msgseq 建立独立申请，格式参照上游 formatGroupRequestFlag；不改写原 flag、不扫历史、不自动重定向旧编号。新卡片替代同群同邀请人的旧 pending 为 superseded，保留已确认/未知结果。QQ 明确返回 120162004 + already agree msg by self 时同步 approved，即使用户点拒绝也不能记成 rejected；120161001 等其他错误仍需保留失败，兼容路径需实机验证。
- 不自动同意申请，不处理群成员加群请求，不把群内“同意”当审批。
- v1.18.4：群邀请实际结果落盘后，使用原连接私聊原邀请人，通知实际同意/拒绝及群号。通知异常只提示超管手动告知，不改变审批状态；重复处理不重发。不要把通知异常当审批失败或向好友申请人发送入群结果。

## 安装包交付

- 更新时同时交付 ZIP。安装包根目录直接包含 metadata.yaml 与 main.py，使用 AstrBot 的安装包校验检查内容。
- `install-packages` 分支只存安装包，源码在 master；下载链接指向包文件，不使用 GitHub 自动生成的源码归档替代安装包。

## 用户提示风格

- v1.17.8：提示只讲结果和必要下一步，删除括号里的重复原因、内部处理过程或多余例子。不要为简短句子补一段解释。
- 按具体文案修改，不能用正则统一删除括号。战报 ID、月份/战队范围、权限、单位、颜文字及 `(替)`/`(规则)`/`(馆主)` 等格式标记仍要保留。
- 结算查询使用「结算查询」抬头与写入区分，无需再附带「这次一个字都没改」之类尾注。命令语法及真实用户数据不受文案清理影响。

## 测试

```bash
pytest tests/   # 默认跳过数据库模块，无需 DB
ASTRBOT_TEST_MYSQL_ENABLE=1 ASTRBOT_TEST_MYSQL_PASSWORD=<测试密码> pytest tests/  # 独立测试实例
```

数据库测试只使用独立的 `ASTRBOT_TEST_MYSQL_HOST/PORT/USER/PASSWORD`（默认本机），
**不继承插件配置**。必须显式设置 `ASTRBOT_TEST_MYSQL_ENABLE=1` 才启用；会创建并删除
`astrbot_battle_report_test_<进程号>`。使用隔离测试实例，勿配置生产服务。
