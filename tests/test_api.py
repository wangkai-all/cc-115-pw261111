"""API 与审计测试：使用临时目录和真实 HTTP 服务。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from app import audit, config, engine, server


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.make_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def base_payload(self, **overrides):
        payload = {
            "left": {"dataset": "orders", "key_fields": ["region", "code"]},
            "right": {"dataset": "payments", "key_fields": ["region", "code"]},
            "join_type": "inner",
        }
        payload.update(overrides)
        return payload


class DatasetApi(ServerCase):
    def test_datasets_listed(self):
        data = self.get("/api/datasets")
        ids = {d["id"] for d in data["datasets"]}
        self.assertEqual(ids, {"orders", "payments"})
        for dataset in data["datasets"]:
            self.assertIn("columns", dataset)
            self.assertGreater(dataset["rows"], 0)

    def test_static_index_served(self):
        with urllib.request.urlopen(self.url("/"), timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
        self.assertIn("JoinLab", body)

    def test_unknown_dataset_rejected(self):
        status, body = self.post("/api/preview", self.base_payload(
            left={"dataset": "/etc/passwd", "key_fields": ["x"]}))
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_missing_key_field_rejected(self):
        status, body = self.post("/api/preview", self.base_payload(
            left={"dataset": "orders", "key_fields": ["nope"]}))
        self.assertEqual(status, 400)


class PreviewApi(ServerCase):
    def test_preview_reports_rows_keys_and_unmatched(self):
        status, data = self.post("/api/preview", self.base_payload())
        self.assertEqual(status, 200)
        stats = data["stats"]
        self.assertEqual(stats["rows"]["left"], 9)
        self.assertEqual(stats["rows"]["right"], 10)
        self.assertEqual(stats["rows"]["result"],
                         sum(k["output_rows"] for k in stats["expansion"]["top_keys"]
                             if k["left_count"] and k["right_count"]))
        self.assertIn("cardinality", stats["keys"])
        self.assertIn("unmatched_left", stats["samples"])

    def test_null_policy_changes_counts(self):
        _, off = self.post("/api/preview", self.base_payload())
        payload = self.base_payload()
        payload["left"]["null_field_override"] = {"region": True, "code": True}
        payload["right"]["null_field_override"] = {"region": True, "code": True}
        _, on = self.post("/api/preview", payload)
        self.assertGreater(on["stats"]["rows"]["matched"], off["stats"]["rows"]["matched"])
        self.assertEqual(on["stats"]["unmatched"]["left_null_key"], 0)

    def test_dedup_compare(self):
        _, before = self.post("/api/preview", self.base_payload())
        payload = self.base_payload()
        payload["left"]["dedup_mode"] = "key"
        payload["right"]["dedup_mode"] = "key"
        _, after = self.post("/api/preview", payload)
        self.assertLessEqual(after["stats"]["rows"]["result"], before["stats"]["rows"]["result"])
        self.assertGreater(after["stats"]["dedup"]["left_dropped"], 0)

    def test_key_detail_via_api(self):
        _, data = self.post("/api/preview", self.base_payload())
        top = data["stats"]["expansion"]["top_keys"][0]
        status, detail = self.post("/api/key-detail", self.base_payload(key_raw=top["key_raw"]))
        self.assertEqual(status, 200)
        self.assertEqual(detail["left_count"], top["left_count"])
        self.assertEqual(detail["right_count"], top["right_count"])
        self.assertTrue(detail["left_rows"])
        self.assertTrue(detail["right_rows"])


class ExecuteApi(ServerCase):
    def test_execute_requires_confirmation(self):
        status, body = self.post("/api/execute", self.base_payload())
        self.assertEqual(status, 400)
        self.assertIn("confirmed", body["error"])

    def test_execute_writes_file_and_audit(self):
        payload = self.base_payload(confirmed=True, output_name="api_test.csv",
                                    max_output_rows=100000)
        status, data = self.post("/api/execute", payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "completed")
        self.assertTrue(os.path.exists(data["output_path"]))
        self.assertEqual(data["written_rows"], data["full_rows"])
        self.assertEqual(data["written_rows"], len(list(engine.iter_join_rows(
            config.make_plan(config.load_source("orders", config.resolve_dataset("orders")),
                             config.load_source("payments", config.resolve_dataset("payments")),
                             payload)))))

        run = data["audit"]
        self.assertIn("sha256", run["inputs"]["left"])
        self.assertIn("key_fields", run["conditions"])
        self.assertIn("null_policy", run["conditions"])
        self.assertIn("dedup", run["rules"])
        self.assertEqual(run["outcome"]["full_rows"], data["full_rows"])

        listed = self.get("/api/runs")["runs"]
        self.assertTrue(any(r["run_id"] == run["run_id"] for r in listed))
        detail = self.get(f"/api/runs/{run['run_id']}")['run']
        self.assertEqual(detail["rules"]["max_output_rows"], 100000)

    def test_over_limit_does_not_write_silently(self):
        payload = self.base_payload(confirmed=True, output_name="limit_test.csv",
                                    max_output_rows=1)
        status, data = self.post("/api/execute", payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "limited")
        self.assertEqual(data["written_rows"], 0)
        self.assertIsNone(data["output_path"])
        self.assertFalse(os.path.exists(os.path.join(config.OUTPUT_DIR, "limit_test.csv")))

    def test_bad_output_name_rejected(self):
        payload = self.base_payload(confirmed=True, output_name="../evil.csv")
        status, body = self.post("/api/execute", payload)
        self.assertEqual(status, 400)


class AuditRecords(unittest.TestCase):
    def test_record_contains_provenance_and_rule(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        left = config.load_source("orders", config.resolve_dataset("orders"))
        right = config.load_source("payments", config.resolve_dataset("payments"))
        payload = {
            "left": {"key_fields": ["region", "code"]},
            "right": {"key_fields": ["region", "code"]},
            "join_type": "inner",
        }
        plan = config.make_plan(left, right, payload)
        out = os.path.join(tmp.name, "out.csv")
        result = engine.execute_join(plan, out, max_output_rows=100000)
        run = audit.record_run(tmp.name, left=plan.left.spec, right=plan.right.spec,
                               join_type="inner", max_output_rows=100000,
                               result=result, requested_output=out)
        self.assertEqual(run["inputs"]["left"]["sha256"],
                         engine.sha256_file(config.resolve_dataset("orders")))
        self.assertEqual(run["outcome"]["output_sha256"], engine.sha256_file(out))
        self.assertEqual(run["rules"]["row_count_rule"],
                         "按实际键频次 Σ(freq_left[k]×freq_right[k])")
        self.assertEqual(run["conditions"]["null_policy"]["left"],
                         {"region": False, "code": False})


if __name__ == "__main__":
    unittest.main()
