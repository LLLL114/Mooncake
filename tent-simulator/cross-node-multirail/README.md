# Cross-node TENT multi-rail experiments

This directory contains the real RDMA baseline harness and its analysis tools.
It does not replace the production routing algorithm. A/B/C measurements exist;
mixed-size, learning-ablation, complete fault-recovery and multi-request batch
controls are not yet complete.

## Execution and output

Run only through SSH on the experiment servers, in Conda environment `mooncake`.
Do not run experimental Python, compilers or tests on the local Mac.
The sender has two eRDMA NICs; the receiver has one. Both use TENT.
Keep all raw data, reports, validation fixtures and build products outside Git:

```
/root/mooncake-tent-multirdma-output/cross-node-multirail/
```

Start with the connectivity scripts in `../connectivity/README.md`. The native
sender additionally requires the private observed TENT library described in
`OBSERVER.md`; the installed Python extension alone is not that library.

## Retained components

| Files | Purpose |
| --- | --- |
| `pilot.py`, `run_receiver.sh`, `run_sender.sh`, `run_native.sh` | Receiver/control service and native sender entry points |
| `native_sender*.py`, `stream_native.cpp`, `native_introspection.cpp` | Async TENT requests, latency records and actual NIC topology |
| `observer.*`, `instrument_tent.py`, `build_observed.py` | Observe shadow copies of TENT sources and link a private test library |
| `run_suite*.py`, `run_collection*.sh`, `prepare_*.py` | Frozen baseline variants, bounded collection and source generation |
| `run_followups_bc.*`, `run_C_resources.*` | Dynamic arrivals, caller alignment and matched receiver resource controls |
| `analyze_*.py`, `audit_*.py`, `summarize_*.py`, `complete_B_diagnostics.py` | Metrics, accounting, arrival and correctness audits |
| `verify_*.py`, `test_stream_native.cpp` | Harness regression checks; these are not real RDMA performance samples |
| `run_restore_control.py` | Separate default-lane restoration controls |

The generated sender and collection variants are retained because the measured
runs identify their exact source hashes. Do not reformat or silently replace
them when resuming a frozen matrix. A new Git commit also changes the recorded
repository identity: retain existing manifests and do not merge newly identified
runs into old cases without checking the suite's identity rules.

## Build dependency

`build_observed.py` is required reproducibility code, not a generated build
artifact. It reuses a matching original TENT CMake build, including its static
archives, compiler flags and link command. On A10 that build was moved to
`/root/mooncake-tent-multirdma-output/cross-node-multirail/build-tent-base/`.
`/root/mooncake/build-tent` is an ignored local compatibility symlink so the
existing CMake absolute paths and frozen manifests still resolve. Neither the
link nor its target is part of this commit. A fresh host must supply its own
matching original TENT build before using `build_observed.py`.

Never overwrite a loaded observed library. Use a fresh output directory for a
new variant. Production TENT sources remain unchanged.

## Receiver variants and protocols

On the receiver, `python -B prepare_extended.py receiver` creates the 128-slot
`receiver_extended.py` without replacing the original driver. To generate the
matched one-lane receiver, use `prepare_receiver_lanes1.py` after that step.
Generators reject unexpected source versions or existing output files.
Both peers must have matching RDMA lane counts. See `FOLLOWUP_BC_PROTOCOL.md`
and `C_RESOURCE_PROTOCOL.md` before starting these experiments.

Host-specific, one-shot retries and the already-applied observer patch were
archived outside Git in `repository-cleanup-20260917/historical-operations/`.
They are historical recovery evidence, not general test entry points; do not
rerun them. The pre-cleanup source snapshot and validation results are stored
beside that archive.
