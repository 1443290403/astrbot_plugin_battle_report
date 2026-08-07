"""战报文本解析器（纯 Python，零框架依赖，便于单元测试）。

解析用户粘贴的战报文本，格式如下::

    战队: KC VS DYG
    时间: 2026.08.01
    规则: 2/3【KOF】
    地点: 435823386
    ------第一轮------
    红莲 2:1 牌大
    凯撒亮 2:1 蓝大
    悠悠球 1:2 老千
    ------第二轮------
    凯撒亮 1:2 老千
    红莲 1:2 老千

解析结果分致命错误（errors，非空则拒绝入库）与可忽略警告（warnings，入库后随
回复提示）。"""

import re
from datetime import datetime, timedelta
from dataclasses import dataclass, field


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
# 替补标记（玩家名末尾）：红莲(替) / 红莲（替） / 红莲 （替） / 红莲(替补) / 红莲（ 替补 ）等
_SUB_RE = re.compile(r"[\s　]*[\(（][\s　]*替(?:补)?[\s　]*[\)）]$")
# 判罚落败标记（玩家名末尾）：红莲(规则) / 红莲（规则）等
_RULED_RE = re.compile(r"[\s　]*[\(（][\s　]*规则[\s　]*[\)）]$")
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


def _clean_player_name(name: str) -> tuple[str, bool, bool]:
    """依次剥离替补/判罚标记（循环直到无标记），返回（干净ID, 是否替补, 是否判罚落败）。"""
    is_sub = is_ruled = False
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
        if not changed:
            return name, is_sub, is_ruled


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


def month_range(month: int | None = None) -> tuple[str, str]:
    """某月的日期区间 [当月1号, 当月月末]。month 缺省/非法时取当前月。"""
    now = datetime.now()
    m = month if month and 1 <= month <= 12 else now.month
    start = f"{now.year}-{m:02d}-01"
    if m == 12:
        end = f"{now.year}-12-31"
    else:
        end = (datetime(now.year, m + 1, 1) - timedelta(days=1)).strftime("%Y-%m-%d")
    return start, end


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
            warnings.append(f"第 {lineno} 行：未识别为对局，已忽略：{line}")
            continue
        if len(hits) > 1:
            errors.append(f"第 {lineno} 行：一行出现多个比分，无法解析：{line}")
            continue

        m = hits[0]
        score_a, score_b = int(m.group(1)), int(m.group(2))
        player_a, a_sub, a_ruled = _clean_player_name(line[: m.start()].strip())
        player_b, b_sub, b_ruled = _clean_player_name(line[m.end():].strip())
        if not player_a or not player_b:
            errors.append(f"第 {lineno} 行：玩家名缺失：{line}")
            continue

        report.duels.append(
            Duel(round_no, player_a, score_a, player_b, score_b, a_sub, b_sub, a_ruled or b_ruled)
        )
        duel_count += 1

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


def split_reports(text: str) -> list[str]:
    """按『战队:』行把文本拆成多份战报（支持一次提交多条）。"""
    chunks: list[str] = []
    current: list[str] = []
    for ln in text.splitlines():
        if ln.strip().startswith("战队:"):
            if current:
                chunks.append("\n".join(current))
            current = [ln]
        else:
            current.append(ln)
    if current:
        chunks.append("\n".join(current))
    return chunks


def determine_match_winner(report: BattleReport) -> str | None:
    """根据规则判定比赛胜者战队。

    规则含『人头赛』：获胜对局数多的一方胜（平局返回 None）。
    否则按 KOF：一方所有有记录对局的选手最后一场均为负（全员败北）时，另一方胜；
    双方都未全员败北或数据异常返回 None（胜负未定）。

    Returns:
        胜者战队名（team_a 或 team_b）；无法判定返回 None。
    """
    if not report.duels:
        return None
    rule = (report.rule or "").lower()
    if "人头" in rule or "head" in rule:
        return _winner_headcount(report)
    return _winner_kof(report)


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
