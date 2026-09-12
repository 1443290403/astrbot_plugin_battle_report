"""战报文本解析器（纯 Python，零框架依赖，便于单元测试）。

解析用户粘贴的战报文本。

**友谊赛**（`kind = "friendly"`）::

    战队: KC VS DYG
    时间: 2026.08.01
    规则: 2/3【KOF】
    地点: 435823386
    ------第一轮------
    红莲 2:1 牌大
    凯撒亮 2:1 蓝大
    悠悠球 1:2 老千

**踢馆赛**（`kind = "raid"`，走 `parse_raid_report`）::

    KC踢馆BAR
    规则：OCG.2026.7.1.MATCH
    2026.8.30 21:00
    踢馆开始！
    雨落 2:1 念心
    雨落 2:1 云猫(馆主)

    左=踢馆方（team_a），右=守馆方（team_b）。与友谊赛的三点差异：
    时间行**没有 `时间:` 前缀**；没有轮次分隔；防守方名后可带 `(馆主)`。
    防守方名字恰为 `规则` 的对局行是**空位占位**（守馆方没派人），不入库。

解析结果分致命错误（errors，非空则拒绝入库）与可忽略警告（warnings，入库后随
回复提示）。"""

import hashlib
import json
import re
from datetime import datetime, timedelta
from dataclasses import dataclass, field


KIND_FRIENDLY = "friendly"
KIND_RAID = "raid"

# 踢馆战报里「守馆方没人守」的空位占位 ID：`雨落 0:0 规则` 的右侧
RAID_PLACEHOLDER = "规则"


@dataclass
class Duel:
    """一场对局（一名玩家 A vs 一名玩家 B，带比分）。"""

    round_no: int
    player_a: str
    score_a: int
    player_b: str
    score_b: int
    a_sub: bool = False  # 玩家 A 是否为替补
    b_sub: bool = False  # 玩家 B 是否为替补
    ruled: bool = False  # 本场对局是否被规则（判罚方比分更低、必为败方）
    owner: bool = False  # 踢馆赛：本对局的**防守方（右侧）**是否为馆主


@dataclass
class BattleReport:
    """一份解析完成的战报。"""

    team_a: str = ""
    team_b: str = ""
    match_time: str = ""  # 归一化为 ISO "YYYY-MM-DD"
    rule: str = ""
    location: str = ""
    group_id: str = ""
    submitted_by: str = ""
    submitted_name: str = ""
    duels: list[Duel] = field(default_factory=list)
    kind: str = KIND_FRIENDLY  # "friendly" | "raid"


@dataclass
class ParseResult:
    """解析结果。errors 非空表示战报不可入库。"""

    report: BattleReport | None
    errors: list[str]
    warnings: list[str]


