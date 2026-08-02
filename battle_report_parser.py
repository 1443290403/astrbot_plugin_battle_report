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
from dataclasses import dataclass, field


@dataclass
class Duel:
    """一场对局（一名玩家 A vs 一名玩家 B，带比分）。"""

    round_no: int
    player_a: str
    score_a: int
    player_b: str
    score_b: int


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
            report.team_a, report.team_b = parts[0].strip(), parts[1].strip()
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
        player_a = line[: m.start()].strip()
        player_b = line[m.end():].strip()
        if not player_a or not player_b:
            errors.append(f"第 {lineno} 行：玩家名缺失：{line}")
            continue

        report.duels.append(Duel(round_no, player_a, score_a, player_b, score_b))
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
    """人头赛：只有一轮，按获胜对局数判定。"""
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
    """2/3 KOF：一方所有选手最后一场均为负（全员败北）时另一方胜。"""
    a_last: dict[str, tuple[int, bool]] = {}  # 选手 -> (对局序号, 该场是否负)
    b_last: dict[str, tuple[int, bool]] = {}
    for idx, d in enumerate(report.duels):
        if d.score_a == 0 and d.score_b == 0:
            continue  # 未打的占位不计
        a_last[d.player_a] = (idx, d.score_a < d.score_b)
        b_last[d.player_b] = (idx, d.score_b < d.score_a)

    def all_defeated(m: dict[str, tuple[int, bool]]) -> bool:
        if not m:
            return False
        return all(is_loss for _, is_loss in m.values())

    a_def = all_defeated(a_last)
    b_def = all_defeated(b_last)
    if a_def and not b_def:
        return report.team_b
    if b_def and not a_def:
        return report.team_a
    return None
