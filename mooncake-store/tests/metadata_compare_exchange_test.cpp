#include <gtest/gtest.h>
#include <ylt/struct_json/json_reader.h>

#include <atomic>
#include <thread>

#include "master_service_test_fixture.h"
#include "client_buffer.h"

namespace mooncake::test {

class MetadataCompareExchangeTest : public MasterServiceTest {
   protected:
    std::unique_ptr<MasterService> MakeService() {
        auto service = std::make_unique<MasterService>(
            MasterServiceConfig::builder().build());
        PrepareSimpleSegment(*service);
        return service;
    }

    void Stage(MasterService& service, const UUID& client,
               const std::string& key, size_t size = 128, bool complete = true,
               ObjectDataType type = ObjectDataType::METADATA) {
        ReplicateConfig config;
        config.data_type = type;
        ASSERT_TRUE(
            service.PutStart(client, key, TenantId::Default(), size, config)
                .has_value());
        if (complete) {
            ASSERT_TRUE(service
                            .PutEnd(client, key, TenantId::Default(),
                                    ReplicaType::MEMORY)
                            .has_value());
        }
    }
};

TEST_F(MetadataCompareExchangeTest, CreateReplaceAndRejectStaleRevision) {
    auto service = MakeService();
    const auto client = generate_uuid();
    const std::string key = "catalog";
    Stage(*service, client, key + "/.cas/a");
    auto created = service->CompareExchangeMetadata(
        client, key, "", key + "/.cas/a", TenantId::Default());
    ASSERT_TRUE(created.has_value());
    ASSERT_TRUE(*created);
    EXPECT_FALSE(service->GetReplicaList(key + "/.cas/a", TenantId::Default())
                     .has_value());
    auto first = service->GetMetadataForUpdate(key, TenantId::Default());
    ASSERT_TRUE(first.has_value());
    Stage(*service, client, key + "/.cas/b", 512);
    auto updated = service->CompareExchangeMetadata(
        client, key, first->token, key + "/.cas/b", TenantId::Default());
    ASSERT_TRUE(updated.has_value());
    ASSERT_TRUE(*updated);
    auto second = service->GetMetadataForUpdate(key, TenantId::Default());
    ASSERT_TRUE(second.has_value());
    EXPECT_NE(first->token, second->token);
    EXPECT_EQ(calculate_total_size(second->object.replicas.front()), 512);
    Stage(*service, client, key + "/.cas/c", 1024);
    auto stale = service->CompareExchangeMetadata(
        client, key, first->token, key + "/.cas/c", TenantId::Default());
    ASSERT_TRUE(stale.has_value());
    EXPECT_FALSE(*stale);
    EXPECT_EQ(service->GetMetadataForUpdate(key, TenantId::Default())->token,
              second->token);
    EXPECT_TRUE(service->GetReplicaList(key + "/.cas/c", TenantId::Default())
                    .has_value());
}

TEST_F(MetadataCompareExchangeTest, ConcurrentUpdatesHaveOneWinner) {
    auto service = MakeService();
    const auto client = generate_uuid();
    Stage(*service, client, "catalog");
    auto first = service->GetMetadataForUpdate("catalog", TenantId::Default());
    ASSERT_TRUE(first.has_value());
    Stage(*service, client, "catalog/.cas/a");
    Stage(*service, client, "catalog/.cas/b");
    std::atomic<int> ready{0};
    std::atomic<int> winners{0};
    auto update = [&](std::string staged) {
        ready.fetch_add(1);
        while (ready.load() != 2) std::this_thread::yield();
        auto result = service->CompareExchangeMetadata(
            client, "catalog", first->token, staged, TenantId::Default());
        EXPECT_TRUE(result.has_value());
        if (result && *result) winners.fetch_add(1);
    };
    std::thread a(update, "catalog/.cas/a");
    std::thread b(update, "catalog/.cas/b");
    a.join();
    b.join();
    EXPECT_EQ(winners.load(), 1);
}

TEST_F(MetadataCompareExchangeTest, FailedOrForeignStageCannotReplaceValue) {
    auto service = MakeService();
    const auto client = generate_uuid();
    Stage(*service, client, "catalog");
    auto old = service->GetMetadataForUpdate("catalog", TenantId::Default());
    ASSERT_TRUE(old.has_value());
    Stage(*service, client, "catalog/.cas/pending", 128, false);
    EXPECT_FALSE(service
                     ->CompareExchangeMetadata(client, "catalog", old->token,
                                               "catalog/.cas/pending",
                                               TenantId::Default())
                     .has_value());
    Stage(*service, client, "catalog/.cas/other");
    auto foreign = service->CompareExchangeMetadata(
        generate_uuid(), "catalog", old->token, "catalog/.cas/other",
        TenantId::Default());
    ASSERT_FALSE(foreign.has_value());
    EXPECT_EQ(foreign.error(), ErrorCode::ILLEGAL_CLIENT);
    Stage(*service, client, "catalog/.cas/payload", 128, true,
          ObjectDataType::KVCACHE);
    EXPECT_FALSE(service
                     ->CompareExchangeMetadata(client, "catalog", old->token,
                                               "catalog/.cas/payload",
                                               TenantId::Default())
                     .has_value());
    EXPECT_EQ(
        service->GetMetadataForUpdate("catalog", TenantId::Default())->token,
        old->token);
}

TEST_F(MetadataCompareExchangeTest, MissingAndInvalidKeysHaveExplicitResults) {
    auto service = MakeService();
    const auto client = generate_uuid();
    auto missing = service->GetMetadataForUpdate("absent", TenantId::Default());
    ASSERT_FALSE(missing.has_value());
    EXPECT_EQ(missing.error(), ErrorCode::OBJECT_NOT_FOUND);
    Stage(*service, client, "catalog/.cas/a");
    EXPECT_FALSE(*service->CompareExchangeMetadata(
        client, "catalog", "stale", "catalog/.cas/a", TenantId::Default()));
    EXPECT_FALSE(service
                     ->CompareExchangeMetadata(client, "catalog", "",
                                               "unrelated", TenantId::Default())
                     .has_value());
    EXPECT_TRUE(service->GetReplicaList("catalog/.cas/a", TenantId::Default())
                    .has_value());
}

TEST_F(MetadataCompareExchangeTest,
       SameAndDifferentShardRelocationsPreserveIdentity) {
    auto service = MakeService();
    const auto client = generate_uuid();
    for (const bool same : {false, true}) {
        const std::string key = same ? "same-shard" : "different-shard";
        std::string staged;
        for (size_t i = 0; i < 100000; ++i) {
            auto candidate = key + "/.cas/" + std::to_string(i);
            if ((MetadataShardIndex(*service, key) ==
                 MetadataShardIndex(*service, candidate)) == same) {
                staged = std::move(candidate);
                break;
            }
        }
        ASSERT_FALSE(staged.empty());
        Stage(*service, client, staged);
        auto before =
            service->GetMetadataForUpdate(staged, TenantId::Default());
        ASSERT_TRUE(before.has_value());
        ASSERT_TRUE(*service->CompareExchangeMetadata(client, key, "", staged,
                                                      TenantId::Default()));
        auto after = service->GetMetadataForUpdate(key, TenantId::Default());
        ASSERT_TRUE(after.has_value());
        EXPECT_EQ(before->token, after->token);
        EXPECT_FALSE(service->GetMetadataForUpdate(staged, TenantId::Default())
                         .has_value());
    }
}

TEST_F(MetadataCompareExchangeTest, TenantScopesRemainIsolated) {
    auto service =
        std::make_unique<MasterService>(MakeStrictTenantConfig({"a", "b"}));
    PrepareSimpleSegment(*service);
    const auto client = generate_uuid();
    ReplicateConfig config;
    config.data_type = ObjectDataType::METADATA;
    std::vector<std::string> tokens;
    for (const auto& name : {"a", "b"}) {
        const TenantId tenant(name);
        const std::string staged = "catalog/.cas/value";
        ASSERT_TRUE(
            service->PutStart(client, staged, tenant, 128, config).has_value());
        ASSERT_TRUE(service->PutEnd(client, staged, tenant, ReplicaType::MEMORY)
                        .has_value());
        auto result = service->CompareExchangeMetadata(client, "catalog", "",
                                                       staged, tenant);
        ASSERT_TRUE(result.has_value());
        ASSERT_TRUE(*result);
        auto query = service->GetMetadataForUpdate("catalog", tenant);
        ASSERT_TRUE(query.has_value());
        tokens.push_back(query->token);
    }
    EXPECT_NE(tokens[0], tokens[1]);
    ASSERT_TRUE(
        service
            ->PutStart(client, "catalog/.cas/next", TenantId("a"), 128, config)
            .has_value());
    ASSERT_TRUE(service
                    ->PutEnd(client, "catalog/.cas/next", TenantId("a"),
                             ReplicaType::MEMORY)
                    .has_value());
    auto wrong_scope = service->CompareExchangeMetadata(
        client, "catalog", tokens[1], "catalog/.cas/next", TenantId("a"));
    ASSERT_TRUE(wrong_scope.has_value());
    EXPECT_FALSE(*wrong_scope);
    EXPECT_EQ(service->GetMetadataForUpdate("catalog", TenantId("a"))->token,
              tokens[0]);
    EXPECT_EQ(service->GetMetadataForUpdate("catalog", TenantId("b"))->token,
              tokens[1]);
}

}  // namespace mooncake::test