# 轮次分隔，如 "------第一轮------" / "=== 第 2 轮 ==="
ROUND_RE = re.compile(
    r"^\s*[-=~]{2,}\s*第\s*([一二三四五六七八九十百零\d]+)\s*轮\s*[-=~]{2,}\s*$"
)
# 战队行："战队: A VS B"
TEAM_RE = re.compile(r"^\s*战队\s*[:：]\s*(.+)$", re.IGNORECASE)
# 时间行："时间: 2026.08.01"
TIME_RE = re.compile(r"^\s*时间\s*[:：]\s*(.+)$", re.IGNORECASE)
# 规则行："规则: 2/3【KOF】"
RULE_RE = re.compile(r"^\s*规则\s*[:：]\s*(.+)$", re.IGNORECASE)
# 地点行："地点: 435823386"
LOC_RE = re.compile(r"^\s*地点\s*[:：]\s*(.+)$", re.IGNORECASE)
# 比分："2:1" / "2：1"
SCORE_RE = re.compile(r"(\d+)\s*[:：]\s*(\d+)")
# 战队分隔符 "VS"（两侧有空格）
VS_RE = re.compile(r"\s+VS\s+", re.IGNORECASE)
# 踢馆战报首行："KC踢馆BAR"（左=踢馆方，右=守馆方）
RAID_RE = re.compile(r"^\s*(.+?)\s*踢馆\s*(.+?)\s*$")
# 踢馆战报的对局开始标记行："踢馆开始！"（不参与任何解析）
_RAID_START_RE = re.compile(r"^\s*踢馆\s*开始\s*[！!]?\s*$")
# 踢馆战报的裸时间行开头（无 `时间:` 前缀）："2026.8.30 21:00"
_BARE_DATE_RE = re.compile(r"^\s*(\d{4}\s*[.\-/年]\s*\d{1,2}\s*[.\-/月]\s*\d{1,2})")
# 替补标记（玩家名末尾）：红莲(替) / 红莲（替） / 红莲 （替） / 红莲(替补) / 红莲（ 替补 ）等
_SUB_RE = re.compile(r"[\s　]*[\(（][\s　]*替(?:补)?[\s　]*[\)）]$")
# 判罚落败标记（玩家名末尾）：红莲(规则) / 红莲（规则）等
_RULED_RE = re.compile(r"[\s　]*[\(（][\s　]*规则[\s　]*[\)）]$")
# 馆主标记（踢馆战报的防守方名末尾）：云猫(馆主) / 云猫（馆主）等
_OWNER_RE = re.compile(r"[\s　]*[\(（][\s　]*馆主[\s　]*[\)）]$")
# 小数点比分（如 2.1）。这是**错误**而非警告：静默丢行会让防守者人数少算一人、
# 踢馆点数直接算错。见 parse_raid_report / parse_battle_report 的报错分支。
_DOT_SCORE_RE = re.compile(r"\d+\s*[.。]\s*\d+")
_DOT_SCORE_HINT = "比分用了小数点（如 2.1），应为冒号（如 2:1）"
# 命令末尾的 时间=X月 参数（旧写法）：时间=7月 / 时间：七月 / 时间 = 7 / 时间＝12月 等
_MONTH_RE = re.compile(r"[\s　]*时间\s*[:：=＝]\s*([一二三四五六七八九十百\d]+)\s*月?[\s　]*$")
# 命令末尾直接写月份（新写法，去掉 时间= 前缀）：七月 / 7月 / 十二月 等
_MONTH_TOKEN_RE = re.compile(r"[\s　]*([一二三四五六七八九十百\d]+)\s*月[\s　]*$")
# 命令末尾的 最近N天 参数：最近7天 / 7天 / 最近 7 天 等
_DAYS_RE = re.compile(r"[\s　]*(?:最近[\s　]*)?(\d+)[\s　]*天[\s　]*$")
# 单个 token 形态的 最近N天（中间位置也能识别）：最近7天 / 7天 / 最近 7 天
_TOKEN_DAYS_RE = re.compile(r"^最近[\s　]*(\d+)[\s　]*天$|^(\d+)[\s　]*天$")
# 单个 token 形态的月份：时间=7月 / 时间=7 / 7月 / 七月（中间位置也能识别）
_TOKEN_MONTH_RE = re.compile(
    r"^时间\s*[:：=＝]\s*([一二三四五六七八九十百\d]+)\s*月?$|^([一二三四五六七八九十百\d]+)\s*月$"
)


def _strip_sub(name: str) -> tuple[str, bool]:
    """剥离玩家名末尾的替补标记，返回（干净ID, 是否替补）。"""
    m = _SUB_RE.search(name)
    if m:
        return name[: m.start()].strip(), True
    return name.strip(), False


def _strip_ruled(name: str) -> tuple[str, bool]:
    """剥离玩家名末尾的判罚落败标记，返回（干净ID, 是否判罚落败）。"""
    m = _RULED_RE.search(name)
    if m:
        return name[: m.start()].strip(), True
    return name.strip(), False


def _strip_owner(name: str) -> tuple[str, bool]:
    """剥离玩家名末尾的馆主标记，返回（干净ID, 是否馆主）。"""
    m = _OWNER_RE.search(name)
    if m:
        return name[: m.start()].strip(), True
    return name.strip(), False


