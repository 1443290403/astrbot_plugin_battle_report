import pytest

import stats


def test_opening_trend_uses_pooled_wins_and_losses_then_keeps_later_days():
    daily = [
        ("2026-11-01", 0, 0),
        ("2026-11-03", 1, 0),
        ("2026-11-08", 0, 3),
        ("2026-11-09", 0, 0),
        ("2026-11-12", 1, 0),
        ("2026-11-13", 2, 1),
    ]
    opening, later = stats.compute_trend_points(daily)
    assert opening == dict(date="2026-11-12", period_start="2026-11-03",
                           win_rate=40.0, total=5, wins=2, losses=3)
    assert later == dict(date="2026-11-13", win_rate=50.0, total=8, wins=4, losses=4)


@pytest.mark.parametrize(("day_one", "day_two", "period_start", "date", "total", "win_rate"), [
    ((6, 4), (2, 3), None, "2026-11-03", 10, 60.0),
    ((3, 2), (1, 4), "2026-11-03", "2026-11-08", 10, 40.0),
    ((3, 2), (6, 4), "2026-11-03", "2026-11-08", 15, 60.0),
])
def test_opening_trend_stops_at_ten_games_and_includes_the_entire_day(
    day_one, day_two, period_start, date, total, win_rate,
):
    points = stats.compute_trend_points([
        ("2026-11-03", *day_one),
        ("2026-11-05", 0, 0),
        ("2026-11-08", *day_two),
        ("2026-11-12", 1, 0),
    ])
    first = points[0]
    assert (first.get("period_start"), first["date"], first["total"], first["win_rate"]) == (
        period_start, date, total, win_rate,
    )
    assert [p["date"] for p in points[1:]] == (
        ["2026-11-08", "2026-11-12"] if period_start is None else ["2026-11-12"]
    )


def test_opening_trend_groups_each_month_without_resetting_the_cumulative_count():
    points = stats.compute_trend_points([
        ("2026-12-29", 5, 1),
        ("2026-12-31", 1, 4),
        ("2027-01-08", 2, 0),
        ("2027-02-03", 1, 0),
        ("2027-02-06", 0, 2),
        ("2027-02-09", 1, 0),
        ("2027-02-12", 1, 0),
    ])
    assert [(p.get("period_start"), p["date"], p["total"]) for p in points] == [
        ("2026-12-29", "2026-12-31", 11),
        (None, "2027-01-08", 13),
        ("2027-02-03", "2027-02-09", 17),
        (None, "2027-02-12", 18),
    ]
    assert points[-1]["win_rate"] == 61.1


@pytest.mark.parametrize("daily", [[], [("2026-11-03", 0, 0)]])
def test_opening_trend_without_decisive_results_has_no_points(daily):
    assert stats.compute_trend_points(daily) == []


@pytest.mark.parametrize("scale", [1, 1.4])
def test_opening_trend_labels_the_whole_group_on_the_axis(monkeypatch, scale):
    import chart
    from PIL import ImageDraw

    ranges = []
    original = ImageDraw.ImageDraw.multiline_text

    def capture(draw, xy, text, *args, **kwargs):
        ranges.append(text)
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "multiline_text", capture)
    points = stats.compute_trend_points([
        ("2026-11-03", 1, 0),
        ("2026-11-08", 0, 3),
        ("2026-11-12", 1, 0),
        ("2026-11-13", 2, 1),
    ])
    with chart._trend_image(points, "友谊累计胜率", 836, 450, scale):
        assert ranges == ["11-03～\n11-12"]
