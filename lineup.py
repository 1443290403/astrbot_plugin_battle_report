"""排表模块：解析战队名单 + 随机配对 + 生成战报模板（纯 Python，便于单测）。

输入格式（用户在命令后粘贴）::

    人头赛            <- 可选规则（也可以没有，用默认规则）
    KC:红莲 凯撒亮 悠悠球
    DYG:老千 蓝大 红大

生成战报模板（第一轮随机配对，比分 0:0 占位，第二轮留空）::

    战队: KC VS DYG
    时间: 2026.08.01
    规则: 人头赛
    地点: 435823386
    ------第一轮------
    红莲 0:0 红大
    凯撒亮 0:0 蓝大
    悠悠球 0:0 老千
    ------第二轮------

一方人数较少时，缺人的位置以 TK 占位，由用户替换为实际玩家名。
"""

import random
import re
from dataclasses import dataclass, field

try:
    # AstrBot 以包方式导入插件（data.plugins.<插件名>.main）
    from .battle_report_parser import ROUND_RE, SCORE_RE, _cn_to_int, parse_battle_report
except ImportError:  # 单测以顶层模块导入
    from battle_report_parser import ROUND_RE, SCORE_RE, _cn_to_int, parse_battle_report

# 队伍行："KC:红莲 悠悠球" / "KC：红莲 悠悠球"
TEAM_LINE_RE = re.compile(r"^([^:：]+)\s*[:：]\s*(.+)$")

_CN_DIGITS = "零一二三四五六七八九"


@dataclass
class RosterResult:
    """名单解析结果。errors 非空表示排表失败。"""

    teams: list[tuple[str, list[str]]] = field(default_factory=list)  # [(队伍名, [成员])]
    rule: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class GeneratedTemplate:
    """排表生成的模板与提示信息。"""

    template: str
    warnings: list[str] = field(default_factory=list)  # 如人数不匹配提示


def parse_lineup(text: str, default_rule: str) -> RosterResult:
    """解析名单文本。

    Args:
        text: 剥离命令词后的文本。首个非空行若不含"队伍名:"则为规则。
        default_rule: 未指定规则时使用的默认规则。

    Returns:
        RosterResult: errors 非空则排表失败。
    """
    result = RosterResult()
    errors: list[str] = []

    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip().replace("　", " ").strip()
        if line:
            lines.append(line)
    if not lines:
        errors.append("名单内容为空，请以『队伍名: 成员1 成员2』格式提供战队名单。")
        return RosterResult(errors=errors)

    rule_found = False
    for lineno, line in enumerate(lines, start=1):
        m = TEAM_LINE_RE.match(line)
        if not m:
            # 第一个非名单行视为规则（如 "/排表 人头赛" 剥离后首行是 "人头赛"）
            if lineno == 1 and not rule_found:
                result.rule = line.strip()
                rule_found = True
                continue
            errors.append(f"第 {lineno} 行：无法识别为队伍名单，格式应为『队伍名: 成员1 成员2』：{line}")
            continue
        team_name = m.group(1).strip()
        players = [p.strip() for p in re.split(r"[\s,，、]+", m.group(2).strip()) if p.strip()]
        if not team_name:
            errors.append(f"第 {lineno} 行：队伍名为空：{line}")
            continue
        if not players:
            errors.append(f"第 {lineno} 行：队伍『{team_name}』没有成员：{line}")
            continue
        result.teams.append((team_name, players))

    if not result.teams:
        errors.append("未解析到任何战队名单。")

    if not rule_found:
        result.rule = default_rule

    result.errors = errors
    return result


def _format_date_dotted(iso_date: str) -> str:
    """ISO "YYYY-MM-DD" 转展示用的 "YYYY.MM.DD"（与用户模板一致）。"""
    parts = iso_date.split("-")
    if len(parts) == 3:
        return ".".join(parts)
    return iso_date


def generate_template(
    team_a: str,
    players_a: list[str],
    team_b: str,
    players_b: list[str],
    match_date: str,   # ISO "YYYY-MM-DD"
    rule: str,
    location: str,
    seed: str | None = None,
) -> GeneratedTemplate:
    """生成战报模板：第一轮随机配对（0:0 占位）+ 空第二轮。

    对局数 = 两队人数最大值；人数较少的一方超出其名单的位置以 TK 占位，
    由用户替换为实际玩家名（第二轮为第一轮胜者晋级，不属于补充位）。

    Args:
        match_date: 比赛日期，ISO "YYYY-MM-DD"。
        seed: 非空则固定随机种子（便于复现/测试）。
    """
    a = list(players_a)
    b = list(players_b)
    if seed:
        random.seed(seed)
    random.shuffle(a)
    random.shuffle(b)

    count = max(len(a), len(b))
    round1_lines = []
    for i in range(count):
        p_a = a[i] if i < len(a) else "TK"
        p_b = b[i] if i < len(b) else "TK"
        round1_lines.append(f"{p_a} 0:0 {p_b}")

    lines = [
        f"战队: {team_a} VS {team_b}",
        f"时间: {_format_date_dotted(match_date)}",
        f"规则: {rule}",
        f"地点: {location}",
        "------第一轮------",
        *round1_lines,
        "------第二轮------",
    ]

    return GeneratedTemplate(template="\n".join(lines), warnings=[])


