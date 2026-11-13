"""预览与执行共用的一套配置解析，供 CLI 和 Web API 调用。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import engine

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")
AUDIT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit")


@dataclass
class TableSource:
    name: str
    path: str
    columns: List[str]
    rows: List[Dict[str, Any]]


def load_source(name: str, path: str) -> TableSource:
    columns, rows = engine.read_table(path)
    return TableSource(name=name, path=path, columns=columns, rows=rows)


def resolve_dataset(key: str) -> str:
    """把示例数据集名字解析为路径；也允许直接传文件路径。"""
    candidates = {
        "orders": os.path.join(DATA_DIR, "orders.csv"),
        "payments": os.path.join(DATA_DIR, "payments.csv"),
    }
    if key in candidates:
        return candidates[key]
    if os.path.exists(key):
        return os.path.abspath(key)
    raise engine.JoinError(f"找不到数据集：{key}")


def parse_window(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise engine.JoinError(f"数值参数无效：{raw!r}")
    return value if value > 0 else default


def build_specs(left_src: TableSource, right_src: TableSource,
                config: Dict[str, Any]) -> tuple:
    """从请求配置构造两侧 SideSpec；CLI、API 共用。"""
    left_cfg = config.get("left", {})
    right_cfg = config.get("right", {})
    left = engine.SideSpec(
        name=left_src.name,
        path=left_src.path,
        columns=list(left_src.columns),
        rows=list(left_src.rows),
        key_fields=engine._clean_fields(left_cfg.get("key_fields"), left_src.columns, f"{left_src.name}（左表）"),
        secondary_fields=engine._clean_fields(left_cfg.get("secondary_fields", []), left_src.columns,
                                              f"{left_src.name}（左表）") if left_cfg.get("secondary_fields") else [],
        dedup_mode=engine._clean_dedup(left_cfg.get("dedup_mode")),
        null_match=_null_flag(left_cfg, "null_match", False),
        null_field_override=_null_overrides(left_cfg.get("null_field_override")),
    )
    right = engine.SideSpec(
        name=right_src.name,
        path=right_src.path,
        columns=list(right_src.columns),
        rows=list(right_src.rows),
        key_fields=engine._clean_fields(right_cfg.get("key_fields"), right_src.columns, f"{right_src.name}（右表）"),
        secondary_fields=engine._clean_fields(right_cfg.get("secondary_fields", []), right_src.columns,
                                              f"{right_src.name}（右表）") if right_cfg.get("secondary_fields") else [],
        dedup_mode=engine._clean_dedup(right_cfg.get("dedup_mode")),
        null_match=_null_flag(right_cfg, "null_match", False),
        null_field_override=_null_overrides(right_cfg.get("null_field_override")),
    )
    return left, right


def _null_flag(cfg: Dict[str, Any], key: str, default: bool) -> bool:
    value = cfg.get(key)
    if value is None:
        return default
    return bool(value)


def _null_overrides(raw: Any) -> Dict[str, bool]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise engine.JoinError("null_field_override 必须是字段到布尔值的映射")
    return {str(k): bool(v) for k, v in raw.items()}


def make_plan(left: TableSource, right: TableSource, config: Dict[str, Any],
              preview_rows: int = 25, top_keys: int = 10) -> engine.Plan:
    lspec, rspec = build_specs(left, right, config)
    return engine.plan_join(lspec, rspec, config.get("join_type", "inner"),
                            preview_rows=preview_rows, top_keys=top_keys)


def default_output_path(left_name: str, right_name: str, join_type: str,
                        run_suffix: Optional[str] = None) -> str:
    suffix = run_suffix or "latest"
    return os.path.join(OUTPUT_DIR, f"{left_name}_{right_name}_{join_type}_{suffix}.csv")
