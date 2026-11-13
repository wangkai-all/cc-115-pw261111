#!/usr/bin/env python3
"""命令行：与页面完全相同的预览 / 执行规则。

用法：
    python3 cli.py preview --config sample.json
    python3 cli.py execute --config sample.json --out outputs/result.csv
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import audit, config, engine


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve(cfg, key):
    spec = cfg.get(key) or {}
    dataset = spec.get("dataset")
    if not dataset:
        return None
    return config.resolve_dataset(dataset)


def main(argv=None):
    parser = argparse.ArgumentParser(description="JoinLab 命令行")
    parser.add_argument("action", choices=["preview", "execute"])
    parser.add_argument("--config", required=True, help="JSON 配置，结构与 /api/preview 请求体一致")
    parser.add_argument("--out", help="执行时的输出文件")
    parser.add_argument("--allow-truncate", action="store_true", help="超过上限时写出截断文件")
    parser.add_argument("--max-rows", type=int, default=engine.DEFAULT_MAX_OUTPUT_ROWS)
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        left_path = resolve(cfg, "left")
        right_path = resolve(cfg, "right")
        if not left_path or not right_path:
            print("错误：配置中缺少 left/right.dataset", file=sys.stderr)
            return 2
        left = config.load_source(os.path.splitext(os.path.basename(left_path))[0], left_path)
        right = config.load_source(os.path.splitext(os.path.basename(right_path))[0], right_path)
        plan = config.make_plan(left, right, cfg)
    except engine.JoinError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.action == "preview":
        if args.json:
            print(json.dumps(plan.stats, ensure_ascii=False, indent=2))
            return 0
        rows = plan.stats["rows"]
        unmatched = plan.stats["unmatched"]
        print(f"关联方式：{plan.stats['join_label']}  字段：{plan.stats['fields']['left']}")
        print(f"行数：左 {rows['left']} / 右 {rows['right']} → 结果 {rows['result']}")
        print(f"匹配 {rows['matched']} 行；未匹配 左 {unmatched['left']} / 右 {unmatched['right']}")
        print(f"键：匹配 {plan.stats['keys']['matched']} 个；基数 "
              f"{plan.stats['keys']['cardinality']}；分解 {plan.stats['keys']['breakdown']}")
        print("膨胀最多的键：")
        for item in plan.stats["expansion"]["top_keys"]:
            print(f"  {item['key']['label']}  {item['left_count']}×{item['right_count']}"
                  f" = {item['output_rows']} 行")
        return 0

    out = args.out or config.default_output_path(left.name, right.name, cfg.get("join_type", "inner"))
    result = engine.execute_join(plan, out, max_output_rows=args.max_rows,
                                 allow_truncate=args.allow_truncate)
    audit.record_run(config.AUDIT_DIR, left=plan.left.spec, right=plan.right.spec,
                     join_type=cfg.get("join_type", "inner"), max_output_rows=args.max_rows,
                     result=result, requested_output=out)
    print(result.note)
    if args.json:
        print(json.dumps({"status": result.status, "full_rows": result.full_rows,
                          "written_rows": result.written_rows,
                          "output_path": result.output_path}, ensure_ascii=False, indent=2))
    return 0 if result.status != "limited" else 3


if __name__ == "__main__":
    raise SystemExit(main())
