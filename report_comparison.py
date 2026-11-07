"""线上战报与本地战报的只读比较口径，供 WebUI 与离线脚本共用。"""
from collections import defaultdict

try:
    from .battle_report_parser import KIND_FRIENDLY, BattleReport, Duel, determine_match_winner
except ImportError:
    from battle_report_parser import KIND_FRIENDLY, BattleReport, Duel, determine_match_winner

# ---------- 匹配键与归一化 ----------

def _key(match_time: str, teams, location: str) -> tuple:
    teams = tuple(sorted(t.upper() for t in teams if t))
    return (match_time, teams, (location or "").strip())


def _slug(match_time: str, teams) -> tuple:
    return _key(match_time, teams, "")[:2]


def _winner_from_duels(duels: list[dict], match: dict) -> str | None:
    """库内 winner 为空时，用 duels 复算胜方。

    战队以 duels 自带的 player_a_team/player_b_team 为准，避免 matches.team_a 与
    对局归属不一致时判错边。
    """
    team_a, team_b = match["team_a"], match["team_b"]
    if duels:
        pa_team = (duels[0].get("player_a_team") or "").upper()
        pb_team = (duels[0].get("player_b_team") or "").upper()
        if pa_team and pb_team:
            team_a, team_b = pa_team, pb_team
    rep = BattleReport(
        team_a=team_a,
        team_b=team_b,
        rule=match.get("rule") or "",
        match_time=match.get("match_time") or "",
    )
    rep.duels = [
        Duel(d["round_no"], d["player_a"], d["score_a"], d["player_b"], d["score_b"],
             a_sub=bool(d.get('a_sub')), b_sub=bool(d.get('b_sub')), ruled=bool(d.get('ruled')))
        for d in duels
    ]
    return determine_match_winner(rep)


def _canon_duel_list(rows: list[tuple], team_a: str, team_b: str) -> list[tuple]:
    """把对局列表规范化成与战队书写顺序无关的形式，并丢弃 0:0 未打的占位。

    rows 每项为 (round_no, player_a, score_a, player_b, score_b)，player_a 属 team_a。
    """
    t1, _t2 = sorted([team_a.upper(), team_b.upper()])
    out = []
    for row in rows:
        round_no, pa, sa, pb, sb = row[:5]
        a_sub, b_sub, ruled = row[5:] if len(row) > 5 else (False, False, False)
        if sa == 0 and sb == 0:
            continue
        if team_a.upper() == t1:
            out.append((round_no, pa, sa, pb, sb, a_sub, b_sub, ruled))
        else:
            out.append((round_no, pb, sb, pa, sa, b_sub, a_sub, ruled))
    return sorted(out, key=lambda row: row[0])


def _site_duels(record: dict) -> list[tuple]:
    rows = [
        (d["round_no"], d["player_a"], d["score_a"], d["player_b"], d["score_b"],
         bool(d.get('a_sub')), bool(d.get('b_sub')), bool(d.get('ruled')))
        for d in record.get("duels", [])
    ]
    return _canon_duel_list(rows, record["team_a"], record["team_b"])


def _db_duels(duels: list[dict], match: dict) -> list[tuple]:
    t_a = match["team_a"]
    rows = []
    for d in duels:
        pa, sa, pb, sb = d['player_a'], d['score_a'], d['player_b'], d['score_b']
        a_sub, b_sub = bool(d.get('a_sub')), bool(d.get('b_sub'))
        if (d.get('player_a_team') or t_a).upper() != t_a.upper():
            pa, sa, pb, sb, a_sub, b_sub = pb, sb, pa, sa, b_sub, a_sub
        rows.append((d['round_no'], pa, sa, pb, sb, a_sub, b_sub, bool(d.get('ruled'))))
    return _canon_duel_list(rows, t_a, match["team_b"])


# ---------- 取数与比对 ----------

