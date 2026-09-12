"""踢馆战报测试：解析、判定、加点、月度降重与封顶。

样例取自需求方给的 5 份真实战报（S1~S5）外加一份无人守馆的（S6）。
"""

from battle_report_parser import (
    KIND_RAID,
    RAID_PLACEHOLDER,
    determine_match_winner,
    determine_raid_winner,
    is_raid_header,
    parse_battle_report,
    parse_raid_report,
    report_fingerprint,
    split_reports,
)
from lineup import format_raid_results
from stats import (
    RAID_DEFENSE_CAP,
    compute_raid_match,
    aggregate_raid,
    raid_attack_points,
    raid_defense_points,
    raid_player_views,
    raid_team_view,
    merge_raid_into_ranking,
    sort_ranking,
    total_points,
)

# ---------- 样例 ----------

# S1 首轮失败：踢馆者第一场就被终结 → 守馆成功 + 守馆首轮
S1 = """KC踢馆FH
规则：OCG.2026.7.1.MATCH
2026.9.4 20:00
踢馆开始！
间桐樱  0:2  幽术师"""

# S3 被馆主终结：4 名防守者后第 5 个是馆主，终结踢馆者 → SHUT DOWN
S3 = """KC踢馆BAR
规则：OCG.2026.7.1.MATCH
2026.8.30 21:00
踢馆开始！
雨落 2:1 念心
雨落 2:0 青日
雨落 2:1 Defender
雨落 2:1 无语靴
雨落 1:2 云猫(馆主)"""

# S4 踢馆成功含馆主：4 人 + 馆主 = 5 个防守者，全被踢穿 → 3 + 2 = 5 分
S4 = """KC踢馆BAR
规则：OCG.2026.7.1.MATCH
2026.8.30 21:00
踢馆开始！
雨落 2:1 念心
雨落 2:0 青日
雨落 2:1 Defender
雨落 2:1 无语靴
雨落 2:1 云猫(馆主)"""

# S5 踢馆成功不含馆主：5 名普通防守者被踢穿 → 3 分
S5 = """KC踢馆BAR
规则：OCG.2026.7.1.MATCH
2026.8.30 21:00
踢馆开始！
雨落 2:1 念心
雨落 2:0 青日
雨落 2:1 Defender
雨落 2:1 无语靴
雨落 2:1 云猫"""

# S6 无人守馆：全是 `规则` 占位行 → 判踢馆方胜，只发徽章，0 分
S6 = """KC踢馆BAR
规则：OCG.2026.7.1.MATCH
2026.8.30 21:00
踢馆开始！
雨落 0:0 规则
雨落 0:0 规则
雨落 0:0 规则
雨落 0:0 规则
雨落 0:0 规则"""


def _duels(report) -> list[dict]:
    """把解析结果转成 compute_raid_match 需要的对局 dict（模拟 database 侧）。"""
    return [
        {
            "seq": i,
            "score_a": d.score_a,
            "score_b": d.score_b,
            "player_b": d.player_b,
            "owner": d.owner,
            "resolved_a": d.player_a,
            "resolved_b": d.player_b,
        }
        for i, d in enumerate(report.duels)
    ]


def _judge(text: str) -> dict:
    """解析 → 判胜负 → 出判定结果（踢馆侧全链路的纯逻辑部分）。"""
    r = parse_raid_report(text)
    assert not r.errors, r.errors
    rep = r.report
    winner = determine_match_winner(rep) or ""
    return compute_raid_match(_duels(rep), winner, rep.team_a, rep.team_b)


# ---------- 解析 ----------

def test_parse_raid_basic():
    r = parse_raid_report(S5)
    assert not r.errors
    rep = r.report
    assert rep.kind == KIND_RAID
    assert (rep.team_a, rep.team_b) == ("KC", "BAR")  # 左=踢馆方，右=守馆方
    assert rep.rule == "OCG.2026.7.1.MATCH"
    assert rep.match_time == "2026-08-30"  # 裸时间行，无 `时间:` 前缀
    assert len(rep.duels) == 5
    # 踢馆没有轮次，round_no 恒为 0
    assert all(d.round_no == 0 for d in rep.duels)
    assert [d.player_b for d in rep.duels][-1] == "云猫"


def test_is_raid_header():
    assert is_raid_header("KC踢馆BAR")
    assert is_raid_header("  KC 踢馆 BAR  ")
    assert not is_raid_header("战队: KC VS DYG")
    assert not is_raid_header("雨落 2:1 念心")
    assert not is_raid_header("")


