#!/usr/bin/env python3
"""Generate three instrumented .cpp copies; never edit the source tree."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

EXPECTED = {
    "quota.cpp": "9d064e873b0e060e30a373b039df253499529eaab2e7df6a555773ebe2efc913",
    "workers.cpp": "2c30639eb9b433ca35ec476383781e96b4e48309d866c0c9b05194378ade8af0",
    "rdma_transport.cpp": "9f37e499581ff6c1f4f3bf7b32fd98573b65eaf657256a183ce3c0478e627302",
}
# Post acceptance semantics rely on failed[] marking the rejected bad_wr suffix.
ENDPOINT_SHA = "1907d9248b97d857ff394e978a740c255b46ed1b2e5f60acbee78954d477a7c7"
REL = Path("mooncake-transfer-engine/tent/src/transport/rdma")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def patch(name, source):
    anchors = []

    def replace(old, new, label, count=1):
        nonlocal source
        actual = source.count(old)
        if actual != count:
            raise ValueError(f"{name}: {label}: expected {count} anchors, got {actual}")
        source = source.replace(old, new)
        anchors.append({"label": label, "count": count})

    # Insert before first include without altering existing include text.
    first = source.index("#include ")
    source = source[:first] + '#include "observer.h"\n' + source[first:]
    anchors.append({"label": "observer include", "count": 1})

    if name == "quota.cpp":
        replace("    slice_dev_ids.clear();", """    ::tent_obs::Allocation tent_observation(
        this, total_length, num_slices, slice_bytes, location, priority,
        device_mask, sched_params_.score_epsilon, smart_selection_enabled_,
        slice_dev_ids);
    slice_dev_ids.clear();""", "allocation RAII")
        replace("        candidates.push_back(c);", """        candidates.push_back(c);
        TENT_OBS_ALLOC(::tent_obs::candidate(
            dev_id, score, inflight, ewma_bw, rank_penalty, device_mask));""",
                "capture existing candidate locals")
        replace("        bool probe_mode = ((++tl_call_count % 100) == 0);",
                """        bool probe_mode = ((++tl_call_count % 100) == 0);
        TENT_OBS_ALLOC(::tent_obs::probe(probe_mode));""", "capture actual probe")
        replace("            uint32_t assigned =\n",
                """            TENT_OBS_ALLOC(::tent_obs::weight(candidates[i].dev_id, w, total_weight));
            uint32_t assigned =
""", "capture consumed weight")
        replace("            return Status::OK();\n        }\n        return Status::DeviceNotFound",
                """            TENT_OBS_ALLOC(::tent_obs::allocation_ok());
            return Status::OK();
        }
        return Status::DeviceNotFound""", "baseline success")
        replace("    return Status::OK();\n}\n\nvoid DeviceSelector::auditStrictLocalNuma()",
                """    TENT_OBS_ALLOC(::tent_obs::allocation_ok());
    return Status::OK();
}