def _clean_player_name(name: str) -> tuple[str, bool, bool, bool]:
    """依次剥离替补/判罚/馆主标记（循环直到无标记）。

    Returns:
        (干净ID, 是否替补, 是否判罚落败, 是否馆主)
    """
    is_sub = is_ruled = is_owner = False
    while True:
        changed = False
        name, s = _strip_sub(name)
        if s:
            is_sub = True
            changed = True
        name, r = _strip_ruled(name)
        if r:
            is_ruled = True
            changed = True
        name, o = _strip_owner(name)
        if o:
            is_owner = True
            changed = True
        if not changed:
            return name, is_sub, is_ruled, is_owner


def _parse_month_filter(payload: str) -> tuple[str, int | None]:
    """从命令 payload 提取末尾的月份参数。

    支持两种写法（均在末尾）：
    - 新写法：直接写 `X月`，如 `七月` / `7月` / `十二月`（默认推荐）
    - 旧写法：`时间=X月`，如 `时间=7月` / `时间：七月` / `时间=7`（全角等号 ＝ 亦可）
    纯数字（不带 月）不识别为月份，避免与趋势的『最近N天』（如 `7`=最近7天）冲突。

    Returns:
        (清理后的 payload, 月份 int | None)。未指定时月份为 None（调用方默认本月）。
    """
    # 先试旧写法（时间=…），再试新写法（末尾裸 X月）
    m = _MONTH_RE.search(payload)
    if not m:
        m = _MONTH_TOKEN_RE.search(payload)
    if not m:
        return payload, None
    month = _cn_to_int(m.group(1))
    return payload[: m.start()].rstrip(), month


def parse_export_payload(payload: str) -> dict:
    """解析 /导出 参数：玩家名 / 胜负范围 / 时间 / 文件格式。

    语法（顺序无关）：导出 [玩家名] [胜场|负场|全部] [X月|最近N天] [csv|json]

    - 时间：`X月`/`时间=X月`（走 _parse_month_filter）或 `最近N天`/`N天`；
      先剥末尾再逐 token 归类，可出现在任意位置；同时给出时月份优先（调用方处理）。
    - 玩家：剩余非关键字 token 用空格 join（多词名兼容）。

    Returns:
        {"player": str|None, "outcome": "全部|胜场|负场",
         "month": int|None, "days": int|None, "fmt": "csv"|"json"|None}
    """
    days = None
    m = _DAYS_RE.search(payload)
    if m:
        days = int(m.group(1))
        payload = payload[: m.start()].rstrip()
    payload, month = _parse_month_filter(payload)
    tokens = payload.split()
    fmt = None
    outcome = "全部"
    rest: list[str] = []
    for t in tokens:
        tl = t.lower()
        if tl in ("csv", "json"):
            fmt = tl
        elif t in ("胜场", "负场"):
            outcome = t
        elif t == "全部":
            outcome = "全部"
        else:
            m = _TOKEN_DAYS_RE.match(t)
            if m:
                days = int(m.group(1) or m.group(2))
                continue
            m = _TOKEN_MONTH_RE.match(t)
            if m:
                month = _cn_to_int(m.group(1) or m.group(2))
                continue
            rest.append(t)
    player = " ".join(rest) if rest else None
    return {"player": player, "outcome": outcome, "month": month, "days": days, "fmt": fmt}


def _month_bounds(year: int, m: int) -> tuple[str, str]:
    """某年某月的 [1号, 月末]。"""
    start = f"{year}-{m:02d}-01"
    if m == 12:
        end = f"{year}-12-31"
    else:
        end = (datetime(year, m + 1, 1) - timedelta(days=1)).strftime("%Y-%m-%d")
    return start, end


def month_range(month: int | None = None) -> tuple[str, str]:
    """某月的日期区间 [当月1号, 当月月末]。month 缺省/非法时取当前月。

    ⚠️ **没有年份参数** —— 永远取 `now.year`，所以 1 月里查不到去年 12 月。
    结算要跨年，走 `settle_month_range`。
    """
    now = datetime.now()
    m = month if month and 1 <= month <= 12 else now.month
    return _month_bounds(now.year, m)


