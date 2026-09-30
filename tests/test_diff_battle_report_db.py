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


def _db_duels(rec: dict, *, pa_team: str | None = None, pb_team: str | None = None) -> list[dict]:
    return [
        {
            "match_id": 0, "round_no": d["round_no"],
            "player_a": d["player_a"], "score_a": d["score_a"],
            "player_b": d["player_b"], "score_b": d["score_b"],
            "player_a_team": pa_team or rec["team_a"],
            "player_b_team": pb_team or rec["team_b"], "result": "A",
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
    assert not res["winner_mismatch"] and not res["duel_mismatch"]


def test_missing_in_db():
    rec = _site_record()
    res = differ.compare([rec], [], {})
    assert [r["id"] for r in res["missing_in_db"]] == ["157101"]
    assert res["matched"] == 0


def test_only_in_db():
    rec = _site_record()
    res = differ.compare([], [_db_match(9, rec)], {9: _db_duels(rec)})
    assert [m["id"] for m in res["only_in_db"]] == [9]


def test_winner_mismatch():
    rec = _site_record()
    res = differ.compare([rec], [_db_match(2, rec, winner="FA")], {2: _db_duels(rec)})
    assert len(res["winner_mismatch"]) == 1
    _r, m, w = res["winner_mismatch"][0]
    assert m["id"] == 2 and w == "FA"


def test_winner_mismatch_uses_recompute_when_db_winner_blank():
    rec = _site_record()
    res = differ.compare([rec], [_db_match(3, rec, winner="")], {3: _db_duels(rec)})
    assert not res["winner_mismatch"]  # 复算出 KC，与网站一致


def test_duel_mismatch():
    rec = _site_record()
    duels = _db_duels(rec)
    duels[0]["score_a"], duels[0]["score_b"] = 0, 2
    res = differ.compare([rec], [_db_match(4, rec)], {4: duels})
    assert len(res["duel_mismatch"]) == 1


def test_zero_zero_placeholder_ignored():
    body = BODY + "\n沐晨  0:0  绯洛"
    rec = _site_record(body, rid="157200")
    assert any(d["score_a"] == 0 and d["score_b"] == 0 for d in rec["duels"])
    res = differ.compare([rec], [_db_match(5, rec)], {5: _db_duels(rec)})
    assert res["matched"] == 1
    assert not res["duel_mismatch"]


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
    assert not res["winner_mismatch"] and not res["duel_mismatch"]


def test_multi_match_in_db_flags_double_counting():
    rec = _site_record()
    res = differ.compare([rec], [_db_match(7, rec), _db_match(8, rec)],
                         {7: _db_duels(rec), 8: _db_duels(rec)})
    assert res["matched"] == 1
    assert len(res["multi_match_in_db"]) == 1
    assert res["multi_match_in_db"][0][1] == [7, 8]
    assert [m["id"] for m in res["only_in_db"]] == [8]


def test_out_of_range_excluded_from_comparison():
    rec = _site_record(rid="156355")
    rec["match_time_in_range"] = False
    res = differ.compare([rec], [], {})
    assert res["missing_in_db"] == []
    assert [r["id"] for r in res["site_out_of_range"]] == ["156355"]
