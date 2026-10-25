"""排表模块单元测试。"""

from lineup import generate_template, parse_lineup


def test_normalize_duel_spacing_preserves_headers_names_markers_scores_and_newlines():
    from lineup import normalize_duel_spacing
    header = ('战队: a队 VS b队\r\n时间: 2026.10.25 12:30 UTC\r\n'
              '规则: 2/3【KOF】\r\n地点: 100\r\n')
    draft = (header + '------第一轮------\r\n'
             '示例  玩家(替) 2:0 萌新  遗老(规则)\r\n'
             '备注  原样保留\r\n------第二轮------\r\n'
             '云玩家\t0：0\t遗老\r\n')
    expected = (header + '------第一轮------\r\n'
                '示例  玩家(替)  2:0  萌新  遗老(规则)\r\n'
                '备注  原样保留\r\n------第二轮------\r\n'
                '云玩家  0：0  遗老\r\n')
    assert normalize_duel_spacing(draft) == expected
    assert normalize_duel_spacing(expected) == expected


def test_normalize_duel_spacing_leaves_non_report_text_and_incomplete_rows_alone():
    from lineup import normalize_duel_spacing
    for text in ['', '说明 2:0 并非轮次内容\n', '------第一轮------\n2:0\n云玩家 2:0\n']:
        assert normalize_duel_spacing(text) == text


def test_normalize_duel_spacing_does_not_change_parsed_duels():
    from lineup import normalize_duel_spacing
    from battle_report_parser import parse_battle_report
    text = ('战队: a队 VS b队\n时间: 2026.10.25\n规则: 2/3【KOF】\n地点: 100\n'
            '------第一轮------\n示例 玩家 2:0 萌新 遗老\n------第二轮------\n')
    before, after = parse_battle_report(text), parse_battle_report(normalize_duel_spacing(text))
    assert not before.errors and not after.errors
    assert before.report.duels == after.report.duels


def test_parse_lineup_ok():
    r = parse_lineup("KC:红莲 凯撒亮 悠悠球\nDYG:老千 蓝大 红大", "2/3【KOF】")
    assert not r.errors
    assert r.teams[0] == ("KC", ["红莲", "凯撒亮", "悠悠球"])
    assert r.teams[1] == ("DYG", ["老千", "蓝大", "红大"])
    assert r.rule == "2/3【KOF】"


def test_parse_lineup_with_rule():
    r = parse_lineup("人头赛\nKC:红莲\nDYG:老千", "2/3【KOF】")
    assert not r.errors
    assert r.rule == "人头赛"


def test_parse_lineup_fullwidth():
    r = parse_lineup("KC：红莲、悠悠球\nDYG：老千，蓝大", "2/3【KOF】")
    assert not r.errors
    assert r.teams[0] == ("KC", ["红莲", "悠悠球"])
    assert r.teams[1] == ("DYG", ["老千", "蓝大"])


def test_parse_lineup_errors():
    r = parse_lineup("这是一行无法识别的文本", "2/3【KOF】")
    assert r.errors


def test_parse_lineup_uppercase_team():
    # 队标含字母时统一转大写
    r = parse_lineup("dyg:老千 蓝大\nkc:红莲 悠悠球", "2/3【KOF】")
    assert not r.errors
    assert r.teams[0] == ("DYG", ["老千", "蓝大"])
    assert r.teams[1] == ("KC", ["红莲", "悠悠球"])


def test_generate_template_equal():
    gen = generate_template(
        "KC", ["红莲", "悠悠球"], "DYG", ["老千", "蓝大"],
        "2026-08-01", "2/3【KOF】", "123456789", seed="test",
    )
    assert not gen.warnings
    t = gen.template
    assert "战队: KC VS DYG" in t
    assert "时间: 2026.08.01" in t
    assert "规则: 2/3【KOF】" in t
    assert "地点: 123456789" in t
    assert "------第一轮------" in t
    assert "------第二轮------" in t
    assert t.count("0:0") == 2


def test_generate_template_fewer_players_tk():
    """一方人数较少时：正常按较多一方排满，少人一侧以 TK 占位且不提示。"""
    gen = generate_template(
        "KC", ["红莲", "悠悠球"], "DYG", ["老千", "蓝大", "红大"],
        "2026-08-01", "2/3【KOF】", "123456789", seed="test",
    )
    assert not gen.warnings
    lines = gen.template.splitlines()
    first = lines.index("------第一轮------")
    second = lines.index("------第二轮------")
    round1 = lines[first + 1:second]
    assert len(round1) == 3          # max(2,3)
    assert gen.template.count("0:0") == 3
    # 少人一侧用 TK 占位
    assert any("TK" in ln for ln in round1)
    assert "待排" not in gen.template


def test_generate_template_seeded_reproducible():
    t1 = generate_template(
        "KC", ["a", "b", "c"], "DYG", ["1", "2", "3"],
        "2026-08-01", "r", "123", seed="x",
    ).template
    t2 = generate_template(
        "KC", ["a", "b", "c"], "DYG", ["1", "2", "3"],
        "2026-08-01", "r", "123", seed="x",
    ).template
    assert t1 == t2


def test_generate_template_random_varies():
    templates = [
        generate_template(
            "KC", ["a", "b", "c", "d", "e"], "DYG", ["1", "2", "3", "4", "5"],
            "2026-08-01", "r", "123",
        ).template
        for _ in range(5)
    ]
    assert len(set(templates)) >= 2  # 5 次运行至少出现 2 种不同配对