void DeviceSelector::auditStrictLocalNuma()""", "smart success")
        replace("    if (!smart_selection_enabled_ || latency <= 0.0) {\n",
                """    if (!smart_selection_enabled_ || latency <= 0.0) {
        TENT_OBS(::tent_obs::bandwidth_release(
            this, dev_id, length, latency, false, 0, 0, 0, 0, 0, 0, 0, 0));
""", "release without learning")
        replace("    // Clamp to [min_multiplier, max_multiplier] of theoretical bandwidth",
                """    const double tent_obs_unclamped = new_ewma;
    // Clamp to [min_multiplier, max_multiplier] of theoretical bandwidth""",
                "capture unclamped original value")
        replace("    dev.ewma_bandwidth_bps.store(new_ewma, std::memory_order_relaxed);",
                """    dev.ewma_bandwidth_bps.store(new_ewma, std::memory_order_relaxed);
    TENT_OBS(::tent_obs::bandwidth_release(
        this, dev_id, length, latency, true, current_ewma, observed_bw,
        new_ewma, alpha, theoretical_bw,
        sched_params_.ewma_min_multiplier * theoretical_bw,
        sched_params_.ewma_max_multiplier * theoretical_bw, tent_obs_unclamped));""",
                "release actual store and clamps")
    elif name == "rdma_transport.cpp":
        replace("    for (auto& request : request_list) {\n",
                """    for (auto& request : request_list) {
        ::tent_obs::ContextScope tent_obs_origin(
            static_cast<uint64_t>(request.target_id), request.length,
            static_cast<int>(request.opcode), -1, 1);
""", "request context")
        replace("        // Only if a single request is enough, we perform aggregated allocation",
                """        TENT_OBS(::tent_obs::threshold(
            static_cast<uint64_t>(request.target_id), request.length,
            static_cast<int>(request.opcode), num_slices,
            max_slice_count / 2, block_size));
        // Only if a single request is enough, we perform aggregated allocation""",
                "actual batch threshold")
    else:
        replace("void Workers::asyncPostSend() {\n",
                """void Workers::asyncPostSend() {
    ::tent_obs::ContextScope tent_obs_origin(0, 0, -1, tl_wid, 2);
""", "post worker context")
        replace("void Workers::asyncPollCq() {\n",
                """void Workers::asyncPollCq() {
    ::tent_obs::ContextScope tent_obs_origin(0, 0, -1, tl_wid, 2);
""", "CQ worker context")
        replace("Status Workers::generatePostPath(RdmaSlice* slice) {\n",
                """Status Workers::generatePostPath(RdmaSlice* slice) {
    ::tent_obs::ContextScope tent_obs_path_origin(
        static_cast<uint64_t>(slice->task->request.target_id),
        slice->task->request.length,
        static_cast<int>(slice->task->request.opcode), tl_wid, 2);
""", "slice allocation peer context")
        # Three retry increments, in source order, with distinct causes.
        marker = "                slice->retry_count++;"
        if source.count(marker) != 3:
            raise ValueError("workers.cpp: expected exactly three retry increments")
        for index, cause in reversed(list(enumerate(("RetryEndpoint", "RetryPost", "RetryCq")))):
            positions = [i for i in range(len(source)) if source.startswith(marker, i)]
            pos = positions[index] + len(marker)
            source = source[:pos] + f"""
                TENT_OBS(::tent_obs::io(::tent_obs::Io::{cause}, tl_wid,
                    slice->source_dev_id, slice->length, slice->retry_count));
""" + source[pos:]
        anchors.append({"label": "three actual retry increment sites", "count": 3})
        replace("            if (slice->failed) {\n",
                """            if (slice->failed) {
                TENT_OBS(::tent_obs::io(::tent_obs::Io::PostRejected, tl_wid,
                    slice->source_dev_id, slice->length, slice->retry_count));
""", "rejected post suffix")
        replace("                slice->submit_ts = getCurrentTimeInNano();",
                """                TENT_OBS(::tent_obs::io(::tent_obs::Io::Post, tl_wid,
                    slice->source_dev_id, slice->length, slice->retry_count));
                slice->submit_ts = getCurrentTimeInNano();""", "accepted post")
        replace("            auto slice = (RdmaSlice*)wc[i].wr_id;\n",
                """            auto slice = (RdmaSlice*)wc[i].wr_id;
            TENT_OBS(::tent_obs::io(
                slice->word == PENDING
                    ? (wc[i].status == IBV_WC_SUCCESS ? ::tent_obs::Io::CqSuccess
                                                    : ::tent_obs::Io::CqError)
                    : (wc[i].status == IBV_WC_SUCCESS ? ::tent_obs::Io::CqLateSuccess
                                                    : ::tent_obs::Io::CqLateError),
                tl_wid, index, slice->length, slice->retry_count,
                static_cast<uint64_t>(wc[i].status)));
""", "actual CQ before terminal filters")
        replace("    std::vector<RdmaSlice*> slice_to_remove;\n",
                """    TENT_OBS(if (::tent_obs::queue_due(tl_wid, -1, false))
        ::tent_obs::queue(tl_wid, -1, false,
            worker.inflight_slices.load(std::memory_order_relaxed),
            worker.inflight_slice_set.size(), worker.requeue_overflow.size()));
    std::vector<RdmaSlice*> slice_to_remove;
""", "worker software queue snapshot")
        replace("        int nr_poll = cq->poll(kPollCount, wc);",
                """        TENT_OBS(if (::tent_obs::queue_due(tl_wid, index, true))
            ::tent_obs::queue(tl_wid, index, true,
                cq->getQuota(), cq->maxCqe(), 0));
        int nr_poll = cq->poll(kPollCount, wc);""", "CQ reserved-WR snapshot")
    return source, anchors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path,
                        help="repository root containing mooncake-transfer-engine (a10-source)")
    parser.add_argument("--builddir", required=True, type=Path)
    parser.add_argument("--check-only", action="store_true",
                        help="validate hashes and all anchors without creating files")
    args = parser.parse_args()
    root, build = args.source_root.resolve(), args.builddir.resolve()
    if build == root or root in build.parents:
        raise ValueError("builddir must be outside source-root")
    inputs, outputs, audit = {}, {}, {}
    for name, expected in EXPECTED.items():
        path = root / REL / name
        raw = path.read_bytes()
        if sha(raw) != expected:
            raise ValueError(f"{path}: SHA256 mismatch, expected {expected}, got {sha(raw)}")
        inputs[name] = raw
        patched, anchors = patch(name, raw.decode("utf-8"))
        outputs[name] = patched.encode("utf-8")
        audit[name] = {"source_sha256": expected, "output_sha256": sha(outputs[name]),
                       "anchors": anchors}
    endpoint = root / REL / "endpoint.cpp"
    if sha(endpoint.read_bytes()) != ENDPOINT_SHA:
        raise ValueError("endpoint.cpp semantics dependency SHA256 mismatch")
    # Check every destination before any write, including symlink aliases.
    support = Path(__file__).resolve().parent
    artifacts = {**outputs, "observer.h": (support / "observer.h").read_bytes(),
                 "observer.cpp": (support / "observer.cpp").read_bytes()}
    manifest = {"schema": "tent-observer-patch-v1", "head_reference": "89c3835",
                "source_root": str(root), "files": audit,
                "endpoint_semantics_sha256": ENDPOINT_SHA,
                "observer_sha256": {n: sha(artifacts[n]) for n in ("observer.h", "observer.cpp")}}
    artifacts["observer-manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    if build.exists() and not build.is_dir():
        raise ValueError("builddir is not a directory")
    for name in artifacts:
        dst = build / name
        resolved = dst.resolve()
        if dst.is_symlink() or resolved == root or root in resolved.parents:
            raise ValueError(f"unsafe destination: {dst}")
        if dst.exists():
            if not dst.is_file() or dst.stat().st_nlink != 1:
                raise ValueError(f"destination is not a plain unlinked file: {dst}")
            if resolved in ((support / "observer.h").resolve(), (support / "observer.cpp").resolve()):
                raise ValueError("builddir must differ from observer source directory")
    # Refuse source changes between reading and generation.
    for name, raw in inputs.items():
        if (root / REL / name).read_bytes() != raw:
            raise ValueError(f"source changed during validation: {name}")
    if not args.check_only:
        build.mkdir(parents=True, exist_ok=True)
        for name, data in artifacts.items():
            (build / name).write_bytes(data)
    print(json.dumps({"validated": True, "check_only": args.check_only,
                      "builddir": str(build), "files": list(artifacts)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"instrument_tent: {error}", file=sys.stderr)
        sys.exit(1)
