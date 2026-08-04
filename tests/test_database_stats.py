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


def test_group_home_and_home_team_filter():
    async def ops(db):
        # 绑定主体
        await db.set_group_home("G1", "KC")
        assert await db.get_group_home("G1") == "KC"
        assert await db.get_group_home("G2") is None

        # 以主体 KC 上传战报
        rep = _make_report()
        rep.group_id = "G1"
        rep.submitted_by = "x"
        rep.submitted_name = "y"
        rep.created_at = 0
        await db.insert_report(rep, "DYG", home_team="KC")

        rows = await db._query("SELECT home_team FROM matches WHERE group_id='G1'")
        assert rows[0]["home_team"] == "KC"

        # 旧数据（空 home_team）绑定后回填
        rep2 = _make_report()
        rep2.group_id = "G1"
        rep2.submitted_by = "x"
        rep2.submitted_name = "y"
        rep2.created_at = 0
        await db.insert_report(rep2, "DYG", home_team="")
        await db.backfill_group_home("G1", "KC")
        rows = await db._query(
            "SELECT home_team FROM matches WHERE group_id='G1' ORDER BY id"
        )
        assert rows[0]["home_team"] == "KC"
        assert rows[1]["home_team"] == "KC"

        # 分析按 home_team 过滤
        pl = await db.get_player_ranking("G1", team="DYG", home_team="KC")
        assert pl and pl[0]["player"] == "老千"
        pl_other = await db.get_player_ranking("G1", team="DYG", home_team="OTHER")
        assert not pl_other

    _with_db(ops)


def test_user_and_player_ids():
    async def ops(db):
        # 上传一份 TEST1 视角的战报
        r = parse_battle_report(
            "战队: TEST1 VS DYG\n时间: 2026.08.01\n规则: 2/3【KOF】\n地点: 1\n"
            "------第一轮------\n红莲 2:1 老千"
        )
        rep = r.report
        rep.group_id = GROUP_ID
        rep.submitted_by = "x"
        rep.submitted_name = "y"
        rep.created_at = 0
        await db.insert_report(rep, "TEST1", home_team="TEST1")

        # 参赛ID池：仅从战报提取 TEST1 选手
        pool = await db.get_player_pool("TEST1")
        assert "红莲" in pool and "老千" not in pool

        # 创建用户并绑定参赛ID
        uid = await db.find_or_create_user("TEST1", "红莲", "10001")
        await db.bind_player_to_user("TEST1", "红莲", uid)
        binding = await db.get_player_binding("TEST1", "红莲")
        assert binding["user_name"] == "红莲"

        # 认领冲突与成功
        status, _ = await db.claim_user_by_name("TEST1", "红莲", "20002")
        assert status == "claimed_else"
        status, _ = await db.claim_user_by_name("TEST1", "红莲", "10001")
        assert status == "ok"

        # 一个QQ绑定多个ID → 复用同一角色（用户=角色，多ID挂其下）
        uid2 = await db.find_or_create_user("TEST1", "别的名字", "10001")
        assert uid2 == uid  # 同QQ复用角色
        await db.bind_player_to_user("TEST1", "红莲", uid)
        await db.bind_player_to_user("TEST1", "凯撒亮", uid)
        assert set(await db.get_user_players("TEST1", uid)) == {"红莲", "凯撒亮"}

        # 用户参赛ID + 合并战绩（跨群）
        agg = await db.get_players_aggregate(None, ["红莲"], home_team="TEST1")
        assert agg["wins"] == 1 and agg["losses"] == 0

    _with_db(ops)


def test_player_record_excludes_same_name_other_team():
    async def ops(db):
        # KC 上传 vs RF：红莲是 RF 对手（player_b_team=RF），不应计入 KC 红莲
        r1 = parse_battle_report(
            "战队: KC VS RF\n时间: 2026.08.01\n规则: 2/3【KOF】\n地点: 1\n"
            "------第一轮------\n凯撒亮 2:1 红莲"
        )
        rep1 = r1.report
        rep1.group_id = GROUP_ID
        rep1.submitted_by = "x"
        rep1.submitted_name = "y"
        rep1.created_at = 0
        await db.insert_report(rep1, "KC", home_team="KC")
        # KC 上传 vs DYG：红莲是己方（player_a_team=KC）
        r2 = parse_battle_report(
            "战队: KC VS DYG\n时间: 2026.08.01\n规则: 2/3【KOF】\n地点: 1\n"
            "------第一轮------\n红莲 2:1 老千"
        )
        rep2 = r2.report
        rep2.group_id = GROUP_ID
        rep2.submitted_by = "x"
        rep2.submitted_name = "y"
        rep2.created_at = 0
        await db.insert_report(rep2, "KC", home_team="KC")

        rec = await db.get_player_record(None, "红莲", home_team="KC")
        # 只算 KC 的红莲（1胜），不含 RF 的同名红莲
        assert rec["wins"] == 1 and rec["losses"] == 0

    _with_db(ops)


def test_rename_user():
    async def ops(db):
        uid = await db.find_or_create_user("KC", "红莲", "10001")
        await db.find_or_create_user("KC", "老千", "20002")
        # 改名成功
        assert await db.rename_user("KC", uid, "红莲2") == "ok"
        # 名字冲突（同战队已有）
        assert await db.rename_user("KC", uid, "老千") == "conflict"
        user = await db.get_user_by_qq("KC", "10001")
        assert user["name"] == "红莲2"

    _with_db(ops)


def test_group_ban():
    async def ops(db):
        assert await db.get_group_ban("G1") is False
        await db.set_group_ban("G1", True)
        assert await db.get_group_ban("G1") is True
        await db.set_group_ban("G1", False)
        assert await db.get_group_ban("G1") is False

    _with_db(ops)


def test_get_all_teams_and_groups():
    async def ops(db):
        await db.set_group_home("G1", "KC")
        await db.set_group_home("G2", "DYG")
        await db.set_group_ban("G3", True)  # 禁用但未绑定
        teams = await db.get_all_teams()
        assert "KC" in teams and "DYG" in teams
        groups = await db.get_all_groups()
        by_id = {g["group_id"]: g for g in groups}
        assert by_id["G1"]["home_team"] == "KC" and by_id["G1"]["banned"] == 0
        assert by_id["G2"]["home_team"] == "DYG"
        assert by_id["G3"]["banned"] == 1  # 禁用群也列出
        # 按战队过滤
        kc = await db.get_all_groups("KC")
        assert [g["group_id"] for g in kc] == ["G1"]

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
