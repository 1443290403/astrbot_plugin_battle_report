from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from command_guard import is_command_message


class Plain:
    def __init__(self, text):
        self.text = text


@pytest.mark.parametrize('text,expected', [
    ('/未知指令 参数', True), ('  /未知\n参数', True), ('/', True),
    ('点歌 示例歌曲', True), ('选歌\t1', True), ('音乐 播放 示例歌曲', True),
    ('音乐\n播放\t示例歌曲', True), ('查歌词', True),
    ('点歌很好玩', False), ('帮我点歌 示例歌曲', False),
    ('这段 /代码 应该怎么写', False), ('chat 你好', False),
    ('你好', False), ('1', False), ('https://example.com/点歌', False),
    ('', False), ('  \n ', False),
])
def test_command_intent_has_whole_command_boundaries_without_chat_prefix(text, expected):
    event = SimpleNamespace(get_messages=lambda: [], get_message_str=lambda: text)
    names = Mock(return_value=['点歌', '选歌', '音乐 播放', '查歌词', '', None])
    assert is_command_message(event, Plain, names) is expected
    if text.lstrip().startswith('/'):
        names.assert_not_called()


def test_only_current_top_level_plain_is_used_without_quoted_command():
    parts = [SimpleNamespace(message_str='/点歌 示例歌曲'), Plain('你好')]
    event = SimpleNamespace(get_messages=lambda: parts, get_message_str=lambda: '/点歌 示例歌曲')
    assert not is_command_message(event, Plain, lambda: ['点歌'])
    assert parts[1].text == '你好'


def test_fragmented_plain_and_mentions_preserve_original_slash():
    parts = [SimpleNamespace(qq='10001'), Plain(' /'), Plain('未知指令 参数')]
    event = SimpleNamespace(get_messages=lambda: parts, get_message_str=lambda: '未知指令 参数')
    assert is_command_message(event, Plain, lambda: [])
    assert [p.text for p in parts if isinstance(p, Plain)] == [' /', '未知指令 参数']
