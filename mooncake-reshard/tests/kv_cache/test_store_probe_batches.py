"""Discovery batches respect native probe limits without losing prefix semantics."""

import pytest
from mooncake.reshard.kv_cache import KVCacheStore, KVCacheStorePlanningLimits
from test_store_contracts import _layout, _manifest
from test_store_execution import MemoryBackend, MemoryNative


@pytest.fixture
def discovery(monkeypatch):
    native = MemoryNative()
    manifests = (_manifest(_layout(1)), _manifest(_layout(2, ((0, 1, 2, 3),))))
    for manifest in manifests:
        store = KVCacheStore(MemoryBackend(native), manifest)
        store.register_layout()
    pages = ("a", "模型", "long-prefix", "last", "tail")
    for index, page in enumerate(pages):
        for key in manifests[index % 2].object_keys(page):
            native.values[key] = b"payload"
    calls = []
    original = native.batch_is_exist

    def probe(keys):
        calls.append(tuple(keys))
        return original(keys)

    monkeypatch.setattr(native, "batch_is_exist", probe)
    return store, native, manifests, pages, calls


@pytest.mark.parametrize("bound", ["keys", "bytes", "pages"])
@pytest.mark.parametrize("missing", [None, 2, 3])
def test_discovery_splits_probes_and_preserves_mixed_layout_prefix(
    discovery, bound, missing
):
    store, native, manifests, pages, calls = discovery
    width = sum(len(m.object_keys(pages[0])) for m in manifests)
    byte_limit = sum(
        len(key.encode("utf-8"))
        for page in pages[:2]
        for manifest in manifests
        for key in manifest.object_keys(page)
    )
    # One byte below the first pair catches character counts used as UTF-8 sizes.
    store.planning_limits = KVCacheStorePlanningLimits(
        max_probe_keys=2 * width if bound == "keys" else 100_000,
        max_probe_bytes=byte_limit - 1 if bound == "bytes" else 16 * 1024 * 1024,
    )
    if bound == "pages":
        store.page_batch_size = 2
    if missing is not None:
        del native.values[manifests[missing % 2].object_keys(pages[missing])[0]]

    context = store.discover(pages, operation_id="read")
    expected = pages if missing is None else pages[:missing]
    assert context.page_keys == expected
    assert context.layout_ids == tuple(
        manifests[i % 2].layout.layout_id for i in range(len(expected))
    )
    assert len(calls) > 1
    for keys in calls:
        assert len(keys) <= store.planning_limits.max_probe_keys
        assert (
            sum(len(k.encode("utf-8")) for k in keys)
            <= store.planning_limits.max_probe_bytes
        )
        assert len(keys) <= store.page_batch_size * width
    if missing is None:
        assert sum(map(len, calls)) == len(pages) * width
    else:
        assert manifests[missing % 2].object_keys(pages[missing])[0] in calls[-1]


@pytest.mark.parametrize("bound", ["keys", "bytes"])
def test_discovery_rejects_one_oversized_page_before_native_probe(discovery, bound):
    store, _, manifests, pages, calls = discovery
    keys = tuple(key for m in manifests for key in m.object_keys(pages[1]))
    store.planning_limits = KVCacheStorePlanningLimits(
        max_probe_keys=len(keys) - 1 if bound == "keys" else 100_000,
        max_probe_bytes=sum(len(k.encode("utf-8")) for k in keys) - 1
        if bound == "bytes"
        else 16 * 1024 * 1024,
    )
    assert store.discover((), operation_id="empty").page_keys == ()
    with pytest.raises(ValueError, match="probe key"):
        store.discover(pages[1:2], operation_id="read")
    assert calls == []


def test_discovery_cancels_between_adaptive_batches(discovery):
    store, _, manifests, pages, calls = discovery
    store.planning_limits = KVCacheStorePlanningLimits(
        max_probe_keys=sum(len(m.object_keys(pages[0])) for m in manifests)
    )
    context = store.discover(pages, operation_id="read", cancelled=lambda: bool(calls))
    assert context.page_keys == ()
    assert len(calls) == 1
