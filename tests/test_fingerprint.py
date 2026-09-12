"""战报内容指纹（重复提交去重）单元测试。

指纹是入库去重的唯一依据，必须**恰好**覆盖「这份战报说了什么」：
少一分会让同一份战报重复入库（统计翻倍），多一分会把用户改过比分后的
正当重提当成重复拒掉。本文件把两侧边界都钉住。纯函数，不需要数据库。
"""

from battle_report_parser import parse_battle_report, report_fingerprint

# 这份战报在 KC 下的指纹。写死在这里是有意的回归锚点：一旦指纹算法
# （字段集合 / 归一化方式 / JSON 序列化参数）被改动，这条会立刻失败 ——
# 线上已有历史行的指纹是按老算法算的，改算法会让同一份战报能再存一次。
_EXPECTED_KC = "17ae61e039d8e72e3aae1c917289e0f7626a4bc9e9fdf04c60f3c328a84928ee"

_BASE = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 435823386
------第一轮------
红莲 2:1 牌大
凯撒亮 2:1 蓝大
------第二轮------
凯撒亮 1:2 老千"""


def _report(text=_BASE):
    r = parse_battle_report(text)
    assert not r.errors, r.errors
    return r.report


def test_same_report_same_fingerprint():
    """同一份战报解析两次 → 同一个指纹（无随机盐、无时间戳）。"""
    assert report_fingerprint(_report()) == report_fingerprint(_report())


def test_fingerprint_is_sha256_hex():
    fp = report_fingerprint(_report())
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_matches_recorded_value():
    """算法锚点：见文件顶部 _EXPECTED_KC 的说明。"""
    assert report_fingerprint(_report(), "KC") == _EXPECTED_KC


def test_changed_score_changes_fingerprint():
    """改一个比分 → 必须视为另一份战报（否则改分重提会被拒）。"""
    changed = _BASE.replace("红莲 2:1 牌大", "红莲 2:0 牌大")
    assert report_fingerprint(_report(changed)) != report_fingerprint(_report())


def test_submitter_group_location_not_in_fingerprint():
    """换提交人 / 换群 / 地点写什么，都**不**改变指纹。

    这是刻意的：KC 群和 DYG 群各提交一次同一场比赛是同一个问题，不该在
    库里留两条（会让跨群统计翻倍）。所以这三个字段不能进指纹。
    """
    base = report_fingerprint(_report())
    for field, value in [
        ("submitted_by", "99999"),
        ("submitted_name", "另一个人"),
        ("group_id", "OTHER_GROUP"),
        ("location", "999999"),
    ]:
        rep = _report()
        setattr(rep, field, value)
        assert report_fingerprint(rep) == base, f"{field} 不该影响指纹"


def test_home_team_in_fingerprint():
    """归属战队不同 → 不同指纹。

    同一组对战文本挂在不同战队下，在统计里是两回事（统计按 home_team
    跨群聚合），不能互相顶掉。
    """
    rep = _report()
    assert report_fingerprint(rep, "KC") != report_fingerprint(rep, "DYG")


def test_home_team_default_empty():
    """不传 home_team 与传空串等价（历史行/旧调用点没有该参数）。"""
    rep = _report()
    assert report_fingerprint(rep) == report_fingerprint(rep, "")


def test_team_order_matters():
    """team_a / team_b 保持原序，不排序 —— 主客关系是战报原文的一部分。"""
    swapped = _BASE.replace("战队: KC VS DYG", "战队: DYG VS KC")
    assert report_fingerprint(_report(swapped)) != report_fingerprint(_report())


def test_case_and_space_insensitive():
    """战队名/规则的字母大小写、首尾空白不产生新指纹。"""
    noisy = _BASE.replace("战队: KC VS DYG", "战队:  kc  VS  dyg ").replace(
        "规则: 2/3【KOF】", "规则: 2/3【kof】"
    )
    assert report_fingerprint(_report(noisy)) == report_fingerprint(_report())


def test_duel_order_matters():
    """对局顺序变了 → 视为不同战报。

    宁可多存一条，也不要在顺序真的变了时把用户的重提当重复拒掉。
    """
    reordered = _BASE.replace(
        "红莲 2:1 牌大\n凯撒亮 2:1 蓝大", "凯撒亮 2:1 蓝大\n红莲 2:1 牌大"
    )
    assert report_fingerprint(_report(reordered)) != report_fingerprint(_report())


def test_match_time_in_fingerprint():
    """时间不同 → 不同指纹。

    同一天同阵容会真的打多场（用户已明确这一点），时间是把它们分开的
    主要手段之一。
    """
    other_day = _BASE.replace("时间: 2026.08.01", "时间: 2026.08.05")
    assert report_fingerprint(_report(other_day)) != report_fingerprint(_report())
