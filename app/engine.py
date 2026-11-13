"""双表关联引擎（纯标准库）。

关键保证
--------
1. 预览（plan）与执行（iter_rows）共用 ``build_side``：空值规则、去重规则、
   复合键规则只有一份实现，不会出现“预览一套、执行一套”。
2. 复合键是带类型标记的字段元组，不是字符串拼接，因此
   (region="A|B", code="C") 与 (region="A", code="B|C") 不会碰撞。
3. 行数按实际键频次计算：matched = Σ(freq_left[k] × freq_right[k])。
4. 空值匹配策略显式传入。False 时键含空值的行不参与匹配，全部计入未匹配。
5. 结果超过上限时只返回受限预览并报告完整规模；只有显式 allow_truncate
   才写截断文件，且状态标记为 partial。
"""

from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

JOIN_TYPES = ("inner", "left", "right", "full")
JOIN_LABELS = {"inner": "内连接", "left": "左连接", "right": "右连接", "full": "全连接"}

DEFAULT_MAX_OUTPUT_ROWS = 200_000
DEFAULT_PREVIEW_ROWS = 25
DEFAULT_TOP_KEYS = 10
DEFAULT_DETAIL_LIMIT = 200

# 键分量类型标记，避免真实值撞上内部占位符。
_V = "v"
_N = "n"

KeyPart = Tuple[str, str]
Key = Tuple[KeyPart, ...]


class JoinError(Exception):
    """可直接展示给用户的配置/数据错误。"""


# ---------------------------------------------------------------------------
# 读取 / 写出 / 文件指纹
# ---------------------------------------------------------------------------


