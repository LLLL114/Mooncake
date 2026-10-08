#!/usr/bin/env python3
"""Summarize terminal request records from one host's monotonic clock.

CLI (all paths are required; use a data directory outside the source tree):
    python3 summarize_requests.py --requests /tmp/run/requests.jsonl \
        --manifest /tmp/run/manifest.json --output-dir /tmp/run/report

Each JSONL object requires request_id, planned_ns, submitted_ns, finished_ns,
bytes, status (success/failure), and source_slot. IDs and slots are nonempty
strings or nonnegative integers. Times/bytes are nonnegative integer values;
planned_ns <= submitted_ns <= finished_ns. Duplicate request IDs are errors.
Extra fields are ignored. The manifest requires integer measurement_start_ns
and measurement_end_ns, with end > start. Invalid input is never skipped.

All intervals are [start, end), anchored to measurement_start_ns. Throughput
uses successful completion times, including arrivals before measurement.
Latency uses planned arrivals during measurement, including drain completions
after measurement. Successful latency quantiles are conditional on success;
failure wait times are reported separately. Slots are buffer IDs, not rails.
Only terminal records are accepted: absent/pending requests, acceptance counts,
timeouts versus other failures, and trace loss cannot be inferred from them.

The input must be a finished export including drain, not a live partial file.
Existing outputs are refused. Neither importing nor running without explicit
CLI paths writes data. Only the Python standard library is used.
"""

import argparse
import json
from pathlib import Path


THROUGHPUT_WINDOWS_NS = (50_000_000, 250_000_000, 1_000_000_000)
MIN_P99_SUCCESSES = 500
QUANTILES = {"p50": 500, "p95": 950, "p99": 990, "p999": 999}


