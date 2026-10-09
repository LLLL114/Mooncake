#include "master_service_test_fixture.h"
#include <thread>

namespace mooncake::test {
namespace {
PartEndRequest End(const std::string& key, uint32_t part,
                   const std::vector<Replica::Descriptor>& replicas) {
    PartEndRequest end{key, part, {}, false};
    for (const auto& r : replicas) end.replica_ids.push_back(r.id);
    return end;
}
}  // namespace

TEST_F(MasterServiceTest, MultipartCompletenessAndImmutableLayout) {
    MasterService service(MasterServiceConfig::builder().build());
    PrepareSimpleSegment(service);
    auto a = generate_uuid(), b = generate_uuid();
    auto tenant = TenantId::Default();
    auto first =
        service.PutPartStart(a, {"page", 1, 2, "manifest", 1024}, tenant);
    ASSERT_TRUE(first);
    ASSERT_TRUE(service.PutPartEnd(a, End("page", 1, *first), tenant));
    ASSERT_FALSE(*service.ExistKey("page", tenant));
    ASSERT_FALSE(service.QueryParts("page", tenant));
    EXPECT_FALSE(
        service.PutPartStart(b, {"page", 0, 2, "other-layout", 1024}, tenant));
    EXPECT_FALSE(
        service.PutPartStart(b, {"page", 0, 3, "manifest", 1024}, tenant));
    auto second =
        service.PutPartStart(b, {"page", 0, 2, "manifest", 2048}, tenant);
    ASSERT_TRUE(second);
    EXPECT_FALSE(service.PutPartEnd(a, End("page", 0, *second), tenant));
    ASSERT_TRUE(service.PutPartEnd(b, End("page", 0, *second), tenant));
    EXPECT_TRUE(*service.ExistKey("page", tenant));
    EXPECT_TRUE(*service.BatchExistKey({"page"}, tenant)[0]);
    auto query = service.QueryParts("page", tenant);
    ASSERT_TRUE(query);
    EXPECT_EQ(query->manifest_key, "manifest");
    ASSERT_EQ(query->parts.size(), 2);
    EXPECT_EQ(query->parts[0].replicas[0].id, second->front().id);
    EXPECT_EQ(query->parts[1].replicas[0].id, first->front().id);
    EXPECT_FALSE(service.GetReplicaList("page", tenant));
    EXPECT_TRUE(service.PutPartEnd(b, End("page", 0, *second), tenant));
    // Different topologies deduplicate the complete object without replacing
    // it.
    auto duplicate =
        service.PutPartStart(a, {"page", 0, 4, "new-layout", 512}, tenant);
    ASSERT_TRUE(duplicate);
    EXPECT_TRUE(duplicate->empty());
}

TEST_F(MasterServiceTest, MultipartLateEndCannotCompleteReallocatedPart) {
    MasterService service(MasterServiceConfig::builder()
                              .set_put_start_discard_timeout_sec(0)
                              .set_put_start_release_timeout_sec(60)
                              .build());
    PrepareSimpleSegment(service);
    auto writer = generate_uuid();
    auto tenant = TenantId::Default();
    PartPutRequest request{"retry", 0, 2, "manifest", 1024};
    auto old = service.PutPartStart(writer, request, tenant);
    ASSERT_TRUE(old);
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
    auto fresh = service.PutPartStart(writer, request, tenant);
    ASSERT_TRUE(fresh);
    EXPECT_NE(old->front().id, fresh->front().id);
    auto stale = service.PutPartEnd(writer, End("retry", 0, *old), tenant);
    ASSERT_FALSE(stale);
    EXPECT_EQ(stale.error(), ErrorCode::INVALID_WRITE);
    ASSERT_TRUE(service.PutPartEnd(writer, End("retry", 0, *fresh), tenant));
    EXPECT_FALSE(*service.ExistKey("retry", tenant));
}

TEST_F(MasterServiceTest, MultipartIncompleteEvictionAndReadLease) {
    MasterService service(
        MasterServiceConfig::builder().set_default_kv_lease_ttl(10000).build());
    PrepareSimpleSegment(service);
    auto writer = generate_uuid();
    auto tenant = TenantId::Default();
    auto first =
        service.PutPartStart(writer, {"lease", 0, 2, "manifest", 1024}, tenant);
    ASSERT_TRUE(first);
    ASSERT_TRUE(service.PutPartEnd(writer, End("lease", 0, *first), tenant));
    MasterServiceTestPeer(service).RunBatchEvictForTesting(1.0, 1.0);
    auto second =
        service.PutPartStart(writer, {"lease", 1, 2, "manifest", 1024}, tenant);
    ASSERT_TRUE(second);
    ASSERT_TRUE(service.PutPartEnd(writer, End("lease", 1, *second), tenant));
    auto read = service.QueryParts("lease", tenant);
    ASSERT_TRUE(read);
    EXPECT_EQ(read->parts[0].replicas[0].id, first->front().id);
    MasterServiceTestPeer(service).RunBatchEvictForTesting(1.0, 1.0);
    EXPECT_TRUE(service.QueryParts("lease", tenant));
    EXPECT_FALSE(service.Remove("lease", tenant));
    EXPECT_TRUE(service.Remove("lease", tenant, true));
    EXPECT_FALSE(*service.ExistKey("lease", tenant));
}

}  // namespace mooncake::test