def test_parse_raid_owner_marker():
    """`(馆主)` 只认右侧（防守方），解析后剥离，并置 owner=True。"""
    r = parse_raid_report(S4)
    assert not r.errors
    owners = [d.owner for d in r.report.duels]
    assert owners == [False, False, False, False, True]
    assert r.report.duels[-1].player_b == "云猫"  # 标记已剥离
    assert r.report.duels[-1].owner is True


def test_parse_raid_left_owner_marker_stripped_and_ignored():
    """左侧写 `(馆主)` 会被剥掉，但不记 owner（馆主只可能是防守方）。"""
    text = S5.replace("雨落 2:1 念心", "雨落(馆主) 2:1 念心")
    r = parse_raid_report(text)
    assert not r.errors
    assert r.report.duels[0].player_a == "雨落"
    assert r.report.duels[0].owner is False


def test_parse_raid_placeholder_row_is_kept():
    """`规则` 占位行照常留在 duels（比分 0:0 由通用口径排除），只是不建 ID。"""
    r = parse_raid_report(S6)
    assert not r.errors
    assert len(r.report.duels) == 5
    assert all(d.player_b == RAID_PLACEHOLDER for d in r.report.duels)
    assert all(d.score_a == 0 and d.score_b == 0 for d in r.report.duels)


def test_parse_raid_decimal_score_is_error():
    """`2.1` 不再静默丢行，升级为整份拒收。"""
    r = parse_raid_report(S1.replace("0:2", "0.2"))
    assert r.report is None
    assert any("小数点" in e for e in r.errors)


def test_parse_friendly_decimal_score_is_error_too():
    """友谊赛同一份检查：小数点比分同样拒收。"""
    r = parse_battle_report(
        "战队: KC VS DYG\n时间: 2026.08.01\n规则: 2/3【KOF】\n"
        "------第一轮------\n红莲 2.1 牌大"
    )
    assert r.report is None
    assert any("小数点" in e for e in r.errors)


def test_parse_raid_missing_duels_is_error():
    r = parse_raid_report("KC踢馆BAR\n规则：OCG.2026.7.1.MATCH\n2026.8.30 21:00\n踢馆开始！")
    assert r.report is None
    assert any("未解析到任何对局" in e for e in r.errors)


def test_parse_raid_missing_header_is_error():
    r = parse_raid_report("雨落 2:1 念心")
    assert r.report is None
    assert any("踢馆" in e for e in r.errors)


def test_split_reports_recognizes_raid_header():
    chunks = split_reports(S5 + "\n" + S6)
    assert len(chunks) == 2
    assert chunks[0].splitlines()[0] == "KC踢馆BAR"
    # 头 + 规则 + 时间 + 标记 + 5 个占位行
    assert len(chunks[1].splitlines()) == 9


# ---------- 胜负判定 ----------

def test_determine_raid_winner_uses_last_duel():
    """看最后一场：最后一场踢馆者胜 → 踢馆方；防守者胜 → 守馆方。"""
    assert determine_raid_winner(parse_raid_report(S5).report) == "KC"
    assert determine_raid_winner(parse_raid_report(S3).report) == "BAR"
    assert determine_raid_winner(parse_raid_report(S1).report) == "FH"


def test_determine_match_winner_dispatches_to_raid():
    """M-07：`determine_match_winner` 是唯一入口，踢馆报分流到踢馆判定。"""
    assert determine_match_winner(parse_raid_report(S3).report) == "BAR"


def test_determine_raid_winner_no_defender_is_raider():
    """无人守馆（全是占位行）→ 判踢馆方胜。"""
    assert determine_raid_winner(parse_raid_report(S6).report) == "KC"


def test_determine_raid_winner_draw_is_none():
    text = S5.replace("雨落 2:1 云猫", "雨落 1:1 云猫")
    assert determine_raid_winner(parse_raid_report(text).report) is None


# ---------- 判定与点数 ----------

def test_raid_points_no_owner_raid_success():
    """S5：踢穿 5 名普通防守者 → 踢馆成功 1 次，3 分，无徽章。"""
    t = aggregate_raid([raid_team_view(_judge(S5), "KC")])
    assert t["raid_success"] == 1
    assert t["raid_success_owner"] == 0
    assert t["attack_points"] == 3
    assert t["badge"] == 0
    assert t["raid_points"] == 3
    # 守馆方没有任何计数
    d = aggregate_raid([raid_team_view(_judge(S5), "BAR")])
    assert d["raid_points"] == 0 and d["hold"] == 0


