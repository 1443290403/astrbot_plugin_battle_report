"""_conf_schema.json 结构测试。

两个目的：

1. **格式**：它是 AstrBot 读的配置文件、也是 conftest 的 DEFAULTS 来源 ——
   写坏一个逗号会让**所有**数据库测试在收集阶段就崩掉，且报错信息是
   「conftest 导入失败」而非「配置有误」，很难一眼看懂。
2. **防死配置回归**：`home_team`、`default_days` 都曾是「界面有开关、代码里
   没人读」的死配置（§10-3 / §10-4）。它们已随 v1.13.0 删除，这里钉住，
   免得日后又被顺手加回来 —— 界面上的每个键都必须是代码真的在用的。
"""

import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_schema_is_valid_json_object():
    schema = _schema()
    assert isinstance(schema, dict) and schema


def test_every_entry_is_well_formed():
    """每个配置项都要有 description / type / default，否则仪表盘渲染会缺字段。"""
    for key, item in _schema().items():
        assert isinstance(item, dict), key
        assert item.get("description"), f"{key} 缺 description"
        assert item.get("type") in ("string", "int", "bool", "float", "list", "object"), \
            f"{key} 的 type 非法: {item.get('type')!r}"
        assert "default" in item, f"{key} 缺 default"
        # default 的类型要和声明的 type 对得上（bool 是 int 的子类，先排掉）
        if item["type"] == "int":
            assert isinstance(item["default"], int) and not isinstance(item["default"], bool), \
                f"{key} 声明 int 但默认值不是 int"
        elif item["type"] == "bool":
            assert isinstance(item["default"], bool), f"{key} 声明 bool 但默认值不是 bool"
        elif item["type"] == "string":
            assert isinstance(item["default"], str), f"{key} 声明 string 但默认值不是 string"


def test_dead_config_keys_stay_deleted():
    """已删除的死配置不许回来（§10-3 的 home_team、§10-4 的 default_days）。"""
    schema = _schema()
    assert "home_team" not in schema, "home_team 已随严格解析删除，别再添加"
    assert "default_days" not in schema, "default_days 是死配置，已删除"


def test_min_duels_present_and_sane():
    """min_duels（§10-6）：提交前置条件，默认 3，必须 ≥1。"""
    item = _schema().get("min_duels")
    assert item is not None, "min_duels 配置缺失"
    assert item["type"] == "int" and item["default"] >= 1
    # 描述里要说清它不是排名门槛，免得和 min_games 混为一谈
    assert "min_games" in (item.get("hint", "") + item.get("description", ""))


def test_min_games_still_present():
    """min_games 是排名门槛，与 min_duels 是两件事，别在清理时误删。"""
    assert "min_games" in _schema()
