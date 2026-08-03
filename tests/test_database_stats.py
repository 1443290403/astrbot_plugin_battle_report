"""数据库聚合查询测试（连接本机 MySQL 测试库，结束时清理）。

若本机 MySQL 不可用则跳过整个模块。使用独立测试库 astrbot_battle_report_test，
不会影响正式库数据。每个测试在同一个事件循环内完成 初始化→操作→清理。
"""

import asyncio

import pytest

from battle_report_parser import determine_match_winner, parse_battle_report, split_reports
from database import Database
from conftest import DEFAULTS

TEST_DB = "astrbot_battle_report_test"
GROUP_ID = "435823386"

SAMPLE = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 435823386
------第一轮------
红莲 2:1 牌大
凯撒亮 2:1 蓝大
悠悠球 1:2 老千
------第二轮------
凯撒亮 1:2 老千
红莲 1:2 老千"""


def _conn_params() -> dict:
    return {
        "host": DEFAULTS["mysql_host"],
        "port": int(DEFAULTS["mysql_port"]),
        "user": DEFAULTS["mysql_user"],
        "password": DEFAULTS["mysql_password"],
        "db": TEST_DB,
    }


def _make_report():
    r = parse_battle_report(SAMPLE)
    assert not r.errors
    r.report.group_id = GROUP_ID
    r.report.submitted_by = "10001"
    r.report.submitted_name = "提交者"
    r.report.created_at = 1700000000
    return r.report


async def _drop_test_db():
    import aiomysql

    conn = await aiomysql.connect(
        host=DEFAULTS["mysql_host"],
        port=int(DEFAULTS["mysql_port"]),
        user=DEFAULTS["mysql_user"],
        password=DEFAULTS["mysql_password"],
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
    finally:
        conn.close()


async def _mysql_reachable() -> bool:
    import aiomysql

    try:
        conn = await aiomysql.connect(
            host=DEFAULTS["mysql_host"],
            port=int(DEFAULTS["mysql_port"]),
            user=DEFAULTS["mysql_user"],
            password=DEFAULTS["mysql_password"],
            connect_timeout=3,
        )
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


if not asyncio.run(_mysql_reachable()):
    pytest.skip("本机 MySQL 不可用，跳过数据库测试", allow_module_level=True)


def _with_db(coro_factory):
    """在单个事件循环内 初始化测试库 → 执行操作 → 清理测试库。"""
    async def run():
        db = Database(**_conn_params())
        await db.initialize()
        try:
            result = await coro_factory(db)
        finally:
            await _drop_test_db()
            await db.close()
        return result

    return asyncio.run(run())


def test_player_ranking():
    async def ops(db):
        mid = await db.insert_report(_make_report())
        assert mid > 0
        pl = await db.get_player_ranking(GROUP_ID)
        assert pl[0]["player"] == "老千"
        assert pl[0]["wins"] == 3
        assert pl[0]["losses"] == 0
        # 积分 = 胜场 × 胜率(小数) = 3 × 1.0 = 3.0
        assert pl[0]["points"] == 3.0
        honglian = next(r for r in pl if r["player"] == "红莲")
        assert honglian["wins"] == 1 and honglian["losses"] == 1

    _with_db(ops)


def test_player_ranking_team_filter():
    async def ops(db):
        await db.insert_report(_make_report())
        # 只统计 DYG 选手
        pl = await db.get_player_ranking(GROUP_ID, team="DYG")
        assert pl[0]["player"] == "老千"
        assert {r["player"] for r in pl} == {"老千", "牌大", "蓝大"}
        # 只统计 KC 选手
        pl_kc = await db.get_player_ranking(GROUP_ID, team="KC")
        assert {r["player"] for r in pl_kc} == {"红莲", "凯撒亮", "悠悠球"}
        # limit=None 返回主体战队全部选手（不受排名条数限制）
        pl_all = await db.get_player_ranking(GROUP_ID, team="KC", limit=None)
        assert {r["player"] for r in pl_all} == {"红莲", "凯撒亮", "悠悠球"}

    _with_db(ops)


def test_team_ranking():
    async def ops(db):
        await db.insert_report(_make_report())
        team = await db.get_team_ranking(GROUP_ID)
        assert team[0]["team"] == "DYG"
        assert team[0]["wins"] == 1
        kc = next(r for r in team if r["team"] == "KC")
        assert kc["losses"] == 1

    _with_db(ops)


def test_player_record_and_trend():
    async def ops(db):
        await db.insert_report(_make_report())
        rec = await db.get_player_record(GROUP_ID, "老千")
        assert rec["wins"] == 3 and rec["losses"] == 0

        trend = await db.get_player_trend(GROUP_ID, "老千")
        assert trend and trend[0][1] == 3  # (date, wins, losses)
        assert trend[0][2] == 0

    _with_db(ops)


def test_export_rows():
    async def ops(db):
        await db.insert_report(_make_report())
        rows = await db.get_export_rows(GROUP_ID)
        assert len(rows) == 5
        assert rows[0]["team_a"] == "KC"

    _with_db(ops)


def test_delete_and_undo():
    async def ops(db):
        mid = await db.insert_report(_make_report())
        last = await db.get_last_match_by_submitter(GROUP_ID, "10001")
        assert last == mid
        ok = await db.delete_match(GROUP_ID, mid)
        assert ok
        assert not await db.get_export_rows(GROUP_ID)
        # 跨群保护：其他群删不掉
        await db.insert_report(_make_report())
        assert not await db.delete_match("999999999", mid)

    _with_db(ops)


def test_teams_replace():
    async def ops(db):
        await db.replace_teams(GROUP_ID, [("KC", ["红莲", "悠悠球"]), ("DYG", ["老千"])])
        teams = await db.get_teams(GROUP_ID)
        assert len(teams) == 3
        # 覆盖写入
        await db.replace_teams(GROUP_ID, [("KC", ["红莲"])])
        teams = await db.get_teams(GROUP_ID)
        assert len(teams) == 1

    _with_db(ops)


def test_batch_reports_db_correct():
    """批量提交两份战报（队伍顺序相反），数据库数据必须各自正确。"""
    text = """战队: KC VS DYG
