"""关联引擎测试：行数规则、空值、复合键、去重、上限与可追溯性。"""

import csv
import json
import os
import tempfile
import unittest

from app import engine


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def spec(path, key_fields, **kwargs):
    columns, rows = engine.read_table(path)
    return engine.SideSpec(
        name=os.path.basename(path),
        path=path,
        columns=columns,
        rows=rows,
        key_fields=list(key_fields),
        **kwargs,
    )


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.dir, name)


class RowCountRules(EngineCase):
    def test_one_to_one(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k", "v"], [["a", 1], ["b", 2]])
        write_csv(right, ["k", "w"], [["a", 10], ["b", 20], ["c", 30]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        self.assertEqual(plan.stats["rows"]["matched"], 2)
        self.assertEqual(plan.stats["rows"]["result"], 2)
        self.assertEqual(plan.stats["keys"]["cardinality"], "one_to_one")
        self.assertEqual(plan.stats["rows"]["left_unmatched"], 0)
        self.assertEqual(plan.stats["rows"]["right_unmatched"], 1)

    def test_one_to_many_uses_frequency_product(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k"], [["a"]])
        write_csv(right, ["k"], [["a"], ["a"], ["a"]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        self.assertEqual(plan.stats["rows"]["matched"], 3)
        self.assertEqual(plan.stats["keys"]["cardinality"], "one_to_many")

    def test_many_to_many_multiplies(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k"], [["a"], ["a"], ["b"]])
        write_csv(right, ["k"], [["a"], ["a"], ["a"], ["b"]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        # a: 2*3=6, b: 1*1=1
        self.assertEqual(plan.stats["rows"]["matched"], 7)
        self.assertEqual(plan.stats["keys"]["breakdown"]["many_to_many"], 1)
        top = plan.stats["expansion"]["top_keys"][0]
        self.assertEqual((top["left_count"], top["right_count"], top["output_rows"]), (2, 3, 6))

    def test_preview_matches_execution_row_count(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k", "v"], [["a", 1], ["a", 2], ["b", 3]])
        write_csv(right, ["k", "w"], [["a", 10], ["b", 20], ["b", 30], ["z", 40]])
        for join_type in engine.JOIN_TYPES:
            ls = spec(left, ["k"])
            rs = spec(right, ["k"])
            plan = engine.plan_join(ls, rs, join_type)
            actual = sum(1 for _ in engine.iter_join_rows(plan))
            self.assertEqual(actual, plan.stats["rows"]["result"],
                             f"{join_type}: 预览 {plan.stats['rows']['result']} vs 实际 {actual}")

    def test_left_join_keeps_left_unmatched(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k"], [["a"], ["x"]])
        write_csv(right, ["k"], [["a"], ["y"]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "left")
        self.assertEqual(plan.stats["rows"]["result"], 2)
        rows = list(engine.iter_join_rows(plan))
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(r["_right_row"] == "" for r in rows))

    def test_full_join_counts_both_sides(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k"], [["a"], ["x"]])
        write_csv(right, ["k"], [["a"], ["y"]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "full")
        self.assertEqual(plan.stats["rows"]["result"], 3)
        self.assertEqual(len(list(engine.iter_join_rows(plan))), 3)


class NullPolicy(EngineCase):
    def _data(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k"], [[""], ["a"]])
        write_csv(right, ["k"], [[""], ["a"]])
        return left, right

    def test_null_not_matched_by_default(self):
        left, right = self._data()
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        self.assertEqual(plan.stats["rows"]["matched"], 1)
        self.assertEqual(plan.stats["unmatched"]["left_null_key"], 1)
        self.assertEqual(plan.stats["unmatched"]["right_null_key"], 1)
        rows = list(engine.iter_join_rows(plan))
        self.assertEqual(len(rows), 1)

    def test_null_matched_when_explicitly_enabled(self):
        left, right = self._data()
        plan = engine.plan_join(
            spec(left, ["k"], null_match=True),
            spec(right, ["k"], null_match=True), "inner")
        self.assertEqual(plan.stats["rows"]["matched"], 2)
        self.assertEqual(plan.stats["unmatched"]["left_null_key"], 0)

    def test_null_per_field_override(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["a", "b"], [["x", ""]])
        write_csv(right, ["a", "b"], [["x", ""]])
        plan = engine.plan_join(
            spec(left, ["a", "b"], null_field_override={"b": True}),
            spec(right, ["a", "b"], null_field_override={"b": True}),
            "inner")
        self.assertEqual(plan.stats["rows"]["matched"], 1)

    def test_null_unmatched_kept_in_full_join(self):
        left, right = self._data()
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "full")
        rows = list(engine.iter_join_rows(plan))
        self.assertEqual(len(rows), plan.stats["rows"]["result"])
        self.assertEqual(plan.stats["rows"]["result"], 3)


class CompositeKeys(EngineCase):
    def test_no_string_collision(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        # ("A|B","C") 与 ("A","B|C") 在拼接方案下会碰撞
        write_csv(left, ["x", "y"], [["A|B", "C"]])
        write_csv(right, ["x", "y"], [["A", "B|C"]])
        plan = engine.plan_join(spec(left, ["x", "y"]), spec(right, ["x", "y"]), "inner")
        self.assertEqual(plan.stats["rows"]["matched"], 0)
        self.assertEqual(plan.stats["rows"]["left_unmatched"], 1)
        self.assertEqual(plan.stats["rows"]["right_unmatched"], 1)

    def test_composite_matches_when_all_fields_equal(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["x", "y"], [["A", "B"], ["A", "C"]])
        write_csv(right, ["x", "y"], [["A", "B"], ["A", "C"], ["A", "D"]])
        plan = engine.plan_join(spec(left, ["x", "y"]), spec(right, ["x", "y"]), "inner")
        self.assertEqual(plan.stats["rows"]["matched"], 2)
        self.assertEqual(plan.stats["rows"]["right_unmatched"], 1)

    def test_secondary_field_extends_key(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k", "s"], [["a", "1"], ["a", "2"]])
        write_csv(right, ["k", "s"], [["a", "2"]])
        base = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        extended = engine.plan_join(
            spec(left, ["k"], secondary_fields=["s"]),
            spec(right, ["k"], secondary_fields=["s"]), "inner")
        self.assertEqual(base.stats["rows"]["matched"], 2)
        self.assertEqual(extended.stats["rows"]["matched"], 1)

    def test_empty_string_vs_missing_is_same_null(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k", "s"], [["a", ""]])
        write_csv(right, ["k", "s"], [["a", ""]])
        plan = engine.plan_join(spec(left, ["k", "s"]), spec(right, ["k", "s"]), "inner",
                                )
        self.assertEqual(plan.stats["rows"]["matched"], 0)

    def test_mismatched_field_count_rejected(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["a", "b"], [["1", "2"]])
        write_csv(right, ["a", "b"], [["1", "2"]])
        with self.assertRaises(engine.JoinError):
            engine.plan_join(spec(left, ["a", "b"]), spec(right, ["a"]), "inner")


class DedupRules(EngineCase):
    def test_dedup_reduces_inflation(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k", "v"], [["a", 1], ["a", 2]])
        write_csv(right, ["k", "w"], [["a", 10], ["a", 20]])
        raw = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        dedup = engine.plan_join(
            spec(left, ["k"], dedup_mode="key"),
            spec(right, ["k"], dedup_mode="key"), "inner")
        self.assertEqual(raw.stats["rows"]["matched"], 4)
        self.assertEqual(dedup.stats["rows"]["matched"], 1)
        self.assertEqual(dedup.stats["dedup"]["left_dropped"], 1)
        self.assertEqual(dedup.stats["dedup"]["right_dropped"], 1)

    def test_dedup_row_is_not_counted_as_unmatched(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k"], [["a"], ["a"]])
        write_csv(right, ["k"], [["a"]])
        plan = engine.plan_join(
            spec(left, ["k"], dedup_mode="key"), spec(right, ["k"]), "left")
        self.assertEqual(plan.stats["rows"]["left_unmatched"], 0)
        self.assertEqual(plan.stats["rows"]["result"], 1)

    def test_amount_dedup_would_lose_valid_rows_but_key_dedup_does_not(self):
        # 金额相同但业务记录不同的样本：按金额去重会误删，按关联键不会。
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k", "amount"], [["a", 100], ["b", 100]])
        write_csv(right, ["k", "amount"], [["a", 100], ["b", 100]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        self.assertEqual(plan.stats["rows"]["matched"], 2)


class OutputLimits(EngineCase):
    def _big(self, n=50):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k"], [["a"]] * n)
        write_csv(right, ["k"], [["a"]] * n)
        return left, right

    def test_over_limit_returns_limited_preview_without_file(self):
        left, right = self._big(30)
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        out = self.path("out.csv")
        result = engine.execute_join(plan, out, max_output_rows=100)
        self.assertEqual(result.status, "limited")
        self.assertEqual(result.written_rows, 0)
        self.assertIsNone(result.output_path)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(result.full_rows, 900)
        self.assertIn("900", result.note)

    def test_explicit_truncate_marks_partial(self):
        left, right = self._big(30)
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        out = self.path("out.csv")
        result = engine.execute_join(plan, out, max_output_rows=100, allow_truncate=True)
        self.assertEqual(result.status, "partial")
        self.assertTrue(result.truncated)
        self.assertEqual(result.written_rows, 100)
        self.assertEqual(result.full_rows, 900)
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 101)  # 表头 + 100

    def test_within_limit_writes_complete_file(self):
        left, right = self._big(5)
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        out = self.path("out.csv")
        result = engine.execute_join(plan, out)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.written_rows, 25)
        with open(out, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 25)


class Traceability(EngineCase):
    def test_result_rows_point_back_to_both_sides(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k", "lv"], [["a", "L1"], ["b", "L2"]])
        write_csv(right, ["k", "rv"], [["a", "R1"], ["a", "R2"]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        rows = list(engine.iter_join_rows(plan))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["left.k"], "a")
            self.assertEqual(row["left.lv"], "L1")
            self.assertEqual(row["_left_row"], 1)
            self.assertIn(row["_right_row"], (1, 2))
        # 行号能反查原始文件
        columns, raw = engine.read_table(right)
        self.assertEqual(raw[0]["rv"], "R1")
        self.assertEqual(raw[1]["rv"], "R2")

    def test_key_detail_shows_both_sides(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k", "lv"], [["a", "L1"], ["a", "L2"]])
        write_csv(right, ["k", "rv"], [["a", "R1"]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        top = plan.stats["expansion"]["top_keys"][0]
        detail = engine.key_detail(plan, top["key_raw"])
        self.assertEqual(detail["left_count"], 2)
        self.assertEqual(detail["right_count"], 1)
        self.assertEqual(detail["output_rows"], 2)
        self.assertEqual([r["lv"] for r in detail["left_rows"]], ["L1", "L2"])
        self.assertEqual([r["rv"] for r in detail["right_rows"]], ["R1"])

    def test_key_detail_rejects_malformed_key(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k"], [["a"]])
        write_csv(right, ["k"], [["a"]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        with self.assertRaises(engine.JoinError):
            engine.key_detail(plan, "a")
        with self.assertRaises(engine.JoinError):
            engine.key_detail(plan, [["x", "a"]])

    def test_output_has_row_provenance_columns(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k"], [["a"]])
        write_csv(right, ["k"], [["a"]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        cols = engine.output_columns(plan.left.spec.columns, plan.right.spec.columns)
        self.assertEqual(cols[:3], ["_left_row", "_right_row", "_key"])


class Reading(EngineCase):
    def test_duplicate_headers_are_disambiguated(self):
        path = self.path("dup.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("k,k,\n")
            fh.write("1,2,3\n")
        columns, rows = engine.read_table(path)
        self.assertEqual(len(columns), len(set(columns)))
        self.assertEqual(rows[0][columns[0]], "1")
        self.assertEqual(rows[0][columns[1]], "2")

    def test_tsv_sniffing(self):
        path = self.path("t.tsv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("k\tv\n1\tx\n")
        columns, rows = engine.read_table(path)
        self.assertEqual(columns, ["k", "v"])
        self.assertEqual(rows[0]["v"], "x")

    def test_whitespace_is_trimmed(self):
        left = self.path("l.csv")
        right = self.path("r.csv")
        write_csv(left, ["k"], [[" a "]])
        write_csv(right, ["k"], [["a"]])
        plan = engine.plan_join(spec(left, ["k"]), spec(right, ["k"]), "inner")
        self.assertEqual(plan.stats["rows"]["matched"], 1)


if __name__ == "__main__":
    unittest.main()