def nearest_rank(values, per_mille):
    """Return rank ceil(n * per_mille / 1000), or None for no samples."""
    if type(per_mille) is not int or not 1 <= per_mille <= 1000:
        raise ValueError("per_mille must be an integer from 1 to 1000")
    ordered = sorted(values)
    return ordered[(len(ordered) * per_mille + 999) // 1000 - 1] if ordered else None


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def read_requests(path):
    """Yield JSON objects from a UTF-8 JSONL file; reject blank/invalid lines."""
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                yield json.loads(line)
            except ValueError as error:
                raise ValueError(f"{path}:{line_number}: {error}") from error


def _validate_record(record, row_number):
    prefix = f"request row {row_number}"
    if not isinstance(record, dict):
        raise ValueError(f"{prefix} must be a JSON object")
    required = ("request_id", "planned_ns", "submitted_ns", "finished_ns",
                "bytes", "status", "source_slot")
    for name in required:
        if name not in record:
            raise ValueError(f"{prefix}: missing {name}")
    for name in ("request_id", "source_slot"):
        value = record[name]
        if not ((type(value) is int and value >= 0)
                or (isinstance(value, str) and value.strip())):
            raise ValueError(f"{prefix}: {name} must be a nonempty string or nonnegative integer")
    for name in ("planned_ns", "submitted_ns", "finished_ns", "bytes"):
        _integer(record[name], f"{prefix}: {name}")
    if not record["planned_ns"] <= record["submitted_ns"] <= record["finished_ns"]:
        raise ValueError(f"{prefix}: require planned_ns <= submitted_ns <= finished_ns")
    if record["status"] not in ("success", "failure"):
        raise ValueError(f"{prefix}: status must be success or failure")


def _totals(records):
    successes = [r for r in records if r["status"] == "success"]
    failures = [r for r in records if r["status"] == "failure"]
    return {
        "requests": len(records),
        "bytes": sum(r["bytes"] for r in records),
        "success_requests": len(successes),
        "success_bytes": sum(r["bytes"] for r in successes),
        "failure_requests": len(failures),
        "failure_bytes": sum(r["bytes"] for r in failures),
        "observed_pending_requests": 0,
        "failure_rate": len(failures) / len(records) if records else None,
    }


def _quantiles(values):
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        **{name: ordered[(len(ordered) * p + 999) // 1000 - 1] if ordered else None
           for name, p in QUANTILES.items()},
        "max": ordered[-1] if ordered else None,
    }


def _latencies(records):
    return {
        "end_to_end_ns": _quantiles([r["finished_ns"] - r["planned_ns"] for r in records]),
        "submission_to_finish_ns": _quantiles(
            [r["finished_ns"] - r["submitted_ns"] for r in records]),
        "queue_ns": _quantiles([r["submitted_ns"] - r["planned_ns"] for r in records]),
    }


def _buckets(records, time_field, start, end, width):
    buckets = [[] for _ in range((end - start + width - 1) // width)]
    for record in records:
        timestamp = record[time_field]
        if start <= timestamp < end:
            buckets[(timestamp - start) // width].append(record)
    for index, bucket in enumerate(buckets):
        left = start + index * width
        yield {"index": index, "start_ns": left, "end_ns": min(left + width, end),
               "duration_ns": min(width, end - left)}, bucket


def _throughput(successes, duration_ns):
    payload_bytes = sum(r["bytes"] for r in successes)
    return {
        "success_requests": len(successes),
        "success_bytes": payload_bytes,
        "bits_per_second": payload_bytes * 8 * 1_000_000_000 / duration_ns,
        "requests_per_second": len(successes) * 1_000_000_000 / duration_ns,
    }


def summarize_requests(records, manifest, latency_window_ms=250):
    """Return (summary, windows) dictionaries; no file writes.

    records is an iterable of decoded JSON objects. latency_window_ms must be
    250, 1000, 3000, or 5000, as preselected for the experiment. All records are kept
    in memory for exact quantiles. ValueError indicates invalid input.
    """
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    start = _integer(manifest.get("measurement_start_ns"), "measurement_start_ns")
    end = _integer(manifest.get("measurement_end_ns"), "measurement_end_ns")
    if end <= start:
        raise ValueError("measurement_end_ns must exceed measurement_start_ns")
    if type(latency_window_ms) is not int or latency_window_ms not in (250, 1000, 3000, 5000):
        raise ValueError("latency_window_ms must be 250, 1000, 3000, or 5000")
    all_records, seen = [], set()
    for row_number, record in enumerate(records, 1):
        _validate_record(record, row_number)
        key = (type(record["request_id"]), record["request_id"])
        if key in seen:
            raise ValueError(f"request row {row_number}: duplicate request_id {record['request_id']!r}")
        seen.add(key)
        all_records.append(record)

    arrivals = [r for r in all_records if start <= r["planned_ns"] < end]
    submitted = [r for r in all_records if start <= r["submitted_ns"] < end]
    finished = [r for r in all_records if start <= r["finished_ns"] < end]
    completed_successes = [r for r in finished if r["status"] == "success"]
    observed = _totals(all_records)
    summary = {
        "schema_version": 1,
        "measurement_start_ns": start,
        "measurement_end_ns": end,
        "duration_ns": end - start,
        "semantics": {
            "clock": "one host monotonic nanoseconds",
            "intervals": "[start, end); anchored to measurement_start_ns",
            "throughput": "unique successful payload by finished_ns; partial windows use actual duration",
            "latency": "planned_ns arrival cohorts, including all exported drain completions",
            "quantiles": "nearest-rank; p999 is P99.9; successful latencies conditional on success",
            "queue_ns": "submitted_ns - planned_ns; injection wait, not a hardware queue",
            "unfinished_at_end": "finished_ns >= exclusive end, including not yet submitted requests",
            "failure_rate_denominator": "all observed terminal requests in the stated cohort",
            "source_slot": "buffer identity only; never interpreted as a rail",
        },
        "observed": observed,
        "measurement": {
            "planned": _totals(arrivals),
            "submitted": _totals(submitted),
            "finished": _totals(finished),
            "arrival_cohort_unfinished_at_end": sum(r["finished_ns"] >= end for r in arrivals),
            "arrival_cohort_not_submitted_at_end": sum(r["submitted_ns"] >= end for r in arrivals),
        },
        "throughput": _throughput(completed_successes, end - start),
        "latency_success": _latencies([r for r in arrivals if r["status"] == "success"]),
        "latency_failure_wait": _latencies([r for r in arrivals if r["status"] == "failure"]),
        "conservation": {
            "scope": "observed unique terminal records only; not proof of full-run acceptance conservation",
            "observed_request_balance": observed["requests"] == observed["success_requests"] + observed["failure_requests"],
            "observed_payload_balance": observed["bytes"] == observed["success_bytes"] + observed["failure_bytes"],
            "accepted_requests": None,
            "unobserved_pending_requests": None,
            "accepted_equals_success_failure_pending": None,
            "drain_verified": None,
            "throughput_window_balances": {},
        },
        "unavailable": {
            "full_run_conservation": "no independent accepted count, pending records, or event-loss counters",
            "timeout_rate": "status does not distinguish timeout from other failure",
            "weight_changes": "no native selection/score observations",
            "migration_and_cross_rail_retries": "no allocation, post, CQ, attempt, or rail observations",
        },
    }
    windows = {"schema_version": 1, "throughput": {}, "latency": {}}
    for width in THROUGHPUT_WINDOWS_NS:
        label = f"{width // 1_000_000}ms"
        series = []
        for bounds, bucket in _buckets(all_records, "finished_ns", start, end, width):
            series.append({**bounds, **_throughput(
                [r for r in bucket if r["status"] == "success"], bounds["duration_ns"]),
                "failure_requests": sum(r["status"] == "failure" for r in bucket),
                "failure_bytes": sum(r["bytes"] for r in bucket if r["status"] == "failure")})
        windows["throughput"][label] = series
        summary["conservation"]["throughput_window_balances"][label] = {
            "requests": sum(w["success_requests"] for w in series) == len(completed_successes),
            "bytes": sum(w["success_bytes"] for w in series) == summary["throughput"]["success_bytes"],
        }

    latency_series = []
    for bounds, bucket in _buckets(arrivals, "planned_ns", start, end, latency_window_ms * 1_000_000):
        successes = [r for r in bucket if r["status"] == "success"]
        eligible = len(successes) >= MIN_P99_SUCCESSES
        metrics = _latencies(successes)
        latency_series.append({
            **bounds, **_totals(bucket),
            "unfinished_at_window_end": sum(r["finished_ns"] >= bounds["end_ns"] for r in bucket),
            "unfinished_at_measurement_end": sum(r["finished_ns"] >= end for r in bucket),
            "p99_end_to_end_ns": metrics["end_to_end_ns"]["p99"] if eligible else None,
            "p99_submission_to_finish_ns": metrics["submission_to_finish_ns"]["p99"] if eligible else None,
            "p99_queue_ns": metrics["queue_ns"]["p99"] if eligible else None,
            "p99_unavailable_reason": None if eligible else "fewer than 500 successful requests",
        })
    windows["latency"] = {
        "window_ns": latency_window_ms * 1_000_000,
        "minimum_successes": MIN_P99_SUCCESSES,
        "windows": latency_series,
        "valid_p99_windows": sum(w["p99_end_to_end_ns"] is not None for w in latency_series),
        "total_windows": len(latency_series),
    }
    windows["latency"]["valid_p99_coverage"] = windows["latency"]["valid_p99_windows"] / len(latency_series)
    summary["conservation"]["arrival_window_request_balance"] = sum(w["requests"] for w in latency_series) == len(arrivals)
    summary["conservation"]["arrival_window_payload_balance"] = sum(w["bytes"] for w in latency_series) == sum(r["bytes"] for r in arrivals)
    return summary, windows


def main(argv=None):
    """Read explicit input paths and create summary.json/windows.json."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests", type=Path, required=True, help="terminal requests JSONL")
    parser.add_argument("--manifest", type=Path, required=True, help="measurement manifest JSON")
    parser.add_argument("--output-dir", type=Path, required=True, help="explicit report directory (outside source tree recommended)")
    parser.add_argument("--latency-window-ms", type=int, choices=(250, 1000, 3000, 5000), default=250)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        summary, windows = summarize_requests(read_requests(args.requests), manifest, args.latency_window_ms)
        outputs = [(args.output_dir / name, json.dumps(data, indent=2, allow_nan=False) + "\n")
                   for name, data in (("summary.json", summary), ("windows.json", windows))]
        for path, _ in outputs:
            if path.exists():
                raise ValueError(f"refusing to overwrite {path}")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for path, content in outputs:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