def split_by_kind(rows: list[dict], *, include_raid: bool = False) -> tuple[list[dict], dict]:
    """按 `matches.kind` 过滤，返回 `(kept, excluded_counts)`。

    默认只留友谊战报：网站只收录友谊赛，踢馆报拿来比对只会全落进「只在库里有」。
    `kind` 为空的历史行按友谊赛处理（与建表默认值 `'friendly'` 一致）。
    """
    kept, excluded = [], defaultdict(int)
    for m in rows:
        kind = (m.get("kind") or KIND_FRIENDLY)
        if kind == KIND_FRIENDLY or include_raid:
            m["kind"] = kind
            kept.append(m)
        else:
            excluded[kind] += 1
    return kept, dict(excluded)



def _site_key(record: dict) -> tuple:
    """网站记录的**内容**指纹：时间 + 战队(无序) + 地点 + 规范化对局序列。

    不能只用 (时间, 战队, 地点) —— 地点是固定房间号，同一天同一房间会打多场，
    该键根本不唯一（实测大量不同对阵共用同一房间号）。必须以对局序列为准。
    """
    return (
        record["match_time"],
        tuple(sorted({record["team_a"].upper(), record["team_b"].upper()})),
        (record["location"] or "").strip(),
        ' '.join((record.get('rule') or '').split()),
        tuple(_site_duels(record)),
    )


def _db_key(match: dict, duels: list[dict]) -> tuple:
    return (
        match["match_time"],
        tuple(sorted({match["team_a"].upper(), match["team_b"].upper()})),
        (match["location"] or "").strip(),
        ' '.join((match.get('rule') or '').split()),
        tuple(_db_duels(duels, match)),
    )


def _session_key(match_time: str, teams, location: str) -> tuple:
    """场次键：日期 + 战队(无序) + 地点。

    这个键**不唯一**（地点是固定房间号，同一天同一房间会打好几场），
    所以它不能当去重依据，只用来提示"疑似同一场被记了两遍"。
    真正的去重依据是 _db_key（含对局序列）。
    """
    return (
        match_time,
        tuple(sorted({(t or "").upper() for t in teams})),
        (location or "").strip(),
    )


def _db_winner(match: dict, db_duels: dict) -> str:
    """库记录的胜方：优先取 matches.winner，为空则由对局复算。"""
    return (match["winner"] or "").upper() or (
        _winner_from_duels(db_duels.get(match["id"], []), match) or ""
    )


