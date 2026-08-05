"""帮助分类与渲染单元测试。"""

from stats import ALL_SECTIONS, CHAT_TYPE_SECTIONS, HELP_SECTIONS, render_help


def test_chat_type_sections():
    assert CHAT_TYPE_SECTIONS["友谊群"] == ["排表", "追加轮次", "记录比分"]
    assert CHAT_TYPE_SECTIONS["战报群"] == ["提交战报", "管理", "查询"]
    assert CHAT_TYPE_SECTIONS["主群"] == ["查询", "用户与参赛ID"]


def test_all_sections_excludes_super():
    assert "超级管理" not in ALL_SECTIONS
    assert set(ALL_SECTIONS) == set(HELP_SECTIONS) - {"超级管理"}


def test_render_help_group_sections():
    text = render_help(["排表", "查询"])
    assert "▎排表" in text
    assert "▎查询" in text
    assert "默认本月" in text  # 查询栏目说明
    assert "群属性：/群聊属性" in text
    assert "/帮助 全部" in text
    assert "▎超级管理" not in text


def test_render_help_super():
    text = render_help(["超级管理"])
    assert "▎超级管理" in text
    assert "群聊属性" in text
    assert "▎排表" not in text
