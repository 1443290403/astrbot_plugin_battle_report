"""爬取线上战报系统（rep.ygobbs2.com）指定区间的战报，导出 JSON/CSV 并标注重复组。

背景：插件 astrbot_plugin_battle_report 的统计结果与实际对战结果存在差异。线上战报
系统是权威数据源，本脚本把指定查询区间的全部战报抓下来，解析正文（正文格式与插件
战报格式完全一致，直接复用 battle_report_parser），并按正文内容找出「完全一样的
战报」——同一份战报被重复提交会让统计翻倍。

注意：线上查询的 starttime/endtime 过滤的是**发布时间**，不是正文里的**比赛时间**。
两者可能不在同一个月（实测区间内就有 1 条比赛时间为 2026.08.26），脚本会标注越界记录。

用法：
    python scripts/crawl_battle_reports.py
    python scripts/crawl_battle_reports.py --group KC --start 2026-09-01 --end 2026-09-30
    python scripts/crawl_battle_reports.py --refresh        # 忽略 HTML 缓存重新抓取

输出（默认 scripts/output/）：
    battle_report_<group>_<start>_<end>.json / .csv
    raw/page_<n>.html        原始 HTML 缓存，重跑时复用（需通过校验）
"""

import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

# 让脚本可以从插件根目录以包方式导入解析器
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from battle_report_parser import (
        determine_match_winner,
        parse_battle_report,
    )
except ImportError:
    print("无法导入 battle_report_parser，请检查脚本路径")
    sys.exit(1)

# Windows 控制台默认 GBK，输出含 ⚠ 等字符会抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

