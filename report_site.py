"""战报站公开页面解析、查询参数与受限分页抓取。"""
import re
import asyncio
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from .battle_report_parser import parse_battle_report, determine_match_winner
except ImportError:
    from battle_report_parser import parse_battle_report, determine_match_winner

BASE_URL = 'https://rep.ygobbs2.com/index.php'
_TOTAL_PAGES_RE = re.compile(r"共(?:有)?\s*(\d+)\s*页")
# 数据行 <tr>...</tr>；表头行用 <th>，不会被 _TD_RE 命中
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)

# 页面有效性标志：抓失败时站点返回一个很短的错误页
_PAGE_MARKER = "战报列表"


class ComparisonError(ValueError):
    """可直接展示给管理员的参数或站点错误。"""


@dataclass(frozen=True)
class ComparisonQuery:
    team: str
    site_team: str
    start: str
    end: str
    publish_start: str
    publish_end: str

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict):
            raise ComparisonError('请求格式不正确。')
        team = str(payload.get('team') or '').strip()
        site_team = str(payload.get('site_team') or team).strip()
        query = {}
        source = str(payload.get('source_url') or '').strip()
        if source:
            try:
                parsed = urlsplit(source)
                port = parsed.port
            except ValueError as exc:
                raise ComparisonError('查询地址格式不正确。') from exc
            if (parsed.scheme != 'https' or parsed.hostname != 'rep.ygobbs2.com'
                    or port not in (None, 443) or parsed.username or parsed.password
                    or parsed.path not in ('', '/', '/index.php')):
                raise ComparisonError('请使用 rep.ygobbs2.com 的 HTTPS 查询地址。')
            query = parse_qs(parsed.query, keep_blank_values=True)
            if any(len(values) != 1 for values in query.values()):
                raise ComparisonError('查询地址含重复参数。')
            if query.get('c') != ['search'] or query.get('a') != ['sub']:
                raise ComparisonError('请使用战报站的搜索结果地址。')
            if any(query.get(k, [''])[0] for k in ('username', 'playername')):
                raise ComparisonError('整队比对请清空发布者和选手筛选。')
            url_team = query.get('groupname', [''])[0].strip()
            site_team = str(payload.get('site_team') or url_team).strip()
            team = team or url_team
        for name in (team, site_team):
            if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
                raise ComparisonError('请填写有效的战队名称，最多64个字符。')
        month = str(payload.get('month') or '').strip()
        if month:
            if not re.fullmatch(r'\d{4}-\d{2}', month):
                raise ComparisonError('月份格式应为 YYYY-MM。')
            try:
                first = date.fromisoformat(month + '-01')
                start, end = str(first), str(first.replace(day=calendar.monthrange(first.year, first.month)[1]))
            except ValueError as exc:
                raise ComparisonError('月份无效。') from exc
        else:
            start = str(payload.get('start') or query.get('starttime', [''])[0])
            end = str(payload.get('end') or query.get('endtime', [''])[0])
        pub_start = str(payload.get('publish_start') or query.get('starttime', [''])[0] or start)
        pub_end = str(payload.get('publish_end') or query.get('endtime', [''])[0] or end)
        for a, b in ((start, end), (pub_start, pub_end)):
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', a + '') or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', b + ''):
                    raise ValueError
                if not 0 <= (date.fromisoformat(b) - date.fromisoformat(a)).days <= 92:
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise ComparisonError('日期范围无效，单次最多查询93天。') from exc
        return cls(team, site_team, start, end, pub_start, pub_end)

    def url(self, page=1):
        return BASE_URL + '?' + urlencode(dict(c='search', a='sub', username='', groupname=self.site_team,
                                              starttime=self.publish_start, endtime=self.publish_end,
                                              playername='', page=page))


def previous_month():
    last = datetime.now(timezone(timedelta(hours=8))).date().replace(day=1) - timedelta(days=1)
    return last.strftime('%Y-%m')


class _BodyText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        if tag == 'br' and not self.skip:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip:
            self.skip -= 1
        if tag in ('p', 'div') and not self.skip:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


class _PanelText(_BodyText):
    def __init__(self):
        super().__init__()
        self.level = 0
        self.found = False

    def handle_starttag(self, tag, attrs):
        if tag == 'div':
            if self.level:
                self.level += 1
            elif not self.found and 'panel-body' in (dict(attrs).get('class') or '').split():
                self.level, self.found = 1, True
        if self.level:
            super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.level:
            super().handle_endtag(tag)
            if tag == 'div':
                self.level -= 1

    def handle_data(self, data):
        if self.level:
            super().handle_data(data)


def _text(fragment):
    parser = _BodyText()
    parser.feed(fragment)
    return ''.join(parser.parts).strip()