时间: 2026.08.03
规则: 2/3【KOF】
地点: 1060889761
------第一轮------
红莲 2:0 黄大
战神 2:1 自大
TSUKI 2:1 宏大
------第二轮------

战队: DYG VS KC
时间: 2026.08.03
规则: 2/3【KOF】
地点: 1060889761
------第一轮------
红莲 2:0 黄大
战神 2:1 自大
TSUKI 2:1 宏大
------第二轮------"""

    async def ops(db):
        chunks = split_reports(text)
        assert len(chunks) == 2
        for c in chunks:
            r = parse_battle_report(c)
            assert not r.errors, r.errors
            rep = r.report
            rep.group_id = GROUP_ID
            rep.submitted_by = "batch"
            rep.submitted_name = "batch"
            rep.created_at = 0
            winner = determine_match_winner(rep)
            await db.insert_report(rep, winner)

        rows = await db._query(
            "SELECT id, team_a, team_b, winner FROM matches WHERE group_id=%s ORDER BY id",
            (GROUP_ID,),
        )
        assert len(rows) == 2
        assert rows[0]["team_a"] == "KC" and rows[0]["winner"] == "KC"
        assert rows[1]["team_a"] == "DYG" and rows[1]["winner"] == "DYG"

        # 两场比赛的左侧选手队伍归属必须正确
        d1 = await db._query(
            "SELECT player_a, player_a_team FROM duels WHERE match_id=%s LIMIT 1",
            (rows[0]["id"],),
        )
        d2 = await db._query(
            "SELECT player_a, player_a_team FROM duels WHERE match_id=%s LIMIT 1",
            (rows[1]["id"],),
        )
        assert d1[0]["player_a"] == "红莲" and d1[0]["player_a_team"] == "KC"
        assert d2[0]["player_a"] == "红莲" and d2[0]["player_a_team"] == "DYG"

    _with_db(ops)