def _sniff_delimiter(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            sample = fh.read(65536)
    except OSError as exc:
        raise JoinError(f"无法读取文件：{path}") from exc
    if not sample.strip():
        raise JoinError(f"文件为空：{path}")
    first = sample.splitlines()[0]
    best = max([",", "\t", ";", "|"], key=lambda d: first.count(d))
    return best if first.count(best) else ","


def _unique_headers(headers: Sequence[str]) -> List[Tuple[str, str]]:
    """把原始表头映射为唯一、可读的列名，保留 原始名 -> 列名 的对应。"""
    used: set = set()
    mapping: List[Tuple[str, str]] = []
    for index, raw in enumerate(headers, start=1):
        clean = (raw or "").strip()
        if not clean:
            clean = f"_col{index}"
        base = clean
        suffix = 1
        while clean in used:
            suffix += 1
            clean = f"{base}__{suffix}"
        used.add(clean)
        mapping.append((raw, clean))
    return mapping


def read_table(path: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """读取 CSV/TSV。每行附加 ``_row``（数据行号，从 1 开始）。

    按列位置读取，因此重复表头（如两列都叫 ``k``）不会互相覆盖，
    重名列会被重命名为 ``k`` / ``k__2``。
    """
    delimiter = _sniff_delimiter(path)
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh, delimiter=delimiter)
            try:
                header = next(reader)
            except StopIteration:
                raise JoinError(f"文件缺少表头：{path}")
            if not any((h or "").strip() for h in header):
                raise JoinError(f"文件缺少表头：{path}")
            mapping = _unique_headers(header)
            columns = [clean for _, clean in mapping]
            rows: List[Dict[str, Any]] = []
            index = 0
            for raw in reader:
                if not raw:
                    continue  # 跳过物理空行；显式空值行（"" 或 ,,）保留
                index += 1
                row: Dict[str, Any] = {}
                for position, (_, clean) in enumerate(mapping):
                    row[clean] = raw[position] if position < len(raw) else ""
                row["_row"] = index
                rows.append(row)
    except UnicodeDecodeError as exc:
        raise JoinError(f"文件不是 UTF-8 编码：{path}") from exc
    except OSError as exc:
        raise JoinError(f"无法读取文件：{path}") from exc
    return columns, rows


def write_table(path: str, columns: Sequence[str], rows: Iterable[Dict[str, Any]]) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
            count += 1
    return count


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 配置 / 键
# ---------------------------------------------------------------------------


def normalize_value(value: Any) -> str:
    """裁剪两端空白；不折叠大小写、不转数值，避免悄悄合并不同业务值。"""
    if value is None:
        return ""
    return str(value).strip()


@dataclass
class SideSpec:
    name: str
    path: str
    columns: List[str]
    rows: List[Dict[str, Any]]
    key_fields: List[str]
    secondary_fields: List[str] = field(default_factory=list)
    dedup_mode: str = "none"  # none | key（每个键保留最早的行）
    null_match: bool = False
    null_field_override: Dict[str, bool] = field(default_factory=dict)

    @property
    def all_key_fields(self) -> List[str]:
        extra = [f for f in self.secondary_fields if f not in self.key_fields]
        return self.key_fields + extra

    def effective_null(self, name: str) -> bool:
        return bool(self.null_field_override.get(name, self.null_match))


@dataclass
class BuiltSide:
    spec: SideSpec
    fields: List[str]
    buckets: Dict[Key, List[Dict[str, Any]]]
    keys_by_index: List[Optional[Key]]
    kept: List[bool]          # 进入匹配池（未被去重丢弃）
    null_blocked: List[bool]  # 键含空值且该字段不允许空值匹配
    null_rows: List[Dict[str, Any]]
    dropped: int

    @property
    def kept_rows(self) -> List[Dict[str, Any]]:
        return [row for row, keep in zip(self.spec.rows, self.kept) if keep]


def _clean_fields(fields: Optional[Sequence[str]], columns: Sequence[str], label: str) -> List[str]:
    if not fields:
        raise JoinError(f"{label}：至少选择一个关联字段")
    unknown = [f for f in fields if f not in columns]
    if unknown:
        raise JoinError(f"{label}：字段不存在：{', '.join(unknown)}")
    out: List[str] = []
    for f in fields:
        if f not in out:
            out.append(f)
    return out


def _clean_dedup(mode: Optional[str]) -> str:
    mode = (mode or "none").strip().lower()
    if mode not in ("none", "key"):
        raise JoinError(f"不支持的去重规则：{mode}")
    return mode


def build_side(spec: SideSpec) -> BuiltSide:
    """按复合键聚合，并应用去重与空值规则。预览与执行都走这里。"""
    fields = spec.all_key_fields
    buckets: Dict[Key, List[Dict[str, Any]]] = {}
    keys_by_index: List[Optional[Key]] = [None] * len(spec.rows)
    kept: List[bool] = [False] * len(spec.rows)
    null_blocked: List[bool] = [False] * len(spec.rows)
    null_rows: List[Dict[str, Any]] = []
    dropped = 0
    seen: Dict[Key, int] = {}

    for index, row in enumerate(spec.rows):
        parts: List[KeyPart] = []
        blocked = False
        for name in fields:
            value = normalize_value(row.get(name))
            if value == "":
                if not spec.effective_null(name):
                    blocked = True
                    break
                parts.append((_N, ""))
            else:
                parts.append((_V, value))
        if blocked:
            null_blocked[index] = True
            null_rows.append(row)
            continue
        key: Key = tuple(parts)
        keys_by_index[index] = key
        if spec.dedup_mode == "key" and key in seen:
            dropped += 1
            continue
        seen[key] = index
        kept[index] = True
        buckets.setdefault(key, []).append(row)
    return BuiltSide(spec, fields, buckets, keys_by_index, kept, null_blocked, null_rows, dropped)


# ---------------------------------------------------------------------------
# 键的序列化 / 展示
# ---------------------------------------------------------------------------


def serialize_key(key: Key) -> List[List[str]]:
    return [[kind, value] for kind, value in key]


def parse_key(raw: Any) -> Key:
    if not isinstance(raw, list) or not raw:
        raise JoinError("键格式无效")
    parts: List[KeyPart] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise JoinError("键格式无效：每个分量必须是 [类型, 值]")
        kind, value = item
        if kind == _N:
            parts.append((_N, ""))
        elif kind == _V:
            if value is None or str(value) == "":
                raise JoinError("键格式无效：值不能为空串")
            parts.append((_V, str(value)))
        else:
            raise JoinError("键格式无效：未知分量类型")
    return tuple(parts)


def key_values(key: Key, fields: Sequence[str]) -> Dict[str, Optional[str]]:
    return {name: (None if part[0] == _N else part[1])
            for name, part in zip(fields, key)}


def key_label(values: Dict[str, Optional[str]], fields: Sequence[str]) -> str:
    return " · ".join(
        f"{name}={values[name] if values[name] is not None else '（空）'}"
        for name in fields
    )


def key_display(key: Key, fields: Sequence[str]) -> Dict[str, Any]:
    values = key_values(key, fields)
    return {"fields": list(fields), "values": values, "label": key_label(values, fields)}


# ---------------------------------------------------------------------------
# 预览 / 规模
# ---------------------------------------------------------------------------


def _coerce_join_type(value: Optional[str]) -> str:
    value = (value or "inner").strip().lower()
    if value not in JOIN_TYPES:
        raise JoinError(f"不支持的关联方式：{value}")
    return value


@dataclass
class Plan:
    left: BuiltSide
    right: BuiltSide
    join_type: str
    stats: Dict[str, Any]

    @property
    def result_rows(self) -> int:
        return int(self.stats["rows"]["result"])


def _row_sample(row: Dict[str, Any], fields: Sequence[str], row_field: str,
                columns: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {row_field: row.get("_row")}
    for f in (columns if columns is not None else fields):
        out[f] = row.get(f, "")
    return out


def plan_join(left: SideSpec, right: SideSpec, join_type: str,
              preview_rows: int = DEFAULT_PREVIEW_ROWS,
              top_keys: int = DEFAULT_TOP_KEYS) -> Plan:
    join_type = _coerce_join_type(join_type)
    lb = build_side(left)
    rb = build_side(right)
    lfields, rfields = lb.fields, rb.fields
    if len(lfields) != len(rfields):
        raise JoinError(
            f"两侧关联字段数量不一致：左 {len(lfields)} 个（{', '.join(lfields)}），"
            f"右 {len(rfields)} 个（{', '.join(rfields)}）。键按字段位置比较，"
            f"请两侧选择相同数量的字段。"
        )

    matched_keys = set(lb.buckets) & set(rb.buckets)
    left_only = set(lb.buckets) - set(rb.buckets)
    right_only = set(rb.buckets) - set(lb.buckets)

    matched_rows = sum(len(lb.buckets[k]) * len(rb.buckets[k]) for k in matched_keys)
    left_only_rows = sum(len(lb.buckets[k]) for k in left_only)
    right_only_rows = sum(len(rb.buckets[k]) for k in right_only)

    left_unmatched = left_only_rows + len(lb.null_rows)
    right_unmatched = right_only_rows + len(rb.null_rows)

    if join_type == "inner":
        result_rows = matched_rows
        result_left_unmatched = result_right_unmatched = 0
    elif join_type == "left":
        result_rows = matched_rows + left_unmatched
        result_left_unmatched, result_right_unmatched = left_unmatched, 0
    elif join_type == "right":
        result_rows = matched_rows + right_unmatched
        result_left_unmatched, result_right_unmatched = 0, right_unmatched
    else:
        result_rows = matched_rows + left_unmatched + right_unmatched
        result_left_unmatched, result_right_unmatched = left_unmatched, right_unmatched

    counts = {"one_to_one": 0, "one_to_many": 0, "many_to_one": 0, "many_to_many": 0}
    for k in matched_keys:
        lf, rf = len(lb.buckets[k]), len(rb.buckets[k])
        if lf == 1 and rf == 1:
            counts["one_to_one"] += 1
        elif lf == 1:
            counts["one_to_many"] += 1
        elif rf == 1:
            counts["many_to_one"] += 1
        else:
            counts["many_to_many"] += 1

    if counts["many_to_many"]:
        cardinality = "many_to_many"
    elif counts["one_to_many"] or counts["many_to_one"]:
        cardinality = "one_to_many"
    else:
        cardinality = "one_to_one"

    def uniqueness(built: BuiltSide) -> Dict[str, Any]:
        kept = sum(len(v) for v in built.buckets.values())
        unique_keys = sum(1 for v in built.buckets.values() if len(v) == 1)
        distinct = len(built.buckets)
        return {
            "kept_rows": kept,
            "distinct_keys": distinct,
            "unique_keys": unique_keys,
            "duplicate_keys": distinct - unique_keys,
            "unique_rate": (unique_keys / distinct) if distinct else 0.0,
            "is_unique": kept == distinct,
        }

    top: List[Dict[str, Any]] = []
    for k in matched_keys:
        lf, rf = len(lb.buckets[k]), len(rb.buckets[k])
        top.append({
            "key": key_display(k, lfields),
            "key_raw": serialize_key(k),
            "left_count": lf,
            "right_count": rf,
            "output_rows": lf * rf,
            "extra_rows": lf * rf - 1,
            "inflates": lf * rf > max(lf, rf),
            "many_to_many": lf > 1 and rf > 1,
        })
    top.sort(key=lambda r: (r["output_rows"], r["left_count"] + r["right_count"]), reverse=True)

    extra_rows = sum(max(0, len(lb.buckets[k]) * len(rb.buckets[k]) - 1) for k in matched_keys)

    def unmatched_samples(built: BuiltSide, only_keys: set, limit: int) -> List[Dict[str, Any]]:
        rows = [r for k in only_keys for r in built.buckets[k]] + list(built.null_rows)
        rows.sort(key=lambda r: r.get("_row", 0))
        return [_row_sample(r, built.fields, "row", built.spec.columns) for r in rows[:limit]]

    stats: Dict[str, Any] = {
        "join_type": join_type,
        "join_label": JOIN_LABELS[join_type],
        "fields": {"left": list(lfields), "right": list(rfields)},
        "field_pairs": [
            {"position": i + 1, "left": lf, "right": rf, "same_name": lf == rf}
            for i, (lf, rf) in enumerate(zip(lfields, rfields))
        ],
        "rows": {
            "left": len(left.rows),
            "right": len(right.rows),
            "matched": matched_rows,
            "left_unmatched": left_unmatched,
            "right_unmatched": right_unmatched,
            "result": result_rows,
            "matched_keys": len(matched_keys),
        },
        "result_unmatched": {"left": result_left_unmatched, "right": result_right_unmatched},
        "keys": {
            "matched": len(matched_keys),
            "left": uniqueness(lb),
            "right": uniqueness(rb),
            "cardinality": cardinality,
            "breakdown": counts,
        },
        "unmatched": {
            "left": left_unmatched,
            "right": right_unmatched,
            "left_null_key": len(lb.null_rows),
            "right_null_key": len(rb.null_rows),
        },
        "dedup": {
            "left_mode": left.dedup_mode,
            "right_mode": right.dedup_mode,
            "left_dropped": lb.dropped,
            "right_dropped": rb.dropped,
        },
        "expansion": {
            "extra_rows": extra_rows,
            "many_to_many_keys": counts["many_to_many"],
            "top_keys": top[:max(1, top_keys)],
        },
        "samples": {
            "unmatched_left": unmatched_samples(lb, left_only, preview_rows),
            "unmatched_right": unmatched_samples(rb, right_only, preview_rows),
        },
        "null_policy": {
            "left": {f: left.effective_null(f) for f in lfields},
            "right": {f: right.effective_null(f) for f in rfields},
        },
    }
    return Plan(lb, rb, join_type, stats)


# ---------------------------------------------------------------------------
# 行迭代（执行与写出共用）
# ---------------------------------------------------------------------------


def output_columns(left_columns: Sequence[str], right_columns: Sequence[str]) -> List[str]:
    return (["_left_row", "_right_row", "_key"]
            + [f"left.{c}" for c in left_columns]
            + [f"right.{c}" for c in right_columns])


def _compose(lrow: Optional[Dict[str, Any]], rrow: Optional[Dict[str, Any]],
             lkey: Optional[Key], lb: BuiltSide, rb: BuiltSide) -> Dict[str, Any]:
    if lkey is None:
        key_label_text = ""
    elif lrow is not None:
        key_label_text = key_label(key_values(lkey, lb.fields), lb.fields)
    elif len(lb.fields) == len(rb.fields):
        # 右表单侧行：键值来自右表，用右表字段名展示，避免标签误导。
        key_label_text = key_label(key_values(lkey, rb.fields), rb.fields)
    else:
        key_label_text = key_label(key_values(lkey, lb.fields), lb.fields)
    out: Dict[str, Any] = {
        "_left_row": "" if lrow is None else lrow.get("_row"),
        "_right_row": "" if rrow is None else rrow.get("_row"),
        "_key": key_label_text,
    }
    for col in lb.spec.columns:
        out[f"left.{col}"] = "" if lrow is None else lrow.get(col, "")
    for col in rb.spec.columns:
        out[f"right.{col}"] = "" if rrow is None else rrow.get(col, "")
    return out


def iter_join_rows(plan: Plan) -> Iterator[Dict[str, Any]]:
    """按左表原始行序产出匹配组合，再补右表独有行。

    规则与 plan_join 完全一致：
    - 空值被排除的行（null_blocked）在左/全连接中作为左表未匹配行输出，
      在右/全连接中作为右表未匹配行输出；
    - 去重丢弃的行不进入结果，只计入 dedup 统计；
    - 键不匹配的行按同样方式归入未匹配。
    """
    lb, rb = plan.left, plan.right
    keep_left_unmatched = plan.join_type in ("left", "full")
    keep_right_unmatched = plan.join_type in ("right", "full")
    matched_left_keys = set(lb.buckets) & set(rb.buckets)

    for index, lrow in enumerate(lb.spec.rows):
        lkey = lb.keys_by_index[index]
        if lb.null_blocked[index]:
            if keep_left_unmatched:
                yield _compose(lrow, None, None, lb, rb)
            continue
        if not lb.kept[index]:
            continue
        rights = rb.buckets.get(lkey, []) if lkey is not None else []
        for rrow in rights:
            yield _compose(lrow, rrow, lkey, lb, rb)
        if keep_left_unmatched and not rights:
            yield _compose(lrow, None, lkey, lb, rb)

    if keep_right_unmatched:
        for index, rrow in enumerate(rb.spec.rows):
            if rb.null_blocked[index]:
                yield _compose(None, rrow, None, lb, rb)
                continue
            if not rb.kept[index]:
                continue
            rkey = rb.keys_by_index[index]
            if rkey is not None and rkey in matched_left_keys:
                continue  # 已在配对阶段产出
            yield _compose(None, rrow, rkey, lb, rb)


def _take(iterator: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in iterator:
        out.append(item)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 单键展开
# ---------------------------------------------------------------------------


def key_detail(plan: Plan, raw_key: Any, limit: int = DEFAULT_DETAIL_LIMIT) -> Dict[str, Any]:
    key = parse_key(raw_key)
    if len(key) != len(plan.left.fields):
        raise JoinError("键长度与关联字段数量不一致")
    lrows = plan.left.buckets.get(key, [])
    rrows = plan.right.buckets.get(key, [])
    return {
        "key": key_display(key, plan.left.fields),
        "key_raw": serialize_key(key),
        "left_count": len(lrows),
        "right_count": len(rrows),
        "output_rows": len(lrows) * len(rrows),
        "left_rows": [_row_view(r, plan.left.spec.columns, "row") for r in lrows[:limit]],
        "right_rows": [_row_view(r, plan.right.spec.columns, "row") for r in rrows[:limit]],
        "left_truncated": len(lrows) > limit,
        "right_truncated": len(rrows) > limit,
        "left_total": len(lrows),
        "right_total": len(rrows),
    }


def _row_view(row: Dict[str, Any], columns: Sequence[str], row_field: str) -> Dict[str, Any]:
    view: Dict[str, Any] = {row_field: row.get("_row")}
    for col in columns:
        view[col] = row.get(col, "")
    return view


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


@dataclass
class ExecuteResult:
    status: str  # completed | limited | partial
    full_rows: int
    written_rows: int
    truncated: bool
    columns: List[str]
    preview: List[Dict[str, Any]]
    output_path: Optional[str]
    plan_stats: Dict[str, Any]
    note: str


def execute_join(plan: Plan, output_path: str,
                 max_output_rows: int = DEFAULT_MAX_OUTPUT_ROWS,
                 preview_rows: int = DEFAULT_PREVIEW_ROWS,
                 allow_truncate: bool = False) -> ExecuteResult:
    full_rows = plan.result_rows
    columns = output_columns(plan.left.spec.columns, plan.right.spec.columns)
    iterator = iter_join_rows(plan)

    if full_rows > max_output_rows and not allow_truncate:
        preview = _take(iterator, preview_rows)
        return ExecuteResult(
            status="limited", full_rows=full_rows, written_rows=0, truncated=False,
            columns=columns, preview=preview, output_path=None, plan_stats=plan.stats,
            note=(f"完整结果 {full_rows} 行，超过上限 {max_output_rows} 行；"
                  f"未写出任何文件，此处仅展示前 {len(preview)} 行受限预览。"),
        )

    limit = max_output_rows if (allow_truncate and full_rows > max_output_rows) else None
    preview: List[Dict[str, Any]] = []
    written = 0
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in iterator:
            if limit is not None and written >= limit:
                break
            if len(preview) < preview_rows:
                preview.append(row)
            writer.writerow({c: row.get(c, "") for c in columns})
            written += 1

    if full_rows > max_output_rows:
        return ExecuteResult(
            status="partial", full_rows=full_rows, written_rows=written, truncated=True,
            columns=columns, preview=preview, output_path=output_path, plan_stats=plan.stats,
            note=(f"完整结果 {full_rows} 行，超过上限 {max_output_rows} 行；"
                  f"已按授权写出前 {written} 行，该文件为截断文件。"),
        )
    return ExecuteResult(
        status="completed", full_rows=full_rows, written_rows=written, truncated=False,
        columns=columns, preview=preview, output_path=output_path, plan_stats=plan.stats,
        note=f"已写出完整结果 {written} 行。",
    )