def test_raid_points_with_owner_stacks():
    """S4：4 人 + 馆主 = 5 名防守者，踢破 → 3 + 2 = 5 分。"""
    t = aggregate_raid([raid_team_view(_judge(S4), "KC")])
    assert t["raid_success"] == 1
    assert t["raid_success_owner"] == 1
    assert t["attack_points"] == 5
    assert t["raid_points"] == 5


def test_raid_points_shutdown():
    """S3：馆主作为第 5 个守馆者终结踢馆者 → SHUT DOWN，+5。"""
    info = _judge(S3)
    assert info["shutdown"] == 1
    assert info["ender"] == "云猫"
    assert info["owner_name"] == "云猫"
    assert info["defender_count"] == 5
    d = aggregate_raid([raid_team_view(info, "BAR")])
    assert d["hold"] == 1
    assert d["shutdown"] == 1
    # ceil(1/3) + 5 = 6：守馆成功按「向上取整」计 1 分（规则原文「由上取整」）
    assert d["raid_points"] == 6


def test_raid_points_first_round_counts_hold_and_first_round():
    """S1：第一场就被终结 → 同时计入守馆成功与守馆首轮（两条奖励独立累积）。"""
    info = _judge(S1)
    assert info["hold"] == 1
    assert info["first_round"] == 1
    assert info["ender"] == "幽术师"
    assert info["defender_count"] == 1


def test_raid_points_not_first_round():
    """非首轮被终结：只计守馆成功。"""
    text = S1.replace("间桐樱  0:2  幽术师", "间桐樱 2:1 幽术师\n间桐樱 0:2 星尘")
    info = _judge(text)
    assert info["hold"] == 1
    assert info["first_round"] == 0


def test_raid_points_no_defender_badge_only():
    """S6：无人守馆 → 踢馆成功 + 徽章，0 分。"""
    t = aggregate_raid([raid_team_view(_judge(S6), "KC")])
    assert t["raid_success"] == 1
    assert t["badge"] == 1
    assert t["attack_points"] == 0
    assert t["raid_points"] == 0


def test_raid_points_low_defender_count():
    """踢破 1–3 名防守者 → 2 分。"""
    assert raid_attack_points(1, False) == 2
    assert raid_attack_points(3, False) == 2
    assert raid_attack_points(4, False) == 3  # 「3 个以上（不包括 3）」
    assert raid_attack_points(0, False) == 0
    assert raid_attack_points(3, True) == 4   # 叠加馆主
    assert raid_attack_points(0, False) == 0


def test_raid_defense_cap():
    """守馆积分封顶 10，且 SHUT DOWN 的 +5 也受这个上限约束。"""
    assert raid_defense_points(0, 0, 0) == 0
    assert raid_defense_points(3, 0, 0) == 1
    assert raid_defense_points(0, 3, 0) == 1
    assert raid_defense_points(6, 6, 0) == 2 + 2  # 两条奖励独立取整后相加
    assert raid_defense_points(3, 3, 1) == 1 + 1 + 5
    assert raid_defense_points(9, 9, 0) == 3 + 3  # 9/3=3 整，未触顶
    assert raid_defense_points(30, 0, 0) == RAID_DEFENSE_CAP
    assert raid_defense_points(0, 0, 2) == RAID_DEFENSE_CAP  # 10 封顶，不是 10 以上
    assert raid_defense_points(99, 99, 99) == RAID_DEFENSE_CAP


def test_raid_defense_points_round_up():
    """取整必须是**向上**取整（规则原文「除以3由上取整」）。

    用户实测：上传 2 把首轮守馆 → 守馆成功 2 次、首轮 2 次，积分应为 2。
    写成 `// 3`（向下取整）会得 0 —— 这条用例就是钉死这个回归的。
    """
    assert raid_defense_points(1, 0, 0) == 1   # ceil(1/3)，不是 0
    assert raid_defense_points(2, 2, 0) == 2   # ceil(2/3) + ceil(2/3) = 1 + 1
    assert raid_defense_points(2, 0, 0) == 1
    assert raid_defense_points(4, 0, 0) == 2   # ceil(4/3) = 2
    assert raid_defense_points(7, 0, 0) == 3   # ceil(7/3) = 3


# ---------- 月度降重 ----------

