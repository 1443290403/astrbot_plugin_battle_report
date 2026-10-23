"""测试使用独立连接配置，绝不继承插件的生产数据库地址。"""

import os
import sys
import json
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

DEFAULTS = {
    "mysql_host": os.environ.get("ASTRBOT_TEST_MYSQL_HOST", "127.0.0.1"),
    "mysql_port": int(os.environ.get("ASTRBOT_TEST_MYSQL_PORT", "3306")),
    "mysql_user": os.environ.get("ASTRBOT_TEST_MYSQL_USER", "root"),
    "mysql_password": os.environ.get("ASTRBOT_TEST_MYSQL_PASSWORD", ""),
}


@pytest.fixture
def qq_invite_card():
    def make(sequence=765, group=30003, app='com.tencent.qun.invite', when=100, json_text=None, **patch):
        card = {'app': app, 'meta': {'news': {
            'jumpUrl': f'mqqapi://group/invite_join?groupcode={group}&msgseq={sequence}'}}}
        return {'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                'self_id': 12345, 'user_id': 20002, 'time': when,
                'message': [{'type': 'json', 'data': {'data': json.dumps(card) if json_text is None else json_text}}],
                **patch}
    return make
