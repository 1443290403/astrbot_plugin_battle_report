"""测试使用独立连接配置，绝不继承插件的生产数据库地址。"""

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

DEFAULTS = {
    "mysql_host": os.environ.get("ASTRBOT_TEST_MYSQL_HOST", "127.0.0.1"),
    "mysql_port": int(os.environ.get("ASTRBOT_TEST_MYSQL_PORT", "3306")),
    "mysql_user": os.environ.get("ASTRBOT_TEST_MYSQL_USER", "root"),
    "mysql_password": os.environ.get("ASTRBOT_TEST_MYSQL_PASSWORD", ""),
}
