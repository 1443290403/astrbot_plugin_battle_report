"""pytest 公共配置：把插件根目录加入 sys.path，并从 _conf_schema.json 读取默认配置。"""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def _load_defaults() -> dict:
    schema = json.loads(
        (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    )
    return {key: item.get("default") for key, item in schema.items()}


DEFAULTS = _load_defaults()
