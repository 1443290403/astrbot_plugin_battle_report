"""数据库聚合查询测试（连接本机 MySQL 测试库，结束时清理）。

若本机 MySQL 不可用则跳过整个模块。使用独立测试库 astrbot_battle_report_test，
不会影响正式库数据。每个测试在同一个事件循环内完成 初始化→操作→清理。
"""

import asyncio

import pytest

from battle_report_parser import (
    determine_match_winner,
    parse_battle_report,
    parse_raid_report,
    split_reports,
)
from database import Database, DuplicateReportError
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


TEAM = "KC"


async def _ins(db, rep=None, winner="", **kw):
    """插入一份默认归属 TEAM 的战报。"""
    rep = rep or _make_report()
    kw.setdefault("home_team", TEAM)
    return await db.insert_report(rep, winner=winner, **kw)



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
    """在单个事件循环内 初始化测试库 → 执行操作 → 清理测试库（每次先清库避免污染）。"""
    async def run():
        await _drop_test_db()
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
        mid = await _ins(db)
        assert mid > 0
        pl = await db.get_player_ranking(TEAM)
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
        await _ins(db)
        # 只统计 DYG 选手
        pl = await db.get_player_ranking(TEAM, team="DYG")
        assert pl[0]["player"] == "老千"
        assert {r["player"] for r in pl} == {"老千", "牌大", "蓝大"}
        # 只统计 KC 选手
        pl_kc = await db.get_player_ranking(TEAM, team="KC")
        assert {r["player"] for r in pl_kc} == {"红莲", "凯撒亮", "悠悠球"}
        # limit=None 返回主体战队全部选手（不受排名条数限制）
        pl_all = await db.get_player_ranking(TEAM, team="KC", limit=None)
        assert {r["player"] for r in pl_all} == {"红莲", "凯撒亮", "悠悠球"}

    _with_db(ops)


def test_player_ranking_role_aggregation():
    async def ops(db):
        await _ins(db)
        # 把红莲、凯撒亮绑定到同一角色 小明 → 排行合并为角色名
        uid = await db.find_or_create_user("KC", "小明")
        await db.bind_player_to_user("KC", "红莲", uid)
        await db.bind_player_to_user("KC", "凯撒亮", uid)
        pl = await db.get_player_ranking(TEAM, team="KC", limit=None)
        names = {r["player"] for r in pl}
        assert "小明" in names
        assert "红莲" not in names and "凯撒亮" not in names
        ming = next(r for r in pl if r["player"] == "小明")
        assert ming["wins"] == 2 and ming["losses"] == 2 and ming["total"] == 4

    _with_db(ops)


def test_player_ranking_wushuang():
    async def ops(db):
        # SAMPLE：老千是 DYG 唯一幸存者，击败 KC 全部 3 人
        await _ins(db, winner="DYG")
        pl = await db.get_player_ranking(TEAM, team=None, limit=None)
        laoqian = next(r for r in pl if r["player"] == "老千")
        assert laoqian["wushuang"] == 1
        assert laoqian["friendship"] == 1
        # 其余人无双=0；每人参与 1 场比赛
        for r in pl:
            if r["player"] != "老千":
                assert r["wushuang"] == 0, r["player"]
            assert r["friendship"] == 1, r["player"]

    _with_db(ops)


def test_player_match_stats_aggregation():
    async def ops(db):
        await _ins(db, winner="DYG")                 # 8月1日
        rep2 = _make_report()
        rep2.match_time = "2026-08-05"
        await _ins(db, rep2, winner="DYG")           # 8月5日 同阵容
        st = await db.get_player_match_stats(TEAM)
        assert st["老千"]["friendship"] == 2
        assert st["老千"]["wushuang"] == 2
        assert st["红莲"]["friendship"] == 2
        assert st["红莲"]["wushuang"] == 0

    _with_db(ops)


def test_player_match_stats_team_filter_cross_team_same_name():
    """跨队同名碰撞：本队与对方战队各有同名玩家，team 过滤只统计本队一侧出场。"""

    async def ops(db):
        rep1 = parse_battle_report(
            "战队: KC VS FH\n时间: 2026.08.01\n规则: 2/3【KOF】\n地点: 1\n"
            "------第一轮------\n知更 2:1 甲"
        ).report
        rep1.group_id = GROUP_ID
        rep1.submitted_by = "10001"
        rep1.submitted_name = "提交者"
        rep1.created_at = 1700000000
        await _ins(db, rep1, winner="KC")  # 知更 在 KC 侧出场

        rep2 = parse_battle_report(
            "战队: KC VS FH\n时间: 2026.08.02\n规则: 2/3【KOF】\n地点: 1\n"
            "------第一轮------\n别天 2:1 知更"
        ).report
        rep2.group_id = GROUP_ID
        rep2.submitted_by = "10001"
        rep2.submitted_name = "提交者"
        rep2.created_at = 1700000000
        await _ins(db, rep2, winner="KC")  # FH 侧也有同名 知更

        # 不指定 team：跨队同名合并，友谊 2 场
        st_all = await db.get_player_match_stats(TEAM)
        assert st_all["知更"]["friendship"] == 2
        # 指定 team=KC：只统计本队一侧出场，友谊 1 场
        st_kc = await db.get_player_match_stats(TEAM, team="KC")
        assert st_kc["知更"]["friendship"] == 1
        assert st_kc["别天"]["friendship"] == 1

        # 排行 total 列与友谊次数口径一致（都只计 KC 一侧）
        pl = await db.get_player_ranking(TEAM, team="KC", limit=None)
        zg = next(r for r in pl if r["player"] == "知更")
        assert zg["total"] == 1 and zg["wins"] == 1 and zg["friendship"] == 1

    _with_db(ops)


def test_resolve_role():
    async def ops(db):
        await _ins(db)
        # 未绑定 → None
        assert await db.resolve_role("KC", "红莲") is None
        uid = await db.find_or_create_user("KC", "小明")
        await db.bind_player_to_user("KC", "红莲", uid)
        await db.bind_player_to_user("KC", "凯撒亮", uid)
        # 按参赛ID解析
        r1 = await db.resolve_role("KC", "红莲")
        assert r1 and r1["user_name"] == "小明"
        assert set(r1["players"]) == {"红莲", "凯撒亮"}
        # 按角色名解析
        r2 = await db.resolve_role("KC", "小明")
        assert r2 and r2["user_name"] == "小明"
        assert set(r2["players"]) == {"红莲", "凯撒亮"}

    _with_db(ops)


