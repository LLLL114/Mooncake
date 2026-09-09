# 多轨 RDMA 候选分配模块

源文件：`rail_scheduler.h`、`rail_scheduler.cpp`，C++17，仅依赖标准库。

```cpp
#include "rail_scheduler.h"
using namespace mooncake::tent::multirail;

Scheduler scheduler(Policy::EarliestFinish);  // 或 Policy::ByteDeficit
std::vector<Candidate> healthy = {
    {0, 1.0, 12.5e9, 1.0},  // id、旧评分占位、标称bytes/s、NUMA惩罚
    {1, 1.0, 12.5e9, 5.0},
};
std::vector<uint64_t> slices(16, 65536);
auto assignment = scheduler.allocate(healthy, slices, now_ns, traffic_class);

// 每个实际post成功的Slice：
scheduler.posted(rail, actual_bytes, post_ns);
// 每个已post Slice的唯一终态（成功或失败）：
scheduler.completed(rail, actual_bytes, post_ns, completion_ns, true, success);
// 未post取消：
scheduler.completed(rail, actual_bytes, 0, now_ns, false, false);
```

`now_ns/post_ns/completion_ns`必须采用同一时钟域。调用方负责过滤健康/拓扑/mask/优先级候选，并保证每个Slice只反馈一次终态。候选score统一要求有限正数；后两种策略不使用它，可传1。相同Scheduler的调用会共享预占和容量状态；不同流量亲和类别使用不同traffic_class保存债务，但仍共享Rail预占。不会自动同步到其他进程。

最大余数可无状态调用：

```cpp
auto assignment = allocateLargestRemainder(healthy, slices, probe);
```

该函数要求传入有意义的旧score，自身不维护在途配额。也可通过`Scheduler(Policy::LargestRemainder)`使用统一预占接口；测试适配器选择无状态调用并由DeviceSelector维护旧配额。

v2的预计完成代价以排队/服务时间为主，NUMA只对新Slice增加有上限的偏好。容量采样要求连续提交burst或可见软件积压，避免把CQ可见延迟导致的posted重叠当成持续链路忙碌。

返回的weights仅作诊断：最大余数是旧分数份额，预计完成时间方案是首片代价倒数的归一化值，字节债务方案是慢目标份额。它们不是可跨算法直接等同的概率权重。性能比较应同时看实际字节分配、吞吐、尾时延、相对均衡和成本。

完整矩阵及调用约束见`../instrumentation/candidate-algorithms.md`，结果见`/root/mooncake-tent-multirdma-output/reports/candidate-algorithms-report.md`。当前是实验分支的可调用模块及测试接入，尚未替换生产默认策略或完成全部Worker/endpoint反馈接线。