def settle_month_range(month: int | None = None) -> tuple[str, str, str]:
    """结算用的月份区间 → `(起始日, 结束日, 'YYYY-MM')`。

    **该月在本年度的序号大于当前月**（即今年这个月还没到）时回退一年 —— 这样
    1 月能结算去年 12 月，`month_range` 的年份缺口不会卡住结算。

    `month` 缺省 = **上个月**（`/结算` `/重置结算` 不带月份时的目标）：没人会想
    结算一个还没过完的当月，所以「不写月份」的自然含义就是「刚过完的那个月」。
    1 月的上个月是 12 月，由下面的回退分支顺带处理，**不用另写一套跨年逻辑**。

    调用方**必须自己挡掉「当前月」**：`month == now.month` 时返回的本月区间尚未
    结束（`end >= 今天`），直接拿去结算会结一个还在进行中的月份。
    年份要回显给用户 —— 9 月跑 `/结算 十二月` 落到的是**去年** 12 月，只写「12月」
    会让人以为结的是今年。
    """
    now = datetime.now()
    if month is None:
        month = 12 if now.month == 1 else now.month - 1
    year = now.year - 1 if month > now.month else now.year
    start, end = _month_bounds(year, month)
    return start, end, f"{year}-{month:02d}"


# 中文数字
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_TENS = {"十": 10, "百": 100}


def _cn_to_int(s: str) -> int | None:
    """中文数字/阿拉伯数字字符串转 int。无法识别返回 None。

    支持 "一/3/十二/二十/二十三" 等写法。
    """
    s = s.strip()
    if s.isdigit():
        return int(s)
    total = 0
    current = 0
    for ch in s:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_TENS:
            # "十" 开头或单独出现时视为 1×10
            if ch == "十" and current == 0:
                current = 1
            total += current * _CN_TENS[ch]
            current = 0
        else:
            return None
    total += current
    return total or None


def _normalize_date(raw: str) -> str:
    """多种日期格式归一化为 ISO "YYYY-MM-DD"。

    支持 "2026.08.01" / "2026-08-01" / "2026/08/01" / "2026.8.1" 等。
    """
    raw = raw.strip().replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-")
    raw = re.sub(r"-+", "-", raw).strip("-")
    parts = raw.split("-")
    if len(parts) != 3:
        raise ValueError(f"无法识别的日期格式: {raw!r}")
    y, m, d = parts
    if not (y.isdigit() and m.isdigit() and d.isdigit()):
        raise ValueError(f"无法识别的日期格式: {raw!r}")
    y, m, d = int(y), int(m), int(d)
    if not (1 <= m <= 12 and 1 <= d <= 31):
        raise ValueError(f"日期数值非法: {raw!r}")
    return f"{y:04d}-{m:02d}-{d:02d}"


