"""战报爬取脚本单元测试（不依赖网络）。

覆盖：HTML 行解析、正文还原、正文归一化指纹、重复组归组、正文解析与胜负比对。
"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import crawl_battle_reports as crawler  # noqa: E402

# 站点结构：表头用 <th>，数据行 6 个 <td>，最后一格是 panel-body
PAGE_HTML = """<html><body>
<table class="table"><caption>战报列表</caption>
<thead><tr><th>ID</th><th>胜方</th><th>负方</th><th>发布时间</th><th>发布玩家</th><th>具体内容</th></tr></thead>
<tbody>
<tr>
  <td>157101</td><td>KC</td><td>FA</td><td>26-09-30</td><td>RLX</td>
  <td><div class="panel panel-default"><div class="panel-body">
  战队: FA VS KC<br>时间: 2026.09.30<br>规则: 2/3【KOF】<br>地点: 801965989<br>
  ------第一轮------<br>地&nbsp; 2:1&nbsp; 间桐樱<br>黑花花  1:2  浸泡<br>
  ------第二轮------<br>地  1:2  绯洛<br>黑花花  1:2  绯洛</div></div></td>
</tr>
<tr>
  <td>157100</td><td>KC</td><td>HYS</td><td>26-09-30</td><td>RLX</td>
  <td><div class="panel panel-default"><div class="panel-body">
  战队: HYS VS KC<br>时间: 2026.08.26<br>规则: 2/3【KOF】<br>地点: 430293653<br>
  ------第一轮------<br>秋燊  2:0  浸泡</div></div></td>
</tr>
</tbody></table>
<div><p>当前为1页，共有20页</p></div>
</body></html>"""


def test_parse_page_extracts_fields():
    records, total_pages = crawler.parse_page(PAGE_HTML)
    assert total_pages == 20
    assert [r["id"] for r in records] == ["157101", "157100"]
    r = records[0]
    assert (r["winner_site"], r["loser_site"]) == ("KC", "FA")
    assert r["publisher"] == "RLX"
    assert r["publish_date"] == "2026-09-30"
    assert r["publish_date_raw"] == "26-09-30"


def test_extract_body_converts_br_and_unescapes():
    records, _ = crawler.parse_page(PAGE_HTML)
    body = records[0]["body"]
    lines = body.splitlines()
    assert lines[0] == "战队: FA VS KC"
    assert lines[1] == "时间: 2026.09.30"
    assert "<br>" not in body
    # &nbsp; 应还原为不换行空格，且不残留标签
    assert "<" not in body


def test_normalize_pub_date():
    assert crawler._normalize_pub_date("26-09-30") == "2026-09-30"
    assert crawler._normalize_pub_date("2026-09-30") == "2026-09-30"
    assert crawler._normalize_pub_date("垃圾") == "垃圾"


def test_norm_content_collapses_noise():
    a = "战队: KC VS DYG\n  时间: 2026.09.01  \n\n地点: 123\n"
    b = "战队: KC VS DYG\n时间: 2026.09.01\n地点: 123"
    assert crawler.norm_content(a) == crawler.norm_content(b)
    assert crawler.norm_content(a) != crawler.norm_content(b.replace("123", "456"))


def test_enrich_parses_and_compares_winner():
    records, _ = crawler.parse_page(PAGE_HTML)
    good = crawler.enrich(records[0], "2026-09-01", "2026-09-30")
    assert good["parse_ok"]
    assert good["team_a"] == "FA" and good["team_b"] == "KC"
    assert good["match_time"] == "2026-09-30"
    assert good["match_time_in_range"] is True
    assert good["winner_computed"] == "KC"
    assert good["winner_match"] is True
    assert len(good["duels"]) == 4

    # 比赛时间 2026.08.26 落在区间外
    outside = crawler.enrich(records[1], "2026-09-01", "2026-09-30")
    assert outside["match_time"] == "2026-08-26"
    assert outside["match_time_in_range"] is False


def test_mark_duplicates_groups_identical_content_only():
    records, _ = crawler.parse_page(PAGE_HTML)
    base = crawler.enrich(records[0], "2026-09-01", "2026-09-30")

    dup = dict(base, id="999999", publisher="别人")          # 正文完全一样 → 同组
    other = dict(base, id="888888", winner_site="FA")        # 正文不同 → 不同组
    other["body"] = base["body"].replace("绯洛", "别天")
    other["content_key"] = crawler.norm_content(other["body"])

    groups = crawler.mark_duplicates([base, dup, other])
    assert groups == [[base["id"], "999999"]], groups  # 组内按 id 升序
    assert base["dup_group"] == 1 and base["dup_size"] == 2
    assert dup["dup_group"] == 1
    assert other["dup_group"] == 0 and other["dup_size"] == 1


def test_mark_duplicates_no_duplicates():
    records, _ = crawler.parse_page(PAGE_HTML)
    entries = [crawler.enrich(r, "2026-09-01", "2026-09-30") for r in records]
    assert crawler.mark_duplicates(entries) == []
    assert all(e["dup_group"] == 0 and e["dup_size"] == 1 for e in entries)