BASE_URL = "https://rep.ygobbs2.com/index.php"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 总页数提示：「当前为1页，共有20页」
_TOTAL_PAGES_RE = re.compile(r"共有\s*(\d+)\s*页")
# 数据行 <tr>...</tr>；表头行用 <th>，不会被 _TD_RE 命中
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td>(.*?)</td>", re.S)
# 正文单元格里的 <div class="panel-body">...</div>
_BODY_RE = re.compile(r'<div class="panel-body">(.*?)</div>', re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")

# 页面有效性标志：抓失败时站点返回一个很短的错误页
_PAGE_MARKER = "战报列表"


class PageError(RuntimeError):
    """某一页在重试后仍无法取得有效内容。"""


def norm_content(text: str) -> str:
    """战报正文归一化，作为「内容是否完全一样」的指纹。

    全角空格转半角、折叠行内空白、逐行 strip、丢弃空行。保留行序。
    """
    lines = [re.sub(r"\s+", " ", ln.replace("　", " ")).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _extract_body(cell: str) -> str | None:
    """从正文单元格 HTML 里还原战报文本（<br> → 换行）。"""
    m = _BODY_RE.search(cell)
    if not m:
        return None
    raw = _BR_RE.sub("\n", m.group(1))
    raw = _TAG_RE.sub("", raw)
    return html.unescape(raw).strip()


def _normalize_pub_date(raw: str) -> str:
    """把表里的「26-09-30」补成「2026-09-30」。"""
    parts = re.split(r"[^0-9]+", raw.strip())
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return raw.strip()
    y, m, d = (int(p) for p in parts)
    if y < 100:
        y += 2000
    return f"{y:04d}-{m:02d}-{d:02d}"


def parse_page(html_text: str) -> tuple[list[dict], int | None]:
    """解析一页 HTML，返回（记录列表, 总页数）。

    记录含 id/publish_date/publisher/winner_site/loser_site/body。
    """
    m = _TOTAL_PAGES_RE.search(html_text)
    total_pages = int(m.group(1)) if m else None

    records: list[dict] = []
    for row in _ROW_RE.findall(html_text):
        cells = _TD_RE.findall(row)
        if len(cells) != 6:
            continue  # 表头行或非数据行
        rid = cells[0].strip()
        if not rid.isdigit():
            continue
        body = _extract_body(cells[5])
        if body is None:
            continue
        records.append(
            {
                "id": rid,
                "winner_site": cells[1].strip(),
                "loser_site": cells[2].strip(),
                "publish_date_raw": cells[3].strip(),
                "publish_date": _normalize_pub_date(cells[3]),
                "publisher": cells[4].strip(),
                "body": body,
            }
        )
    return records, total_pages


def fetch_page(page: int, group: str, start: str, end: str, timeout: int) -> str:
    """请求单页，返回 HTML 文本。"""
    url = (
        f"{BASE_URL}?c=search&starttime={start}&endtime={end}"
        f"&groupname={urllib.parse.quote(group)}&a=sub&page={page}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_page_verified(
    page: int,
    group: str,
    start: str,
    end: str,
    expected: int | None,
    *,
    cache_dir: Path | None,
    refresh: bool,
    timeout: int,
    max_retries: int,
    backoff: float,
) -> tuple[list[dict], int | None]:
    """取一页并校验，失败则重试；缓存文件同样要过校验才复用。

    expected 为该页应有记录数（末页传 None，表示 1~page_size 皆可）。
    """
    cache_file = cache_dir / f"page_{page}.html" if cache_dir else None
    last_err = ""

    for attempt in range(1, max_retries + 1):
        html_text = None
        if cache_file and cache_file.exists() and not refresh:
            html_text = cache_file.read_text(encoding="utf-8", errors="replace")
            source = "cache"
        else:
            source = "http"

        if html_text is None:
            try:
                html_text = fetch_page(page, group, start, end, timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = f"请求失败：{e}"
                html_text = None

        if html_text is not None:
            if _PAGE_MARKER not in html_text:
                last_err = f"响应缺少『{_PAGE_MARKER}』标记（{len(html_text)} 字节，疑似错误页）"
            else:
                records, total_pages = parse_page(html_text)
                ok = len(records) > 0 if expected is None else len(records) == expected
                if ok:
                    if cache_file and source == "http":
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        cache_file.write_text(html_text, encoding="utf-8")
                    return records, total_pages
                last_err = f"解析到 {len(records)} 条，期望 {expected} 条"

        if attempt < max_retries:
            wait = backoff * attempt
            print(f"  第 {page} 页第 {attempt} 次失败（{last_err}），{wait:.0f}s 后重试…")
            # 缓存命中但校验失败时，退化为重新请求
            if cache_file and cache_file.exists() and not refresh:
                try:
                    cache_file.unlink()
                except OSError:
                    pass
            time.sleep(wait)

    raise PageError(f"第 {page} 页重试 {max_retries} 次仍失败：{last_err}")


def enrich(record: dict, start: str, end: str) -> dict:
    """解析正文，补上结构化字段与胜负比对结果。"""
    result = parse_battle_report(record["body"])
    out = dict(record)
    out["content_key"] = norm_content(record["body"])
    out["parse_ok"] = result.report is not None
    out["parse_errors"] = result.errors
    out["parse_warnings"] = result.warnings
    if result.report is None:
        out.update(
            team_a="", team_b="", match_time="", rule="", location="",
            winner_computed=None, winner_match=None, match_time_in_range=None,
            duels=[],
        )
        return out

    rep = result.report
    winner = determine_match_winner(rep)
    out.update(
        team_a=rep.team_a,
        team_b=rep.team_b,
        match_time=rep.match_time,
        rule=rep.rule,
        location=rep.location,
        winner_computed=winner,
        winner_match=(winner == record["winner_site"]) if winner else None,
        match_time_in_range=(start <= rep.match_time <= end),
        duels=[
            {
                "round_no": d.round_no,
                "player_a": d.player_a,
                "score_a": d.score_a,
                "player_b": d.player_b,
                "score_b": d.score_b,
                "a_sub": d.a_sub,
                "b_sub": d.b_sub,
                "ruled": d.ruled,
            }
            for d in rep.duels
        ],
    )
    return out


def mark_duplicates(records: list[dict]) -> list[list[str]]:
    """按正文内容指纹归组，给同内容的记录打上 dup_group / dup_size。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        groups[r["content_key"]].append(r)

    dup_groups: list[list[str]] = []
    group_no = 0
    for key in sorted(groups, key=lambda k: groups[k][0]["id"]):
        members = sorted(groups[key], key=lambda r: r["id"])
        if len(members) > 1:
            group_no += 1
            dup_groups.append([m["id"] for m in members])
        for m in members:
            m["dup_group"] = group_no if len(members) > 1 else 0
            m["dup_size"] = len(members)
    return dup_groups


def group_same_match(records: list[dict]) -> list[list[str]]:
    """按「比赛时间 + 对战战队(无序)」聚合，作为参考信息（该维度重复多为双方各自提交）。"""
    groups: dict[tuple, list[str]] = defaultdict(list)
    for r in records:
        if not r.get("match_time"):
            continue
        teams = tuple(sorted({r["team_a"], r["team_b"]}))
        if len(teams) != 2:
            continue
        groups[(r["match_time"], teams)].append(r["id"])
    return [sorted(v) for v in groups.values() if len(v) > 1]


def build_summary(records: list[dict], dup_groups, same_match, meta: dict) -> dict:
    parse_failed = [r["id"] for r in records if not r["parse_ok"]]
    mismatch = [r["id"] for r in records if r.get("winner_match") is False]
    undecided = [r["id"] for r in records if r["parse_ok"] and r.get("winner_computed") is None]
    out_of_range = [r["id"] for r in records if r.get("match_time_in_range") is False]
    return {
        **meta,
        "total": len(records),
        "unique_ids": len({r["id"] for r in records}),
        "unique_contents": len({r["content_key"] for r in records}),
        "parse_failed": parse_failed,
        "winner_mismatch": mismatch,
        "winner_undecided": undecided,
        "out_of_range": out_of_range,
        "publishers": dict(Counter(r["publisher"] for r in records).most_common()),
        "duplicate_groups": dup_groups,
        "duplicate_ids": sorted({i for g in dup_groups for i in g}),
        "same_match_groups": same_match,
    }


def write_csv(path: Path, records: list[dict]) -> None:
    cols = [
        "id", "publish_date", "publisher", "winner_site", "loser_site",
        "match_time", "rule", "location", "winner_computed", "winner_match",
        "match_time_in_range", "dup_group", "dup_size", "parse_ok",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(records, key=lambda x: x["id"]):
            w.writerow(r)


def print_summary(summary: dict, records: list[dict]) -> None:
    by_id = {r["id"]: r for r in records}
    print()
    print("=" * 68)
    print(f"区间 {summary['start']} ~ {summary['end']}  战队 {summary['group']}")
    print(f"页数 {summary['pages']}   记录 {summary['total']} 条   唯一 ID {summary['unique_ids']}")
    print(f"正文内容唯一值 {summary['unique_contents']}   解析失败 {len(summary['parse_failed'])}")
    print()
    print("提交人分布：")
    for name, cnt in summary["publishers"].items():
        print(f"  {name}: {cnt}")
    print()
    if summary["duplicate_groups"]:
        print(f"⚠ 内容完全一样的战报：{len(summary['duplicate_groups'])} 组 / "
              f"{len(summary['duplicate_ids'])} 条")
        for i, ids in enumerate(summary["duplicate_groups"], 1):
            print(f"  组 {i}: {ids}")
            for rid in ids:
                r = by_id[rid]
                print(f"    {rid}  发布 {r['publish_date']}  {r['publisher']}"
                      f"  |  {r['winner_site']} 胜 {r['loser_site']} 负"
                      f"  |  {r['match_time'] or '?'} @ {r['location'] or '?'}")
    else:
        print("✓ 没有内容完全一样的战报")
    print()
    if summary["winner_mismatch"]:
        print(f"⚠ 站点胜方与正文复算结果不符：{len(summary['winner_mismatch'])} 条 "
              f"{summary['winner_mismatch']}")
    if summary["winner_undecided"]:
        print(f"· 正文无法判定胜方（胜负未定）：{len(summary['winner_undecided'])} 条")
    if summary["parse_failed"]:
        print(f"⚠ 正文解析失败：{summary['parse_failed']}")
    if summary["out_of_range"]:
        print(f"· 比赛时间不在区间内：{summary['out_of_range']}")
    print(f"· (比赛时间+战队) 重复的场次组：{len(summary['same_match_groups'])} 组"
          f"（双方各自提交属正常，仅供参考）")
    print("=" * 68)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="爬取线上战报系统指定区间的战报并标注重复组"
    )
    ap.add_argument("--group", default="KC", help="战队名（默认 KC）")
    ap.add_argument("--start", default="2026-09-01", help="区间起（发布时间，默认 2026-09-01）")
    ap.add_argument("--end", default="2026-09-30", help="区间止（发布时间，默认 2026-09-30）")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认 scripts/output）")
    ap.add_argument("--timeout", type=int, default=45, help="单次请求超时秒数（默认 45）")
    ap.add_argument("--max-retries", type=int, default=5, help="每页最大尝试次数（默认 5）")
    ap.add_argument("--backoff", type=float, default=3.0, help="重试退避基数秒（默认 3）")
    ap.add_argument("--delay", type=float, default=1.0, help="页间间隔秒（默认 1）")
    ap.add_argument("--refresh", action="store_true", help="忽略 HTML 缓存重新抓取")
    ap.add_argument("--no-cache", action="store_true", help="不读写 HTML 缓存")
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    out_dir = Path(args.out_dir) if args.out_dir else script_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = None if args.no_cache else out_dir / "raw"

    stem = f"battle_report_{args.group}_{args.start}_{args.end}"

    records: list[dict] = []
    try:
        print(f"抓取第 1 页…")
        first, total_pages = fetch_page_verified(
            1, args.group, args.start, args.end, None,
            cache_dir=cache_dir, refresh=args.refresh, timeout=args.timeout,
            max_retries=args.max_retries, backoff=args.backoff,
        )
        records.extend(first)
        page_size = len(first)
        if total_pages is None:
            total_pages = 1
        print(f"共 {total_pages} 页，每页 {page_size} 条")

        for page in range(2, total_pages + 1):
            expected = page_size if page < total_pages else None
            print(f"抓取第 {page}/{total_pages} 页…")
            got, _ = fetch_page_verified(
                page, args.group, args.start, args.end, expected,
                cache_dir=cache_dir, refresh=args.refresh, timeout=args.timeout,
                max_retries=args.max_retries, backoff=args.backoff,
            )
            records.extend(got)
            if args.delay:
                time.sleep(args.delay)
    except PageError as e:
        print(f"\n✗ {e}")
        print("  已抓到的内容不会导出，避免产出不完整数据。")
        return 1
    except KeyboardInterrupt:
        print("\n✗ 已中断，未导出。")
        return 1

    # 唯一性校验：ID 重复说明分页错乱
    ids = [r["id"] for r in records]
    dup_ids = [i for i, c in Counter(ids).items() if c > 1]
    if dup_ids:
        print(f"\n✗ 抓取结果存在重复 ID（分页可能错乱）：{dup_ids}")
        return 1

    entries = [enrich(r, args.start, args.end) for r in records]
    dup_groups = mark_duplicates(entries)
    same_match = group_same_match(entries)

    meta = {
        "group": args.group,
        "start": args.start,
        "end": args.end,
        "pages": total_pages,
        "source": f"{BASE_URL}?c=search&starttime={args.start}&endtime={args.end}&groupname={args.group}&a=sub",
    }
    summary = build_summary(entries, dup_groups, same_match, meta)

    json_path = out_dir / f"{stem}.json"
    csv_path = out_dir / f"{stem}.csv"
    json_path.write_text(
        json.dumps({"summary": summary, "records": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_csv(csv_path, entries)

    print_summary(summary, entries)
    print(f"\n已导出：\n  {json_path}\n  {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
