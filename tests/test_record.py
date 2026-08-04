"""记录比分（/记录）逻辑单元测试。"""

from lineup import parse_score_token, record_from_info, record_result

DRAFT = """战队: KC VS DYG
时间: 2026.08.01
规则: r
地点: 123
------第一轮------
红莲 0:0 老千
凯撒亮 2:0 蓝大
悠悠球 0:0 红大
------第二轮------"""


def test_parse_score_token():
    assert parse_score_token("2:0") == (2, 0)
    assert parse_score_token("20") == (2, 0)
    assert parse_score_token("12") == (1, 2)
    assert parse_score_token("2：0") == (2, 0)
    assert parse_score_token("abc") is None


def test_record_to_last_unrecorded():
    # 红莲最后一场未记录对阵是 "红莲 0:0 老千"，填入 2:0
    r = record_result(DRAFT, "红莲", 2, 0)
    assert r.ok
    assert "红莲  2:0  老千" in r.new_text
    assert "红莲 0:0 老千" not in r.new_text


def test_record_player_on_right_side():
    # 红大在右侧，红大 2:0 → "悠悠球  0:2  红大"
    r = record_result(DRAFT, "红大", 2, 0)
    assert r.ok
    assert "悠悠球  0:2  红大" in r.new_text


def test_record_no_unrecorded_matchup_fails():
    # 蓝大已记录（凯撒亮 2:0 蓝大），没有未记录对阵
    r = record_result(DRAFT, "蓝大", 2, 0)
    assert not r.ok
    assert any("待记录" in e for e in r.errors)


def test_record_with_opponent_uses_existing():
    # 情形2：红莲有未记录对阵 → 直接填入，忽略 opponent 黄大
    r = record_result(DRAFT, "红莲", 2, 0, opponent="黄大")
    assert r.ok
    assert "红莲  2:0  老千" in r.new_text
    assert "黄大" not in r.new_text


def test_record_insert_new_into_latest_round():
    # 情形2：新选手无未记录对阵 → 插入最新轮次（第二轮）
    r = record_result(DRAFT, "新选手", 2, 0, opponent="黄大")
    assert r.ok
    assert "新选手  2:0  黄大" in r.new_text
    # 应位于第二轮段
    lines = r.new_text.splitlines()
    second_idx = next(i for i, ln in enumerate(lines) if "第二轮" in ln)
    assert "新选手  2:0  黄大" in lines[second_idx:]


def test_record_from_info_compact():
    r = record_from_info(DRAFT, "红莲 20")
    assert r.ok
    assert "红莲  2:0  老千" in r.new_text


def test_record_from_info_full():
    r = record_from_info(DRAFT, "新选手 20 黄大")
    assert r.ok
    assert "新选手  2:0  黄大" in r.new_text


def test_record_from_info_bad_format():
    r = record_from_info(DRAFT, "红莲 2:0 蓝大 额外")
    assert not r.ok


def test_record_multiple_lines():
    # 逐行处理：有效行应用，无效行跳过，其余行继续
    current = DRAFT
    applied = []
    errors = []
    for info in ["红莲 20", "蓝大 20", "悠悠球 12 黄大"]:
        r = record_from_info(current, info)
        if r.ok:
            applied.extend(r.added_lines)
            current = r.new_text
        else:
            errors.extend(r.errors)
    assert "红莲  2:0  老千" in current  # 有效
    assert "悠悠球  1:2  红大" in current  # 有效（填入悠悠球已有未记录对阵）
    assert any("待记录" in e for e in errors)  # 蓝大 已记录 → 失败


TEAM_DRAFT = """战队: TEST1 VS TEST
时间: 2026.08.02
规则: 2/3【KOF】
地点: 123
------第一轮------
YE 2:0 红大
悠悠球 1:2 空枭
随便 0:0 黄大
------第二轮------"""


def test_record_align_team_order_insert():
    # 空枭无未记录对阵 → 插入，但按队伍对齐为 YE 1:2 空枭（空枭属 TEST/右，YE 属 TEST1/左）
    r = record_from_info(TEAM_DRAFT, "空枭 21 YE")
    assert r.ok
    assert "YE  1:2  空枭" in r.new_text
    assert "空枭  2:1  YE" not in r.new_text