def test_repeat_raid_same_team_takes_max():
    """当月重复踢穿同一战队不累计，取最高分那场。"""
    v_low = raid_team_view({**_judge(S5)}, "KC")   # 3 分
    v_high = raid_team_view({**_judge(S4)}, "KC")  # 5 分
    agg = aggregate_raid([v_low, v_high])
    assert agg["raid_success"] == 2       # 次数照记
    assert agg["attack_points"] == 5      # 分数只取最高的那一场


def test_repeat_raid_different_teams_accumulate():
    """不同对手队累计。"""
    v1 = raid_team_view(_judge(S4), "KC")  # 打 BAR，5 分
    v2 = raid_team_view({**_judge(S4), "defender_team": "FH"}, "KC")  # 打 FH，5 分
    assert aggregate_raid([v1, v2])["attack_points"] == 10


def test_raid_view_unknown_team_is_none():
    assert raid_team_view(_judge(S5), "ZZZ") is None


# ---------- 个人口径 ----------

def test_raid_player_views_raider():
    """踢馆方：出场者拿到踢馆成功 + 点数，与战队侧一致。"""
    info = _judge(S4)
    views = dict(raid_player_views(info))
    assert set(views) == {"雨落"}
    assert views["雨落"]["raid_success"] == 1
    assert views["雨落"]["raid_success_owner"] == 1
    assert views["雨落"]["attack_points"] == 5
    assert views["雨落"]["team"] == "KC"


def test_raid_player_views_no_defender_group_agrees_with_team():
    """无人守馆：个人侧的踢馆成功/徽章/0 分要和战队侧逐字段一致。"""
    info = _judge(S6)
    views = dict(raid_player_views(info))
    assert views["雨落"]["raid_success"] == 1
    assert views["雨落"]["badge"] == 1
    assert views["雨落"]["attack_points"] == 0
    team = aggregate_raid([raid_team_view(info, "KC")])
    assert (team["raid_success"], team["badge"], team["raid_points"]) == (1, 1, 0)


def test_raid_player_views_defender_gets_hold():
    """守馆方：守馆成功/首轮/SHUT DOWN 都归终结踢馆者的那名防守者。"""
    views = dict(raid_player_views(_judge(S3)))
    assert views["云猫"]["hold"] == 1
    assert views["云猫"]["first_round"] == 0
    assert views["云猫"]["shutdown"] == 1
    assert views["云猫"]["team"] == "BAR"
    # 被终结的踢馆者不在守馆方的个人视图里
    assert "雨落" not in views


def test_raid_player_views_team_field_matches_side():
    """每份个人视图都带 team，调用方据此只取自己这一侧。"""
    info = _judge(S3)
    assert dict(raid_player_views(info))["云猫"]["team"] == "BAR"


def test_aggregate_raid_empty():
    agg = aggregate_raid([])
    assert agg["raid_points"] == 0
    assert agg["raid_success"] == 0 and agg["hold"] == 0


# ---------- 提交回执 ----------

def test_format_raid_results_verdict_appears_once():
    """回执的结论行只出现一次（曾经被 append 两遍）。"""
    out = format_raid_results(parse_raid_report(S4).report, "KC")
    assert out.count("⚔️ 踢馆成功") == 1
    assert out.count("🛡️ 守馆成功") == 0


def test_format_raid_results_hold_verdict():
    """踢馆失败时，**踢馆方**看到的结论行说的是自己的失败，不是对手的守馆成功。

    这行以前恒用守馆方措辞（`🛡️ 守馆成功（BAR）`），于是 KC 在自己群里提交一份
    没踢穿的战报，回执上写的是对手守馆成功 —— 和上面 `❌ 进攻失败` 自相矛盾。
    """
    out = format_raid_results(parse_raid_report(S3).report, "KC")
    assert out.count("💀 踢馆失败（KC）") == 1
    assert out.count("🛡️ 守馆成功") == 0
    assert out.splitlines()[-1] == "💀 踢馆失败（KC），防守者5人，含馆主"


def test_format_raid_results_hold_verdict_defender_perspective():
    """同一份没踢穿的战报，**守馆方**看到的仍是自己的守馆成功。"""
    out = format_raid_results(parse_raid_report(S3).report, "BAR")
    assert out.count("🛡️ 守馆成功（BAR）") == 1
    assert out.splitlines()[-1] == "🛡️ 守馆成功（BAR），防守者5人，含馆主"