def norm_content(text: str) -> str:
    """战报正文归一化，作为「内容是否完全一样」的指纹。

    统一对局分隔与行首尾，保留参赛 ID 内部空格和行序。
    """
    try:
        from .lineup import normalize_duel_spacing
    except ImportError:
        from lineup import normalize_duel_spacing
    lines = [ln.strip() for ln in normalize_duel_spacing(text).splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _extract_body(cell: str) -> str | None:
    """从正文单元格 HTML 里还原战报文本（<br> → 换行）。"""
    parser = _PanelText()
    parser.feed(cell)
    return ''.join(parser.parts).strip() if parser.found else None


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
        rid = _text(cells[0])
        if not rid.isdigit():
            continue
        body = _extract_body(cells[5])
        if body is None:
            continue
        records.append(
            {
                "id": rid,
                "winner_site": _text(cells[1]),
                "loser_site": _text(cells[2]),
                "publish_date_raw": _text(cells[3]),
                "publish_date": _normalize_pub_date(_text(cells[3])),
                "publisher": _text(cells[4]),
                "body": body,
            }
        )
    return records, total_pages


def enrich(record: dict, start: str, end: str) -> dict:
    """解析正文，补上结构化字段与胜负比对结果。"""
    result = parse_battle_report(record["body"])
    out = dict(record)
    out["content_key"] = norm_content(record["body"])
    out["parse_ok"] = result.report is not None and not result.errors
    out["parse_errors"] = result.errors
    out["parse_warnings"] = result.warnings
    if not out['parse_ok']:
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




class _SameSiteRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != 'https' or target.hostname != 'rep.ygobbs2.com' or target.port not in (None, 443):
            raise ComparisonError('战报站返回了异常跳转，本次抓取已停止。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_html(url):
    request = Request(url, headers={'User-Agent': 'AstrBot-BattleReport/1.28 (read-only comparison)',
                                    'Accept': 'text/html'})
    try:
        with build_opener(_SameSiteRedirect()).open(request, timeout=15) as response:
            if response.status != 200:
                raise ComparisonError('战报站暂不可用。')
            content = response.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise ComparisonError('战报站单页内容过大，已停止抓取。')
            charset = response.headers.get_content_charset() or 'utf-8'
            return content.decode(charset)
    except ComparisonError:
        raise
    except Exception as exc:
        raise ComparisonError('无法连接或读取战报站，请检查服务器网络后重试。') from exc


class SiteClient:
    """固定站点、限时串行抓取；不接受不完整分页作为成功结果。"""
    def __init__(self, loader=None, *, delay=0.5):
        self.loader = loader
        self.delay = delay

    async def page(self, query, number):
        for attempt in range(2):
            try:
                text = (await self.loader(query.url(number)) if self.loader
                        else await asyncio.to_thread(fetch_html, query.url(number)))
                if _PAGE_MARKER not in text or '<table' not in text.lower():
                    raise ComparisonError('战报站页面结构异常，未生成对比结果。')
                records, total = parse_page(text)
                if total is None and re.search(r'[?&](?:amp;)?page=[2-9][0-9]*', text):
                    raise ComparisonError('无法确认网站完整页数，未生成对比结果。')
                data_rows = 0
                for row in _ROW_RE.findall(text):
                    cells = _TD_RE.findall(row)
                    first = _text(cells[0]) if cells else ''
                    if first.isdigit() or (len(cells) == 6 and first.casefold()
                                            not in ('id', '编号', '序号', '战报id', '战报编号')):
                        data_rows += 1
                if data_rows != len(records):
                    raise ComparisonError('部分网站记录无法读取，未生成对比结果。')
                current = re.search(r'当前为\s*(\d+)\s*页', _text(text))
                if current and int(current[1]) != number:
                    raise ComparisonError('网站返回了错误页码，未生成对比结果。')
                if not records and not all(word in _text(text) for word in ('胜方', '负方')):
                    raise ComparisonError('无法确认网站返回的是空列表，未生成对比结果。')
                return records, total
            except ComparisonError:
                if attempt:
                    raise
                await asyncio.sleep(self.delay)

    async def crawl(self, query, progress):
        records, pages = await self.page(query, 1)
        if pages == 0 and records:
            raise ComparisonError('网站分页数量异常。')
        pages = pages or 1
        if not 1 <= pages <= 200 or (pages > 1 and not records):
            raise ComparisonError('网站分页数量异常或超过200页，请缩短查询区间。')
        size = len(records)
        seen = {r['id'] for r in records}
        if len(seen) != len(records):
            raise ComparisonError('网站分页出现重复编号，未生成对比结果。')
        total_bytes = sum(len(r['body'].encode('utf-8')) for r in records)
        if len(records) > 10000 or total_bytes > 20 * 1024 * 1024:
            raise ComparisonError('查询结果过多，请缩短日期范围。')
        await progress(1, pages, len(records))
        for number in range(2, pages + 1):
            await asyncio.sleep(self.delay)
            batch, total = await self.page(query, number)
            if total not in (None, pages) or not batch or (number < pages and len(batch) != size) or len(batch) > size:
                raise ComparisonError('抓取过程中网站分页发生变化，请重新对比。')
            if any(r['id'] in seen for r in batch) or len({r['id'] for r in batch}) != len(batch):
                raise ComparisonError('网站分页出现重复编号，未生成对比结果。')
            seen.update(r['id'] for r in batch)
            records.extend(batch)
            total_bytes += sum(len(r['body'].encode('utf-8')) for r in batch)
            if total_bytes > 20 * 1024 * 1024 or len(records) > 10000:
                raise ComparisonError('查询结果过多，请缩短日期范围。')
            await progress(number, pages, len(records))
        return [enrich(record, query.start, query.end) for record in records]