def test_get_players_trend():
    async def ops(db):
        await _ins(db)
        # 红莲、凯撒亮同一天各 1胜1负 → 合并 2胜2负
        pts = await db.get_players_trend(TEAM, ["红莲", "凯撒亮"], None)
        assert len(pts) == 1
        _, w, l = pts[0]
        assert w == 2 and l == 2
        # 空列表
        assert await db.get_players_trend(TEAM, [], None) == []

    _with_db(ops)


def test_unbind_player():
    async def ops(db):
        await _ins(db)
        uid = await db.find_or_create_user("KC", "小明")
        await db.bind_player_to_user("KC", "红莲", uid)
        assert (await db.get_player_binding("KC", "红莲"))["user_id"] == uid
        # 解除绑定
        await db.unbind_player("KC", "红莲")
        assert (await db.get_player_binding("KC", "红莲"))["user_id"] is None
        # 未绑定 ID 无影响
        await db.unbind_player("KC", "不存在的选手")

    _with_db(ops)


def test_home_team_vs_opponents():
    async def ops(db):
        # KC 对 DYG 一胜
        await _ins(db, winner="KC")
        # KC 对 FH 一负
        rep2 = _make_report()
        rep2.team_b = "FH"
        await _ins(db, rep2, winner="FH")
        # KC 对 FH 一胜
        rep3 = _make_report()
        rep3.team_b = "FH"
        # 必须换比赛时间：指纹只认战报内容（不含群号/提交人），
        # 内容一字不差会被唯一键当重复战报拒收
        rep3.match_time = "2026-08-05"
        await _ins(db, rep3, winner="KC")

        rows = await db.get_home_team_vs_opponents("KC")
        by_opp = {r["opponent"]: r for r in rows}
        assert by_opp["DYG"]["wins"] == 1 and by_opp["DYG"]["losses"] == 0
        assert by_opp["DYG"]["total"] == 1 and by_opp["DYG"]["win_rate"] == 100.0
        assert by_opp["FH"]["wins"] == 1 and by_opp["FH"]["losses"] == 1
        assert by_opp["FH"]["total"] == 2 and by_opp["FH"]["win_rate"] == 50.0

    _with_db(ops)


def test_home_team_record():
    async def ops(db):
        await _ins(db, winner="KC")        # 一胜
        rep2 = _make_report()
        rep2.team_b = "FH"
        await _ins(db, rep2, winner="FH")  # 一负
        rep3 = _make_report()
        rep3.team_b = "FH"
        # 必须换比赛时间：指纹只认战报内容（不含群号/提交人），
        # 内容一字不差会被唯一键当重复战报拒收
        rep3.match_time = "2026-08-05"
        await _ins(db, rep3, winner="KC")  # 一胜

        rec = await db.get_home_team_record(TEAM)
        assert rec["wins"] == 2 and rec["losses"] == 1 and rec["total"] == 3
        assert rec["win_rate"] == 66.7

    _with_db(ops)


def test_date_filtered_trend_and_export():
    async def ops(db):
        # 8月战报
        await _ins(db, winner="KC")
        # 7月战报
        rep2 = _make_report()
        rep2.match_time = "2026-07-15"
        await _ins(db, rep2, winner="KC")

        # 趋势：7月区间只返回7月数据（老千7、8月都打了）
        jul = await db.get_player_trend(TEAM, "老千", "2026-07-01", "2026-07-31")
        assert [d for d, _, _ in jul] == ["2026-07-15"]
        aug = await db.get_player_trend(TEAM, "老千", "2026-08-01", "2026-08-31")
        assert [d for d, _, _ in aug] == ["2026-08-01"]

        # 导出 rows：7月区间只有7月那份的5局
        rows = await db.get_export_rows(TEAM, "2026-07-01", "2026-07-31")
        assert len(rows) == 5
        assert all(str(r["match_time"]) == "2026-07-15" for r in rows)
        # 无过滤 → 10局
        assert len(await db.get_export_rows(TEAM)) == 10

        # 合并转发导出 reports：7月只有1份
        reports = await db.get_reports_for_export(TEAM, "2026-07-01", "2026-07-31")
        assert len(reports) == 1
        assert str(reports[0]["match_time"]) == "2026-07-15"

    _with_db(ops)


def test_player_record_and_trend():
    async def ops(db):
        await _ins(db)
        # 红莲是 KC 选手：1胜1负，同一场（友谊 1）
        rec = await db.get_player_record(TEAM, "红莲")
        assert rec["wins"] == 1 and rec["losses"] == 1
        assert rec["friendship"] == 1

        trend = await db.get_player_trend(TEAM, "红莲")
        assert trend and trend[0][1] == 1  # (date, wins, losses)
        assert trend[0][2] == 1

    _with_db(ops)


def test_player_record_friendship():
    async def ops(db):
        await _ins(db, winner="DYG")                    # 8月1日：红莲 2场对局（同一场）
        rep2 = _make_report()
        rep2.match_time = "2026-08-05"
        await _ins(db, rep2, winner="DYG")              # 8月5日：红莲 2场对局（另一场）
        rec = await db.get_player_record(TEAM, "红莲")
        assert rec["wins"] == 2 and rec["losses"] == 2
        assert rec["friendship"] == 2                   # 两场都参与

        # 0:0 未完结对局不计友谊：红莲仅 0:0 的场次不计入友谊
        rep3 = parse_battle_report(
            "战队: KC VS FH\n时间: 2026.08.09\n规则: 2/3【KOF】\n地点: 1\n"
            "------第一轮------\n红莲 0:0 甲\n凯撒亮 2:1 乙"
        ).report
        rep3.group_id = GROUP_ID
        rep3.submitted_by = "10001"
        rep3.submitted_name = "提交者"
        rep3.created_at = 1700000000
        await _ins(db, rep3, winner="KC")
        rec2 = await db.get_player_record(TEAM, "红莲")
        assert rec2["friendship"] == 2                  # 0:0 场不计，仍 2 场
        assert rec2["total"] == 5                       # total 是对局数（含 0:0）

    _with_db(ops)


