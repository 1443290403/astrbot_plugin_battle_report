"""战报比对脚本单元测试（纯逻辑，不连库、不联网）。"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import crawl_battle_reports as crawler  # noqa: E402
import diff_battle_report_db as differ  # noqa: E402

# 对局序列使 team_b(=KC) 获胜：FA 两名选手最后一场均负，KC 未全员败北
BODY = """战队: FA VS KC
时间: 2026.09.30
规则: 2/3【KOF】
地点: 801965989
------第一轮------
地  2:1  间桐樱
黑花花  1:2  浸泡
------第二轮------
地  1:2  绯洛
黑花花  1:2  绯洛"""

# 同一时间/战队/地点，但阵容完全不同（模拟同一天同一房间的另一场比赛）
BODY_OTHER = """战队: FA VS KC
时间: 2026.09.30
规则: 2/3【KOF】
地点: 801965989
------第一轮------
甲  2:0  丙
乙  2:0  丁"""


def _site_record(body: str = BODY, rid: str = "157101") -> dict:
    raw = {
        "id": rid, "winner_site": "KC", "loser_site": "FA",
        "publish_date": "2026-09-30", "publish_date_raw": "26-09-30",
        "publisher": "RLX", "body": body,
    }
    return crawler.enrich(raw, "2026-09-01", "2026-09-30")


def _db_match(mid: int, rec: dict, winner: str | None = None) -> dict:
    return {
        "id": mid, "group_id": "G1", "home_team": "KC",
        "team_a": rec["team_a"], "team_b": rec["team_b"],
        "match_time": rec["match_time"], "rule": rec["rule"], "location": rec["location"],
        "winner": rec["winner_site"] if winner is None else winner,
        "submitted_by": "", "submitted_name": "", "created_at": 0,
    }


def _db_duels(rec: dict) -> list[dict]:
    return [
        {
            "match_id": 0, "round_no": d["round_no"],
            "player_a": d["player_a"], "score_a": d["score_a"],
            "player_b": d["player_b"], "score_b": d["score_b"],
            "player_a_team": rec["team_a"], "player_b_team": rec["team_b"], "result": "A",
        }
        for d in rec["duels"]
    ]


def test_fixture_winner_is_kc():
    rec = _site_record()
    assert rec["parse_ok"]
    assert rec["winner_site"] == "KC"
    assert rec["winner_computed"] == "KC"   # 夹具自洽，后面才不会被胜负差异污染


def test_all_matched_no_diffs():
    rec = _site_record()
    res = differ.compare([rec], [_db_match(1, rec)], {1: _db_duels(rec)})
    assert res["matched"] == 1
    assert not res["missing_in_db"] and not res["only_in_db"]
    assert not res["winner_mismatch"] and not res["db_duplicate_keys"]


def test_missing_in_db():
    rec = _site_record()
    res = differ.compare([rec], [], {})
    assert [r["id"] for r in res["missing_in_db"]] == ["157101"]
    assert res["matched"] == 0


def test_only_in_db():
    rec = _site_record()
    res = differ.compare([], [_db_match(9, rec)], {9: _db_duels(rec)})
    assert [m["id"] for m in res["only_in_db"]] == [9]


def test_db_duplicate_same_content():
    rec = _site_record()
    res = differ.compare([rec], [_db_match(7, rec), _db_match(8, rec)],
                         {7: _db_duels(rec), 8: _db_duels(rec)})
    assert len(res["db_duplicate_keys"]) == 1
    assert [m["id"] for m in list(res["db_duplicate_keys"].values())[0]] == [7, 8]
    # 库中两条都对应上网站这一条，不产生"只在库里有"
    assert res["matched"] == 1
    assert res["only_in_db"] == []


def test_site_duplicate_same_content():
    a, b = _site_record(rid="157101"), _site_record(rid="157102")
    res = differ.compare([a, b], [_db_match(1, a)], {1: _db_duels(a)})
    assert len(res["site_duplicate_keys"]) == 1
    assert res["missing_in_db"] == []  # 库里有这一份，不算缺失


def test_different_duels_same_session_do_not_match():
    """回归：地点是固定房间号，同一天同一房间有多场不同比赛，不能当成同场重复。"""
    site = _site_record()
    other = _site_record(BODY_OTHER, rid="157999")
    res = differ.compare([site], [_db_match(1, other)], {1: _db_duels(other)})
    assert res["matched"] == 0
    assert [r["id"] for r in res["missing_in_db"]] == ["157101"]
    assert [m["id"] for m in res["only_in_db"]] == [1]
    assert res["db_duplicate_keys"] == {}


def test_same_session_more_rows_than_site_is_flagged():
    """网站那天那个房间只打了一场，库里却有两版 → 多出来的是重复计数。"""
    a = _site_record(rid="157101")                 # 网站判 KC 胜
    b = _site_record(BODY_OTHER, rid="157999")     # 同日期/战队/地点，内容不同
    res = differ.compare(
        [a],  # 网站只有一份
        [_db_match(11, a, winner="KC"), _db_match(12, b, winner="FA")],
        {11: _db_duels(a), 12: _db_duels(b)},
    )
    assert len(res["db_session_groups"]) == 1
    g = res["db_session_groups"][0]
    assert g["site_count"] == 1
    assert g["db_exceeds_site"] is True
    assert g["winner_conflict"] is True
    assert [m["db_id"] for m in g["matches"]] == [11, 12]
    assert res["db_duplicate_keys"] == {}  # 内容不同，不算"同内容重复"


def test_same_session_count_matches_site_not_flagged():
    """网站同场次也有两份（双方各报一版都被收录）→ 不是重复，不该动。"""
    a = _site_record(rid="157101")
    b = _site_record(BODY_OTHER, rid="157999")
    res = differ.compare(
        [a, b],
        [_db_match(11, a, winner="KC"), _db_match(12, b, winner="FA")],
        {11: _db_duels(a), 12: _db_duels(b)},
    )
    assert len(res["db_session_groups"]) == 1
    g = res["db_session_groups"][0]
    assert g["site_count"] == 2
    assert g["db_exceeds_site"] is False
    assert res["missing_in_db"] == []


def test_raid_rows_excluded_by_default():
    """网站只收录友谊赛，踢馆报必须排除，否则全落进「只在库里有」。"""
    rows = [
        {"id": 1, "kind": differ.KIND_FRIENDLY},
        {"id": 2, "kind": "raid"},
        {"id": 3, "kind": "raid"},
        {"id": 4, "kind": None},        # 历史行：建表默认 friendly
        {"id": 5},                      # 老 schema：无 kind 列
    ]
    kept, excluded = differ.split_by_kind(rows)
    assert [m["id"] for m in kept] == [1, 4, 5]
    assert excluded == {"raid": 2}
    assert kept[0]["kind"] == differ.KIND_FRIENDLY  # 空的补成 friendly


def test_raid_rows_included_on_demand():
    rows = [{"id": 1, "kind": differ.KIND_FRIENDLY}, {"id": 2, "kind": "raid"}]
    kept, excluded = differ.split_by_kind(rows, include_raid=True)
    assert [m["id"] for m in kept] == [1, 2]
    assert excluded == {}


def test_winner_mismatch():
    rec = _site_record()
    res = differ.compare([rec], [_db_match(2, rec, winner="FA")], {2: _db_duels(rec)})
    assert len(res["winner_mismatch"]) == 1
    _r, m, w = res["winner_mismatch"][0]
    assert m["id"] == 2 and w == "FA"


def test_winner_recomputed_when_db_winner_blank():
    rec = _site_record()
    res = differ.compare([rec], [_db_match(3, rec, winner="")], {3: _db_duels(rec)})
    assert not res["winner_mismatch"]  # 复算出 KC，与网站一致


def test_zero_zero_placeholder_ignored():
    body = BODY + "\n沐晨  0:0  绯洛"
    rec = _site_record(body, rid="157200")
    assert any(d["score_a"] == 0 and d["score_b"] == 0 for d in rec["duels"])
    res = differ.compare([rec], [_db_match(5, rec)], {5: _db_duels(rec)})
    assert res["matched"] == 1


def test_team_order_flipped_in_db_still_matches():
    rec = _site_record()
    flipped = dict(_db_match(6, rec, winner="KC"), team_a=rec["team_b"], team_b=rec["team_a"])
    duels = _db_duels(rec)
    for d in duels:  # 库内两侧对调
        d["player_a"], d["player_b"] = d["player_b"], d["player_a"]
        d["score_a"], d["score_b"] = d["score_b"], d["score_a"]
        d["player_a_team"], d["player_b_team"] = d["player_b_team"], d["player_a_team"]
    res = differ.compare([rec], [flipped], {6: duels})
    assert res["matched"] == 1
    assert not res["winner_mismatch"]


def test_out_of_range_excluded_from_comparison():
    rec = _site_record(rid="156355")
    rec["match_time_in_range"] = False
    res = differ.compare([rec], [], {})
    assert res["missing_in_db"] == []
    assert [r["id"] for r in res["site_out_of_range"]] == ["156355"]
