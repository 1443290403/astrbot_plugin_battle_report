"""pytest 公共配置：把插件根目录加入 sys.path，并从 _conf_schema.json 读取默认配置。

MySQL 密码通过环境变量 ASTRBOT_TEST_MYSQL_PASSWORD 提供（避免把凭据写进代码/仓库）；
未设置时回退到配置默认值（通常为空 → 数据库测试会跳过）。
"""

import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def _load_defaults() -> dict:
    schema = json.loads(
        (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    )
    defaults = {key: item.get("default") for key, item in schema.items()}
    defaults["mysql_password"] = os.environ.get(
        "ASTRBOT_TEST_MYSQL_PASSWORD", defaults.get("mysql_password", "")
    )
    return defaults


DEFAULTS = _load_defaults()
