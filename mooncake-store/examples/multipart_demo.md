# Multipart Store demo

This experiment implements the memory-only KV reshard subset of
[RFC #4366](https://github.com/kvcache-ai/Mooncake/issues/4366).

One Store key owns a fixed Part table and an immutable `manifest_key`. Each Part
has its own length, writer, start time and replica IDs. For this prototype the
replicas remain in ObjectMetadata's existing arena, so segment cleanup, leases,
removal and eviction reuse the existing machinery. There are no hidden per-rank
Store keys. The snapshot-local `\x1fpN` suffix addresses a Part only inside the
client's ranged-read snapshot; it is never published to the Master as a key.

## Python interface

- `batch_put_parts_from(keys, part_index, part_count, manifest_key, pointers, sizes)`:
  concatenate each row's registered buffers into the writer's Part. Zero means
  success or deduplication of a completed Part/object. In-flight duplicate writes
  return an error, not a completed upload.
- `batch_query_parts(keys)`: return `(status, manifest_key, part_count)` per page;
  incomplete objects return an error and `batch_is_exist` returns zero.
- `prepare_get_parts_snapshot(keys, expected_manifests, expected_counts)`:
  check source identity, grant the whole-object read lease and prepare Part
  descriptors for `get_into_ranges_from_template`.

The first writer fixes the manifest reference and Part count. Writers with a
conflicting layout cannot fill an incomplete object. A completed object wins
against writers from other topologies. End/Revoke are scoped by replica IDs, so
an old write cannot complete or revoke a reallocated Part.

Run `multipart_demo.py --master HOST:PORT --metadata HTTP_METADATA_URL` with the
new native library. It owns a memory provider and starts three independent writer
processes, verifies all-parts visibility, then checks the bytes of all three
Parts and cross-layout deduplication. The Master must already be running.

## Deliberate scope

This is a RealClient, memory-only prototype with one memory replica per Part.
It rejects HA/oplog/snapshot deployments, offload and tenant quotas. Copy/Move,
ordinary Upsert and ordinary End/Revoke cannot mutate multipart objects. Existing
ordinary Put/Get objects keep their original behavior. New multipart objects,
including those with one Part, use the explicit Part read interface.

Persistence, disk tiers, DummyClient IPC, configurable replication and a complete
migration of the legacy replica arena into Part-owned containers are deferred.
Manifests used by the KV adapter are immutable metadata objects with hard pinning;
manifest garbage collection is also outside this experiment.

The independent C++ test target is `master_service_multipart_test`.
