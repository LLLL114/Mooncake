#!/usr/bin/env python3
"""Window metrics for complete simulation traces; reject sampled/dropped traces."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import external_output
import argparse
import csv
import json
import math
import pathlib


def percentile(values, q):
    return sorted(values)[max(0, math.ceil(q*len(values))-1)] if values else None


def total_variation(vectors, seconds):
    return sum(sum(abs(x-y) for x, y in zip(a, b))/2
               for a, b in zip(vectors, vectors[1:]))/seconds


def jain(values):
    return sum(values)**2/(len(values)*sum(v*v for v in values)) if any(values) else None


def analyze(data, window_ns):
    cfg = data["config"]
    if not cfg.get("observe") or cfg.get("trace_every") != 1 or data["trace_dropped"]:
        raise ValueError("window analysis requires full, non-dropped events")
    if window_ns <= 0:
        raise ValueError("window_ns must be positive")
    rails = cfg["rails"]
    capacity = cfg["capacity_bytes_per_second"]
    count = data["virtual_end_ns"]//window_ns+1
    bins = [{"allocated":[0]*rails,"completed":[0]*rails,"latencies":[],"p":None} for _ in range(count)]
    allocation_count = completion_count = 0
    latest_end = {}
    for row in data["trace"]:
        bucket = bins[row["time_ns"]//window_ns]
        event = row["event"]
        if event == "allocate":
            allocation_count += 1
            bucket["allocated"][row["rail"]] += row["bytes"]
        elif event == "complete":
            completion_count += 1
            bucket["completed"][row["rail"]] += row["bytes"]
            latest_end[row["request"]] = max(latest_end.get(row["request"],0),row["time_ns"])
        elif event == "candidates":
            weights = [0.0]*rails
            for candidate in row["weights"]:
                weights[candidate["rail"]] = candidate["p"]
            bucket["p"] = weights
    if allocation_count != data["slices"] or completion_count != data["slices"]:
        raise ValueError("missing or duplicate allocation/completion events")
    for req, end in latest_end.items():
        bins[end//window_ns]["latencies"].append(data["request_latency_ns"][req])
    rows, weight_series, share_series = [], [], []
    previous_p = None
    for index, bucket in enumerate(bins):
        # Fixed-width windows; the final partial window is identified, not used
        # to infer full-window saturation. Events on an edge enter next window.
        allocated = bucket["allocated"]
        completed = bucket["completed"]
        share = [n/sum(allocated) for n in allocated] if sum(allocated) else None
        utilization = [n/(window_ns/1e9)/c for n,c in zip(completed,capacity)]
        previous_p = bucket["p"] if bucket["p"] is not None else previous_p
        if previous_p is not None:
            weight_series.append(previous_p)
        if share is not None:
            share_series.append(share)
        rows.append({"start_ns":index*window_ns,"partial_window":(index+1)*window_ns>data["virtual_end_ns"],
            "allocated_bytes":allocated,"completed_bytes":completed,"allocation_share":share,
            "last_candidate_p":previous_p,"normalized_utilization":utilization,
            "jain":jain(utilization),"request_count":len(bucket["latencies"]),
            "request_p99_ns":percentile(bucket["latencies"],.99) if len(bucket["latencies"])>=100 else None})
    seconds = data["virtual_end_ns"]/1e9
    return {"source":"simulation","window_ns":window_ns,
        "weight_tv_per_second":total_variation(weight_series,seconds) if weight_series else None,
        "traffic_tv_per_second":total_variation(share_series,seconds),
        "weight_semantics":"last actual candidate weights held within fixed request class; not a shadow reference probe",
        "idle_semantics":"empty allocation windows excluded from share transitions",
        "rows":rows}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("input",type=pathlib.Path)
    parser.add_argument("output",type=pathlib.Path)
    parser.add_argument("--window-ns",type=int,default=1000000)
    args=parser.parse_args()
    args.output=external_output(args.output)
    result=analyze(json.loads(args.input.read_text()),args.window_ns)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/"metrics.json").write_text(json.dumps(result,indent=2)+"\n")
    with (args.output/"windows.csv").open("w",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=list(result["rows"][0]))
        writer.writeheader()
        for row in result["rows"]:
            writer.writerow({k:json.dumps(v) if isinstance(v,list) else v for k,v in row.items()})
    print("ANALYSIS_OK",len(result["rows"]),"windows")


if __name__=="__main__":
    main()