def test_players_aggregate_friendship_dedup():
    async def ops(db):
        # 同一用户两个参赛ID（红莲、凯撒亮）在同一场都出场 → 友谊按比赛去重
        rep1 = parse_battle_report(
            "战队: KC VS DYG\n时间: 2026.08.01\n规则: 2/3【KOF】\n地点: 1\n"
            "------第一轮------\n红莲 2:1 老千\n凯撒亮 2:1 牌大"
        ).report
        rep1.group_id = GROUP_ID
        rep1.submitted_by = "10001"
        rep1.submitted_name = "提交者"
        rep1.created_at = 1700000000
        await _ins(db, rep1, winner="KC")
        # 另一场只有红莲出场
        rep2 = parse_battle_report(
            "战队: KC VS DYG\n时间: 2026.08.05\n规则: 2/3【KOF】\n地点: 1\n"
            "------第一轮------\n红莲 2:1 牌大"
        ).report
        rep2.group_id = GROUP_ID
        rep2.submitted_by = "10001"
        rep2.submitted_name = "提交者"
        rep2.created_at = 1700000000
        await _ins(db, rep2, winner="KC")
        agg = await db.get_players_aggregate(TEAM, ["红莲", "凯撒亮"])
        assert agg["friendship"] == 2                   # 去重：两场，非 3
        assert agg["total"] == 3                        # 对局数 2+1
        agg1 = await db.get_players_aggregate(TEAM, ["红莲"])
        assert agg1["friendship"] == 2
        # 跨队同名：只计本队一侧出场
        rep3 = parse_battle_report(
            "战队: KC VS FH\n时间: 2026.08.09\n规则: 2/3【KOF】\n地点: 1\n"
            "------第一轮------\n别天 2:1 红莲"
        ).report
        rep3.group_id = GROUP_ID
        rep3.submitted_by = "10001"
        rep3.submitted_name = "提交者"
        rep3.created_at = 1700000000
        await _ins(db, rep3, winner="KC")               # 红莲 在 FH 侧出场
        agg2 = await db.get_players_aggregate(TEAM, ["红莲"])
        assert agg2["friendship"] == 2                  # FH 侧同名不计入（对局/友谊均只计本队一侧）
        assert agg2["total"] == 2

    _with_db(ops)


def test_export_rows():
    async def ops(db):
        await _ins(db)
        rows = await db.get_export_rows(TEAM)
        assert len(rows) == 5
        assert rows[0]["team_a"] == "KC"

    _with_db(ops)


def test_team_scope_cross_group():
    async def ops(db):
        # 两个不同群的 KC 战报，按战队跨群查询应都返回
        await _ins(db)  # group_id=GROUP_ID
        rep2 = _make_report()
        rep2.group_id = "OTHER_GROUP"
        # 必须换比赛时间：指纹只认战报内容（不含群号/提交人），
        # 内容一字不差会被唯一键当重复战报拒收
        rep2.match_time = "2026-08-05"
        await _ins(db, rep2)  # group_id=OTHER_GROUP

        rows = await db.get_export_rows(TEAM)
        assert len(rows) == 10  # 两份 × 5 局
        assert {r["group_id"] for r in rows} == {GROUP_ID, "OTHER_GROUP"}

        pl = await db.get_player_ranking(TEAM, None, None, 1, None, team=None)
        assert len(pl) >= 5

    _with_db(ops)


def test_delete_and_undo():
    async def ops(db):
        mid = await _ins(db)
        last = await db.get_last_match_by_submitter(GROUP_ID, "10001")
        assert last == mid
        # 插入时 duels 有 5 局
        before = await db._query(
            "SELECT COUNT(*) AS n FROM duels WHERE match_id=%s", (mid,)
        )
        assert before[0]["n"] == 5
        # 跨群保护：其他群删不掉，duels 关联数据不受影响
        assert not await db.delete_match("999999999", mid)
        still = await db._query(
            "SELECT COUNT(*) AS n FROM duels WHERE match_id=%s", (mid,)
        )
        assert still[0]["n"] == 5
        # 正常删除：matches 与其关联 duels 一并删除
        ok = await db.delete_match(GROUP_ID, mid)
        assert ok
        assert not await db.get_export_rows(TEAM)
        after = await db._query(
            "SELECT COUNT(*) AS n FROM duels WHERE match_id=%s", (mid,)
        )
        assert after[0]["n"] == 0

    _with_db(ops)


def test_insert_raw_text_and_seq():
    async def ops(db):
        mid = await _ins(db, winner="KC", raw_text="原始战报文本")
        m = await db._query("SELECT raw_text FROM matches WHERE id=%s", (mid,))
        assert m[0]["raw_text"] == "原始战报文本"
        duels = await db._query(
            "SELECT seq, round_no, player_a FROM duels WHERE match_id=%s ORDER BY seq",
            (mid,),
        )
        assert [d["seq"] for d in duels] == [0, 1, 2, 3, 4]
        assert duels[0]["player_a"] == "红莲"
        assert duels[4]["player_a"] == "红莲"  # 第二轮最后一场，顺序保持

    _with_db(ops)


def test_get_reports_for_export():
    async def ops(db):
        mid = await _ins(db, winner="KC", raw_text="第一份")
        rep2 = _make_report()
        rep2.submitted_by = "10002"
        # 必须换比赛时间：指纹只认战报内容（不含群号/提交人），
        # 内容一字不差会被唯一键当重复战报拒收
        rep2.match_time = "2026-08-05"
        mid2 = await _ins(db, rep2, winner="DYG")

        reports = await db.get_reports_for_export(TEAM)
        assert [r["match_id"] for r in reports] == [mid, mid2]
        first = reports[0]
        assert first["raw_text"] == "第一份"
        assert first["winner"] == "KC" and first["home_team"] == "KC"
        assert [d["seq"] for d in first["duels"]] == [0, 1, 2, 3, 4]
        assert [d["round_no"] for d in first["duels"]] == [1, 1, 1, 2, 2]
        assert reports[1]["raw_text"] == ""
        assert reports[1]["submitted_by"] == "10002"

    _with_db(ops)


def test_insert_ruled_flags():
    async def ops(db):
        text = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲(规则) 1:2 老千
