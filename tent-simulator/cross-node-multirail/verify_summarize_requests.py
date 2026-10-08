"""Independent stdlib checks; temporary input/output stays outside the repo.

Run: python3 -B verify_summarize_requests.py
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from summarize_requests import nearest_rank, summarize_requests


MS = 1_000_000
START = 9_000_000_000_000_000_000  # Preserve integer precision above 2**53.
MANIFEST = {"measurement_start_ns": START, "measurement_end_ns": START + 1000 * MS}


def request(identifier, planned, submitted, finished, size=100, status="success"):
    return {"request_id": identifier, "planned_ns": START + planned,
            "submitted_ns": START + submitted, "finished_ns": START + finished,
            "bytes": size, "status": status, "source_slot": identifier % 2}


class SummarizerChecks(unittest.TestCase):
    def test_three_second_windows_keep_boundary_and_drain_cohorts(self):
        manifest={**MANIFEST, "measurement_end_ns": START + 60_000 * MS}
        rows=[request(i, (i // 500) * 3000 * MS, (i // 500) * 3000 * MS,
                      (i // 500) * 3000 * MS + (4000 * MS if i % 500 >= 494 else MS))
              for i in range(10000)]
        summary, windows=summarize_requests(rows, manifest, 3000)
        series=windows['latency']['windows']
        self.assertEqual(len(series),20)
        self.assertTrue(all(w['duration_ns']==3000*MS and w['success_requests']==500 for w in series))
        self.assertTrue(all(w['p99_end_to_end_ns']==4000*MS for w in series))
        self.assertEqual(summary['measurement']['arrival_cohort_unfinished_at_end'],6)
        self.assertTrue(summary['conservation']['arrival_window_request_balance'])

    def test_nearest_rank(self):
        values = list(range(1, 1001))
        for percentile, expected in ((500, 500), (950, 950), (990, 990), (999, 999)):
            self.assertEqual(nearest_rank(reversed(values), percentile), expected)
        self.assertEqual(nearest_rank([9, 1], 500), 1)
        self.assertEqual(nearest_rank([9, 1], 999), 9)
        self.assertIsNone(nearest_rank([], 990))

    def test_boundary_drain_failure_and_conservation(self):
        rows = [request(0, -10 * MS, -5 * MS, 0, 10),
                request(1, 0, 10 * MS, 250 * MS, 20),
                request(2, 249 * MS, 250 * MS, 1200 * MS, 30),
                request(3, 250 * MS, 270 * MS, 300 * MS, 40, "failure"),
                request(4, 500 * MS, 500 * MS, 1000 * MS, 50),
                request(5, 1000 * MS, 1000 * MS, 1001 * MS, 60)]
        summary, windows = summarize_requests(reversed(rows), MANIFEST)
        self.assertEqual(summary["throughput"]["success_bytes"], 30)
        self.assertEqual(summary["throughput"]["bits_per_second"], 240)
        self.assertEqual(summary["measurement"]["planned"]["requests"], 4)
        self.assertEqual(summary["measurement"]["arrival_cohort_unfinished_at_end"], 2)
        self.assertEqual(summary["measurement"]["planned"]["failure_rate"], 0.25)
        self.assertEqual(summary["latency_success"]["end_to_end_ns"]["p99"], 951 * MS)
        self.assertEqual(summary["latency_success"]["queue_ns"]["p50"], MS)
        self.assertEqual(summary["latency_success"]["submission_to_finish_ns"]["p99"], 950 * MS)
        self.assertEqual(summary["latency_failure_wait"]["end_to_end_ns"]["max"], 50 * MS)
        self.assertEqual([w["success_bytes"] for w in windows["throughput"]["250ms"]], [10, 20, 0, 0])
        self.assertEqual(windows["throughput"]["250ms"][1]["failure_bytes"], 40)
        cohort = windows["latency"]["windows"][0]
        self.assertEqual(cohort["success_requests"], 2)
        self.assertEqual(cohort["unfinished_at_window_end"], 2)
        self.assertIsNone(cohort["p99_end_to_end_ns"])
        self.assertTrue(summary["conservation"]["observed_payload_balance"])
        self.assertTrue(summary["conservation"]["arrival_window_request_balance"])
        self.assertIsNone(summary["conservation"]["accepted_equals_success_failure_pending"])
        for balance in summary["conservation"]["throughput_window_balances"].values():
            self.assertTrue(all(balance.values()))

    def test_p99_success_threshold_and_cross_window_slow_requests(self):
        rows = [request(i, 0, 10, 100 if i < 494 else 2_000 * MS) for i in range(500)]
        # The 495th nearest rank must include a slow drain completion.
        summary, windows = summarize_requests(rows, MANIFEST)
        first = windows["latency"]["windows"][0]
        self.assertEqual(first["p99_end_to_end_ns"], 2_000 * MS)
        self.assertEqual(first["p99_queue_ns"], 10)
        self.assertEqual(summary["throughput"]["success_requests"], 494)
        self.assertEqual(windows["latency"]["valid_p99_coverage"], 0.25)
        rows[-1]["status"] = "failure"
        _, windows = summarize_requests(rows, MANIFEST)
        first = windows["latency"]["windows"][0]
        self.assertEqual(first["failure_requests"], 1)
        self.assertIsNone(first["p99_end_to_end_ns"])
        self.assertIsNone(first["p99_queue_ns"])

    def test_empty_and_partial_windows(self):
        manifest = {**MANIFEST, "measurement_end_ns": START + 275 * MS}
        summary, windows = summarize_requests([], manifest, 1000)
        self.assertEqual(summary["throughput"]["bits_per_second"], 0)
        self.assertIsNone(summary["latency_success"]["end_to_end_ns"]["p999"])
        self.assertIsNone(summary["observed"]["failure_rate"])
        self.assertEqual(len(windows["throughput"]["50ms"]), 6)
        _, windows = summarize_requests([request(0, 260 * MS, 260 * MS, 270 * MS, 100)], manifest)
        last = windows["throughput"]["250ms"][-1]
        self.assertEqual(last["duration_ns"], 25 * MS)
        self.assertEqual(last["bits_per_second"], 32000)

    def test_invalid_input_is_rejected(self):
        good = request(0, 0, 1, 2)
        invalid = [{**good, "finished_ns": None}, {**good, "submitted_ns": 0},
                   {**good, "bytes": -1}, {**good, "planned_ns": float(START)},
                   {**good, "bytes": True}, {**good, "status": "pending"},
                   {**good, "source_slot": []}, {"request_id": 3}, []]
        for record in invalid:
            with self.subTest(record=record), self.assertRaises(ValueError):
                summarize_requests([record], MANIFEST)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            summarize_requests([good, good], MANIFEST)
        with self.assertRaises(ValueError):
            summarize_requests([], {**MANIFEST, "measurement_end_ns": START})

    def test_cli_paths_roundtrip_and_no_overwrite(self):
        script = Path(__file__).with_name("summarize_requests.py")
        with tempfile.TemporaryDirectory(prefix="request-summary-check-") as directory:
            root = Path(directory)
            manifest, records, output = root / "manifest.json", root / "requests.jsonl", root / "report"
            manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
            records.write_text(json.dumps(request(0, 0, 1, 2)) + "\n", encoding="utf-8")
            command = [sys.executable, "-B", str(script), "--requests", str(records),
                       "--manifest", str(manifest), "--latency-window-ms", "3000", "--output-dir", str(output)]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((output / "summary.json").read_text())["observed"]["requests"], 1)
            self.assertEqual(set(json.loads((output / "windows.json").read_text())["throughput"]), {"50ms", "250ms", "1000ms"})
            saved = (output / "summary.json").read_bytes()
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((output / "summary.json").read_bytes(), saved)
            result = subprocess.run([sys.executable, "-B", str(script)], cwd=root, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            records.write_text("not JSON\n", encoding="utf-8")
            command[-1] = str(root / "invalid-report")
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requests.jsonl:1", result.stderr)
            self.assertFalse((root / "invalid-report").exists())


if __name__ == "__main__":
    unittest.main()
