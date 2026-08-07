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


def test_team_ranking():
    async def ops(db):
        await _ins(db)
        team = await db.get_team_ranking(TEAM)
        assert team[0]["team"] == "DYG"
        assert team[0]["wins"] == 1
        kc = next(r for r in team if r["team"] == "KC")
        assert kc["losses"] == 1

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
        # 红莲是 KC 选手：1胜1负
        rec = await db.get_player_record(TEAM, "红莲")
        assert rec["wins"] == 1 and rec["losses"] == 1

        trend = await db.get_player_trend(TEAM, "红莲")
        assert trend and trend[0][1] == 1  # (date, wins, losses)
        assert trend[0][2] == 1

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
            # 旧 schema：INT 主键/外键/用户ID/时间戳
            await cur.execute(
                "CREATE TABLE matches (id INT AUTO_INCREMENT PRIMARY KEY, created_at INT NOT NULL)"
            )
            await cur.execute(
                "CREATE TABLE duels (id INT AUTO_INCREMENT PRIMARY KEY, match_id INT NOT NULL, "
                "score_a INT NOT NULL, score_b INT NOT NULL, result ENUM('A','B','DRAW') NOT NULL, "
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
        await _ins(db, rep, "DYG")

        rows = await db._query("SELECT home_team FROM matches WHERE group_id='G1'")
        assert rows[0]["home_team"] == "KC"

        # 旧数据（空 home_team）绑定后回填
        rep2 = _make_report()
        rep2.group_id = "G1"
        rep2.submitted_by = "x"
        rep2.submitted_name = "y"
        rep2.created_at = 0
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
