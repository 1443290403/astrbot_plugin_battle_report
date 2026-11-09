"""Daily limits are directional, across groups and independent of A/B layout."""
from datetime import date

from friendly_audit import daily_friendly_limits


def row(a='a队', b='b队', winner='a队', count=1, day='2026-11-01'):
    return dict(team_a=a, team_b=b, winner=winner, match_count=count, match_time=day)


def test_each_team_has_three_wins_instead_of_three_matches_combined():
    data = daily_friendly_limits([row(count=3), row(winner='b队', count=3)], 'a队')
    assert data['summary']['reports'] == 6
    assert data['summary']['exceeded_pairs'] == data['summary']['excess_wins'] == 0
    assert data['records'] == []
    all_rows = daily_friendly_limits([row(count=3), row(winner='b队', count=3)], 'a队', only_exceeded=False)
    assert all_rows['records'][0]['wins'] == all_rows['records'][0]['opponent_wins'] == 3


def test_reversed_sides_case_and_cross_group_counts_merge_without_duel_counting():
    rows = [row(count=2), row(a='B队', b='A队', winner=' a队 ', count=2), row(winner='B队', count=5),
            row(b='c队', count=3), row(day='2026-11-02', count=3)]
    data = daily_friendly_limits(rows, 'A队')
    assert data['summary'] == dict(reports=15, skipped_reports=0, days=2, pair_days=3,
                                  exceeded_days=1, exceeded_pairs=1, excess_wins=3)
    match = data['records'][0]
    assert match['wins'] == 4 and match['opponent_wins'] == 5
    assert match['excess_wins'] == 1 and match['opponent_excess_wins'] == 2
    assert match['date'] == '2026-11-01'


def test_unknown_winners_nonparticipants_and_invalid_pairs_are_reported_as_skipped():
    rows = [row(count=4), row(winner='', count=2), row(winner='c队'), row(a='c队', b='d队'),
            row(b='a队'), row(b=''), row(a='', b='a队', winner='a队')]
    data = daily_friendly_limits(rows, 'a队')
    assert data['summary']['reports'] == 4 and data['summary']['skipped_reports'] == 7
    assert data['summary']['exceeded_pairs'] == 1


def test_pagination_follows_full_month_aggregation_and_keeps_entire_summary():
    rows = [row(day=date(2026, 11, day), count=4) for day in range(1, 31)]
    rows += [row(b='c队', count=5), row(b='d队', count=2)]
    first = daily_friendly_limits(rows, 'a队', limit=30)
    second = daily_friendly_limits(rows, 'a队', offset=30, limit=30)
    assert first['total'] == second['total'] == 31
    assert len(first['records']) == 30 and len(second['records']) == 1
    assert first['summary'] == second['summary']
    assert first['summary']['reports'] == 127 and first['summary']['excess_wins'] == 32
    assert first['records'][0]['date'] == '2026-11-30'
    assert daily_friendly_limits(rows, 'a队', only_exceeded=False)['total'] == 32


def test_empty_month_has_zero_summary_and_no_violations():
    result = daily_friendly_limits([], 'a队')
    assert result['total'] == 0 and result['records'] == []
    assert all(value == 0 for value in result['summary'].values())