def parse_battle_report(text: str) -> ParseResult:
    """解析战报文本。

    Returns:
        ParseResult: errors 非空时 report 为 None（不可入库）。
    """
    report = BattleReport()
    errors: list[str] = []
    warnings: list[str] = []

    # 行切分与清洗：去 \r、全角空格转半角、忽略空行
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip().replace("　", " ").strip()
        if line:
            lines.append(line)
    if not lines:
        return ParseResult(None, ["战报内容为空。"], [])

    round_no = 0
    duel_count = 0
    in_round = False

    for lineno, line in enumerate(lines, start=1):
        # 轮次分隔
        m = ROUND_RE.match(line)
        if m:
            r = _cn_to_int(m.group(1))
            round_no = r if r is not None else round_no + 1
            in_round = True
            continue

        # 战队: A VS B
        m = TEAM_RE.match(line)
        if m:
            parts = VS_RE.split(m.group(1).strip())
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                errors.append(f"第 {lineno} 行：战队格式应为『战队: A VS B』，得到：{line}")
                continue
            # 队标含字母时统一转大写（与排表名单一致）
            report.team_a, report.team_b = parts[0].strip().upper(), parts[1].strip().upper()
            continue

        # 时间:
        m = TIME_RE.match(line)
        if m:
            try:
                report.match_time = _normalize_date(m.group(1).strip())
            except ValueError as e:
                errors.append(f"第 {lineno} 行：时间格式无法识别（应为如 2026.08.01），原因：{e}")
            continue

        # 规则:
        m = RULE_RE.match(line)
        if m:
            report.rule = m.group(1).strip()
            continue

        # 地点:
        m = LOC_RE.match(line)
        if m:
            report.location = m.group(1).strip()
            continue

        # --- 对局行：玩家A 比分 玩家B ---
        if not in_round:
            errors.append(f"第 {lineno} 行：对局『{line}』出现在任何轮次分隔之前。")
            continue

        hits = list(SCORE_RE.finditer(line))
        if not hits:
            # 小数点比分（2.1）是格式错误，不是「无关行」——静默丢这一行会少算一名
            # 对阵选手，胜负与点数直接错。其余无法识别的行仍是警告。
            if _DOT_SCORE_RE.search(line):
                errors.append(f"第 {lineno} 行：{_DOT_SCORE_HINT}：{line}")
            else:
                warnings.append(f"第 {lineno} 行：未识别为对局，已忽略：{line}")
            continue
        if len(hits) > 1:
            errors.append(f"第 {lineno} 行：一行出现多个比分，无法解析：{line}")
            continue

        m = hits[0]
        score_a, score_b = int(m.group(1)), int(m.group(2))
        player_a, a_sub, a_ruled, _ = _clean_player_name(line[: m.start()].strip())
        player_b, b_sub, b_ruled, _ = _clean_player_name(line[m.end():].strip())
        if not player_a or not player_b:
            errors.append(f"第 {lineno} 行：玩家名缺失：{line}")
            continue

        report.duels.append(
            Duel(round_no, player_a, score_a, player_b, score_b, a_sub, b_sub, a_ruled or b_ruled)
        )
        duel_count += 1

    # 替补判定（领域规则）：第一轮没出战的选手，在后续轮次出场即视为替补，
    # 即使文本没有 (替) 标记。替补替换首轮选手、不扩容总出战人数。
    # 首轮名单按左右队伍分别统计（左=team_a，右=team_b），含 0:0 占位。
    r1_a = {d.player_a for d in report.duels if d.round_no == 1}
    r1_b = {d.player_b for d in report.duels if d.round_no == 1}
    for d in report.duels:
        if d.round_no > 1:
            if d.player_a not in r1_a:
                d.a_sub = True
            if d.player_b not in r1_b:
                d.b_sub = True

    # 完整性校验
    if not report.team_a or not report.team_b:
        errors.append("缺少战队信息，需以『战队: A VS B』开头。")
    if not report.match_time:
        errors.append("缺少时间信息，需以『时间: 2026.08.01』开头。")
    if duel_count == 0:
        errors.append("未解析到任何对局。")

    if errors:
        return ParseResult(None, errors, warnings)
    return ParseResult(report, [], warnings)


def is_raid_header(line: str) -> bool:
    """该行是否是踢馆战报的首行（如 `KC踢馆BAR`）。"""
    ln = line.strip()
    if not ln or ln.startswith("战队:") or "规则" in ln:
        return False
    return bool(RAID_RE.match(ln))


def split_reports(text: str) -> list[str]:
    """按战报首行（`战队: …` 或 `A踢馆B`）把文本拆成多份（支持一次提交多条）。"""
    chunks: list[str] = []
    current: list[str] = []
    for ln in text.splitlines():
        if ln.strip().startswith("战队:") or is_raid_header(ln):
            if current:
                chunks.append("\n".join(current))
            current = [ln]
        else:
            current.append(ln)
    if current:
        chunks.append("\n".join(current))
    return chunks