def test_format_raid_results_breached_verdict_defender_perspective():
    """被踢穿的守馆方看到的结论行说的是自己的失败，不是踢馆方的成功。"""
    out = format_raid_results(parse_raid_report(S4).report, "BAR")
    assert out.count("💀 守馆失败（BAR）") == 1
    assert out.count("⚔️ 踢馆成功") == 0
    assert out.splitlines()[-1] == "💀 守馆失败（BAR），防守者5人，含馆主"


def test_format_raid_results_lines_and_owner_marker():
    """提交方是踢馆方（S4 里 KC = team_a）→ 用「进攻」措辞，且还原 `(馆主)` 标记。"""
    lines = format_raid_results(parse_raid_report(S4).report, "KC").splitlines()
    assert lines[0] == "雨落 2:1 念心 ✅ 进攻成功"
    assert lines[-2] == "雨落 2:1 云猫(馆主) ✅ 进攻成功"
    assert len(lines) == 6  # 5 局 + 1 行结论


def test_format_raid_results_defender_perspective():
    """同一份战报，提交方是守馆方（BAR = team_b）→ 换成「防守」措辞，比分不变。

    ✅/❌ 跟着比分走：本群是守馆方就看右侧，右侧赢=防守成功、右侧输=防守失败。
    """
    lines = format_raid_results(parse_raid_report(S4).report, "BAR").splitlines()
    # 雨落 2:1 念心 —— 右侧（守馆方）输了
    assert lines[0] == "雨落 2:1 念心 ❌ 防守失败"
    assert lines[-2] == "雨落 2:1 云猫(馆主) ❌ 防守失败"
    assert len(lines) == 6
    # 比分列恒为 `踢馆方:守馆方`，只有措辞随视角变
    assert lines[0].split()[1] == "2:1"


def test_format_raid_results_defender_perspective_win():
    """守馆方视角下，右侧赢了 → 防守成功。"""
    lines = format_raid_results(parse_raid_report(S1).report, "FH").splitlines()
    assert lines[0] == "间桐樱 0:2 幽术师 ✅ 防守成功"


def test_format_raid_results_draw():
    """最后一场平局 → 胜负未定，不算分。"""
    text = S5.replace("雨落 2:1 云猫", "雨落 1:1 云猫")
    out = format_raid_results(parse_raid_report(text).report, "KC")
    assert out.count("⚠️ 胜负未定") == 1


# ---------- 排行合并 ----------

def test_total_points():
    assert total_points(2.25, 0) == 2.25
    assert total_points(2.25, 5) == 7.25
    assert total_points(None, 3) == 3


def test_merge_raid_into_ranking():
    rows = [
        {"player": "雨落", "points": 2.0, "wins": 2, "losses": 0, "draws": 0, "total": 2},
        {"player": "红莲", "points": 9.0, "wins": 3, "losses": 0, "draws": 0, "total": 3},
    ]
    merge_raid_into_ranking(rows, {"雨落": {"raid_points": 5, "raid_success": 1}})
    assert rows[0]["raid_points"] == 5 and rows[0]["total_points"] == 7.0
    # 没踢馆记录的人按 0，总积分 = 积分
    assert rows[1]["raid_points"] == 0 and rows[1]["total_points"] == 9.0


def test_sort_ranking_by_total_points():
    """排序按总积分，踢馆积分能把人顶上去。"""
    rows = [
        {"player": "红莲", "points": 9.0, "wins": 3, "losses": 0, "draws": 0,
         "total": 3, "raid_points": 0, "total_points": 9.0},
        {"player": "雨落", "points": 2.0, "wins": 2, "losses": 0, "draws": 0,
         "total": 2, "raid_points": 10, "total_points": 12.0},
    ]
    ordered = sort_ranking(rows, limit=None)
    assert [r["player"] for r in ordered] == ["雨落", "红莲"]
    assert [r["player"] for r in sort_ranking(rows, limit=1)] == ["雨落"]


# ---------- 指纹 ----------

def test_raid_fingerprint_differs_from_friendly():
    """踢馆报的指纹带上 kind，与同内容的友谊报不同（否则会被当重复丢掉）。"""
    raid = parse_raid_report(S5).report
    friendly = parse_battle_report(
        "战队: KC VS DYG\n时间: 2026.08.01\n------第一轮------\n红莲 2:1 牌大"
    ).report
    assert report_fingerprint(raid) != report_fingerprint(friendly)


def test_raid_fingerprint_distinguishes_owner_flag():
    """S4 与 S5 只差一个 `(馆主)`，指纹必须不同。"""
    assert report_fingerprint(parse_raid_report(S4).report) != report_fingerprint(
        parse_raid_report(S5).report
    )