def format_roster_display(teams: list[tuple[str, list[str]]]) -> str:
    """把名单格式化为便于查看的文本（看排表命令用）。"""
    if not teams:
        return "（当前群尚未设置战队名单）"
    lines = []
    for name, players in teams:
        lines.append(f"{name}: {'、'.join(players)}")
    return "\n".join(lines)


# ---------- 追加轮次 ----------

@dataclass
class RoundBuildResult:
    """/第N轮 构建结果。"""

    ok: bool
    new_text: str
    added_lines: list[str]
    errors: list[str]


def parse_round_no(s: str) -> int | None:
    """解析轮次号（数字或中文），必须 >= 2（第 1 轮由排表生成），否则返回 None。"""
    n = _cn_to_int(s)
    if n is None or n < 2:
        return None
    return n


def _int_to_cn(n: int) -> str:
    """整数转中文数字（1-99）。"""
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN_DIGITS[n % 10]
    tens, ones = divmod(n, 10)
    return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")


def _find_round_index(lines: list[str], round_no: int) -> int | None:
    """在草稿行中定位第 round_no 轮的段头行号。"""
    for i, ln in enumerate(lines):
        m = ROUND_RE.match(ln)
        if m and _cn_to_int(m.group(1)) == round_no:
            return i
    return None


def _append_round(draft_text: str, round_no: int, lines: list[str]) -> str:
    """把第 round_no 轮的对局行追加进草稿（已有该段则插入，否则在末尾新建）。"""
    out = draft_text.splitlines()
    idx = _find_round_index(out, round_no)
    if idx is not None:
        insert_at = idx + 1
        while insert_at < len(out) and not ROUND_RE.match(out[insert_at]):
            insert_at += 1
        out[insert_at:insert_at] = lines
        return "\n".join(out).rstrip() + "\n"
    block = [f"------第{_int_to_cn(round_no)}轮------"] + lines
    return "\n".join(out + [""] + block).rstrip() + "\n"


def build_next_round(
    draft_text: str,
    round_no: int,
    info_lines: list[str],
    seed: str | None = None,
) -> RoundBuildResult:
    """在战报草稿上追加第 round_no 轮。

    无 info_lines 时：从上一轮可确认的胜者中随机配对（两队各自洗牌后按索引配对，
    人数较少一侧以 TK 占位）。
    有 info_lines 时：每行『玩家A』『玩家B』或『玩家A 比分 玩家B』追加为对局；
    比分可写冒号形式（2:0）或紧凑两位数字形式（20 表示 2:0）。
    """
    errors: list[str] = []
    parsed = parse_battle_report(draft_text)
    if parsed.errors:
        return RoundBuildResult(
            False, draft_text, [],
            ["当前战报无法解析：\n" + "\n".join(parsed.errors)],
        )
    report = parsed.report

    prev_round = max((d.round_no for d in report.duels if d.round_no < round_no), default=None)
    if prev_round is None:
        return RoundBuildResult(
            False, draft_text, [],
            [f"未找到第 {round_no} 轮之前的轮次，请先完成上一轮。"],
        )

    if not info_lines:
        # 随机匹配上一轮胜者
        winners_a: list[str] = []
        winners_b: list[str] = []
        for d in report.duels:
            if d.round_no != prev_round:
                continue
            if d.score_a > d.score_b:
                winners_a.append(d.player_a)
            elif d.score_b > d.score_a:
                winners_b.append(d.player_b)
        if not winners_a and not winners_b:
            return RoundBuildResult(
                False, draft_text, [],
                [f"第 {prev_round} 轮没有可确认的胜者（请先在战报中填写上一轮比分）。"],
            )
        a, b = list(winners_a), list(winners_b)
        if seed:
            random.seed(seed)
        random.shuffle(a)
        random.shuffle(b)
        count = max(len(a), len(b))
        added = []
        for i in range(count):
            pa = a[i] if i < len(a) else "TK"
            pb = b[i] if i < len(b) else "TK"
            added.append(f"{pa} 0:0 {pb}")
    else:
        added = []
        for line in info_lines:
            line = line.strip()
            if not line:
                continue
            m = SCORE_RE.search(line)
            if m:
                sa, sb = int(m.group(1)), int(m.group(2))
                pa = line[: m.start()].strip()
                pb = line[m.end():].strip()
                if not pa or not pb:
                    errors.append(f"无法解析对局行：{line}")
                    continue
                added.append(f"{pa} {sa}:{sb} {pb}")
            else:
                parts = line.split()
                if len(parts) == 2:
                    added.append(f"{parts[0]} 0:0 {parts[1]}")
                elif len(parts) == 3 and len(parts[1]) == 2 and parts[1].isdigit():
                    # 紧凑比分：20 → 2:0（KOF 每局比分不超过两位数字）
                    sa, sb = int(parts[1][0]), int(parts[1][1])
                    added.append(f"{parts[0]} {sa}:{sb} {parts[2]}")
                else:
                    errors.append(
                        f"无法解析对局行：{line}（应为『玩家A 玩家B』或『玩家A 比分 玩家B』）"
                    )
        if errors:
            return RoundBuildResult(False, draft_text, [], errors)
        if not added:
            return RoundBuildResult(False, draft_text, [], ["没有可添加的对局。"])

    new_text = _append_round(draft_text, round_no, added)
    return RoundBuildResult(True, new_text, added, [])