def parse_raid_report(text: str) -> ParseResult:
    """解析一份踢馆战报。

    格式::

        KC踢馆BAR
        规则：OCG.2026.7.1.MATCH
        2026.8.30 21:00          ← 裸时间行，无 `时间:` 前缀
        踢馆开始！
        雨落 2:1 念心
        雨落 2:1 云猫(馆主)

    与友谊赛的三点差异：

    1. 首行 `A踢馆B` 给出双方队标（左=踢馆方 team_a，右=守馆方 team_b），
       并且**没有轮次分隔** —— 所有对局的 `round_no` 恒为 0（还原时不输出轮次头）。
    2. 时间行不带前缀，靠 `_normalize_date` 直接认。
    3. 防守方名字恰为 `规则` 的对局行是**空位占位**（守馆方没派人守这个位置）。
       这类行**照常入库**（比分一律是 `0:0`，全库通用的「0:0 = 还没打」口径
       自动把它排除在胜负与人数之外），只是**不建成参赛ID** —— 否则战队 ID 池
       里会多出一个叫「规则」的假人。见 database.insert_report。

    胜负判定见 `determine_raid_winner`（看最后一场，不是 KOF）。
    """
    report = BattleReport(kind=KIND_RAID)
    errors: list[str] = []
    warnings: list[str] = []

    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip().replace("　", " ").strip()
        if line:
            lines.append(line)
    if not lines:
        return ParseResult(None, ["战报内容为空。"], [])

    duel_lines = 0
    seen_time = False

    for lineno, line in enumerate(lines, start=1):
        # 首行：A踢馆B
        m = RAID_RE.match(line)
        if m and not report.team_a:
            a, b = m.group(1).strip(), m.group(2).strip()
            if not a or not b:
                errors.append(f"第 {lineno} 行：踢馆格式应为『A踢馆B』，得到：{line}")
                continue
            report.team_a, report.team_b = a.upper(), b.upper()
            continue

        # 对局开始标记行：跳过，不进警告
        if _RAID_START_RE.match(line):
            continue

        m = RULE_RE.match(line)
        if m:
            report.rule = m.group(1).strip()
            continue

        # 裸时间行：2026.8.30 21:00（前半是日期，时刻部分忽略）
        m = _BARE_DATE_RE.match(line)
        if m:
            try:
                report.match_time = _normalize_date(m.group(1))
                seen_time = True
                continue
            except ValueError as e:
                errors.append(f"第 {lineno} 行：时间格式无法识别，原因：{e}")
                continue

        # --- 对局行：踢馆者 比分 防守者 ---
        hits = list(SCORE_RE.finditer(line))
        if not hits:
            if _DOT_SCORE_RE.search(line):
                errors.append(f"第 {lineno} 行：{_DOT_SCORE_HINT}：{line}")
            else:
                warnings.append(f"第 {lineno} 行：未识别为对局，已忽略：{line}")
            continue
        if len(hits) > 1:
            errors.append(f"第 {lineno} 行：一行出现多个比分，无法解析：{line}")
            continue

        m = hits[0]
        score_a, score_b = int(m.group(1)), int(m.group(2))
        player_a, _, _, _ = _clean_player_name(line[: m.start()].strip())
        player_b, _, _, b_owner = _clean_player_name(line[m.end():].strip())
        if not player_a or not player_b:
            errors.append(f"第 {lineno} 行：玩家名缺失：{line}")
            continue

        duel_lines += 1
        # 防守方是 `规则` 占位行（守馆方没派人）也照常记入 duels：它的比分必为
        # 0:0，全库通用的「0:0 = 还没打」口径会把它排除在胜负/人数之外；入库侧
        # 另按 RAID_PLACEHOLDER 跳过建 ID。丢掉整行反而会让无人守馆的战报里
        # 连踢馆者的名字都查不到。
        report.duels.append(
            Duel(0, player_a, score_a, player_b, score_b, owner=b_owner)
        )

    if not report.team_a or not report.team_b:
        errors.append("缺少战队信息，需以『KC踢馆BAR』开头。")
    if not report.match_time:
        errors.append("缺少时间信息，需写一行日期（如 2026.8.30 21:00）。")
    if duel_lines == 0:
        errors.append("未解析到任何对局。")

    if errors:
        return ParseResult(None, errors, warnings)
    return ParseResult(report, [], warnings)


