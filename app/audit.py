"""执行审计：每次执行都落盘一份 JSON，记录文件指纹、条件与处理规则。"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from . import engine


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def record_run(audit_dir: str, *, left: engine.SideSpec, right: engine.SideSpec,
               join_type: str, max_output_rows: int,
               result: engine.ExecuteResult,
               requested_output: str,
               dedup_rules: Optional[Dict[str, Any]] = None,
               extra_rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """写出审计记录并返回该记录。"""
    os.makedirs(audit_dir, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    entry: Dict[str, Any] = {
        "run_id": run_id,
        "created_at": _now_iso(),
        "status": result.status,
        "note": result.note,
        "inputs": {
            "left": {
                "name": left.name,
                "path": os.path.abspath(left.path),
                "sha256": engine.sha256_file(left.path),
                "rows": len(left.rows),
                "columns": list(left.columns),
            },
            "right": {
                "name": right.name,
                "path": os.path.abspath(right.path),
                "sha256": engine.sha256_file(right.path),
                "rows": len(right.rows),
                "columns": list(right.columns),
            },
        },
        "conditions": {
            "join_type": join_type,
            "join_label": engine.JOIN_LABELS.get(join_type, join_type),
            "key_fields": {"left": list(left.all_key_fields), "right": list(right.all_key_fields)},
            "secondary_fields": {"left": list(left.secondary_fields), "right": list(right.secondary_fields)},
            "null_policy": {
                "left": {f: left.effective_null(f) for f in left.all_key_fields},
                "right": {f: right.effective_null(f) for f in right.all_key_fields},
            },
        },
        "rules": {
            "dedup": {
                "left": left.dedup_mode,
                "right": right.dedup_mode,
                "left_dropped": result.plan_stats.get("dedup", {}).get("left_dropped", 0),
                "right_dropped": result.plan_stats.get("dedup", {}).get("right_dropped", 0),
            },
            "max_output_rows": max_output_rows,
            "row_count_rule": "按实际键频次 Σ(freq_left[k]×freq_right[k])",
            **(dedup_rules or {}),
            **(extra_rules or {}),
        },
        "outcome": {
            "full_rows": result.full_rows,
            "written_rows": result.written_rows,
            "truncated": result.truncated,
            "requested_output": requested_output,
            "output_path": os.path.abspath(result.output_path) if result.output_path else None,
            "output_sha256": (engine.sha256_file(result.output_path)
                              if result.output_path and os.path.exists(result.output_path) else None),
        },
        "preview_stats": result.plan_stats,
    }
    path = os.path.join(audit_dir, f"run-{run_id}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False, indent=2)
    entry["_audit_path"] = path
    return entry


def list_runs(audit_dir: str, limit: int = 50) -> List[Dict[str, Any]]:
    if not os.path.isdir(audit_dir):
        return []
    entries: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(audit_dir), reverse=True):
        if not name.startswith("run-") or not name.endswith(".json"):
            continue
        path = os.path.join(audit_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        entries.append({
            "run_id": data.get("run_id"),
            "created_at": data.get("created_at"),
            "status": data.get("status"),
            "note": data.get("note"),
            "join_type": data.get("conditions", {}).get("join_type"),
            "full_rows": data.get("outcome", {}).get("full_rows"),
            "written_rows": data.get("outcome", {}).get("written_rows"),
            "truncated": data.get("outcome", {}).get("truncated"),
            "left": data.get("inputs", {}).get("left", {}).get("name"),
            "right": data.get("inputs", {}).get("right", {}).get("name"),
            "path": path,
        })
        if len(entries) >= limit:
            break
    return entries
