"""Read-only daily friendly wins, scoped to one team's locally uploaded reports."""
DAILY_WIN_LIMIT = 3


def daily_friendly_limits(rows, team, *, only_exceeded=True, offset=0, limit=30):
    """Combine SQL counts across groups and reversed A/B sides before paging."""
    home = team.strip().upper()
    groups = {}
    skipped = 0
    for row in rows:
        count = int(row['match_count'])
        a, b, winner = (str(row.get(k) or '').strip() for k in ('team_a', 'team_b', 'winner'))
        ak, bk, wk = a.upper(), b.upper(), winner.upper()
        if not ak or not bk or ak == bk or home not in (ak, bk) or wk not in (ak, bk):
            skipped += count
            continue
        opponent = b if ak == home else a
        day = str(row['match_time'])
        key = (day, opponent.upper())
        group = groups.setdefault(key, dict(date=day, team=team, opponent=opponent,
                                           wins=0, opponent_wins=0))
        group['wins' if wk == home else 'opponent_wins'] += count
    records = sorted(groups.values(), key=lambda r: (r['date'], r['opponent'].upper()), reverse=True)
    for row in records:
        row.update(excess_wins=max(0, row['wins'] - DAILY_WIN_LIMIT),
                   opponent_excess_wins=max(0, row['opponent_wins'] - DAILY_WIN_LIMIT),
                   total=row['wins'] + row['opponent_wins'])
        row['exceeded'] = bool(row['excess_wins'] or row['opponent_excess_wins'])
    violations = [r for r in records if r['exceeded']]
    summary = dict(reports=sum(r['total'] for r in records), skipped_reports=skipped,
                   days=len({r['date'] for r in records}), pair_days=len(records),
                   exceeded_days=len({r['date'] for r in violations}), exceeded_pairs=len(violations),
                   excess_wins=sum(r['excess_wins'] + r['opponent_excess_wins'] for r in violations))
    selected = violations if only_exceeded else records
    return dict(summary=summary, daily_limit=DAILY_WIN_LIMIT, total=len(selected),
                records=selected[offset:offset + limit], offset=offset, limit=limit,
                only_exceeded=only_exceeded)