def compare(site_records: list[dict], db_matches: list[dict], db_duels: dict) -> dict:
    """按内容指纹比对，产出差异与重复。"""
    site = [r for r in site_records if r.get("parse_ok") and r.get("match_time_in_range")]
    out_of_range = [r for r in site_records if r.get("parse_ok") and not r.get("match_time_in_range")]
    parse_failed = [r for r in site_records if not r.get("parse_ok")]

    site_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in site:
        site_by_key[_site_key(r)].append(r)

    db_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for m in db_matches:
        db_by_key[_db_key(m, db_duels.get(m["id"], []))].append(m)

    missing = [r for k in site_by_key if k not in db_by_key for r in site_by_key[k]]
    only_in_db = [m for k in db_by_key if k not in site_by_key for m in db_by_key[k]]
    matched = sum(len(site_by_key[k]) for k in site_by_key if k in db_by_key)

    # 内容完全相同的多条：库内重复会直接让统计翻倍
    db_dupes = {k: v for k, v in db_by_key.items() if len(v) > 1}
    site_dupes = {k: v for k, v in site_by_key.items() if len(v) > 1}

    winner_mismatch: list[tuple] = []
    for k in set(site_by_key) & set(db_by_key):
        s = site_by_key[k][0]
        for m in db_by_key[k]:
            db_w = _db_winner(m, db_duels)
            if db_w and db_w != s["winner_site"].upper():
                winner_mismatch.append((s, m, db_w))

    # 日期/战队/地点只能聚合待核线索，不能证明是同一场或重复计数。
    # 网站发布时间范围、遗漏发布和分页时点都可能造成两边条数不同。
    site_sessions: dict[tuple, int] = defaultdict(int)
    for r in site:
        site_sessions[_session_key(r["match_time"], (r["team_a"], r["team_b"]), r["location"])] += 1

    by_session: dict[tuple, list[dict]] = defaultdict(list)
    for m in db_matches:
        by_session[_session_key(m["match_time"], (m["team_a"], m["team_b"]), m["location"])].append(m)

    session_groups: list[dict] = []
    for k, rows in sorted(by_session.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if len(rows) < 2:
            continue
        wins = {_db_winner(m, db_duels) for m in rows} - {""}
        site_n = site_sessions[k]
        session_groups.append({
            "match_time": k[0],
            "teams": list(k[1]),
            "location": k[2],
            "site_count": site_n,
            "db_exceeds_site": len(rows) > site_n,
            "winner_conflict": len(wins) > 1,
            "matches": [
                {"db_id": m["id"], "winner": _db_winner(m, db_duels),
                 "submitted_name": m["submitted_name"],
                 "duel_count": len(db_duels.get(m["id"], []))}
                for m in rows
            ],
        })

    return {
        "site_total": len(site_records),
        "site_in_range": len(site),
        "db_total": len(db_matches),
        "matched": matched,
        "missing_in_db": missing,
        "only_in_db": only_in_db,
        "winner_mismatch": winner_mismatch,
        "db_duplicate_keys": db_dupes,
        "site_duplicate_keys": site_dupes,
        "db_session_groups": session_groups,
        "site_out_of_range": out_of_range,
        "site_parse_failed": parse_failed,
    }




def build_comparison(site_records, reports, meta):
    """将只读快照转成可分页展示/导出的结果，不产生任何修复操作。"""
    try:
        from .lineup import report_to_text
    except ImportError:
        from lineup import report_to_text
    kept, excluded = split_by_kind([dict(r) for r in reports])
    matches, duels = [], {}
    for report in kept:
        match = dict(report, id=report['match_id'], raw_text='')
        match['duels'] = []
        for raw in report['duels']:
            d = dict(raw)
            if ((d.get('player_a_team') or report['team_a']).upper() != report['team_a'].upper()
                    and (d.get('player_b_team') or report['team_a']).upper() == report['team_a'].upper()):
                d['player_a'], d['player_b'] = d['player_b'], d['player_a']
                d['score_a'], d['score_b'] = d['score_b'], d['score_a']
                d['a_sub'], d['b_sub'] = bool(d.get('b_sub')), bool(d.get('a_sub'))
            d.update(player_a_team=report['team_a'], player_b_team=report['team_b'])
            match['duels'].append(d)
        match['body'] = report_to_text(match)
        matches.append(match)
        duels[match['id']] = match['duels']
    other_team = [r for r in site_records if r.get('parse_ok') and meta['site_team'].upper()
                  not in (r.get('team_a', '').upper(), r.get('team_b', '').upper())]
    other_ids = {r['id'] for r in other_team}
    allowed = [r for r in site_records if r['id'] not in other_ids]
    result = compare(allowed, matches, duels)

    def site(r):
        return dict(id=str(r['id']), team_a=r.get('team_a', ''), team_b=r.get('team_b', ''),
                    match_time=r.get('match_time', ''), rule=r.get('rule', ''),
                    location=r.get('location', ''), winner=r.get('winner_site', ''),
                    publish_date=r.get('publish_date', ''), publisher=r.get('publisher', ''),
                    body=r['body'], errors=r.get('parse_errors', []), warnings=r.get('parse_warnings', []))

    def local(r):
        return dict(id=str(r['id']), team_a=r['team_a'], team_b=r['team_b'], match_time=r['match_time'],
                    rule=r['rule'], location=r['location'], winner=_db_winner(r, duels),
                    body=r['body'], publisher=r.get('submitted_name', ''))

    categories = []
    def category(key, label, rows, note):
        categories.append(dict(key=key, label=label, count=len(rows), note=note, records=rows))

    def item(site_rows=(), local_rows=(), note=''):
        return dict(site=[site(r) for r in site_rows], local=[local(r) for r in local_rows], note=note)

    site_by, local_by = defaultdict(list), defaultdict(list)
    for r in allowed:
        if r.get('parse_ok') and r.get('match_time_in_range'):
            site_by[_site_key(r)].append(r)
    for r in matches:
        local_by[_db_key(r, duels[r['id']])].append(r)
    category('matched', '内容匹配', [item(rows, local_by[k]) for k, rows in site_by.items() if k in local_by],
             '按日期、双方战队、地点、规则及有比分的完整对局序列匹配，包含替补与判罚标记。')
    category('missing_in_db', '网站独有', [item([r]) for r in result['missing_in_db']],
             '所选网站发布范围内有记录，本地比赛范围内没有相同内容；可能存在另一版本，请结合内容差异查看。')
    category('only_in_db', '本地独有', [item(local_rows=[r]) for r in result['only_in_db']],
             '网站本次发布范围内未检出相同内容，不代表战报虚假或应当删除。')
    ss, ds = defaultdict(list), defaultdict(list)
    for r in result['missing_in_db']:
        ss[_session_key(r['match_time'], (r['team_a'], r['team_b']), r['location'])].append(r)
    for r in result['only_in_db']:
        ds[_session_key(r['match_time'], (r['team_a'], r['team_b']), r['location'])].append(r)
    category('content_differences', '内容差异候选', [item(rows, ds[k], '同日期、战队和地点，内容不同；同日可能有多场，需人工核对。')
             for k, rows in ss.items() if k in ds], '候选组不自动认定为同一场，也不会自动修改比分。')
    category('winner_mismatch', '胜负不一致', [item([s], [m], '内容匹配，但网站胜方与本地胜方不同。')
             for s, m, _ in result['winner_mismatch']], '需检查网站胜方列、原文及本地判胜；不自动覆盖任何一方。')
    category('db_duplicates', '本地重复', [item(local_rows=rows) for rows in result['db_duplicate_keys'].values()],
             '本地存在多条相同规范内容；仅列出记录编号，不自动删除。')
    category('site_duplicates', '网站重复', [item(rows) for rows in result['site_duplicate_keys'].values()],
             '网站存在多条相同规范内容，各网站编号均保留。')
    category('site_winner_mismatch', '网站胜负待核', [item([r], note='网站胜方列与原文计算结果不同。')
             for r in allowed if r.get('parse_ok') and r.get('winner_match') is False], '网站列值也可能有误，不作为真实性证明。')
    category('parse_failed', '解析失败', [item([r]) for r in result['site_parse_failed']],
             '保留原文与解析错误，不把无法解析的记录当作缺失。')
    category('out_of_range', '比赛日期越界', [item([r]) for r in result['site_out_of_range']],
             '网站按发布时间搜索，正文比赛时间可能落在其他月份；这些记录不参与本次内容匹配。')
    category('other_team', '其他战队', [item([r]) for r in other_team],
             '网站搜索可能返回相近队名，双方战队均不是指定网站队名的记录不参与比对。')
    return dict(meta=meta, summary=dict(site_total=len(site_records), site_in_range=result['site_in_range'],
                db_total=len(matches), matched_groups=categories[0]['count'], excluded=excluded,
                review_count=sum(c['count'] for c in categories[1:])), categories=categories,
                notes=['网站搜索日期是发布时间，本地按比赛日期取数。',
                       '独有记录和同日同地点的差异仅供核对，不能据此判定虚假或重复计数。',
                       '仅比较友谊战报；本次操作未修改战报、比分或发送状态。'])
