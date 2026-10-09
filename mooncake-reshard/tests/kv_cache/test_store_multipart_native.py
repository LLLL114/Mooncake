"""Actual Store byte correctness across TP1/PP3 and TP2/PP2."""

import os

import pytest
from mooncake.reshard.kv_cache import (
    KVCacheStoreFormat,
    MultipartKVCacheStore,
    plan_kv_cache_store_upload,
)
from test_kv_cache_reshard import _placement
from test_store_contracts import _manifest
from test_store_native import _bound, _real, cluster  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.getenv("MC_KV_STORE_NATIVE") != "1", reason="native Store opt-in"
)


@pytest.mark.parametrize("source_fmt", tuple(KVCacheStoreFormat))
@pytest.mark.parametrize("target_fmt", tuple(KVCacheStoreFormat))
def test_tp1pp3_to_tp2pp2_exact_bytes(cluster, source_fmt, target_fmt):  # noqa: F811
    native = _real(cluster[0])
    try:
        source = _placement(
            "source", (tuple(range(4)), tuple(range(4, 8)), tuple(range(8, 12))), 1
        )
        target = _placement("target", (tuple(range(6)), tuple(range(6, 12))), 2)
        plan = plan_kv_cache_store_upload(source, object_format=source_fmt)
        store = MultipartKVCacheStore(native, _manifest(plan.layout))
        store.register_layout()
        for i, participant in enumerate(source.parts):
            with _bound(
                native, source, participant.participant_id, source_fmt, 1, "put"
            ) as (binding, _, _):
                assert store.upload(plan, ("page",), binding, operation_id="put") == (
                    True,
                )
            assert native.batch_is_exist(["page"]) == [int(i == 2)]
        target_store = MultipartKVCacheStore(
            native,
            _manifest(
                plan_kv_cache_store_upload(target, object_format=target_fmt).layout
            ),
        )
        context = target_store.discover(("page", "missing"), operation_id="read")
        assert context.page_keys == ("page",)
        for participant in target.parts:
            with _bound(
                native,
                target,
                participant.participant_id,
                target_fmt,
                1,
                "read",
                fill=False,
            ) as (binding, buffers, expected):
                reader = target_store.prepare_page_reader(target, binding)
                assert reader.load(context, [{key: 0 for key in reader.regions}]) == 1
                assert [bytes(b) for b in buffers] == expected
                assert (
                    reader.load(
                        context,
                        [{key: 0 for key in reader.regions}],
                        cancelled=lambda: True,
                    )
                    == 0
                )
    finally:
        native.close()