def is_kof(rule: str) -> bool:
    """规则是否为 KOF（即不是人头赛）。

    判定方式：规则行含『人头』或『head』（忽略大小写）为人头赛，其余一律按 KOF。
    空串按 KOF 处理 —— 与默认规则 `2/3【KOF】` 及历史数据一致。

    这是全插件唯一的规则类型判定入口：`determine_match_winner` 与
    `stats.compute_match_stats`（无双的数据兜底只在 KOF 成立）都复用它。
    """
    r = (rule or "").lower()
    return "人头" not in r and "head" not in r


def report_fingerprint(report: BattleReport, home_team: str = "") -> str:
    """战报内容指纹（sha256 十六进制），用于识别「完全相同的战报」被重复提交。

    刻意**不含** `group_id` / `location` / `submitted_by` / `raw_text`：
    同一场战报换个人提交、或同一战队的另一个群再提交一次，都算重复 ——
    统计是按 `home_team` 跨群聚合的，重复入库会让统计直接翻倍。

    `team_a`/`team_b` **保持原序、不排序**：『A VS B』与『B VS A』是两份不同的
    战报，排序会把它们误判为重复。

    **`kind` 只在非友谊赛时才进指纹。** 这样友谊赛的指纹与引入踢馆之前逐字节
    一致 —— 已有行的指纹仍然有效（改算法 = 破坏历史去重，见 REQUIREMENTS §5-M12），
    `tests/test_fingerprint.py` 里写死的锚点也仍然成立；踢馆报则自带类型区分。

    各字段在这里再做一次防御性归一化（大小写/空白），不依赖 parser 的归一化
    策略 —— 否则哪天解析侧的归一化改了，同一场比赛会算出两个指纹、幂等静默失效。
    """
    kind = (report.kind or KIND_FRIENDLY).strip()
    duels = []
    for d in report.duels:
        row = [
            int(d.round_no),
            str(d.player_a),
            int(d.score_a),
            str(d.player_b),
            int(d.score_b),
            1 if d.a_sub else 0,
            1 if d.b_sub else 0,
            1 if d.ruled else 0,
        ]
        # 馆主标记也是**只在踢馆时**进指纹：友谊赛那 8 个元素一个都不能动，
        # 否则历史指纹全作废（§5-M12）。
        if kind == KIND_RAID:
            row.append(1 if d.owner else 0)
        duels.append(row)
    payload = {
        "home_team": (home_team or "").strip().upper(),
        "team_a": (report.team_a or "").strip().upper(),
        "team_b": (report.team_b or "").strip().upper(),
        "match_time": str(report.match_time or "").strip(),
        "rule": (report.rule or "").strip().lower(),
        # 用 json 序列化而非手工拼分隔符：玩家名来自战报原文、可含任意字符，
        # 手工拼 `"a|b"` 会与字段边界产生歧义。
        "duels": duels,
    }
    if kind != KIND_FRIENDLY:
        payload["kind"] = kind
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def determine_match_winner(report: BattleReport) -> str | None:
    """根据战报类型/规则判定比赛胜者战队。

    **踢馆赛**（`kind == "raid"`）走 `determine_raid_winner`，不看规则行 ——
    踢馆的规则行是 `OCG.2026.7.1.MATCH` 这种赛制名，KOF/人头那套对它不适用。

    友谊赛：人头赛（`is_kof` 为假）按获胜对局数多的一方胜（平局返回 None）；
    否则按 KOF：一方所有有记录对局的选手最后一场均为负（全员败北）时，另一方胜；
    双方都未全员败北或数据异常返回 None（胜负未定）。

    Returns:
        胜者战队名（team_a 或 team_b）；无法判定返回 None。
    """
    if report.kind == KIND_RAID:
        return determine_raid_winner(report)
    if not report.duels:
        return None
    if is_kof(report.rule):
        return _winner_kof(report)
    return _winner_headcount(report)


