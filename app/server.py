"""JoinLab HTTP 服务（纯标准库）。

只暴露 JSON API + 单页前端。数据文件限定在 data/ 与 uploads/ 目录内，
避免任意路径读取。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from . import audit, config, engine

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_DIRS = (config.DATA_DIR, UPLOAD_DIR)

MAX_BODY_BYTES = 2 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _safe_dataset_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError("缺少数据集")
    value = raw.strip()
    candidates = {"orders": os.path.join(config.DATA_DIR, "orders.csv"),
                  "payments": os.path.join(config.DATA_DIR, "payments.csv")}
    if value in candidates:
        return candidates[value]
    path = os.path.abspath(value)
    if not any(path.startswith(os.path.abspath(d) + os.sep) for d in ALLOWED_DIRS):
        raise ApiError("只能读取 data/ 或 uploads/ 下的文件")
    if not os.path.isfile(path):
        raise ApiError(f"文件不存在：{value}", 404)
    return path


def _load(raw: Any, label: str) -> config.TableSource:
    path = _safe_dataset_path(raw)
    name = os.path.splitext(os.path.basename(path))[0]
    return config.load_source(f"{name}", path)


def _window(body: Dict[str, Any], key: str, default: int, cap: int) -> int:
    value = config.parse_window(body.get(key, default), default)
    return min(value, cap)


def api_datasets() -> Dict[str, Any]:
    items = []
    for key in ("orders", "payments"):
        path = os.path.join(config.DATA_DIR, f"{key}.csv")
        columns, rows = engine.read_table(path)
        items.append({"id": key, "name": key, "path": path, "columns": columns,
                      "rows": len(rows), "sample": rows[:3]})
    return {"datasets": items}


def api_preview(body: Dict[str, Any]) -> Dict[str, Any]:
    left = _load(body.get("left", {}).get("dataset", "orders"), "left")
    right = _load(body.get("right", {}).get("dataset", "payments"), "right")
    preview_rows = _window(body, "preview_rows", engine.DEFAULT_PREVIEW_ROWS, 100)
    top_keys = _window(body, "top_keys", engine.DEFAULT_TOP_KEYS, 50)
    plan = config.make_plan(left, right, body, preview_rows=preview_rows, top_keys=top_keys)
    return {
        "stats": plan.stats,
        "columns": {"left": left.columns, "right": right.columns},
        "sources": {"left": left.name, "right": right.name},
    }


def api_key_detail(body: Dict[str, Any]) -> Dict[str, Any]:
    left = _load(body.get("left", {}).get("dataset", "orders"), "left")
    right = _load(body.get("right", {}).get("dataset", "payments"), "right")
    plan = config.make_plan(left, right, body)
    limit = _window(body, "detail_limit", engine.DEFAULT_DETAIL_LIMIT, 500)
    if "key_raw" not in body:
        raise ApiError("缺少 key_raw")
    return engine.key_detail(plan, body["key_raw"], limit=limit)


def api_execute(body: Dict[str, Any]) -> Dict[str, Any]:
    left = _load(body.get("left", {}).get("dataset", "orders"), "left")
    right = _load(body.get("right", {}).get("dataset", "payments"), "right")
    if not body.get("confirmed"):
        raise ApiError("执行前需要确认预览结果（confirmed=true）")
    max_rows = _window(body, "max_output_rows", engine.DEFAULT_MAX_OUTPUT_ROWS, 1_000_000)
    plan = config.make_plan(left, right, body)
    join_type = body.get("join_type", "inner")
    name = body.get("output_name") or ""
    if name and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise ApiError("输出文件名只能包含字母、数字、下划线、点和短横线")
    output_path = os.path.join(config.OUTPUT_DIR,
                               name if name else os.path.basename(
                                   config.default_output_path(left.name, right.name, join_type)))
    allow_truncate = bool(body.get("allow_truncate", False))
    result = engine.execute_join(plan, output_path, max_output_rows=max_rows,
                                 allow_truncate=allow_truncate)
    run = audit.record_run(
        config.AUDIT_DIR, left=plan.left.spec, right=plan.right.spec,
        join_type=join_type, max_output_rows=max_rows, result=result,
        requested_output=output_path,
        dedup_rules={
            "left_secondary_fields": list(plan.left.spec.secondary_fields),
            "right_secondary_fields": list(plan.right.spec.secondary_fields),
        },
    )
    return {
        "status": result.status,
        "note": result.note,
        "full_rows": result.full_rows,
        "written_rows": result.written_rows,
        "truncated": result.truncated,
        "columns": result.columns,
        "preview": result.preview,
        "output_path": result.output_path,
        "audit": run,
        "stats": result.plan_stats,
    }


def api_runs() -> Dict[str, Any]:
    return {"runs": audit.list_runs(config.AUDIT_DIR)}


def api_run_detail(run_id: str) -> Dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", run_id):
        raise ApiError("无效的运行 ID")
    path = os.path.join(config.AUDIT_DIR, f"run-{run_id}.json")
    if not os.path.isfile(path):
        raise ApiError("找不到该运行记录", 404)
    with open(path, "r", encoding="utf-8") as fh:
        return {"run": json.load(fh)}


class Handler(BaseHTTPRequestHandler):
    server_version = "JoinLab/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("[joincraft] " + fmt % args + "\n")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, message: str, status: int) -> None:
        self._send_json({"error": message}, status)

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError("请求体过大", 413)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("请求体不是合法 JSON")
        if not isinstance(value, dict):
            raise ApiError("请求体必须是 JSON 对象")
        return value

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = os.path.abspath(os.path.join(STATIC_DIR, relative))
        if not target.startswith(os.path.abspath(STATIC_DIR) + os.sep) or not os.path.isfile(target):
            self._send_error("文件不存在", 404)
            return
        content_type = "text/html; charset=utf-8" if target.endswith(".html") else "text/plain; charset=utf-8"
        if target.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif target.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        with open(target, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/api/datasets":
                self._send_json(api_datasets())
            elif path == "/api/runs":
                self._send_json(api_runs())
            elif path.startswith("/api/runs/"):
                self._send_json(api_run_detail(path[len("/api/runs/"):]))
            elif path.startswith("/api/"):
                self._send_error("未知接口", 404)
            else:
                self._serve_static(path)
        except ApiError as exc:
            self._send_error(str(exc), exc.status)
        except engine.JoinError as exc:
            self._send_error(str(exc), 400)
        except Exception:  # noqa: BLE001
            self._send_error("服务内部错误", 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urllib.parse.urlparse(self.path).path
            body = self._read_body()
            if path == "/api/preview":
                self._send_json(api_preview(body))
            elif path == "/api/key-detail":
                self._send_json(api_key_detail(body))
            elif path == "/api/execute":
                self._send_json(api_execute(body))
            else:
                self._send_error("未知接口", 404)
        except ApiError as exc:
            self._send_error(str(exc), exc.status)
        except engine.JoinError as exc:
            self._send_error(str(exc), 400)
        except Exception:  # noqa: BLE001
            self._send_error("服务内部错误", 500)


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    os.makedirs(config.AUDIT_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "127.0.0.1")
    httpd = make_server(host, port)
    print(f"JoinLab 已启动：http://{host}:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