凯撒亮 2:1 蓝大"""
        parsed = parse_battle_report(text)
        assert not parsed.errors, parsed.errors
        report = parsed.report
        report.group_id = GROUP_ID
        report.submitted_by = "10001"
        report.submitted_name = "提交者"
        report.created_at = 0
        mid = await _ins(db, report, winner="DYG")
        duels = await db._query(
            "SELECT player_a, ruled, player_b FROM duels WHERE match_id=%s ORDER BY seq",
            (mid,),
        )
        assert duels[0]["player_a"] == "红莲" and duels[0]["ruled"] == 1
        assert duels[1]["ruled"] == 0

    _with_db(ops)


def test_insert_sub_flags():
    async def ops(db):
        text = """战队: KC VS DYG
时间: 2026.08.01
规则: 2/3【KOF】
地点: 123
------第一轮------
红莲(替) 2:0 老千（替）
凯撒亮 2:1 蓝大"""
        parsed = parse_battle_report(text)
        assert not parsed.errors, parsed.errors
        report = parsed.report
        report.group_id = GROUP_ID
        report.submitted_by = "10001"
        report.submitted_name = "提交者"
        report.created_at = 0
        mid = await _ins(db, report, winner="KC")
        duels = await db._query(
            "SELECT player_a, a_sub, player_b, b_sub FROM duels WHERE match_id=%s ORDER BY seq",
            (mid,),
        )
        assert duels[0]["player_a"] == "红莲" and duels[0]["a_sub"] == 1
        assert duels[0]["player_b"] == "老千" and duels[0]["b_sub"] == 1
        assert duels[1]["player_a"] == "凯撒亮" and duels[1]["a_sub"] == 0
        assert duels[1]["b_sub"] == 0

    _with_db(ops)


def test_migration_v10_int_to_bigint():
    """模拟旧 INT schema 的库升级：v10 迁移把主键/外键/用户ID/时间戳扩为 BIGINT。"""
    async def run():
        import aiomysql

        base = dict(_conn_params())
        base.pop("db", None)
        conn = await aiomysql.connect(**base, charset="utf8mb4", autocommit=True)
        async with conn.cursor() as cur:
            await cur.execute(
                f"DROP DATABASE IF EXISTS `{TEST_DB}`"
            )
            await cur.execute(
                f"CREATE DATABASE `{TEST_DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.close()

        conn = await aiomysql.connect(**_conn_params(), charset="utf8mb4", autocommit=True)
        async with conn.cursor() as cur:
            # 旧 schema：INT 主键/外键/用户ID/时间戳。
            # 种子版本 9 意味着 v10~v16 的分支会全跑一遍，所以 duels 必须照**v9 时点的真实
            # 列**建全（除了故意缩水的 id/match_id 类型）：v14 的替补回填要读
            # round_no / player_a / player_b / a_sub / b_sub，少一列就 1054。
            # matches 只被 v15/v16 探测式加列、没有分支读它的存量列，故照旧最简。
            await cur.execute(
                "CREATE TABLE matches (id INT AUTO_INCREMENT PRIMARY KEY, created_at INT NOT NULL)"
            )
            await cur.execute(
                "CREATE TABLE duels (id INT AUTO_INCREMENT PRIMARY KEY, match_id INT NOT NULL, "
                "round_no INT NOT NULL, "
                "player_a VARCHAR(64) NOT NULL, score_a INT NOT NULL, "
                "player_b VARCHAR(64) NOT NULL, score_b INT NOT NULL, "
                "player_a_team VARCHAR(64) NOT NULL, player_b_team VARCHAR(64) NOT NULL, "
                "result ENUM('A','B','DRAW') NOT NULL, seq INT NOT NULL DEFAULT 0, "
                "a_sub TINYINT NOT NULL DEFAULT 0, b_sub TINYINT NOT NULL DEFAULT 0, "
                "CONSTRAINT fk_duels_match FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE)"
            )
            await cur.execute("CREATE TABLE teams (id INT AUTO_INCREMENT PRIMARY KEY)")
            await cur.execute("CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, created_at INT NOT NULL)")
            await cur.execute("CREATE TABLE player_ids (id INT AUTO_INCREMENT PRIMARY KEY, user_id INT NULL, created_at INT NOT NULL)")
            await cur.execute("CREATE TABLE group_home (group_id VARCHAR(64) PRIMARY KEY, created_at INT NOT NULL)")
            await cur.execute("CREATE TABLE group_ban (group_id VARCHAR(64) PRIMARY KEY, created_at INT NOT NULL)")
            await cur.execute("CREATE TABLE schema_version (version INT NOT NULL)")
            await cur.execute("INSERT INTO schema_version (version) VALUES (9)")
        conn.close()

        db = Database(**_conn_params())
        try:
            await db.initialize()
            rows = await db._query(
                "SELECT table_name AS tbl, column_name AS col, data_type AS dt "
                "FROM information_schema.COLUMNS "
                "WHERE table_schema = DATABASE() AND column_name IN ('id','match_id','user_id','created_at')"
            )
            types = {(r["tbl"], r["col"]): r["dt"] for r in rows}
            for t, c in [
                ("matches", "id"), ("duels", "id"), ("duels", "match_id"),
                ("teams", "id"), ("users", "id"), ("player_ids", "id"),
                ("player_ids", "user_id"),
                ("matches", "created_at"), ("users", "created_at"),
                ("player_ids", "created_at"), ("group_home", "created_at"),
                ("group_ban", "created_at"),
            ]:
                assert types.get((t, c)) == "bigint", f"{t}.{c} = {types.get((t, c))}"
            # 外键已重建
            fk = await db._query(
                "SELECT COUNT(*) AS n FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE constraint_schema = DATABASE() AND table_name='duels' "
                "AND constraint_name='fk_duels_match'"
            )
            assert fk[0]["n"] == 1
        finally:
            await _drop_test_db()
            await db.close()

    asyncio.run(run())


def test_teams_replace():
    """replace_teams 是覆盖写入：旧名单行会被清掉。

    `get_teams` 已随死代码一并删除（它唯一的调用点就是这里），改为直接查库 ——
    验证的是 replace_teams 的语义，不是某个读取函数的返回格式。
    """

    async def ops(db):
        await db.replace_teams(GROUP_ID, [("KC", ["红莲", "悠悠球"]), ("DYG", ["老千"])])
        rows = await db._query(
            "SELECT team_name, player_name FROM teams WHERE group_id=%s", (GROUP_ID,)
        )
        got = sorted((r["team_name"], r["player_name"]) for r in rows)
        assert got == sorted([("KC", "红莲"), ("KC", "悠悠球"), ("DYG", "老千")])
        # 覆盖写入：上一次的 DYG 老千 与 KC 悠悠球 都不该留下
        await db.replace_teams(GROUP_ID, [("KC", ["红莲"])])
        rows = await db._query(
            "SELECT team_name, player_name FROM teams WHERE group_id=%s", (GROUP_ID,)
        )
        assert [(r["team_name"], r["player_name"]) for r in rows] == [("KC", "红莲")]

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
        await _ins(db, rep, "DYG")

        rows = await db._query("SELECT home_team FROM matches WHERE group_id='G1'")
        assert rows[0]["home_team"] == "KC"

        # 旧数据（空 home_team）绑定后回填
        rep2 = _make_report()
        rep2.group_id = "G1"
        rep2.submitted_by = "x"
        rep2.submitted_name = "y"
        rep2.created_at = 0
        # 必须换比赛时间：指纹只认战报内容（不含群号/提交人），
        # 内容一字不差会被唯一键当重复战报拒收
        rep2.match_time = "2026-08-05"
        await _ins(db, rep2, "DYG", home_team="")
        await db.backfill_group_home("G1", "KC")
        rows = await db._query(
            "SELECT home_team FROM matches WHERE group_id='G1' ORDER BY id"
        )
        assert rows[0]["home_team"] == "KC"
        assert rows[1]["home_team"] == "KC"

        # 分析按 home_team 过滤
        pl = await db.get_player_ranking("KC", team="DYG")
        assert pl and pl[0]["player"] == "老千"
        pl_other = await db.get_player_ranking("OTHER", team="DYG")
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
        await _ins(db, rep, "TEST1", home_team="TEST1")

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
        agg = await db.get_players_aggregate("TEST1", ["红莲"])
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
        await _ins(db, rep1, "KC")
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
        await _ins(db, rep2, "KC")

        rec = await db.get_player_record("KC", "红莲")
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


def test_group_chat_type():
    async def ops(db):
        # 缺省为友谊群
        assert await db.get_group_chat_type("G1") == "友谊群"
        await db.set_group_chat_type("G1", "战报群")
        assert await db.get_group_chat_type("G1") == "战报群"
        await db.set_group_chat_type("G1", "主群")
        assert await db.get_group_chat_type("G1") == "主群"
        # 其他群不受影响
        assert await db.get_group_chat_type("G2") == "友谊群"

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
            await _ins(db, rep, winner)

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


def test_duplicate_report_rejected():
    """同一份战报再提交一次 → DuplicateReportError，且第二次不落库（§10-8）。

    两次提交只换提交人：指纹刻意不含 submitted_by / group_id，所以这依然是
    「同一份战报」—— 换个人重发、或另一个群也贴一遍，都不该让统计翻倍。
    """
    import pytest as _pytest

    async def ops(db):
        mid = await _ins(db)
        rep2 = _make_report()
        rep2.submitted_by = "99999"
        rep2.submitted_name = "另一个人"
        with _pytest.raises(DuplicateReportError) as ei:
            await _ins(db, rep2)
        # 带上已有 ID，用户才能拿它去 /战报删除 后重提
        assert ei.value.match_id == mid
        n = await db._query("SELECT COUNT(*) AS n FROM matches")
        assert n[0]["n"] == 1, "重复战报不该落库"

    _with_db(ops)


# ---------- 踢馆 ----------

RAID_SAMPLE = """KC踢馆BAR
规则：OCG.2026.7.1.MATCH
2026.8.30 21:00
踢馆开始！
雨落 2:1 念心
雨落 2:0 青日
雨落 2:1 Defender
雨落 2:1 无语靴
雨落 2:1 云猫(馆主)"""

# 同一场的第二种样例：踢馆者换成友谊报里已存在的 红莲，
# 用来验证「插入踢馆报后，红莲的友谊口径数一个字都不变」。
RAID_SAMPLE_EXISTING = RAID_SAMPLE.replace("雨落", "红莲")

# 同一支对手队、更小的馆：2 人 = 2 分（用于「重复踢同一队取最高」）
RAID_SAMPLE_FEWER = """KC踢馆BAR
规则：OCG.2026.7.1.MATCH
2026.8.30 21:00
踢馆开始！
雨落 2:1 念心
雨落 2:0 青日"""

# 守馆方视角：馆主第 5 个出场终结踢馆者 → SHUT DOWN
RAID_SAMPLE_HOLD = """KC踢馆BAR
规则：OCG.2026.7.1.MATCH
2026.8.30 21:00
踢馆开始！
雨落 2:1 念心
雨落 2:0 青日
雨落 2:1 Defender
雨落 2:1 无语靴
雨落 1:2 云猫(馆主)"""

_RAID_KEYS = {
    "raid_points", "total_points", "attack_points", "defense_points",
    "raid_success", "raid_success_owner", "hold", "first_round", "shutdown",
    "badge", "opponent", "defender_count", "has_owner",
}


def _strip_raid(obj):
    """摘掉踢馆字段，只留友谊口径。

    踢馆报入库后「友谊口径的数一个字都不能变」是本次 18 处 `kind` 过滤的
    验收标准，所以比对前先把踢馆字段拿掉。
    """
    if isinstance(obj, list):
        return [_strip_raid(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _strip_raid(v) for k, v in obj.items() if k not in _RAID_KEYS}
    return obj


def _make_raid_report(text=RAID_SAMPLE):
    r = parse_raid_report(text)
    assert not r.errors, r.errors
    r.report.group_id = GROUP_ID
    r.report.submitted_by = "10002"
    r.report.submitted_name = "踢馆提交者"
    r.report.created_at = 1700000100
    return r.report


async def _ins_raid(db, text=RAID_SAMPLE, team="KC"):
    rep = _make_raid_report(text)
    winner = determine_match_winner(rep) or ""
    return await db.insert_report(rep, winner=winner, home_team=team, raw_text=text)


async def _friendly_snapshot(db):
    """所有友谊赛口径的查询结果快照。"""
    return {
        "ranking": await db.get_player_ranking(TEAM, limit=None),
        "ranking_kc": await db.get_player_ranking(TEAM, team="KC", limit=None),
        "team_record": await db.get_home_team_record(TEAM),
        "honglian": await db.get_player_record(TEAM, "红莲"),
        "aggregate": await db.get_players_aggregate(TEAM, ["红莲", "老千"]),
        "trend": await db.get_player_trend(TEAM, "红莲"),
        "team_trend": await db.get_team_trend(TEAM, "KC"),
        "vs": await db.get_home_team_vs_opponents(TEAM),
    }


def test_raid_does_not_move_friendly_numbers():
    """插入一份踢馆报后，所有友谊赛查询逐字段不变（18 处 kind 过滤的验收）。"""
    async def ops(db):
        await _ins(db)
        before = await _friendly_snapshot(db)

        await _ins_raid(db)

        after = await _friendly_snapshot(db)
        # `vs` 会多出一行「只打过踢馆」的对手（BAR），只比插入前就存在的那些行
        before_opps = {r["opponent"] for r in before["vs"]}
        after["vs"] = [r for r in after["vs"] if r["opponent"] in before_opps]
        assert _strip_raid(after) == _strip_raid(before)

    _with_db(ops)


def test_raid_same_player_keeps_friendly_untouched():
    """踢馆者是友谊报里的同一个人：友谊口径不变，只有踢馆积分增加。"""
    async def ops(db):
        await _ins(db, winner="KC")
        before = await _friendly_snapshot(db)
        honglian_before = next(
            r for r in before["ranking_kc"] if r["player"] == "红莲"
        )
        assert honglian_before["raid_points"] == 0

        await _ins_raid(db, RAID_SAMPLE_EXISTING)

        after = await _friendly_snapshot(db)
        honglian_after = next(r for r in after["ranking_kc"] if r["player"] == "红莲")
        assert honglian_after["raid_points"] == 5          # 4 人 + 馆主 = 3 + 2
        assert honglian_after["total_points"] == honglian_before["points"] + 5
        # 友谊口径逐字段不变
        for key in ("wins", "losses", "draws", "total", "points", "friendship", "wushuang"):
            assert honglian_after[key] == honglian_before[key], key

    _with_db(ops)


def test_raid_team_stats_both_sides():
    """战队级踢馆汇总：踢馆方拿加点，守馆方拿守馆计数（对称取数，M-02 的例外）。"""
    async def ops(db):
        await _ins_raid(db)
        kc = await db.get_raid_team_stats("KC")
        assert kc["raid_success"] == 1
        assert kc["raid_success_owner"] == 1
        assert kc["attack_points"] == 5
        assert kc["raid_points"] == 5
        assert kc["hold"] == 0

        # 守馆方是 BAR：它不在 home_team 里，按 team_a/team_b 对称也能查到
        bar = await db.get_raid_team_stats("BAR")
        assert bar["hold"] == 0 and bar["raid_success"] == 0 and bar["raid_points"] == 0

    _with_db(ops)


def test_home_team_record_exposes_attack_points():
    """`get_home_team_record` 也要给出踢馆加点（排行图片左侧面板的第 4 行）。

    护栏是**等值**而不是写死魔数：这个键必须与 `get_raid_team_stats` 的
    同名字段一致 —— 面板要求「零额外查询」，靠的就是复用 record 里已经
    取到的 raid 汇总。哪天有人在 main.py 里另算一遍，这里不会报警；
    但哪天 record 忘了透传（返回 0 或 KeyError），这里会。
    """
    async def ops(db):
        await _ins(db)
        await _ins_raid(db)
        rec = await db.get_home_team_record("KC")
        raid = await db.get_raid_team_stats("KC")
        assert rec["attack_points"] == raid["attack_points"]
        assert rec["attack_points"] == 5
        # 面板其余几行也用到的键，一并确认在同一个 dict 里
        for k in ("total", "wins", "losses", "win_rate", "total_points",
                  "hold", "first_round"):
            assert k in rec, k

    _with_db(ops)


def test_raid_defender_gets_hold_points():
    """守馆方视角：馆主第 5 个出场终结踢馆者 → SHUT DOWN，+5。"""
    async def ops(db):
        await _ins_raid(db, RAID_SAMPLE_HOLD)
        bar = await db.get_raid_team_stats("BAR")
        assert bar["hold"] == 1
        assert bar["shutdown"] == 1
        # ceil(守馆成功1/3)=1 + 5×SHUT DOWN
        assert bar["defense_points"] == 6
        assert bar["raid_points"] == 6

    _with_db(ops)


def test_raid_player_stats():
    """个人级踢馆汇总：踢馆方拿到加点，守馆方的终结者拿到守馆分。"""
    async def ops(db):
        await _ins_raid(db)
        players = await db.get_raid_player_stats("KC")
        assert players["雨落"]["raid_points"] == 5
        assert players["雨落"]["raid_success_owner"] == 1

        await _ins_raid(db, RAID_SAMPLE_HOLD)
        holders = await db.get_raid_player_stats("BAR")
        assert holders["云猫"]["hold"] == 1
        assert holders["云猫"]["shutdown"] == 1
        assert holders["云猫"]["raid_points"] == 6   # ceil(1/3) + 5×SHUT DOWN

    _with_db(ops)


def test_raid_stats_for_players_resolves_names():
    """参赛ID → 已解析名（M-03）：改过名的用户查踢馆分照样对得上。"""
    async def ops(db):
        await _ins_raid(db)
        uid = await db.find_or_create_user("KC", "夜雨")
        await db.bind_player_to_user("KC", "雨落", uid)
        agg = await db.get_raid_stats_for_players("KC", ["雨落"])
        assert agg["raid_points"] == 5

    _with_db(ops)


def test_raid_repeat_same_team_takes_max():
    """当月重复踢穿同一战队不累计，取最高分那场。"""
    async def ops(db):
        await _ins_raid(db)                          # 4 人 + 馆主 = 5 分
        await _ins_raid(db, RAID_SAMPLE_FEWER)       # 2 人 = 2 分
        kc = await db.get_raid_team_stats("KC")
        assert kc["raid_success"] == 2
        assert kc["attack_points"] == 5, "同一对手队只取最高那场"

    _with_db(ops)


def test_raid_export_keeps_kind_and_owner():
    """导出链路能取到踢馆报，且带 kind 与逐局的 owner（导出不过滤 kind）。"""
    async def ops(db):
        mid = await _ins_raid(db)
        rows = await db.get_export_rows("KC")
        # 一行一局，踢馆报的 5 局都在，且带 kind 与逐局 owner
        raid_rows = [r for r in rows if r["kind"] == "raid"]
        assert len(raid_rows) == 5
        assert {r["match_id"] for r in raid_rows} == {mid}
        assert [r["owner"] for r in raid_rows] == [0, 0, 0, 0, 1]

        reports = await db.get_reports_for_export("KC")
        raid = next(r for r in reports if r["match_id"] == mid)
        assert raid["kind"] == "raid"
        assert raid["duels"][-1]["owner"] == 1
        assert raid["duels"][-1]["player_b"] == "云猫"

    _with_db(ops)


def test_raid_placeholder_creates_no_player_id():
    """`规则` 占位行不建参赛ID（否则 ID 池里会多出一个叫「规则」的假人）。"""
    text = RAID_SAMPLE.replace("云猫(馆主)", "规则").replace("无语靴", "规则")
    async def ops(db):
        await _ins_raid(db, text)
        rows = await db._query(
            "SELECT player_name FROM player_ids WHERE home_team = %s", ("BAR",)
        )
        names = {r["player_name"] for r in rows}
        assert "规则" not in names
        assert "念心" in names

    _with_db(ops)


# ---------- 月度结算（settlement，v1.15.0）----------

def test_settlement_roundtrip():
    """写入后能原样取回；未结算的月份返回空 dict。"""
    async def ops(db):
        assert await db.get_settlement("KC", "2026-07") == {}

        await db.set_settlement("KC", "2026-07", [
            {"rank_no": 1, "player": "红莲", "total_points": 20.0, "bonus": 60.0},
            {"rank_no": 2, "player": "老千", "total_points": 3.0, "bonus": 9.0},
        ])
        got = await db.get_settlement("KC", "2026-07")
        assert got == {"红莲": 60.0, "老千": 9.0}
        # 别的月份 / 别的战队不受影响
        assert await db.get_settlement("KC", "2026-08") == {}
        assert await db.get_settlement("BAR", "2026-07") == {}

    _with_db(ops)


def test_settlement_entries_detail():
    """`get_settlement_entries` 给出只读查询回执所需的明细（按名次升序）。

    `get_settlement` 只给 `{玩家名: 奖金}`，缺 rank_no / total_points，
    拼不出回执 —— 非管理员的 `/结算` 走的是这个。
    """
    async def ops(db):
        assert await db.get_settlement_entries("KC", "2026-07") == []

        await db.set_settlement("KC", "2026-07", [
            {"rank_no": 2, "player": "老千", "total_points": 3.0, "bonus": 9.0},
            {"rank_no": 1, "player": "红莲", "total_points": 20.0, "bonus": 60.0},
        ])
        got = await db.get_settlement_entries("KC", "2026-07")
        # 按 rank_no 升序（写入顺序是反的）
        assert [e["rank_no"] for e in got] == [1, 2]
        assert [e["player"] for e in got] == ["红莲", "老千"]
        assert got[0]["total_points"] == 20.0 and got[0]["bonus"] == 60.0
        # 与 get_settlement 同源：名字到奖金的映射一致
        assert {e["player"]: e["bonus"] for e in got} == await db.get_settlement(
            "KC", "2026-07"
        )
        # 别的月份 / 别的战队的战绩不受影响
        assert await db.get_settlement_entries("KC", "2026-08") == []
        assert await db.get_settlement_entries("BAR", "2026-07") == []

    _with_db(ops)


def test_get_qq_ids_by_names_returns_only_bound_qqs():
    """结算公告的 @ 靠这个取 QQ：按**展示名**（`users.name`）查，取不到的就不返回。

    关键点是**按 users.name 而不是 player_ids.player_name 查** —— 榜上的
    `player` 是 `COALESCE(u.name, d.player_a)`，绑定了角色的选手显示的是角色名，
    拿参赛ID去查会一个都匹配不上、整条公告 @ 不出任何人。
    """
    async def ops(db):
        assert await db.get_qq_ids_by_names("KC", []) == {}          # 空名单不查库
        assert await db.get_qq_ids_by_names("KC", ["红莲"]) == {}    # 查不到 → 空

        await db.find_or_create_user("KC", "红莲", "10001")
        await db.find_or_create_user("KC", "老千", "20002")
        await db.find_or_create_user("KC", "无Q的")                  # 有角色、没绑 QQ
        await db.bind_player_to_user(
            "KC", "HY_红莲", (await db.get_user_by_qq("KC", "10001"))["id"]
        )

        got = await db.get_qq_ids_by_names("KC", ["红莲", "老千"])
        assert got == {"红莲": "10001", "老千": "20002"}
        # 没绑 QQ 的、库里没有的都不出现在返回值里（调用方退化成字面 @名字）
        assert "无Q的" not in await db.get_qq_ids_by_names("KC", ["无Q的"])
        assert await db.get_qq_ids_by_names("KC", ["查无此人"]) == {}
        # 别的战队的同名角色不串门
        assert await db.get_qq_ids_by_names("BAR", ["红莲"]) == {}
        # 参赛ID（player_ids.player_name）查不出来 —— 这正是要按 users.name 的理由
        assert await db.get_qq_ids_by_names("KC", ["HY_红莲"]) == {}

    _with_db(ops)


def test_clear_settlement_restores_unsettled_month():
    """`/重置结算` 的数据层：删干净、只删本队本月、删完能重新结算。

    重置之后两个读取口都要回到「未结算」的样子 —— `get_settlement` 空 dict
    （`/排行` 少一列奖金）、`get_settlement_entries` 空 list（`/结算` 回
    「尚未结算」），且 `settlement_awards_changed` 会因此判成「第一次结算」。
    """
    async def ops(db):
        await db.set_settlement("KC", "2026-07", [
            {"rank_no": 1, "player": "红莲", "total_points": 20.0, "bonus": 60.0},
            {"rank_no": 2, "player": "老千", "total_points": 3.0, "bonus": 9.0},
        ])
        await db.set_settlement("KC", "2026-08", [
            {"rank_no": 1, "player": "红莲", "total_points": 10.0, "bonus": 30.0},
        ])

        assert await db.clear_settlement("KC", "2026-07") == 2   # 返回删掉的行数

        assert await db.get_settlement("KC", "2026-07") == {}
        assert await db.get_settlement_entries("KC", "2026-07") == []
        # 别的月份不受影响
        assert await db.get_settlement("KC", "2026-08") == {"红莲": 30.0}
        # 重复重置 = 无事发生（返回 0，不报错）
        assert await db.clear_settlement("KC", "2026-07") == 0
        # 没结算过的月份 / 别的战队也返回 0
        assert await db.clear_settlement("KC", "2026-09") == 0
        assert await db.clear_settlement("BAR", "2026-08") == 0

        # 重置后可以重新结算，且旧行不会残留成重复
        await db.set_settlement("KC", "2026-07", [
            {"rank_no": 1, "player": "悠悠球", "total_points": 5.0, "bonus": 15.0},
        ])
        assert await db.get_settlement("KC", "2026-07") == {"悠悠球": 15.0}

        rows = await db._query(
            "SELECT COUNT(*) AS n FROM settlements WHERE home_team = %s AND month = %s",
            ("KC", "2026-07"),
        )
        assert rows[0]["n"] == 1

    _with_db(ops)


def test_settlement_rerun_overwrites_without_duplicating():
    """重结算 = 覆盖：同名玩家不会出现两行，行数不翻倍，旧奖金作废。"""
    async def ops(db):
        await db.set_settlement("KC", "2026-07", [
            {"rank_no": 1, "player": "红莲", "total_points": 20.0, "bonus": 60.0},
            {"rank_no": 2, "player": "老千", "total_points": 3.0, "bonus": 9.0},
        ])
        # 追加到 8 名：红莲的名次/奖金变了，又多出一个人
        await db.set_settlement("KC", "2026-07", [
            {"rank_no": 1, "player": "红莲", "total_points": 30.0, "bonus": 90.0},
            {"rank_no": 2, "player": "老千", "total_points": 3.0, "bonus": 9.0},
            {"rank_no": 3, "player": "悠悠球", "total_points": 1.0, "bonus": 3.0},
        ])
        got = await db.get_settlement("KC", "2026-07")
        assert got == {"红莲": 90.0, "老千": 9.0, "悠悠球": 3.0}

        rows = await db._query(
            "SELECT COUNT(*) AS n FROM settlements WHERE home_team = %s AND month = %s",
            ("KC", "2026-07"),
        )
        assert rows[0]["n"] == 3

    _with_db(ops)


def test_settlement_rerun_back_to_four_drops_extra_rows():
    """从 12 名回到前 4 名：多出来的行必须被删掉，不能残留。"""
    async def ops(db):
        entries = [
            {"rank_no": i, "player": f"P{i}", "total_points": 10.0, "bonus": 30.0}
            for i in range(1, 13)
        ]
        await db.set_settlement("KC", "2026-07", entries)
        assert len(await db.get_settlement("KC", "2026-07")) == 12

        await db.set_settlement("KC", "2026-07", entries[:4])
        got = await db.get_settlement("KC", "2026-07")
        assert sorted(got) == ["P1", "P2", "P3", "P4"]

    _with_db(ops)


def test_settlement_isolated_per_team_and_month():
    """同月不同队、同队不同月互不覆盖。"""
    async def ops(db):
        await db.set_settlement("KC", "2026-07", [
            {"rank_no": 1, "player": "红莲", "total_points": 1.0, "bonus": 3.0},
        ])
        await db.set_settlement("BAR", "2026-07", [
            {"rank_no": 1, "player": "红莲", "total_points": 2.0, "bonus": 6.0},
        ])
        await db.set_settlement("KC", "2026-08", [
            {"rank_no": 1, "player": "红莲", "total_points": 4.0, "bonus": 12.0},
        ])
        assert await db.get_settlement("KC", "2026-07") == {"红莲": 3.0}
        assert await db.get_settlement("BAR", "2026-07") == {"红莲": 6.0}
        assert await db.get_settlement("KC", "2026-08") == {"红莲": 12.0}

    _with_db(ops)


def test_settlement_player_name_matches_ranking():
    """结算按解析后的展示名落库，能直接对上 `get_player_ranking` 的 `player` 列。

    对不上就会出现「榜上有名、奖金列却是 0」—— 这是本功能最容易静默出错的地方。
    """
    async def ops(db):
        await _ins(db, winner="KC")
        rows = await db.get_player_ranking("KC", "2026-08-01", "2026-08-31", 1, None, team="KC")
        assert rows, "样本战报应当产出排行行"

        entries = [
            {"rank_no": i + 1, "player": r["player"],
             "total_points": r.get("total_points") or 0, "bonus": 10.0}
            for i, r in enumerate(rows)
        ]
        await db.set_settlement("KC", "2026-08", entries)
        bonus_map = await db.get_settlement("KC", "2026-08")
        assert set(bonus_map) == {r["player"] for r in rows}
        for r in rows:
            assert r["player"] in bonus_map

    _with_db(ops)


def test_settlement_table_survives_raid_insert():
    """踢馆报不影响结算表（结算与 kind 无关，只按战队+月份取数）。"""
    async def ops(db):
        await _ins_raid(db)
        assert await db.get_settlement("KC", "2026-08") == {}
        await db.set_settlement("KC", "2026-08", [
            {"rank_no": 1, "player": "雨落", "total_points": 5.0, "bonus": 15.0},
        ])
        assert await db.get_settlement("KC", "2026-08") == {"雨落": 15.0}

    _with_db(ops)


def test_settlement_decimals_roundtrip():
    """小数奖金（总积分 × 3 常有小数）不能丢精度。"""
    async def ops(db):
        await db.set_settlement("KC", "2026-07", [
            {"rank_no": 2, "player": "红莲", "total_points": 12.5, "bonus": 37.5},
        ])
        assert await db.get_settlement("KC", "2026-07") == {"红莲": 37.5}

    _with_db(ops)