def determine_raid_winner(report: BattleReport) -> str | None:
    """踢馆赛胜负：**看最后一场**。

    踢馆是 1 对多：踢馆方（team_a）要打穿防守方全部人，守馆方（team_b）只要
    终结踢馆者一次即胜。因此：

    - 最后一场踢馆者胜 → 踢馆方把守馆方打穿了 → team_a 胜（踢馆成功）
    - 最后一场防守者胜 → team_b 胜（守馆成功）
    - 一场未打（无人守馆，duels 为空，只有 `规则` 占位行）→ team_a 胜，0 分
    - 最后一场平局 → 胜负未定，返回 None（调用方拒录）

    `0:0` 占位行与防守方为 `规则` 的空位行都跳过。

    Returns:
        胜者战队名（team_a 或 team_b）；无法判定返回 None。
    """
    played = [
        d for d in report.duels
        if not (d.score_a == 0 and d.score_b == 0) and d.player_b != RAID_PLACEHOLDER
    ]
    if not played:
        # 无人守馆：没人拦得住踢馆者，判踢馆方胜（点数另算，见 stats.raid_attack_points）
        return report.team_a
    last = played[-1]
    if last.score_a > last.score_b:
        return report.team_a
    if last.score_b > last.score_a:
        return report.team_b
    return None


def _winner_headcount(report: BattleReport) -> str | None:
    """人头赛：只有一轮，按获胜对局数判定。判罚方比分更低（必为败方），由比分自然判定。"""
    wins_a = wins_b = 0
    for d in report.duels:
        if d.score_a == 0 and d.score_b == 0:
            continue  # 未打的占位
        if d.score_a > d.score_b:
            wins_a += 1
        elif d.score_b > d.score_a:
            wins_b += 1
    if wins_a == wins_b:
        return None
    return report.team_a if wins_a > wins_b else report.team_b


def _winner_kof(report: BattleReport) -> str | None:
    """2/3 KOF：一方可出战人数 = 第一轮出场的不同选手数（含 0:0 未打选手）。

    累计『不可出战』（各自最后一场为负或非零平分）的人数达到该数即『无人可出战』，
    另一方胜。0:0 为未完结：该选手仍可出战（计入人数、不计落败），平局只可能是
    1:1 等非零平分。替补不增加可出战总人数。
    """
    a_last: dict[tuple[str, bool], bool] = {}  # (选手, 是否替补) -> 是否不可出战
    b_last: dict[tuple[str, bool], bool] = {}
    a_capacity = b_capacity = 0
    seen_a_r1: set[tuple[str, bool]] = set()
    seen_b_r1: set[tuple[str, bool]] = set()
    for d in report.duels:
        key_a = (d.player_a, d.a_sub)
        key_b = (d.player_b, d.b_sub)
        played = not (d.score_a == 0 and d.score_b == 0)
        if played:
            # 负 或 非零平分 → 不可出战；0:0 未打不更新状态（仍可出战）。
            # 判罚方比分更低（必为败方），由比分自然判定。
            a_last[key_a] = d.score_a <= d.score_b
            b_last[key_b] = d.score_b <= d.score_a
        if d.round_no == 1:
            if key_a not in seen_a_r1:
                seen_a_r1.add(key_a)
                a_capacity += 1
            if key_b not in seen_b_r1:
                seen_b_r1.add(key_b)
                b_capacity += 1

    a_lost = sum(1 for loss in a_last.values() if loss)
    b_lost = sum(1 for loss in b_last.values() if loss)
    a_def = a_capacity > 0 and a_lost >= a_capacity
    b_def = b_capacity > 0 and b_lost >= b_capacity

    if a_def and not b_def:
        return report.team_b
    if b_def and not a_def:
        return report.team_a
    return None
